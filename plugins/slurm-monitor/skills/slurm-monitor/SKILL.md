---
name: slurm-monitor
description: Monitor Slurm jobs submitted or adopted in Codex. Register job IDs for a background notification, inspect delivery, or cancel a watch.
---

Once Slurm returns job IDs for work in this chat, or the user asks to monitor existing jobs, call `monitor_watch` with those IDs as strings. Skip registration if the user opted out. Use the current chat's persistent UUID from the interaction context or read `CODEX_THREAD_ID` in its shell. Never guess the ID or select the latest chat. Treat an absent ID as missing routing information. Users normally only supply job numbers.

Use one call per group and host. The default `all` waits for all jobs. Select `any` for one notification at the first terminal job. Omit `host` for the configured local scheduler. Pass the user's reminder wording as `prompt` when supplied, otherwise omit it. For a requested reminder change use `monitor_set_prompt` before the event fires. Use `pull` for manual lookup and `webhook` only when requested.

Report the registered IDs and trigger condition. Check the returned worker and socket flags before promising automatic notification. After registration the background worker handles polling. Use `monitor_list` with the chat ID, `monitor_read`, or `open_monitor_panel` for status. Use `monitor_cancel` to stop a watch.

On a terminal event use the user's instructions and the original chat context. Distinguish scheduler state and exit code from scientific convergence. After handling it, acknowledge its exact wait and event IDs with `monitor_acknowledge`. If delivery is uncertain, inspect the existing receipt and chat history before taking action.
