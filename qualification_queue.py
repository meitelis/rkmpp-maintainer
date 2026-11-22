"""Run the qualification stages independently of an SSH/browser connection."""
import datetime
import hashlib
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path

from core import command, locked, save_json
from evidence import manifest
from limits import job_budget
from maintainer import recover_jobs, run_job, smoke
from qualification import runtime_checks
from repair_check import real_repair
from runner import Docker
from schedule import wait_for_window
from model_settings import memory_bytes, settings


MAX_INTERRUPTED_STAGE_RETRIES = 1  # Crash-only; actual test failures never retry.


def hardware_checks(config, workspace, repair_result):
    """Qualify the repaired shipping image through the release hardware gate.

    The separate v11-smoke tag is rebuilt alongside a shipping image, but its
    identity alone proves nothing about the binary that would be exported.
    Docker.hardware runs the three RKMPP suites and compares both go-vod
    binaries byte-for-byte. Do not qualify a floating smoke tag independently.
    """
    workspace = Path(workspace).resolve()
    repair_job = Path(repair_result.get("job", "")).resolve()
    root = (repair_job / "source").resolve()
    tag = repair_result.get("image")
    expected_id = repair_result.get("image_id")
    if (repair_result.get("status") != "passed" or
            repair_job.parent != (workspace / "jobs").resolve() or
            root.parent != repair_job or not root.is_dir() or
            not isinstance(tag, str) or
            not re.fullmatch(r"local/memories-rkmpp:repair-qualified-[a-f0-9]{8}", tag) or
            not isinstance(expected_id, str) or
            not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_id)):
        raise RuntimeError("Hardware qualification requires an exact successful repaired image")

    job = workspace / "jobs" / ("hardware-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"status": "running", "kind": "repaired-shipping-image-hardware-qualification",
              "job": str(job), "source_job": str(repair_job),
              "image": tag, "expected_image_id": expected_id}
    save_json(job / "result.json", result)
    try:
        with job_budget(config, workspace):
            docker = Docker(config, workspace)
            docker.verify()
            observed_id = command(["docker", "image", "inspect", tag,
                                   "--format", "{{.Id}}"], timeout=30)
            if observed_id != expected_id:
                raise RuntimeError("Repaired shipping image changed before hardware qualification")
            evidence = docker.hardware("memories", root, tag, job / "hardware.log")
            if (not isinstance(evidence, dict) or evidence.get("ok") is not True or
                    evidence.get("image_id") != expected_id or
                    not evidence.get("go_vod_sha256")):
                raise RuntimeError("Hardware matrix did not prove shipping binary identity")
            result.update(status="passed", image_id=expected_id, hardware_qualification=evidence)
    except Exception as error:
        result.update(status="failed", error=str(error))
        (job / "error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)
    return result


def qualified_model_memory(state, config, profile):
    """Require independently recorded memory and identity for every LLM stage.

    A run may contain several repair attempts. Each attempt that started the
    model must have usable metrics, otherwise the release cannot be qualified.
    """
    ceiling=memory_bytes(settings(config,profile)[1].get("memory","12g"))
    peaks=[]
    for stage in ("model-smoke","memories-replay","real-go-repair","immich-upgrade"):
        result=state.get("steps",{}).get(stage,{}).get("result",{})
        job_path=result.get("job")
        if not job_path:
            raise RuntimeError("Missing job path for model-backed stage: "+stage)
        job=Path(job_path)
        source=job/"run-manifest.json"
        if not source.is_file():
            raise RuntimeError("Missing model identity manifest for "+stage)
        observed=json.loads(source.read_text())
        if observed.get("model_identity_verified") is not True:
            raise RuntimeError("Model identity could not be verified for "+stage)
        files=sorted(job.glob("model-runtime*.json"))
        if not files:
            raise RuntimeError("Missing model memory evidence for "+stage)
        for path in files:
            memory=json.loads(path.read_text())
            if memory.get("sampling_errors"):
                raise RuntimeError("Model memory sampling failed for "+stage+": "+path.name)
            cgroup=memory.get("cgroup_memory",{})
            try:
                peak=int(cgroup.get("memory.peak",cgroup.get("memory/memory.max_usage_in_bytes",0)))
            except (TypeError, ValueError):
                peak=0
            if not peak or memory.get("state",{}).get("OOMKilled") is not False:
                raise RuntimeError("Missing/failed model memory or OOM evidence for "+stage+": "+path.name)
            if peak>ceiling:
                raise RuntimeError("Model memory exceeded qualified ceiling for "+stage+": "+path.name)
            peaks.append(peak)
    return max(peaks)


def run_suite(config, workspace, profile="coding", ignore_window=False, cases=None):
    workspace=Path(workspace)
    identity=manifest(config,Path(__file__).parent,smoke=profile=="smoke")
    key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:12]
    directory=workspace/"qualifications"/key
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/"result.json"
    cases=cases or [
        ("runtime",lambda:runtime_checks(config,workspace),"passed"),
        ("model-smoke",lambda:smoke(config,workspace,model_profile=profile),"ready"),
        ("memories-replay",lambda:run_job("memories",config,workspace,"11.1.4",
            base_ref="7a6840dc98b4",source_version="11.1.2",model_profile=profile),"built"),
        ("real-go-repair",lambda:real_repair(config,workspace,model_profile=profile),"passed"),
        ("hardware",lambda:hardware_checks(config,workspace,
            state["steps"]["real-go-repair"]["result"]),"passed"),
        # Current release transition rehearsals use pinned previous-version
        # fork snapshots. These validate today's integration changes, never a
        # hypothetical future release or production client playback.
        ("immich-upgrade",lambda:run_job("immich",config,workspace,"3.4.2",
            base_ref="2cb02a166dac",source_version="3.4.1",model_profile=profile),"built"),
    ]
    state=json.loads(path.read_text()) if path.exists() else {"status":"pending","identity":identity,
        "deployment_approved":False,"model_profile":profile,"window_override":ignore_window,"steps":{}}
    if state["status"] in {"failed","interrupted"}:
        raise RuntimeError("This qualification revision stopped; preserve evidence and review its failure before retrying: "+str(path))
    if state["status"] == "qualified-for-candidate-preparation":
        return state  # A completed revision never runs twice after a restart.
    try:
        for name,execute,expected in cases:
            previous=state["steps"].get(name,{})
            if previous.get("status") == "passed":
                continue
            if previous.get("status") == "failed":
                state.update(status="failed", error="Persisted failed stage requires review: " + name)
                break
            if previous.get("status") == "running":
                # Exclusive qualification lock plus isolated-daemon recovery must
                # have succeeded before this crash-only retry is considered.
                retries = state.setdefault("restart_retries", {}).get(name, 0)
                if retries >= MAX_INTERRUPTED_STAGE_RETRIES:
                    state.update(status="interrupted", error=(
                        "Worker repeatedly stopped during " + name + "; evidence requires review"))
                    break
                state["restart_retries"][name] = retries + 1
                state.setdefault("interruption_history", []).append({
                    "step": name, "abandoned_step": previous,
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "action": "retry unfinished stage as a fresh isolated job"})
                state["steps"].pop(name)
                save_json(path, state)
            state.update(status="waiting",next_step=name)
            save_json(path,state)
            print(json.dumps({"qualification":key,"status":"waiting","next_step":name}),flush=True)
            # Contention with an unrelated active maintainer job is not a
            # failed hardware/LLM test. Keep the unattempted stage pending.
            while True:
                wait_for_window(config,profile,wait=True,ignore=ignore_window)
                attempted=False
                try:
                    with locked(workspace/"maintainer.lock"):
                        recover_jobs(workspace)
                        state.update(status="running")
                        state["steps"][name]={"status":"running"}
                        save_json(path,state)
                        attempted=True
                        result=execute()
                    break
                except RuntimeError as error:
                    if attempted or str(error)!="Another maintainer job is running":
                        raise
                    state.update(status="waiting",next_step=name)
                    state["steps"].pop(name,None)
                    save_json(path,state)
                    print(json.dumps({"qualification":key,"status":"busy","next_step":name}),flush=True)
                    time.sleep(max(1,min(int(config.get("work_window",{}).get("idle_retry_seconds",300)),60)))
            passed=result.get("status")==expected
            state["steps"][name]={"status":"passed" if passed else "failed","result":result}
            save_json(path,state)
            if not passed:
                state.update(status="failed",error="Qualification step failed: "+name)
                break
        else:
            if identity["external_model_endpoint"]:
                raise RuntimeError("External model identity cannot be qualified by this local suite")
            state["peak_model_memory_bytes"]=qualified_model_memory(state,config,profile)
            state.update(status="qualified-for-candidate-preparation",completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    except Exception as error:
        state.update(status="failed",error=str(error))
        (directory/"error.txt").write_text(traceback.format_exc())
    finally:
        state["report"]=str(path)
        save_json(path,state)
    print(json.dumps({"status":state["status"],"report":str(path)}),flush=True)
    return state


def qualification_once(config, workspace, profile="coding", ignore_window=False):
    """One run, never parallel to a detached or second supervised worker."""
    workspace = Path(workspace).resolve()
    with locked(workspace / "qualification.lock"):
        with locked(workspace / "maintainer.lock"):
            recover_jobs(workspace)
            Docker(config, workspace).reap()  # Inner daemon only.
        return run_suite(config, workspace, profile, ignore_window)


def write_supervisor_status(workspace, status, **extra):
    """Portainer-visible status, separate from each stage's detailed evidence."""
    data = {"status": status,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **extra}
    save_json(Path(workspace) / "qualifier-status.json", data)
    return data


def supervisor_health(workspace):
    """Healthy means active, waiting, completed, or intentionally disabled.

    A failed worker remains alive for diagnostics but must be UNHEALTHY in
    Docker rather than showing as a misleading healthy idle container.
    """
    try:
        state = json.loads((Path(workspace) / "qualifier-status.json").read_text())
    except (OSError, ValueError, TypeError):
        return {"healthy": False, "status": "missing-or-corrupt-supervisor-status"}
    status = state.get("status") if isinstance(state, dict) else None
    if status not in {"disabled", "busy", "running", "completed", "blocked"}:
        return {"healthy": False, "status": "invalid-supervisor-status"}
    return {"healthy": status != "blocked", "status": status,
            "reason": state.get("reason")}


def supervise(config, workspace, profile="coding", enabled=False):
    """Docker PID 1: restart after crash, report terminal failure as unhealthy."""
    if not enabled:
        write_supervisor_status(workspace, "disabled",
                                reason="QUALIFICATION_ENABLED=false")
        print(json.dumps({"status":"idle","reason":"QUALIFICATION_ENABLED=false"}), flush=True)
    else:
        while True:
            try:
                write_supervisor_status(workspace, "running",
                                        reason="qualification worker active")
                result = qualification_once(config, workspace, profile)
                successful = result["status"] == "qualified-for-candidate-preparation"
                write_supervisor_status(
                    workspace, "completed" if successful else "blocked",
                    reason=result["status"], report=result.get("report"))
                print(json.dumps({"status":result["status"],"report":result.get("report")}), flush=True)
                break
            except Exception as error:
                if isinstance(error, RuntimeError) and str(error) == "Another maintainer job is running":
                    write_supervisor_status(workspace, "busy",
                                            reason="Another worker owns qualification lock")
                    print(json.dumps({"status":"busy","retry_seconds":60,
                                      "reason":"Existing worker holds qualification or maintenance lock"}), flush=True)
                    time.sleep(60)
                    continue
                write_supervisor_status(workspace, "blocked", reason=str(error)[:1000])
                print(json.dumps({"status":"blocked","reason":str(error)[:1000]}), flush=True)
                break
    # Preserve terminal failure evidence without Docker restart looping.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-profile", choices=["coding","smoke"], default="coding")
    parser.add_argument("--ignore-window", action="store_true")
    parser.add_argument("--serve", action="store_true", help="Run as Portainer foreground service")
    parser.add_argument("--health", action="store_true", help="Portainer health probe")
    args = parser.parse_args()
    config = json.loads((Path(__file__).parent / "config.json").read_text())
    workspace = Path(os.environ.get("WORKSPACE", "/workspace"))
    workspace.mkdir(parents=True, exist_ok=True)
    if args.health:
        observed = supervisor_health(workspace)
        print(json.dumps(observed), flush=True)
        raise SystemExit(0 if observed["healthy"] else 1)
    if args.serve:
        if args.ignore_window:
            parser.error("Supervised qualification cannot bypass the night/idle window")
        supervise(config, workspace, args.model_profile,
                  enabled=os.environ.get("QUALIFICATION_ENABLED") == "true")
    else:
        result = qualification_once(config, workspace, args.model_profile, args.ignore_window)
        raise SystemExit(0 if result["status"] == "qualified-for-candidate-preparation" else 1)
