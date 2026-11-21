## Optional RK3588 CPU + NPU experiment (one existing stack)

`MODEL_PROFILE=hybrid` is an **opt-in** model profile for the existing
`rkmpp-maintainer` stack. The default `coding` profile remains CPU-only Qwen
Q3_K_L. No persistent container, second daemon, production deployment, or
automatic upgrade is introduced.

The hybrid profile uses the exact-hash Qwen3-Coder-30B-A3B-Instruct Q4_0 GGUF
tested on the board (SHA-256 `cb55db3283b0b3790c8bf0b976ce0b09f64d182611e89703186c383c090825c4`)
and the pinned [Rockchip llama.cpp backend](https://github.com/Mojo24x7/rk-llama.cpp/tree/712f4de2de8e75f0ce2193779144bee1a4e35447).
Only `Q4_0` was selected because the backend's NPU matmuls do not support
Q3_K_L. The 17 GiB CPU coding model is not silently substituted.

The **same** isolated Docker builder compiles `Dockerfile.rknpu2` once if its
image is absent. Before loading the ~17 GB Qwen model, it launches a small
temporary RKNN probe using the **same** `/dev/dri` device-group mapping
and a narrowly scoped `systempaths=unconfined` setting on the temporary
NPU model/probe only, allowing the RKNN runtime to inspect SoC system paths.
No direct `/proc` bind mount is attempted (modern runc rejects those). The probe must create an RKNN matmul context
**and allocate actual NPU tensor memory**. This replaces the previous
`--list-devices` check, which incorrectly reported success when the
runtime could not open the NPU driver. A failed probe aborts quickly, **before the 17 GB GGUF checksum**, with a
specific error rather than failing after model loading.

Only after the probe passes does it validate the pinned Qwen model's checksum
and launch it with `--device /dev/dri`, requesting **two NPU layers** with
`-ngl 2 --no-kv-offload`, and pins four A76 cores. It applies a
262,144 file-descriptor limit to avoid the previously observed RKNN fd
exhaustion. Docker enforces **18 GiB combined RAM+swap** for the model inside
the existing **19 GiB** isolated builder and verifies live cgroup limits.
An inaccessible NPU stops the hybrid request; successful device enumeration alone is not considered proof of NPU availability.
CPU mode can always be selected again without rebuilding the stack.

If present, `/workspace/experiments/coder-rknpu2-20261119/coder-q4.gguf` is
verified by size and SHA-256 and reused **without duplicating the 17 GB file**.
Otherwise the publisher-pinned GGUF is downloaded and verified in
`/workspace/models`. Keep the 1 GiB admission reserve and do not change
system-wide swap/zram or the builder ceiling.

**Board qualification before enabling unattended updates:**

1. Check that the existing builder exposes `/dev/dri/renderD129`. Do not
   install new services or alter the host kernel.
2. With the Portainer maintainer set to idle and no concurrent model jobs,
   open its Console and run:
   `python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid --ignore-window`.
   The manual override skips the night schedule, **not** memory admission.
3. Confirm `model-runtime.json` records enforced no-swap cgroup, backend
   `rknpu2`, `npu_layers_requested=2`, and device detection. Independently
   check actual RKNPU kernel-load samples during inference and model logs for
   hardware matmuls. **Device detection is not proof of NPU computation.**
4. Confirm protected source-repair tests pass. Then benchmark the same Q4_0 GGUF
   and workload on CPU only:
   `python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid_cpu --ignore-window`
   The `hybrid_cpu` control builds the **same pinned llama.cpp source** with
   `-DLLAMA_RKNPU2=OFF`, checks that no RKNPU device is advertised, passes
   **no** `/dev/dri` devices and runs `-ngl 0 --no-kv-offload`. It reuses the
   same model bytes, context, thread count, sampling, CPU affinity and memory limits.
   Because the backend compilation differs (necessarily), this is a matched
   model/workload comparison, not byte-identical executable performance.
   Compare completed wall-clock time and correctness. Never run both simultaneously.
   Only then consider selecting `MODEL_PROFILE=hybrid` for ordinary watcher runs.

**Known limitation:** The Q4_0 hybrid model, usable context length,
NPU utilisation, protected-repair success, and net throughput are **not yet
qualified in the live maintainer**. The first on-board test may hit the
18 GiB ceiling; in that case stop testing and keep `coding` rather than
raising the nested limit or compromising your other services.
The on-board model's cgroup settings must be checked separately even though
the previously tested lightweight Alpine container enforced its limits.


## November 20 nested-NPU startup regression

The first hybrid smoke (`smoke-974c5fa3`) failed during model startup with
`failed to open rknpu module`, `failed to open rknn device`, and
`Failed to allocate tensor memory via RKNN API`. The runtime enumerated the
compiled backend but was unable to allocate NPU memory. This was a preflight
coverage bug, **not** evidence that the model needed more RAM.

The correction gives only the short-lived NPU container visibility into
Rockchip's system paths and adds a direct RKNN allocation probe. It has not yet been validated on the ROCK 5B+ through the
nested Docker daemon. If it fails, inspect the immediate probe error and the
availability of `/sys/firmware/devicetree/base/compatible` inside the builder.
Do not disable safety limits, give the model full `--privileged` access, or
touch the host driver simply to bypass an initialization error. The rest of the maintainer remains CPU mode
by default.

## November 21 on-board DRM device isolation

The ROCK 5B+ reports `/dev/dri/renderD129` as the `RKNPU` driver on both
the host and existing Docker-in-Docker builder. Nevertheless, the nested
`rknpu-probe` failed to create an RKNN matmul context with only
`--device /dev/dri/renderD129`, even when seccomp was disabled or
`CAP_SYS_ADMIN` was added. The **same RKNN probe succeeded** when the
nested container received `--device /dev/dri`, without `--privileged`.
Adding `/dev/dma_heap` also succeeded but was not necessary for the probe.

Therefore the NPU-only path now uses `--device /dev/dri` for **both** the
temporary allocation probe and the model. This gives the NPU workload access
to the DRM device group, including non-NPU DRM nodes, but is much narrower
than `--privileged`. CPU profiles remain unchanged. The allocation probe
now runs **before** hashing/downloading the 17 GB GGUF. Successful small
allocation proves driver access, **not** full-model success, correct coding
repair, or a speedup; those require the subsequent paired on-board benchmark.

## November 21 Q4 CPU baseline failure and correction

The first matched `hybrid_cpu` smoke (`smoke-fc7813b6`) failed before coding,
because the previous baseline used an RKNPU-enabled binary with `-ngl 0`.
That pinned fork **still initialized an RKNN allocation** even with zero layers,
and with no DRM passthrough the model aborted. Giving the CPU baseline NPU
permissions would invalidate the benchmark.

The corrected control builds the exact same pinned `rk-llama.cpp` revision with
`LLAMA_RKNPU2=OFF` into a separately tagged image
(`local/rkmpp-llama-cpu:712f4de-control1`) **within the existing builder**.
It verifies that the CPU binary does not enumerate an RKNPU backend before it
checksums/loads the existing 17 GB model. The NPU path and default CPU coding
Q3 profile remain unchanged. The 18 GiB model and 19 GiB builder ceilings remain.

Successful compilation and CI do not establish a successful large Q4 CPU
inference run: test on the ROCK 5B+ before interpreting timing results.

## Experimental NPU core/layer tuning (November 21)

The pinned RK3588 backend already selects **all three NPU cores (0, 1, 2)**
when `RKNPU_CORES` is absent, as it is in our container. NPU core count
and `-ngl` decoder-layer count are independent controls; requesting additional
layers does **not** turn on an additional NPU core. Verify actual per-core
hardware load rather than treating requested cores or layers as measured use.

The controller remains on its usual two-layer hybrid configuration and the
standard CPU coding profile remains unchanged. A **manual smoke only**
experiment can now request 3 or 4 offloaded layers without altering either
profile, the 18 GiB no-swap model cap, the 19 GiB builder cap, or the
pinned GGUF/image:

```sh
docker exec rkmpp-maintainer-maintainer-1 python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid --npu-layers 3 --ignore-window
docker exec rkmpp-maintainer-maintainer-1 python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid --npu-layers 4 --ignore-window
```

Run experiments **sequentially**, checking the previous run's
`model-runtime.json` for real cgroup headroom. The override is
validated against the original `0..4` RKNN safety bound; requesting 5+
fails before launching anything. Both tests retain standard source-repair,
runtime metrics, protected behavioral checks and no-swap enforcement.

### Why adding layers did not help, and the distinct MoE experiment

On the ROCK 5B+ with the same pinned Q4_0 coding model and repair fixture:
- CPU-only: 15.21 prompt tok/s, **6.29 decode tok/s**.
- NPU 2 layers: 15.13 prompt tok/s, **4.41 decode tok/s**.
- NPU 3 layers: 14.05 prompt tok/s, **4.35 decode tok/s**.

This is a negative result for simply increasing dense-layer offload. Qwen3-Coder
30B-A3B is a Mixture-of-Experts model: expert matmuls are `GGML_OP_MUL_MAT_ID`,
and the pinned backend explicitly **does not** move these to the NPU unless
`RKNPU_MOE` is set. This is a more meaningful bottleneck to test.

The backend includes expert batching during prefill, but expert weight
requantization and NPU execution are *not* qualified for this 17.3 GB model.
Do **not** enable `RKNPU_MOE` for unattended `watch`, `once`, or
`run` jobs. The optional `--npu-moe` switch is accepted only for
`smoke --model-profile hybrid`, with no persistent default changes. It
sets `RKNPU_MOE=1` plus prefill diagnostic logs
(`RKNPU_MOE_DBG=1`) on the transient NPU model only. A one-layer option
allows first testing the smallest expert memory footprint:

```sh
docker exec rkmpp-maintainer-maintainer-1 python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid --npu-layers 1 --npu-moe --ignore-window
```

**Caution:** Expert acceleration is experimental in this pinned fork.
Enabling it can lead to model startup failure, increased RKNN DMA allocations,
slow generation, crashes, or poor output quality; the model remains constrained
to the existing 18 GiB no-swap cgroup, with 19 GiB builder ceiling. No
`RKNPU_DOMAINS` override is used—upstream warns that custom domains with
concurrent NPU models can trigger kernel panic. Never run two experiments
simultaneously or while another NPU-heavy workload is running. Inspect
`model-runtime.json` and the model log, and monitor actual NPU core load
during inference (where readable: `/sys/kernel/debug/rknpu/load`).

The smoke result also records `npu_moe_evidence`: a count of
`[moe-batch]` backend log lines and whether the model log was captured.
This confirms the *expert-batching code path* was entered during prefill,
not NPU core execution or numerical correctness. No dispatch evidence means
the experiment did not establish an active MoE offload.

If the one-layer trial fails or memory use is excessive, stop—not increase
RAM or offload layers. If it completes correctly with materially better
decode/prompt throughput and safe memory headroom, compare a **matched**
one-layer run without `--npu-moe` to isolate the expert acceleration effect.
Only then consider 2-layer MoE. Full correctness requires more than the
small source-repair smoke; keep the automatic maintenance default unchanged.
