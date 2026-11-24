"""Deliver opted-in job events to Codex chats or configured webhooks."""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from websockets.exceptions import WebSocketException

from .app_server_client import AppServerClient, RpcError
from .ledger import MonitorLedger

logger = logging.getLogger(__name__)
UNSENT = ("pending", "waiting_for_idle", "waiting_for_queue", "retrying")
FINISHED = ("completed", "failed", "interrupted", "cancelled", "already_acknowledged")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def wakeup_message(row: dict) -> str:
    """Keep the stable event marker for recovery; append only user-supplied prose."""
    parts = [f"[slurm-monitor-event:{row['event_id']}]"]
    if row["instruction"]:
        parts.append(row["instruction"])
    parts.append(row["payload"])
    return "\n".join(parts)


class Dispatcher:
    def __init__(
        self,
        ledger: MonitorLedger,
        connect: Callable[[], AppServerClient] | None = None,
        *,
        webhook_url: str = "",
        webhook_secret: str = "",
        webhook_proxy: str = "",
        webhook_timeout: float = 20.0,
        webhook_max_attempts: int = 8,
        webhook_retry_base: float = 5.0,
    ) -> None:
        self.ledger = ledger
        self.connect = connect
        self.api: AppServerClient | None = None
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.webhook_proxy = webhook_proxy.strip()
        self.webhook_timeout = webhook_timeout
        self.webhook_max_attempts = webhook_max_attempts
        self.webhook_retry_base = webhook_retry_base

    def candidates(self) -> list[dict]:
        with self.ledger.connect() as db:
            rows = db.execute(
                "SELECT e.id AS event_id,e.payload,e.wait_id,w.thread,w.mode,"
                "w.status AS wait_status,w.instruction,"
                "w.delivery_kind,"
                "d.prompt,d.message_id,d.status,d.submission_id,d.turn_id,"
                "d.created,d.updated,d.error,COALESCE(d.attempts,0) AS attempts "
                "FROM waits w JOIN events e ON e.wait_id=w.id "
                "LEFT JOIN deliveries d ON d.event_id=e.id "
                "WHERE w.delivery_kind!='pull' AND (w.status='ready' OR d.event_id IS NOT NULL) "
                "AND (d.status IS NULL OR d.status NOT IN (?,?,?,?,?)) "
                "AND (w.delivery_kind!='webhook' OR COALESCE(d.status,'')!='uncertain') "
                "ORDER BY COALESCE(d.updated,0),e.created,e.id",
                FINISHED,
            ).fetchall()
            return [dict(row) for row in rows]

    def prepare(self, row: dict) -> None:
        if row["status"] is not None:
            return
        now = time.time()
        row.update(
            status="pending",
            created=now,
            updated=now,
            attempts=0,
            prompt=wakeup_message(row),
            message_id=str(
                uuid5(NAMESPACE_URL, f"slurm-monitor-trigger:{row['event_id']}")
            ),
        )
        with self.ledger.connect() as db:
            db.execute(
                "INSERT INTO deliveries(event_id,thread_id,message_id,prompt,status,"
                "created,updated,attempts) "
                "VALUES(?,?,?,?,?,?,?,0)",
                (
                    row["event_id"],
                    row["thread"],
                    row["message_id"],
                    row["prompt"],
                    "pending",
                    now,
                    now,
                ),
            )

    def record(
        self,
        row: dict,
        status: str,
        *,
        error: str | None = None,
        submission_id: str | None = None,
        turn_id: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        now = time.time()
        next_attempts = (row.get("attempts") or 0) + (1 if increment_attempts else 0)
        with self.ledger.connect() as db:
            db.execute(
                "UPDATE deliveries SET status=?,error=?,updated=?,"
                "submission_id=COALESCE(?,submission_id),turn_id=COALESCE(?,turn_id),"
                "attempts=? WHERE event_id=?",
                (
                    status,
                    error,
                    now,
                    submission_id,
                    turn_id,
                    next_attempts,
                    row["event_id"],
                ),
            )
        if (status, error) != (row["status"], row["error"]):
            logger.info(
                "event=%s thread=%s status=%s", row["event_id"], row["thread"], status
            )
        row.update(status=status, error=error, updated=now, attempts=next_attempts)

    def wait_status(self, row: dict) -> str:
        return self.ledger.wait_status(row["wait_id"])

    def deliver_webhook(self, row: dict) -> None:
        wait_status = self.wait_status(row)
        if wait_status != "ready":
            self.record(
                row,
                wait_status if wait_status == "cancelled" else "already_acknowledged",
            )
            return
        if row["status"] == "posting":
            self.record(
                row, "uncertain", error="Service restarted during webhook delivery"
            )
            return
        url = self.webhook_url
        if not url:
            self.record(row, "failed", error="webhook_url 缺失")
            return
        attempts = row.get("attempts") or 0
        if row["status"] == "retrying" and attempts > 0 and self.webhook_retry_base > 0:
            delay = min(300.0, self.webhook_retry_base * (2 ** max(0, attempts - 1)))
            if time.time() - (row.get("updated") or 0.0) < delay:
                return
        payload = json.loads(row["payload"]) | {"instruction": row["instruction"]}
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "slurm-monitor/1.0",
            "X-Slurm-Monitor-Event-Id": row["event_id"],
        }
        secret = self.webhook_secret
        if secret:
            digest = hmac.new(
                secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            headers["X-Slurm-Monitor-Signature"] = f"sha256={digest}"
        host = (urlsplit(url).hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            opener = build_opener(ProxyHandler({}))
        elif self.webhook_proxy:
            opener = build_opener(
                ProxyHandler({"http": self.webhook_proxy, "https": self.webhook_proxy})
            )
        else:
            opener = build_opener()
        request = Request(url, data=raw_body, headers=headers, method="POST")
        self.record(row, "posting")
        try:
            with opener.open(request, timeout=self.webhook_timeout) as response:
                status_code = getattr(response, "status", 200)
                if 200 <= status_code < 300:
                    self.record(row, "completed", increment_attempts=True)
                    return
                raise HTTPError(
                    url, status_code, f"HTTP {status_code}", response.headers, None
                )
        except HTTPError as error:
            detail = f"HTTP {error.code}"
            transient = error.code >= 500 or error.code in {408, 429}
            if transient and attempts + 1 < self.webhook_max_attempts:
                self.record(row, "retrying", error=detail, increment_attempts=True)
            else:
                self.record(row, "failed", error=detail, increment_attempts=True)
        except (URLError, OSError) as error:
            # The receiver may already have acted when the response was lost.
            self.record(
                row, "uncertain", error=str(error)[:600], increment_attempts=True
            )

    def advance(self, api: AppServerClient, row: dict) -> None:
        event_id, thread_id = row["event_id"], row["thread"]
        wait_status = self.wait_status(row)
        if row["status"] not in UNSENT:
            turn = api.find_turn(
                thread_id,
                row["message_id"],
                f"[slurm-monitor-event:{event_id}]",
                row["created"],
                row["turn_id"],
            )
            if turn:
                status = (
                    "accepted" if turn["status"] == "inProgress" else turn["status"]
                )
                self.record(row, status, turn_id=turn["id"])
                if status == "accepted" and thread_id not in api.resumed:
                    api.resume(thread_id)
                return
        queue = api.queue(thread_id)
        own = next(
            (
                item
                for item in queue
                if item["clientUserMessageId"] == row["message_id"]
            ),
            None,
        )
        if wait_status != "ready":
            if own:
                api.delete(thread_id, own["id"])
            self.record(
                row,
                wait_status if wait_status == "cancelled" else "already_acknowledged",
            )
            return
        if own is None and row["status"] not in UNSENT:
            self.record(row, "uncertain", error="队列和历史中暂未找到回执，不重复投递")
            return
        if api.thread(thread_id)["status"]["type"] == "active":
            self.record(row, "queued" if own else "waiting_for_idle")
            return
        if queue and (own is None or own["id"] != queue[0]["id"]):
            self.record(row, "queued" if own else "waiting_for_queue")
            return
        thread = api.resume(thread_id)
        if thread["status"]["type"] != "idle":
            self.record(row, "queued" if own else "waiting_for_idle")
            return
        if self.wait_status(row) != "ready":
            return
        if own is None:
            self.record(row, "queuing")
            own = api.enqueue(thread_id, row["message_id"], row["prompt"])
            self.record(row, "queued", submission_id=own["id"])
            return
        self.record(row, "starting", submission_id=own["id"])
        turn = api.start(thread_id, own["id"])
        status = "accepted" if turn["status"] == "inProgress" else turn["status"]
        self.record(row, status, turn_id=turn["id"])

    def tick(self) -> None:
        for row in self.candidates():
            self.prepare(row)
            if row["status"] in UNSENT and self.wait_status(row) != "ready":
                status = self.wait_status(row)
                self.record(
                    row, status if status == "cancelled" else "already_acknowledged"
                )
                continue
            delivery_kind = row.get("delivery_kind") or "codex"
            if delivery_kind == "webhook":
                self.deliver_webhook(row)
                continue
            if self.connect is None:
                self.record(row, "failed", error="此服务未配置 Codex 控制连接")
                continue
            try:
                if self.api is None:
                    self.api = self.connect()
                self.advance(self.api, row)
                if row["status"] in FINISHED and row["thread"] in self.api.resumed:
                    self.api.unsubscribe(row["thread"])
            except RpcError as error:
                self.record(row, row["status"], error=str(error)[:600])
            except (
                OSError,
                WebSocketException,
                ValueError,
                KeyError,
                TypeError,
            ) as error:
                # A lost response may hide a successful write, so preserve the pre-send phase.
                self.record(row, row["status"], error=str(error)[:600])
                self.close()
                continue

    def close(self) -> None:
        if self.api is not None:
            with suppress(Exception):
                self.api.close()
            self.api = None
