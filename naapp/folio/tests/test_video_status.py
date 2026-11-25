"""A video's Cloudflare Stream state reaches assets.stream_status only as a StreamStatus.

The parser, the upload owner, Folio's Cloudflare client and the database are
real; Cloudflare is a loopback server answering its documented JSON
(cloudflare_wire), since the tests do not reach Cloudflare. test_cloudflare_stream
covers every documented state; here one of them, and an unrecognized one, go
through the stored column and its CHECK.
"""

from __future__ import annotations

import pytest
from cloudflare_wire import STREAM_UID, cloudflare_stream_api
from fastapi import HTTPException
from test_present_privacy import OWNER, present_world

from folio.models import StreamStatus, VideoDirectUploadRequest
from folio.repository import create_video_direct_upload, get_asset_metadata

__all__ = ["present_world"]


@pytest.fixture
async def cloudflare_world(present_world):
    """present_world whose Folio talks to the loopback Cloudflare Stream API."""
    _, db, settings = present_world
    async with cloudflare_stream_api() as api:
        yield db, settings.model_copy(update=api.settings()), api


async def _video(db, settings) -> object:
    """A video asset created through Folio's direct upload, pending its upload."""
    upload = await create_video_direct_upload(
        db,
        principal=OWNER,
        settings=settings,
        video=VideoDirectUploadRequest(content_type="video/mp4"),
    )
    return upload["asset_id"]


async def _stored_status(db, asset_id) -> str:
    row = await db.fetch_one(
        "SELECT stream_status FROM {{tables.assets}} WHERE asset_id=$1", asset_id
    )
    return row["stream_status"]


async def test_a_documented_cloudflare_state_is_stored(cloudflare_world):
    db, settings, api = cloudflare_world
    asset_id = await _video(db, settings)
    assert await _stored_status(db, asset_id) == StreamStatus.PENDING_UPLOAD
    api.state = "downloading"

    metadata = await get_asset_metadata(db, principal=OWNER, settings=settings, asset_id=asset_id)

    assert await _stored_status(db, asset_id) == StreamStatus.QUEUED
    assert metadata["stream_status"] == StreamStatus.QUEUED
    assert [(sent.method, sent.path.rsplit("/", 1)[-1]) for sent in api.received] == [
        ("POST", "direct_upload"),
        ("GET", STREAM_UID),
    ]


async def test_an_unrecognized_cloudflare_state_is_refused_before_it_is_written(
    cloudflare_world,
):
    db, settings, api = cloudflare_world
    asset_id = await _video(db, settings)
    api.state = "inprogress"
    await get_asset_metadata(db, principal=OWNER, settings=settings, asset_id=asset_id)
    assert await _stored_status(db, asset_id) == StreamStatus.PROCESSING
    api.state = "melting"

    with pytest.raises(HTTPException) as refused:
        await get_asset_metadata(db, principal=OWNER, settings=settings, asset_id=asset_id)

    assert refused.value.status_code == 502
    assert await _stored_status(db, asset_id) == StreamStatus.PROCESSING
