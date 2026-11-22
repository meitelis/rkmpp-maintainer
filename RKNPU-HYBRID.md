# RK3588 Qwen3-Coder runtime decision (21 November 2026)

## Qualified default: CPU-only Q4_0 at 16K

The default `coding` profile uses Qwen3-Coder-30B-A3B-Instruct
**Q4_0**, from the pinned SHA-256
`cb55db3283b0b3790c8bf0b976ce0b09f64d182611e89703186c383c090825c4`
(17,304,492,352 bytes). It reuses the existing board GGUF without
duplication. **The executable is the exact pinned
[Mojo24x7/rk-llama.cpp@712f4de](https://github.com/Mojo24x7/rk-llama.cpp/tree/712f4de2de8e75f0ce2193779144bee1a4e35447)
compiled with `LLAMA_RKNPU2=OFF`.** No NPU access is granted to its model
container. `hybrid_cpu` remains a matched CPU Q4 diagnostic at 4K context.

Model settings: 16,384-token context, `--threads 4`, CPUs 4–7,
`--threads-batch 4`, 128/64 batch/ubatch, flash attention, Q8_0
K/V cache, **18 GiB inner model RAM limit, no swap**, **19 GiB
builder limit, no swap**, and a further 1 GiB available-host-RAM reserve.
These limits and Portainer ownership are unchanged. The full output cap for
coding jobs is 4096 tokens.

## Evidence collected on 24 GiB ROCK 5B+

All rows below used the **same exact Q4_0 GGUF** and completed the protected
`go-vod/http_options.py` repair test correctly. Model-cgroup peak was
captured on the device. No row was OOM-killed.

| job / model profile | context | prompt tok/s | generation tok/s | peak model GiB |
| --- | ---: | ---: | ---: | ---: |
| `smoke-af64bc20`, Q4 CPU | 4096 | 15.21 | 6.29 | 16.50 |
| `smoke-9c7b3557`, Q4 CPU | **16384** | **14.94** | **6.23** | **17.13** |
| `smoke-27d9bd01`, original hybrid | 4096 | 15.13 | 4.41 | 17.76 |
| `smoke-72e4627b`, CPU-mapped experts + RKNN | 4096 | 10.55 | 4.33 | 16.24 |

A separate NPU-expert attempt, `smoke-15e053bb`, was killed on model
startup by its 18 GiB *container* cgroup limit (exit 137). The builder
also has a separate 19 GiB ceiling; this is **not** a reason to raise
limits or to blame other services for exhausting host RAM.

Q4 16K's observed generation throughput is 0.95% below Q4 4K for the
**short smoke workload**, with 0.63 GiB higher peak memory. This result
**does not measure** decoding at a full 16K context or prove unattended
Immich/Memories upgrades will succeed. Watch actual RAM during those
longer tasks.

## Why the experiments were stopped

The pinned backend exposes RKNPU as a GGML **ACCEL** device by default,
not a GPU. Its `--gpu-layers/-ngl` argument reported that GPU layers were
ignored. Thus the historical requests for 2 or 3 offloaded layers do
**not** prove different real NPU layer counts. An RKNN allocation probe
confirms driver access, not executed model matmuls. The full repair
workflow and generation-speed comparison were the deciding metrics.

The upstream backend has hardware restrictions: the RK3588 matmul API
supports symmetric W4A4, W8A8 and W16A16, **not W4A16**, and its own
single-board [benchmarks](https://github.com/Mojo24x7/rk-llama.cpp/blob/712f4de2de8e75f0ce2193779144bee1a4e35447/BENCHMARKS.md)
report CPU-mapped expert weights as a better strategy than forcing all
MoE experts onto the NPU for single-token generation. Those results were
on a related, but *different*, Qwen3-30B MoE model.

**PR [#17](https://github.com/meitelis/rkmpp-maintainer/pull/17) and
PR [#18](https://github.com/meitelis/rkmpp-maintainer/pull/18)
were closed without merge after board testing.** Experimental `hybrid`
is retained for diagnosis and rollback, but is not the production default.
Do not introduce new NPU offload, expert requantization, GPU classification,
or swap-limit experiments without a concrete backend implementation change
and a paired performance/correctness result.

The goal remains **Detect → Compare → Reconcile → Repair → Test → Export**:
candidate images only; the owner deploys via Portainer.
