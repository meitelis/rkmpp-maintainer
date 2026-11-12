# RKMPP maintainer

A **Portainer-managed Docker stack** for the 24 GB ROCK 5B+ that watches official Immich and Memories releases, adapts your RKMPP integrations with a local model, tests the result and exports candidate images. Application deployment stays manual.

The Radxa installation belongs under **`/armbian/rkmpp-maintainer`**. Deploy, update, start and stop its services in **Portainer** using [portainer-stack.yml](portainer-stack.yml). The root `compose.yml` is for development and CI, not the Radxa deployment.

This repository maintains both [memories-rkmpp](https://github.com/meitelis/memories-rkmpp) and [immich-rkmpp](https://github.com/meitelis/immich-rkmpp). Original source provenance is recorded in [SOURCE.md](SOURCE.md).

## Folder layout

```text
/armbian/rkmpp-maintainer/
  portainer-stack.yml    Portainer deployment definition
  config.json           Pinned model and runtime settings
  data/
    models/             Downloaded GGUF weights
    jobs/               Candidate source, images, logs and results
    monitor/            Release-monitor checkouts
    state.json          Attempted release records
  builder/              Isolated Docker daemon's images and build cache
```

Both persistent mounts are absolute paths under `/armbian`; they do not depend on Portainer's own checkout directory. The stack refuses missing host paths instead of silently using empty directories.

## Migrate the earlier home-directory installation

Let any running smoke or historical-upgrade job finish first. Then run this **one-time preparation** from the legacy checkout:

```sh
cd ~/rkmpp-maintainer
git pull --ff-only
sudo bash scripts/migrate-to-portainer.sh
```

The script refuses an active job, a different source path or an existing `/armbian/rkmpp-maintainer` destination. It stops only the old external maintainer project, moves the complete checkout and downloaded workspace to `/armbian/rkmpp-maintainer`, copies the stopped builder's cache into `builder/` with its metadata, and builds the local controller image. The original cache volume is retained for rollback. It does not deploy a replacement stack through the CLI.

If interrupted after the move, retain both copies of the builder cache and inspect the reported error before retrying preparation. Do not deploy Portainer against a partial cache copy. Existing Memories and Immich stacks are outside this migration.

## New installation preparation

If no legacy installation exists, prepare the directory and image on the Radxa:

```sh
cd /armbian
git clone https://github.com/meitelis/rkmpp-maintainer.git
cd rkmpp-maintainer
mkdir -p data builder
docker build -t local/rkmpp-maintainer:prototype-3-portainer .
```

Building the image is a host preparation step. Portainer owns the running services.

## Deploy in Portainer

In the Radxa Docker Standalone environment, choose **Stacks → Add stack → Repository**:

| Setting | Value |
| --- | --- |
| Stack name | `rkmpp-maintainer` |
| Repository URL | `https://github.com/meitelis/rkmpp-maintainer` |
| Reference | `refs/heads/main` |
| Compose path | `portainer-stack.yml` |
| `MAINTAINER_ACTION` | `idle` initially |
| `AUTO_UPDATE` | `false` initially |
| `DIND_STORAGE_DRIVER` | `overlay2` unless the existing builder used another driver |

Disable image re-pulling for the locally built maintainer image. It is `local/rkmpp-maintainer:prototype-3-portainer` with `pull_policy: never`; this stack file has no build directive. Enter variables in Portainer or load the existing `.env` file there; Portainer does not automatically read `/armbian/rkmpp-maintainer/.env`.

Deploy the stack. Its control should show **Total**, with a healthy builder and running maintainer. No host Docker socket or host ports are mounted/published by this stack. The builder remains privileged, as in the tested prototype, and is intended for these trusted source repositories.

## Run tests from Portainer

With `MAINTAINER_ACTION=idle`, open the maintainer container's **Console**, select `/bin/sh`, and connect. The idle service does not own the job lock, so Console jobs can run without fighting a background monitor.

First validate failure handling with the actual isolated daemon, then run the model repair:

```sh
python3 /opt/maintainer/maintainer.py runtime-check
python3 /opt/maintainer/maintainer.py smoke
```

Coding uses **Qwen3.8-27B Q4_K_M** (17,442,399,968 bytes), pinned to Bartowski's exact revision and SHA-256. The Radxa profile selects cores 4–7 (the four faster cores), four inference/prompt threads, a 16K context, Q8 KV caches with flash attention, bounded batching and an 18 GiB memory/swap ceiling. Actual peak memory and speed must be measured on the board before qualification. The model unloads before compilation. The publisher's thinking sampler is temperature 1.0, top-p 0.95 and top-k 20; reasoning effort is medium with a bounded reasoning budget.

The **smoke control** remains Ornith-1.5-9B Q5_K_M so ordinary ARM64 CI can validate the controller within its smaller RAM allowance. `--model-profile coding` runs that same protected exercise with Qwen on the Radxa. Model-specific settings and identities are recorded separately; a control-model CI pass does not qualify the larger model.

The smoke exercise contains a broken HTTP-option helper that checks only ffprobe support. The model must inspect the local file and its reference and repair the source so that options require support from both FFmpeg and ffprobe. Five protected behavioral tests cover mismatched capabilities, absent help, exact tokens, order and whitespace before image build/export. Untouched source fails. Success reports `kind: real-local-model-smoke`, `source_edit_verified: true` and `status: ready`; this alone is not release-upgrade qualification.

```sh
python3 /opt/maintainer/maintainer.py smoke --model-profile coding --wait-for-window
python3 /opt/maintainer/repair_check.py --model-profile coding --wait-for-window
```

The coding model sources are [Qwen's official card](https://huggingface.co/Qwen/Qwen3.8-27B) and the [pinned GGUF file](https://huggingface.co/bartowski/Qwen3.8-27B-GGUF/blob/f7f782271ce686458a3702c5d9f30d9c1354087c/Qwen3.8-27B-Q4_K_M.gguf).

For the historical Memories upgrade:

```sh
python3 /opt/maintainer/maintainer.py run --repo memories --target 11.1.2 --base-ref d6a9471dfbd3dd721bd67698c062ddcc773e232c --source-version 11.1.1
```

This starts from the actual pre-upgrade fork, reconciles upstream changes, lets Ornith inspect/adapt the candidate, then runs browser, Go race/vet/build, PHP and deployment checks before building/exporting the ARM64 engine. Results remain in `data/jobs/`. `prepare` runs only reconciliation; `run --no-agent` skips model adaptation for diagnosis.

The coding loop keeps an observation ledger even when old file contents leave its context. Unchanged repeated pages, diffs and file lists are flagged; a successful source edit resets the progress budget. After two duplicates or twelve actions without an edit, and during the final two turns, the model must complete a missing required observation, edit already-read source, complete its review, or report a specific blocker. During this restricted phase, observation tools may only access the exact outstanding required paths; unrelated reads remain forbidden. Required file and diff observations include every paginated line, with explicit continuation; a truncated first page cannot complete a required observation. An impossible minimum review budget fails before generation. Finishing requires explicit `review_complete=true`, empty blockers and the controller's required observations. Paths are scoped by tool: for example, Memories' `lib/Service/BinExt.php` is an **upstream** protocol file, not a file expected inside the local helper. An admitted pause or pending investigation fails the job even if the action was named `finish`. The prose contradiction check is conservative and cannot verify arbitrary natural-language claims.

Successful upgrade jobs now report **`status: built`**, **`review_status: pending-human-review`** and **`deployment_approved: false`**. Exit code zero means the checks/build/export completed, not that deployment was approved. `agent-review.json` records the model's self-report, actual observations and edit count. `ready` is reserved for the isolated smoke exercise. Historical replays are labeled `kind: historical-replay`: their deliberately old starting point may omit later fixes from your current branch, so they are not automatic replacements for the live service. Earlier job evidence is retained with its original result format.

Jobs have a six-hour deadline across all repair rounds, a 2 GiB free-space reserve and build/export space preflights. Timeouts remove named transient check containers; recovery records interrupted jobs and reaps only this maintainer’s labeled orphan containers under its exclusive lock. `run-manifest.json` records controller source identity, model/runtime digests, configuration and limits. Failed smoke jobs retain structured results and errors. See [qualification requirements](QUALIFICATION.md).

Model profiles resolve their own runtime and sampler settings. Each request records numeric token/timing counters and elapsed time; each model run records Docker/cgroup memory evidence before cleanup. Internal reasoning is not retained. NPU operation remains unvalidated.

## Overnight admission

New heavy jobs are admitted only **00:00–08:00 Europe/Dublin**, with daylight-saving changes handled by the timezone database. An active job may finish after 08:00; another job must wait until the next window. Before admission, the board must be sufficiently idle: sampled CPU at most 25%, normalized load at most 0.3 and available RAM greater than the selected model ceiling plus 1 GiB headroom. Busy jobs retry admission after five minutes. The builder's low CPU share prioritizes other services when they become active.

`watch` applies this gate to each candidate separately; outside the window it still performs lightweight release checks and wakes for the next opening. Manual heavy commands also respect the gate: `--wait-for-window` queues the command, while `--ignore-window` is an explicit manual/CI override. Waiting does not hold the maintenance job lock. Scheduling is enforced on the Radxa and continues independently of the laptop.

## Enable monitoring in Portainer

After testing, update the stack environment in Portainer to `MAINTAINER_ACTION=watch`. Keep `AUTO_UPDATE=false` for release checks every six hours. Once real upgrade tests pass, set `AUTO_UPDATE=true` in Portainer to prepare candidates sequentially for new stable releases.

Each release is attempted once automatically; failed-job evidence is retained. Android-only tags, prereleases and drafts are ignored. The monitor does not merge, publish or deploy. Use **Logs** in Portainer to monitor it. For an explicit Console job, switch the service back to `idle` between jobs; do not interrupt a running upgrade by redeploying it.

## Update the maintainer

Pull the source and rebuild its local image under `/armbian`:

```sh
cd /armbian/rkmpp-maintainer
git pull --ff-only
docker build -t local/rkmpp-maintainer:prototype-3-portainer .
```

Then update/redeploy the **existing stack in Portainer**, keeping its environment settings and disabling image re-pulling. Run validation from its Console. Do not use `docker compose up`, `down`, or `run` to manage the Portainer deployment. The one-time migration above is the exception that removes the legacy external project.

## Candidate images and publication

Built jobs save `image.tar`, `result.json`, source patches, review evidence and logs. Memories also exports `memories-helper.tar.gz`. A build and a model completion claim do not validate real RKMPP, browser, HDR or audio playback. Candidates need human source review and hardware testing before manual deployment through the existing application stacks in Portainer.

An explicit `run ... --publish` can create a draft PR in the relevant fork when a privately supplied GitHub token has the necessary contents, pull-request and workflow permissions. The token is not passed to model or build subprocesses. The background Portainer stack deliberately receives no token. No registry upload or automatic production deployment is implemented.

## Development and CI

`compose.yml` uses a separate relative workspace and named builder volume for development/CI. These commands are for an isolated development checkout:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py
docker compose config --quiet
docker compose -f portainer-stack.yml config --quiet
bash -n scripts/migrate-to-portainer.sh
docker compose run --build --rm maintainer smoke
```

CI runs the full Linux suite and real pinned-model smoke on ARM64. Source checks cover protected edits, credential isolation, process bounds, locking, reasoning/action separation, runtime limits, idle mode and a three-way upgrade fixture. CI first runs the full Linux checks, then actual daemon/PHP failure checks and the real model repair; downloadable evidence excludes model weights and image archives. A CI smoke pass is not a claim that the historical upgrade or hardware playback passed on the Radxa. Old Qwen2.5 results remain evidence for that older configuration.

Sources: [Portainer stack deployment](https://docs.portainer.io/user/docker/stacks/add), [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server), [Ornith model](https://huggingface.co/ornith-ai/Ornith-1.5-9B), [pinned GGUF](https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF/tree/abdd624b12ebf020b767fff532ff44fe552b28c3).
