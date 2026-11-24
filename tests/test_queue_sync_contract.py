#!/usr/bin/env python3
"""Source-level regression checks for fail-closed PS4 queue synchronization.

These checks do not emulate GNM or prove hardware completion. They guard
against reintroducing CPU-forged GPU completion after a failed submission.
"""
from pathlib import Path
import re
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


submit = function_body(queue, "vk_ps4_QueueSubmit(", "vk_ps4_QueueWaitIdle(")
wait_idle = function_body(queue, "vk_ps4_QueueWaitIdle(", "vk_ps4_DeviceWaitIdle(")
create_fence = function_body(sync, "vk_ps4_CreateFence(", "vk_ps4_DestroyFence(")

require(
    re.search(r"WAIT_REG_MEM submit FAILED[\\s\\S]*?return VK_ERROR_DEVICE_LOST;", submit) is not None,
    "failed WAIT_REG_MEM aborts QueueSubmit",
)
require(
    re.search(r"sceGnmSubmitCommandBuffers FAILED[\\s\\S]*?return VK_ERROR_DEVICE_LOST;", submit) is not None,
    "failed command-buffer submission aborts without signaling its fence",
)
require(
    re.search(r"semaphore EOP submit FAILED[\\s\\S]*?return VK_ERROR_DEVICE_LOST;", submit) is not None,
    "failed semaphore EOP returns device-lost",
)
require(
    re.search(r"fence EOP submit FAILED[\\s\\S]*?return VK_ERROR_DEVICE_LOST;", submit) is not None,
    "failed fence EOP returns device-lost",
)
require(
    "sem->signal_value = next_signal_value;" in submit
    and submit.index("sem->signal_value = next_signal_value;") < submit.index("semaphore EOP submit FAILED"),
    "semaphore generation advances before attempting EOP, so stale label cannot match",
)
require(
    "f->signal_value = next_signal_value;" in submit
    and submit.index("f->signal_value = next_signal_value;") < submit.index("fence EOP submit FAILED"),
    "fence generation advances before attempting EOP, so stale label cannot match",
)
require(
    re.search(r"QueueWaitIdle: EOP submit FAILED[\\s\\S]*?return VK_ERROR_DEVICE_LOST;", wait_idle) is not None,
    "QueueWaitIdle propagates EOP submission failure",
)
require(
    "fence->signal_value = 1u;" in create_fence,
    "new unsignaled fences start at generation one, distinct from zero label",
)
require(
    "fence->label = (volatile uint32_t *)fence->label_mem.mapped;\\n    *fence->label = fence->signal_value;"
    in create_fence,
    "initial fence label matches only the signaled initial state",
)
require(
    "#ifndef VK_USE_PLATFORM_PS4" in submit,
    "host-only no-GPU fallback remains compile-time isolated",
)

print("Queue synchronization source contract checks passed.")
