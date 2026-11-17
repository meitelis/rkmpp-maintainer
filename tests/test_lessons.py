"""Verified lessons are only learned from successful RK3588 releases."""
import tempfile
import unittest
from pathlib import Path

from lessons import record_verified_repair, verified_lessons


def qualified():
    return {"kind":"release-candidate","status":"built","ready_to_deploy":True,
        "delivery":{"sha256":"verified"},"image_id":"sha256:"+"a"*64,
        "hardware_qualification":{"ok":True},
        "repair_failures":[{"attempt":1,"error":"MPP invalid surface during HEVC seek"}]}


class LessonTests(unittest.TestCase):
    def test_only_verified_success_can_be_remembered(self):
        with tempfile.TemporaryDirectory() as root:
            result=qualified()
            for change in (
                {"status":"failed"},{"ready_to_deploy":False},
                {"hardware_qualification":{"ok":False}},{"delivery":None},
                {"kind":"historical-replay"}):
                attempt={**result,**change}
                self.assertFalse(record_verified_repair(
                    root,"memories","11.1.5",attempt,["go-vod/ffmpeg/args.go"]))
            self.assertEqual(verified_lessons(root,"memories"),"")

    def test_successful_repair_is_reusable_as_bounded_context(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(record_verified_repair(root,"immich","3.4.2",
                qualified(),["overlays/server/src/video.ts"]))
            text=verified_lessons(root,"immich")
            self.assertIn("3.4.2",text)
            self.assertIn("MPP invalid surface",text)
            self.assertIn("overlays/server/src/video.ts",text)
            self.assertNotIn("3.4.2",verified_lessons(root,"memories"))

    def test_unrepaired_success_does_not_add_useless_history(self):
        with tempfile.TemporaryDirectory() as root:
            success={**qualified(),"repair_failures":[]}
            self.assertFalse(record_verified_repair(root,"immich","3.4.2",success,[]))
            self.assertFalse((Path(root)/"lessons").exists())

    def test_history_is_bounded_and_replaces_same_version(self):
        with tempfile.TemporaryDirectory() as root:
            for patch in range(9):
                result=qualified()
                result["image_id"]="sha256:"+f"{patch+1:064x}"
                record_verified_repair(root,"memories",f"11.1.{patch}",
                    result,[f"go-vod/{patch}.go"])
            self.assertNotIn("11.1.0",verified_lessons(root,"memories"))
            self.assertIn("11.1.8",verified_lessons(root,"memories"))
            result=qualified()
            record_verified_repair(root,"memories","11.1.8",result,["revised.go"])
            self.assertIn("revised.go",verified_lessons(root,"memories"))


if __name__=="__main__":
    unittest.main()
