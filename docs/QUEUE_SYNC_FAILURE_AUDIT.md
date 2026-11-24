# Queue synchronization failure audit

Base audited: `f4d940b723771b3fe637e8ac0704bb5d6ab19622`

## Changes in this branch

- A failed `WAIT_REG_MEM` submission now returns `VK_ERROR_DEVICE_LOST`; the wait semaphore is not consumed and command buffers are not submitted.
- A failed command-buffer submission no longer CPU-signals the caller's fence.
- Failed semaphore/fence EOP submissions return `VK_ERROR_DEVICE_LOST`; neither path writes a GPU completion label from the CPU or marks the object signaled.
- Semaphore/fence signal generations advance before attempting EOP submission. This keeps an old label value from comparing equal after a failed attempt.
- Newly created unsignaled fences use generation 1 with label 0. Previously both were zero, so the existing equality check could report a never-submitted fence as complete.
- `QueueWaitIdle` propagates an EOP submission error.
- `tests/test_queue_sync_contract.py` adds source-level regression guards. CMake registers this check when a Python 3 interpreter is found.

## Validation performed

- Reviewed the edited source at the branch head.
- Ten source-contract conditions were evaluated against the fetched branch contents; all passed.
- The Python test itself has not been executed in a local runtime.
- No host CMake build, CTest execution, OpenOrbis build, or PS4 hardware test has been performed.

## Remaining blockers — do not treat this backend as hardware-qualified

1. The device owns one shared `gnm_epilogue_cmd` buffer. QueueSubmit submits packets from this buffer and then reuses it for subsequent wait/signal packets. The source does not establish that the GPU has finished consuming a submitted buffer before it is overwritten.
2. The same device-wide epilogue buffer is reachable from queues in more than one queue family. The queue struct contains a `submit_mutex` field, but the audited queue/device code does not use or initialize it. Concurrent submissions from distinct queues therefore need a separately designed serialization/lifetime solution.
3. The PS4-specific EOP/WAIT_REG_MEM packet behavior, GPU label memory visibility, cache coherency, and firmware 14.02 ABI are not validated by these host/source checks.
4. The previously identified indirect resource-table binding gap and GPU-visible descriptor allocation/readback gaps remain outside this change.

These remaining issues require evidence-backed implementation and runtime validation. This document does not claim successful GPU dispatch or PS4 compatibility.


## Additional source findings (not fixed in this PR)

### Shared epilogue command-buffer lifetime

- `VkPs4Device` owns exactly one `gnm_epilogue_cmd` allocation. The device initializer allocates it once and the device teardown releases it.
- Both `vk_ps4_queue_emit_waitmem()` and `vk_ps4_queue_emit_eop()` initialize command recording at the same `dev->gnm_epilogue_cmd` address.
- `vk_ps4_QueueSubmit()` submits that address for a wait packet, then can immediately rewrite it for later signal packets or the fence packet. The submit call returning success does not, in the inspected source, prove the GPU has finished fetching/consuming those command bytes.
- The same allocation is device-wide, not queue-owned. `vk_ps4_CreateDevice()` creates queue handles for every requested queue; it does not initialize `queue->submit_mutex`, and no lock operation appears in the audited queue path.
- Consequently, adding a mutex alone would serialize CPU writers but would not prove that the GPU has finished reading the buffer before the next writer overwrites it. A safe implementation needs an evidence-backed in-flight command-buffer lifetime/retirement scheme, plus cross-queue serialization and validated completion/readback. This PR deliberately does not invent that mechanism.

### Binary semaphore consumption

- The wait path emits `WAIT_REG_MEM` against the semaphore's current `signal_value`, then only clears the CPU `signaled` flag; it does not advance or otherwise consume the GPU label generation.
- A later wait can therefore compare against the same already-written label value. The host fallback also contains an empty `if (!sem->signaled)` branch and proceeds instead of rejecting or correctly deferring an unsatisfied wait.
- Correct binary semaphore consumption requires separate, well-defined signaled/pending generations and queue-order semantics. This is not safely repaired by changing one comparison or incrementing `signal_value` because that same field is also used by signal packets and subsequent waits.

### Next acceptance work

1. Design command-buffer storage with explicit ownership per in-flight submission and a proven retirement event; test multiple queues and overlapping submits.
2. Define binary semaphore state transitions separately from GPU label values; cover wait-before-signal, signal-wait-signal, repeated waits, multiple submits, and failed submissions.
3. Only then implement the design with the platform's verified synchronization primitives and validate it on the target firmware/hardware.
