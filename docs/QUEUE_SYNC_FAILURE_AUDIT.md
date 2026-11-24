# Queue synchronization failure audit

Base audited: `f4d940b723771b3fe637e8ac0704bb5d6ab19622`

## Changes in this branch

- A failed `WAIT_REG_MEM` submission now returns `VK_ERROR_DEVICE_LOST`; the wait semaphore is not consumed and command buffers are not submitted.
- A failed command-buffer submission no longer CPU-signals the caller's fence.
- Failed semaphore/fence EOP submissions return `VK_ERROR_DEVICE_LOST`; neither path writes a GPU completion label from the CPU or marks the object signaled.
- Semaphore/fence signal generations advance before attempting EOP submission. This keeps an old label value from comparing equal after a failed attempt.
- Newly created unsignaled fences use generation 1 with label 0. Previously both were zero, so the existing equality check could report a never-submitted fence as complete.
- `QueueWaitIdle` propagates an EOP submission error.
- `DeviceWaitIdle` now calls `QueueWaitIdle` for every created queue handle (including multiple queues in one family) and propagates the first failure instead of waiting only on `queues[0]`.
- `tests/test_queue_sync_contract.py` adds source-level regression guards. CMake registers this check when a Python 3 interpreter is found.

## Validation performed

- Reviewed the edited source at the branch head.
- Ten original source-contract conditions were evaluated against the fetched branch contents; all passed. An additional condition now guards the all-queues `DeviceWaitIdle` behavior; the updated Python test has not been executed.
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


## Follow-up audit: no safe retirement primitive identified in this repository

A repository-wide path inventory and source review found no queue/mutex implementation or platform synchronization wrapper that can be reused for this fix. The queue structure has an opaque `submit_mutex` field, but the repository has no implementation/use site for it in the audited queue/device files. Therefore this branch cannot safely introduce a lock by guessing an Orbis mutex ABI.

Additional concrete hazards in the current source:

- `vk_ps4_QueueWaitIdle` writes the idle label into the final dword of the same device-wide epilogue allocation, then records/submits EOP into its beginning. Concurrent QueueSubmit/QueueWaitIdle calls can overwrite the same command storage and label without ownership or serialization.
- `vk_ps4_DeviceWaitIdle` previously waited only on `queues[0]`; the branch now iterates all created queue handles sequentially and propagates errors. This removes the single-queue omission, but does not resolve the shared epilogue-buffer lifetime/concurrency hazard described above.
- `vk_ps4_WaitForFences` treats `f->signaled` as sufficient even on Orbis, but `vk_ps4_GetFenceStatus` overwrites that result with the GPU-label comparison whenever a label exists. These two entry points therefore disagree after CPU-side signaling (for example, acquire/present paths) and need a unified, spec-backed state model.
- Timeline semaphores are parsed and exposed by the sync implementation, but QueueSubmit treats every semaphore as a binary semaphore and uses the binary `signal_value` label path. Timeline semaphore queue operations are not implemented by the current queue path; this must be rejected or implemented explicitly rather than silently treated as binary.

## Safe boundary

No additional synchronization code was changed in this follow-up. The repository does not currently provide an evidenced mutex primitive or a completion/retirement primitive for reusing the shared command buffer. The next implementation step must first select and verify the platform synchronization API and the GNM completion mechanism from authoritative SDK/header/runtime evidence, then add a per-submission command-buffer lifetime model and tests. Host-only source checks cannot qualify these hardware contracts.


## Follow-up implementation: all-queue DeviceWaitIdle

- Replaced the first-queue-only wait with a loop over all non-null entries in `dev->queues[0..queue_count)`.
- Each queue is passed to `vk_ps4_QueueWaitIdle`; the first non-`VK_SUCCESS` result is returned immediately.
- Added a source-contract assertion for iteration, per-queue waiting, and error propagation.
- This is a source-level correction only. It does not prove the EOP completion mechanism or shared command-buffer storage safe under concurrent QueueSubmit/QueueWaitIdle calls.


## Follow-up implementation: reject unsignaled host semaphore waits

- The host/no-GPU fallback previously had an empty `if (!sem->signaled)` branch and then cleared the semaphore state, silently allowing dependent work to continue without satisfying its wait.
- The fallback now returns `VK_ERROR_DEVICE_LOST` before changing semaphore state when an unsignaled wait has no GPU wait-packet path.
- Already-signaled host semaphores retain the synchronous CPU fallback behavior.
- Added a source-contract assertion that the unsignaled check and fail-closed return occur before semaphore consumption.
- This is not a replacement for proper asynchronous GPU semaphore waits. The Orbis path still needs correct generation consumption, command-buffer lifetime/retirement, and hardware validation.
- The updated Python source-contract test has not been executed in a runtime.


## Follow-up implementation: reject unsupported timeline queue operations

- `QueueSubmit` now preflights every wait/signal semaphore across the complete submit batch before issuing any GPU work.
- If any referenced semaphore is timeline-typed, the call returns `VK_ERROR_FEATURE_NOT_PRESENT`; the backend no longer silently treats a timeline semaphore as binary or partially executes earlier submits in the same batch.
- This is an explicit unsupported-feature boundary, not timeline semaphore support. Host timeline counter/signal entry points remain separately implemented and are not evidence of GPU queue integration.

## Follow-up implementation: consistent CPU/GPU fence status

- `GetFenceStatus` now accepts the CPU-side signaled flag before consulting the GPU label, matching the existing `WaitForFences` predicate.
- Previously it overwrote a true CPU-side state with a stale GPU label comparison, which could report a fence as not ready after CPU-side signaling paths.
- This consistency fix does not qualify the GPU label memory or EOP completion mechanism.


## API evidence: `sceGnmSubmitDone` is not a proven retirement fence

- The OpenOrbis public `GnmDriver.h` declaration documents `sceGnmSubmitDone()` as: "Tell the driver that all command buffers were submitted for this frame." It does not document GPU completion, command-buffer retirement, or a wait guarantee. Source: https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/blob/0a1aaf9dd4a92695538bdeb09fb056d06dd11725/include/orbis/GnmDriver.h
- Therefore, the existing `sceGnmSubmitDone()` call after the one-shot device-init command buffer cannot be repurposed as evidence that a submitted epilogue buffer is no longer being read by the GPU.
- shadPS4 and GPCS4 contain emulator-side implementations of GNM submission, but emulator scheduling/`WaitGpuIdle` behavior is not a retail PS4 firmware 14.02 contract. Those implementations are not used as proof of hardware retirement here.
- The queue source comments were corrected to stop describing QueueSubmit as serialized or the submit path as synchronously retired. The shared epilogue buffer remains a hard blocker; no speculative lock, SubmitDone-as-fence, or guessed firmware wait API was added.

### Evidence references

- OpenOrbis header (public declaration/comment): https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/blob/0a1aaf9dd4a92695538bdeb09fb056d06dd11725/include/orbis/GnmDriver.h
- shadPS4 emulation implementation (reference only, not hardware ABI proof): https://github.com/shadps4-emu/shadPS4/blob/5c3b6910b99ea4e4d019533490137fe80e400306/src/core/libraries/gnmdriver/gnmdriver.cpp
- GPCS4 emulation wrapper (reference only): https://github.com/Inori/GPCS4/blob/88480de36f983c66f7dd2c5951fead1a740d47a5/GPCS4/SceModules/SceGnmDriver/sce_gnm_submit.cpp
