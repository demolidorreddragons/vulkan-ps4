/*
 * vk_ps4_queue.c — VkQueue implementation via sceGnmSubmitCommandBuffers.
 *
 * vkQueueSubmit submits recorded command buffers to the GPU.  After the
 * user's command buffers, if a fence or signal semaphores are present, an
 * EOP event write is appended to the device's epilogue command buffer and
 * submitted so the GPU writes a signal_value to each sync object's label
 * when all preceding work completes.  WaitForFences polls those labels.
 *
 * Wait semaphores emit a WAIT_REG_MEM packet into the epilogue command
 * buffer and submit it before the user's command buffers, so the GPU blocks
 * until the signaling submit's EOP write completes.  On host builds where
 * sceGnmSubmitCommandBuffers is a no-op, the CPU-side signaled flag is the
 * fallback (safe because QueueSubmit is serialized).
 */

#include "vk_ps4_internal.h"

#include <string.h>

/* Emit an EOP event write into the epilogue command buffer that writes
 * signal_value to the GPU label at gpuaddr.  Returns the number of bytes
 * written to the epilogue buffer. */
static uint32_t vk_ps4_queue_emit_eop(
    VkPs4Device *dev, uint64_t gpuaddr, uint32_t signal_value
) {
    GnmCommandBuffer cmd = sceGnmCmdInit(
        dev->gnm_epilogue_cmd,
        dev->gnm_epilogue_cmd_dwords * sizeof(uint32_t),
        NULL, NULL
    );
    /* CACHE_FLUSH_AND_INV_TS_EVENT flushes all caches and writes the
     * immediate value to gpuaddr when the GPU reaches this point. */
    sceGnmDrawCmdEventWriteEop(
        &cmd, GNM_CACHE_FLUSH_AND_INV_TS_EVENT, gpuaddr,
        GNM_DATA_SEL_SEND_DATA32, (uint64_t)signal_value
    );
    return (uint32_t)(cmd.cmdptr - cmd.beginptr) * sizeof(uint32_t);
}

/* Emit a WAIT_REG_MEM packet into the epilogue command buffer that blocks
 * the GPU until the label at gpuaddr equals refval.  Returns the number of
 * bytes written.  This is used for wait-semaphore GPU synchronization. */
static uint32_t vk_ps4_queue_emit_waitmem(
    VkPs4Device *dev, uint64_t gpuaddr, uint32_t refval
) {
    GnmCommandBuffer cmd = sceGnmCmdInit(
        dev->gnm_epilogue_cmd,
        dev->gnm_epilogue_cmd_dwords * sizeof(uint32_t),
        NULL, NULL
    );
    /* Wait until the GPU label equals refval.  Mask 0xffffffff compares
     * all 32 bits. */
    sceGnmDrawCmdWaitMem(
        &cmd, GNM_WAIT_REG_MEM_FUNC_EQUAL, gpuaddr, refval, 0xffffffff
    );
    return (uint32_t)(cmd.cmdptr - cmd.beginptr) * sizeof(uint32_t);
}

VKAPI_ATTR VkResult VKAPI_CALL
vk_ps4_QueueSubmit(VkQueue queue, uint32_t submitCount, const VkSubmitInfo *pSubmits, VkFence fence) {
    if (!queue) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    VkPs4Queue *q = (VkPs4Queue *)queue;
    VkPs4Device *dev = q->device;
    vk_ps4_log("QueueSubmit: count=%u family=%u", submitCount, q->family_index);

    for (uint32_t i = 0; i < submitCount; i++) {
        const VkSubmitInfo *submit = &pSubmits[i];

        /* Wait semaphores — emit GPU WaitMem packets into the epilogue
         * command buffer and submit them before the user's command buffers.
         * This makes the GPU block until each wait semaphore's label matches
         * its signal_value, providing real GPU-side synchronization.
         *
         * On host builds (no epilogue buffer or no-op submit), only an
         * already-signaled CPU-side semaphore can be consumed. An unsignaled
         * wait cannot block asynchronously here and therefore fails closed. */
        for (uint32_t w = 0; w < submit->waitSemaphoreCount; w++) {
            VkPs4Semaphore *sem = (VkPs4Semaphore *)submit->pWaitSemaphores[w];
            if (!sem) continue;

            if (dev && dev->gnm_epilogue_cmd && sem->label) {
                /* GPU wait: emit WaitMem into epilogue and submit it. */
                uint64_t label_addr = (uint64_t)(uintptr_t)sem->label;
                uint32_t wait_bytes = vk_ps4_queue_emit_waitmem(
                    dev, label_addr, sem->signal_value
                );
                void *wait_addr = dev->gnm_epilogue_cmd;
                int32_t wait_result = sceGnmSubmitCommandBuffers(
                    1, &wait_addr, &wait_bytes, NULL, NULL
                );
                if (wait_result != 0) {
                    /* A failed GPU wait cannot be treated as satisfied.
                     * Preserve the binary semaphore state for diagnosis
                     * and fail the submission closed. */
                    vk_ps4_log("QueueSubmit: WAIT_REG_MEM submit FAILED: %d",
                               wait_result);
                    return VK_ERROR_DEVICE_LOST;
                }
            } else {
                /* Host fallback: check CPU-side signaled flag. */
                if (!sem->signaled) {
                    /* No GPU wait packet exists in this build, so an
                     * unsignaled semaphore cannot be consumed safely.
                     * Fail closed rather than silently executing work. */
                    vk_ps4_log("QueueSubmit: unsignaled semaphore has no GPU wait path");
                    return VK_ERROR_DEVICE_LOST;
                }
            }
            /* Reset the semaphore after waiting (binary semaphore). */
            sem->signaled = false;
        }

        /* Submit each command buffer */
        for (uint32_t c = 0; c < submit->commandBufferCount; c++) {
            VkPs4CommandBuffer *cmd = (VkPs4CommandBuffer *)submit->pCommandBuffers[c];
            if (!cmd || !cmd->pm4_buffer) continue;

            uint32_t cmd_size_bytes = cmd->pm4_used * sizeof(uint32_t);
            if (cmd_size_bytes == 0) continue;

            void *dcb_addr = cmd->pm4_buffer;

            /* Submit to GNM — pass NULL for ccb_addrs and ccb_sizes
             * since we don't use a separate constant command buffer. */
            vk_ps4_log("QueueSubmit: sceGnmSubmitCommandBuffers dwords=%u",
                       cmd->pm4_used);
            int32_t result = sceGnmSubmitCommandBuffers(
                1, &dcb_addr, &cmd_size_bytes, NULL, NULL
            );

            if (result != 0) {
                vk_ps4_log("QueueSubmit: sceGnmSubmitCommandBuffers FAILED: %d",
                           result);
                /* Failed submission is not completion.  Leave the fence
                 * unsignaled and its GPU label untouched. */
                return VK_ERROR_DEVICE_LOST;
            }
        }

        /* Signal semaphores — emit EOP writes to their GPU labels.
         * Clear the CPU-side signaled flag before emitting the EOP so
         * that WaitForFences/Semaphore doesn't short-circuit on a stale
         * value from a previous signal (e.g., AcquireNextImageKHR). */
        for (uint32_t s = 0; s < submit->signalSemaphoreCount; s++) {
            VkPs4Semaphore *sem = (VkPs4Semaphore *)submit->pSignalSemaphores[s];
            if (!sem) continue;
            uint32_t next_signal_value = sem->signal_value + 1;
            sem->signal_value = next_signal_value;
            sem->signaled = false;
            if (dev && dev->gnm_epilogue_cmd && sem->label) {
                uint64_t label_addr = (uint64_t)(uintptr_t)sem->label;
                uint32_t eop_bytes = vk_ps4_queue_emit_eop(
                    dev, label_addr, next_signal_value
                );
                void *eop_addr = dev->gnm_epilogue_cmd;
                int32_t result = sceGnmSubmitCommandBuffers(
                    1, &eop_addr, &eop_bytes, NULL, NULL
                );
                if (result != 0) {
                    /* Failed EOP submission is not a signal.  Do not write
                     * the completion label from the CPU.  The generation
                     * remains advanced so the previous label cannot match. */
                    vk_ps4_log("QueueSubmit: semaphore EOP submit FAILED: %d",
                               result);
                    return VK_ERROR_DEVICE_LOST;
                }
#ifndef VK_USE_PLATFORM_PS4
                /* Host submit is a no-op; there is no GPU to signal. */
                sem->signaled = true;
                *sem->label = sem->signal_value;
#endif
            } else {
                /* No epilogue buffer (host) — CPU-side signaling. */
                sem->signaled = true;
                if (sem->label) *sem->label = sem->signal_value;
            }
        }
    }

    /* Signal fence — emit an EOP write to the fence's GPU label.
     * Clear the CPU-side signaled flag before emitting the EOP so
     * WaitForFences doesn't short-circuit on a stale value from a
     * previous signal (e.g., AcquireNextImageKHR sets signaled=true). */
    if (fence) {
        VkPs4Fence *f = (VkPs4Fence *)fence;
        uint32_t next_signal_value = f->signal_value + 1;
        f->signal_value = next_signal_value;
        f->signaled = false;
        if (dev && dev->gnm_epilogue_cmd && f->label) {
            uint64_t label_addr = (uint64_t)(uintptr_t)f->label;
            uint32_t eop_bytes = vk_ps4_queue_emit_eop(
                dev, label_addr, next_signal_value
            );
            void *eop_addr = dev->gnm_epilogue_cmd;
            int32_t result = sceGnmSubmitCommandBuffers(
                1, &eop_addr, &eop_bytes, NULL, NULL
            );
            if (result != 0) {
                /* Failed EOP submission is not completion. */
                vk_ps4_log("QueueSubmit: fence EOP submit FAILED: %d", result);
                return VK_ERROR_DEVICE_LOST;
            }
#ifndef VK_USE_PLATFORM_PS4
            /* Host submit is a no-op; there is no GPU to signal. */
            f->signaled = true;
            *f->label = f->signal_value;
#endif
        } else {
            /* No epilogue buffer (host) — CPU-side signaling. */
            f->signaled = true;
            if (f->label) *f->label = f->signal_value;
        }
    }

    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL
vk_ps4_QueueWaitIdle(VkQueue queue) {
    if (!queue) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    VkPs4Queue *q = (VkPs4Queue *)queue;
    /* With the EOP fence mechanism, QueueWaitIdle should wait for the last
     * submitted fence.  Since QueueSubmit is serialized and the GPU processes
     * command buffers in order, the last EOP write completing means all
     * prior work is done.  For now, we rely on the synchronous submit model
     * — sceGnmSubmitCommandBuffers queues work but the EOP label is polled
     * by WaitForFences.  QueueWaitIdle has no fence to poll, so we submit a
     * no-op EOP write and poll it. */
    VkPs4Device *dev = q->device;
    if (dev && dev->gnm_epilogue_cmd) {
        /* Use the last dword of the epilogue buffer as the idle label.
         * The EOP packet (~6 dwords) is written at the start of the buffer,
         * so it won't overlap the label at the end. */
        volatile uint32_t *idle_label =
            (volatile uint32_t *)((uintptr_t)dev->gnm_epilogue_cmd +
                (dev->gnm_epilogue_cmd_dwords - 1) * sizeof(uint32_t));
        *idle_label = 0;
        uint64_t label_addr = (uint64_t)(uintptr_t)idle_label;
        uint32_t eop_bytes = vk_ps4_queue_emit_eop(dev, label_addr, 1);
        void *eop_addr = dev->gnm_epilogue_cmd;
        int32_t result = sceGnmSubmitCommandBuffers(
            1, &eop_addr, &eop_bytes, NULL, NULL
        );
#ifdef VK_USE_PLATFORM_PS4
        if (result != 0) {
            vk_ps4_log("QueueWaitIdle: EOP submit FAILED: %d", result);
            return VK_ERROR_DEVICE_LOST;
        }
        if (result == 0) {
            /* Poll until the GPU writes the label. */
            while (*idle_label != 1) {
                /* spin */
            }
        }
#else
        /* Host build: sceGnmSubmitCommandBuffers is a no-op, so the GPU
         * never writes the label.  Set it ourselves to avoid an infinite
         * loop — on host there is no real GPU work to wait for. */
        (void)result;
        *idle_label = 1;
#endif
    }
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL
vk_ps4_DeviceWaitIdle(VkDevice device) {
    if (!device) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    VkPs4Device *dev = (VkPs4Device *)device;

    /* Every queue handle represents a separately created queue, including
     * additional queues from the same family.  Waiting only on queues[0]
     * can return while work remains pending on another queue.  Drain each
     * queue in sequence and propagate the first failure. */
    for (uint32_t i = 0; i < dev->queue_count; i++) {
        if (!dev->queues[i]) {
            continue;
        }
        VkResult result = vk_ps4_QueueWaitIdle((VkQueue)dev->queues[i]);
        if (result != VK_SUCCESS) {
            return result;
        }
    }
    return VK_SUCCESS;
}
