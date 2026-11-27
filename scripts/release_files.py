#!/usr/bin/env python3
"""The files a PyPI release uploads: built in CI, locked by sha256, never rebuilt.

``build`` builds the wheel and sdist of every package of the PyPI lanes twice
and refuses unless both builds are byte for byte the same, then writes
SHA256SUMS beside the files and installs each lane's wheels, by path, into a
clean environment that imports them. CI uploads that directory as the run's
release-files artifact. A release tag's annotated message repeats the sha256
lines of its lane's files, and the PyPI workflow uploads a published release's
assets only when they are exactly those files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path

from release_versions import PYPI_LANES, VersionRefusal

SUMS = "SHA256SUMS"
# One locked file in sha256sum format: the lowercase sha256, two spaces, the
# file's basename.
LOCKED_FILE = re.compile(r"([0-9a-f]{64})  ([^\s/]+)")
INSTALL_PYTHON = "3.12"
# Run in the clean environment: each wheel is installed from its own path and
# its top-level modules import.
INSTALL_CHECK = """\
import importlib, importlib.metadata, json, sys
from pathlib import Path
for name, wheel, modules in json.loads(sys.argv[1]):
    direct = importlib.metadata.distribution(name).read_text("direct_url.json") or "{}"
    url = json.loads(direct).get("url")
    if url != Path(wheel).as_uri():
        sys.exit(f"{name} is installed from {url}, not from {wheel}")
    for module in modules:
        importlib.import_module(module)
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digests(directory: Path) -> dict[str, str]:
    """Every regular file of ``directory`` but SHA256SUMS and dotfiles, as name -> sha256."""
    found = {}
    for path in sorted(directory.iterdir()):
        if path.name == SUMS or path.name.startswith("."):
            continue
        if not path.is_file() or path.is_symlink():
            raise VersionRefusal(f"{path.name} in {directory} is not a regular file")
        found[path.name] = sha256(path)
    return found


def tag_lines(files: dict[str, str]) -> str:
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))


def write_sums(directory: Path) -> None:
    (directory / SUMS).write_text(tag_lines(digests(directory)), encoding="utf-8")


def read_sums(directory: Path) -> dict[str, str]:
    """The files SHA256SUMS lists, refusing unless they are exactly the directory's files."""
    listed: dict[str, str] = {}
    for line in (directory / SUMS).read_text(encoding="utf-8").splitlines():
        matched = LOCKED_FILE.fullmatch(line)
        if not matched:
            raise VersionRefusal(f"{SUMS} has a line that is not a sha256 and a file name: {line!r}")
        listed[matched.group(2)] = matched.group(1)
    present = digests(directory)
    if missing := sorted(set(listed) - set(present)):
        raise VersionRefusal(f"{SUMS} lists missing files: {', '.join(missing)}")
    if unlisted := sorted(set(present) - set(listed)):
        raise VersionRefusal(f"{', '.join(unlisted)} not in {SUMS}")
    for name, digest in sorted(listed.items()):
        if present[name] != digest:
            raise VersionRefusal(f"{name} has sha256 {present[name]}, {SUMS} lists {digest}")
    return listed


def package_files(directory: Path, package: str, version: str) -> tuple[str, str]:
    """The one wheel and the one sdist of ``package`` at ``version`` in ``directory``."""
    name = package.replace("-", "_")
    present = [path.name for path in sorted(directory.iterdir())]
    wheels = [file for file in present if re.fullmatch(
        re.escape(f"{name}-") + r"[^-]+-[^-]+-[^-]+-[^-]+\.whl", file)]
    sdists = [file for file in present if re.fullmatch(re.escape(f"{name}-") + r"[^-]+\.tar\.gz", file)]
    wheel, sdist = f"{name}-{version}-py3-none-any.whl", f"{name}-{version}.tar.gz"
    if wheels != [wheel] or sdists != [sdist]:
        raise VersionRefusal(
            f"{directory} must hold exactly {wheel} and {sdist}; found wheels {wheels} and sdists {sdists}"
        )
    return wheel, sdist


def lane_files(directory: Path, prefix: str, version: str) -> list[str]:
    """The release files of one PyPI lane at ``version``, its packages in order."""
    return [file for package, _ in PYPI_LANES[prefix] for file in package_files(directory, package, version)]


def lanes_to_build(prefixes: Iterable[str] | None) -> list[str]:
    if prefixes is None:
        return list(PYPI_LANES)
    chosen = list(prefixes)
    for prefix in chosen:
        if prefix not in PYPI_LANES:
            raise VersionRefusal(f"{prefix} publishes nothing to PyPI")
    return chosen


def manifest_version(root: Path, directory: str) -> str:
    with (root / directory / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def uv_build(root: Path, project: str, out: Path) -> None:
    run_command(["uv", "build", "--quiet", "--no-sources", "--sdist", "--wheel", "--out-dir", str(out), project],
                cwd=root)


def run_command(argv: list[str], cwd: Path | None = None) -> None:
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise VersionRefusal(f"{' '.join(argv)} failed: {detail}")


def build(
    root: Path,
    out: Path,
    prefixes: Iterable[str] | None = None,
    builder: Callable[[Path, str, Path], None] = uv_build,
    versions: Callable[[Path, str], str] = manifest_version,
) -> dict[str, str]:
    """Build each lane's packages twice into ``out``; return each built lane's version."""
    built = {}
    for prefix in lanes_to_build(prefixes):
        for _package, directory in PYPI_LANES[prefix]:
            with tempfile.TemporaryDirectory(prefix="naapp-release-files-") as temporary:
                first, second = Path(temporary) / "first", Path(temporary) / "second"
                first.mkdir()
                second.mkdir()
                builder(root, directory, first)
                builder(root, directory, second)
                files = digests(first)
                rebuilt = digests(second)
                if differing := sorted(name for name in {*files, *rebuilt} if files.get(name) != rebuilt.get(name)):
                    raise VersionRefusal(f"the build of {directory} is not reproducible: {', '.join(differing)}")
                for name in files:
                    if (out / name).exists():
                        raise VersionRefusal(f"{name} was built twice")
                    shutil.copyfile(first / name, out / name)
        built[prefix] = versions(root, PYPI_LANES[prefix][0][1])
    shipped = {file for prefix, version in built.items() for file in lane_files(out, prefix, version)}
    if extra := sorted(set(digests(out)) - shipped):
        raise VersionRefusal(f"the builds left files no lane ships: {', '.join(extra)}")
    write_sums(out)
    return built


def wheel_modules(wheel: Path) -> list[str]:
    """The top-level modules and packages a wheel installs."""
    with zipfile.ZipFile(wheel) as archive:
        tops = {name.split("/", 1)[0] for name in archive.namelist()}
    return sorted(top.removesuffix(".py") for top in tops if not top.endswith((".dist-info", ".data")))


def check_installs(
    out: Path,
    versions: dict[str, str],
    runner: Callable[[list[str]], None] = run_command,
    modules: Callable[[Path], list[str]] = wheel_modules,
    find_links: bool = True,
) -> None:
    """Install each lane's wheels together, by path, into a clean environment and import them.

    With ``find_links`` a lane resolves another lane's packages from ``out``
    when PyPI lacks them; without it every other dependency comes from PyPI,
    as it will for whoever installs the published files."""
    for prefix, version in versions.items():
        specs = []
        for package, _ in PYPI_LANES[prefix]:
            wheel = out / package_files(out, package, version)[0]
            specs.append((package, str(wheel), modules(wheel)))
        # A lane that depends on another lane resolves it from this build when PyPI lacks it.
        argv = ["uv", "run", "--isolated", "--no-project", "--no-config", "--python", INSTALL_PYTHON,
                *(["--find-links", str(out)] if find_links else [])]
        for _package, wheel, _modules in specs:
            argv += ["--with", wheel]
        runner([*argv, "python", "-I", "-c", INSTALL_CHECK, json.dumps(specs)])


def locked_files(root: Path, tag: str) -> dict[str, str]:
    """The release files an annotated tag's message locks, as name -> sha256."""
    ref = f"refs/tags/{tag}"

    def git(*argv: str) -> str:
        completed = subprocess.run(["git", *argv], cwd=root, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise VersionRefusal(f"git {' '.join(argv)} failed: {completed.stderr.strip()}")
        return completed.stdout

    if git("cat-file", "-t", ref).strip() != "tag":
        raise VersionRefusal(f"{tag} is not an annotated tag; its message must list the release files")
    locked: dict[str, str] = {}
    for line in git("for-each-ref", "--format=%(contents)", ref).splitlines():
        matched = LOCKED_FILE.fullmatch(line)
        if not matched:
            continue
        digest, name = matched.groups()
        if name in locked:
            raise VersionRefusal(f"{tag} lists {name} more than once")
        locked[name] = digest
    if not locked:
        raise VersionRefusal(f"{tag} lists no release files")
    return locked


def verify_assets(assets: Path, locked: dict[str, str]) -> None:
    """Refuse unless ``assets`` holds exactly the locked files, byte for byte."""
    present = {path.name: path for path in assets.iterdir()}
    if missing := sorted(set(locked) - set(present)):
        raise VersionRefusal(f"release assets are missing locked files: {', '.join(missing)}")
    if extra := sorted(set(present) - set(locked)):
        raise VersionRefusal(f"release assets hold files not in the tag message: {', '.join(extra)}")
    for name, digest in sorted(locked.items()):
        path = present[name]
        if not path.is_file() or path.is_symlink():
            raise VersionRefusal(f"release asset {name} is not a regular file")
        if (actual := sha256(path)) != digest:
            raise VersionRefusal(f"release asset {name} has sha256 {actual}, the tag locks {digest}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build", help="build, lock and install-check the release files")
    built.add_argument("--out", type=Path, required=True, help="an empty directory for the files")
    built.add_argument("--lane", action="append", help="one PyPI lane prefix; repeat; default every lane")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        arguments.out.mkdir(parents=True, exist_ok=True)
        if any(arguments.out.iterdir()):
            raise VersionRefusal(f"{arguments.out} is not empty")
        check_installs(arguments.out, build(root, arguments.out, arguments.lane))
    except (VersionRefusal, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"release files refused: {error}", file=sys.stderr)
        return 1
    print((arguments.out / SUMS).read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
