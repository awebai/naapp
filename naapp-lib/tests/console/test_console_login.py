"""Console passkey login on real PostgreSQL, through the human origin.

Every ceremony is real: B3 issues the options, the software authenticator
answers them, and py_webauthn verifies the assertion. Each owner has their own
scope; the app's predicate says which scopes may log in here.

The routes are built inside a function, so their annotations are evaluated
eagerly (no postponed annotations) for FastAPI to see the session dependency.
"""

from contextlib import aclosing
from datetime import timedelta
from time import monotonic_ns
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from aweb_naapp_testing import Member
from aweb_naapp_testing.human_members import human_namespace
from aweb_naapp_testing.webauthn import SoftAuthenticator
from awid.registry import RegistryClient
from awid_service.testing import REGISTRY_URL, SERVICE_TOKEN
from fastapi import APIRouter, Depends, FastAPI

from aweb_naapp.console import (
    ClientIdentity,
    ConsoleRoute,
    ConsoleSession,
    ConsoleSessions,
    LoginLimiter,
    ScopeMember,
    console_login_router,
)
from aweb_naapp.runtime import human_member, team_auth
from aweb_naapp.runtime.origins import split_origins
from aweb_naapp.runtime.team_auth import AWIDTeamCache
from tests.console.support import HUMAN, OWNER_A, OWNER_B, Console, NoInstallation

AGENT = "https://api.console.example"


def _limiter(console: Console, limit: int = 20) -> LoginLimiter:
    return LoginLimiter(
        console.db, client_identity=ClientIdentity(), limit=limit, window=timedelta(minutes=1)
    )


def _app(
    console: Console,
    limiter: LoginLimiter,
    prefix: str = "",
    sessions: ConsoleSessions | None = None,
) -> Any:
    sessions = sessions or console.sessions()
    human = FastAPI()
    human.include_router(console_login_router(sessions=sessions, limiter=limiter, prefix=prefix))
    pages = APIRouter(route_class=ConsoleRoute)

    @pages.get("/whoami")
    async def whoami(
        session: Annotated[ConsoleSession, Depends(sessions.current)],
    ) -> dict[str, str]:
        return {"subject": session.subject, "scope": session.scope}

    human.include_router(pages)
    return split_origins(FastAPI(), human, agent_origin=AGENT, human_origin=HUMAN)


def _client(app: Any, origin: str = HUMAN) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin)


async def _login(
    client: httpx.AsyncClient, device: SoftAuthenticator, scope: str, prefix: str = "", **kw: Any
) -> httpx.Response:
    options = await client.post(f"{prefix}/login/options", json={"scope": scope})
    assert options.status_code == 200
    assertion = device.get(options.json(), **kw)
    response = await client.post(f"{prefix}/login", json={"scope": scope, "assertion": assertion})
    # Each request carries exactly the cookie a test gives it.
    client.cookies.clear()
    return response


def _cookie(response: httpx.Response) -> str:
    return response.headers["set-cookie"].split(";", 1)[0]


async def test_an_owner_logs_in_with_a_user_verified_passkey(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        response = await _login(client, device, OWNER_A)
        assert response.status_code == 200
        assert response.json()["csrf_token"]
        whoami = await client.get("/whoami", headers={"Cookie": _cookie(response)})
    assert whoami.status_code == 200
    assert whoami.json()["scope"] == OWNER_A
    assert whoami.json()["subject"]


class MembershipDeadlineTransport(httpx.AsyncBaseTransport):
    """Advance only the verifier's clock at a real registry response."""

    def __init__(self, registry, expiry: str, certificate_id: str) -> None:
        self.registry = registry
        self.expiry = expiry
        self.certificate_id = certificate_id
        self.now = monotonic_ns()
        self.team_reads = 0
        self.kind_reads = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.registry.transport.handle_async_request(request)
        if request.method == "GET" and request.url.path == "/v1/namespaces/example.com/teams/people":
            assert response.status_code == 200
            self.team_reads += 1
            if self.expiry == "retry_error" and self.team_reads == 2:
                return httpx.Response(503, json={"detail": "fixture-no-log-value"})
            if (self.expiry == "cold" and self.team_reads == 1) or (
                self.expiry == "retry" and self.team_reads == 2
            ):
                self.now += 1_000_000_001
        elif request.method == "POST" and request.url.path == "/v1/id-certificates/status":
            assert response.status_code == 200
            self.kind_reads += 1
            if self.kind_reads == 1 and self.expiry != "cold":
                self.now += 2
                if self.expiry == "revoked":
                    await self.registry.revoke(self.certificate_id)
        return response


@pytest.mark.parametrize(
    ("expiry", "status", "code", "reads"),
    [
        ("cached", 200, None, 2),
        ("cold", 503, "principal_status_unavailable", 1),
        ("retry", 503, "principal_status_unavailable", 2),
        ("retry_error", 503, "principal_status_unavailable", 2),
        ("revoked", 403, "principal_not_member", 2),
    ],
)
async def test_login_rechecks_membership_once_when_it_expires_during_identity_status(
    console: Console, registry, monkeypatch, caplog, expiry, status, code, reads
):
    team = "people:example.com"
    member = Member(team_id=team)
    await registry.add_team(team, member.team_did_key)
    _, device = await console.enrol(OWNER_A)
    async with human_namespace(registry, "example.com") as namespace:
        human = await namespace.register(member)
        transport = MembershipDeadlineTransport(registry, expiry, member.certificate_id)

        def clock() -> int:
            return transport.now

        monkeypatch.setattr(human_member, "monotonic_ns", clock)
        monkeypatch.setattr(team_auth, "time", SimpleNamespace(monotonic_ns=clock))
        cache = AWIDTeamCache(
            registry_url=REGISTRY_URL,
            ttl_seconds=1,
            service_token=SERVICE_TOKEN,
            transport=transport,
        )
        if expiry != "cold":
            cached = await cache.get(team, member.certificate_id)
            transport.now = cached.expires_at - 1

        async with aclosing(RegistryClient(
            REGISTRY_URL,
            transport=transport,
            service_token=SERVICE_TOKEN,
            domain_verifier=namespace.authority,
        )) as client:
            async def membership(scope: str) -> ScopeMember:
                assert scope == OWNER_A
                return await human_member.check_human_member(
                    cache, client, team_id=team, principal=human.principal,
                    certificate=member.certificate(),
                )

            sessions = ConsoleSessions(
                console.db, passkeys=console.passkeys, scopes=console.admits,
                member=membership, installations=NoInstallation(),
            )
            async with _client(_app(console, _limiter(console), sessions=sessions)) as browser:
                result = await _login(browser, device, OWNER_A)

        assert (result.status_code, result.json().get("detail")) == (status, code)
        assert transport.team_reads == reads
        reasons = {
            "cold": ("team_observation_stale", 1000),
            "retry": ("team_refresh_stale", 1000),
            "retry_error": ("team_refresh_unavailable", 0),
        }
        expected = []
        if expiry in reasons:
            reason, elapsed_ms = reasons[expiry]
            expected = [f"principal_status_unavailable reason={reason} elapsed_ms={elapsed_ms}"]
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "aweb_naapp.runtime.principal_status"
        ] == expected
        assert transport.kind_reads == (0 if expiry == "cold" else 1)
        sessions_opened = await console.db.fetch_value("SELECT count(*) FROM {{tables.console_sessions}}")
        assert sessions_opened == (1 if status == 200 else 0)

async def test_login_without_user_verification_opens_no_session(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        response = await _login(client, device, OWNER_A, user_verified=False)
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert await console.db.fetch_value("SELECT count(*) FROM {{tables.console_sessions}}") == 0


async def test_each_owner_logs_in_only_to_their_own_scope(console: Console) -> None:
    _, device_a = await console.enrol(OWNER_A)
    _, device_b = await console.enrol(OWNER_B)
    async with _client(_app(console, _limiter(console))) as client:
        a = await _login(client, device_a, OWNER_A)
        b = await _login(client, device_b, OWNER_B)
        crossed = await _login(client, device_a, OWNER_B)
        whoami_a = await client.get("/whoami", headers={"Cookie": _cookie(a)})
        whoami_b = await client.get("/whoami", headers={"Cookie": _cookie(b)})
    assert whoami_a.json()["scope"] == OWNER_A
    assert whoami_b.json()["scope"] == OWNER_B
    assert whoami_a.json()["subject"] != whoami_b.json()["subject"]
    assert crossed.status_code == 401
    assert "set-cookie" not in crossed.headers


async def test_a_scope_the_app_does_not_accept_cannot_log_in(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        options = dict((await client.post("/login/options", json={"scope": OWNER_A})).json())
        console.allowed.discard(OWNER_A)
        refused_options = await client.post("/login/options", json={"scope": OWNER_A})
        refused_login = await client.post(
            "/login", json={"scope": OWNER_A, "assertion": device.get(options)}
        )
    assert refused_options.status_code == 401
    assert refused_login.status_code == 401
    assert "set-cookie" not in refused_login.headers


async def test_a_refused_scope_gets_the_same_answer_as_a_failed_login(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        unknown = await client.post("/login/options", json={"scope": "app.owner.nobody"})
        console.allowed.add("app.owner.nobody")
        no_approver = await client.post("/login/options", json={"scope": "app.owner.nobody"})
        failed = await _login(client, device, OWNER_A, user_verified=False)
    assert unknown.status_code == no_approver.status_code == failed.status_code == 401
    assert unknown.json() == no_approver.json() == failed.json()


async def test_login_requests_are_rate_limited_per_client(console: Console) -> None:
    await console.enrol(OWNER_A)
    limiter = _limiter(console, limit=2)
    async with _client(_app(console, limiter)) as client:
        codes = [
            (await client.post("/login/options", json={"scope": scope})).status_code
            for scope in (OWNER_A, OWNER_B, OWNER_A)
        ]
    assert codes[2] == 429
    assert (await limiter.volume(OWNER_A, since=timedelta(minutes=5))).requests == 2


async def test_a_refused_scope_is_not_metered(console: Console) -> None:
    limiter = _limiter(console)
    async with _client(_app(console, limiter)) as client:
        for n in range(3):
            await client.post("/login/options", json={"scope": f"app.owner.guess{n}"})
    volume = await console.db.fetch_value("SELECT count(*) FROM {{tables.console_login_volume}}")
    assert volume == 0


async def test_logout_needs_the_csrf_token_and_ends_the_session(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        response = await _login(client, device, OWNER_A)
        cookie, csrf = _cookie(response), response.json()["csrf_token"]
        without = await client.post("/logout", headers={"Cookie": cookie})
        logout = await client.post("/logout", headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        after = await client.get("/whoami", headers={"Cookie": cookie})
    assert without.status_code == 403
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert after.status_code == 401


async def test_the_login_routes_take_a_prefix(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console), prefix="/console")) as client:
        response = await _login(client, device, OWNER_A, prefix="/console")
        root = await client.post("/login/options", json={"scope": OWNER_A})
    assert response.status_code == 200
    assert root.status_code == 404


@pytest.mark.parametrize(
    "header",
    [
        ("Authorization", "Bearer token"),
        ("X-AWEB-Signature", "sig"),
        ("X-AWID-Team", "team"),
    ],
)
async def test_team_auth_is_refused_on_the_console(
    console: Console, header: tuple[str, str]
) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        options = dict((await client.post("/login/options", json={"scope": OWNER_A})).json())
        login = await client.post(
            "/login",
            json={"scope": OWNER_A, "assertion": device.get(options)},
            headers={header[0]: header[1]},
        )
        client.cookies.clear()
        session = await _login(client, device, OWNER_A)
        whoami = await client.get(
            "/whoami", headers={"Cookie": _cookie(session), header[0]: header[1]}
        )
    assert login.status_code == 403
    assert "set-cookie" not in login.headers
    assert whoami.status_code == 403


async def test_the_console_is_not_served_on_the_agent_origin(console: Console) -> None:
    subject, device = await console.enrol(OWNER_A)
    app = _app(console, _limiter(console))
    async with _client(app, AGENT) as client:
        response = await client.post("/login/options", json={"scope": OWNER_A})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "set-cookie" not in response.headers

    async with _client(app) as client:
        login = await _login(client, device, OWNER_A)
        assert login.status_code == 200
        whoami = await client.get("/whoami", headers={"Cookie": _cookie(login)})
    assert whoami.status_code == 200
    assert whoami.json() == {"subject": subject, "scope": OWNER_A}


async def test_login_responses_carry_the_console_headers(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        responses = [
            await client.post("/login/options", json={"scope": OWNER_A}),
            await client.post("/login/options", json={"scope": "app.owner.nobody"}),
            await client.post("/login/options", json={}),
            await _login(client, device, OWNER_A),
        ]
    for response in responses:
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("path", "body", "field", "error_type"),
    [
        (
            "/login/options",
            {"scope": {"unexpected": "submitted-value"}},
            "scope",
            "string_type",
        ),
        (
            "/login",
            {"scope": ["submitted-value"], "assertion": {}},
            "scope",
            "string_type",
        ),
        (
            "/login",
            {"scope": OWNER_A, "assertion": "submitted-value"},
            "assertion",
            "dict_type",
        ),
        (
            "/login",
            {"scope": OWNER_A, "assertion": ["submitted-value"]},
            "assertion",
            "dict_type",
        ),
        (
            "/login",
            {"scope": OWNER_A, "unrelated": "submitted-value"},
            "assertion",
            "missing",
        ),
    ],
    ids=[
        "options-scope",
        "login-scope",
        "assertion-string",
        "assertion-list",
        "missing-assertion",
    ],
)
async def test_login_validation_returns_only_location_and_type(
    console: Console, path: str, body: dict[str, Any], field: str, error_type: str
) -> None:
    async with _client(_app(console, _limiter(console))) as client:
        response = await client.post(path, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": [{"loc": ["body", field], "type": error_type}]}
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


async def _closed(console: Console) -> int:
    return await console.db.fetch_value(
        "SELECT count(*) FROM {{tables.console_sessions}} WHERE closed_at IS NOT NULL"
    )


async def test_a_session_ends_when_a_recovery_replaces_its_approver(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        cookie = _cookie(await _login(client, device, OWNER_A))
        assert (await client.get("/whoami", headers={"Cookie": cookie})).status_code == 200
        await console.recover(OWNER_A, "new-phone@example.com")
        after = await client.get("/whoami", headers={"Cookie": cookie})
    assert after.status_code == 401
    assert after.json() == {"detail": "console_session_required"}
    assert await _closed(console) == 1


async def test_a_session_ends_when_the_app_stops_admitting_its_scope(console: Console) -> None:
    _, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        cookie = _cookie(await _login(client, device, OWNER_A))
        console.allowed.discard(OWNER_A)
        refused = await client.get("/whoami", headers={"Cookie": cookie})
        console.allowed.add(OWNER_A)
        still_refused = await client.get("/whoami", headers={"Cookie": cookie})
    assert refused.status_code == still_refused.status_code == 401
    assert await _closed(console) == 1


async def test_another_owners_session_survives_a_recovery(console: Console) -> None:
    _, device_a = await console.enrol(OWNER_A)
    _, device_b = await console.enrol(OWNER_B)
    async with _client(_app(console, _limiter(console))) as client:
        cookie_b = _cookie(await _login(client, device_b, OWNER_B))
        await console.recover(OWNER_A, "new-phone@example.com")
        whoami = await client.get("/whoami", headers={"Cookie": cookie_b})
    assert whoami.status_code == 200
    assert whoami.json()["scope"] == OWNER_B


async def test_a_session_ends_when_the_owner_retires_its_approver(console: Console) -> None:
    subject, device = await console.enrol(OWNER_A)
    async with _client(_app(console, _limiter(console))) as client:
        cookie = _cookie(await _login(client, device, OWNER_A))
        assert (await client.get("/whoami", headers={"Cookie": cookie})).status_code == 200
        await console.retire(OWNER_A, subject)
        after = await client.get("/whoami", headers={"Cookie": cookie})
    assert after.status_code == 401
    assert after.json() == {"detail": "console_session_required"}
    assert await _closed(console) == 1


def _assert_session_ended(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json() == {"detail": "console_session_required"}
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_revoking_a_key_ends_its_session_but_preserves_the_sibling_key_session(
    console: Console,
) -> None:
    subject, key_a = await console.enrol(OWNER_A)
    key_b = await console.add_passkey(subject)
    async with _client(_app(console, _limiter(console))) as client:
        cookie_a = _cookie(await _login(client, key_a, OWNER_A))
        cookie_b = _cookie(await _login(client, key_b, OWNER_A))
        for cookie in (cookie_a, cookie_b):
            assert (await client.get("/whoami", headers={"Cookie": cookie})).status_code == 200
        async with console.db.transaction() as tx:
            await console.passkeys.revoke_passkey_in(
                tx,
                scope=OWNER_A,
                subject=subject,
                credential_id=next(iter(key_a.credentials)),
                authorized_by="team-owner",
            )
        revoked = await client.get("/whoami", headers={"Cookie": cookie_a})
        surviving = await client.get("/whoami", headers={"Cookie": cookie_b})
        replayed = await client.get("/whoami", headers={"Cookie": cookie_a})
    _assert_session_ended(revoked)
    _assert_session_ended(replayed)
    assert surviving.status_code == 200
    assert surviving.json()["subject"] == subject
    assert await _closed(console) == 1


async def test_retiring_a_subject_ends_all_its_key_sessions_and_preserves_another_subject(
    console: Console,
) -> None:
    subject, key_a = await console.enrol(OWNER_A)
    key_b = await console.add_passkey(subject)
    _, other = await console.enrol(OWNER_B)
    async with _client(_app(console, _limiter(console))) as client:
        cookies = [_cookie(await _login(client, key, OWNER_A)) for key in (key_a, key_b)]
        other_cookie = _cookie(await _login(client, other, OWNER_B))
        async with console.db.transaction() as tx:
            await console.passkeys.retire_in(
                tx, scope=OWNER_A, subject=subject, authorized_by="team-owner"
            )
        ended = [await client.get("/whoami", headers={"Cookie": cookie}) for cookie in cookies]
        surviving = await client.get("/whoami", headers={"Cookie": other_cookie})
    for response in ended:
        _assert_session_ended(response)
    assert surviving.status_code == 200
    assert surviving.json()["scope"] == OWNER_B
    assert await _closed(console) == 2
