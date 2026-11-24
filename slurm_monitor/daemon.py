"""Observe Slurm and dispatch events independently of chat or MCP lifetimes."""

import asyncio
import fcntl
import logging
from pathlib import Path

from .app_server_client import AppServerClient
from .dispatcher import Dispatcher
from .ledger import MonitorLedger
from .observer import observe_slurm
from .settings import Settings, private_state, settings


def worker_running(directory: Path) -> bool:
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


async def run(ledger: MonitorLedger, config: Settings) -> None:
    socket = config.socket_path
    dispatcher = Dispatcher(
        ledger,
        (lambda: AppServerClient(socket)) if socket else None,
        webhook_url=config.webhook_url,
        webhook_secret=config.webhook_secret,
        webhook_proxy=config.webhook_proxy,
    )

    async def deliver() -> None:
        while True:
            await asyncio.to_thread(dispatcher.tick)
            await asyncio.sleep(2)

    async def observe() -> None:
        while True:
            await observe_slurm(ledger, config.ssh_hosts, config.local_host)
            await asyncio.sleep(config.poll_seconds)

    try:
        await asyncio.gather(observe(), deliver())
    finally:
        dispatcher.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    directory = private_state()
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A monitor worker already owns this database")
        asyncio.run(run(MonitorLedger(directory / "monitor.sqlite3"), settings()))


if __name__ == "__main__":
    main()
