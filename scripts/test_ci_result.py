"""The single required check passes only when every selected job succeeded."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from ci_result import ALWAYS, JOBS, problems

SCRIPT = Path(__file__).resolve().with_name('ci_result.py')


def needs(**results):
    """Every job succeeded or skipped as its package is selected, then the overrides."""
    return {job: {'result': results.get(job.replace('-', '_'), 'success')} for job in [*ALWAYS, *JOBS]}


class Result(unittest.TestCase):
    def test_every_selected_job_succeeding_passes(self):
        selected = ['naapp/folio']
        jobs = needs(**{job.replace('-', '_'): 'skipped' for job, package in JOBS.items() if package not in selected})
        self.assertEqual(problems(jobs, selected), [])

    def test_a_failed_or_cancelled_job_fails(self):
        for result in ('failure', 'cancelled'):
            with self.subTest(result=result):
                self.assertTrue(problems(needs(logs=result), sorted(set(JOBS.values()))))

    def test_a_selected_package_whose_job_was_skipped_fails(self):
        self.assertTrue(problems(needs(folio_e2e='skipped'), ['naapp/folio']))

    def test_a_job_that_always_runs_must_succeed(self):
        for job in ALWAYS:
            with self.subTest(job=job):
                self.assertTrue(problems(needs(**{job.replace('-', '_'): 'skipped'}), []))

    def test_an_unknown_or_missing_job_fails(self):
        jobs = needs()
        jobs['new-job'] = {'result': 'success'}
        self.assertTrue(problems(jobs, []))
        jobs = needs()
        del jobs['logs']
        self.assertTrue(problems(jobs, []))

    def test_every_package_has_a_job_and_every_job_a_package(self):
        from affected_packages import PACKAGES
        self.assertEqual(set(JOBS.values()), set(PACKAGES))

    def test_the_command_reads_the_environment_and_exits_by_the_outcome(self):
        selected = json.dumps(['naapp/logs'])
        for result, status in (('success', 0), ('failure', 1)):
            with self.subTest(result=result):
                jobs = needs(**{job.replace('-', '_'): 'skipped' for job, package in JOBS.items() if package != 'naapp/logs'})
                jobs['logs']['result'] = result
                outcome = subprocess.run(
                    [sys.executable, str(SCRIPT)], capture_output=True, text=True,
                    env=dict(os.environ, CI_NEEDS=json.dumps(jobs), CI_PACKAGES=selected),
                )
                self.assertEqual(outcome.returncode, status, outcome.stdout + outcome.stderr)


if __name__ == '__main__':
    unittest.main()
