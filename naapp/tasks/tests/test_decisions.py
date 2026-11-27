"""Table tests for every lease and authority rule (spec sections 3 and 4).

Each row names the facts, the caller and the time, and the verdict: the next
state, or the refusal code. Codes are asserted, never wording.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aweb_naapp.runtime.team_auth import Principal

from aweb_tasks.decisions import (
    NO_LEASE,
    ClaimEnded,
    Closing,
    Code,
    Holder,
    Lease,
    LeaseKind,
    Refusal,
    Task,
    TaskStatus,
    add_dependency,
    break_lock,
    claim,
    close,
    close_claimed,
    holder_of,
    lock,
    release,
    release_all,
    remove_dependency,
    renew,
    renew_on_write,
    reopen,
    set_parent,
    takeover,
    update_status,
)

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
CLAIM_LIFE = timedelta(hours=24)
LOCK_LIFE = timedelta(minutes=30)
INSTALLATION = "inst-1"
NEIGHBOUR_INSTALLATION = "inst-2"

ALICE = Holder(team_id="team-a", identity="did:aw:alice")
BOB = Holder(team_id="team-a", identity="did:aw:bob")
# Bob's identity in a neighbouring team is a different holder.
BOB_NEIGHBOUR = Holder(team_id="team-b", identity="did:aw:bob")


def held(holder: Holder, revision: int = 1, *, expires_at: datetime = T0 + timedelta(hours=1)) -> Lease:
    return Lease(holder=holder, revision=revision, expires_at=expires_at)


def expired(holder: Holder, revision: int = 1) -> Lease:
    return Lease(holder=holder, revision=revision, expires_at=T0)


def task(
    number: int = 1,
    *,
    status: TaskStatus = TaskStatus.OPEN,
    claim: Lease = NO_LEASE,
    installation_id: str = INSTALLATION,
) -> Task:
    return Task(number=number, installation_id=installation_id, status=status, claim=claim)


def code_of(verdict: object) -> Code | None:
    return verdict.code if isinstance(verdict, Refusal) else None


# --- Holder identity -------------------------------------------------------


def principal(**overrides: object) -> Principal:
    fields: dict[str, object] = dict(
        team_id="team-a",
        did_key="did:key:z6Mk-alice",
        did_aw="did:aw:alice",
        address="example.com/alice",
        alias="alice",
        certificate_id="cert-1",
        team_did_key="did:key:z6Mk-team",
        did_aw_registry_verified=True,
    )
    fields.update(overrides)
    return Principal(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A registry-verified did:aw survives key rotation, so it names the holder.
        ({}, Holder("team-a", "did:aw:alice")),
        ({"did_key": "did:key:z6Mk-rotated"}, Holder("team-a", "did:aw:alice")),
        # An unverified did:aw is not authority: the signing key names the holder.
        ({"did_aw_registry_verified": False}, Holder("team-a", "did:key:z6Mk-alice")),
        ({"did_aw": None, "did_aw_registry_verified": False}, Holder("team-a", "did:key:z6Mk-alice")),
        # The same identity in another team is another holder.
        ({"team_id": "team-b"}, Holder("team-b", "did:aw:alice")),
        # The alias and certificate are recorded, never compared.
        ({"alias": "renamed", "certificate_id": "cert-2"}, Holder("team-a", "did:aw:alice")),
    ],
    ids=["did-aw", "did-aw-after-rotation", "unverified-did-aw", "no-did-aw", "other-team", "alias-cert-ignored"],
)
def test_holder_of(overrides: dict[str, object], expected: Holder) -> None:
    assert holder_of(principal(**overrides)) == expected


# --- Reading liveness ------------------------------------------------------


@pytest.mark.parametrize(
    ("lease", "now", "live"),
    [
        (NO_LEASE, T0, None),
        (held(ALICE, expires_at=T0 + timedelta(seconds=1)), T0, ALICE),
        # Live while now < expires_at; at expires_at it is free.
        (held(ALICE, expires_at=T0), T0, None),
        (held(ALICE, expires_at=T0), T0 + timedelta(seconds=1), None),
        (Lease(holder=None, revision=3, expires_at=None), T0, None),
    ],
    ids=["no-lease", "before-expiry", "at-expiry", "after-expiry", "released"],
)
def test_live_holder(lease: Lease, now: datetime, live: Holder | None) -> None:
    assert lease.live_holder(now) == live


# --- Claim and lock: acquiring ---------------------------------------------


@pytest.mark.parametrize(
    ("lease", "caller", "presented", "expected"),
    [
        (NO_LEASE, ALICE, None, Lease(ALICE, 1, T0 + CLAIM_LIFE)),
        (expired(BOB, 4), ALICE, None, Lease(ALICE, 5, T0 + CLAIM_LIFE)),
        (expired(BOB, 4), ALICE, 4, Lease(ALICE, 5, T0 + CLAIM_LIFE)),
        # Holding it already renews it.
        (held(ALICE, 2), ALICE, None, Lease(ALICE, 3, T0 + CLAIM_LIFE)),
        # The same resident reclaiming after expiry.
        (expired(ALICE, 2), ALICE, None, Lease(ALICE, 3, T0 + CLAIM_LIFE)),
        (held(BOB, 2), ALICE, None, Code.CLAIMED_BY_OTHER),
        (held(BOB, 2), ALICE, 2, Code.CLAIMED_BY_OTHER),
        (held(BOB_NEIGHBOUR, 2), BOB, None, Code.CLAIMED_BY_OTHER),
        # Seen free, then taken during the lock wait.
        (held(BOB, 5), ALICE, 4, Code.LEASE_CHANGED),
        (expired(BOB, 5), ALICE, 4, Code.LEASE_CHANGED),
    ],
    ids=[
        "free", "expired-other", "expired-other-presented", "own-live-renews", "own-expired-reclaims",
        "live-other", "live-other-presented", "same-did-other-team", "taken-during-wait",
        "changed-during-wait",
    ],
)
def test_claim(lease: Lease, caller: Holder, presented: int | None, expected: Lease | Code) -> None:
    verdict = claim(INSTALLATION, task(claim=lease), caller, T0, presented_revision=presented)
    assert (code_of(verdict) or verdict) == expected


@pytest.mark.parametrize(
    ("status", "installation_id", "expected"),
    [
        (TaskStatus.OPEN, INSTALLATION, None),
        # A stored-blocked task can be claimed: blocked is not closed.
        (TaskStatus.BLOCKED, INSTALLATION, None),
        (TaskStatus.CLOSED, INSTALLATION, Code.TASK_CLOSED),
        (TaskStatus.OPEN, NEIGHBOUR_INSTALLATION, Code.NOT_FOUND),
    ],
    ids=["open", "blocked", "closed", "other-installation"],
)
def test_claim_task_facts(status: TaskStatus, installation_id: str, expected: Code | None) -> None:
    verdict = claim(INSTALLATION, task(status=status, installation_id=installation_id), ALICE, T0)
    assert code_of(verdict) == expected


def test_claim_missing_task() -> None:
    assert code_of(claim(INSTALLATION, None, ALICE, T0)) == Code.NOT_FOUND


@pytest.mark.parametrize(
    ("lease", "caller", "presented", "expected"),
    [
        (NO_LEASE, ALICE, None, Lease(ALICE, 1, T0 + LOCK_LIFE)),
        (expired(BOB, 4), ALICE, None, Lease(ALICE, 5, T0 + LOCK_LIFE)),
        (held(ALICE, 2), ALICE, None, Lease(ALICE, 3, T0 + LOCK_LIFE)),
        (expired(ALICE, 2), ALICE, None, Lease(ALICE, 3, T0 + LOCK_LIFE)),
        (held(BOB, 2), ALICE, None, Code.LOCKED_BY_OTHER),
        (held(BOB, 5), ALICE, 4, Code.LEASE_CHANGED),
    ],
    ids=["free", "expired-other", "own-live-renews", "own-expired-relocks", "live-other", "taken-during-wait"],
)
def test_lock(lease: Lease, caller: Holder, presented: int | None, expected: Lease | Code) -> None:
    verdict = lock(lease, caller, T0, presented_revision=presented)
    assert (code_of(verdict) or verdict) == expected


# --- Claim and lock: the holder's own renew and release --------------------


@pytest.mark.parametrize("kind", list(LeaseKind), ids=lambda k: k.name.lower())
@pytest.mark.parametrize(
    ("lease", "caller", "presented", "expected_revision", "code"),
    [
        (held(ALICE, 2), ALICE, None, 3, None),
        (held(ALICE, 2), ALICE, 2, 3, None),
        # Not the caller's live lease, with no revision presented.
        (NO_LEASE, ALICE, None, None, Code.NOT_HOLDER),
        (held(BOB, 2), ALICE, None, None, Code.NOT_HOLDER),
        (expired(ALICE, 2), ALICE, None, None, Code.NOT_HOLDER),
        (held(BOB_NEIGHBOUR, 2), BOB, None, None, Code.NOT_HOLDER),
        # A presented revision that no longer describes the caller's live lease.
        (held(ALICE, 3), ALICE, 2, None, Code.LEASE_CHANGED),
        (expired(ALICE, 2), ALICE, 2, None, Code.LEASE_CHANGED),
        (held(BOB, 3), ALICE, 2, None, Code.LEASE_CHANGED),
    ],
    ids=[
        "own", "own-presented", "none", "other", "own-expired", "same-did-other-team",
        "stale-revision", "expired-presented", "taken-over-presented",
    ],
)
class TestHolderTransitions:
    def test_renew(
        self, kind: LeaseKind, lease: Lease, caller: Holder, presented: int | None,
        expected_revision: int | None, code: Code | None,
    ) -> None:
        verdict = renew(kind, lease, caller, T0, presented_revision=presented)
        if code is None:
            assert verdict == Lease(caller, expected_revision, T0 + kind.lifetime)  # type: ignore[arg-type]
        else:
            assert code_of(verdict) == code

    def test_release(
        self, kind: LeaseKind, lease: Lease, caller: Holder, presented: int | None,
        expected_revision: int | None, code: Code | None,
    ) -> None:
        verdict = release(kind, lease, caller, T0, presented_revision=presented)
        if code is None:
            assert verdict == Lease(None, expected_revision, None)  # type: ignore[arg-type]
        else:
            assert code_of(verdict) == code


@pytest.mark.parametrize(
    ("lease", "caller", "expected"),
    [
        # Any write the holder makes to its task renews the claim.
        (held(ALICE, 2), ALICE, Lease(ALICE, 3, T0 + CLAIM_LIFE)),
        # Anyone else's write leaves the claim as it is.
        (held(BOB, 2), ALICE, held(BOB, 2)),
        (expired(ALICE, 2), ALICE, expired(ALICE, 2)),
        (NO_LEASE, ALICE, NO_LEASE),
    ],
    ids=["holder", "other", "expired-own", "no-claim"],
)
def test_renew_on_write(lease: Lease, caller: Holder, expected: Lease) -> None:
    assert renew_on_write(lease, caller, T0) == expected


def test_release_all_takes_only_the_callers_live_leases() -> None:
    leases = {
        ("claim", 1): held(ALICE, 2),
        ("claim", 2): held(BOB, 2),
        ("claim", 3): expired(ALICE, 2),
        ("lock", "repo/main"): held(ALICE, 7),
        ("lock", "repo/dev"): held(BOB_NEIGHBOUR, 1),
    }
    assert release_all(leases, ALICE, T0) == {
        ("claim", 1): Lease(None, 3, None),
        ("lock", "repo/main"): Lease(None, 8, None),
    }


# --- Expiry races (section 3's named tests) --------------------------------


@pytest.mark.parametrize("kind", list(LeaseKind), ids=lambda k: k.name.lower())
def test_expiry_then_new_holder_beats_late_renewal_and_release(kind: LeaseKind) -> None:
    acquire = (lambda lease, caller, now: claim(INSTALLATION, task(claim=lease), caller, now)) \
        if kind is LeaseKind.CLAIM else (lambda lease, caller, now: lock(lease, caller, now))
    alices = acquire(NO_LEASE, ALICE, T0)
    assert isinstance(alices, Lease)
    after_expiry = T0 + kind.lifetime
    bobs = acquire(alices, BOB, after_expiry)
    assert bobs == Lease(BOB, alices.revision + 1, after_expiry + kind.lifetime)
    assert isinstance(bobs, Lease)
    # Alice, late, still presents her revision.
    late = after_expiry + timedelta(seconds=1)
    assert code_of(renew(kind, bobs, ALICE, late, presented_revision=alices.revision)) == Code.LEASE_CHANGED
    assert code_of(release(kind, bobs, ALICE, late, presented_revision=alices.revision)) == Code.LEASE_CHANGED
    # Bob's lease is untouched by her attempts and he can renew it.
    assert renew(kind, bobs, BOB, late, presented_revision=bobs.revision) == Lease(
        BOB, bobs.revision + 1, late + kind.lifetime
    )


@pytest.mark.parametrize("kind", list(LeaseKind), ids=lambda k: k.name.lower())
def test_same_resident_reclaims_after_expiry(kind: LeaseKind) -> None:
    acquire = (lambda lease, caller, now: claim(INSTALLATION, task(claim=lease), caller, now)) \
        if kind is LeaseKind.CLAIM else (lambda lease, caller, now: lock(lease, caller, now))
    first = acquire(NO_LEASE, ALICE, T0)
    assert isinstance(first, Lease)
    after_expiry = T0 + kind.lifetime
    assert code_of(renew(kind, first, ALICE, after_expiry)) == Code.NOT_HOLDER
    again = acquire(first, ALICE, after_expiry)
    assert again == Lease(ALICE, first.revision + 1, after_expiry + kind.lifetime)


# --- Override tools: takeover and break ------------------------------------


@pytest.mark.parametrize(
    ("lease", "caller", "presented", "expected"),
    [
        # A member taking over another's live claim: it transfers.
        (held(BOB, 2), ALICE, 2, Lease(ALICE, 3, T0 + CLAIM_LIFE)),
        # The holder itself.
        (held(ALICE, 2), ALICE, 2, Code.NOT_OVERRIDABLE),
        # A member of a neighbouring team never sees this claim as its own.
        (held(BOB, 2), BOB_NEIGHBOUR, 2, Lease(BOB_NEIGHBOUR, 3, T0 + CLAIM_LIFE)),
        # A free lease has nothing to take over: the caller presented a live one.
        (NO_LEASE, ALICE, 0, Code.LEASE_CHANGED),
        (expired(BOB, 2), ALICE, 2, Code.LEASE_CHANGED),
        (held(BOB, 3), ALICE, 2, Code.LEASE_CHANGED),
    ],
    ids=["other", "holder", "neighbour-identity", "free", "expired", "stale-revision"],
)
def test_takeover(lease: Lease, caller: Holder, presented: int, expected: Lease | Code) -> None:
    verdict = takeover(INSTALLATION, task(claim=lease), caller, T0, presented_revision=presented)
    assert (code_of(verdict) or verdict) == expected


@pytest.mark.parametrize(
    ("status", "installation_id", "expected"),
    [
        (TaskStatus.CLOSED, INSTALLATION, Code.TASK_CLOSED),
        # A neighbouring team's installation is invisible to the caller.
        (TaskStatus.OPEN, NEIGHBOUR_INSTALLATION, Code.NOT_FOUND),
    ],
    ids=["closed", "other-installation"],
)
def test_takeover_task_facts(status: TaskStatus, installation_id: str, expected: Code) -> None:
    subject = task(status=status, claim=held(BOB, 2), installation_id=installation_id)
    assert code_of(takeover(INSTALLATION, subject, ALICE, T0, presented_revision=2)) == expected


@pytest.mark.parametrize(
    ("lease", "caller", "presented", "expected"),
    [
        # Break frees the lock; it does not pass to the breaker.
        (held(BOB, 2), ALICE, 2, Lease(None, 3, None)),
        (held(ALICE, 2), ALICE, 2, Code.NOT_OVERRIDABLE),
        (held(BOB, 2), BOB_NEIGHBOUR, 2, Lease(None, 3, None)),
        (NO_LEASE, ALICE, 0, Code.LEASE_CHANGED),
        (expired(BOB, 2), ALICE, 2, Code.LEASE_CHANGED),
        (held(BOB, 3), ALICE, 2, Code.LEASE_CHANGED),
    ],
    ids=["other", "holder", "neighbour-identity", "free", "expired", "stale-revision"],
)
def test_break_lock(lease: Lease, caller: Holder, presented: int, expected: Lease | Code) -> None:
    verdict = break_lock(lease, caller, T0, presented_revision=presented)
    assert (code_of(verdict) or verdict) == expected


# --- Closing ---------------------------------------------------------------


def ended(number: int, previous: Lease) -> ClaimEnded:
    assert previous.holder is not None
    return ClaimEnded(
        number=number, previous=previous.holder, lease=Lease(None, previous.revision + 1, None)
    )


@pytest.mark.parametrize(
    ("subject", "descendants", "with_children", "expected"),
    [
        (task(1), [], False, Closing(closed=(1,), ended_claims=())),
        # Closing a task with the caller's own claim releases it.
        (task(1, claim=held(ALICE, 2)), [], False, Closing((1,), (ended(1, held(ALICE, 2)),))),
        # An expired claim is free: nothing to end.
        (task(1, claim=expired(BOB, 2)), [], False, Closing((1,), ())),
        (task(1, claim=held(BOB, 2)), [], False, Code.CLAIMED_BY_OTHER),
        (task(1, status=TaskStatus.CLOSED), [], False, Code.TASK_CLOSED),
        (task(1, status=TaskStatus.BLOCKED), [], False, Closing((1,), ())),
        (task(1), [task(2)], False, Code.HAS_OPEN_CHILDREN),
        (task(1), [task(2, status=TaskStatus.BLOCKED)], False, Code.HAS_OPEN_CHILDREN),
        # Closed descendants neither block nor get closed again.
        (task(1), [task(2, status=TaskStatus.CLOSED)], False, Closing((1,), ())),
        (task(1), [task(2), task(3)], True, Closing((1, 2, 3), ())),
        (task(1), [task(2, status=TaskStatus.CLOSED), task(3)], True, Closing((1, 3), ())),
        # Every descendant, not only direct children: the store passes them all.
        (task(1), [task(2), task(4, claim=held(BOB, 1))], True, Code.CLAIMED_BY_OTHER),
        (task(1), [task(2, claim=held(ALICE, 5))], True, Closing((1, 2), (ended(2, held(ALICE, 5)),))),
        # A closed descendant's leftover claim is not this close's to judge.
        (task(1), [task(2, status=TaskStatus.CLOSED, claim=held(BOB, 1))], True, Closing((1,), ())),
        (task(1, claim=held(BOB, 2)), [task(2)], True, Code.CLAIMED_BY_OTHER),
        (task(1, installation_id=NEIGHBOUR_INSTALLATION), [], False, Code.NOT_FOUND),
        (task(1), [task(2, installation_id=NEIGHBOUR_INSTALLATION)], True, Code.NOT_FOUND),
        (task(1), [task(2, installation_id=NEIGHBOUR_INSTALLATION)], False, Code.NOT_FOUND),
        (None, [], False, Code.NOT_FOUND),
    ],
    ids=[
        "unclaimed", "own-claim-released", "expired-claim", "claimed-by-other", "already-closed",
        "blocked", "open-child", "blocked-child", "closed-child", "with-children",
        "with-children-skips-closed", "descendant-claimed-by-other", "descendant-own-claim",
        "closed-descendant-claim", "root-claimed-with-children", "other-installation",
        "descendant-other-installation", "descendant-other-installation-no-children", "missing",
    ],
)
def test_close(
    subject: Task | None, descendants: list[Task], with_children: bool, expected: Closing | Code
) -> None:
    verdict = close(INSTALLATION, subject, descendants, ALICE, T0, with_children=with_children)
    assert (code_of(verdict) or verdict) == expected


@pytest.mark.parametrize(
    ("subject", "presented", "descendants", "seen", "with_children", "expected"),
    [
        # The override: closing a task another holds ends that claim, naming both holders.
        (task(1, claim=held(BOB, 2)), 2, [], {}, False, Closing((1,), (ended(1, held(BOB, 2)),))),
        # Unheld or own: the same as close.
        (task(1), None, [], {}, False, Closing((1,), ())),
        (task(1, claim=held(ALICE, 2)), 2, [], {}, False, Closing((1,), (ended(1, held(ALICE, 2)),))),
        (task(1, claim=expired(BOB, 2)), 2, [], {}, False, Closing((1,), ())),
        # The root's claim changed since the caller saw it.
        (task(1, claim=held(BOB, 3)), 2, [], {}, False, Code.LEASE_CHANGED),
        (task(1, claim=held(BOB, 1)), None, [], {}, False, Code.LEASE_CHANGED),
        (task(1, status=TaskStatus.CLOSED), None, [], {}, False, Code.TASK_CLOSED),
        (task(1), None, [task(2)], {2: 0}, False, Code.HAS_OPEN_CHILDREN),
        (
            task(1, claim=held(BOB, 2)), 2, [task(2, claim=held(BOB_NEIGHBOUR, 4)), task(3)],
            {2: 4, 3: 0}, True,
            Closing((1, 2, 3), (ended(1, held(BOB, 2)), ended(2, held(BOB_NEIGHBOUR, 4)))),
        ),
        # A descendant's claim moved during the lock wait.
        (task(1), None, [task(2, claim=held(BOB, 5))], {2: 4}, True, Code.LEASE_CHANGED),
        # A descendant that appeared during the lock wait.
        (task(1), None, [task(2)], {}, True, Code.LEASE_CHANGED),
        (task(1), None, [task(2, status=TaskStatus.CLOSED)], {2: 0}, True, Closing((1,), ())),
        (task(1, installation_id=NEIGHBOUR_INSTALLATION), None, [], {}, False, Code.NOT_FOUND),
        (task(1), None, [task(2, installation_id=NEIGHBOUR_INSTALLATION)], {2: 0}, True, Code.NOT_FOUND),
        (None, None, [], {}, False, Code.NOT_FOUND),
    ],
    ids=[
        "other-holder", "unheld", "own", "expired", "root-changed", "root-claimed-during-wait",
        "already-closed", "open-child", "with-children-overrides-all", "descendant-changed",
        "descendant-appeared", "closed-descendant-skipped", "other-installation",
        "descendant-other-installation", "missing",
    ],
)
def test_close_claimed(
    subject: Task | None, presented: int | None, descendants: list[Task], seen: dict[int, int],
    with_children: bool, expected: Closing | Code,
) -> None:
    verdict = close_claimed(
        INSTALLATION, subject, descendants, ALICE, T0,
        presented_revision=presented, seen_revisions=seen, with_children=with_children,
    )
    assert (code_of(verdict) or verdict) == expected


@pytest.mark.parametrize(
    ("subject", "status", "descendants", "expected"),
    [
        (task(1), TaskStatus.CLOSED, [], Closing((1,), ())),
        (task(1, claim=held(BOB, 2)), TaskStatus.CLOSED, [], Code.CLAIMED_BY_OTHER),
        (task(1, claim=held(ALICE, 2)), TaskStatus.CLOSED, [], Closing((1,), (ended(1, held(ALICE, 2)),))),
        (task(1), TaskStatus.CLOSED, [task(2)], Code.HAS_OPEN_CHILDREN),
        # Only a change to closed ends someone's work: open and blocked stay open to all.
        (task(1, claim=held(BOB, 2)), TaskStatus.BLOCKED, [], Closing((), ())),
        (task(1, status=TaskStatus.BLOCKED, claim=held(BOB, 2)), TaskStatus.OPEN, [], Closing((), ())),
        # Setting the status it already has changes nothing, so it refuses nothing.
        (task(1, status=TaskStatus.CLOSED), TaskStatus.CLOSED, [], Closing((), ())),
        # An update out of closed is a reopen, which has no claim to protect.
        (task(1, status=TaskStatus.CLOSED), TaskStatus.OPEN, [], Closing((), ())),
        (task(1, installation_id=NEIGHBOUR_INSTALLATION), TaskStatus.BLOCKED, [], Code.NOT_FOUND),
        (None, TaskStatus.OPEN, [], Code.NOT_FOUND),
    ],
    ids=[
        "close", "close-claimed-by-other", "close-own-claim", "close-open-child",
        "block-claimed-by-other", "unblock-claimed-by-other", "same-status", "reopen",
        "other-installation", "missing",
    ],
)
def test_update_status(
    subject: Task | None, status: TaskStatus, descendants: list[Task], expected: Closing | Code
) -> None:
    verdict = update_status(INSTALLATION, subject, status, descendants, ALICE, T0)
    assert (code_of(verdict) or verdict) == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        (task(1, status=TaskStatus.CLOSED), None),
        (task(1, status=TaskStatus.OPEN), Code.TASK_NOT_CLOSED),
        (task(1, status=TaskStatus.BLOCKED), Code.TASK_NOT_CLOSED),
        (task(1, status=TaskStatus.CLOSED, installation_id=NEIGHBOUR_INSTALLATION), Code.NOT_FOUND),
        (None, Code.NOT_FOUND),
    ],
    ids=["closed", "open", "blocked", "other-installation", "missing"],
)
def test_reopen(subject: Task | None, expected: Code | None) -> None:
    # A closed parent does not matter: reopening a child is allowed.
    assert code_of(reopen(INSTALLATION, subject)) == expected


# --- Dependencies and parents ----------------------------------------------


@pytest.mark.parametrize(
    ("number", "target", "waits_on", "expected"),
    [
        (1, 2, {}, None),
        (1, 1, {}, Code.DEPENDENCY_CYCLE),
        (1, 2, {2: {1}}, Code.DEPENDENCY_CYCLE),
        (1, 2, {2: {3}, 3: {4}, 4: {1}}, Code.DEPENDENCY_CYCLE),
        (1, 2, {2: {3}, 3: {4}}, None),
        (1, 2, {1: {3}, 3: {2}}, None),
        # A diamond is not a cycle.
        (1, 4, {2: {4}, 3: {4}, 1: {2, 3}}, None),
    ],
    ids=["no-edges", "self", "direct", "long", "chain-no-cycle", "already-reachable", "diamond"],
)
def test_add_dependency(
    number: int, target: int, waits_on: dict[int, set[int]], expected: Code | None
) -> None:
    verdict = add_dependency(INSTALLATION, task(number), task(target), waits_on)
    assert code_of(verdict) == expected


@pytest.mark.parametrize(
    ("subject", "target", "expected"),
    [
        (task(1), None, Code.NOT_FOUND),
        (None, task(2), Code.NOT_FOUND),
        (task(1), task(2, installation_id=NEIGHBOUR_INSTALLATION), Code.NOT_FOUND),
        (task(1, installation_id=NEIGHBOUR_INSTALLATION), task(2), Code.NOT_FOUND),
        # A live claim held by someone else does not stop a dependency.
        (task(1, claim=held(BOB, 1)), task(2), None),
        (task(1, status=TaskStatus.CLOSED), task(2, status=TaskStatus.CLOSED), None),
    ],
    ids=["missing-target", "missing-task", "target-other-installation", "task-other-installation", "claimed", "closed"],
)
def test_dependency_references(subject: Task | None, target: Task | None, expected: Code | None) -> None:
    assert code_of(add_dependency(INSTALLATION, subject, target, {})) == expected
    assert code_of(remove_dependency(INSTALLATION, subject, target)) == expected


@pytest.mark.parametrize(
    ("number", "parent", "parent_of", "expected"),
    [
        (1, 2, {}, None),
        (1, 1, {}, Code.PARENT_CYCLE),
        (1, 2, {2: 1}, Code.PARENT_CYCLE),
        (1, 3, {3: 2, 2: 1}, Code.PARENT_CYCLE),
        (1, 3, {3: 2, 2: None}, None),
        # Moving a task under its own sibling is fine.
        (1, 2, {1: 5, 2: 5}, None),
    ],
    ids=["no-parents", "self", "child", "grandchild", "unrelated-chain", "sibling"],
)
def test_set_parent(number: int, parent: int, parent_of: dict[int, int | None], expected: Code | None) -> None:
    assert code_of(set_parent(INSTALLATION, task(number), task(parent), parent_of)) == expected


@pytest.mark.parametrize(
    ("subject", "parent", "expected"),
    [
        (task(1), None, Code.NOT_FOUND),
        (None, task(2), Code.NOT_FOUND),
        (task(1), task(2, installation_id=NEIGHBOUR_INSTALLATION), Code.NOT_FOUND),
        (task(1, claim=held(BOB, 1)), task(2), None),
        (task(1), task(2, status=TaskStatus.CLOSED), None),
    ],
    ids=["missing-parent", "missing-task", "parent-other-installation", "claimed", "closed-parent"],
)
def test_set_parent_references(subject: Task | None, parent: Task | None, expected: Code | None) -> None:
    assert code_of(set_parent(INSTALLATION, subject, parent, {})) == expected


def test_refusal_is_the_apps_shared_shape() -> None:
    verdict = claim(INSTALLATION, task(status=TaskStatus.CLOSED), ALICE, T0)
    assert isinstance(verdict, Refusal)
    detail = verdict.detail()
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "task_closed"
    assert detail["message"]


def test_lease_kind_lifetimes() -> None:
    assert LeaseKind.CLAIM.lifetime == CLAIM_LIFE
    assert LeaseKind.LOCK.lifetime == LOCK_LIFE
