"""The copied aweb server test helpers are exactly the release this repository pins."""

import ast
import hashlib
import importlib
import tomllib
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent / "aweb_server_tests"
LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
MODULES = sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem != "__init__")


def _definitions(path: Path) -> dict[str, str]:
    """Each top-level definition's source text, from its first decorator to its end."""
    source = path.read_text()
    lines = source.splitlines()
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [leaf.id for target in targets for leaf in ast.walk(target) if isinstance(leaf, ast.Name)]
        else:
            continue
        first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        text = "\n".join(lines[first - 1 : node.end_lineno])
        for name in names:
            found[name] = text
    return found


def test_the_copies_come_from_the_pinned_aweb_server_release() -> None:
    lock = tomllib.loads(LOCK.read_text())
    (aweb,) = [package for package in lock["package"] if package["name"] == "aweb"]
    for name in MODULES:
        module = importlib.import_module(f"tests.server_compat.aweb_server_tests.{name}")
        assert module.AWEB_TAG == f"server-v{aweb['version']}", (
            f"{name} was copied from {module.AWEB_TAG} but naapp-lib pins aweb {aweb['version']}: "
            "rerun scripts/server_test_support.py from that tag"
        )


@pytest.mark.parametrize("name", MODULES)
def test_every_copied_definition_matches_its_recorded_source(name: str) -> None:
    module = importlib.import_module(f"tests.server_compat.aweb_server_tests.{name}")
    definitions = _definitions(PACKAGE / f"{name}.py")
    recorded = dict(module.COPIED)
    copied = {key: text for key, text in definitions.items() if key not in {"AWEB_TAG", "AWEB_PATH", "AWEB_BLOB", "COPIED", "ENVIRONMENT_DEFAULTS"}}
    texts = {text for text in copied.values()}
    digests = {hashlib.sha256(text.encode()).hexdigest() for text in texts}
    assert digests == set(recorded.values()), (
        f"{name}: copied definitions differ from the record of {module.AWEB_PATH} at {module.AWEB_TAG}"
    )
    for key, digest in recorded.items():
        assert hashlib.sha256(copied[key].encode()).hexdigest() == digest, key
