"""Integration checks on temporary databases and controlled scheduler/transport responses."""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from slurm_monitor import observer
from slurm_monitor.dispatcher import Dispatcher
from slurm_monitor.ledger import MonitorLedger
from slurm_monitor.models import Watch


class Chat:
    def __init__(self) -> None:
        self.resumed = set()
        self.items = []
        self.turns = []
        self.busy = True
        self.lose_reply = ""

    def queue(self, thread: str) -> list:
        return [item for item in self.items if item["thread"] == thread]

    def thread(self, thread: str) -> dict:
        return {"status": {"type": "active" if self.busy else "idle"}}

    def resume(self, thread: str) -> dict:
        self.resumed.add(thread)
        return self.thread(thread)

    def enqueue(self, thread: str, message_id: str, prompt: str) -> dict:
        item = {
            "id": str(uuid4()),
            "clientUserMessageId": message_id,
            "text": prompt,
            "thread": thread,
        }
        self.items.append(item)
        if self.lose_reply == "enqueue":
            raise OSError("Lost reply")
        return item

    def start(self, thread: str, identifier: str) -> dict:
        item = self.queue(thread)[0]
        assert item["id"] == identifier
        self.items.remove(item)
        self.turns.append(item)
        if self.lose_reply == "start":
            raise OSError("Lost reply")
        return {"status": "completed", "id": identifier}

    def find_turn(
        self, thread: str, message: str, marker: str, since: float, turn_id: str
    ) -> dict | None:
        for item in self.turns:
            if item["thread"] == thread and item["clientUserMessageId"] == message:
                return {"id": item["id"], "status": "completed"}
        return None

    def close(self) -> None:
        self.resumed.clear()

    def unsubscribe(self, thread: str) -> None:
        self.resumed.discard(thread)

    def delete(self, thread: str, identifier: str) -> None:
        self.items = [item for item in self.items if item["id"] != identifier]


@pytest.mark.parametrize("lost", ["start", "enqueue", ""])
def test_group_delivery_and_restart(tmp_path: Path, lost: str) -> None:
    ledger = MonitorLedger(tmp_path / "db")
    group = Watch(job_ids=["11", "12_4"], thread_id=uuid4())
    all_wait = ledger.register(group, "local", "")["id"]
    first = group.model_copy(update={"mode": "any", "thread_id": uuid4()})
    any_wait = ledger.register(first, "local", "custom")["id"]
    assert all_wait == ledger.register(group, "local", "")["id"]
    assert len(ledger.active_jobs()) == 2
    ledger.observe("local", "11", "FAILED", "1:0")
    assert ledger.wait_status(all_wait) == "waiting"
    assert ledger.wait_status(any_wait) == "ready"
    chat = Chat()
    worker = Dispatcher(ledger, lambda: chat)
    worker.tick()
    assert (
        not chat.items
        and ledger.read(any_wait)["delivery_status"] == "waiting_for_idle"
    )
    chat.busy = False
    chat.lose_reply = lost
    worker.tick()
    worker.tick()
    restarted = Dispatcher(MonitorLedger(tmp_path / "db"), lambda: chat)
    ledger.observe("local", "12_4", "COMPLETED", "0:0")
    for _ in range(8):
        restarted.tick()
    assert len(chat.turns) == 2
    sent = {item["thread"]: item["text"].splitlines() for item in chat.turns}
    assert len(sent[str(group.thread_id)]) == 2  # marker and event only
    assert sent[str(first.thread_id)][1] == "custom"
    assert ledger.read(all_wait)["delivery_status"] == "completed"
    assert ledger.read(any_wait)["delivery_status"] == "completed"
    event = ledger.read(any_wait)["event"]
    with pytest.raises(ValueError):
        ledger.control(any_wait, str(uuid4()))
    assert ledger.control(any_wait, event["event_id"])["status"] == "acknowledged"


def test_scheduler_accounting_and_cancellation(tmp_path: Path) -> None:
    ledger = MonitorLedger(tmp_path / "db")
    group = Watch(job_ids=["21_3"], thread_id=uuid4(), delivery="pull")
    watch = ledger.register(group, "local", "")
    ledger.set_prompt(watch["id"], "edited")
    responses = AsyncMock(
        side_effect=[
            ValueError("Invalid job id"),
            "21_3.batch|COMPLETED|0:0|24.batch",
            "",
            "21_3|OUT_OF_MEMORY|1:0|24",
        ]
    )
    with patch.object(observer, "scheduler_read", responses):
        asyncio.run(observer.observe_slurm(ledger, (), "local"))
        assert ledger.wait_status(watch["id"]) == "waiting"
        asyncio.run(observer.observe_slurm(ledger, (), "local"))
    result = ledger.read(watch["id"])
    assert result["instruction"] == "edited"
    assert result["event"]["jobs"][0]["state"] == "OUT_OF_MEMORY"
    assert observer.scheduler_row("21_3|COMPLETED|0:0|24", "24") == ("COMPLETED", "0:0")
    assert not Dispatcher(ledger).candidates()
    assert ledger.control(watch["id"])["status"] == "cancelled"


def test_uncertain_does_not_send_again(tmp_path: Path) -> None:
    ledger = MonitorLedger(tmp_path / "db")
    wait = ledger.register(Watch(job_ids=["13"], thread_id=uuid4()), "local", "")
    ledger.observe("local", "13", "COMPLETED")
    chat = Chat()
    chat.busy = False
    worker = Dispatcher(ledger, lambda: chat)
    worker.tick()
    chat.items.clear()
    worker.tick()
    worker.tick()
    assert ledger.read(wait["id"])["delivery_status"] == "uncertain"
    assert not chat.items and not chat.turns


def test_signed_webhook_and_edit(tmp_path: Path) -> None:
    ledger = MonitorLedger(tmp_path / "db")
    wait = ledger.register(
        Watch(job_ids=["10"], thread_id=uuid4(), delivery="webhook"), "local", ""
    )
    ledger.set_prompt(wait["id"], "own words")
    ledger.observe("local", "10", "TIMEOUT", "0:15")
    with patch("slurm_monitor.dispatcher.build_opener") as opener:
        opener.return_value.open.return_value.__enter__.return_value.status = 204
        worker = Dispatcher(
            ledger, webhook_url="http://localhost/events", webhook_secret="test"
        )
        worker.tick()
        worker.tick()
        request = opener.return_value.open.call_args.args[0]
        assert opener.return_value.open.call_count == 1
    assert json.loads(request.data)["instruction"] == "own words"
    signature = hmac.new(b"test", request.data, hashlib.sha256).hexdigest()
    assert request.headers["X-slurm-monitor-signature"] == "sha256=" + signature
    assert ledger.read(wait["id"])["delivery_status"] == "completed"


def test_lost_webhook_response_stops_delivery(tmp_path: Path) -> None:
    ledger = MonitorLedger(tmp_path / "db")
    wait = ledger.register(
        Watch(job_ids=["11"], thread_id=uuid4(), delivery="webhook"), "local", ""
    )
    ledger.observe("local", "11", "COMPLETED", "0:0")
    with patch("slurm_monitor.dispatcher.build_opener") as opener:
        opener.return_value.open.side_effect = TimeoutError("read timed out")
        worker = Dispatcher(ledger, webhook_url="http://localhost/events")
        worker.tick()
        worker.tick()
        assert opener.return_value.open.call_count == 1
    assert ledger.read(wait["id"])["delivery_status"] == "uncertain"
