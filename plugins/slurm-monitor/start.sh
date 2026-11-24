#!/bin/sh
set -eu
exec "$HOME/.local/share/codex-slurm-monitor/bin/python" -I -m slurm_monitor.server
