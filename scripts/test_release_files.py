#!/usr/bin/env python3
"""The PyPI release files: built twice and identical, locked by sha256, uploaded unchanged."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from fixture_repo import init_fixture_repo, run_fixture

import release_files as FILES
from release_versions import PYPI_LANES, VersionRefusal

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def directory(test: unittest.TestCase, files: dict[str, bytes]) -> Path:
    path = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, path)
    for name, data in files.items():
        (path / name).write_bytes(data)
    return path


def lane(package: str, version: str) -> dict[str, bytes]:
    name = package.replace("-", "_")
    return {f"{name}-{version}-py3-none-any.whl": f"{name} wheel".encode(),
            f"{name}-{version}.tar.gz": f"{name} sdist".encode()}


class LaneFilesTest(unittest.TestCase):
    def test_a_lane_is_one_wheel_and_one_sdist_of_each_of_its_packages(self):
        files = {**lane("aweb-naapp", "0.6.0"), **lane("aweb-naapp-testing", "0.6.0"),
                 **lane("aweb-secrets-values", "0.1.0")}
        self.assertEqual(FILES.lane_files(directory(self, files), "aweb-naapp-v", "0.6.0"), [
            "aweb_naapp-0.6.0-py3-none-any.whl", "aweb_naapp-0.6.0.tar.gz",
            "aweb_naapp_testing-0.6.0-py3-none-any.whl", "aweb_naapp_testing-0.6.0.tar.gz",
        ])

    def test_a_lane_missing_a_file_or_at_another_version_refuses(self):
        cases = {
            "no testing package": lane("aweb-naapp", "0.6.0"),
            "another version": {**lane("aweb-naapp", "0.6.0"), **lane("aweb-naapp-testing", "0.5.9")},
            "two wheels": {**lane("aweb-naapp", "0.6.0"), **lane("aweb-naapp-testing", "0.6.0"),
                           "aweb_naapp-0.6.0-py3-none-linux_x86_64.whl": b"other"},
        }
        for reason, files in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(VersionRefusal, "aweb_naapp"):
                FILES.lane_files(directory(self, files), "aweb-naapp-v", "0.6.0")


class SumsTest(unittest.TestCase):
    def test_the_sums_name_every_file_with_its_digest(self):
        files = lane("aweb-naapp-journey-support", "0.1.0")
        path = directory(self, files)
        FILES.write_sums(path)
        self.assertEqual(FILES.read_sums(path), {name: digest(data) for name, data in files.items()})
        self.assertEqual((path / "SHA256SUMS").read_text(), FILES.tag_lines(FILES.read_sums(path)))

    def test_sums_that_do_not_match_the_files_refuse(self):
        files = lane("aweb-naapp-journey-support", "0.1.0")
        changed, extra, missing = (directory(self, files) for _ in range(3))
        for path in (changed, extra, missing):
            FILES.write_sums(path)
        (changed / "aweb_naapp_journey_support-0.1.0.tar.gz").write_bytes(b"rebuilt")
        (extra / "unlisted.whl").write_bytes(b"extra")
        (missing / "aweb_naapp_journey_support-0.1.0.tar.gz").unlink()
        for path, reason in ((changed, "has sha256"), (extra, "unlisted.whl"), (missing, "missing")):
            with self.subTest(reason=reason), self.assertRaisesRegex(VersionRefusal, reason):
                FILES.read_sums(path)


class BuildTest(unittest.TestCase):
    PROJECTS = {"aweb-naapp-journey-support": ("naapp/secrets/e2e", "0.1.0")}

    def fake_builder(self, outputs: list[dict[str, bytes]]):
        builds = []

        def build(_root: Path, project: str, out: Path) -> None:
            builds.append((project, out))
            for name, data in outputs[len(builds) - 1].items():
                (out / name).write_bytes(data)
        return builds, build

    def test_each_package_is_built_twice_and_only_its_distributions_are_kept(self):
        files = lane("aweb-naapp-journey-support", "0.1.0")
        builds, builder = self.fake_builder([{**files, ".gitignore": b"*"}, {**files, ".gitignore": b"*"}])
        out = directory(self, {})
        FILES.build(ROOT, out, prefixes=["secrets-v"], builder=builder, versions=lambda _r, _p: "0.1.0")
        self.assertEqual([project for project, _ in builds], ["naapp/secrets/e2e", "naapp/secrets/e2e"])
        self.assertNotEqual(builds[0][1], builds[1][1])
        self.assertEqual(sorted(path.name for path in out.iterdir()), sorted([*files, "SHA256SUMS"]))
        self.assertEqual(FILES.read_sums(out), {name: digest(data) for name, data in files.items()})

    def test_two_builds_with_different_bytes_refuse(self):
        files = lane("aweb-naapp-journey-support", "0.1.0")
        rebuilt = {**files, "aweb_naapp_journey_support-0.1.0.tar.gz": b"another mtime"}
        _builds, builder = self.fake_builder([files, rebuilt])
        out = directory(self, {})
        with self.assertRaisesRegex(VersionRefusal, "not reproducible: aweb_naapp_journey_support-0.1.0.tar.gz"):
            FILES.build(ROOT, out, prefixes=["secrets-v"], builder=builder, versions=lambda _r, _p: "0.1.0")
        self.assertFalse((out / "SHA256SUMS").exists())

    def test_the_install_check_installs_each_lanes_wheels_by_path_together(self):
        files = {**lane("aweb-naapp", "0.6.0"), **lane("aweb-naapp-testing", "0.6.0")}
        out = directory(self, files)
        commands = []
        FILES.check_installs(out, {"aweb-naapp-v": "0.6.0"}, runner=lambda argv: commands.append(argv),
                             modules=lambda wheel: [wheel.name.split("-")[0]])
        (argv,) = commands
        self.assertEqual(argv[:6], ["uv", "run", "--isolated", "--no-project", "--no-config", "--python"])
        wheels = [argv[index + 1] for index, word in enumerate(argv) if word == "--with"]
        self.assertEqual(wheels, [str(out / "aweb_naapp-0.6.0-py3-none-any.whl"),
                                  str(out / "aweb_naapp_testing-0.6.0-py3-none-any.whl")])
        self.assertIn("aweb_naapp_testing", argv[-1])
        self.assertEqual(argv[argv.index("--find-links") + 1], str(out))

    def test_a_real_build_of_the_aweb_naapp_lane_is_reproducible_and_imports_its_wheels(self):
        out = directory(self, {})
        versions = FILES.build(ROOT, out, prefixes=["aweb-naapp-v"])
        version = FILES.manifest_version(ROOT, "naapp-lib")
        self.assertEqual(versions, {"aweb-naapp-v": version})
        self.assertEqual(sorted(FILES.read_sums(out)), [
            f"aweb_naapp-{version}-py3-none-any.whl", f"aweb_naapp-{version}.tar.gz",
            f"aweb_naapp_testing-{version}-py3-none-any.whl", f"aweb_naapp_testing-{version}.tar.gz",
        ])
        FILES.check_installs(out, versions)

    def test_every_lanes_wheels_install_from_this_build_and_import(self):
        out = directory(self, {})
        FILES.check_installs(out, FILES.build(ROOT, out))

    def test_every_lane_is_built_by_default(self):
        self.assertEqual(FILES.lanes_to_build(None), list(PYPI_LANES))
        self.assertEqual(FILES.lanes_to_build(["secrets-v"]), ["secrets-v"])
        with self.assertRaisesRegex(VersionRefusal, "logs-v publishes nothing to PyPI"):
            FILES.lanes_to_build(["logs-v"])


class LockedTagTest(unittest.TestCase):
    def tagged(self, message: str, *, annotated: bool = True) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        init_fixture_repo(root)
        for argv in (["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                      "--allow-empty", "-m", "source"],
                     ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "tag",
                      *(["-a", "-m", message] if annotated else []), "secrets-v0.1.0"]):
            run_fixture(argv, cwd=root, check=True, capture_output=True)
        return root

    def test_an_annotated_tag_locks_the_files_its_message_lists(self):
        files = {"a.whl": "1" * 64, "a.tar.gz": "2" * 64}
        root = self.tagged(f"naapp secrets-v0.1.0\n\nCI run passed.\n\n{FILES.tag_lines(files)}")
        self.assertEqual(FILES.locked_files(root, "secrets-v0.1.0"), files)

    def test_a_tag_that_locks_nothing_refuses(self):
        cases = {
            "not an annotated tag": self.tagged("", annotated=False),
            "lists no release files": self.tagged("naapp secrets-v0.1.0"),
            "more than once": self.tagged(f"{'1' * 64}  a.whl\n{'2' * 64}  a.whl\n"),
        }
        for reason, root in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(VersionRefusal, reason):
                FILES.locked_files(root, "secrets-v0.1.0")

    def test_assets_must_be_exactly_the_locked_files(self):
        files = lane("aweb-naapp-journey-support", "0.1.0")
        locked = {name: digest(data) for name, data in files.items()}
        FILES.verify_assets(directory(self, files), locked)
        wheel = "aweb_naapp_journey_support-0.1.0-py3-none-any.whl"
        cases = {
            "missing locked files": {name: data for name, data in files.items() if name != wheel},
            "not in the tag message": {**files, "extra.whl": b"x"},
            "the tag locks": {**files, wheel: b"rebuilt"},
        }
        for reason, assets in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(VersionRefusal, reason):
                FILES.verify_assets(directory(self, assets), locked)


if __name__ == "__main__":
    unittest.main()
