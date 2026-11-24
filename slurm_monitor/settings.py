"""Per-user settings shared by the observer and the MCP plugin."""

import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_seconds: int = Field(default=30, ge=1)
    local_host: str = "local"
    ssh_hosts: tuple[str, ...] = ()
    control_socket: str = "~/.codex/app-server-control/app-server-control.sock"
    prompt: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_proxy: str = ""

    @model_validator(mode="after")
    def check_destinations(self) -> "Settings":
        if self.webhook_url:
            url = urlsplit(self.webhook_url)
            if not url.hostname or url.scheme not in {"https", "http"}:
                raise ValueError("Expected an HTTP(S) webhook URL")
        return self

    @property
    def socket_path(self) -> Path | None:
        return Path(self.control_socket).expanduser() if self.control_socket else None


def home() -> Path:
    return Path.home() / ".local/share/codex-slurm-monitor"


def settings_path() -> Path:
    return Path.home() / ".config/codex-slurm-monitor/config.toml"


def settings() -> Settings:
    with settings_path().open("rb") as stream:
        return Settings(**tomllib.load(stream))


def private_state() -> Path:
    os.umask(0o077)
    directory = home() / "state"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory
