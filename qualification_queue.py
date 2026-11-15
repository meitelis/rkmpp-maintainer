"""Run the qualification stages independently of an SSH/browser connection."""
import datetime
import hashlib
import json
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


def hardware_checks(config, workspace, expected_image_id):
    job=Path(workspace)/"jobs"/("hardware-"+uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result={"status":"running","kind":"offline-radxa-hardware-qualification","job":str(job),"tests":{}}
    save_json(job/"result.json",result)
    try:
        with job_budget(config,workspace):
            docker=Docker(config,workspace)
            docker.verify()
            image="local/memories-rkmpp:v11-smoke"
            result["image_id"]=command(["docker","image","inspect",image,"--format","{{.Id}}"],timeout=30)
            if result["image_id"]!=expected_image_id:
                raise RuntimeError("Hardware test image changed after the qualified source build")
            cache=job/"cache"
            cache.mkdir()
            # The pinned go-vod image sets TMPDIR=/cache/tmp. Go testing.T.TempDir
            # fails before any VPU check if that directory does not exist.
            # Create it inside this isolated job cache, never in host Docker.
            temporary=cache/"tmp"
            temporary.mkdir(mode=0o1777)
            temporary.chmod(0o1777)
            devices=["/dev/mpp_service","/dev/rga","/dev/dma_heap","/dev/dri","/dev/mali0"]
            for suite,extra in [("ffmpeg","TestSegmentArgsRestartFMP4"),("core","TestFMP4SeekRestart"),("api",None)]:
                name="rkmpp-maintainer-hardware-"+uuid.uuid4().hex[:12]
                selection="^(TestRKMPPHardware.*"+("|"+extra if extra else "")+")$"
                args=["docker","run","--rm","--name",name,"--label","rkmpp.maintainer.transient=true",
                    "--network","none","--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
                    "--security-opt","systempaths=unconfined","--pids-limit","256","--cpus","4",
                    "--memory","4g","--memory-swap","4g","--user","0:0",
                    "--tmpfs","/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                    "--mount","type=bind,src="+str(cache)+",dst=/cache"]
                for dev in devices:args += ["--device",dev+":"+dev]
                args += ["-e","MEMORIES_RKMPP_HW_TEST=1","--entrypoint","/app/"+suite+".test",
                         image,"-test.run",selection,"-test.v","-test.timeout","5m"]
                try:
                    output=command(args,timeout=420,log=job/(suite+".log"))
                finally:
                    docker.cleanup(name)
                with (job/(suite+".log")).open() as handle:
                    passed=sum(bool(re.search(r"--- PASS: TestRKMPPHardware",line)) for line in handle)
                if not passed or "warning: no tests to run" in output:
                    raise RuntimeError("Hardware suite did not execute passing tests: "+suite)
                result["tests"][suite]={"status":"passed","hardware_pass_records":passed}
                save_json(job/"result.json",result)
            result["status"]="passed"
    except Exception as error:
        result.update(status="failed",error=str(error))
        (job/"error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job/"result.json",result)
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
        ("memories-replay",lambda:run_job("memories",config,workspace,"11.1.2",
            base_ref="d6a9471dfbd3dd721bd67698c062ddcc773e232c",source_version="11.1.1",model_profile=profile),"built"),
        ("real-go-repair",lambda:real_repair(config,workspace,model_profile=profile),"passed"),
        ("hardware",lambda:hardware_checks(config,workspace,
            state["steps"]["real-go-repair"]["result"]["hardware_test_image_id"]),"passed"),
        ("immich-upgrade",lambda:run_job("immich",config,workspace,"3.4.1",model_profile=profile),"built"),
    ]
    state=json.loads(path.read_text()) if path.exists() else {"status":"pending","identity":identity,
        "deployment_approved":False,"model_profile":profile,"window_override":ignore_window,"steps":{}}
    if state["status"] in {"failed","interrupted"}:
        raise RuntimeError("This qualification revision stopped; preserve evidence and review its failure before retrying: "+str(path))
    try:
        for name,execute,expected in cases:
            previous=state["steps"].get(name,{})
            if previous.get("status")=="passed":continue
            if previous.get("status")=="running":
                state.update(status="interrupted",error="Previous qualification worker stopped during "+name)
                break
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


if __name__=="__main__":
    import argparse,os
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-profile",choices=["coding","smoke"],default="coding")
    parser.add_argument("--ignore-window",action="store_true")
    args=parser.parse_args()
    config=json.loads((Path(__file__).parent/"config.json").read_text())
    workspace=Path(os.environ.get("WORKSPACE","/workspace"))
    workspace.mkdir(parents=True,exist_ok=True)
    with locked(workspace/"qualification.lock"):
        with locked(workspace/"maintainer.lock"):
            recover_jobs(workspace)
            Docker(config,workspace).reap()
        result=run_suite(config,workspace,args.model_profile,args.ignore_window)
        raise SystemExit(0 if result["status"]=="qualified-for-candidate-preparation" else 1)
