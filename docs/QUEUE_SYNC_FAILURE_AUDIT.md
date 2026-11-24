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


## Follow-up: fail closed on unbounded or unavailable QueueWaitIdle

- The Orbis path previously spun forever if the submitted EOP marker was never observed. It now uses a bounded poll budget and returns `VK_ERROR_DEVICE_LOST` if the marker remains absent. The budget is an iteration bound, not a wall-clock timeout or a hardware completion guarantee.
- QueueWaitIdle now rejects missing or undersized epilogue command storage instead of silently returning success without a completion marker.
- These guards prevent an infinite hang and false success. They do not solve the shared command-buffer lifetime, GPU-visible label coherency, or firmware 14.02 completion contract.


## Follow-up: descriptor-table pointer path is not yet GPU-safe

A separate inspection of the descriptor path found a concrete blocker relevant to compute dispatch:

- `vk_ps4_CmdBindDescriptorSets` handles immediate descriptors and the pointer usage types `PTR_CONSTBUFFERTABLE`, `PTR_RESOURCETABLE`, `PTR_SAMPLERTABLE`, `PTR_RWRESOURCETABLE` (and graphics also `PTR_VERTEXBUFFERTABLE`). It does not handle `PTR_INDIRECTRESOURCETABLE` (0x1c), `PTR_INDIRECTINTERNALRESOURCETABLE` (0x1d), or `PTR_INDIRECTRWRESOURCETABLE` (0x1e). The switch falls through to the default no-op, so a shader requiring the indirect table gets no corresponding user-data pointer.
- The Gate A compute artifact reports usage type 0x1c at user-data start register 2 and a two-SGPR indirect resource pointer. This makes the missing 0x1c path directly relevant to the current shader artifact, not merely a theoretical Vulkan feature.
- Descriptor table arrays in this backend are allocated with `vk_ps4_alloc_zero`. The default allocator in `vk_ps4_alloc` is host `malloc` / aligned allocation unless the application supplies an allocator. The binding code passes these array addresses to `sceGnmDrawCmdSetPointerUserData`.
- The pinned OpenGNM implementation of `sceGnmDrawCmdSetPointerUserData` copies the pointer value into the user-data register range; it does not allocate, map, or make the pointed-to host array GPU-resident. Therefore the current path does not establish a valid GPU address for these descriptor arrays.
- No speculative mapping or indirect-table layout was added. The implementation needs a proven GPU-visible allocation/address model and the exact indirect-table encoding before it can be corrected safely.

Evidence:
- Backend descriptor binding: `src/vk_ps4_descriptor.c`, graphics and compute usage-type switches.
- Backend default allocator: `src/vk_ps4_entry.c`, `vk_ps4_alloc`.
- Pinned OpenGNM pointer writer: https://github.com/PS4-OpenGNM/OpenGNM/blob/1f3c309fb149d534b4db9506c1efa106dc771896/src/drawcommandbuffer.c#L1510-L1534
- Pinned OpenOrbis usage enum previously audited: `PTR_INDIRECTRESOURCETABLE=0x1c`, `PTR_INDIRECTINTERNALRESOURCETABLE=0x1d`, `PTR_INDIRECTRWRESOURCETABLE=0x1e`.

Status: descriptor-table GPU residency and the Gate A 0x1c binding path remain blocked. Do not claim compute descriptor binding or hardware dispatch readiness.


## Follow-up: refine descriptor memory finding

The memory audit found that this backend does already contain a DirectMemory-backed Vulkan allocation path; the blocker is narrower than "there is no GPU memory mechanism":

- `vk_ps4_AllocateMemory` calls `sceGnmDirectMemoryAllocate` for the advertised Onion/Garlic memory types and retains a `GnmDirectMemory` handle.
- `vk_ps4_BindBufferMemory` derives a buffer descriptor base from `mem->gnm_mem.mapped + offset`. This establishes the intended GPU-visible path for Vulkan buffer resources within this backend's assumptions, although FW 14.02 behavior/coherency remains unqualified.
- Descriptor payload arrays are separate allocations: `alloc_binding_resources` uses `vk_ps4_alloc_zero` (the host allocator) for `GnmBuffer`, `GnmTexture`, and `GnmSampler` arrays. They are passed directly to `sceGnmDrawCmdSetPointerUserData`.
- Therefore a plausible engineering direction is to back descriptor payload arrays with dedicated `GnmDirectMemory` allocations and point user-data at their mapped GPU-visible addresses. This is not implemented: it still requires validating the correct memory type, alignment, lifetime through GPU consumption, cache behavior, and the exact table representation expected by each input-usage type. In particular, it does not by itself solve the missing indirect-table (0x1c/0x1d/0x1e) semantics.
- Do not equate the existing buffer allocation path with proof of retail firmware 14.02 GPU residency or cache coherence; no console readback test exists.

This refines the earlier finding: the backend has a DirectMemory primitive for Vulkan buffer allocations, but its descriptor-array storage does not use it.


## Follow-up: DirectMemory address and cache audit

A further source-level cross-check found a potentially more fundamental address-space inconsistency in the existing buffer path:

- OpenGNM's Orbis implementation calls `sceKernelAllocateDirectMemory` to obtain a kernel direct-memory offset, then passes that offset to `sceKernelMapDirectMemory` to obtain `mapped`, a process virtual mapping. It later passes `directmemory` to `sceKernelReleaseDirectMemory`. See `src/helpers.c` in OpenGNM.
- `vk_ps4_BindBufferMemory` passes `mem->gnm_mem.mapped + offset` to `sceGnmBufSetBaseAddress`; OpenGNM's `sceGnmBufSetBaseAddress` encodes the supplied pointer bits directly into the GNM descriptor.
- `vk_ps4_GetBufferDeviceAddress` previously returned `mem->gnm_mem.directmemory + offset`, i.e. the kernel allocation offset rather than the mapped pointer used by GNM descriptors. That made the two public paths inconsistent on Orbis (the host fallback masks this because it assigns the same value to both fields).
- The implementation now returns `mapped + offset`, matching the address passed to GNM buffer descriptors. This is a source-derived correction, not proof of retail firmware 14.02 behavior or a successful GPU access test.
- `vk_ps4_FlushMappedMemoryRanges` and `vk_ps4_InvalidateMappedMemoryRanges` are unconditional no-ops. Their comments assert Onion coherence / Garlic visibility, but this repository does not provide firmware-14.02 evidence or a hardware readback test establishing those cache guarantees.
- This is a source audit, not a claim that the mismatch has been reproduced on hardware. No speculative pointer cast or cache operation was added.

Reference: OpenGNM DirectMemory struct/API documentation: https://ps4-opengnm.github.io/opengnm/reference/helpers/


## Follow-up: indirect input-usage metadata evidence

Pinned OpenGNM source confirms:
- `GnmShaderInputUsageType` assigns `PTR_INDIRECTRESOURCETABLE=0x1c`, `PTR_INDIRECTINTERNALRESOURCETABLE=0x1d`, and `PTR_INDIRECTRWRESOURCETABLE=0x1e`.
- `sceGnmShaderInputUsageTypeSize()` classifies all three as two dwords, the same pointer-width class as the ordinary table-pointer usage types.
- The public header and shader-binary reference do not define the pointed-to indirect-table payload layout, nesting/indirection rules, or per-slot descriptor representation. The generic size helper only establishes the user-data pointer width; it does not establish the table contents.
- Therefore adding a switch case that simply passes the existing `GnmBuffer[]` / `GnmTexture[]` array would be an unsupported assumption. The missing 0x1c path remains deliberately unimplemented pending an authoritative layout or known-good shader/runtime example.

Pinned source references:
- `include/gnm_shader.h`: https://github.com/PS4-OpenGNM/opengnm/blob/7a920d7e0415edc6ff0e17b4a38a393809736dfe/include/gnm_shader.h
- `include/gnm_shaderbinary.h`: https://github.com/PS4-OpenGNM/opengnm/blob/11954c30baca7bdd61fb2f3cd271be6201a28e1b/include/gnm_shaderbinary.h
- `src/helpers.c` DirectMemory mapping: https://github.com/PS4-OpenGNM/opengnm/blob/898753cedc859d0d4b50113f8ab1626c9aa97500/src/helpers.c


## Follow-up: retail completion-notification candidate discovered (not yet qualified)

A broader cross-repository audit found a concrete candidate that was missed by the earlier statement that no platform completion-notification API had been identified:

- OpenGNM's published driver reference declares `sceGnmAddEqEvent(OrbisKernelEqueue, id, udata)` and `sceGnmDeleteEqEvent` for registering/removing GPU events on a kernel event queue. The same reference marks `sceGnmGetEqTimeStamp` as a retail stub, but does not mark Add/Delete as stubs.
- OpenOrbis `libkernel.h` declares `sceKernelCreateEqueue`, `sceKernelWaitEqueue`, `sceKernelDeleteEqueue`, and the `scePthreadMutexInit/Lock/Unlock/Destroy` family. This gives public declarations for an event queue and CPU-side serialization primitive; it does not by itself prove their behavior on firmware 14.02 or define the exact object ABI/initialization required by this backend.
- Public GNM command-buffer documentation confirms `sceGnmDrawCmdEventWriteEop` emits an EOP event with data to a GPU-visible address. The documentation retrieved does not specify the exact interrupt-select encoding needed to trigger the registered EQ event, nor does it prove that the event is delivered only after the write is globally visible on retail FW 14.02.
- Emulator and reverse-engineered runtime material describes GfxEop/graphics-core EQ notifications, but that is supporting research only, not retail firmware ABI proof.

This is a viable investigation path, not a completed fix. Before using it for command-buffer retirement, verify from an authoritative GNM packet definition or known-good retail sample: (1) the exact EOP interrupt-select bits and event ID, (2) whether the EQ notification is ordered after the EOP memory write, (3) event delivery on retail 14.02, (4) safe wait/timeout behavior, and (5) how multiple queues/events are distinguished. A kernel mutex can serialize CPU recording/submission, but it cannot replace the GPU completion event needed before reusing command storage.

References:
- OpenGNM driver API: https://ps4-opengnm.github.io/opengnm/reference/driver/#scegnmaddeqevent
- OpenGNM EOP command API: https://ps4-opengnm.github.io/opengnm/reference/drawcommandbuffer/#scegnmdrawcmdeventwriteeop
- OpenOrbis libkernel declarations: https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/blob/master/include/orbis/libkernel.h
- OpenOrbis GNM driver declarations: https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/blob/master/include/orbis/GnmDriver.h

No synchronization implementation was changed on the strength of this lead. The shared epilogue-buffer reuse remains unsafe until the retail completion contract is established and tested.


### EOP interrupt-select cross-check

The OpenGNM wrapper signature for `sceGnmDrawCmdEventWriteEop` exposes only event type, destination, data selector, and immediate value; it does not expose an interrupt-select argument. The OpenGNM type reference enumerates event types (including `GNM_CACHE_FLUSH_AND_INV_TS_EVENT`) but does not document a public way to request the GfxEop interrupt through this helper. Generic AMD EVENT_WRITE_EOP documentation describes INT_SEL packet bits, but that is not a PS4 retail ABI contract. Therefore the event-queue lead does not yet provide an implementable completion callback through the current wrapper. Do not patch packet bits manually until a PS4-specific authoritative definition or a known-good retail sample confirms the exact packet encoding and event ordering.

Additional research reference: https://ps4-opengnm.github.io/opengnm/reference/types/ (GnmEventType) and https://ps4-opengnm.github.io/opengnm/reference/drawcommandbuffer/#scegnmdrawcmdeventwriteeop.


### Known-good SDK sample pattern located

A public PS4 GNMX sample (Enderderder/SonyEngine/PS4Engine/PS4Engine/Source.cpp) provides a stronger design reference than the generic API declarations:

- Creates a kernel EOP event queue with sceKernelCreateEqueue and registers Gnm::kEqEventGfxEop through Gnm::addEqEvent.
- Allocates a separate GPU label for each render context and cycles through multiple contexts. A context is not reset/overwritten until its label indicates the GPU has freed it; while waiting, the CPU blocks on sceKernelWaitEqueue.
- Submits through GnmxGfxContext::submitAndFlipWithEopInterrupt, which explicitly writes the context label and triggers the EOP event queue. On shutdown it waits for all in-flight context labels before unregistering/deleting the event queue and freeing allocations.

This validates the architecture pattern (per-in-flight-context storage + GPU label + EOP event notification) with a real PS4 SDK-facing sample. It does not reveal the implementation of the proprietary GNMX helper or prove that the low-level OpenGNM wrapper can emit the same interrupt packet. In particular, the current sceGnmDrawCmdEventWriteEop wrapper has no interrupt-select parameter. The sample therefore narrows the implementation task: either expose/use a verified low-level EOP-interrupt packet builder, or adapt an available supported GNM submission helper; do not substitute the existing polling-only EOP path and assume it raises GfxEop.

Source: https://github.com/Enderderder/SonyEngine/blob/master/PS4Engine/PS4Engine/Source.cpp


## Fail-closed compute pipeline validation for indirect descriptor tables

The compute pipeline creation path now scans the parsed GNM input-usage slots and rejects shader binaries containing usage types 0x1c, 0x1d, or 0x1e. The descriptor binder does not implement these indirect-table payloads, and the public OpenGNM headers identify the usage types and pointer width but do not define a proven payload layout. Previously, such a pipeline could be created and later have the required binding silently skipped. It now returns the existing unsupported-feature failure path rather than presenting an incomplete pipeline as usable.

This is a safety boundary, not implementation of indirect descriptor tables. It applies to compute pipeline creation only; graphics pipeline validation remains to be audited and guarded separately. The source-contract test was extended to assert this rejection. The test has not yet been executed in a runtime, and no CMake build, CTest, OpenOrbis build, or PS4 hardware validation has been performed.

Evidence: OpenGNM documents the usage enum values in its shader reference: https://ps4-opengnm.github.io/opengnm/reference/shader/ (types 0x1c-0x1e).
