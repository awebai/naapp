"""Every lease and authority rule of the Tasks app, as pure functions.

Each function takes the facts it needs (the task, its lease and revision, its
descendants, the caller and the time) and returns the next state or a
:class:`Refusal`. Nothing here reads or writes storage or the clock: the store
reads the facts inside its transaction, after any lock wait, and applies the
verdict in that same transaction.

Scopes are not checked here. naapp-lib's ``authorize_request`` admits each
request against the installation's grant for the tool (``scope_denied``,
``authority_unavailable``) before any of these rules is asked.

A claim and a lock are leases: live while ``now < expires_at``, free once
expired or released. Every transition increments the lease's revision, which
is never reset, so a revision a caller presents names exactly one state.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum

from aweb_naapp.runtime.team_auth import Principal


class Code(StrEnum):
    """The refusal codes, as the app answers them."""

    # The lease is not in the state the caller presented, or it changed during the lock wait.
    LEASE_CHANGED = "lease_changed"
    CLAIMED_BY_OTHER = "claimed_by_other"
    LOCKED_BY_OTHER = "locked_by_other"
    # The holder's own tools act only on the caller's own live lease.
    NOT_HOLDER = "not_holder"
    # The override tools act on another's lease, never the caller's own.
    NOT_OVERRIDABLE = "not_overridable"
    TASK_CLOSED = "task_closed"
    TASK_NOT_CLOSED = "task_not_closed"
    HAS_OPEN_CHILDREN = "has_open_children"
    # Missing, or outside the caller's installation: the two are indistinguishable.
    NOT_FOUND = "not_found"
    DEPENDENCY_CYCLE = "dependency_cycle"
    PARENT_CYCLE = "parent_cycle"


@dataclass(frozen=True)
class Refusal:
    code: Code
    message: str


@dataclass(frozen=True)
class Holder:
    """Who holds a lease: one identity within one team."""

    team_id: str
    identity: str


def holder_of(principal: Principal) -> Holder:
    """The holder a verified request acts as.

    A did:aw the registry verified for this signer survives key rotation, so it
    names the holder; otherwise the signing did:key does. A grant-backed worker
    signs as its resident, so it holds as the resident.
    """
    if principal.did_aw_registry_verified and principal.did_aw:
        return Holder(team_id=principal.team_id, identity=principal.did_aw)
    return Holder(team_id=principal.team_id, identity=principal.did_key)


class LeaseKind(Enum):
    CLAIM = (timedelta(hours=24), Code.CLAIMED_BY_OTHER)
    LOCK = (timedelta(minutes=30), Code.LOCKED_BY_OTHER)

    def __init__(self, lifetime: timedelta, held_by_other: Code) -> None:
        self.lifetime = lifetime
        self.held_by_other = held_by_other


@dataclass(frozen=True)
class Lease:
    """A claim's or lock's current state. ``holder`` is None once released."""

    holder: Holder | None
    revision: int
    expires_at: datetime | None

    def live_holder(self, now: datetime) -> Holder | None:
        if self.holder is None or self.expires_at is None or now >= self.expires_at:
            return None
        return self.holder


# The state of a task or resource that has never been claimed or locked.
NO_LEASE = Lease(holder=None, revision=0, expires_at=None)


class TaskStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    CLOSED = "closed"


@dataclass(frozen=True)
class Task:
    number: int
    installation_id: str
    status: TaskStatus
    claim: Lease


@dataclass(frozen=True)
class SeenClaim:
    """A claim as the store read it before its lock wait: its revision, and its
    live holder at that read (None when it was free)."""

    revision: int
    holder: Holder | None


@dataclass(frozen=True)
class ClaimEnded:
    """A claim a close released, with the holder it was taken from."""

    number: int
    previous: Holder
    lease: Lease


@dataclass(frozen=True)
class Closing:
    """What a close changes: the tasks it closes and the claims it ends."""

    closed: tuple[int, ...]
    ended_claims: tuple[ClaimEnded, ...]


NOTHING_CLOSED = Closing(closed=(), ended_claims=())

_MESSAGES = {
    Code.LEASE_CHANGED: "The lease changed since it was read; read it again",
    Code.CLAIMED_BY_OTHER: "Someone else holds a live claim on this task",
    Code.LOCKED_BY_OTHER: "Someone else holds a live lock on this resource",
    Code.NOT_HOLDER: "The caller does not hold this lease",
    Code.NOT_OVERRIDABLE: "The caller holds this lease; use the holder's own tools",
    Code.TASK_CLOSED: "The task is closed",
    Code.TASK_NOT_CLOSED: "The task is not closed",
    Code.HAS_OPEN_CHILDREN: "The task has open children; close it with its children to close them too",
    Code.NOT_FOUND: "No such task",
    Code.DEPENDENCY_CYCLE: "The dependency would make a task wait on itself",
    Code.PARENT_CYCLE: "The parent would make a task its own ancestor",
}


def _refuse(code: Code) -> Refusal:
    return Refusal(code=code, message=_MESSAGES[code])


def _outside(installation_id: str, *tasks: Task | None) -> Refusal | None:
    """Every task an operation touches must exist in the caller's installation."""
    if any(t is None or t.installation_id != installation_id for t in tasks):
        return _refuse(Code.NOT_FOUND)
    return None


def _granted(lease: Lease, holder: Holder | None, now: datetime, kind: LeaseKind) -> Lease:
    expires_at = None if holder is None else now + kind.lifetime
    return Lease(holder=holder, revision=lease.revision + 1, expires_at=expires_at)


# --- Leases ----------------------------------------------------------------


def _acquire(
    kind: LeaseKind, lease: Lease, caller: Holder, now: datetime, presented_revision: int | None
) -> Lease | Refusal:
    # The store presents the revision it read before its lock wait; a transition in
    # between means the caller acted on a state that no longer exists.
    if presented_revision is not None and lease.revision != presented_revision:
        return _refuse(Code.LEASE_CHANGED)
    live = lease.live_holder(now)
    if live is not None and live != caller:
        return _refuse(kind.held_by_other)
    # Free, or already the caller's: either way the caller holds it afresh.
    return _granted(lease, caller, now, kind)


def claim(
    installation_id: str,
    task: Task | None,
    caller: Holder,
    now: datetime,
    *,
    presented_revision: int | None = None,
) -> Lease | Refusal:
    if refused := _outside(installation_id, task):
        return refused
    assert task is not None
    if task.status is TaskStatus.CLOSED:
        return _refuse(Code.TASK_CLOSED)
    return _acquire(LeaseKind.CLAIM, task.claim, caller, now, presented_revision)


def lock(
    lease: Lease, caller: Holder, now: datetime, *, presented_revision: int | None = None
) -> Lease | Refusal:
    return _acquire(LeaseKind.LOCK, lease, caller, now, presented_revision)


def _own(lease: Lease, caller: Holder, now: datetime, presented_revision: int | None) -> Refusal | None:
    """The holder's own tools act only on the caller's live lease.

    A presented revision says which live lease the caller means; any difference
    in revision, holder or liveness is ``lease_changed``. Without one, the
    caller means whatever it holds now.
    """
    is_own = lease.live_holder(now) == caller
    if presented_revision is not None:
        return None if is_own and lease.revision == presented_revision else _refuse(Code.LEASE_CHANGED)
    return None if is_own else _refuse(Code.NOT_HOLDER)


def renew(
    kind: LeaseKind, lease: Lease, caller: Holder, now: datetime, *, presented_revision: int | None = None
) -> Lease | Refusal:
    if refused := _own(lease, caller, now, presented_revision):
        return refused
    return _granted(lease, caller, now, kind)


def release(
    kind: LeaseKind, lease: Lease, caller: Holder, now: datetime, *, presented_revision: int | None = None
) -> Lease | Refusal:
    if refused := _own(lease, caller, now, presented_revision):
        return refused
    return _granted(lease, None, now, kind)


def renew_on_write(claim: Lease, caller: Holder, now: datetime) -> Lease:
    """A claim after a write to its task: the holder's write renews it, anyone else's leaves it."""
    if claim.live_holder(now) != caller:
        return claim
    return _granted(claim, caller, now, LeaseKind.CLAIM)


def release_all[K](leases: Mapping[K, Lease], caller: Holder, now: datetime) -> dict[K, Lease]:
    """The caller's live leases, each released. Everyone else's are left out."""
    return {
        key: Lease(holder=None, revision=lease.revision + 1, expires_at=None)
        for key, lease in leases.items()
        if lease.live_holder(now) == caller
    }


def _override(lease: Lease, caller: Holder, now: datetime, presented_revision: int) -> Refusal | None:
    """The override tools act on another's live lease, at the revision the caller saw."""
    live = lease.live_holder(now)
    if live is None or lease.revision != presented_revision:
        return _refuse(Code.LEASE_CHANGED)
    if live == caller:
        return _refuse(Code.NOT_OVERRIDABLE)
    return None


def takeover(
    installation_id: str, task: Task | None, caller: Holder, now: datetime, *, presented_revision: int
) -> Lease | Refusal:
    """Another's live claim, transferred to the caller."""
    if refused := _outside(installation_id, task):
        return refused
    assert task is not None
    if task.status is TaskStatus.CLOSED:
        return _refuse(Code.TASK_CLOSED)
    if refused := _override(task.claim, caller, now, presented_revision):
        return refused
    return _granted(task.claim, caller, now, LeaseKind.CLAIM)


def break_lock(lease: Lease, caller: Holder, now: datetime, *, presented_revision: int) -> Lease | Refusal:
    """Another's live lock, freed. It does not pass to the caller."""
    if refused := _override(lease, caller, now, presented_revision):
        return refused
    return _granted(lease, None, now, LeaseKind.LOCK)


# --- Closing and reopening -------------------------------------------------


def _close(
    installation_id: str,
    task: Task | None,
    descendants: Sequence[Task],
    caller: Holder,
    now: datetime,
    *,
    with_children: bool,
    overrides: bool,
) -> Closing | Refusal:
    """Every affected row is checked before any is changed."""
    if refused := _outside(installation_id, task, *descendants):
        return refused
    assert task is not None
    if task.status is TaskStatus.CLOSED:
        return _refuse(Code.TASK_CLOSED)
    # Closed descendants are neither in the way nor closed again.
    open_descendants = [d for d in descendants if d.status is not TaskStatus.CLOSED]
    if open_descendants and not with_children:
        return _refuse(Code.HAS_OPEN_CHILDREN)
    closing = [task, *open_descendants]
    ended: list[ClaimEnded] = []
    for t in closing:
        live = t.claim.live_holder(now)
        if live is None:
            continue
        # Only the override ends someone else's work.
        if live != caller and not overrides:
            return _refuse(Code.CLAIMED_BY_OTHER)
        ended.append(ClaimEnded(
            number=t.number, previous=live, lease=_granted(t.claim, None, now, LeaseKind.CLAIM)
        ))
    return Closing(closed=tuple(t.number for t in closing), ended_claims=tuple(ended))


def close(
    installation_id: str,
    task: Task | None,
    descendants: Sequence[Task],
    caller: Holder,
    now: datetime,
    *,
    with_children: bool = False,
) -> Closing | Refusal:
    """The ordinary close. ``descendants`` is every descendant, not only direct children."""
    return _close(
        installation_id, task, descendants, caller, now, with_children=with_children, overrides=False
    )


def close_claimed(
    installation_id: str,
    task: Task | None,
    descendants: Sequence[Task],
    caller: Holder,
    now: datetime,
    *,
    presented_revision: int | None,
    seen_claims: Mapping[int, SeenClaim],
    with_children: bool = False,
) -> Closing | Refusal:
    """The override close: it ends every live claim it closes, recording both holders.

    ``presented_revision`` comes from the caller: the revision of the task's claim
    it saw live, or None when it saw the task unheld. A presented claim must still
    be live at that revision; a task seen unheld must still be free or the
    caller's own. ``seen_claims`` comes from the store: each descendant's claim as
    read before its lock wait. A descendant whose revision or live holder differs,
    or that was not there, is ``lease_changed``. The installation boundary and
    the task's status are judged before any lease.
    """
    if refused := _outside(installation_id, task, *descendants):
        return refused
    assert task is not None
    if task.status is TaskStatus.CLOSED:
        return _refuse(Code.TASK_CLOSED)
    live = task.claim.live_holder(now)
    if presented_revision is None:
        if live not in (None, caller):
            return _refuse(Code.LEASE_CHANGED)
    elif live is None or task.claim.revision != presented_revision:
        return _refuse(Code.LEASE_CHANGED)
    if with_children:
        for d in descendants:
            seen = seen_claims.get(d.number)
            if seen != SeenClaim(revision=d.claim.revision, holder=d.claim.live_holder(now)):
                return _refuse(Code.LEASE_CHANGED)
    return _close(
        installation_id, task, descendants, caller, now, with_children=with_children, overrides=True
    )


def update_status(
    installation_id: str,
    task: Task | None,
    status: TaskStatus,
    descendants: Sequence[Task],
    caller: Holder,
    now: datetime,
) -> Closing | Refusal:
    """An update that sets a status. Only a change to closed can end someone's
    work, so only that is judged, as an ordinary close without its children."""
    if refused := _outside(installation_id, task):
        return refused
    assert task is not None
    if status is TaskStatus.CLOSED and task.status is not TaskStatus.CLOSED:
        return close(installation_id, task, descendants, caller, now)
    return NOTHING_CLOSED


def reopen(installation_id: str, task: Task | None) -> Refusal | None:
    """A closed task, reopened. Its parent's status does not matter."""
    if refused := _outside(installation_id, task):
        return refused
    assert task is not None
    if task.status is not TaskStatus.CLOSED:
        return _refuse(Code.TASK_NOT_CLOSED)
    return None


# --- Dependencies and parents ----------------------------------------------


def inside(installation_id: str, task: Task | None) -> Refusal | None:
    """The boundary alone, for operations no other rule judges: creating under a
    parent (a new task has no descendants, and the parent's status and claim do
    not matter), editing fields, commenting, and reading one task."""
    return _outside(installation_id, task)


def _reaches(start: int, goal: int, edges: Mapping[int, Collection[int]]) -> bool:
    seen: set[int] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        pending.extend(edges.get(node, ()))
    return False


def add_dependency(
    installation_id: str,
    task: Task | None,
    target: Task | None,
    waits_on: Mapping[int, Collection[int]],
) -> Refusal | None:
    """``task`` waits on ``target``. ``waits_on`` is the installation's existing edges."""
    if refused := _outside(installation_id, task, target):
        return refused
    assert task is not None and target is not None
    if _reaches(target.number, task.number, waits_on):
        return _refuse(Code.DEPENDENCY_CYCLE)
    return None


def remove_dependency(installation_id: str, task: Task | None, target: Task | None) -> Refusal | None:
    return _outside(installation_id, task, target)


def set_parent(
    installation_id: str,
    task: Task | None,
    parent: Task | None,
    parent_of: Mapping[int, int | None],
) -> Refusal | None:
    """``task`` moves under ``parent``. ``parent_of`` is the installation's existing parents."""
    if refused := _outside(installation_id, task, parent):
        return refused
    assert task is not None and parent is not None
    ancestors = {n: [p] for n, p in parent_of.items() if p is not None}
    if _reaches(parent.number, task.number, ancestors):
        return _refuse(Code.PARENT_CYCLE)
    return None
