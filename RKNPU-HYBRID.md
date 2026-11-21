# RK3588 MoE execution policy — CPU-mapped experts with NPU-eligible dense operations

This document describes the **single** hybrid profile in the existing
`rkmpp-maintainer` service. It is opt-in, is not the default unattended update
profile, and never deploys updated Immich or Memories containers automatically.

## Decision and evidence

The model is Qwen3-Coder-30B-A3B-Instruct Q4_0, SHA-256
`cb55db3283b0b3790c8bf0b976ce0b09f64d182611e89703186c383c090825c4`
(17,304,492,352 bytes), using the already-pinned
[`Mojo24x7/rk-llama.cpp@712f4de`](https://github.com/Mojo24x7/rk-llama.cpp/tree/712f4de2de8e75f0ce2193779144bee1a4e35447).
The file is reused after hash validation; no duplicate 17 GB download.

On the user's 24 GB ROCK 5B+, the same real source-repair smoke passed with:
- CPU-only Q4 control: **15.21 prompt / 6.29 decode tok/s**.
- Previous RKNN profile, requesting `-ngl 2`: **15.13 / 4.41 tok/s**.
- Previous RKNN profile, requesting `-ngl 3`: **14.05 / 4.35 tok/s**.

These data **do not prove that two or three layers ran on the NPU**. This pinned
backend defaults to GGML's **ACCEL** device type, not GPU: the CLI explicitly
warns that `--gpu-layers` is ignored. ACCEL devices can still own and execute
supported operations through the normal CPU buffer candidate list. NPU
allocation probe success alone does not demonstrate that matmuls actually
executed there.

The attempt to put **MoE expert weights** on the NPU, even with one requested
layer, hit the **18 GiB model cgroup** and exited 137 (`OOMKilled=true`).
That was an inner container OOM, *not evidence that all 24 GB physical RAM was
consumed*. The builder is capped at **19 GiB**; raising the inner cap alone
would be dangerous. Do not repeat that approach.

The pinned backend's own
[benchmarks](https://github.com/Mojo24x7/rk-llama.cpp/blob/712f4de2de8e75f0ce2193779144bee1a4e35447/BENCHMARKS.md)
on **a different, non-Coder Qwen3-30B-A3B MoE** report a better practical
single-board strategy: CPU expert routing with memory-mapped, un-repacked
weights, and NPU-eligible dense matrices. Those measurements are a useful
architectural basis, **not** a performance guarantee for this Coder model,
24 GB board, kernel or workload. This fork documents that INT4 NPU matmuls
batch poorly; forcing Q4 weights into W8A8 may help dense prefill, but is
not a reason to duplicate the large routed expert tensors.

## One coherent, memory-bounded policy

`--model-profile hybrid` now uses:

- `--cpu-moe`: experts remain on the CPU, rather than attempting NPU
  requantization/allocation of the entire routed expert set.
- `--no-repack`: expert GGUF Q4_0 weights remain mmap-backed and reclaimable.
- `--fit off`: do not let llama.cpp's GPU-fit planner silently adjust placement;
  Docker cgroups still enforce model RAM and **no swap**.
- `--no-kv-offload`: host KV cache, avoiding unsupported RKNN KV writes.
- `RKNPU_HYBRID=W8A8_STANDARD`, `RKNPU_GLUE=1`: the same supported dense
  pipeline as previously, with eligible **non-expert** tensors offered to
  RKNPU's ACCEL backend.
- Exactly four A76 cores (`cpu_set=4-7`), `--threads 4` and flash attention.
  The model context remains 4096; expert routing is **not** reduced.
- Existing NPU driver/allocation probe via the board-verified
  `--device /dev/dri:/dev/dri` and `systempaths=unconfined`, without
  `--privileged` or custom `RKNPU_DOMAINS`.

There is **no** `-ngl` knob or fake NPU-layer counter. The hybrid runtime
records `moe_expert_placement=cpu-mmap`,
`npu_allocation_probe_passed=true`, and
`npu_execution_verified=false` until there is **independent** hardware
evidence. Default coding mode remains the previously validated CPU Q3_K_L;
`hybrid_cpu` remains the matched pure-CPU Q4_0 control compiled from the
same pinned fork with `-DLLAMA_RKNPU2=OFF`.

The model's 18 GiB memory-and-swap ceiling, DinD builder's **19 GiB** ceiling,
and the work-window 1 GiB host reserve are unchanged. The maintainer runs in
the existing Portainer stack. The deployment remains owner-controlled.

## One board qualification, no random parameter sweep

Build and recreate the maintainer image from the clean PR branch in Portainer
(**do not pull a remote image**, keep `MAINTAINER_ACTION=idle`), then execute:

```sh
docker exec rkmpp-maintainer-maintainer-1 python3 /opt/maintainer/maintainer.py smoke --model-profile hybrid --ignore-window
```

The smoke must pass agent source edit, protected tests and image export.
Inspect its `model.log` for the expected ACCEL/backend buffer placement,
actual RKNN matmul activity if reported, and absence of unexpected NPU expert
allocation, and its `model-runtime.json` for `OOMKilled=false`, no-swap
enforcement and peak memory. During inference, independently inspect
`/sys/kernel/debug/rknpu/load` for real hardware activity, where accessible.
A correct edit or successful preflight alone is insufficient evidence.

Compare `agent.jsonl` prompt/decode timing against the existing pure-CPU Q4
control, using the **same task, model hash, context and thread count**.
Include load cost separately from inference time. The repair fixture alone
does not validate general coding quality; manual review of genuine patch
output remains necessary. The target is to **beat** CPU-only Q4's
6.29 decode tok/s with coherent edits and lower or equal memory demand.
If it does not, **prefer the CPU profile**; stop experimenting with arbitrary
NPU layer counts or RAM increases.

Upstream examples sometimes use `-ngl 99` even though the ACCEL backend logs
the GPU-layers warning. This configuration intentionally avoids pretending
that the integer controls the ACCEL device's real offload amount.
