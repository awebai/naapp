"""aweb_naapp_testing.server: a team's coordination server, found through the
team's AWID record and reached through its in-process transport."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from aweb_naapp_testing import Member
from aweb_naapp_testing.server import coordination_server
from redis.asyncio import Redis

from aweb_naapp.runtime.team_server import TeamServerUnavailable


def _member() -> Member:
    # The server's AWID client caches team keys in the Redis other runs share.
    return Member(team_id=f"t{uuid4().hex[:12]}:naapp.example", alias="alice")


async def test_a_published_team_finds_the_server_and_connects_to_it(coordination) -> None:
    member = _member()
    await coordination.add_member(member)
    directory = coordination.directory()
    with pytest.raises(TeamServerUnavailable) as unpublished:
        await directory.resolve(member.team_id)
    assert unpublished.value.code == "team_server_unpublished"

    await coordination.publish_server(member)
    server = await coordination.directory().resolve(member.team_id)
    assert server.server_url == coordination.server.url

    connected = await coordination.connect(member)
    assert (connected["team_id"], connected["alias"]) == (member.team_id, "alice")


async def test_a_member_of_a_team_unknown_to_awid_cannot_connect(coordination) -> None:
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await coordination.connect(_member())
    assert refused.value.response.status_code == 401


async def _cached(member: Member) -> list[str]:
    """The keys the shared Redis holds for the member's team."""
    name, domain = member.team_id.split(":", 1)
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        return [key async for key in redis.scan_iter(match=f"*{domain}/{name}*")]


async def test_the_coordination_server_leaves_no_team_it_cached_behind(
    coordination_registry, test_db_factory, monkeypatch
) -> None:
    known, unknown = _member(), _member()
    async with coordination_server(
        coordination_registry, test_db_factory, monkeypatch
    ) as coordination:
        await coordination.add_member(known)
        await coordination.publish_server(known)
        await coordination.connect(known)
        with pytest.raises(httpx.HTTPStatusError):
            await coordination.connect(unknown)
        assert await _cached(known)
    assert await _cached(known) == []
    assert await _cached(unknown) == []
