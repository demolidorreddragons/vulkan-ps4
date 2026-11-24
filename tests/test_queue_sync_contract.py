#!/usr/bin/env python3
"""Source-level regression checks for fail-closed PS4 queue synchronization.

These checks do not emulate GNM or prove hardware completion. They guard
against reintroducing CPU-forged GPU completion after a failed submission.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
queue = (root / "src/vk_ps4_queue.c").read_text(encoding="utf-8")
sync = (root / "src/vk_ps4_sync.c").read_text(encoding="utf-8")
buffer = (root / "src/vk_ps4_buffer.c").read_text(encoding="utf-8")


def require(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)
    print("PASS: " + message)


def function_body(source, name, next_name):
    start = source.index(name)
    end = source.index(next_name, start)
    return source[start:end]


def failure_returns_device_lost(source, marker):
    start = source.index(marker)
    end = source.index("return VK_ERROR_DEVICE_LOST;", start)
    return end - start < 300


submit = function_body(queue, "vk_ps4_QueueSubmit(", "vk_ps4_QueueWaitIdle(")
wait_idle = function_body(queue, "vk_ps4_QueueWaitIdle(", "vk_ps4_DeviceWaitIdle(")
device_wait_idle = queue[queue.index("vk_ps4_DeviceWaitIdle("):]
create_fence = function_body(sync, "vk_ps4_CreateFence(", "vk_ps4_DestroyFence(")
get_fence_status = function_body(sync, "vk_ps4_GetFenceStatus(", "vk_ps4_CreateSemaphore(")

require(
    failure_returns_device_lost(submit, "WAIT_REG_MEM submit FAILED"),
    "failed WAIT_REG_MEM aborts QueueSubmit",
)
require(
    failure_returns_device_lost(submit, "sceGnmSubmitCommandBuffers FAILED"),
    "failed command-buffer submission aborts QueueSubmit",
)
command_error = submit[submit.index("sceGnmSubmitCommandBuffers FAILED"):]
command_error = command_error[:command_error.index("return VK_ERROR_DEVICE_LOST;")]
require(
    "f->signaled = true;" not in command_error
    and "*f->label =" not in command_error,
    "failed command-buffer submission does not forge fence completion",
)
sem_error = submit[submit.index("semaphore EOP submit FAILED"):]
sem_error = sem_error[:sem_error.index("return VK_ERROR_DEVICE_LOST;")]
require(
    "semaphore EOP submit FAILED" in submit
    and "return VK_ERROR_DEVICE_LOST;" in submit[submit.index("semaphore EOP submit FAILED"):]
    and "*sem->label =" not in sem_error,
    "failed semaphore EOP does not CPU-write its completion label",
)
fence_error = submit[submit.index("fence EOP submit FAILED"):]
fence_error = fence_error[:fence_error.index("return VK_ERROR_DEVICE_LOST;")]
require(
    "fence EOP submit FAILED" in submit
    and "return VK_ERROR_DEVICE_LOST;" in submit[submit.index("fence EOP submit FAILED"):]
    and "*f->label =" not in fence_error,
    "failed fence EOP does not CPU-write its completion label",
)
require(
    "sem->signal_value = next_signal_value;" in submit
    and submit.index("sem->signal_value = next_signal_value;") < submit.index("semaphore EOP submit FAILED"),
    "semaphore generation advances before EOP, preventing stale-label match",
)
require(
    "f->signal_value = next_signal_value;" in submit
    and submit.index("f->signal_value = next_signal_value;") < submit.index("fence EOP submit FAILED"),
    "fence generation advances before EOP, preventing stale-label match",
)
require(
    failure_returns_device_lost(wait_idle, "QueueWaitIdle: EOP submit FAILED"),
    "QueueWaitIdle propagates EOP submission failure",
)
require(
    "fence->signal_value = 1u;" in create_fence
    and "*fence->label = fence->signaled ? fence->signal_value : 0u;" in create_fence,
    "new unsignaled fences start at generation one with a zero label",
)

require(
    "for (uint32_t i = 0; i < dev->queue_count; i++)" in device_wait_idle
    and "vk_ps4_QueueWaitIdle((VkQueue)dev->queues[i])" in device_wait_idle
    and "if (result != VK_SUCCESS)" in device_wait_idle,
    "DeviceWaitIdle waits every created queue and propagates failures",
)

require(
    "if (!sem->signaled)" in submit
    and "unsignaled semaphore has no GPU wait path" in submit
    and submit.index("if (!sem->signaled)") < submit.index("sem->signaled = false;"),
    "host fallback rejects unsignaled waits before consuming semaphore state",
)

require(
    "#ifndef VK_USE_PLATFORM_PS4" in submit,
    "host-only no-GPU fallback remains compile-time isolated",
)

require(
    "sem->is_timeline" in submit
    and "VK_ERROR_FEATURE_NOT_PRESENT" in submit
    and submit.index("timeline semaphore waits are unsupported") < submit.index("for (uint32_t i = 0; i < submitCount; i++) {", submit.index("timeline semaphore waits are unsupported")),
    "timeline semaphore queue operations are rejected before GPU submission",
)
require(
    "bool done = f->signaled;" in get_fence_status
    and "if (!done && f->label)" in get_fence_status,
    "GetFenceStatus preserves CPU-signaled fences consistently with WaitForFences",
)

queue_wait_idle = function_body(queue, "vk_ps4_QueueWaitIdle(", "vk_ps4_DeviceWaitIdle(")
require(
    "!dev || !dev->gnm_epilogue_cmd || dev->gnm_epilogue_cmd_dwords < 8" in queue_wait_idle,
    "QueueWaitIdle fails closed when epilogue storage is absent or undersized",
)
require(
    "const uint32_t max_polls = 1u << 24;" in queue_wait_idle
    and "if (*idle_label != 1)" in queue_wait_idle
    and "return VK_ERROR_DEVICE_LOST;" in queue_wait_idle,
    "QueueWaitIdle uses a bounded completion poll and reports missing GPU progress",
)

device_address = buffer[buffer.index("vk_ps4_GetBufferDeviceAddress("):]
require(
    "buf->memory->gnm_mem.mapped" in device_address
    and "buf->memory->gnm_mem.directmemory" not in device_address,
    "buffer device address uses the same mapped VA as GNM buffer descriptors",
)

print("Queue synchronization and buffer-address source contract checks passed.")
