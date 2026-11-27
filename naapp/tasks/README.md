# Tasks

A team's tasks, the claims on them and its locks, as an aweb app with the id
`tasks`. Agents and people reach it as `aw tasks <verb>`.

`aweb_tasks.decisions` holds every lease and authority rule as a pure function:
each takes the facts it needs and returns the next state or a `Refusal` with
the code the app answers with.
