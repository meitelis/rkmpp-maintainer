import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import maintainer


class JobTests(unittest.TestCase):
    def test_failed_checks_never_export_or_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            def prepare(project, job, *args):
                root, upstream = job/'source', job/'upstream'
                root.mkdir(); upstream.mkdir()
                return root, upstream, [], []
            with patch('maintainer.prepare', side_effect=prepare), patch('maintainer.validate'), \
                 patch('maintainer.Docker') as cls, patch('maintainer.publish') as publish:
                docker = cls.return_value
                docker.checks.side_effect = RuntimeError('regression failed')
                result = maintainer.run_job('immich', {}, workspace, '3.4.1', publish_code=True, no_agent=True)
                self.assertEqual(result['status'], 'failed')
                docker.build.assert_not_called()
                docker.export.assert_not_called()
                publish.assert_not_called()
                self.assertTrue((Path(result['job'])/'result.json').exists())

    def test_model_is_unloaded_before_checks_and_build(self):
        with tempfile.TemporaryDirectory() as temp:
            events = []
            def prepare(project, job, *args):
                root, upstream = job/'source', job/'upstream'
                root.mkdir(); upstream.mkdir()
                return root, upstream, [], []
            @contextlib.contextmanager
            def model():
                events.append('model-loaded')
                try: yield 'http://local'
                finally: events.append('model-unloaded')
            with patch('maintainer.prepare', side_effect=prepare), patch('maintainer.validate'), \
                 patch('maintainer.git', return_value='diff'), patch('maintainer.run_agent', return_value='done'), \
                 patch('maintainer.Docker') as cls:
                docker = cls.return_value
                docker.model.side_effect = model
                docker.checks.side_effect = lambda *args: events.append('checks')
                docker.build.side_effect = lambda *args: events.append('build')
                docker.export.return_value = 'sha256:fixture'
                result = maintainer.run_job('immich', {'repair_rounds': 2, 'agent_turns': 4}, Path(temp), '3.4.1')
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(events, ['model-loaded','model-unloaded','checks','build'])
