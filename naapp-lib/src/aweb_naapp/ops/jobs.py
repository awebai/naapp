"""The reviewed one-off jobs a naapp's production service may run.

An app's ``ops/one-off-jobs.json`` maps each job name to its exact argv. An
argv element is either a fixed token or a typed slot, ``{"slot": NAME, "type":
TYPE}``, that the operator fills with a value of that type. Nothing else runs:
a job outside the file, a slot left empty or added, or a value of the wrong
type is refused before Render is called.

Render takes a job's command as one string. Fixed tokens and every slot type
are limited to characters a shell gives no meaning to, so the string the job
runs splits back into exactly the argv that was reviewed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from aweb_naapp.ops.render import OpsError

JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SLOT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FIXED_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
SLOT_TYPES = {
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
    "uuid": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
}


@dataclass(frozen=True)
class Slot:
    name: str
    type: str


Element = str | Slot


def _element(job: str, raw: object) -> Element:
    if isinstance(raw, str):
        if not FIXED_TOKEN_RE.fullmatch(raw):
            raise OpsError(f"one-off job {job!r} has a fixed token a shell would interpret: {raw!r}")
        return raw
    if isinstance(raw, dict) and set(raw) == {"slot", "type"}:
        name, kind = raw["slot"], raw["type"]
        if isinstance(name, str) and SLOT_NAME_RE.fullmatch(name) and kind in SLOT_TYPES:
            return Slot(name, kind)
    raise OpsError(
        f"one-off job {job!r} has an element that is neither a fixed token nor a typed slot "
        f"({sorted(SLOT_TYPES)}): {raw!r}"
    )


@dataclass(frozen=True)
class JobAllowlist:
    jobs: dict[str, tuple[Element, ...]]

    @classmethod
    def load(cls, path: Path) -> JobAllowlist:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpsError(f"invalid one-off job file: {path}") from exc
        if not isinstance(raw, dict) or not raw:
            raise OpsError("the one-off job file must map job names to argv lists")
        jobs: dict[str, tuple[Element, ...]] = {}
        for name, argv in raw.items():
            if not JOB_NAME_RE.fullmatch(name):
                raise OpsError(f"one-off job name {name!r} must be lowercase words joined by dashes")
            if not isinstance(argv, list) or not argv:
                raise OpsError(f"one-off job {name!r} must be a nonempty argv list")
            elements = tuple(_element(name, item) for item in argv)
            slots = [element.name for element in elements if isinstance(element, Slot)]
            if len(slots) != len(set(slots)):
                raise OpsError(f"one-off job {name!r} names a slot twice")
            jobs[name] = elements
        return cls(jobs)

    def argv(self, name: str, args: dict[str, str]) -> list[str]:
        """The listed job's argv with each slot filled by its typed value."""
        if name not in self.jobs:
            raise OpsError(f"{name!r} is not a listed job; the file lists {sorted(self.jobs)}")
        elements = self.jobs[name]
        slots = {element.name: element for element in elements if isinstance(element, Slot)}
        if set(args) != set(slots):
            raise OpsError(
                f"job {name!r} takes exactly the slots {sorted(slots)}, given {sorted(args)}"
            )
        for slot_name, value in args.items():
            kind = slots[slot_name].type
            if not SLOT_TYPES[kind].fullmatch(value):
                raise OpsError(f"slot {slot_name!r} must be a {kind}")
        return [element if isinstance(element, str) else args[element.name] for element in elements]


def parse_slot_args(text: str) -> dict[str, str]:
    """Whitespace-separated ``slot=value`` pairs, each slot given once."""
    args: dict[str, str] = {}
    for pair in text.split():
        name, separator, value = pair.partition("=")
        if not separator or not name or name in args:
            raise OpsError(f"job arguments must be distinct slot=value pairs, got {pair!r}")
        args[name] = value
    return args
