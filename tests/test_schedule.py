import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintainer
from schedule import admission, host_idle, window

UTC = datetime.timezone.utc
RULE = {"enabled":True, "timezone":"Europe/Dublin", "start_hour":0, "end_hour":8, "idle_only":True}


class ScheduleTests(unittest.TestCase):
    def test_idle_cpu_does_not_override_the_larger_models_ram_reserve(self):
        config={"work_window":RULE,"model_runtime":{"memory":"18g"}}
        with patch("schedule.cpu_sample",side_effect=[(100,95),(200,185)]), \
             patch("schedule.time.sleep"), patch("schedule.os.getloadavg",return_value=(0.5,0,0),create=True), \
             patch("schedule.os.cpu_count",return_value=8), \
             patch("schedule.Path.read_text",return_value="MemAvailable: 19000000 kB\n"):
            result=host_idle(config)
        self.assertFalse(result["allowed"])
        self.assertGreater(result["required_memory_bytes"],result["available_memory_bytes"])

    def test_midnight_is_included_and_eight_am_is_excluded(self):
        config = {"work_window":RULE}
        self.assertTrue(window(config, datetime.datetime(2026,11,12,0,0,tzinfo=UTC))["allowed"])
        self.assertTrue(window(config, datetime.datetime(2026,11,12,7,59,tzinfo=UTC))["allowed"])
        self.assertFalse(window(config, datetime.datetime(2026,11,12,8,0,tzinfo=UTC))["allowed"])
        self.assertFalse(window(config, datetime.datetime(2026,11,12,23,59,tzinfo=UTC))["allowed"])

    def test_dublin_summer_time_is_used_instead_of_fixed_utc(self):
        config = {"work_window":RULE}
        self.assertTrue(window(config, datetime.datetime(2026,7,12,23,0,tzinfo=UTC))["allowed"])
        self.assertFalse(window(config, datetime.datetime(2026,7,12,7,0,tzinfo=UTC))["allowed"])

    def test_next_start_crosses_dst_using_local_midnight(self):
        result = window({"work_window":RULE}, datetime.datetime(2026,10,24,12,tzinfo=UTC))
        self.assertEqual(result["next_start"], "2026-10-25T00:00:00+01:00")

    def test_busy_host_defers_even_during_night_window(self):
        with patch("schedule.host_idle", return_value={"allowed":False,"reason":"busy"}) as idle:
            result = admission({"work_window":RULE}, now=datetime.datetime(2026,11,12,2,tzinfo=UTC))
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "busy")
        idle.assert_called_once()

    def test_daytime_does_not_sample_or_start_heavy_work(self):
        with patch("schedule.host_idle") as idle:
            result = admission({"work_window":RULE}, now=datetime.datetime(2026,11,12,12,tzinfo=UTC))
        self.assertFalse(result["allowed"])
        idle.assert_not_called()

    def test_one_job_may_finish_but_next_job_is_deferred_after_window_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"config.json"
            path.write_text('{}')
            records = [{"project":"memories","latest":"11.1.2","update":True},
                       {"project":"immich","latest":"3.4.1","update":True}]
            with patch.object(sys,"argv",["maintainer","once","--config",str(path),"--workspace",temp]), \
                 patch("maintainer.discover",return_value=records), \
                 patch("maintainer.admission",side_effect=[{"allowed":True},{"allowed":False,"reason":"08:00","retry_seconds":300}]), \
                 patch("maintainer.run_job",return_value={"status":"built","job":"first-job"}) as run:
                with self.assertRaises(SystemExit) as stopped:
                    maintainer.main()
            self.assertEqual(stopped.exception.code,2)
            self.assertEqual(run.call_count,1)
            state=json.loads((Path(temp)/"state.json").read_text())
            self.assertIn("memories:11.1.2",state)
            self.assertNotIn("immich:3.4.1",state)
