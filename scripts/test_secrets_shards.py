"""Controls for the Secrets collection and isolated child boundaries."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from secrets_shards import junit_environment, load_shards, run_shards, service_environments, validate_collections


class ShardCheck(unittest.TestCase):
    def test_the_shard_check_reports_deleted_and_unassigned_secrets_modules(self):
        scripts = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copyfile(scripts / 'secrets_shards.py', root / 'scripts/secrets_shards.py')
            (root / 'candidate-gate').mkdir()
            manifest = root / 'candidate-gate/secrets-shards.json'
            manifest.write_text(json.dumps({
                'version': 1,
                'shards': [['tests/test_a.py', 'tests/test_e2e.py'], ['tests/test_removed.py']],
            }))
            tests = root / 'package/tests'
            tests.mkdir(parents=True)
            for name in ('test_a.py', 'test_removed.py'):
                (tests / name).write_text('def test_case():\n    raise AssertionError("collection only")\n')
            (tests / 'test_e2e.py').write_text(
                'import pytest\n@pytest.mark.e2e\ndef test_case():\n    assert False\n'
            )
            (tests.parent / 'pytest.ini').write_text('[pytest]\nmarkers = e2e\n')
            (root / 'Makefile').write_text(
                'check-secrets-shards:\n'
                f'\t@cd package && {shlex.quote(sys.executable)} ../scripts/secrets_shards.py'
                f' --check -- {shlex.quote(sys.executable)} -m pytest -q -m "not e2e"\n'
                '.DEFAULT:\n\t@:\n'
            )
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
            env.pop('PYTEST_ADDOPTS', None)
            env.pop('AWEB_SECRETS_SHARD_SERVICES', None)
            for stale in (True, False):
                with self.subTest(stale=stale):
                    if stale:
                        (tests / 'test_removed.py').rename(tests / 'test_added.py')
                    else:
                        (tests / 'test_added.py').rename(tests / 'test_removed.py')
                    result = subprocess.run(
                        ['make', 'check-secrets-shards'],
                        cwd=root, env=env, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode == 0, not stale, result.stdout + result.stderr)
                    if stale:
                        output = result.stdout + result.stderr
                        self.assertIn('module tests/test_removed.py in secrets-shards.json was not collected', output)
                        self.assertIn('collected module tests/test_added.py is missing from secrets-shards.json', output)


class Partition(unittest.TestCase):
    def test_exact_union_preserves_node_order_and_parameter_separators(self):
        full = ['tests/a.py::test_x[::1]', 'tests/a.py::test_y', 'tests/b.py::test_z']
        self.assertEqual(validate_collections(full, [full[:2], full[2:]]), [2, 1])
        self.assertEqual(validate_collections(full, [full[:1], full[1:2], full[2:]]), [1, 1, 1])
        for shards in ([full[:1], full[2:]], [full[:2], full],
                       [full[:2], [*full[2:], 'tests/new.py::test_new']],
                       [list(reversed(full[:2])), full[2:]], [full[:2] * 2, full[2:]]):
            with self.subTest(shards=shards), self.assertRaises(ValueError):
                validate_collections(full, shards)
        with self.assertRaises(ValueError):
            validate_collections([], [[], []])

    def test_manifest_refuses_overlap_and_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            for name in ('a', 'b', 'c'):
                (root / f'tests/{name}.py').touch()
            manifest = root / 'shards.json'
            valid = ([['tests/a.py'], ['tests/b.py']],
                     [['tests/a.py'], ['tests/b.py'], ['tests/c.py']])
            for shards in (*valid, [['tests/a.py'], ['tests/a.py']],
                           [['tests/a.py'], ['../outside.py']], [['tests/a.py']]):
                manifest.write_text(json.dumps({'version': 1, 'shards': shards}))
                with self.subTest(shards=shards):
                    if shards in valid:
                        self.assertEqual(load_shards(root, manifest), shards)
                    else:
                        with self.assertRaises(ValueError):
                            load_shards(root, manifest)

    def test_mutating_a_shard_service_to_a_shared_port_refuses(self):
        pairs = [dict(postgres_port=5432, redis_port=6379), dict(postgres_port=5432, redis_port=6380)]
        with self.assertRaisesRegex(ValueError, 'must be distinct'):
            service_environments(json.dumps(pairs), 2)

    def test_each_shard_needs_its_own_service_pair(self):
        pairs = [dict(postgres_port=5432, redis_port=6379), dict(postgres_port=5433, redis_port=6380)]
        with self.assertRaisesRegex(ValueError, 'one Postgres and Redis pair per shard'):
            service_environments(json.dumps(pairs), 3)

    def test_children_overlap_with_distinct_services_and_keep_failed_exit(self):
        pairs = [dict(postgres_port=5432, redis_port=6379), dict(postgres_port=5433, redis_port=6380),
                 dict(postgres_port=5434, redis_port=6381)]
        environments = service_environments(json.dumps(pairs), 3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            witness = root / 'child.py'
            witness.write_text('''
import os
from pathlib import Path
import sys
import time
port = os.environ['TEST_DB_PORT']
peers = {'5432', '5433', '5434'} - {port}
Path(port).touch()
deadline = time.monotonic() + 5
while not all(Path(peer).exists() for peer in peers):
    if time.monotonic() > deadline:
        raise RuntimeError('Another shard did not start')
    time.sleep(.01)
print(port, os.environ['REDIS_URL'], *sys.argv[1:])
raise SystemExit(23 if port == '5432' else 0)
''')
            evidence = root / 'evidence'
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = run_shards(root, [['tests/a.py'], ['tests/b.py'], ['tests/c.py']], environments,
                                    evidence, [sys.executable, str(witness)])
            self.assertEqual(status, 23, stdout.getvalue())
            shard_inputs = [(5432, 6379, 'tests/a.py'), (5433, 6380, 'tests/b.py'),
                            (5434, 6381, 'tests/c.py')]
            for index, (port, redis, module) in enumerate(shard_inputs, 1):
                log = (evidence / f'run-{index}' / 'pytest.log').read_text()
                self.assertIn(f'{port} redis://127.0.0.1:{redis}/0', log)
                self.assertIn(f'run-{index}/pytest', log)
                self.assertIn(f'run-{index}/cache', log)
                self.assertIn(module, log)
            self.assertIn('Secrets shard 1 exit 23', stdout.getvalue())
            self.assertIn('Secrets shard 2 exit 0', stdout.getvalue())
            self.assertIn('Secrets shard 3 exit 0', stdout.getvalue())


class JUnitReports(unittest.TestCase):
    def test_each_shard_writes_its_own_report_and_collection_writes_none(self):
        env = {'PYTEST_ADDOPTS': '-rs --junitxml=/reports/naapp_secrets.xml', 'OTHER': 'kept'}
        self.assertEqual(junit_environment(env, 'shard-1')['PYTEST_ADDOPTS'],
                         '-rs --junitxml=/reports/naapp_secrets-shard-1.xml')
        self.assertEqual(junit_environment(env, 'shard-2')['PYTEST_ADDOPTS'],
                         '-rs --junitxml=/reports/naapp_secrets-shard-2.xml')
        self.assertEqual(junit_environment(env, None)['PYTEST_ADDOPTS'], '-rs')
        self.assertEqual(junit_environment(env, None)['OTHER'], 'kept')

    def test_an_environment_without_a_report_is_unchanged(self):
        env = {'PYTEST_ADDOPTS': '-rs'}
        self.assertEqual(junit_environment(env, 'shard-1'), env)
        self.assertEqual(junit_environment({}, 'shard-1'), {})


if __name__ == '__main__':
    unittest.main()
