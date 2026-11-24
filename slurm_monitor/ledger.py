"""A subscription, its terminal snapshot and its delivery have distinct lifecycles."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .models import Watch

TERMINAL = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)


class MonitorLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS waits (
                    id TEXT PRIMARY KEY, thread TEXT NOT NULL, host TEXT NOT NULL,
                    mode TEXT NOT NULL, delivery_kind TEXT NOT NULL, instruction TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    wait_id TEXT NOT NULL REFERENCES waits(id), host TEXT NOT NULL,
                    scheduler_id TEXT NOT NULL, state TEXT NOT NULL, exit_code TEXT,
                    observed REAL, error TEXT, terminal INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(wait_id, scheduler_id));
                CREATE INDEX IF NOT EXISTS jobs_active ON jobs(host,scheduler_id,terminal);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, wait_id TEXT NOT NULL UNIQUE REFERENCES waits(id),
                    payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY REFERENCES events(id), thread_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    submission_id TEXT, turn_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    error TEXT, attempts INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def register(self, request: Watch, host: str, instruction: str) -> dict:
        jobs = sorted(set(request.job_ids))
        key = [str(request.thread_id), host, jobs, request.mode, request.delivery]
        identifier = str(uuid5(NAMESPACE_URL, "slurm-monitor:" + json.dumps(key)))
        with self.connect() as db:
            old = db.execute(
                "SELECT instruction FROM waits WHERE id=?", (identifier,)
            ).fetchone()
            if old and request.prompt is not None and old["instruction"] != instruction:
                raise ValueError(
                    "Already registered. Use monitor_set_prompt to edit the reminder."
                )
            if not old:
                db.execute(
                    "INSERT INTO waits VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        str(request.thread_id),
                        host,
                        request.mode,
                        request.delivery,
                        instruction,
                        "waiting",
                        time.time(),
                    ),
                )
                db.executemany(
                    "INSERT INTO jobs(wait_id,host,scheduler_id,state) VALUES(?,?,?,?)",
                    [(identifier, host, job, "UNKNOWN") for job in jobs],
                )
        return self.read(identifier)

    def active_jobs(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT j.host,j.scheduler_id FROM jobs j JOIN waits w ON w.id=j.wait_id "
                    "WHERE w.status='waiting' AND j.terminal=0 ORDER BY j.host,j.scheduler_id"
                )
            ]

    @staticmethod
    def _jobs(db: sqlite3.Connection, wait_id: str) -> list[dict]:
        return [
            dict(row)
            for row in db.execute(
                "SELECT scheduler_id,state,exit_code,observed,error FROM jobs WHERE wait_id=? "
                "ORDER BY scheduler_id",
                (wait_id,),
            )
        ]

    def observe(
        self,
        host: str,
        job_id: str,
        state: str,
        exit_code: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self.connect() as db:
            rows = db.execute(
                "SELECT w.* FROM waits w JOIN jobs j ON w.id=j.wait_id WHERE "
                "w.status='waiting' AND j.terminal=0 AND j.host=? AND j.scheduler_id=?",
                (host, job_id),
            ).fetchall()
            for wait in rows:
                db.execute(
                    "UPDATE jobs SET state=?,exit_code=?,observed=?,error=?,terminal=? "
                    "WHERE wait_id=? AND scheduler_id=?",
                    (
                        state,
                        exit_code,
                        now,
                        error,
                        state in TERMINAL,
                        wait["id"],
                        job_id,
                    ),
                )
                if state not in TERMINAL:
                    continue
                jobs = self._jobs(db, wait["id"])
                if wait["mode"] == "all" and any(
                    job["state"] not in TERMINAL for job in jobs
                ):
                    continue
                event_id = str(uuid5(NAMESPACE_URL, f"slurm-event:{wait['id']}"))
                payload = {
                    "event_id": event_id,
                    "wait_id": wait["id"],
                    "kind": "jobs.terminal",
                    "thread_id": wait["thread"],
                    "mode": wait["mode"],
                    "host": host,
                    "jobs": jobs,
                }
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?)",
                    (
                        event_id,
                        wait["id"],
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
                db.execute("UPDATE waits SET status='ready' WHERE id=?", (wait["id"],))

    def read(self, wait_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT w.*,e.payload,d.status AS delivery_status,d.error AS delivery_error,"
                "d.turn_id FROM waits w LEFT JOIN events e ON w.id=e.wait_id "
                "LEFT JOIN deliveries d ON e.id=d.event_id WHERE w.id=?",
                (wait_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown wait ID")
            result = dict(row)
            payload = result.pop("payload")
            result["event"] = json.loads(payload) if payload else None
            result["jobs"] = self._jobs(db, wait_id)
            return result

    def list_waits(self, thread: str | None, offset: int) -> dict:
        with self.connect() as db:
            query = "SELECT id FROM waits" + (" WHERE thread=?" if thread else "")
            params = (thread, offset) if thread else (offset,)
            rows = db.execute(
                query + " ORDER BY created DESC,id LIMIT 51 OFFSET ?", params
            ).fetchall()
        return {
            "waits": [self.read(row["id"]) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def wait_status(self, wait_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM waits WHERE id=?", (wait_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown wait ID")
            return row["status"]

    def set_prompt(self, wait_id: str, prompt: str) -> dict:
        with self.connect() as db:
            if not db.execute(
                "UPDATE waits SET instruction=? WHERE id=? AND status='waiting'",
                (prompt, wait_id),
            ).rowcount:
                raise ValueError("Prompt can only be changed while waiting")
        return self.read(wait_id)

    def control(self, wait_id: str, event_id: str | None = None) -> dict:
        with self.connect() as db:
            if event_id is None:
                db.execute(
                    "UPDATE waits SET status='cancelled' WHERE id=? AND status!='acknowledged'",
                    (wait_id,),
                )
            elif not db.execute(
                "UPDATE waits SET status='acknowledged' WHERE id=? AND status!='cancelled' "
                "AND EXISTS(SELECT 1 FROM events WHERE id=? AND wait_id=?)",
                (wait_id, event_id, wait_id),
            ).rowcount:
                raise ValueError("Event does not match an active wait")
        return self.read(wait_id)
