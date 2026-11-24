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
