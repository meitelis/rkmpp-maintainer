#!/usr/bin/env python3
"""Local RKMPP release maintenance. Candidates only; deployment is manual."""
import argparse
import contextlib
import difflib
import datetime
import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import traceback
import uuid
from pathlib import Path
from agent import run_agent
from core import command, http_json, latest_release, locked, save_json, version
from profiles import PROJECTS, current_version, git, reconcile, validate
from repair_fixture import BROKEN, REFERENCE, create_fixture
from runner import Docker
from limits import job_budget, require_space
from evidence import CandidateSourceMutation, candidate_source_digest, record_runtime, save_manifest
from qualification import runtime_checks
from model_settings import settings
from schedule import admission, seconds_until_next_night, wait_for_window
from delivery import verify_release_image
from lessons import verified_lessons, record_verified_repair

HERE = Path(__file__).resolve().parent


def phase(job, result, name):
    result["phase"] = name
    result["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_json(job / "result.json", result)
    print(f"{result['updated_at']} Job {job.name}: {name}", flush=True)


def recover_jobs(workspace):
    # The caller holds the global lock, so no earlier process still owns a job.
    for path in (workspace / "jobs").glob("*/result.json"):
        result = json.loads(path.read_text())
        if result.get("status") in {"preparing", "running"}:
            result.update(status="interrupted", error="Previous controller stopped before completing this job",
                          finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            save_json(path, result)


def github(path, data=None, method=None):
    return http_json("https://api.github.com/" + path, data=data,
                     token=os.environ.get("GITHUB_TOKEN"), method=method)


def clone(repository, target, ref="main"):
    command(["git", "clone", "--no-checkout", "--filter=blob:none", "https://github.com/" + repository + ".git", str(target)], timeout=1800)
    git(target, "checkout", "--detach", ref)
    git(target, "config", "user.name", "RKMPP maintainer")
    git(target, "config", "user.email", "rkmpp-maintainer@localhost")


def prepare(project, job, target, base_ref="main", source_version=None):
    version(target)
    profile = PROJECTS[project]
    root, upstream = job / "source", job / "upstream"
    clone(profile["fork"], root, base_ref)
    old = source_version or current_version(project, root)
    version(old)
    if version(target) <= version(old):
        raise RuntimeError(f"Target {target} is not newer than baseline {old}")
    command(["git", "init", str(upstream)], timeout=30)
    url = "https://github.com/" + profile["upstream"] + ".git"
    for tag, ref in [("v" + old, "base"), ("v" + target, "target")]:
        git(upstream, "fetch", "--depth", "1", url, "refs/tags/" + tag + ":refs/heads/" + ref)
    git(upstream, "checkout", "--detach", "target")
    old_ref, new_ref = git(upstream, "rev-parse", "base^{commit}"), git(upstream, "rev-parse", "target^{commit}")
    notes, changes = reconcile(project, root, upstream, old, target, old_ref, new_ref)
    save_json(job / "release.json", {"project": project, "baseline": old, "target": target,
        "upstream_commit": new_ref, "fork_base": git(root, "rev-parse", "HEAD"),
        "changes": changes, "merge_notes": notes})
    (job / "upstream.diff").write_text(git(upstream, "diff", "base", "target") + "\n")
    # Baseline after trusted deterministic reconciliation. The model cannot edit checks.
    return root, upstream, notes, changes


def publish(project, root, target, job):
    if not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("Publishing needs GITHUB_TOKEN; local candidate is retained")
    profile = PROJECTS[project]
    base = git(root, "rev-parse", "HEAD")
    changes = git(root, "diff", "--name-only", "HEAD").splitlines()
    changes += git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    elements = []
    for name in sorted(set(changes)):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Publication refuses deleted or symlinked files")
        elements.append({"path": name, "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                         "type": "blob", "content": path.read_text()})
    if not elements:
        raise RuntimeError("No source change to publish")
    api = "repos/" + profile["fork"]
    # Reject publication against a main branch that moved after qualification.
    # A silently stale fork would invalidate the tested candidate's source base.
    if github(api + "/git/ref/heads/main")["object"]["sha"] != base:
        raise RuntimeError("Fork main advanced after candidate build; reconcile and requalify before publishing")
    base_tree = github(api + "/git/commits/" + base)["tree"]["sha"]
    tree = github(api + "/git/trees", {"base_tree": base_tree, "tree": elements})["sha"]
    commit = github(api + "/git/commits", {"message": f"Prepare {project} {target} RKMPP candidate",
                                          "tree": tree, "parents": [base]})["sha"]
    branch = "automation/" + project + "-" + target + "-" + job.name[-8:]
    github(api + "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit})
    pr = github(api + "/pulls", {"head": branch, "base": "main", "draft": True,
        "title": f"Qualified candidate: {project} {target} RKMPP update",
        "body": "Prepared by the local Radxa maintainer after source checks, a complete ARM64 build, "
                "and isolated RK3588 hardware qualification of the exact exported image ID. "
                "Synthetic hardware tests do not establish real browser/phone playback. "
                "REVIEW ONLY — do not merge until the owner imports the image, tests production playback "
                "and explicitly accepts this exact PR/image. Failed attempts never publish PRs. "
                "The local job retains logs, image.tar, result.json and the matching Memories helper where applicable. "
                "The controller never deploys production apps."})
    return {"branch": branch, "commit": commit, "pull_request": pr["html_url"],
            "pull_request_number": pr["number"], "publication_status": "published"}


def review_targets(project, root, changes):
    if project == "memories":
        return {"read": ["go-vod/main.go", "memories_rkmpp_v11/lib/Listener/PlayerScriptListener.php"],
                "upstream": ["lib/Service/BinExt.php"],
                "diff": [p for p in changes if p.startswith("go-vod/")]}
    paths = [p for p in changes if (root / "overlays" / p).is_file()]
    runtime = [p for p in changes if p.startswith(("server/src/", "web/src/"))
               and not re.search(r"\.(spec|test)\.", p)]
    local = ["upstream.json", *["overlays/" + p for p in paths]]
    consumer = "overlays/server/src/controllers/video-stream.controller.ts"
    if (root / consumer).is_file():
        local.append(consumer)
    return {"read":list(dict.fromkeys(local)), "diff":list(dict.fromkeys([*paths, *runtime]))}


def previous_review(job, project, target):
    try:
        release = (job / "release.json").read_bytes()
    except FileNotFoundError:
        return None  # Without a release identity, start a fresh review.
    candidates = sorted(job.parent.glob(f"{project}-{target}-*/agent-0.jsonl"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            result = json.loads((path.parent / "result.json").read_text())
            if (result.get("kind") == "release-candidate"
                    and result.get("status") in {"failed", "interrupted"}
                    and result.get("phase") in {"model-review-1", "export"}
                    and (path.parent / "release.json").read_bytes() == release):
                return path
        except (OSError, ValueError):
            continue
    return None


def run_job(project, config, workspace, target, base_ref="main", source_version=None, publish_code=False, no_agent=False,
            model_profile="coding"):
    job = workspace / "jobs" / (project + "-" + target + "-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"project": project, "target": target, "job": str(job), "status": "preparing",
              "kind": "historical-replay" if base_ref != "main" or source_version else "release-candidate",
              "review_status": "pending-human-review", "deployment_approved": False,
              "candidate_verified": False, "ready_to_deploy": False,
              "agent_review": {"review_complete": False, "assessment": "skipped" if no_agent else "not-run"}}
    phase(job, result, "preflight")
    try:
        save_manifest(job, config, HERE, smoke=model_profile == "smoke")
        with job_budget(config, workspace):
            docker = Docker(config, workspace)
            docker.verify()
            docker.reap()
            phase(job, result, "reconciliation")
            root, upstream, notes, changes = prepare(project, job, target, base_ref, source_version)
            task = f"Upgrade {project} from the reconciled baseline to official {target}. "
            important = [p for p in changes if not p.startswith(("l10n/", "android/", "docs/")) and "lock" not in p]
            task += "Upstream files changed: " + ", ".join(important[:150])
            task += "\nMerge notes: " + "\n".join(notes)
            task += "\nRead upstream files/diffs to verify player and protocol compatibility. "
            task += "Build commands, workflows and tests are protected. Report blockers instead of bypassing them."
            task += verified_lessons(workspace, project)
            errors = ""
            tag = "local/" + project + "-rkmpp:candidate-" + target + "-" + job.name[-8:]
            rounds = 1 if no_agent else config["repair_rounds"]
            hardware = None
            for attempt in range(rounds):
                if not no_agent:
                    phase(job, result, f"model-review-{attempt + 1}")
                    result["agent_review"] = {"review_complete": False, "assessment": "in-progress",
                                              "attempt": attempt + 1}
                    # The model edits the candidate. It is unloaded before builds and hardware tests.
                    with docker.model(profile=model_profile,
                                      metrics_path=job / f"model-runtime-{attempt}.json") as endpoint:
                        record_runtime(job, docker)
                        review = run_agent(root, upstream,
                            task + "\nLast failure (repair before resubmitting):\n" + errors[-10000:],
                            lambda p: git(upstream, "diff", "base", "target", "--", p), endpoint,
                            project, job / f"agent-{attempt}.jsonl", turns=config["agent_turns"],
                            generation=settings(config, model_profile)[2],
                            required_observations=review_targets(project, root, changes),
                            previous_transcript=previous_review(job, project, target)
                            if attempt == 0 and result["kind"] == "release-candidate" else None)
                        result["agent_review"] = review
                        save_json(job / f"agent-review-{attempt}.json", review)
                        save_json(job / "agent-review.json", review)
                        (job / f"agent-summary-{attempt}.txt").write_text(review["summary"] + "\n")
                try:
                    phase(job, result, f"checks-{attempt + 1}")
                    # Freeze the reviewed candidate BEFORE any checks, including
                    # fork-controlled preflight scripts, may touch the checkout.
                    source_id = candidate_source_digest(root)
                    result["candidate_source_sha256"] = source_id
                    validate(root)
                    docker.checks(project, root, job / f"checks-{attempt}.log")
                    if candidate_source_digest(root) != source_id:
                        raise CandidateSourceMutation(
                            "Candidate source changed during independent checks")
                    phase(job, result, f"build-{attempt + 1}")
                    docker.build(project, root, tag, job / f"build-{attempt}.log")
                    if candidate_source_digest(root) != source_id:
                        raise CandidateSourceMutation("Candidate source changed during build")
                    phase(job, result, f"hardware-{attempt + 1}")
                    hardware = docker.hardware(project, root, tag, job / f"hardware-{attempt}.log")
                    if candidate_source_digest(root) != source_id:
                        raise CandidateSourceMutation("Candidate source changed during hardware checks")
                    if not isinstance(hardware, dict) or hardware.get("ok") is not True or not hardware.get("image_id"):
                        raise RuntimeError("Independent RK3588 hardware qualification was not completed")
                    result["hardware_qualification"] = hardware
                    result["repair_attempts"] = attempt + 1
                    break
                except CandidateSourceMutation:
                    # Never train/retry on a checkout that a source-provided
                    # executable changed outside the reviewed model edits.
                    raise
                except RuntimeError as error:
                    errors = str(error)
                    result.setdefault("repair_failures", []).append({
                        "attempt": attempt + 1, "error": errors[-12000:]})
                    # A failed build/qualification never leaves its image tagged
                    # as a deployable candidate in our isolated builder.
                    try:
                        docker.discard_failed_candidate(tag)
                    except Exception as cleanup_error:
                        result.setdefault("cleanup_warnings", []).append(str(cleanup_error))
                    save_json(job / "result.json", result)
                    if attempt + 1 == rounds:
                        raise RuntimeError(f"Candidate failed after {rounds} check/build/hardware attempts: {errors}") from error
            if hardware is None:
                raise RuntimeError("Missing successful RK3588 hardware qualification")
            phase(job, result, "export")
            if candidate_source_digest(root) != result["candidate_source_sha256"]:
                raise CandidateSourceMutation("Candidate source changed after hardware qualification")
            result.update(image=tag, image_id=docker.export(tag, job / "image.tar"))
            if candidate_source_digest(root) != result["candidate_source_sha256"]:
                raise CandidateSourceMutation("Candidate source changed during export")
            if result["image_id"] != hardware["image_id"]:
                raise RuntimeError("Exported image differs from hardware-qualified candidate")
            reviewed = not no_agent and result["agent_review"].get("review_complete") is True
            deliverable = reviewed and result["kind"] == "release-candidate"
            # Technically qualified means suitable for an OWNER-CONTROLLED trial,
            # never a claim that real clients have been tested or deployed.
            result["candidate_verified"] = bool(deliverable)
            result["ready_to_deploy"] = False
            result["artifact_status"] = ("validated-awaiting-owner-review" if deliverable else
                "historical-replay" if result["kind"] != "release-candidate" else "requires-source-review")
            git(root, "add", "-N", "--", ".")
            (job / "candidate.diff").write_text(git(root, "diff", "HEAD") + "\n")
            result["candidate_diff_sha256"] = hashlib.sha256((job / "candidate.diff").read_bytes()).hexdigest()
            if project == "memories":
                helper_path = job / "memories-helper.tar.gz"
                with tarfile.open(helper_path, "w:gz") as archive:
                    archive.add(root / "memories_rkmpp_v11", arcname="memories_rkmpp_v11", recursive=True)
                with helper_path.open("rb") as handle:
                    result["helper_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            # Do not advertise a release until its saved image is complete,
            # matches the hardware-tested ID, and can be loaded by Docker.
            if deliverable:
                result["delivery"] = verify_release_image(job, result)
            result["status"] = "built"
            save_json(job / "result.json", result)
            if publish_code:
                # Publication is strictly downstream of independent hardware and
                # a complete model review; never publish debug/historical jobs.
                if not reviewed or result["kind"] != "release-candidate":
                    raise RuntimeError("Publishing requires a reviewed, hardware-qualified release candidate")
                try:
                    if candidate_source_digest(root) != result["candidate_source_sha256"]:
                        raise RuntimeError("Candidate source changed before publication")
                    result.update(publish(project, root, target, job))
                except Exception as error:
                    result["publication_error"] = str(error)
                    result["publication_status"] = "retryable"
                    result["status"] = "publication-pending"
        return result
    except Exception as error:
        result.update(status="failed", error=str(error), ready_to_deploy=False, artifact_status="blocked")
        (job / "error.txt").write_text(traceback.format_exc())
        # A failed attempt may leave an incomplete archive. Retain diagnostic
        # logs and source evidence, but never leave an installable-looking tar.
        for artifact in ("image.tar", "image.tar.sha256", "memories-helper.tar.gz"):
            (job / artifact).unlink(missing_ok=True)
        # Also cover failures outside the inner check/build retry loop (model
        # refusal, export mismatch, an unexpected exception). Only our own
        # candidate tag on the verified isolated daemon may be discarded.
        if "docker" in locals() and "tag" in locals():
            try:
                docker.discard_failed_candidate(tag)
            except Exception as cleanup_error:
                result.setdefault("cleanup_warnings", []).append(str(cleanup_error))
        return result
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)



def publish_existing(workspace, job_name):
    """Retry only GitHub publication, never a successful hardware build."""
    job = (workspace / "jobs" / job_name).resolve()
    if job.parent != (workspace / "jobs").resolve():
        raise RuntimeError("Publication job must be inside the maintainer workspace")
    result = json.loads((job / "result.json").read_text())
    if result.get("status") != "publication-pending" or result.get("candidate_verified") is not True:
        raise RuntimeError("Only verified candidates with pending publication can be retried")
    hardware = result.get("hardware_qualification", {})
    if (not isinstance(hardware, dict) or hardware.get("ok") is not True or
            hardware.get("image_id") != result.get("image_id") or
            result.get("agent_review", {}).get("review_complete") is not True or
            result.get("kind") != "release-candidate"):
        raise RuntimeError("Candidate failed the independent hardware/source publication gate")
    patch = job / "candidate.diff"
    saved_patch = patch.read_bytes() if patch.is_file() else b""
    fresh_patch = (git(job / "source", "diff", "HEAD") + "\n").encode()
    if (not saved_patch or saved_patch != fresh_patch or
            hashlib.sha256(saved_patch).hexdigest() != result.get("candidate_diff_sha256")):
        raise RuntimeError("Candidate source changed since hardware qualification")
    # One verification contract for initial delivery and delayed PR retries.
    # Re-check the original archive rather than trusting a retained status bit.
    if verify_release_image(job, result) != result.get("delivery"):
        raise RuntimeError("Qualified candidate delivery metadata changed after export")
    if candidate_source_digest(job / "source") != result.get("candidate_source_sha256"):
        raise RuntimeError("Candidate source changed since source/build qualification")
    docker = Docker(json.loads((HERE / "config.json").read_text()), workspace)
    docker.verify()
    image_id = command(["docker", "image", "inspect", result["image"], "--format", "{{.Id}}"], timeout=30)
    if image_id != result["image_id"]:
        raise RuntimeError("Candidate image ID changed since hardware qualification")
    result.update(publish(result["project"], job / "source", result["target"], job))
    result["status"] = "built"
    result.pop("publication_error", None)
    save_json(job / "result.json", result)
    return result



def accept_deployed_candidate(workspace, job_name, observed_image_id, source_reviewed=False,
                              playback_confirmed=False):
    """Owner-only acceptance; no implicit deployment or success inference."""
    if not source_reviewed or not playback_confirmed:
        raise RuntimeError("Requires explicit source review and successful real-client playback confirmation")
    if not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("Merging requires a scoped GITHUB_TOKEN")
    job = (workspace / "jobs" / job_name).resolve()
    if job.parent != (workspace / "jobs").resolve():
        raise RuntimeError("Job must be inside the maintainer workspace")
    result = json.loads((job / "result.json").read_text())
    if (result.get("status") != "built" or result.get("kind") != "release-candidate" or
            result.get("candidate_verified") is not True or
            result.get("image_id") != observed_image_id or
            result.get("agent_review", {}).get("review_complete") is not True):
        raise RuntimeError("Owner's deployed image does not match a complete verified candidate")
    hardware = result.get("hardware_qualification", {})
    if (not isinstance(hardware, dict) or hardware.get("ok") is not True or
            hardware.get("image_id") != observed_image_id):
        raise RuntimeError("Deployed image ID does not match the RK3588 hardware-qualified image")
    number, sha, branch = result.get("pull_request_number"), result.get("commit"), result.get("branch")
    if not isinstance(number, int) or not isinstance(sha, str) or not isinstance(branch, str):
        raise RuntimeError("Published PR metadata is missing")
    if not re.fullmatch(r"automation/(?:memories|immich)-\d+\.\d+\.\d+-[a-f0-9]{8}", branch):
        raise RuntimeError("Refusing to merge or clean up a non-automation branch")
    repo = PROJECTS[result["project"]]["fork"]
    api = "repos/" + repo
    release = json.loads((job / "release.json").read_text())
    if github(api + "/git/ref/heads/main")["object"]["sha"] != release["fork_base"]:
        raise RuntimeError("Fork main moved since qualification; prepare a new release candidate")
    pr = github(api + "/pulls/" + str(number))
    if (pr.get("state") != "open" or pr.get("draft") or
            pr.get("head", {}).get("sha") != sha or pr.get("head", {}).get("ref") != branch or
            pr.get("base", {}).get("ref") != "main" or
            pr.get("base", {}).get("sha") != release["fork_base"]):
        raise RuntimeError("PR no longer matches the qualified candidate and source base")
    checks = github(api + "/commits/" + sha + "/check-runs?per_page=100")
    runs = checks.get("check_runs", [])
    if not runs or checks.get("total_count") != len(runs) or any(
            run.get("status") != "completed" or run.get("conclusion") != "success" for run in runs):
        raise RuntimeError("GitHub CI checks are missing, incomplete or not all successful")
    # This API call is possible only after the user explicitly supplies the two
    # acceptance flags and the observed running image ID.
    merged = github(api + "/pulls/" + str(number) + "/merge",
                    {"sha":sha, "merge_method":"squash"}, method="PUT")
    if merged.get("merged") is not True or not merged.get("sha"):
        raise RuntimeError("GitHub did not confirm the requested merge")
    result.update(deployment_approved=True, ready_to_deploy=True,
                  artifact_status="owner-accepted-live",
                  review_status="owner-accepted-live",
                  owner_confirmed_image_id=observed_image_id,
                  merge_commit=merged["sha"],
                  accepted_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    # Only owner-confirmed successful production playback may teach future
    # autonomous repairs. Failed or merely hardware-tested candidates do not.
    if result.get("repair_failures"):
        try:
            paths = git(job / "source", "diff", "--name-only", "HEAD").splitlines()
            result["verified_repair_recorded"] = record_verified_repair(
                workspace, result["project"], result["target"], result, paths)
        except (OSError, ValueError, RuntimeError) as error:
            result["verified_repair_warning"] = str(error)[:400]
    save_json(job / "result.json", result)
    try:
        # Only delete our own branch after GitHub confirms the merge. The
        # accepted commit and full candidate evidence remain in Git/history.
        github(api + "/git/refs/heads/" + branch, method="DELETE")
        result["merged_branch_removed"] = True
    except Exception as error:
        result["branch_cleanup_warning"] = str(error)
    save_json(job / "result.json", result)
    return {"status":"merged", "project":result["project"], "pull_request":result["pull_request"],
            "merge_commit":merged["sha"], "image_id":observed_image_id}



def discover(workspace):
    records = []
    for project, profile in PROJECTS.items():
        releases = github("repos/" + profile["upstream"] + "/releases?per_page=100")
        latest = latest_release(releases)
        if not latest:
            raise RuntimeError("No stable upstream release found")
        local = workspace / "monitor" / project
        local.parent.mkdir(parents=True, exist_ok=True)
        if not local.exists():
            clone(profile["fork"], local)
        else:
            git(local, "fetch", "origin", "main")
            git(local, "checkout", "--detach", "origin/main")
        current = current_version(project, local)
        records.append({"project": project, "current": current, "latest": latest["tag_name"].removeprefix("v"),
                        "update": version(latest["tag_name"]) > version(current)})
    return records


def smoke(config, workspace, model_profile="smoke"):
    """Require a model repair; retain the outcome and error even when it fails."""
    job = workspace / "jobs" / ("smoke-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"status":"preparing", "kind":"real-local-model-smoke", "job":str(job),
              "exercise":"http-options-intersection", "source_edit_verified":False}
    phase(job, result, "preflight")
    try:
        save_manifest(job, config, HERE, smoke=model_profile == "smoke")
        with job_budget(config, workspace):
            root, upstream = job / "source", job / "upstream"
            source = create_fixture(root, upstream)
            (root / "Dockerfile").write_text("FROM " + config["python_image"] + "\nWORKDIR /app\nCOPY . .\nRUN python -m unittest discover -s tests -v\nCMD [\"python\", \"-c\", \"print('maintainer smoke passed')\"]\n")
            docker = Docker(config, workspace)
            docker.reap()
            task = ("Repair go-vod/http_options.py: supported_http_options must return requested option names "
                    "supported by BOTH configured binaries, preserving requested order and matching exact whitespace-separated "
                    "tokens. Read the local file and the upstream reference for the same path, then make the source correction. "
                    "Use action upstream with path go-vod/http_options.py for the reference. "
                    "Do not change tests. Finish only after completing the repair; otherwise report a blocker.")
            phase(job, result, "model-repair")
            with docker.model(profile=model_profile, metrics_path=job / "model-runtime.json") as endpoint:
                record_runtime(job, docker)
                review = run_agent(root, upstream, task,
                    lambda _: "".join(difflib.unified_diff(BROKEN.splitlines(True), REFERENCE.splitlines(True))),
                    endpoint, "memories", job / "agent.jsonl", turns=8,
                    generation=settings(config, model_profile)[2], require_edit=True,
                    required_observations={"read":["go-vod/http_options.py"],
                                           "upstream":["go-vod/http_options.py"]})
            if source.read_text() == BROKEN:
                raise RuntimeError("Repair exercise did not change the broken source")
            save_json(job / "agent-review.json", review)
            phase(job, result, "checks")
            docker.checks("fixture", root, job / "checks.log")
            tag = "local/rkmpp-maintainer:smoke-" + job.name[-8:]
            phase(job, result, "build")
            docker.build("fixture", root, tag, job / "build.log")
            phase(job, result, "export")
            result.update(status="ready", source_edit_verified=True, agent_review=review,
                model=settings(config, model_profile)[0], model_runtime=settings(config, model_profile)[1],
                external_model_endpoint=bool(os.environ.get("MODEL_ENDPOINT")), image=tag,
                image_id=docker.export(tag, job / "image.tar"))
    except Exception as error:
        result.update(status="failed", error=str(error))
        (job / "error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)
    return result



def release_attempt_due(entry, now, max_attempts, retry_seconds, *, watch=False):
    """Decide whether an unapproved candidate deserves another bounded attempt."""
    if not entry:
        return True, "new", 0
    if entry.get("status") == "built":
        return False, "built", int(entry.get("attempts", 1))
    attempts = int(entry.get("attempts", 1))
    if attempts >= max_attempts:
        return False, "retry-limit", attempts
    previous = entry.get("last_attempt_at")
    if watch and previous:
        try:
            last = datetime.datetime.fromisoformat(previous)
            if last.tzinfo is not None and (now - last).total_seconds() < retry_seconds:
                return False, "cooldown", attempts
        except ValueError:
            pass  # Old/incomplete state is retried, not permanently skipped.
    return True, "retry", attempts


def poll_releases(args, config, workspace, profile):
    """One bounded discovery cycle. Caller holds the job lock; caller owns sleeping."""
    delay = max(1, int(config.get("poll_seconds", 86400)))
    failed = deferred = False
    if args.action == "watch" and os.environ.get("AUTO_UPDATE") != "true":
        # Disabled means idle, NOT repeated network checks with no action.
        print(json.dumps({"status": "idle", "reason": "AUTO_UPDATE disabled; no release checks or builds"}), flush=True)
        return delay, failed, deferred
    records = discover(workspace)
    print(json.dumps(records), flush=True)
    if args.action not in {"once", "watch"}:
        return delay, failed, deferred
    auto_publish = args.publish or (args.action == "watch" and os.environ.get("AUTO_PUBLISH") == "true")
    if auto_publish and not os.environ.get("GITHUB_TOKEN"):
        print(json.dumps({"status":"blocked", "reason":"AUTO_PUBLISH requires a scoped GITHUB_TOKEN; no build started"}), flush=True)
        return delay, True, False

    state_path = workspace / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    max_attempts = max(1, int(config.get("release_max_attempts", 3)))
    retry_seconds = max(60, int(config.get("release_retry_seconds", 3600)))
    for record in records:
        if not record["update"]:
            continue
        key = record["project"] + ":" + record["latest"]
        now = datetime.datetime.now(datetime.timezone.utc)
        due, reason, attempts = release_attempt_due(
            state.get(key), now, max_attempts, retry_seconds, watch=args.action == "watch")
        if not due:
            if reason == "cooldown":
                last = datetime.datetime.fromisoformat(state[key]["last_attempt_at"])
                seconds = max(1, int((last + datetime.timedelta(seconds=retry_seconds) - now).total_seconds()) + 1)
                delay = min(delay, seconds)
                deferred = True
            elif reason == "retry-limit":
                failed = True
            if reason != "built":
                print(json.dumps({"status": reason, "project": record["project"],
                                  "version": record["latest"], "attempts": attempts}), flush=True)
            continue

        if state.get(key, {}).get("status") == "publication-pending":
            if not auto_publish:
                deferred = True
                print(json.dumps({"status":"publication-pending", "project":record["project"],
                                  "version":record["latest"], "job":state[key]["job"]}), flush=True)
                continue
            try:
                published = publish_existing(workspace, Path(state[key]["job"]).name)
                state[key] = {"status":"built", "job":published["job"],
                              "attempts": attempts + 1, "last_attempt_at": now.isoformat()}
                save_json(state_path, state)
                print(json.dumps({"status":"published", "project":record["project"],
                                  "pull_request":published["pull_request"]}), flush=True)
            except Exception as error:
                state[key]["attempts"] = attempts + 1
                state[key]["last_attempt_at"] = now.isoformat()
                save_json(state_path, state)
                print(json.dumps({"status":"publication-retry-failed", "error":str(error)}), flush=True)
                failed = True
            continue

        gate = admission(config, profile) if args.action == "watch" or not args.ignore_window else {"allowed": True}
        if not gate["allowed"]:
            deferred = True
            print(json.dumps({"status": "deferred", "project": record["project"], **gate}), flush=True)
            delay = min(delay, gate.get("retry_seconds", 300))
            continue

        # Count a crash or host reboot as an attempt: persist BEFORE launching.
        # Restart recovery must never hot-loop model/build work indefinitely.
        state[key] = {"status": "running", "attempts": attempts + 1,
                      "last_attempt_at": now.isoformat()}
        save_json(state_path, state)
        result = run_job(record["project"], config, workspace, record["latest"],
                         publish_code=auto_publish, no_agent=args.no_agent, model_profile=profile)
        # A built debug job without a verified archive is not a release.
        if result["status"] == "built" and (
                result.get("candidate_verified") is not True or
                not isinstance(result.get("delivery"), dict)):
            result.update(status="failed", ready_to_deploy=False,
                          artifact_status="blocked",
                          error="No verified, importable image.tar was produced")
            # The normal job writes its own result. Never create an unrelated
            # path from a malformed return value while rejecting an unsafe build.
            expected_jobs = (workspace / "jobs").resolve()
            job_path = Path(result.get("job", "")).resolve()
            if job_path.parent == expected_jobs and (job_path / "result.json").is_file():
                save_json(job_path / "result.json", result)
        state[key] = {"status": result["status"], "job": result["job"],
                      "attempts": attempts + 1, "last_attempt_at": now.isoformat()}
        save_json(state_path, state)
        print(json.dumps(result), flush=True)
        if result["status"] != "built":
            failed = True
            delay = min(delay, retry_seconds)
    return delay, failed, deferred


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["idle", "check", "once", "watch", "prepare", "run", "smoke", "runtime-check", "accept-deployment"])
    parser.add_argument("--repo", choices=PROJECTS)
    parser.add_argument("--target")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--source-version")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--job-id", help="Exact maintainer jobs/ directory name")
    parser.add_argument("--observed-image-id", help="Running production container image ID observed by owner")
    parser.add_argument("--confirm-source-reviewed", action="store_true")
    parser.add_argument("--confirm-live-playback", action="store_true")

    parser.add_argument("--no-agent", action="store_true", help="Test trusted reconciliation/builds without asking a model")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("WORKSPACE", "/workspace")))
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--model-profile", choices=["coding", "smoke", "hybrid", "hybrid_cpu"])
    parser.add_argument("--wait-for-window", action="store_true", help="Wait for the night/idle admission gate")
    parser.add_argument("--ignore-window", action="store_true", help="Explicit manual override of night/idle admission")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if args.action == "idle":
        # Keep the Portainer service available for Console jobs without owning the job lock.
        print("Ready for Portainer Console jobs. Set MAINTAINER_ACTION=watch in the stack to monitor releases.", flush=True)
        while True:
            time.sleep(60)
    profile = args.model_profile or os.environ.get("MODEL_PROFILE") or (
        "smoke" if args.action == "smoke" else "coding")
    if profile not in {"smoke", "coding", "hybrid", "hybrid_cpu"}:
        parser.error("Invalid MODEL_PROFILE; expected coding, smoke, hybrid or hybrid_cpu")
    if config.get("work_window", {}).get("enabled", False) and args.action in {"runtime-check", "smoke", "run", "once"}:
        # Reap abandoned owned workloads before judging whether enough RAM is idle.
        with locked(workspace / "maintainer.lock"):
            recover_jobs(workspace)
            Docker(config, workspace).reap()
    if args.action in {"runtime-check", "smoke", "run", "once"}:
        if not wait_for_window(config, profile, wait=args.wait_for_window, ignore=args.ignore_window)["allowed"]:
            raise SystemExit(2)
    if args.action == "watch":
        while True:
            try:
                with locked(workspace / "maintainer.lock"):
                    recover_jobs(workspace)
                    if (os.environ.get("AUTO_UPDATE") == "true"
                            and config.get("work_window", {}).get("enabled", False)):
                        Docker(config, workspace).reap()
                    delay, _, _ = poll_releases(args, config, workspace, profile)
            except Exception as error:
                # Network, GitHub and upstream errors should not hot-loop the
                # Portainer restart policy. Preserve state and try again
                # without creating or deploying an unqualified candidate.
                busy = isinstance(error, RuntimeError) and str(error) == "Another maintainer job is running"
                delay = (max(1, int(config.get("work_window", {}).get("idle_retry_seconds", 300)))
                         if busy else max(60, min(3600, int(config.get("poll_seconds", 86400)))))
                print(json.dumps({"status": "busy" if busy else "error",
                                  "error_type": type(error).__name__,
                                  "reason": str(error)[:500],
                                  "retry_seconds": delay}), flush=True)
            # Wake at the next local night-window start, not N hours after
            # a long build. Shorter failure/idle retries remain in effect.
            # Never hold the job lock while sleeping.
            time.sleep(max(1, min(delay, seconds_until_next_night(config))))
    with locked(workspace / "maintainer.lock"):
        recover_jobs(workspace)
        if args.action == "accept-deployment":
            if not args.job_id or not args.observed_image_id:
                parser.error("accept-deployment requires --job-id and --observed-image-id")
            print(json.dumps(accept_deployed_candidate(
                workspace, args.job_id, args.observed_image_id,
                args.confirm_source_reviewed, args.confirm_live_playback), indent=2))
            return
        if args.action == "runtime-check":
            result = runtime_checks(config, workspace)
            print(json.dumps(result, indent=2))
            if result["status"] != "passed":
                raise SystemExit(1)
            return
        if args.action == "smoke":
            result = smoke(config, workspace, model_profile=profile)
            print(json.dumps(result, indent=2))
            if result["status"] != "ready":
                raise SystemExit(1)
            return
        if args.action == "prepare":
            if not args.repo or not args.target:
                parser.error("prepare needs --repo and --target")
            job = workspace / "jobs" / ("prepare-" + args.repo + "-" + uuid.uuid4().hex[:8])
            job.mkdir(parents=True)
            root, upstream, notes, changes = prepare(args.repo, job, args.target.removeprefix("v"), args.base_ref, args.source_version)
            print(json.dumps({"job": str(job), "source": str(root), "notes": notes,
                              "status": "needs-reconciliation" if notes else "prepared",
                              "changed_files": len(changes)}, indent=2))
            return
        if args.action == "run":
            if not args.repo or not args.target:
                parser.error("run needs --repo and --target")
            result = run_job(args.repo, config, workspace, args.target.removeprefix("v"),
                             args.base_ref, args.source_version, args.publish, args.no_agent, model_profile=profile)
            print(json.dumps(result, indent=2))
            if result["status"] != "built":
                raise SystemExit(1)
            return
        _, failed, deferred = poll_releases(args, config, workspace, profile)
        if args.action == "once" and failed:
            raise SystemExit(1)
        if args.action == "once" and deferred:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
