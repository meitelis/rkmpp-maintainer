# Qualification evidence

Scope: unattended preparation of candidate images in the isolated builder. Successful compilation or model self-report does not authorize replacing a running application. Portainer continues to own deployment.

Required checks for this revision:

1. Full Linux unit suite, Python syntax, both Compose files and migration syntax pass.
2. `runtime-check` passes using the actual pinned PHP/Python images and isolated Docker daemon. Invalid PHP must fail before compatibility execution; valid PHP must pass; the broken behavioral fixture must fail; a timed-out check container must be removed.
3. The selected coding model repairs the protected HTTP-option exercise, completes every required observation, passes all five behavioral checks, and builds/exports the result. The smaller Ornith control runs in ARM64 CI; the larger Qwen coding profile must pass independently on the Radxa.
4. A Memories historical upgrade replay passes reconciliation, full required review coverage, protected source checks and candidate build/export. A deliberately old replay remains evidence only and must not replace the current service.
5. A current-fork Immich 3.4.0 → 3.4.1 candidate passes the same gates plus playback-fragment checks against the built image.
6. A genuine model repair in real integration source is checked independently; the Python smoke alone does not satisfy this criterion.
7. Portainer image identity, controller source digest, runtime/model digests, settings, source commits, final patches and job results are retained. No negative/failed case may be reported as a passing candidate.

Candidate qualification must identify exactly which revision and configuration passed. Changes to the controller, model, runtime, checks or application integration require the relevant checks again. Unsupported layouts, unresolved conflicts, budget exhaustion, unverified external endpoints and failed checks retain failure evidence and prevent candidate approval.

The implementation records a six-hour whole-job budget, a 2 GiB free-space reserve, build/export preflights, per-operation limits, timestamped tool outcomes and complete paginated observations. The builder has an 18 GiB ceiling and six-CPU ceiling in the production Portainer stack. Images, models, job evidence and production services are preserved.

No qualification result is asserted in this document until the corresponding evidence is attached.

`python3 /opt/maintainer/repair_check.py` recreates the missing FFmpeg startup probe in a disposable checkout of the current Memories fork. A protected Go AST contract must first fail for that known fault, then pass after the model repair as part of the full Go/browser/PHP/build/export pipeline. The reference is the unmodified current fork, and the result is explicitly repair-qualification evidence, not a new release or deployment approval.

Routine work admits new jobs only 00:00–08:00 Europe/Dublin on an idle board. In-flight jobs may finish after the closing time. Qualification using a manual window override must record that choice; the routine watch gate remains in effect. Qwen3.8-27B qualification requires measured memory headroom and source-repair results, not just a GGUF file-size estimate.

`python3 /opt/maintainer/qualification_queue.py` runs the stages in order and waits for each permitted night/idle admission without holding the maintenance job lock. Results live in `/workspace/qualifications/<identity>/result.json`; failed revisions stop rather than retry indefinitely. The queue tests the exact hardware image produced by the current-fork repair, before running the Immich upgrade. It requires verified model identity and cgroup peak-memory/OOM evidence before producing a qualified result. Starting it detached inside the Portainer controller allows it to continue after SSH or the laptop disconnects.
