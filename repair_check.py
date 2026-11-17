"""Qualify a real Go integration repair on a disposable current-fork checkout."""
import datetime
import difflib
import hashlib
import json
import traceback
import uuid
from pathlib import Path

from agent import run_agent
from core import command, save_json
from evidence import record_runtime, save_manifest
from limits import job_budget
from maintainer import clone
from profiles import git, validate
from runner import Docker
from model_settings import settings
from schedule import wait_for_window


# This is an independent startup contract, not a model-editable test. Direct
# top-level calls must execute before server creation; comments/dead branches do
# not satisfy it. Existing application tests and the actual image build follow.
STARTUP_CHECK = r'''package main
import (
    "go/ast"
    "go/parser"
    "go/token"
    "testing"
)
func TestMaintainerStartupHTTPContract(t *testing.T) {
    f, err := parser.ParseFile(token.NewFileSet(), "main.go", nil, 0)
    if err != nil { t.Fatal(err) }
    var body *ast.BlockStmt
    for _, decl := range f.Decls {
        if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == "main" { body = fn.Body }
    }
    if body == nil { t.Fatal("STARTUP_HTTP_CONTRACT: missing main") }
    calls := map[string]int{}
    server := -1
    for i, stmt := range body.List {
        ast.Inspect(stmt, func(n ast.Node) bool {
            call, ok := n.(*ast.CallExpr); if !ok { return true }
            fn, ok := call.Fun.(*ast.SelectorExpr); if !ok { return true }
            pkg, ok := fn.X.(*ast.Ident)
            if ok && pkg.Name == "api" && fn.Sel.Name == "NewServer" { server = i }
            return true
        })
        expr, ok := stmt.(*ast.ExprStmt); if !ok { continue }
        call, ok := expr.X.(*ast.CallExpr); if !ok || len(call.Args) != 1 { continue }
        fn, ok := call.Fun.(*ast.SelectorExpr); if !ok { continue }
        pkg, ok := fn.X.(*ast.Ident)
        if !ok || pkg.Name != "ffmpeg" || fn.Sel.Name != "DetectHTTPOptions" { continue }
        arg, ok := call.Args[0].(*ast.SelectorExpr); if !ok { continue }
        cfg, ok := arg.X.(*ast.Ident)
        if ok && cfg.Name == "c" { calls[arg.Sel.Name] = i }
    }
    if server < 0 { t.Fatal("STARTUP_HTTP_CONTRACT: missing server creation") }
    for _, name := range []string{"FFmpeg", "FFprobe"} {
        i, exists := calls[name]
        if !exists || i >= server { t.Fatalf("STARTUP_HTTP_CONTRACT: %s must be probed before workers start", name) }
    }
}
'''


def real_repair(config, workspace, model_profile="coding"):
    job = Path(workspace) / "jobs" / ("memories-repair-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"status":"preparing", "kind":"real-integration-repair-qualification", "job":str(job),
              "project":"memories", "deployment_approved":False, "source_edit_verified":False}
    save_json(job / "result.json", result)
    try:
        save_manifest(job, config, Path(__file__).parent, smoke=model_profile == "smoke")
        with job_budget(config, workspace):
            docker = Docker(config, workspace)
            docker.reap()
            root, reference = job / "source", job / "reference"
            clone("meitelis/memories-rkmpp", root)
            result["fork_base"] = git(root, "rev-parse", "HEAD")
            source = root / "go-vod/main.go"
            original = source.read_text()
            needle = "\tffmpeg.DetectHTTPOptions(c.FFmpeg)\n"
            if original.count(needle) != 1:
                raise RuntimeError("Qualification baseline no longer contains the known two-binary startup contract")
            (reference / "go-vod").mkdir(parents=True)
            (reference / "go-vod/main.go").write_text(original)
            broken = original.replace(needle, "", 1)
            source.write_text(broken)
            (root / "go-vod/maintainer_startup_test.go").write_text(STARTUP_CHECK)
            (job / "seed.diff").write_text("".join(difflib.unified_diff(original.splitlines(True), broken.splitlines(True))))
            result["phase"] = "prove-seed-failure"
            save_json(job / "result.json", result)
            try:
                docker.run(config["go_image"], root, ["sh", "-ec",
                    "cd go-vod && gofmt -w maintainer_startup_test.go && go test -count=1 -run '^TestMaintainerStartupHTTPContract$' ."],
                    job / "seed-check.log", timeout=900)
            except RuntimeError as error:
                if "STARTUP_HTTP_CONTRACT" not in str(error):
                    raise
            else:
                raise RuntimeError("Known broken real source unexpectedly passed its independent startup check")
            result["phase"] = "model-repair"
            save_json(job / "result.json", result)
            with docker.model(smoke=model_profile == "smoke", metrics_path=job / "model-runtime.json") as endpoint:
                record_runtime(job, docker)
                review = run_agent(root, reference,
                    "Repair the missing HTTP-option capability probe in go-vod/main.go. Both configured binaries "
                    "c.FFmpeg and c.FFprobe must be probed before workers/server start. The upstream tool provides "
                    "the unmodified CURRENT FORK reference for this qualification exercise, not a newer release. "
                    "Read local go-vod/main.go and action upstream path go-vod/main.go. Make the smallest real source "
                    "repair, preserve all other integration behavior, and finish. Do not modify any tests.",
                    lambda _:"".join(difflib.unified_diff(broken.splitlines(True), original.splitlines(True))),
                    endpoint, "memories", job / "agent.jsonl", turns=12, generation=settings(config, model_profile)[2],
                    required_observations={"read":["go-vod/main.go"], "upstream":["go-vod/main.go"]}, require_edit=True)
            result["phase"] = "checks-and-build"
            save_json(job / "result.json", result)
            save_json(job / "agent-review.json", review)
            validate(root)
            docker.checks("memories", root, job / "checks.log")
            tag = "local/memories-rkmpp:repair-qualified-" + job.name[-8:]
            docker.build("memories", root, tag, job / "build.log")
            result["hardware_test_image_id"] = command(["docker","image","inspect",
                "local/memories-rkmpp:v11-smoke","--format","{{.Id}}"],timeout=30)
            result.update(status="passed", source_edit_verified=True, image=tag,
                          image_id=docker.export(tag, job / "image.tar"), agent_review=review)
            (job / "repair.diff").write_text("".join(difflib.unified_diff(broken.splitlines(True), source.read_text().splitlines(True))))
            result["final_main_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    except Exception as error:
        result.update(status="failed", error=str(error))
        (job / "error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)
    return result


if __name__ == "__main__":
    import argparse
    import os
    from core import locked
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-profile", choices=["coding", "smoke"], default="coding")
    parser.add_argument("--ignore-window", action="store_true")
    parser.add_argument("--wait-for-window", action="store_true")
    args = parser.parse_args()
    workspace = Path(os.environ.get("WORKSPACE", "/workspace"))
    config = json.loads((Path(__file__).parent / "config.json").read_text())
    workspace.mkdir(parents=True, exist_ok=True)
    if config.get("work_window", {}).get("enabled", False):
        with locked(workspace / "maintainer.lock"):
            Docker(config, workspace).reap()
    if not wait_for_window(config, args.model_profile, wait=args.wait_for_window, ignore=args.ignore_window)["allowed"]:
        raise SystemExit(2)
    with locked(workspace / "maintainer.lock"):
        result = real_repair(config, workspace, model_profile=args.model_profile)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "passed" else 1)
