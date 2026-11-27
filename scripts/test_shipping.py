#!/usr/bin/env python3
"""Focused contracts for the tag-only shipping surface."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from fixture_repo import fixture_env, init_fixture_repo

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CANDIDATE = load("release_candidate", "scripts/release_candidate.py")
PUBLISH = load("publish_release", "scripts/publish_release.py")
VERSIONS = sys.modules["release_versions"]

# Every manifest a release tag reads, with the package it names and its version
# in the fixture repository.
PROJECTS = {
    "naapp-lib": ("aweb-naapp", "0.1.0"),
    "naapp-lib/testing": ("aweb-naapp-testing", "0.1.0"),
    "naapp/secrets": ("aweb-secrets", "0.1.0"),
    "naapp/secrets/e2e": ("aweb-naapp-journey-support", "0.1.0"),
    "naapp/logs": ("aweb-logs", "0.1.0"),
    "naapp/folio": ("folio", "0.1.0"),
    "naapp/library": ("library", "0.1.0"),
    "naapp/secrets/values": ("aweb-secrets-values", "0.1.0rc1"),
    "naapp/secrets/runner": ("aweb-secrets-runner", "0.1.0rc1"),
}


def command(root: Path, *argv: str) -> str:
    completed = subprocess.run(argv, cwd=root, text=True, env=fixture_env(), capture_output=True)
    if completed.returncode:
        raise AssertionError(f"{' '.join(argv)} failed: {completed.stderr}")
    return completed.stdout.strip()


def repository(test: unittest.TestCase, extra: dict[str, str] | None = None) -> Path:
    """A committed fixture repository with every release manifest, plus ``extra`` files."""
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    init_fixture_repo(root)
    command(root, "git", "config", "user.name", "test")
    command(root, "git", "config", "user.email", "test@example.com")
    for directory, (name, version) in PROJECTS.items():
        path = root / directory
        path.mkdir(parents=True, exist_ok=True)
        dependencies = '["aweb-secrets-values==0.1.0rc1"]' if name == "aweb-secrets-runner" else "[]"
        (path / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = {dependencies}\n'
        )
    values = root / "naapp/secrets/values/src/aweb_secrets_values"
    values.mkdir(parents=True)
    (values / "__init__.py").write_text("CODEC_VERSION = 1\n")
    for path, text in (extra or {}).items():
        (root / path).write_text(text)
    command(root, "git", "add", ".")
    command(root, "git", "commit", "-qm", "release source")
    return root


def set_version(root: Path, directory: str, version: str) -> None:
    manifest = root / directory / "pyproject.toml"
    manifest.write_text(re.sub(r'version = "[^"]+"', f'version = "{version}"', manifest.read_text()))


class TagContractTest(unittest.TestCase):
    def test_manifest_tags_must_match_the_tagged_commit(self):
        root = repository(self)
        tags = ["secrets-v0.1.0", "logs-v0.1.0", "folio-v0.1.0", "library-v0.1.0"]
        source, versions = CANDIDATE.validate_tags(root, tags)
        self.assertEqual(len(source), 40)
        self.assertEqual(versions, {"secrets-v": "0.1.0", "logs-v": "0.1.0",
                                    "folio-v": "0.1.0", "library-v": "0.1.0"})
        for tag in ("secrets-v0.1.1", "logs-v0.1.1", "folio-v0.1.1", "library-v0.1.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(CANDIDATE.CandidateRefusal, "does not match"):
                CANDIDATE.validate_tags(root, [tag])

    def test_publisher_reads_version_from_the_tag_not_current_head(self):
        root = repository(self)
        command(root, "git", "tag", "-a", "folio-v0.1.0", "-m", "tested")
        set_version(root, "naapp/folio", "9.9.9")
        command(root, "git", "commit", "-qam", "later")
        prefix, version, source = PUBLISH.tag_identity(root, "folio-v0.1.0")
        self.assertEqual((prefix, version), ("folio-v", "0.1.0"))
        self.assertNotEqual(source, command(root, "git", "rev-parse", "HEAD"))

    def test_image_lanes_tag_finals_only(self):
        for prefix in ("secrets-v", "logs-v", "folio-v", "library-v"):
            self.assertEqual(VERSIONS.tag_parts(f"{prefix}1.2.3"), (prefix, "1.2.3"))
            for tag in (f"{prefix}1.2.3rc1", f"{prefix}1.2.3-rc.1"):
                with self.subTest(tag=tag), self.assertRaises(VERSIONS.VersionRefusal):
                    VERSIONS.tag_parts(tag)

    def test_rc_spelling_and_positive_number_are_strict(self):
        self.assertEqual(VERSIONS.tag_parts("aweb-secrets-runner-v1.2.3rc1"),
                         ("aweb-secrets-runner-v", "1.2.3rc1"))
        for tag in ("aweb-secrets-runner-v1.2.3rc0", "aweb-secrets-runner-v1.2.3rc01",
                    "aweb-secrets-runner-v1.2.3-rc.1", "aweb-secrets-runner-v01.2.3rc1",
                    "aweb-naapp-v1.2.3-beta.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(VERSIONS.VersionRefusal, "unsupported"):
                VERSIONS.tag_parts(tag)

    def test_no_lane_outside_this_repository_has_a_tag(self):
        for tag in ("server-v1.2.3", "awid-service-v1.2.3", "awid-v1.2.3", "aw-v1.2.3",
                    "channel-v1.2.3", "pi-v1.2.3", "skills-v1.2.3", "a2a-gw-v1.2.3"):
            with self.subTest(tag=tag), self.assertRaisesRegex(VERSIONS.VersionRefusal, "unsupported"):
                VERSIONS.tag_parts(tag)


class AwebNaappLaneTest(unittest.TestCase):
    """naapp releases aweb-naapp and aweb-naapp-testing, at one version."""

    def test_an_aweb_naapp_candidate_binds_both_packages_to_its_version(self):
        root = repository(self)
        self.assertEqual(CANDIDATE.validate_tags(root, ["aweb-naapp-v0.1.0"])[1], {"aweb-naapp-v": "0.1.0"})
        command(root, "git", "tag", "-a", "aweb-naapp-v0.1.0", "-m", "tagged")
        self.assertEqual(PUBLISH.tag_identity(root, "aweb-naapp-v0.1.0")[:2], ("aweb-naapp-v", "0.1.0"))

    def test_an_aweb_naapp_testing_version_that_drifted_refuses(self):
        root = repository(self)
        set_version(root, "naapp-lib/testing", "0.1.1")
        command(root, "git", "commit", "-qam", "drifted")
        with self.assertRaisesRegex(CANDIDATE.CandidateRefusal, "naapp-lib/testing"):
            CANDIDATE.validate_tags(root, ["aweb-naapp-v0.1.0"])
        command(root, "git", "tag", "-a", "aweb-naapp-v0.1.0", "-m", "tagged")
        with self.assertRaisesRegex(PUBLISH.PublishRefusal, "naapp-lib/testing"):
            PUBLISH.tag_identity(root, "aweb-naapp-v0.1.0")


class PypiLaneContractTest(unittest.TestCase):
    def test_each_pypi_lane_publishes_the_package_its_manifest_names(self):
        import tomllib

        for prefix, packages in PUBLISH.PYPI_LANES.items():
            manifest = PUBLISH.MANIFESTS[prefix]
            primary = Path(manifest).parent.as_posix()
            self.assertEqual(CANDIDATE.MANIFESTS[prefix], manifest, prefix)
            lockstep = [f"{directory}/pyproject.toml" for _, directory in packages if directory != primary]
            self.assertEqual(CANDIDATE.LOCKSTEP.get(prefix, []), lockstep, prefix)
            if prefix == "aweb-secrets-runner-v":
                names = [package for package, _ in packages]
                self.assertLess(names.index("aweb-secrets-values"), names.index("aweb-secrets-runner"))
            for package, directory in packages:
                project = tomllib.loads((ROOT / directory / "pyproject.toml").read_text(encoding="utf-8"))["project"]
                self.assertEqual(project["name"], package, prefix)

    def test_the_secrets_tag_publishes_the_journey_support_package_and_its_image(self):
        self.assertEqual(PUBLISH.PYPI_LANES["secrets-v"],
                         (("aweb-naapp-journey-support", "naapp/secrets/e2e"),))
        self.assertEqual(PUBLISH.oci_images("secrets-v"), ("ghcr.io/awebai/secrets",))

    def _recording_publisher(self):
        commands: list[tuple[str, ...]] = []

        def run(_root, *argv, **_kwargs):
            commands.append(argv)
            return ""

        for target, value in ((PUBLISH, "run"), (PUBLISH, "request_json")):
            self.addCleanup(setattr, target, value, getattr(target, value))
        self.addCleanup(patch.dict(os.environ, {"UV_PUBLISH_TOKEN": "test-token-not-a-secret"}).stop)
        patch.dict(os.environ, {"UV_PUBLISH_TOKEN": "test-token-not-a-secret"}).start()
        PUBLISH.run, PUBLISH.request_json = run, lambda _url: (200, {})
        return commands

    def test_the_aweb_naapp_lane_publishes_naapp_lib_then_its_testing_package(self):
        root = repository(self)
        commands = self._recording_publisher()
        PUBLISH.publish_pypi(root, "aweb-naapp-v", "0.1.0")
        built = [argv[-1] for argv in commands if argv[:2] == ("uv", "build")]
        inspected = [argv[argv.index("--package") + 1] for argv in commands if "inspect-staged" in argv]
        self.assertEqual(built, ["naapp-lib", "naapp-lib/testing"])
        self.assertEqual(inspected, ["aweb-naapp", "aweb-naapp-testing"])

    def test_the_journey_support_package_must_version_with_secrets(self):
        root = repository(self)
        set_version(root, "naapp/secrets/e2e", "0.0.9")
        command(root, "git", "commit", "-qam", "drifted")
        with self.assertRaisesRegex(CANDIDATE.CandidateRefusal, "naapp/secrets/e2e"):
            CANDIDATE.validate_tags(root, ["secrets-v0.1.0"])
        command(root, "git", "tag", "-a", "secrets-v0.1.0", "-m", "tested")
        with self.assertRaisesRegex(PUBLISH.PublishRefusal, "naapp/secrets/e2e"):
            PUBLISH.tag_identity(root, "secrets-v0.1.0")

    def test_the_journey_support_package_ships_only_its_module(self):
        with tempfile.TemporaryDirectory() as directory:
            command(ROOT, "uv", "build", "--sdist", "--wheel", "--no-sources", "--out-dir", directory,
                    "naapp/secrets/e2e")
            wheels = list(Path(directory).glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                modules = [name for name in wheel.namelist() if ".dist-info/" not in name]
                shipped = wheel.read("team_journey_support.py")
            self.assertEqual(modules, ["team_journey_support.py"])
            self.assertEqual(shipped, (ROOT / "naapp/secrets/e2e/team_journey_support.py").read_bytes())
            (sdist,) = Path(directory).glob("*.tar.gz")
            import tarfile
            with tarfile.open(sdist) as archive:
                sources = sorted(Path(name).name for name in archive.getnames() if name.endswith(".py"))
            self.assertEqual(sources, ["team_journey_support.py"])


class OciLaneContractTest(unittest.TestCase):
    def test_each_image_lane_publishes_its_app_repository(self):
        with patch.dict(os.environ):
            os.environ.pop("NAAPP_IMAGE_NAMESPACE", None)
            self.assertEqual({prefix: PUBLISH.oci_images(prefix) for prefix in PUBLISH.OCI_APPS}, {
                "secrets-v": ("ghcr.io/awebai/secrets",),
                "logs-v": ("ghcr.io/awebai/logs",),
                "folio-v": ("ghcr.io/awebai/folio",),
                "library-v": ("ghcr.io/awebai/library",),
            })

    def test_the_image_namespace_can_name_another_ghcr_owner_or_path(self):
        for namespace, expected in (("ghcr.io/example", "ghcr.io/example/logs"),
                                    ("ghcr.io/awebai/naapp-dryrun", "ghcr.io/awebai/naapp-dryrun/logs")):
            with self.subTest(namespace=namespace), patch.dict(os.environ, {"NAAPP_IMAGE_NAMESPACE": namespace}):
                self.assertEqual(PUBLISH.oci_images("logs-v"), (expected,))

    def test_an_image_namespace_outside_ghcr_or_malformed_refuses(self):
        for namespace in ("docker.io/awebai", "ghcr.io", "ghcr.io/", "ghcr.io/AwebAI", "ghcr.io/awebai/",
                          "ghcr.io//awebai", "ghcr.io/awebai/../x", " ghcr.io/awebai"):
            with self.subTest(namespace=namespace), patch.dict(os.environ, {"NAAPP_IMAGE_NAMESPACE": namespace}):
                with self.assertRaisesRegex(PUBLISH.PublishRefusal, "NAAPP_IMAGE_NAMESPACE"):
                    PUBLISH.oci_images("logs-v")

    def test_the_image_workflow_publishes_under_its_repository_owner_unless_a_variable_names_a_namespace(self):
        workflow = (ROOT / ".github/workflows/service-image-release.yml").read_text(encoding="utf-8")
        self.assertIn(
            "NAAPP_IMAGE_NAMESPACE: ${{ vars.NAAPP_IMAGE_NAMESPACE || format('ghcr.io/{0}', github.repository_owner) }}",
            workflow,
        )

    def test_an_image_tag_dispatches_to_its_image_publisher_only(self):
        root = repository(self)
        command(root, "git", "tag", "-a", "logs-v0.1.0", "-m", "tagged")
        published: list[tuple[str, str]] = []
        with patch.object(PUBLISH, "publish_oci", lambda _root, prefix, version, *_a, **_kw:
                          published.append((prefix, version))), \
                patch.object(PUBLISH, "publish_pypi", lambda *_a, **_kw: self.fail("no PyPI lane")):
            PUBLISH.publish(root, "logs-v0.1.0", only="oci")
        self.assertEqual(published, [("logs-v", "0.1.0")])
        with self.assertRaisesRegex(PUBLISH.PublishRefusal, "does not publish to pypi"):
            PUBLISH.publish(root, "logs-v0.1.0", only="pypi")

    def test_each_image_builds_its_app_dockerfile_from_the_repository_root(self):
        for prefix, app in (("secrets-v", "secrets"), ("logs-v", "logs"),
                            ("folio-v", "folio"), ("library-v", "library")):
            with self.subTest(prefix=prefix):
                self.assertTrue((ROOT / f"naapp/{app}/Dockerfile").is_file())
                calls: list[tuple[str, ...]] = []
                with patch.object(PUBLISH, "run", lambda _root, *argv, **_kw: calls.append(argv)):
                    PUBLISH.build_oci_archive(ROOT, prefix, "1.2.3", "a" * 40, Path("image.oci.tar"))
                (argv,) = calls
                self.assertEqual(argv[:3], ("docker", "buildx", "build"))
                self.assertEqual(argv[argv.index("--file") + 1], f"naapp/{app}/Dockerfile")
                self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64,linux/arm64")
                self.assertIn("org.opencontainers.image.version=1.2.3", argv)
                self.assertIn(f"org.opencontainers.image.revision={'a' * 40}", argv)
                self.assertIn(f"SOURCE_COMMIT={'a' * 40}", argv)
                self.assertEqual(argv[-1], ".")


def ci_run(sha: str, *, run_id: int = 7, event: str = "push", branch: str = "main",
           status: str = "completed", conclusion: str = "success", jobs=None) -> dict:
    """A CI run as release_candidate.ci_runs reports it."""
    if jobs is None:
        jobs = [("Select the affected packages", "success"), ("Secrets lint and tests", "success"),
                ("naapp CI", "success")]
    return {"id": run_id, "head_sha": sha, "event": event, "head_branch": branch, "status": status,
            "conclusion": conclusion, "html_url": f"https://github.com/awebai/naapp/actions/runs/{run_id}",
            "jobs": [{"name": name, "conclusion": result} for name, result in jobs]}


class QualifyingRunTest(unittest.TestCase):
    """A commit qualifies by one CI run of exactly that commit in which every job succeeded."""

    SHA = "a" * 40

    def test_a_green_push_run_on_main_qualifies(self):
        green = ci_run(self.SHA)
        self.assertEqual(CANDIDATE.qualifying_run([green], self.SHA), green)

    def test_a_green_manual_run_of_the_commit_qualifies(self):
        manual = ci_run(self.SHA, event="workflow_dispatch", branch="qualify")
        self.assertEqual(CANDIDATE.qualifying_run([manual], self.SHA), manual)

    def test_a_qualifying_run_is_found_among_others(self):
        green = ci_run(self.SHA, run_id=9)
        runs = [ci_run(self.SHA, run_id=8, status="completed", conclusion="cancelled"),
                ci_run("b" * 40, run_id=10), green]
        self.assertEqual(CANDIDATE.qualifying_run(runs, self.SHA), green)

    def test_runs_that_do_not_qualify_are_refused_with_their_reasons(self):
        cases = {
            "no CI run": [],
            "another commit": [ci_run("b" * 40)],
            "pull_request run on feature": [ci_run(self.SHA, event="pull_request", branch="feature")],
            "push run on feature": [ci_run(self.SHA, branch="feature")],
            "in_progress": [ci_run(self.SHA, status="in_progress", conclusion="")],
            "failure": [ci_run(self.SHA, conclusion="failure")],
            "Logs lint and tests skipped": [ci_run(self.SHA, jobs=[("Logs lint and tests", "skipped"),
                                                                     ("naapp CI", "success")])],
            "no naapp CI job": [ci_run(self.SHA, jobs=[("Logs lint and tests", "success")])],
        }
        for reason, runs in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(CANDIDATE.CandidateRefusal) as refused:
                    CANDIDATE.qualifying_run(runs, self.SHA)
                message = str(refused.exception)
                self.assertIn(f"no CI run qualifies {self.SHA}", message)
                if runs and runs[0]["head_sha"] == self.SHA:
                    self.assertIn(reason, message)

    def test_runs_are_read_from_the_ci_workflow_with_their_jobs(self):
        stub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, stub)
        listed = {"workflow_runs": [{key: value for key, value in ci_run(self.SHA).items() if key != "jobs"}]}
        jobs = {"jobs": [{"name": "naapp CI", "conclusion": "success", "steps": []}]}
        (stub / "gh").write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            "open(sys.argv[0] + '.calls', 'a').write(json.dumps(argv) + '\\n')\n"
            "if argv[:2] == ['repo', 'view']:\n"
            "    print('awebai/naapp')\n"
            "elif argv[1].endswith('/jobs?per_page=100'):\n"
            f"    print({json.dumps(json.dumps(jobs))})\n"
            "else:\n"
            f"    print({json.dumps(json.dumps(listed))})\n"
        )
        (stub / "gh").chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            runs = CANDIDATE.ci_runs(ROOT, self.SHA)
        self.assertEqual(runs, [ci_run(self.SHA, jobs=[("naapp CI", "success")])])
        calls = [json.loads(line) for line in (stub / "gh.calls").read_text().splitlines()]
        self.assertEqual(calls[1:], [
            ["api", f"repos/awebai/naapp/actions/workflows/ci.yml/runs?head_sha={self.SHA}&per_page=100"],
            ["api", "repos/awebai/naapp/actions/runs/7/jobs?per_page=100"],
        ])


class CandidateContractTest(unittest.TestCase):
    """Tags are created only on a commit of main that a fully green CI run tested."""

    def candidate(self) -> Path:
        root = repository(self)
        origin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, origin)
        command(origin, "git", "init", "-q", "--bare")
        command(root, "git", "remote", "add", "origin", str(origin))
        command(root, "git", "push", "-q", "origin", "main")
        command(root, "git", "fetch", "-q", "origin")
        return root

    def test_a_qualified_commit_is_tagged_with_its_run(self):
        root = self.candidate()
        source = command(root, "git", "rev-parse", "HEAD")
        set_version(root, "naapp/logs", "0.2.0")
        command(root, "git", "commit", "-qam", "later")
        command(root, "git", "push", "-q", "origin", "main")
        printed = io.StringIO()
        with redirect_stdout(printed):
            tagged = CANDIDATE.create_candidate(root, ["logs-v0.1.0"], source,
                                                runs=lambda _root, sha: [ci_run(sha)])
        self.assertEqual(tagged, source)
        self.assertIn(f"candidate PASSED: {source}", printed.getvalue())
        self.assertEqual(command(root, "git", "rev-parse", "logs-v0.1.0^{commit}"), source)
        self.assertIn(f"CI run https://github.com/awebai/naapp/actions/runs/7 passed every job for {source}.",
                      command(root, "git", "tag", "-l", "-n9", "logs-v0.1.0"))

    def test_an_unqualified_commit_gets_no_tag(self):
        root = self.candidate()
        with self.assertRaisesRegex(CANDIDATE.CandidateRefusal, "no CI run qualifies"):
            CANDIDATE.create_candidate(root, ["logs-v0.1.0"],
                                       runs=lambda _root, sha: [ci_run(sha, conclusion="failure")])
        self.assertEqual(command(root, "git", "tag", "--list"), "")

    def test_a_commit_not_on_main_gets_no_tag(self):
        root = self.candidate()
        command(root, "git", "commit", "-q", "--allow-empty", "-m", "unpushed")
        with self.assertRaisesRegex(CANDIDATE.CandidateRefusal, "is not on origin/main"):
            CANDIDATE.create_candidate(root, ["logs-v0.1.0"], runs=lambda _root, sha: [ci_run(sha)])
        self.assertEqual(command(root, "git", "tag", "--list"), "")

    def test_the_manifests_are_read_at_the_source_commit_not_the_checkout(self):
        root = self.candidate()
        set_version(root, "naapp/logs", "0.3.0")
        source, versions = CANDIDATE.validate_tags(root, ["logs-v0.1.0"])
        self.assertEqual(versions, {"logs-v": "0.1.0"})
        self.assertEqual(source, command(root, "git", "rev-parse", "HEAD"))

    def test_the_command_takes_a_source_commit(self):
        root = self.candidate()
        refused = subprocess.run(
            [sys.executable, str(ROOT / "scripts/release_candidate.py"), "--source", "no-such-commit",
             "logs-v0.1.0"],
            cwd=root, env=fixture_env(), capture_output=True, text=True,
        )
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("candidate refused:", refused.stderr)
        self.assertIn("no-such-commit", refused.stderr)
        self.assertEqual(command(root, "git", "tag", "--list"), "")


class CustomerReleaseContractTest(unittest.TestCase):
    """Customer lanes use the real tag/manifest/promotion boundary, without publishing."""

    def repository(self):
        return repository(self)

    def test_customer_candidates_bind_each_image_and_runner_to_its_version(self):
        root = self.repository()
        source, versions = CANDIDATE.validate_tags(
            root, ["secrets-v0.1.0", "logs-v0.1.0", "folio-v0.1.0", "library-v0.1.0",
                   "aweb-secrets-runner-v0.1.0rc1"]
        )
        self.assertEqual(source, command(root, "git", "rev-parse", "HEAD"))
        self.assertEqual(versions, {
            "secrets-v": "0.1.0", "logs-v": "0.1.0", "folio-v": "0.1.0", "library-v": "0.1.0",
            "aweb-secrets-runner-v": "0.1.0rc1",
        })

    def test_customer_image_version_mismatch_refuses(self):
        root = self.repository()
        CANDIDATE.validate_tags(root, ["secrets-v0.1.0", "logs-v0.1.0"])
        for tag in ("secrets-v0.1.1", "logs-v0.1.1"):
            with self.subTest(tag=tag), self.assertRaises(CANDIDATE.CandidateRefusal):
                CANDIDATE.validate_tags(root, [tag])

    def test_runner_values_version_mismatch_refuses_candidate_and_publisher(self):
        root = self.repository()
        tag = "aweb-secrets-runner-v0.1.0rc1"
        CANDIDATE.validate_tags(root, [tag])
        values = root / "naapp/secrets/values/pyproject.toml"
        values.write_text(values.read_text().replace('version = "0.1.0rc1"', 'version = "0.1.1rc1"'))
        command(root, "git", "commit", "-qam", "mismatched values")
        with self.assertRaises(CANDIDATE.CandidateRefusal):
            CANDIDATE.validate_tags(root, [tag])
        command(root, "git", "tag", "-a", tag, "-m", "candidate")
        with self.assertRaises(PUBLISH.PublishRefusal):
            PUBLISH.tag_identity(root, tag)

    def test_customer_publisher_uses_tagged_source_not_later_manifests(self):
        root = self.repository()
        source = command(root, "git", "rev-parse", "HEAD")
        tags = ("secrets-v0.1.0", "logs-v0.1.0", "folio-v0.1.0", "library-v0.1.0",
                "aweb-secrets-runner-v0.1.0rc1")
        for tag in tags:
            command(root, "git", "tag", "-a", tag, "-m", "candidate")
        for directory in PROJECTS:
            manifest = root / directory / "pyproject.toml"
            manifest.write_text(re.sub(r'version = "[^"]+"', 'version = "9.9.9"', manifest.read_text()))
        command(root, "git", "commit", "-qam", "later unrelated release")
        for tag in tags:
            with self.subTest(tag=tag):
                prefix, version, observed = PUBLISH.tag_identity(root, tag)
                self.assertEqual((prefix, version), VERSIONS.tag_parts(tag))
                self.assertEqual(observed, source)

    def test_equal_versions_with_wrong_values_dependency_refuse(self):
        root = self.repository()
        tag = "aweb-secrets-runner-v0.1.0rc1"
        CANDIDATE.validate_tags(root, [tag])
        manifest = root / "naapp/secrets/runner/pyproject.toml"
        manifest.write_text(manifest.read_text().replace(
            'aweb-secrets-values==0.1.0rc1', 'aweb-secrets-values==0.0.9'
        ))
        command(root, "git", "commit", "-qam", "wrong dependency with equal package versions")
        with self.assertRaises(CANDIDATE.CandidateRefusal):
            CANDIDATE.validate_tags(root, [tag])
        command(root, "git", "tag", "-a", tag, "-m", "bad dependency")
        with self.assertRaises(PUBLISH.PublishRefusal):
            PUBLISH.tag_identity(root, tag)

    def test_runner_final_needs_no_prior_rc_and_keeps_lockstep_values(self):
        root = self.repository()
        for directory in ("naapp/secrets/values", "naapp/secrets/runner"):
            manifest = root / directory / "pyproject.toml"
            manifest.write_text(manifest.read_text().replace("0.1.0rc1", "0.1.0"))
        command(root, "git", "commit", "-qam", "final versions")
        _source, versions = CANDIDATE.validate_tags(root, ["aweb-secrets-runner-v0.1.0"])
        self.assertEqual(versions["aweb-secrets-runner-v"], "0.1.0")
        runner = root / "naapp/secrets/runner/pyproject.toml"
        runner.write_text(runner.read_text().replace("aweb-secrets-values==0.1.0", "aweb-secrets-values==0.0.9"))
        command(root, "git", "commit", "-qam", "runner pins another values release")
        with self.assertRaises(VERSIONS.VersionRefusal):
            CANDIDATE.validate_tags(root, ["aweb-secrets-runner-v0.1.0"])

    def test_built_runner_requires_the_exact_values_release_and_toolkit_floor(self):
        from email.parser import BytesParser
        import tomllib

        version = tomllib.loads((ROOT / "naapp/secrets/values/pyproject.toml").read_text())["project"]["version"]
        with tempfile.TemporaryDirectory() as directory:
            command(ROOT, "uv", "build", "--wheel", "--no-sources", "--out-dir", directory,
                    "naapp/secrets/runner")
            wheels = list(Path(directory).glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
                self.assertEqual(len(names), 1)
                requirements = BytesParser().parsebytes(wheel.read(names[0])).get_all("Requires-Dist")
            self.assertIn(f"aweb-secrets-values=={version}", requirements)
            self.assertIn("aweb-naapp[pages]>=0.5.1", requirements)



if __name__ == "__main__":
    unittest.main()
