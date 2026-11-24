"""Install the worker and Codex plugin for the current Unix user."""

# Configuration
manage_service = True

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = Path.home() / ".local/share/codex-slurm-monitor"
CONFIG = Path.home() / ".config/codex-slurm-monitor/config.toml"
LABEL = "org.codex.slurm-monitor"


def run(command: list[str], **options: object) -> None:
    subprocess.run(command, check=True, **options)


def install_worker(python: Path) -> None:
    args = [str(python), "-I", "-m", "slurm_monitor.daemon"]
    search_path = os.environ.get("PATH", os.defpath)
    if platform.system() == "Linux":
        unit = Path.home() / ".config/systemd/user/codex-slurm-monitor.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        # systemd expands percent specifiers even inside quoted fields.
        command = " ".join(
            json.dumps(arg.replace("%", "%%"), ensure_ascii=False) for arg in args
        )
        path = json.dumps("PATH=" + search_path.replace("%", "%%"), ensure_ascii=False)
        unit.write_text(f"""[Unit]
Description=Codex Slurm monitor

[Service]
ExecStart={command}
Environment={path}
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
""")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", unit.name])
        run(["systemctl", "--user", "restart", unit.name])
        run(["systemctl", "--user", "is-active", "--quiet", unit.name])
    else:
        service_file = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        service_file.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LABEL}"],
            capture_output=True,
            check=False,
        )
        service_file.write_bytes(
            plistlib.dumps(
                {
                    "Label": LABEL,
                    "ProgramArguments": args,
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "EnvironmentVariables": {"PATH": search_path},
                    "StandardErrorPath": str(RUNTIME / "worker.log"),
                }
            )
        )
        run(["launchctl", "bootstrap", domain, str(service_file)])


def main() -> None:
    if sys.version_info < (3, 11) or platform.system() not in {"Linux", "Darwin"}:
        raise SystemExit("Use Python 3.11+ on Linux or macOS.")
    if not shutil.which("codex"):
        raise SystemExit("Install Codex CLI with plugin support first.")
    if os.environ.get("CODEX_HOME"):
        Path(os.environ["CODEX_HOME"]).expanduser().mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
    run(["codex", "plugin", "add", "--help"], stdout=subprocess.DEVNULL)
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    python = RUNTIME / "bin/python"
    if not python.is_file():
        run([sys.executable, "-m", "venv", str(RUNTIME)])
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(ROOT)])
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        contents = (ROOT / "config.toml").read_text()
        if os.environ.get("CODEX_HOME"):
            socket = (
                Path(os.environ["CODEX_HOME"]).resolve()
                / "app-server-control/app-server-control.sock"
            )
            contents = contents.replace(
                '"~/.codex/app-server-control/app-server-control.sock"',
                json.dumps(str(socket), ensure_ascii=False),
            )
        with CONFIG.open("x") as stream:
            stream.write(contents)
    run(
        [
            str(python),
            "-I",
            "-c",
            "from slurm_monitor.settings import settings; settings()",
        ]
    )
    # Store a stable marketplace path so the source checkout can be moved after installation.
    marketplace = RUNTIME / "marketplace"
    for folder in [".agents/plugins", "plugins/slurm-monitor"]:
        shutil.copytree(ROOT / folder, marketplace / folder, dirs_exist_ok=True)
    run(["codex", "plugin", "marketplace", "add", str(marketplace)])
    run(["codex", "plugin", "add", "slurm-monitor@slurm-tools"])
    if manage_service:
        install_worker(python)
    else:
        print(f"Start the worker with {python} -I -m slurm_monitor.daemon")
    print(f"Configuration: {CONFIG}")
    run(
        [
            str(python),
            "-I",
            "-c",
            "from slurm_monitor.server import call; print(call('monitor_status', {}))",
        ]
    )
    print("Open a new Codex chat to load Slurm Monitor.")


if __name__ == "__main__":
    main()
