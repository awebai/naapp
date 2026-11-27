# Tasks

A team's tasks, the claims on them and its locks, as an aweb app with the id
`tasks`. Agents and people reach it as `aw tasks <verb>`.

`aweb_tasks.decisions` holds every lease and authority rule as a pure function:
each takes the facts it needs and returns the next state or a `Refusal` with
the code the app answers with.

## Deploying it

Configuration is the environment only, under `TASKS_`: `DATABASE_URL`,
`PUBLIC_ORIGIN`, `APP_ID`, the registry (`AWID_REGISTRY_URL`, with
`AWID_SERVICE_TOKEN` or `AWID_MEMBER_CREDENTIALS`), `APP_KEY_FILES` for the
kids `tasks:emit-1` and `tasks:grant-read-1`, and `APP_EVENTS_ORIGIN`, the
team's aweb server. The image serves on port 8765 and answers `/live`.

The hosted origin is `https://tasks.aweb.ai`, the manifest template's
`app.origin`. Only it (or a loopback origin) may serve the hosted id `tasks`;
any other deployment sets its own `TASKS_APP_ID`, and its members reach it as
`aw <that id>`.
