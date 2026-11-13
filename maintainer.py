#!/usr/bin/env python3
"""Local RKMPP release maintenance. Candidates only; deployment is manual."""
import argparse
import contextlib
import difflib
import datetime
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
from evidence import record_runtime, save_manifest
from qualification import runtime_checks
from model_settings import settings
from schedule import admission, wait_for_window

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
    base_tree = github(api + "/git/commits/" + base)["tree"]["sha"]
    tree = github(api + "/git/trees", {"base_tree": base_tree, "tree": elements})["sha"]
    commit = github(api + "/git/commits", {"message": f"Prepare {project} {target} RKMPP candidate",
                                          "tree": tree, "parents": [base]})["sha"]
    branch = "automation/" + project + "-" + target + "-" + job.name[-8:]
    github(api + "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit})
    pr = github(api + "/pulls", {"head": branch, "base": "main", "draft": True,
        "title": f"Candidate: {project} {target} RKMPP update",
        "body": "Prepared by the local Radxa maintainer. Source checks and the candidate build passed locally. "
                "This is a draft for review and manual deployment. Hardware playback is not established by compilation. "
                "The local job contains logs, image.tar, result.json and the matching Memories helper where applicable. "
                "The controller does not merge or deploy."})
    return {"branch": branch, "commit": commit, "pull_request": pr["html_url"]}


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


def run_job(project, config, workspace, target, base_ref="main", source_version=None, publish_code=False, no_agent=False,
            model_profile="coding"):
    job = workspace / "jobs" / (project + "-" + target + "-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"project": project, "target": target, "job": str(job), "status": "preparing",
              "kind": "historical-replay" if base_ref != "main" or source_version else "release-candidate",
              "review_status": "pending-human-review", "deployment_approved": False,
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
            errors = ""
            tag = "local/" + project + "-rkmpp:candidate-" + target + "-" + job.name[-8:]
            for attempt in range(1 if no_agent else config["repair_rounds"]):
                if not no_agent:
                    phase(job, result, f"model-review-{attempt + 1}")
                    result["agent_review"] = {"review_complete": False, "assessment": "in-progress",
                                              "attempt": attempt + 1}
                    # Model lifetime ends before compiler/build memory is needed.
                    with docker.model(smoke=model_profile == "smoke", metrics_path=job / f"model-runtime-{attempt}.json") as endpoint:
                        record_runtime(job, docker)
                        review = run_agent(root, upstream, task + "\nLast check failures:\n" + errors[-10000:],
                            lambda p: git(upstream, "diff", "base", "target", "--", p), endpoint,
                            project, job / "agent.jsonl", turns=config["agent_turns"],
                            generation=settings(config, model_profile)[2],
                            required_observations=review_targets(project, root, changes))
                        result["agent_review"] = review
                        save_json(job / "agent-review.json", review)
                        (job / "agent-summary.txt").write_text(review["summary"] + "\n")
                try:
                    phase(job, result, f"checks-{attempt + 1}")
                    validate(root)
                    docker.checks(project, root, job / f"checks-{attempt}.log")
                    phase(job, result, f"build-{attempt + 1}")
                    docker.build(project, root, tag, job / f"build-{attempt}.log")
                    break
                except RuntimeError as error:
                    errors = str(error)
                    if attempt + 1 == (1 if no_agent else config["repair_rounds"]):
                        raise
            phase(job, result, "export")
            result.update(status="built", image=tag,
                          image_id=docker.export(tag, job / "image.tar"))
            git(root, "add", "-N", "--", ".")
            (job / "candidate.diff").write_text(git(root, "diff", "HEAD") + "\n")
            if project == "memories":
                with tarfile.open(job / "memories-helper.tar.gz", "w:gz") as archive:
                    archive.add(root / "memories_rkmpp_v11", arcname="memories_rkmpp_v11", recursive=True)
            save_json(job / "result.json", result)
            if publish_code:
                # Publication failure does not discard a successfully built local image.
                try:
                    result.update(publish(project, root, target, job))
                except Exception as error:
                    result["publication_error"] = str(error)
        return result
    except Exception as error:
        result.update(status="failed", error=str(error))
        (job / "error.txt").write_text(traceback.format_exc())
        return result
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)


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
            with docker.model(smoke=model_profile == "smoke", metrics_path=job / "model-runtime.json") as endpoint:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["idle", "check", "once", "watch", "prepare", "run", "smoke", "runtime-check"])
    parser.add_argument("--repo", choices=PROJECTS)
    parser.add_argument("--target")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--source-version")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-agent", action="store_true", help="Test trusted reconciliation/builds without asking a model")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("WORKSPACE", "/workspace")))
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--model-profile", choices=["coding", "smoke"])
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
    profile = args.model_profile or ("smoke" if args.action == "smoke" else "coding")
    if config.get("work_window", {}).get("enabled", False) and args.action in {"runtime-check", "smoke", "run", "once", "watch"}:
        # Reap abandoned owned workloads before judging whether enough RAM is idle.
        with locked(workspace / "maintainer.lock"):
            recover_jobs(workspace)
            Docker(config, workspace).reap()
    if args.action in {"runtime-check", "smoke", "run", "once"}:
        if not wait_for_window(config, profile, wait=args.wait_for_window, ignore=args.ignore_window)["allowed"]:
            raise SystemExit(2)
    with locked(workspace / "maintainer.lock"):
        recover_jobs(workspace)
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
            print(json.dumps({"job": str(job), "source": str(root), "notes": notes, "changed_files": len(changes)}, indent=2))
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
        while True:
            failed = False
            deferred = False
            delay = config.get("poll_seconds", 21600)
            records = discover(workspace)
            print(json.dumps(records), flush=True)
            if args.action == "once" or (args.action == "watch" and os.environ.get("AUTO_UPDATE") == "true"):
                state_path = workspace / "state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                for record in records:
                    key = record["project"] + ":" + record["latest"]
                    if record["update"] and key not in state:
                        gate = admission(config, profile) if args.action == "watch" or not args.ignore_window else {"allowed":True}
                        if not gate["allowed"]:
                            deferred = True
                            print(json.dumps({"status":"deferred", "project":record["project"], **gate}), flush=True)
                            delay = min(delay, gate.get("retry_seconds",300))
                            continue
                        result = run_job(record["project"], config, workspace, record["latest"],
                                         publish_code=args.publish, no_agent=args.no_agent, model_profile=profile)
                        state[key] = {"status": result["status"], "job": result["job"]}
                        failed = failed or result["status"] != "built"
                        save_json(state_path, state)
                        print(json.dumps(result), flush=True)
            if args.action != "watch":
                if args.action == "once" and failed:
                    raise SystemExit(1)
                if args.action == "once" and deferred:
                    raise SystemExit(2)
                break
            time.sleep(max(1, delay))


if __name__ == "__main__":
    main()
