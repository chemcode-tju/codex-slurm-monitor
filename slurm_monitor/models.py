from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


JobID = Annotated[str, Field(pattern=r"^[0-9]+(_[0-9]+)?$", max_length=64)]


class Watch(Input):
    thread_id: UUID = Field(description="Persistent ID of the originating Codex chat")
    job_ids: list[JobID] = Field(min_length=1, max_length=200)
    host: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    mode: Literal["all", "any"] = "all"
    delivery: Literal["codex", "pull", "webhook"] = "codex"
    prompt: str | None = Field(default=None, max_length=16000)


class ListWatches(Input):
    thread_id: UUID | None = None
    offset: int = Field(default=0, ge=0)


class Read(Input):
    wait_id: UUID


class Acknowledge(Read):
    event_id: UUID


class EditPrompt(Read):
    prompt: str = Field(max_length=16000)
