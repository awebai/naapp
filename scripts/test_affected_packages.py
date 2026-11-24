"""Which packages a change selects for testing, on this repository's own layout."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from affected_packages import PACKAGES, affected

SCRIPT = Path(__file__).resolve().with_name('affected_packages.py')
EVERY = set(PACKAGES)


class Affected(unittest.TestCase):
    def test_a_change_inside_one_app_selects_that_app(self):
        self.assertEqual(affected(['naapp/folio/src/folio/api.py']), {'naapp/folio'})
        self.assertEqual(affected(['naapp/library/Makefile']), {'naapp/library'})

    def test_a_runtime_change_selects_every_package_that_uses_the_runtime(self):
        # The values codec depends on nothing, the runtime included.
        users = EVERY - {'naapp/secrets/values'}
        self.assertEqual(affected(['naapp-lib/src/aweb_naapp/runtime/app.py']), users)
        self.assertEqual(affected(['naapp-lib/testing/src/aweb_naapp_testing/network.py']), users)

    def test_a_change_selects_the_packages_that_depend_on_it(self):
        # Secrets tests with Logs installed from this repository.
        self.assertEqual(affected(['naapp/logs/src/aweb_logs/app.py']), {'naapp/logs', 'naapp/secrets'})
        # The values codec is shared by Secrets and its runner.
        self.assertEqual(
            affected(['naapp/secrets/values/src/aweb_secrets_values/codec.py']),
            {'naapp/secrets/values', 'naapp/secrets', 'naapp/secrets/runner'},
        )

    def test_a_nested_package_is_its_own_package(self):
        self.assertEqual(affected(['naapp/secrets/runner/src/x.py']), {'naapp/secrets/runner'})
        self.assertEqual(affected(['naapp/secrets/e2e/team_journey_support.py']), {'naapp/secrets'})

    def test_a_change_inside_no_package_selects_every_package(self):
        for path in ('README.md', 'scripts/test-egress/native.py', 'docs/vectors/owner-act-v1.json',
                     '.github/workflows/ci.yml', 'naapp/README.md'):
            with self.subTest(path=path):
                self.assertEqual(affected([path]), EVERY)

    def test_no_change_selects_no_package(self):
        self.assertEqual(affected([]), set())

    def test_the_command_reads_paths_and_prints_the_selection(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input='naapp/folio/x.py\nnaapp/library/y.py\n',
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), ['naapp/folio', 'naapp/library'])

    def test_every_package_is_a_project_of_this_repository(self):
        root = SCRIPT.parents[1]
        for package in PACKAGES:
            with self.subTest(package=package):
                self.assertTrue((root / package / 'pyproject.toml').is_file())


if __name__ == '__main__':
    unittest.main()
