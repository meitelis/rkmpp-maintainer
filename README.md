# RKMPP maintainer

A Docker stack for the 24 GB ROCK 5B+ that watches official Immich and Memories releases, reconciles your RKMPP forks, asks a local coding model to adapt source, runs checks and exports candidate images. Deployment stays manual.

This is a prototype to test on the board, not a claim that a small model can maintain every future release. Start with the real-model smoke test, then the historical Memories replay. No GitHub token is needed for these tests.

This standalone repository maintains both [memories-rkmpp](https://github.com/meitelis/memories-rkmpp) and [immich-rkmpp](https://github.com/meitelis/immich-rkmpp). It was extracted from the original Memories prototype; [SOURCE.md](SOURCE.md) records the original source commits and authorship.

## Get the standalone stack

On the ROCK 5B+:

```sh
git clone https://github.com/meitelis/rkmpp-maintainer.git
cd rkmpp-maintainer
```

All commands below run from this repository's root. Linux and Docker Compose are required. This is its own stack, separate from your existing Memories and Immich stacks. Copying or cloning it does not start monitoring; the default monitoring configuration does not build updates until you enable `AUTO_UPDATE`.

## What runs

- **Maintainer:** a Python controller with a bounded JSON file-editing agent. It reads upstream diffs and edits source, with three repair rounds of up to 24 tool turns. Tests, workflows, build commands and the maintainer itself are protected from model edits.
- **Local model:** a transient llama.cpp container, loaded during editing and removed before tests/builds. Coding and smoke both use **Ornith-1.5-9B Q5_K_M** (6.64 GB weights plus context/runtime), pinned to the publisher's exact revision and SHA-256 in `config.json`. The model container has a **12 GiB memory limit**, four CPU threads, and a **16K context**. The limit is a ceiling, not a measured memory requirement or a limit on the whole Docker stack.
- **Builder:** a separate Docker-in-Docker daemon. It has its own images/cache and no host Docker socket. The controller refuses a daemon without its dedicated label. DinD is privileged and is not a hardened boundary against hostile repositories; use it for these trusted source trees.

No production container is updated. Candidates are saved as `data/jobs/<job>/image.tar`, alongside `result.json`, logs and a patch. Memories jobs also save the matching `memories-helper.tar.gz`.

## First check on the Radxa

Run from this directory. Docker Compose is required.

```sh
docker compose run --build --rm maintainer smoke
```

This pulls the pinned images, downloads the 6.64 GB coding model, asks it to fix a broken addition function, unloads it, runs immutable tests, builds a small Docker image, and exports the image. It uses neither your GitHub write credentials nor your running application stacks. Success ends with `"kind": "real-local-model-smoke"` and `"status": "ready"`; `result.json` identifies the model filename and checksum. An external `MODEL_ENDPOINT` is marked separately in that result.

The model uses the publisher's coding sampling settings: temperature 0.6, top-p 0.95 and top-k 20. Thinking is enabled but capped at 1,024 tokens per turn by the pinned llama.cpp server. A 4,096-token output limit leaves room for the final action. The server separates thinking into `reasoning_content`; only the final schema-constrained JSON action enters the editing loop or its transcript. Empty, truncated or unparsed-thinking responses fail the job. These budgets can be adjusted in `config.json` after measuring the board.

### Update an existing maintainer checkout

```sh
git pull --ff-only
docker compose run --build --rm maintainer smoke
```

This builds `local/rkmpp-maintainer:prototype-2-ornith` and tests the selected model. Existing `.env`, jobs, and cached models stay in place; the new weights use a different filename from the old Qwen2.5 cache. If the monitor was already running, recreate it with `docker compose up -d --build maintainer` after smoke succeeds. That retains your existing `AUTO_UPDATE` setting.

If nested overlay2 is unavailable on the host, set `DIND_STORAGE_DRIVER=vfs` in `.env` and recreate the builder. VFS needs substantially more disk space. Data and the nested image cache stay in this stack's directories/volume.

## Replay the previous Memories upgrade

After smoke passes:

```sh
docker compose run --rm maintainer run --repo memories --target 11.1.2 --base-ref d6a9471dfbd3dd721bd67698c062ddcc773e232c --source-version 11.1.1
```

This starts from your actual pre-upgrade main, reads official 11.1.1 and 11.1.2, reconciles go-vod changes and version contracts, lets the 9B model inspect/adapt the candidate, then runs browser, Go race/vet/build, PHP and deployment checks before building/exporting the ARM64 engine. The coding model is cached after the smoke test. The result remains local.

For diagnosis, `prepare` performs just cloning/reconciliation, and `run --no-agent` tests deterministic reconciliation and builds. Neither substitutes for evaluating the local model's ability to resolve a future incompatible change.

## Enable monitoring

```sh
cp .env.example .env
docker compose up -d --build
docker compose logs -f maintainer
```

The default `AUTO_UPDATE=false` checks releases every six hours but does not invoke the model or build upgrades. After the board tests pass, set `AUTO_UPDATE=true` in `.env` and recreate the maintainer. It then prepares candidates sequentially when an official stable `x.y.z` release is newer than your fork's advertised baseline. Android-only tags, prereleases and drafts are ignored.

Each version is attempted once automatically. A failed job retains its errors and does not keep hammering the model. To retry after reviewing a failure, use an explicit `run --repo ... --target ...`. Existing ready jobs are not overwritten. Nothing merges main or deploys.

## Optional draft PRs

After a candidate passes all local checks, an explicit `--publish` can create a new branch and draft PR in the relevant fork:

```sh
# GITHUB_TOKEN is set privately in the host shell, not pasted into the command.
docker compose run --rm -e GITHUB_TOKEN maintainer run --repo memories --target VERSION --publish
```

Use a fine-grained token limited to the two forks, with contents and pull-request write permissions. Workflow write permission is also needed when the trusted version synchronizer updates a workflow's version assertion. The token is not sent to the model or inherited by build/test subprocesses. A publication error retains the local image and appears in `result.json`. The background monitor deliberately does not receive a token by default.

No registry upload is implemented in this first version. Load a successful exported image into the host daemon yourself with `docker load -i data/jobs/JOB/image.tar`, then deploy it through your existing Portainer stack using the exact tag from `result.json`. For Memories, also install the matching helper and ensure the official app and engine protocol agree.

## Project-specific behavior

Memories uses three-way merges for the upstream `go-vod/` changes and synchronizes the explicit app/protocol version guards. The separate helper must also be reviewed against upstream player hooks. Existing historical playback measurements are not relabeled as results for the new version.

Immich reconciles changed upstream files into your `overlays/`, pins the target official source commit/runtime digest, updates pnpm's declared version and runs the existing source, Docker server/web and candidate-FFmpeg fragment checks. A changed official build layout, deleted integrated file, protected-test conflict or unsupported package manager can require manual work. Failed attempts do not produce a ready result.

A compiled image and passing source tests do not establish real RKMPP, browser or perceptual playback. Candidate builds run without VPU devices in the isolated builder; test playback when you choose to deploy.

## NPU and alternative local inference

The reference backend is CPU llama.cpp. Set `MODEL_ENDPOINT` to another local compatible server to evaluate an alternative backend. It must support `/v1/chat/completions`, schema-constrained JSON, the generation settings in `config.json`, and separation of thinking from final action text. External servers manage their own model memory and reasoning budget; the maintainer cannot unload or limit them. Ornith's hybrid Qwen3.5 architecture and NPU operation are not validated with RKLLM by this prototype.

## Development and evidence

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py
docker compose config --quiet
```

Unit tests cover release selection, token isolation, job locking, process timeouts, file/symlink restrictions, protected checks, bounded malformed-model output, reasoning/action separation, runtime limits and a three-way upgrade fixture. The CI workflow builds the controller on ARM64 and runs the actual pinned 9B-model smoke in the isolated daemon. Check the workflow result for the specific commit before claiming that Docker/LLM smoke has passed. The simple edit/build/export fixture validates integration; it does not establish real upgrade reliability, Radxa inference speed, or completed historical replay with Ornith. Previous Qwen2.5 validation remains historical evidence for that older configuration.

Data and caches can grow with builds. Keep the result folders/images you need and remove obsolete ones deliberately; this prototype does not delete old job evidence automatically.

Sources: [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server), [Ornith model and coding settings](https://huggingface.co/ornith-ai/Ornith-1.5-9B), [pinned publisher GGUF](https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF/tree/abdd624b12ebf020b767fff532ff44fe552b28c3), [Rockchip RKLLM](https://github.com/airockchip/rknn-llm).
