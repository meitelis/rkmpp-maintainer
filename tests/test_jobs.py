import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import maintainer
from agent import AgentBlocked, run_agent


class JobTests(unittest.TestCase):
    def test_once_cli_fails_when_an_attempted_candidate_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.json'
            config.write_text('{}')
            with patch.object(sys, 'argv', ['maintainer', 'once', '--config', str(config), '--workspace', temp]), \
                 patch('maintainer.discover', return_value=[{'project':'memories', 'latest':'11.1.2', 'update':True}]), \
                 patch('maintainer.run_job', return_value={'status':'failed', 'job':'failure-evidence'}):
                with self.assertRaises(SystemExit) as stopped:
                    maintainer.main()
                self.assertEqual(stopped.exception.code, 1)

    def test_once_cli_succeeds_when_no_update_is_needed(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.json'
            config.write_text('{}')
            with patch.object(sys, 'argv', ['maintainer', 'once', '--config', str(config), '--workspace', temp]), \
                 patch('maintainer.discover', return_value=[{'project':'memories', 'latest':'11.1.2', 'update':False}]), \
                 patch('maintainer.run_job') as run:
                maintainer.main()
                run.assert_not_called()

    def test_false_finish_cannot_build_even_when_checks_would_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            def prepare(project, job, *args):
                root, upstream = job/'source', job/'upstream'
                root.mkdir(); upstream.mkdir()
                for base, name in [(root, 'go-vod/main.go'),
                                   (root, 'memories_rkmpp_v11/lib/Listener/PlayerScriptListener.php'),
                                   (upstream, 'lib/Service/BinExt.php')]:
                    path = base/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('review source\n')
                return root, upstream, [], []
            def false_finish(*args, **kwargs):
                return run_agent(*args, **kwargs, responder=lambda *_: json.dumps({
                    'action':'finish', 'summary':'Reporting pause pending investigation. I will continue next turn.',
                    'review_complete':True, 'blockers':[]}))
            with patch('maintainer.prepare', side_effect=prepare), \
                 patch('maintainer.run_agent', side_effect=false_finish), \
                 patch('maintainer.Docker') as cls, patch('maintainer.publish') as publish:
                cls.return_value.model.return_value.__enter__.return_value = 'http://local'
                result = maintainer.run_job('memories', {'repair_rounds':3, 'agent_turns':24},
                                            Path(temp), '11.1.2', publish_code=True)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('Incomplete review', result['error'])
                self.assertFalse(result['agent_review']['review_complete'])
                cls.return_value.checks.assert_not_called()
                cls.return_value.build.assert_not_called()
                cls.return_value.export.assert_not_called()
                publish.assert_not_called()

    def test_agent_blocker_never_reaches_checks_build_export_or_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            def prepare(project, job, *args):
                root, upstream = job/'source', job/'upstream'
                root.mkdir(); upstream.mkdir()
                return root, upstream, [], []
            with patch('maintainer.prepare', side_effect=prepare), \
                 patch('maintainer.run_agent', side_effect=AgentBlocked('Agent reported blocker: protected file')), \
                 patch('maintainer.Docker') as cls, patch('maintainer.publish') as publish:
                docker = cls.return_value
                docker.model.return_value.__enter__.return_value = 'http://local'
                result = maintainer.run_job('memories', {'repair_rounds': 3, 'agent_turns': 24},
                                            Path(temp), '11.1.2', publish_code=True)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('protected file', result['error'])
                docker.checks.assert_not_called()
                docker.build.assert_not_called()
                docker.export.assert_not_called()
                publish.assert_not_called()
                docker.model.return_value.__exit__.assert_called_once()
                self.assertTrue((Path(result['job'])/'error.txt').exists())

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
                 patch('maintainer.git', return_value='diff'), patch('maintainer.run_agent', return_value={
                     'summary':'done', 'review_complete':True, 'blockers':[], 'assessment':'model-self-report'}), \
                 patch('maintainer.Docker') as cls:
                docker = cls.return_value
                docker.model.side_effect = model
                docker.checks.side_effect = lambda *args: events.append('checks')
                docker.build.side_effect = lambda *args: events.append('build')
                docker.export.return_value = 'sha256:fixture'
                result = maintainer.run_job('immich', {'repair_rounds': 2, 'agent_turns': 4}, Path(temp), '3.4.1')
                self.assertEqual(result['status'], 'built')
                self.assertEqual(result['review_status'], 'pending-human-review')
                self.assertFalse(result['deployment_approved'])
                self.assertTrue((Path(result['job'])/'agent-review.json').exists())
                self.assertEqual(events, ['model-loaded','model-unloaded','checks','build'])

    def test_historical_build_without_agent_is_not_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            def prepare(project, job, *args):
                root, upstream = job/'source', job/'upstream'
                (root/'memories_rkmpp_v11').mkdir(parents=True); upstream.mkdir()
                return root, upstream, [], []
            with patch('maintainer.prepare', side_effect=prepare), patch('maintainer.validate'), \
                 patch('maintainer.git', return_value='patch'), patch('maintainer.Docker') as cls:
                cls.return_value.export.return_value = 'sha256:fixture'
                result = maintainer.run_job('memories', {}, Path(temp), '11.1.2',
                    base_ref='old-commit', source_version='11.1.1', no_agent=True)
                self.assertEqual(result['status'], 'built')
                self.assertEqual(result['kind'], 'historical-replay')
                self.assertEqual(result['review_status'], 'pending-human-review')
                self.assertFalse(result['deployment_approved'])
                self.assertFalse(result['agent_review']['review_complete'])
                self.assertEqual(result['agent_review']['assessment'], 'skipped')

    def test_run_cli_accepts_built_as_a_successful_build(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.json'
            config.write_text('{}')
            with patch.object(sys, 'argv', ['maintainer', 'run', '--repo', 'memories', '--target', '11.1.2',
                    '--config', str(config), '--workspace', str(Path(temp)/'workspace')]), \
                 patch('maintainer.run_job', return_value={'status':'built'}):
                maintainer.main()  # Returns normally, so detached wrappers record exit code zero.
