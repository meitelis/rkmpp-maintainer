"""Fail-closed publication, owner acceptance, and isolated cleanup regressions."""
import datetime
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import maintainer
from runner import Docker


def qualified(job, image="sha256:qualified"):
    return {
        "status":"built", "kind":"release-candidate", "project":"memories",
        "target":"11.1.5", "job":str(job), "candidate_verified":True,"ready_to_deploy":False,
        "agent_review":{"review_complete":True}, "image_id":image,
        "hardware_qualification":{"ok":True,"image_id":image},
        "pull_request":"https://github.com/meitelis/memories-rkmpp/pull/100",
        "pull_request_number":100,
        "branch":"automation/memories-11.1.5-aabbccdd",
        "commit":"a"*40,
    }


class ReleaseFlowTests(unittest.TestCase):
    def test_watch_can_publish_only_after_opt_in_and_token(self):
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(action="watch",publish=False,no_agent=False,ignore_window=False)
            record=[{"project":"memories","latest":"11.1.5","update":True}]
            with patch.dict(os.environ,{"AUTO_UPDATE":"true","AUTO_PUBLISH":"true","GITHUB_TOKEN":"scoped"},clear=True), \
                 patch("maintainer.discover",return_value=record), \
                 patch("maintainer.admission",return_value={"allowed":True}), \
                 patch("maintainer.run_job",return_value={"status":"built","job":"qualified",
                     "candidate_verified":True,"ready_to_deploy":False,"delivery":{"image_id":"sha256:qualified"}}) as job:
                maintainer.poll_releases(args,{"poll_seconds":3600},Path(temp),"coding")
            self.assertTrue(job.call_args.kwargs["publish_code"])

    def test_missing_publication_token_blocks_expensive_unattended_work(self):
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(action="watch",publish=False,no_agent=False,ignore_window=False)
            with patch.dict(os.environ,{"AUTO_UPDATE":"true","AUTO_PUBLISH":"true"},clear=True), \
                 patch("maintainer.discover",return_value=[]), patch("maintainer.run_job") as job:
                _,failed,_=maintainer.poll_releases(args,{},Path(temp),"coding")
            self.assertTrue(failed)
            job.assert_not_called()

    def test_pending_publication_retries_without_rebuilding_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)
            (workspace/"state.json").write_text(json.dumps({
                "memories:11.1.5":{"status":"publication-pending","job":str(workspace/"jobs/memories-11.1.5-01234567"),"attempts":1}}))
            args=SimpleNamespace(action="watch",publish=False,no_agent=False,ignore_window=False)
            record=[{"project":"memories","latest":"11.1.5","update":True}]
            with patch.dict(os.environ,{"AUTO_UPDATE":"true","AUTO_PUBLISH":"true","GITHUB_TOKEN":"scoped"},clear=True), \
                 patch("maintainer.discover",return_value=record), \
                 patch("maintainer.publish_existing",return_value={
                     "status":"built","job":str(workspace/"jobs/memories-11.1.5-01234567"),
                     "pull_request":"https://github.com/meitelis/memories-rkmpp/pull/100"}) as publisher, \
                 patch("maintainer.run_job") as job:
                _,failed,_=maintainer.poll_releases(args,{},workspace,"coding")
            self.assertFalse(failed)
            self.assertEqual(publisher.call_count,1)
            job.assert_not_called()
            self.assertEqual(json.loads((workspace/"state.json").read_text())["memories:11.1.5"]["status"],"built")

    def test_confirmation_cannot_be_inferred_from_health_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError,"explicit source review"):
                maintainer.accept_deployed_candidate(Path(temp),"job","sha256:qualified")
                
    def test_publication_retry_rejects_tampered_source_before_remote_write(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"jobs"/"memories-11.1.5-aabbccdd"
            (job/"source").mkdir(parents=True)
            result=qualified(job)
            result["status"]="publication-pending"
            saved=b"original diff\n"
            result["candidate_diff_sha256"]=hashlib.sha256(saved).hexdigest()
            (job/"candidate.diff").write_bytes(saved)
            (job/"result.json").write_text(json.dumps(result))
            with patch("maintainer.git",return_value="different diff"), \
                 patch("maintainer.publish") as publish:
                with self.assertRaisesRegex(RuntimeError,"source changed"):
                    maintainer.publish_existing(Path(temp),job.name)
            publish.assert_not_called()

    def test_accept_rejects_wrong_running_image_without_contacting_github(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"jobs"/"memories-11.1.5-aabbccdd"
            job.mkdir(parents=True)
            (job/"result.json").write_text(json.dumps(qualified(job)))
            with patch.dict(os.environ,{"GITHUB_TOKEN":"scoped"}), patch("maintainer.github") as github:
                with self.assertRaisesRegex(RuntimeError,"does not match"):
                    maintainer.accept_deployed_candidate(Path(temp),job.name,"sha256:other",True,True)
            github.assert_not_called()

    def test_accept_rejects_a_moved_main_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"jobs"/"memories-11.1.5-aabbccdd"
            job.mkdir(parents=True)
            (job/"result.json").write_text(json.dumps(qualified(job)))
            (job/"release.json").write_text(json.dumps({"fork_base":"b"*40}))
            with patch.dict(os.environ,{"GITHUB_TOKEN":"scoped"}), \
                 patch("maintainer.github",return_value={"object":{"sha":"c"*40}}):
                with self.assertRaisesRegex(RuntimeError,"main moved"):
                    maintainer.accept_deployed_candidate(Path(temp),job.name,"sha256:qualified",True,True)

    def test_confirmed_image_and_exact_pr_with_green_ci_can_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"jobs"/"memories-11.1.5-aabbccdd"
            job.mkdir(parents=True)
            (job/"result.json").write_text(json.dumps(qualified(job)))
            (job/"release.json").write_text(json.dumps({"fork_base":"b"*40}))
            commands=[]
            def github(path,data=None,method=None):
                commands.append((path,method))
                if path.endswith("/git/ref/heads/main"): return {"object":{"sha":"b"*40}}
                if path.endswith("/pulls/100"): return {"state":"open","draft":False,
                    "head":{"sha":"a"*40,"ref":"automation/memories-11.1.5-aabbccdd"},
                    "base":{"ref":"main","sha":"b"*40}}
                if path.endswith("/check-runs?per_page=100"): return {"total_count":2,"check_runs":[
                    {"status":"completed","conclusion":"success"} for _ in range(2)]}
                if path.endswith("/pulls/100/merge"): return {"merged":True,"sha":"d"*40}
                if method=="DELETE": return {}
                raise AssertionError(path)
            with patch.dict(os.environ,{"GITHUB_TOKEN":"scoped"}), patch("maintainer.github",side_effect=github):
                result=maintainer.accept_deployed_candidate(
                    Path(temp),job.name,"sha256:qualified",True,True)
            self.assertEqual(result["status"],"merged")
            self.assertTrue(json.loads((job/"result.json").read_text())["deployment_approved"])
            self.assertEqual([method for _,method in commands if method=="PUT"],["PUT"])
            self.assertEqual([method for _,method in commands if method=="DELETE"],["DELETE"])

    def test_ci_failure_blocks_merge_even_when_owner_accepts(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"jobs"/"memories-11.1.5-aabbccdd"
            job.mkdir(parents=True)
            (job/"result.json").write_text(json.dumps(qualified(job)))
            (job/"release.json").write_text(json.dumps({"fork_base":"b"*40}))
            commands=[]
            def github(path,data=None,method=None):
                commands.append((path,method))
                if path.endswith("/git/ref/heads/main"):return {"object":{"sha":"b"*40}}
                if path.endswith("/pulls/100"):return {"state":"open","draft":False,
                    "head":{"sha":"a"*40,"ref":"automation/memories-11.1.5-aabbccdd"},
                    "base":{"sha":"b"*40,"ref":"main"}}
                if path.endswith("/check-runs?per_page=100"):return {
                    "total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}
                raise AssertionError(path)
            with patch.dict(os.environ,{"GITHUB_TOKEN":"scoped"}),patch("maintainer.github",side_effect=github):
                with self.assertRaisesRegex(RuntimeError,"CI checks"):
                    maintainer.accept_deployed_candidate(Path(temp),job.name,"sha256:qualified",True,True)
            self.assertFalse(any(method=="PUT" for _,method in commands))

    def test_failed_candidate_cleanup_refuses_unrelated_images(self):
        with tempfile.TemporaryDirectory() as temp:
            docker=Docker({},temp)
            with patch.object(docker,"verify"),patch("runner.command") as command:
                with self.assertRaisesRegex(RuntimeError,"non-candidate"):
                    docker.discard_failed_candidate("local/memories-rkmpp:11.1.4-candidate")
                command.assert_not_called()
                command.side_effect=["sha256:failed","removed"]
                docker.discard_failed_candidate("local/memories-rkmpp:candidate-11.1.5-aabbccdd")
                self.assertEqual(command.call_args.args[0][:3],["docker","image","rm"])
                

if __name__ == "__main__":
    unittest.main()
