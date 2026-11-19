# RK3588 inference experiment

## Admission result, 2026-11-19

The ROCK 5B+ reported kernel 6.1.172-vendor-rk35xx, RKNPU v0.9.8, and an NPU render device at /dev/dri/renderD129. The debugfs load sample showed 0% on each of the three cores while idle. This identifies the hardware only.

The board reported 20,240,172 and 20,237,056 KiB MemAvailable. Both samples were below the existing 20 GiB admission floor (20,971,520 KiB). The active Portainer builder had Docker Memory and MemorySwap both set to 20,401,094,656 bytes (19 GiB). PR #10 specifies a 21 GiB builder. The host also had active zram. The experiment stopped before model load. No service, swap setting or Portainer limit was changed.

The [backend revision](https://github.com/Mojo24x7/rk-llama.cpp/tree/712f4de2de8e75f0ce2193779144bee1a4e35447) documents Q4_0 on Qwen3-30B-A3B-Instruct-2507. The [Coder Q4_0 candidate](https://huggingface.co/n00b001/Qwen3-Coder-30B-A3B-Instruct-Q4_0-GGUF/tree/6f416a66beacce7c45fce4a663b2a7d5ad00e0c0) is a separate qwen3moe artifact. Its publisher lists 17,304,492,352 bytes and SHA-256 cb55db3283b0b3790c8bf0b976ce0b09f64d182611e89703186c383c090825c4. Verify bytes and digest locally before use. Runtime correctness, fit, and NPU execution with this GGUF remain unknown. The coding profile stays on the pinned CPU Q3_K_L.

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
