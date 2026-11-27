"""The Tasks app's HTTP service: one route per manifest tool.

Every route is admitted by naapp-lib's guard (team-auth, the declared tool,
the installation's grant and its scopes), then by the installation fence, and
acts through a :class:`~aweb_tasks.store.TaskStore` bound to the admitted
installation and the verified principal.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from aweb_naapp.egress.send import Resolver
from aweb_naapp.runtime.app import NaappRuntime, mount_manifest_routes
from aweb_naapp.runtime.authorization import Authorization
from aweb_naapp.runtime.health import mount_health
from aweb_naapp.runtime.installation import InstallationRefused, apply_installation_migrations
from aweb_naapp.runtime.operator_egress import OperatorEgress
from aweb_naapp.runtime.team_auth import refusal
from awid.external_authority import TXTOutcomeResolver
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from aweb_tasks import MANIFEST_PATH
from aweb_tasks.config import TasksSettings
from aweb_tasks.decisions import Code, TaskStatus
from aweb_tasks.store import Clock, Fields, TaskRefused, TaskStore, utc_now

MIGRATIONS_PATH = Path(__file__).resolve().parent / "migrations"

# The HTTP status of each refusal: a missing task is not found; every other
# refusal is the task's or lease's state conflicting with the request.
REFUSAL_STATUS: dict[Code, int] = {code: 409 for code in Code} | {Code.NOT_FOUND: 404}

TaskType = Literal["task", "bug", "feature", "epic"]
# aw sends no body when none of a tool's optional body parameters is given.
OptionalBody = Body(default=None)
Priority = Annotated[int, Field(ge=0, le=4)]
Text = Annotated[str, Field(min_length=1)]


def refused(error: TaskRefused) -> HTTPException:
    return refusal(REFUSAL_STATUS[error.refusal.code], str(error.refusal.code), error.refusal.message)


class CreateTask(BaseModel, extra="forbid"):
    title: Text
    body: str = ""
    type: TaskType = "task"
    priority: Priority = 2
    labels: list[Text] = []
    parent: int | None = None
    assignee: Text | None = None


class UpdateTask(BaseModel, extra="forbid"):
    title: Text | None = None
    body: str | None = None
    type: TaskType | None = None
    priority: Priority | None = None
    labels: list[Text] | None = None
    parent: int | None = None
    assignee: Text | None = None
    status: TaskStatus | None = None


class Close(BaseModel, extra="forbid"):
    with_children: bool = False


class CloseClaimed(BaseModel, extra="forbid"):
    revision: int | None = None
    with_children: bool = False


class Comment(BaseModel, extra="forbid"):
    body: Text


class Dependency(BaseModel, extra="forbid"):
    on: int


class Renewal(BaseModel, extra="forbid"):
    revision: int | None = None


class Override(BaseModel, extra="forbid"):
    revision: int


class Release(BaseModel, extra="forbid"):
    number: int | None = None
    revision: int | None = None
    all: bool = False

    @model_validator(mode="after")
    def one_target(self) -> "Release":
        if self.all == (self.number is not None):
            raise ValueError("give exactly one of number or all")
        if self.all and self.revision is not None:
            raise ValueError("revision names one claim; it does not go with all")
        return self


class LockRequest(BaseModel, extra="forbid"):
    resource: Text
    reason: str | None = None


class LockRenewal(BaseModel, extra="forbid"):
    resource: Text
    revision: int | None = None


class LockOverride(BaseModel, extra="forbid"):
    resource: Text
    revision: int


def create_app(
    settings: TasksSettings | None = None,
    *,
    awid_transport: httpx.AsyncBaseTransport | None = None,
    awid_txt_resolver: TXTOutcomeResolver | None = None,
    resolver: Resolver | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Tasks' app. Hook events and team-server reads leave through the guard
    the settings configure; ``resolver`` replaces its DNS resolver."""
    resolved = settings or TasksSettings()
    runtime = NaappRuntime(
        resolved,
        module_name="tasks",
        migrations_path=MIGRATIONS_PATH,
        manifest_path=MANIFEST_PATH,
        stores=(apply_installation_migrations,),
        awid_transport=awid_transport,
        awid_txt_resolver=awid_txt_resolver,
        egress=OperatorEgress.from_settings(resolved, resolver=resolver),
    )
    app = FastAPI(title="tasks", lifespan=runtime.lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    # The installation fence refuses at admission, and again inside every
    # transaction, on entry or just before commit: one answer for all of them.
    @app.exception_handler(InstallationRefused)
    async def installation_refused(request: Request, error: InstallationRefused) -> JSONResponse:
        return JSONResponse(
            status_code=error.status, content={"detail": {"code": error.code, "message": str(error)}}
        )
    mount_manifest_routes(app, runtime=runtime)
    mount_health(app, resolved, checks=runtime.health_checks())

    # Every admitted member of an installed team has what the installation
    # grants: the first version's policy does not distinguish members.
    async def members(request: Request, principal: Any, tool: Any) -> bool:
        return True

    authorized = runtime.authorized(grants=runtime.installation_grants(), policy=members)

    async def store(
        request: Request, authorization: Annotated[Authorization, Depends(authorized)]
    ) -> TaskStore:
        permit = await runtime.installation_fence().permit(authorization.principal.team_id)
        if (authorization.grant.installation_id, authorization.grant.generation) != (
            permit.admitted.installation_id,
            permit.admitted.generation,
        ):
            raise InstallationRefused("generation_superseded", 409, "Installation changed during admission")
        return TaskStore(
            permit.database(),
            installation_id=permit.admitted.installation_id,
            principal=authorization.principal,
            events=runtime.app_events,
            app_id=resolved.app_id,
            clock=clock,
        )

    Store = Annotated[TaskStore, Depends(store)]

    async def answer(action: Callable[[], Any]) -> Any:
        try:
            return await action()
        except TaskRefused as error:
            raise refused(error) from None

    @app.post("/v1/tasks")
    async def create(payload: CreateTask, tasks: Store) -> dict:
        return await answer(lambda: tasks.create(
            title=payload.title, body=payload.body, type=payload.type, priority=payload.priority,
            labels=payload.labels, parent=payload.parent, assignee=payload.assignee,
        ))

    @app.get("/v1/tasks/{number}")
    async def show(number: int, tasks: Store) -> dict:
        return await answer(lambda: tasks.show(number))

    @app.get("/v1/tasks")
    async def list_tasks(
        tasks: Store, status: TaskStatus | None = None, label: str | None = None, assignee: str | None = None,
        parent: int | None = None, type: TaskType | None = None, priority: Priority | None = None,
    ) -> list[dict]:
        return await answer(lambda: tasks.list_tasks(
            status=status, label=label, assignee=assignee, parent=parent, type=type, priority=priority,
        ))

    @app.patch("/v1/tasks/{number}")
    async def update(number: int, payload: UpdateTask, tasks: Store) -> dict:
        return await answer(lambda: tasks.update(number, Fields(**payload.model_dump(exclude_unset=True))))

    @app.post("/v1/tasks/{number}/close")
    async def close(number: int, tasks: Store, payload: Close | None = OptionalBody) -> dict:
        given = payload or Close()
        return await answer(lambda: tasks.close(number, with_children=given.with_children))

    @app.post("/v1/tasks/{number}/reopen")
    async def reopen(number: int, tasks: Store) -> dict:
        return await answer(lambda: tasks.reopen(number))

    @app.post("/v1/tasks/{number}/comments")
    async def comment_add(number: int, payload: Comment, tasks: Store) -> dict:
        return await answer(lambda: tasks.comment(number, payload.body))

    @app.get("/v1/tasks/{number}/comments")
    async def comment_list(number: int, tasks: Store) -> list[dict]:
        return await answer(lambda: tasks.comments(number))

    @app.post("/v1/tasks/{number}/dependencies")
    async def dep_add(number: int, payload: Dependency, tasks: Store) -> dict:
        return await answer(lambda: tasks.add_dependency(number, payload.on))

    @app.delete("/v1/tasks/{number}/dependencies/{on}")
    async def dep_remove(number: int, on: int, tasks: Store) -> dict:
        return await answer(lambda: tasks.remove_dependency(number, on))

    @app.get("/v1/ready")
    async def ready(tasks: Store) -> list[dict]:
        return await answer(tasks.ready)

    @app.get("/v1/active")
    async def active(tasks: Store, holder: str | None = None) -> list[dict]:
        return await answer(lambda: tasks.active(holder=holder))

    @app.get("/v1/blocked")
    async def blocked(tasks: Store) -> list[dict]:
        return await answer(tasks.blocked)

    @app.post("/v1/tasks/{number}/claim")
    async def claim(number: int, tasks: Store) -> dict:
        return await answer(lambda: tasks.claim(number))

    @app.post("/v1/releases")
    async def release(payload: Release, tasks: Store) -> dict:
        if payload.all:
            return await answer(tasks.release_all)
        assert payload.number is not None
        number = payload.number
        return await answer(lambda: tasks.release(number, payload.revision))

    @app.post("/v1/tasks/{number}/claim/renew")
    async def claim_renew(number: int, tasks: Store, payload: Renewal | None = OptionalBody) -> dict:
        given = payload or Renewal()
        return await answer(lambda: tasks.renew_claim(number, given.revision))

    @app.post("/v1/locks")
    async def lock(payload: LockRequest, tasks: Store) -> dict:
        return await answer(lambda: tasks.lock(payload.resource, payload.reason))

    @app.post("/v1/locks/renew")
    async def lock_renew(payload: LockRenewal, tasks: Store) -> dict:
        return await answer(lambda: tasks.renew_lock(payload.resource, payload.revision))

    @app.post("/v1/locks/release")
    async def unlock(payload: LockRenewal, tasks: Store) -> dict:
        return await answer(lambda: tasks.unlock(payload.resource, payload.revision))

    @app.get("/v1/locks")
    async def locks(tasks: Store, holder: str | None = None) -> list[dict]:
        return await answer(lambda: tasks.locks(holder=holder))

    @app.post("/v1/tasks/{number}/takeover")
    async def takeover(number: int, payload: Override, tasks: Store) -> dict:
        return await answer(lambda: tasks.takeover(number, payload.revision))

    @app.post("/v1/locks/break")
    async def break_lock(payload: LockOverride, tasks: Store) -> dict:
        return await answer(lambda: tasks.break_lock(payload.resource, payload.revision))

    @app.post("/v1/tasks/{number}/close-claimed")
    async def close_claimed(number: int, tasks: Store, payload: CloseClaimed | None = OptionalBody) -> dict:
        given = payload or CloseClaimed()
        return await answer(lambda: tasks.close(
            number, with_children=given.with_children, overrides=True, presented_revision=given.revision,
        ))

    @app.get("/v1/tasks/{number}/history")
    async def history(number: int, tasks: Store) -> list[dict]:
        return await answer(lambda: tasks.history(number))

    return app


def serve() -> FastAPI:
    """The service for this deployment, configured from its ``TASKS_``
    environment only: ``uvicorn aweb_tasks.app:serve --factory``."""
    return create_app(TasksSettings(_env_file=None))
