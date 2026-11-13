"""Run the qualification stages independently of an SSH/browser connection."""
import datetime
import hashlib
import json
import re
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
            wait_for_window(config,profile,wait=True,ignore=ignore_window)
            with locked(workspace/"maintainer.lock"):
                recover_jobs(workspace)
                state.update(status="running")
                state["steps"][name]={"status":"running"}
                save_json(path,state)
                result=execute()
            passed=result.get("status")==expected
            state["steps"][name]={"status":"passed" if passed else "failed","result":result}
            save_json(path,state)
            if not passed:
                state.update(status="failed",error="Qualification step failed: "+name)
                break
        else:
            if identity["external_model_endpoint"]:
                raise RuntimeError("External model identity cannot be qualified by this local suite")
            smoke_job=Path(state["steps"]["model-smoke"]["result"]["job"])
            observed=json.loads((smoke_job/"run-manifest.json").read_text())
            memory=json.loads((smoke_job/"model-runtime.json").read_text())
            if not observed.get("model_identity_verified"):
                raise RuntimeError("The selected model identity was not verified")
            cgroup=memory.get("cgroup_memory",{})
            peak=int(cgroup.get("memory.peak",cgroup.get("memory/memory.max_usage_in_bytes",0)))
            if not peak or memory.get("state",{}).get("OOMKilled"):
                raise RuntimeError("Peak model memory/OOM evidence is missing or failed")
            ceiling=memory_bytes(settings(config,profile)[1].get("memory","12g"))
            if peak>ceiling:
                raise RuntimeError("Observed model memory exceeds its qualified ceiling")
            state["peak_model_memory_bytes"]=peak
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
