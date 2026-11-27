#!/usr/bin/env python3
"""Write the aweb server test helpers the server-compatibility suite uses.

Usage: scripts/server_test_support.py <aweb checkout> <aweb server release tag | commit>

The suite in naapp-lib/tests/server_compat drives this repository's runtime
against the real aweb server it pins. Its fixtures and helpers are aweb's own
server test code, which aweb does not publish. This script copies, verbatim
from server/tests at the given tag, only the top-level definitions the suite
names (ROOTS) and what they use in turn, into one module per aweb file under
naapp-lib/tests/server_compat/aweb_server_tests (aweb's file name without its
test_ prefix), with the imports they need. Each module records the tag, the
aweb path, that file's blob and the sha256 of every copied definition's source
text; tests/server_compat/test_aweb_server_tests.py checks the copies against
that record. Run it again from the new tag when the aweb pin moves.

A full commit SHA in place of the tag copies from a reviewed aweb commit whose
server/tests change no release carries yet; the record names that commit, and
the copies are replaced by the re-copy from the first server tag that does.
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "naapp-lib/tests/server_compat/aweb_server_tests"
IMPORT_PREFIX = "tests.server_compat.aweb_server_tests"
TESTS = "server/tests"

# The definitions the suite's test files and its real_stack import or take as
# fixtures, by aweb module.
ROOTS = {
    "conftest": ("shared_test_pool", "aweb_cloud_db"),
    "installation_fixtures": ("seed_hook_installation", "install_fixture_app"),
    "test_app_events_http": ("_build_events_app", "_seed_team_and_agent"),
    "test_team_auth_deps": ("_make_certificate", "_encode_certificate"),
    "test_app_task_requests": ("_app_http", "_body", "_sign", "_stage", "app_request_context"),
    "test_dashboard_privacy": ("TEAM", "privacy_app"),
    "test_task_answers": ("_v2_request",),
}

# Module-level statements a copied definition relies on without naming them.
# aweb modules whose top level sets environment defaults with os.environ.setdefault.
ENVIRONMENT_DEFAULT_MODULES = {"conftest"}


def environment_defaults(current) -> dict[str, str]:
    """The os.environ.setdefault(name, value) calls at the top level of an aweb module."""
    if current.name not in ENVIRONMENT_DEFAULT_MODULES:
        return {}
    defaults = {}
    for node in current.tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (
            isinstance(call, ast.Call)
            and ast.unparse(call.func) == "os.environ.setdefault"
            and len(call.args) == 2
        ):
            defaults[ast.literal_eval(call.args[0])] = ast.literal_eval(call.args[1])
    return defaults

BUILTIN_FIXTURES = {"monkeypatch", "tmp_path", "tmp_path_factory", "request", "capsys", "caplog"}


def git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True
    ).stdout


class Module:
    def __init__(self, checkout: Path, tag: str, name: str, local: set[str]) -> None:
        self.name = name
        self.path = f"{TESTS}/{name}.py"
        self.blob = git(checkout, "rev-parse", f"{tag}:{self.path}").strip()
        self.source = git(checkout, "show", f"{tag}:{self.path}")
        self.tree = ast.parse(self.source)
        self.local = local
        self.definitions: dict[str, ast.stmt] = {}
        self.imports: dict[str, ast.stmt] = {}
        self.local_imports: dict[str, tuple[str, str]] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.definitions[node.name] = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name):
                            self.definitions[leaf.id] = node
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_import(node)
            elif isinstance(node, ast.If):
                for inner in node.body:
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        self._record_import(inner)

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        module = getattr(node, "module", None) or ""
        source = module.removeprefix("tests.")
        for alias in node.names:
            bound = (alias.asname or alias.name).split(".")[0]
            if isinstance(node, ast.ImportFrom) and source in self.local:
                self.local_imports[bound] = (source, alias.name)
            else:
                self.imports[bound] = node

    def text(self, node: ast.stmt) -> str:
        first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        lines = self.source.splitlines()
        return "\n".join(lines[first - 1 : node.end_lineno])


def referenced(node: ast.stmt) -> set[str]:
    names = {leaf.id for leaf in ast.walk(node) if isinstance(leaf, ast.Name)}
    for leaf in ast.walk(node):
        if isinstance(leaf, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names |= {arg.arg for arg in leaf.args.args + leaf.args.kwonlyargs}
        if isinstance(leaf, ast.Attribute) and isinstance(leaf.value, ast.Name):
            names.add(leaf.value.id)
    return names


def collect(checkout: Path, tag: str):
    listing = git(checkout, "ls-tree", "--name-only", f"{tag}:{TESTS}").split()
    local = {name[:-3] for name in listing if name.endswith(".py")}
    modules: dict[str, Module] = {}

    def module(name: str) -> Module:
        if name not in modules:
            modules[name] = Module(checkout, tag, name, local)
        return modules[name]

    # Fixtures a copied definition can take by parameter: those of the modules it
    # comes from and of conftest, which pytest offers to every test.
    fixtures = {}
    for name in (*ROOTS, "conftest"):
        for definition, node in module(name).definitions.items():
            if any("fixture" in ast.unparse(d) for d in getattr(node, "decorator_list", [])):
                fixtures.setdefault(definition, name)

    chosen: dict[tuple[str, str], ast.stmt] = {}
    imports: dict[str, list[ast.stmt]] = {}
    nested: dict[str, str] = {}
    pending = [(source, name) for source, names in ROOTS.items() for name in names]
    while pending:
        source, name = pending.pop()
        current = module(source)
        if name in current.local_imports:
            pending.append(current.local_imports[name])
            continue
        if name not in current.definitions:
            raise SystemExit(f"{current.path} at {tag} defines no {name}")
        node = current.definitions[name]
        if (source, name) in chosen:
            continue
        chosen[(source, name)] = node
        # Imports inside a definition name aweb's modules as aweb lays them out.
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module and inner.module.removeprefix("tests.") in local:
                imported = inner.module.removeprefix("tests.")
                nested[inner.module] = imported
                pending.extend((imported, alias.name) for alias in inner.names)
        for used in referenced(node) - {name}:
            if used in current.definitions:
                pending.append((source, used))
            elif used in current.local_imports:
                pending.append(current.local_imports[used])
            elif used in current.imports:
                statement = current.imports[used]
                imports.setdefault(source, [])
                if statement not in imports[source]:
                    imports[source].append(statement)
            elif used in fixtures and used not in BUILTIN_FIXTURES:
                pending.append((fixtures[used], used))
    return modules, chosen, imports, nested


# Names this repository's runtime defines in another module than the runtime the
# aweb release was tested with: (module at the release, name) -> module here.
RUNTIME_MOVES = {
    ("aweb_naapp.runtime.task_requests", "app_request_credential"): "aweb_naapp.runtime.app_credentials",
}


def relocated(statement: ast.stmt) -> list[ast.stmt]:
    """``statement``, with each name RUNTIME_MOVES relocates imported from its module here."""
    if not isinstance(statement, ast.ImportFrom):
        return [statement]
    by_module: dict[str, list[ast.alias]] = {}
    for alias in statement.names:
        target = RUNTIME_MOVES.get((statement.module, alias.name), statement.module)
        by_module.setdefault(target, []).append(alias)
    return [ast.ImportFrom(module=module, names=names, level=statement.level) for module, names in by_module.items()]


def module_name(source: str) -> str:
    """The copy's module name: aweb's, without the test_ prefix pytest collects,
    and never conftest, which pytest would load as one."""
    return "server_conftest" if source == "conftest" else source.removeprefix("test_")


def render_module(tag: str, current: Module, chosen, imports) -> str:
    """``tag`` is the server release tag, or the full aweb commit, copied from."""
    nodes = sorted(
        {id(node): (node.lineno, node) for (s, _), node in chosen.items() if s == current.name}.values()
    )
    names_of = {}
    for (s, name), node in chosen.items():
        if s == current.name:
            names_of.setdefault(id(node), []).append(name)
    sibling_imports: dict[str, list[tuple[str, str]]] = {}
    for _, node in nodes:
        for used in sorted(referenced(node)):
            if used in current.local_imports and used not in current.definitions:
                source, original = current.local_imports[used]
                sibling_imports.setdefault(source, [])
                if (original, used) not in sibling_imports[source]:
                    sibling_imports[source].append((original, used))
    lines = [
        f'"""aweb {current.path} at {tag}, the definitions the server-compatibility suite uses.',
        "",
        "Copied verbatim by scripts/server_test_support.py and checked against COPIED by",
        "tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the",
        "script from the aweb tag this repository pins.",
        '"""',
        "# ruff: noqa",
    ]
    # aweb's module-level environment defaults, recorded as data: the suite's
    # conftest applies them only while its own tests run, never at import.
    defaults = environment_defaults(current)
    statements = list(imports.get(current.name, []))
    # Imports are not part of a copied definition: they are written in canonical
    # form and order, so a regenerated module is byte-identical.
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in current.tree.body):
        lines.append("from __future__ import annotations")
    lines += sorted({ast.unparse(moved) for statement in statements for moved in relocated(statement)})
    for source, pairs in sorted(sibling_imports.items()):
        names = ", ".join(original if original == bound else f"{original} as {bound}" for original, bound in sorted(pairs))
        lines.append(f"from {IMPORT_PREFIX}.{module_name(source)} import {names}")
    record = []
    blocks = [f"ENVIRONMENT_DEFAULTS = {defaults!r}"] if defaults else []
    for _, node in nodes:
        text = current.text(node)
        blocks.append(text)
        record.append((sorted(names_of[id(node)])[0], hashlib.sha256(text.encode()).hexdigest()))
    lines += [
        "",
        *(
            [
                "# Copied from an aweb commit, not a release: replaced by the re-copy",
                "# from the first server tag that carries this change.",
            ]
            if is_commit(tag)
            else []
        ),
        f"AWEB_REF = {tag!r}",
        f"AWEB_PATH = {current.path!r}",
        f"AWEB_BLOB = {current.blob!r}",
        "COPIED = {",
        *(f"    {name!r}: {digest!r}," for name, digest in record),
        "}",
        "",
        "",
        "\n\n\n".join(blocks),
    ]
    return "\n".join(lines) + "\n"


def is_commit(ref: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", ref) is not None


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} <aweb checkout> <aweb server release tag | commit>")
    checkout, tag = Path(sys.argv[1]), sys.argv[2]
    if not (tag.startswith("server-v") or is_commit(tag)):
        raise SystemExit(f"{tag!r} is neither a server release tag nor a full commit SHA")
    modules, chosen, imports, nested = collect(checkout, tag)
    sources = sorted({source for source, _ in chosen})
    PACKAGE.mkdir(parents=True, exist_ok=True)
    for stale in PACKAGE.glob("*.py"):
        stale.unlink()
    aliases = "".join(f"    {name!r}: {module_name(source)!r},\n" for name, source in sorted(nested.items()))
    (PACKAGE / "__init__.py").write_text(
        f'"""aweb\'s server test helpers at {tag}, one module per aweb file."""\n\n'
        "# Module names that copied definitions import inside their bodies, as aweb\n"
        "# lays its tests out, with the copy each one names.\n"
        f"AWEB_MODULE_NAMES = {{\n{aliases}}}\n"
    )
    for source in sources:
        (PACKAGE / f"{module_name(source)}.py").write_text(render_module(tag, modules[source], chosen, imports))
    print(f"wrote {len(chosen)} definitions from {len(sources)} aweb modules into {PACKAGE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
