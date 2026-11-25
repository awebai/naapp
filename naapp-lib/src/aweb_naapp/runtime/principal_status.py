"""Value-free diagnostics for a refusal to observe current principal status."""

import logging
from enum import StrEnum
from time import monotonic_ns

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class PrincipalStatusReason(StrEnum):
    TEAM_OBSERVATION_STALE = "team_observation_stale"
    TEAM_OBSERVATION_UNAVAILABLE = "team_observation_unavailable"
    TEAM_REFRESH_STALE = "team_refresh_stale"
    TEAM_REFRESH_UNAVAILABLE = "team_refresh_unavailable"
    IDENTITY_BINDING_UNAVAILABLE = "identity_binding_unavailable"
    IDENTITY_KIND_UNAVAILABLE = "identity_kind_unavailable"
    IDENTITY_AGE_UNAVAILABLE = "identity_age_unavailable"
    IDENTITY_DEADLINE = "identity_deadline"
    MEMBERSHIP_DEADLINE = "membership_deadline"
    REQUESTER_IDENTITY_UNAVAILABLE = "requester_identity_unavailable"
    REQUESTER_IDENTITY_DEADLINE = "requester_identity_deadline"
    OWNER_MEMBER_OBSERVATION_ERROR = "owner_member_observation_error"


def log_principal_status_unavailable(
    reason: PrincipalStatusReason, started: int, *, now: int | None = None
) -> None:
    elapsed = max(0, (monotonic_ns() if now is None else now) - started)
    logger.warning(
        "principal_status_unavailable reason=%s elapsed_ms=%d", reason.value, elapsed // 1_000_000
    )


class PrincipalStatusUnavailable(HTTPException):
    """Log the decision where its reason and monotonic start are known."""

    def __init__(
        self, reason: PrincipalStatusReason, started: int, *, now: int | None = None
    ) -> None:
        self.reason = reason
        log_principal_status_unavailable(reason, started, now=now)
        super().__init__(503, "principal_status_unavailable")
