from __future__ import annotations

from aweb_naapp.runtime.settings import NaappSettings
from pydantic import Field
from pydantic_settings import SettingsConfigDict


class TasksSettings(NaappSettings):
    """Runtime configuration for Tasks: the shared naapp settings under TASKS_.

    Only the environment configures it; a .env in whatever directory the
    process starts in is not its configuration.
    """

    model_config = SettingsConfigDict(env_prefix="TASKS_", env_file=None, extra="ignore")

    database_url: str = Field(default="postgresql://localhost/tasks")
    public_origin: str = Field(default="http://127.0.0.1:8765")
    app_id: str = Field(default="tasks")
