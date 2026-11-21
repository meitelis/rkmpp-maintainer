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
temporary RKNN probe using the **same** `/dev/dri/renderD129` device mapping
and read-only Rockchip SoC device-tree compatibility file at
`/proc/device-tree/compatible`. The probe must create an RKNN matmul context
**and allocate actual NPU tensor memory**. This replaces the previous
`--list-devices` check, which incorrectly reported success when the
runtime could not open the NPU driver. A failed probe aborts quickly with a
specific error rather than failing after the entire model load.

Only after the probe passes does it launch the model with
`--device /dev/dri/renderD129`, requesting **two NPU layers** with
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
4. Confirm protected source-repair tests pass. Then test the same Q4_0 GGUF,
   same backend binary, context and workload on CPU only:
   `python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid_cpu --ignore-window`
   The `hybrid_cpu` control uses `-ngl 0` with no NPU device. Compare
   completed wall-clock time and correctness. Never run both simultaneously. Only then consider
   selecting `MODEL_PROFILE=hybrid` for ordinary watcher runs.

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

The correction adds the Rockchip container device-tree mount and a direct RKNN
allocation probe. It has not yet been validated on the ROCK 5B+ through the
nested Docker daemon. If it fails, inspect the immediate probe error and the
availability of `/sys/firmware/devicetree/base/compatible` inside the builder.
Do not disable safety limits, use `--privileged` on the model, or touch the host
driver simply to bypass the error. The rest of the maintainer remains CPU mode
by default.
