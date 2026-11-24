"""Each app serves the manifest bytes recorded in served-manifest-digests.json.

Installations pin an app by its manifest digest. The record was taken from aweb
before the apps moved; a change here means every installation needs a new pin.
"""
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = json.loads((ROOT / 'scripts/served-manifest-digests.json').read_text())


def served_digests(app):
    environment = {name: os.environ[name] for name in ('PATH', 'HOME', 'UV_CACHE_DIR') if name in os.environ}
    result = subprocess.run(
        ['uv', 'run', '--frozen', '--project', f'naapp/{app}', 'python', 'scripts/served_manifest_digests.py', app],
        cwd=ROOT, env=environment, check=True, capture_output=True, text=True, timeout=600,
    )
    return json.loads(result.stdout)


class ServedManifests(unittest.TestCase):
    def test_every_app_serves_its_recorded_manifest_bytes(self):
        for app, recorded in RECORD['apps'].items():
            with self.subTest(app=app):
                self.assertEqual(served_digests(app), recorded)


if __name__ == '__main__':
    unittest.main()
