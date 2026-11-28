#!/usr/bin/env python3
"""The owner-act delivery generator writes the checked-in public vector byte for byte."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate_owner_act_delivery_vector.py"
VECTOR = ROOT / "docs/vectors/owner-act-delivery-v1.json"


class GenerateOwnerActDeliveryVector(unittest.TestCase):
    def test_the_generator_reproduces_the_checked_in_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "owner-act-delivery-v1.json"
            subprocess.run([sys.executable, str(SCRIPT), "--output", str(output)], cwd=ROOT, check=True)
            self.assertEqual(output.read_bytes(), VECTOR.read_bytes())


if __name__ == "__main__":
    unittest.main()
