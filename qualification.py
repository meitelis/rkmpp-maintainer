"""Exercise failure handling against the isolated daemon before model evaluation."""
import datetime
import json
import time
import tempfile
import traceback
import uuid
from pathlib import Path

from core import command, save_json
from delivery import verify_archive_config
from evidence import save_manifest
from limits import job_budget
from repair_fixture import create_fixture
from runner import Docker, PHP_LINT_CHECK


def runtime_checks(config, workspace):
    job = Path(workspace) / "jobs" / ("runtime-" + uuid.uuid4().hex[:8])
    job.mkdir(parents=True)
    result = {"status":"running", "kind":"isolated-runtime-qualification", "job":str(job),
              "model_used":False, "checks":{}}
    save_json(job / "result.json", result)
    try:
        save_manifest(job, config, Path(__file__).parent)
        with job_budget(config, workspace):
            docker = Docker(config, workspace)
            docker.verify()
            docker.reap()
            # Pull before applying a two-second execution timeout.
            command(["docker", "pull", config["python_image"]], timeout=600, log=job / "pull.log")
            command(["docker", "pull", config["php_image"]], timeout=600, log=job / "pull.log")
            # Exercise the same manifest/config check used for release delivery
            # with an archive produced by the actual isolated Docker daemon.
            tag = "local/rkmpp-export-check:" + uuid.uuid4().hex
            command(["docker", "tag", config["python_image"], tag], timeout=30)
            try:
                with tempfile.TemporaryDirectory(dir=job) as directory:
                    archive = Path(directory) / "image.tar"
                    image_id = docker.export(tag, archive)
                    verify_archive_config(archive, tag, image_id)
                    command(["docker", "image", "rm", tag], timeout=30)
                    command(["docker", "load", "-i", str(archive)], timeout=600)
                    loaded = command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
                    if loaded != image_id:
                        raise RuntimeError("Loaded Docker archive has a different image ID")
            finally:
                if command(["docker", "images", "-q", tag], timeout=30):
                    command(["docker", "image", "rm", tag], timeout=30)
            result["checks"]["docker_archive_roundtrip"] = "passed"
            root = job / "php"
            (root / "memories_rkmpp_v11").mkdir(parents=True)
            (root / "scripts").mkdir()
            php = root / "memories_rkmpp_v11/syntax.php"
            php.write_text("<?php this is invalid PHP syntax\n")
            (root / "scripts/test-v11-compat.php").write_text("<?php file_put_contents('/src/compatibility-ran', 'yes');\n")
            try:
                docker.run(config["php_image"], root, ["sh", "-ec", PHP_LINT_CHECK], job / "php-invalid.log")
            except RuntimeError as error:
                if "Parse error" not in str(error) and "Errors parsing" not in str(error):
                    raise
                if (root / "compatibility-ran").exists():
                    raise RuntimeError("Invalid PHP did not prevent compatibility execution")
            else:
                raise RuntimeError("Invalid PHP was accepted")
            php.write_text("<?php echo 'valid';\n")
            docker.run(config["php_image"], root, ["sh", "-ec", PHP_LINT_CHECK], job / "php-valid.log")
            if not (root / "compatibility-ran").exists():
                raise RuntimeError("Valid PHP did not execute the compatibility check")
            result["checks"]["php_failure_propagation"] = "passed"
            fixture, upstream = job / "fixture", job / "upstream"
            create_fixture(fixture, upstream)
            try:
                docker.checks("fixture", fixture, job / "broken-fixture.log")
            except RuntimeError as error:
                if "FAIL: test_option_must_exist_in_both_binaries" not in str(error):
                    raise
            else:
                raise RuntimeError("Unmodified broken fixture passed its behavioral checks")
            result["checks"]["broken_source_rejected"] = "passed"
            started = time.monotonic()
            try:
                docker.run(config["python_image"], fixture, ["python", "-c", "import time; time.sleep(60)"],
                           job / "timeout.log", timeout=2)
            except RuntimeError as error:
                if "timed out" not in str(error):
                    raise
            else:
                raise RuntimeError("Sleeping test did not time out")
            remaining = command(["docker", "ps", "-aq", "--filter", "name=^/" + docker.last_check_container + "$"], timeout=30)
            if remaining:
                raise RuntimeError("Timed-out test container still exists")
            result["checks"]["daemon_timeout_cleanup"] = {"status":"passed", "elapsed_seconds":time.monotonic() - started}
            result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=str(error))
        (job / "error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(job / "result.json", result)
    return result
