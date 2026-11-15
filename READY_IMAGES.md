# Ready-to-deploy RKMPP images (no automatic deployment)

This controller's deliverable is an **installable ARM64 Docker image archive** for
either `meitelis/immich-rkmpp` or `meitelis/memories-rkmpp`, *not* a replacement
running stack, new Immich account, or a GitHub merge. Image artifacts remain inside
the isolated builder until the owner explicitly imports and deploys them.

## Candidate gate

A normal release candidate must pass all of the following, for the **same** image ID:

1. Trusted upstream tag/commit/runtime pin reconciliation.
2. The local coding model's complete source review without blockers; protected
   tests, build files and verification gates cannot be weakened by the model.
3. Independent source/Go/PHP/browser checks plus a complete ARM64 production build.
4. Immich: candidate FFmpeg Original/remux/shared-AAC fragment tests **and** all
   38 distinct RK3588 synthetic hardware cases: SDR H.264/HEVC (480–2160p),
   cold seeks, software-decode fallback, paced encoding, rotation,
   AAC/PCM/FLAC, 10-bit SDR, HDR Mali/CPU test paths.
5. Memories: independent ffmpeg/core/api **hardware** Go matrix, with
   skipped device tests treated as failures; the production go-vod binary must
   match the tested smoke binary by SHA-256.
6. Full `docker save` export of that tested image ID, with source patches,
   build and test logs and retained qualification evidence.

The controller runs hardware tests in the isolated Docker-in-Docker daemon,
with RK3588 devices exposed to its test containers. The normal production
Immich/Nextcloud containers, databases and media are never mounted or modified.

If a build, source check, fragment test or hardware test fails, the controller
passes its diagnostic error to the coding model for a bounded repair/rebuild/
retest cycle (default `repair_rounds=3`). The model may change allowed
integration source, not the protected verification. Every retry gets separate
test and model logs and a fresh hardware scratch directory.

The release artifact is advertised as `ready_to_deploy=true` **only** after
the complete gate passes and the exported image ID equals the qualified image
ID. `--no-agent` historical/debug runs remain `requires-source-review`.
Failed or incomplete builds stay `blocked`, with their evidence retained.
`deployment_approved` remains false: the maintainer **never changes the user's
running application stack**.

## Delivery

Output: `/armbian/rkmpp-maintainer/data/jobs/<job>/`

- `image.tar`: candidate runtime image suitable for `docker load`.
- `result.json`: project, release, image tag/ID, repair attempts, hardware
  qualification, `ready_to_deploy` and any repair failures.
- `build-*.log`, `checks-*.log`, `hardware-*.log`: per-attempt evidence.
- `hardware-*/`: synthetic hardware report or Memories device test logs.
- `candidate.diff`, `agent-review-*.json`, `run-manifest.json`:
  source, review, runtime and model provenance.
- Memories: `memories-helper.tar.gz` for the matching Nextcloud browser helper;
  the go-vod Docker image alone is not a complete browser-helper update.

The owner can inspect the result and import an accepted archive on the ROCK 5B+:

```sh
docker load -i /armbian/rkmpp-maintainer/data/jobs/JOB-ID/image.tar
docker image inspect IMAGE-TAG --format '{{.Id}} {{.Architecture}}'
```

Only the owner updates the existing Portainer app stack. Preserve the current
image and database backup. Switching an image back does not necessarily undo
Immich database migrations. A validated synthetic/hardware matrix is not a
claim of universal smooth playback or guaranteed HDR visual fidelity on every
phone/browser; real client behavior remains a final deployment acceptance step.

Do not enable unattended image preparation until the new controller itself
passes CI and a real board qualification cycle. The controller's
`AUTO_UPDATE=false` default is retained.
