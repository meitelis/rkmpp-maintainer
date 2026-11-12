#!/usr/bin/env python3
"""Local RKMPP release maintenance. Candidates only; deployment is manual."""
import argparse
import contextlib
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
from runner import Docker

HERE = Path(__file__).resolve().parent


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


def run_job(project, config, workspace, target, base_ref="main", source_version=None, publish_code=False, no_agent=False):
    job = workspace / "jobs" / (project + "-" + target + "-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"project": project, "target": target, "job": str(job), "status": "preparing"}
    save_json(job / "result.json", result)
    try:
        docker = Docker(config, workspace)
        docker.verify()
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
                # Model lifetime ends before compiler/build memory is needed.
                with docker.model() as endpoint:
                    summary = run_agent(root, upstream, task + "\nLast check failures:\n" + errors[-10000:],
                        lambda p: git(upstream, "diff", "base", "target", "--", p), endpoint,
                        project, job / "agent.jsonl", turns=config["agent_turns"])
                    (job / "agent-summary.txt").write_text(summary + "\n")
            try:
                validate(root)
                docker.checks(project, root, job / f"checks-{attempt}.log")
                docker.build(project, root, tag, job / f"build-{attempt}.log")
                break
            except RuntimeError as error:
                errors = str(error)
                if attempt + 1 == (1 if no_agent else config["repair_rounds"]):
                    raise
        result.update(status="ready", image=tag,
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


def smoke(config, workspace):
    """A real local-model edit, immutable test, Docker build and image export."""
    job = workspace / "jobs" / ("smoke-" + uuid.uuid4().hex[:8])
    root = job / "source"
    (root / "go-vod").mkdir(parents=True)
    (root / "tests").mkdir()
    source = root / "go-vod/calc.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    (root / "tests/test_calc.py").write_text(
        "import sys, unittest\nsys.path.insert(0, 'go-vod')\nfrom calc import add\n"
        "class TestAdd(unittest.TestCase):\n def test_add(self):\n  self.assertEqual(add(2, 3), 5)\n  self.assertEqual(add(-2, 5), 3)\n")
    (root / "Dockerfile").write_text("FROM " + config["python_image"] + "\nWORKDIR /app\nCOPY . .\nRUN python -m unittest discover -s tests -v\nCMD [\"python\", \"-c\", \"print('maintainer smoke passed')\"]\n")
    docker = Docker(config, workspace)
    task = "The function add(a,b) in go-vod/calc.py must add a and b, but currently subtracts. Read this file, use replace to fix that one operator, then finish. Do not change tests."
    with docker.model(smoke=True) as endpoint:
        run_agent(root, root, task, lambda _: "", endpoint, "memories", job / "agent.jsonl", turns=10)
    docker.checks("fixture", root, job / "checks.log")
    tag = "local/rkmpp-maintainer:smoke-" + job.name[-8:]
    docker.build("fixture", root, tag, job / "build.log")
    result = {"status": "ready", "kind": "real-local-model-smoke", "job": str(job),
              "image": tag, "image_id": docker.export(tag, job / "image.tar")}
    save_json(job / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "once", "watch", "prepare", "run", "smoke"])
    parser.add_argument("--repo", choices=PROJECTS)
    parser.add_argument("--target")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--source-version")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-agent", action="store_true", help="Test trusted reconciliation/builds without asking a model")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("WORKSPACE", "/workspace")))
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    with locked(workspace / "maintainer.lock"):
        if args.action == "smoke":
            print(json.dumps(smoke(config, workspace), indent=2))
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
                             args.base_ref, args.source_version, args.publish, args.no_agent)
            print(json.dumps(result, indent=2))
            if result["status"] != "ready":
                raise SystemExit(1)
            return
        while True:
            records = discover(workspace)
            print(json.dumps(records), flush=True)
            if args.action == "once" or (args.action == "watch" and os.environ.get("AUTO_UPDATE") == "true"):
                state_path = workspace / "state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                for record in records:
                    key = record["project"] + ":" + record["latest"]
                    if record["update"] and key not in state:
                        result = run_job(record["project"], config, workspace, record["latest"],
                                         publish_code=args.publish, no_agent=args.no_agent)
                        state[key] = {"status": result["status"], "job": result["job"]}
                        save_json(state_path, state)
                        print(json.dumps(result), flush=True)
            if args.action != "watch":
                break
            time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    main()
