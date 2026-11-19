# RK3588 inference experiment

## Admission result, 2026-11-19

The ROCK 5B+ reported kernel 6.1.172-vendor-rk35xx, RKNPU v0.9.8, and an NPU render device at /dev/dri/renderD129. The debugfs load sample showed 0% on each of the three cores while idle. This identifies the hardware only.

The board reported 20,240,172 and 20,237,056 KiB MemAvailable. Both samples were below the existing 20 GiB admission floor (20,971,520 KiB). The active Portainer builder had Docker Memory and MemorySwap both set to 20,401,094,656 bytes (19 GiB). PR #10 specifies a 21 GiB builder. The host also had active zram. The experiment stopped before model load. No service, swap setting or Portainer limit was changed.

The [backend revision](https://github.com/Mojo24x7/rk-llama.cpp/tree/712f4de2de8e75f0ce2193779144bee1a4e35447) documents Q4_0 on Qwen3-30B-A3B-Instruct-2507. The [Coder Q4_0 candidate](https://huggingface.co/n00b001/Qwen3-Coder-30B-A3B-Instruct-Q4_0-GGUF/tree/6f416a66beacce7c45fce4a663b2a7d5ad00e0c0) is a separate qwen3moe artifact. Its publisher lists 17,304,492,352 bytes and SHA-256 cb55db3283b0b3790c8bf0b976ce0b09f64d182611e89703186c383c090825c4. Verify bytes and digest locally before use. Runtime correctness, fit, and NPU execution with this GGUF remain unknown. The coding profile stays on the pinned CPU Q3_K_L.

## Exact-model board probes, 2026-11-19

A later isolated run used an 18 GiB model cap plus the existing 1 GiB host reserve. The builder stayed under its live 19 GiB ceiling. The full Q4_0 file passed SHA-256 verification. The pinned backend compiled natively with `LLAMA_RKNPU2=ON`, `LLAMA_BUILD_UI=OFF` and `LLAMA_USE_PREBUILT_UI=OFF`. The embedded UI was unnecessary for the HTTP test. The bundled librknnrt.so digest was `d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8`.

Each server ran on CPUs 4–7 with 512 tokens of context, a private loopback endpoint, verified equal memory/memsw cgroup limits, swappiness zero, and 0.5-second host sampling. The arithmetic request processed 21 prompt tokens and generated three. These short runs used sequentially warmed file caches, so they cannot establish repair throughput or compare total RAM. Cgroup peaks can exclude already cached model pages and driver allocations.

| Q4_0 probe | Outcome | Inference samples with NPU activity | Peak sampled model cgroup |
| --- | --- | --- | ---: |
| CPU, offload disabled | Answered 42 | 0 of 4 | 2,063,523,840 bytes |
| RKNPU, one requested layer | Answered 42 | 0 of 8 | 9,069,359,104 bytes |
| RKNPU, one layer plus explicit final-block expert weights | Answered 42 | 0 of 5 | 1,844,834,304 bytes |
| Accelerator mode with final-block weights on RKNPU and remaining weights explicitly on CPU | OOMKilled during load, exit 137 | Never reached inference | 19,327,352,832 bytes |
| RKNPU, three requested layers | Graph initialization aborted, exit 133 | Never reached inference | 17,733,300,224 bytes |

The three-layer log identifies `cache_k_l46 (view)` in an RKNPU buffer whose backend cannot run `SET_ROWS`. A follow-up on an isolated test board can keep the KV cache on CPU with `--no-kv-offload` and retain the kernel-load and backend-dispatch measurements. Every sampled model swap counter was zero. Host zram occupancy nevertheless rose from about 0.37 to 2.45 GiB across the session. Further live-board experimentation stopped to preserve production headroom. There is no qualified hybrid profile or paired application benchmark.

The retained archive `inference-evidence.tar.gz` has SHA-256 `37bc163fcb7495c4717176b4ae9465b9bd82dba28a396aba3be17b97a3d3972a`. It contains the five probe configurations, scripts, server logs, states, samples, available responses and build evidence.

## Historical CPU observations

Both job manifests identify the pinned Coder Q3_K_L and a 17 GiB runtime. The controller digests differ. These records give historical context only.

| Existing job | Recorded outcome | Live calls / replayed turns | Recorded live call time | Prompt / generation tokens per second | Model cgroup peak |
| --- | --- | ---: | ---: | ---: | ---: |
| immich-3.4.2-7a6aae3b | Built, 38/38 image hardware cases passed | 1 / 30 | 1,529 s | 3.71 / 2.14 | 9,559,314,432 bytes |
| memories-11.1.2-6b3a7c6a | Historical replay failed on a closed connection | 2 / 0 | 346 s | 6.01 / 4.67 | Last retained peak 4,747,894,784 bytes; final snapshot failed |

Run `python3 benchmark_timings.py JOB/agent-*.jsonl` on one job at a time. Rates use total recorded tokens divided by total phase time. Call times exclude replayed reads, failed requests without a transcript entry, startup, tools and build time.

## Paired run procedure

1. Use an isolated builder with equal memory and memory-plus-swap limits of 21 GiB. Require model limits of 19 GiB with zero swap allowance, the usual host reserve and idle admission. Check effective cgroups, Docker settings, OOM state and swap counters. Preserve the Portainer stack.
2. Pin controller, backend build, runtime library, model hash, and both applications' upstream/fork commits. Save the protected tests and initial broken trees by digest. Show that each protected repair fails on the starting tree. Give each arm a fresh copy, the same prompt, blank replay history, context, sampling seed, tool and time budgets, and all eight active experts.
3. Before adding any runnable profile, qualify the exact Coder Q4_0 artifact in isolation. Record GGUF metadata, build options, runtime image digest, device enumeration, assigned operators and per-core NPU load before, during and after generation. Check for competing NPU users and repeat with offload disabled on the same Q4_0 artifact. Require attributable NPU work, valid model actions and bounded memory. An idle load reading or a successful CPU fallback cannot qualify offload.
4. Once qualified, compare Q3_K_L CPU and Q4_0 hybrid sequentially in alternating order. Repeat each app and arm at least twice. Time each whole job with a monotonic clock. Preserve transcripts, timing coverage, cold/warm cache policy, cgroup peaks, NPU samples, patch digests and independent test results, including failed attempts. Compare protected passes per attempt and time to a correct repair. Run the existing full source, build and exact-image hardware gates for both apps. Keep the timing comparison separate from the decision to approve a candidate.
