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
    "#ifndef VK_USE_PLATFORM_PS4" in submit,
    "host-only no-GPU fallback remains compile-time isolated",
)

print("Queue synchronization source contract checks passed.")
