"""A local control connection; none of the read operations invoke a model."""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, socket_path: Path) -> None:
        from websockets.sync.client import unix_connect

        self.socket = unix_connect(
            str(socket_path), open_timeout=10, max_size=16 * 1024 * 1024
        )
        self.identifier = 0
        self.resumed: set[str] = set()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "slurm_monitor", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.socket.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.socket.close()
            raise

    def request(self, method: str, params: dict | None) -> dict:
        self.identifier += 1
        identifier = self.identifier
        self.socket.send(
            json.dumps({"id": identifier, "method": method, "params": params})
        )
        deadline = time.monotonic() + 30
        while True:
            message = json.loads(
                self.socket.recv(timeout=max(0, deadline - time.monotonic()))
            )
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise RpcError(str(message["error"]))
                return message["result"]
            if "id" in message and "method" in message:
                # Preserve the original chat's permissions. Never approve requests unattended.
                logger.warning(
                    "User action requested by app-server: %s", message["method"]
                )

    def resume(self, thread_id: str) -> dict:
        result = self.request(
            "thread/resume", {"threadId": thread_id, "excludeTurns": True}
        )
        self.resumed.add(thread_id)
        return result["thread"]

    def thread(self, thread_id: str) -> dict:
        return self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )["thread"]

    def queue(self, thread_id: str) -> list[dict]:
        items, cursor = [], None
        while True:
            page = self.request(
                "thread/queue/list",
                {
                    "threadId": thread_id,
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            items.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return items

    def find_turn(
        self,
        thread_id: str,
        message_id: str,
        marker: str,
        since: float,
        turn_id: str | None,
    ) -> dict | None:
        """Reconcile an ambiguous reply against persisted history, without resending."""
        cursor = None
        while True:
            page = self.request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "itemsView": "notLoaded",
                    "limit": 25,
                    "sortDirection": "desc",
                    "cursor": cursor,
                },
            )
            for turn in page["data"]:
                if turn["id"] == turn_id:
                    return turn
                if turn.get("startedAt") is not None and turn["startedAt"] < since - 5:
                    return None
                item_cursor = None
                while True:
                    items = self.request(
                        "thread/items/list",
                        {
                            "threadId": thread_id,
                            "turnId": turn["id"],
                            "limit": 100,
                            "deferOutput": {
                                "commandExecution": True,
                                "mcpToolCall": True,
                            },
                            "sortDirection": "asc",
                            "cursor": item_cursor,
                        },
                    )
                    for entry in items["data"]:
                        item = entry.get("item", entry)
                        if item.get("type") == "userMessage" and (
                            item.get("id") == message_id
                            or any(
                                value.get("text", "").startswith(marker + "\n")
                                for value in item.get("content", [])
                            )
                        ):
                            return turn
                    item_cursor = items.get("nextCursor")
                    if not item_cursor:
                        break
            cursor = page.get("nextCursor")
            if not cursor:
                return None

    def enqueue(self, thread_id: str, message_id: str, prompt: str) -> dict:
        return self.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "clientUserMessageId": message_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )["queuedSubmission"]

    def start(self, thread_id: str, submission_id: str) -> dict:
        # Starting this exact queued item avoids steering an already active turn.
        return self.request(
            "thread/queue/start",
            {
                "threadId": thread_id,
                "queuedSubmissionId": submission_id,
            },
        )["turn"]

    def close(self) -> None:
        self.socket.close()

    def delete(self, thread_id: str, submission_id: str) -> None:
        self.request(
            "thread/queue/delete",
            {"threadId": thread_id, "queuedSubmissionId": submission_id},
        )

    def unsubscribe(self, thread_id: str) -> None:
        self.request("thread/unsubscribe", {"threadId": thread_id})
        self.resumed.discard(thread_id)
