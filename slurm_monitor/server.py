"""Codex MCP stdio entry point. The daemon owns all scheduler and delivery I/O."""

import json
import sqlite3
import sys
from pathlib import Path

from .daemon import worker_running
from .ledger import MonitorLedger
from .models import Acknowledge, EditPrompt, Input, ListWatches, Read, Watch
from .settings import private_state, settings

PANEL = "ui://slurm-monitor/jobs.html"
TOOLS = {
    "monitor_watch": (
        Watch,
        "Watch existing Slurm IDs together and notify their originating chat.",
    ),
    "monitor_list": (
        ListWatches,
        "List watches and their jobs, optionally scoped to a chat.",
    ),
    "monitor_read": (
        Read,
        "Read one watch, its job observations, event and delivery receipt.",
    ),
    "monitor_cancel": (Read, "Cancel a notification subscription. Jobs keep running."),
    "monitor_acknowledge": (Acknowledge, "Mark this exact event as handled."),
    "monitor_set_prompt": (
        EditPrompt,
        "Change the reminder text for a waiting subscription.",
    ),
    "monitor_status": (Input, "Check observer and Codex socket availability."),
    "open_monitor_panel": (ListWatches, "Open the job monitor panel."),
}


def catalog() -> list[dict]:
    tools = []
    for name, (model, description) in TOOLS.items():
        descriptor = {
            "name": name,
            "description": description,
            "inputSchema": model.model_json_schema(),
            "annotations": {
                "readOnlyHint": name
                in {
                    "monitor_list",
                    "monitor_read",
                    "monitor_status",
                    "open_monitor_panel",
                }
            },
        }
        if name == "open_monitor_panel":
            descriptor["_meta"] = {
                "ui": {"resourceUri": PANEL, "visibility": ["model", "app"]},
                "openai/ui": {"entrypoints": [{"type": "thread"}]},
            }
        tools.append(descriptor)
    return tools


def call(name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        raise ValueError("Unknown monitor tool")
    model, _ = TOOLS[name]
    args = model.model_validate(arguments)
    directory = private_state()
    config = settings()
    running = worker_running(directory)
    socket = config.socket_path
    health = {
        "worker_running": running,
        "socket_present": bool(socket and socket.is_socket()),
        "local_host": config.local_host,
        "ssh_hosts": config.ssh_hosts,
    }
    if name == "monitor_status":
        return health
    ledger = MonitorLedger(directory / "monitor.sqlite3")
    if isinstance(args, Watch):
        host = args.host or config.local_host
        if host != config.local_host and host not in config.ssh_hosts:
            raise ValueError("Host is not configured in ssh_hosts")
        if args.delivery == "codex" and socket is None:
            raise ValueError(
                "Set control_socket for Codex notifications or choose pull delivery"
            )
        if args.delivery == "webhook" and not config.webhook_url:
            raise ValueError("Set webhook_url in the private configuration first")
        prompt = config.prompt if args.prompt is None else args.prompt
        return ledger.register(args, host, prompt) | health
    if isinstance(args, ListWatches):
        thread = str(args.thread_id) if args.thread_id else None
        return ledger.list_waits(thread, args.offset) | {"thread_id": thread}
    wait_id = str(args.wait_id)
    if isinstance(args, EditPrompt):
        return ledger.set_prompt(wait_id, args.prompt)
    if isinstance(args, Acknowledge):
        return ledger.control(wait_id, str(args.event_id))
    if name == "monitor_cancel":
        return ledger.control(wait_id)
    return ledger.read(wait_id)


def dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        version = params.get("protocolVersion")
        return {
            "protocolVersion": version if version in versions else versions[-1],
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "slurm-monitor", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": catalog()}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    resource = {
        "name": "Slurm jobs",
        "uri": PANEL,
        "mimeType": "text/html;profile=mcp-app",
    }
    if method == "resources/list":
        return {"resources": [resource]}
    if method == "resources/read" and params.get("uri") == PANEL:
        return {
            "contents": [
                resource
                | {
                    "text": Path(__file__).with_name("panel.html").read_text(),
                    "_meta": {
                        "ui": {"csp": {"connectDomains": [], "resourceDomains": []}}
                    },
                }
            ]
        }
    if method == "tools/call":
        try:
            result = call(params["name"], params.get("arguments", {}))
        except (ValueError, OSError) as error:
            return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
        return {
            "structuredContent": result,
            "content": [{"type": "text", "text": json.dumps(result)}],
        }
    raise LookupError(method)


def main() -> None:
    for line in sys.stdin:
        message_id = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise TypeError("Expected a JSON-RPC request")
            if "id" not in message:
                continue
            message_id = message["id"]
            reply = {"result": dispatch(message["method"], message.get("params", {}))}
        except (LookupError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            reply = {
                "error": {
                    "code": -32601 if isinstance(error, LookupError) else -32603,
                    "message": str(error),
                }
            }
        print(json.dumps({"jsonrpc": "2.0", "id": message_id} | reply), flush=True)


if __name__ == "__main__":
    main()
