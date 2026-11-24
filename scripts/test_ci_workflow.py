"""The CI workflow and its required check agree on every job and package."""
import unittest
from pathlib import Path

import yaml
from affected_packages import PACKAGES
from ci_result import ALWAYS, JOBS

WORKFLOWS = Path(__file__).resolve().parents[1] / '.github/workflows'


def workflow():
    return yaml.load((WORKFLOWS / 'ci.yml').read_text(), Loader=yaml.BaseLoader)


class Workflow(unittest.TestCase):
    def test_the_workflow_runs_exactly_the_jobs_the_check_knows(self):
        self.assertEqual(set(workflow()['jobs']), {*ALWAYS, *JOBS, 'result'})

    def test_the_check_waits_for_every_job_and_always_runs(self):
        result = workflow()['jobs']['result']
        self.assertEqual(set(result['needs']), {*ALWAYS, *JOBS})
        self.assertEqual(result['if'], 'always()')
        self.assertIn('python3 scripts/ci_result.py', [step.get('run') for step in result['steps']])

    def test_each_package_job_runs_for_its_own_package_only(self):
        jobs = workflow()['jobs']
        for name, package in JOBS.items():
            with self.subTest(job=name):
                job = jobs[name]
                self.assertEqual(job['needs'], 'plan')
                self.assertEqual(job['if'], f"contains(fromJSON(needs.plan.outputs.packages), '{package}')")
                self.assertEqual(job['env']['PACKAGES'], package)

    def test_every_package_has_a_job(self):
        self.assertEqual(set(JOBS.values()), set(PACKAGES))

    def test_naapp_lib_shards_are_a_path_and_everything_else(self):
        job = workflow()['jobs']['naapp-lib']
        shards = [entry['tests'] for entry in job['strategy']['matrix']['include']]
        self.assertEqual(len(shards), 2)
        path = next(shard for shard in shards if not shard.startswith('--'))
        self.assertEqual(sorted(shards), sorted([path, f'--ignore={path}']))
        self.assertEqual(job['env']['NAAPP_LIB_TESTS'], '${{ matrix.tests }}')

    def test_every_test_job_keeps_its_per_test_results(self):
        for name, job in workflow()['jobs'].items():
            commands = [line.split() for step in job['steps'] for line in step.get('run', '').splitlines()]
            if not any(words[:2] == ['make', 'test'] or words[1:2] and words[0] == 'make'
                       and words[1].startswith('e2e-') for words in commands):
                continue
            with self.subTest(job=name):
                env = job.get('env', {})
                self.assertTrue('JUNIT_DIR' in env or '--junitxml=' in env.get('PYTEST_ADDOPTS', ''))
                uploads = [step for step in job['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
                self.assertEqual(len(uploads), 1)
                self.assertEqual(uploads[0]['if'], 'always()')
                self.assertEqual(uploads[0]['with']['path'], 'junit/')

    def test_no_trigger_filters_by_path(self):
        for path in sorted(WORKFLOWS.glob('*.yml')):
            triggers = yaml.load(path.read_text(), Loader=yaml.BaseLoader)['on']
            for event, settings in (triggers.items() if isinstance(triggers, dict) else ()):
                with self.subTest(workflow=path.name, event=event):
                    self.assertFalse({'paths', 'paths-ignore'} & set(settings or {}))

    def test_every_job_runs_make_targets_at_the_repository_root(self):
        for name, job in workflow()['jobs'].items():
            for step in job['steps']:
                for line in step.get('run', '').splitlines():
                    words = line.split()
                    if words[:1] == ['make']:
                        with self.subTest(job=name, line=line):
                            self.assertNotIn('-C', words)
                            self.assertNotIn('working-directory', step)


if __name__ == '__main__':
    unittest.main()
