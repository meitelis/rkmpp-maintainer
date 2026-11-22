"""End-to-end delivery gate tests using real Docker-save-style archives.

No Docker daemon is needed: the content-addressed image configuration,
archive checksum, and hardware-qualified ID must agree independently.
"""
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from delivery import verify_release_image


class DeliveryGateTests(unittest.TestCase):
    def prepare(self, directory, *, project="immich", config_match=True,
                checksum_match=True, helper=True, blob=False, corrupt_config=False,
                corrupt_layer=False, missing_layer=False):
        job=Path(directory)
        with io.BytesIO() as output:
            with tarfile.open(fileobj=output,mode="w") as layer:
                blob=b"real Docker layer fixture"
                entry=tarfile.TarInfo("proof.txt")
                entry.size=len(blob)
                layer.addfile(entry,io.BytesIO(blob))
            layer_bytes=output.getvalue()
        layer_digest=hashlib.sha256(layer_bytes).hexdigest()
        content=json.dumps({"architecture":"arm64","os":"linux",
            "rootfs":{"type":"layers","diff_ids":["sha256:"+layer_digest]}}).encode()
        digest=hashlib.sha256(content).hexdigest()
        config_path = "blobs/sha256/" + digest if blob else digest + ".json"
        expected_id="sha256:" + (digest if config_match else "0"*64)
        image=f"local/{project}-rkmpp:candidate-11.1.5-aabbccdd"
        with tarfile.open(job/"image.tar","w") as output:
            members={
                config_path:content + (b"x" if corrupt_config else b""),
                "manifest.json":json.dumps([{"Config":config_path,
                    "RepoTags":[image],"Layers":["layer/layer.tar"]}]).encode(),
            }
            if not missing_layer:
                members["layer/layer.tar"] = layer_bytes + (b"modified" if corrupt_layer else b"")
            for name, blob in members.items():
                header=tarfile.TarInfo(name)
                header.size=len(blob)
                output.addfile(header,io.BytesIO(blob))
        actual=hashlib.sha256((job/"image.tar").read_bytes()).hexdigest()
        checksum=(actual if checksum_match else "f"*64)
        (job/"image.tar.sha256").write_text(checksum+"  image.tar\n")
        if project=="memories" and helper:
            with tarfile.open(job/"memories-helper.tar.gz","w:gz") as archive:
                blob=b"helper"
                entry=tarfile.TarInfo("memories_rkmpp_v11/README")
                entry.size=len(blob)
                archive.addfile(entry,io.BytesIO(blob))
        helper_hash = (hashlib.sha256((job/"memories-helper.tar.gz").read_bytes()).hexdigest()
                       if project == "memories" and helper else None)
        result={"kind":"release-candidate","project":project,
                "agent_review":{"review_complete":True},
                "candidate_verified":True,"ready_to_deploy":False,
                "artifact_status":"validated-awaiting-owner-review",
                "image_id":expected_id,"image":image,
                "helper_sha256":helper_hash,
                "hardware_qualification":{"ok":True,"image_id":expected_id,"arch":"arm64"}}
        return result

    def test_verified_image_is_a_real_deployable_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory)
            delivery=verify_release_image(directory,result)
            self.assertEqual(delivery["image_id"],result["image_id"])
            self.assertTrue(delivery["bytes"]>1024)
            self.assertEqual(delivery["sha256"],hashlib.sha256(
                (Path(directory)/"image.tar").read_bytes()).hexdigest())

    def test_corrupt_layer_rejected_even_if_archive_checksum_is_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,corrupt_layer=True)
            with self.assertRaisesRegex(RuntimeError,"layer checksum mismatch"):
                verify_release_image(directory,result)

    def test_missing_layer_rejected_without_importing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,missing_layer=True)
            with self.assertRaisesRegex(RuntimeError,"Docker export is invalid"):
                verify_release_image(directory,result)

    def test_matching_memories_helper_hash_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,project="memories")
            self.assertEqual(verify_release_image(directory,result)["helper_sha256"],
                             result["helper_sha256"])
            with (Path(directory)/"memories-helper.tar.gz").open("ab") as target:
                target.write(b"tampered")
            with self.assertRaisesRegex(RuntimeError,"helper checksum changed"):
                verify_release_image(directory,result)

    def test_candidate_cannot_claim_owner_approval_before_live_review(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory)
            result["ready_to_deploy"]=True
            with self.assertRaisesRegex(RuntimeError,"owner deployment acceptance"):
                verify_release_image(directory,result)

    def test_blob_layout_validates_config_content(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.prepare(directory, blob=True)
            self.assertEqual(verify_release_image(directory, result)["image_id"], result["image_id"])
            result = self.prepare(directory, blob=True, corrupt_config=True)
            with self.assertRaisesRegex(RuntimeError, "config does not match"):
                verify_release_image(directory, result)

    def test_corrupt_archive_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,checksum_match=False)
            with self.assertRaisesRegex(RuntimeError,"checksum mismatch"):
                verify_release_image(directory,result)

    def test_wrong_image_config_fails_even_with_correct_checksum_and_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,config_match=False)
            with self.assertRaisesRegex(RuntimeError,"config does not match"):
                verify_release_image(directory,result)

    def test_unqualified_hardware_and_unreviewed_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory)
            result["hardware_qualification"]["ok"]=False
            with self.assertRaisesRegex(RuntimeError,"hardware qualification"):
                verify_release_image(directory,result)
            result["hardware_qualification"]["ok"]=True
            result["agent_review"]["review_complete"]=False
            with self.assertRaisesRegex(RuntimeError,"source review"):
                verify_release_image(directory,result)

    def test_historical_replay_cannot_claim_release_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory)
            result["kind"]="historical-replay"
            with self.assertRaisesRegex(RuntimeError,"Historical"):
                verify_release_image(directory,result)

    def test_memories_requires_matching_helper_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.prepare(directory,project="memories",helper=False)
            with self.assertRaisesRegex(RuntimeError,"Nextcloud helper"):
                verify_release_image(directory,result)
