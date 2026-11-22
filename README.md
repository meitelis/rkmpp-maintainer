# RKMPP maintainer

A **Portainer-managed, local RK3588 release maintainer** for
[memories-rkmpp](https://github.com/meitelis/memories-rkmpp) and
[immich-rkmpp](https://github.com/meitelis/immich-rkmpp).
It prepares update candidates; **you deploy running applications manually**.

## Contract

`Detect → Compare → Reconcile → Repair → Test → Export`

1. Detect newer **stable** upstream Memories/Immich releases against each fork's `main`.
2. Reconcile known RKMPP integration files with official changes; use the local
   coding model for bounded source-only repair. The model has file tools, **not a shell**.
3. Run independent source/build checks and board-specific MPP/RGA/playback tests.
   Retry a failed repair up to the configured limit; preserve evidence if it fails.
4. Export a verified Docker `image.tar`, matching metadata and (for Memories)
   the Nextcloud helper. Optionally open a GitHub PR for the reviewed candidate.
5. **Stop.** Never modify the running Immich or Memories stack, and never
   automatically merge a PR. After source review, manual deployment and actual
   client playback checks, the owner may explicitly accept that exact image/PR.

The Git-visible candidate source (tracked and unignored untracked files) is\nfingerprinted after independent checks and rechecked after build, hardware\ntests, export, and before PR publication. Generated ignored caches are not\npart of this fingerprint; the independently tested Docker image ID and\nexport archive checksum remain required. Any detected source mutation fails\nthe candidate instead of silently publishing changed source.\n\nA passing LLM report, Docker build or CI job is **not** sufficient to approve
a candidate. See [qualification](QUALIFICATION.md) and
[delivery gates](READY_IMAGES.md).

## Current status and limitations

- **Model:** pinned Qwen3-Coder-30B-A3B-Instruct **Q4_0**, CPU-only, 16K
  context, four A76 cores (4–7), Q8_0 KV, no swap. The model cgroup is **18 GiB**,
  the isolated Docker builder **19 GiB**; the host reserves another 1 GiB.
- **Board evidence:** the actual default coding profile passed the protected
  repair smoke `smoke-cffa6889` on 22 Nov 2026. Earlier matched Q4 CPU 16K
  measurement: **14.94 prompt / 6.23 generation tok/s**, **17.13 GiB peak**,
  no OOM. Tested NPU hybrids were slower; they are not the default.
- **Not yet established:** sustained full-16K prompts, an end-to-end unattended
  upgrade of both applications, and real production-client playback of a new
  candidate. These need a separate on-board qualification; passing CI is not a
  substitute.
- **Trust boundary:** upstream source and LLM-readable diffs are untrusted.
  Fork-supplied build/preflight/hardware scripts now execute in a disposable
  container **inside the isolated Docker daemon**, not directly in the
  token-bearing controller. Where needed, they still have broad control over
  that **inner** Docker daemon, and hardware qualification may use privileged
  **inner** containers. This is risk reduction, **not a hostile-code sandbox**.
  Keep automatic preparation and publication disabled until the revised
  execution path and a full upgrade are qualified.

## Operation

The production source checkout is `/armbian/rkmpp-maintainer`. Its
`data/` holds models, `jobs/` evidence, candidate archives, `monitor/`
checkouts and release state; `builder/` holds the isolated daemon's cache.
Only the **Portainer** stack manages the running services. Do not run host
`docker compose up/down` against this stack.

| Setting | Behavior |
| --- | --- |
| `MAINTAINER_ACTION=idle` | No watcher; keeps Portainer Console jobs available |
| `MAINTAINER_ACTION=watch`, `AUTO_UPDATE=false` | **Idle:** no upstream release requests, candidate builds or publishing |
| `MAINTAINER_ACTION=watch`, `AUTO_UPDATE=true` | Check on startup and at Dublin-local midnight; prepare candidates only in **00:00–08:00** when host CPU/RAM is idle |
| `AUTO_PUBLISH=false` | Keep verified candidates locally; **no GitHub PR** |
| `QUALIFICATION_ENABLED=false` | Supervised qualifier stays idle until explicitly enabled |
| `AUTO_PUBLISH=true` | Request a candidate PR only after checks/hardware qualification; requires a narrowly scoped `GITHUB_TOKEN`. **Do not enable before execution isolation is qualified.** |

New heavy jobs must satisfy the night window, CPU/load thresholds and RAM
headroom. In-flight jobs may finish after 08:00; later jobs defer. Failed
releases have bounded cooldown/retries; unexpected GitHub errors back off
rather than restarting the container repeatedly. Daily wake-ups account
for Irish daylight-saving changes.

The stack defaults to **`AUTO_UPDATE=false` and `AUTO_PUBLISH=false`**
until end-to-end qualification. Existing Portainer environment values take
precedence over these defaults. Enabling `AUTO_UPDATE` never enables
production deployment.

## Build and deploy the maintainer (not your applications)

The current development work is in
[PR #10](https://github.com/meitelis/rkmpp-maintainer/pull/10).
Until it merges, build its branch explicitly rather than accidentally
building `main`:

```sh
git -c safe.directory=/armbian/rkmpp-maintainer -C /armbian/rkmpp-maintainer fetch origin codex/qwen-17g-board-admission-20261116
git -c safe.directory=/armbian/rkmpp-maintainer -C /armbian/rkmpp-maintainer switch --detach FETCH_HEAD
docker build -t local/rkmpp-maintainer:pr10 /armbian/rkmpp-maintainer
```

In Portainer, update the existing `rkmpp-maintainer` stack using
[`portainer-stack.yml`](portainer-stack.yml), with
`pull_policy: never`. Recreate **only the maintainer service** without
pulling the local image; preserve builder/data mounts, environment and
all application containers. If Portainer loads the stack from Git,
its repository ref must match the intended branch until PR #10 merges
(the pre-existing `main` ref is not automatically updated by building
a different host checkout). See the
[one-time legacy migration script](scripts/migrate-to-portainer.sh)
for older installations.

Confirm the deployed image before testing:

```sh
docker inspect rkmpp-maintainer-maintainer-1 --format '{{.Image}}'
docker image inspect local/rkmpp-maintainer:pr10 --format '{{.Id}}'
docker exec rkmpp-maintainer-maintainer-1 python3 /opt/maintainer/maintainer.py smoke --model-profile coding --ignore-window
```

The two IDs must match. A `status: ready` **smoke** verifies only a controlled
repair exercise.

### Supervised qualification (opt-in)

The Portainer stack now includes a separate `qualifier` service running the
same local image **without a GitHub token**. Set `QUALIFICATION_ENABLED=true`
in the Portainer stack environment to start it; the default false stays idle.
Use the PR #10 stack definition, not the older main version. There is no need
to launch another `docker exec -d` background worker.

The worker is Docker's foreground process, so `restart: unless-stopped`
revives it after a crash or host reboot. A dedicated lock prevents overlapping
workers, including the currently running detached qualification. Every stage
is admitted only in the Dublin 00:00–08:00 window and when host resources
permit. Successful stages are checkpointed under
`/workspace/qualifications/<identity>/result.json`. An interrupted stage
may be safely retried once in a new isolated job after transient cleanup;
a second interruption or a real test failure stops that revision and retains
evidence. Completed revisions are not rerun. The container remains alive
but idle after a terminal result, avoiding a restart loop.

Inspect `docker logs rkmpp-maintainer-qualifier-1 --tail 50`, and the
saved JSON under `/armbian/rkmpp-maintainer/data/qualifications/`.
No deployment, application restart, or automatic PR merge is performed.
Historical replay qualification alone cannot certify a future real release.

## Candidate delivery and acceptance

Qualified jobs retain `/workspace/jobs/<job>/result.json`,
`image.tar`, checksums, logs, source patch and hardware reports.
The owner reviews the PR/diff, imports the matching image, updates only
the relevant app in Portainer, verifies live playback, and compares
the running image ID with `result.json:image_id`.

Only then, **if a candidate PR was published**, the owner may explicitly
authorize its merge from the maintainer Console:

```sh
python3 /opt/maintainer/maintainer.py accept-deployment \
  --job-id JOB-DIRECTORY-NAME \
  --observed-image-id sha256:ACTUAL-RUNNING-IMAGE-ID \
  --confirm-source-reviewed --confirm-live-playback
```

The controller rechecks the exact PR head, CI results and image evidence
before merging. Stale revisions or failed checks are rejected.
A production rollback remains a manual Portainer operation.

Further details: [qualification and known failures](QUALIFICATION.md),
[deliverable validation](READY_IMAGES.md), [CPU/NPU board evidence](RKNPU-HYBRID.md),
[source provenance](SOURCE.md). Development/CI uses the separate root
`compose.yml`; it is **not** the Portainer deployment manifest.
