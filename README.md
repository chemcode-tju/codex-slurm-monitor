# Codex Slurm Monitor

在 Codex 里说「帮我盯一下 8123 和 8124，都结束后在这里告诉我」。Codex 用作业号登记一次，后台检查 Slurm。条件满足后，同一聊天收到事件并启动一个回合。等待期间不调用模型。也可以在提交作业时直接说「算完告诉我」。

## 安装

在**运行目标 Codex 聊天的主机**上安装。需要 Python 3.11 或更高版本、支持 `codex plugin add` 的 Codex CLI，以及能访问 Slurm 的 `squeue` 和 `sacct`。集群用登录节点的 systemd 用户服务，macOS 用当前用户的 LaunchAgent。远程 Codex 聊天通常在远端安装。

```sh
gh repo clone liyang-tju/codex-slurm-monitor
cd codex-slurm-monitor
python3 install.py
```

安装器建立独立 Python 环境、安装一个包含 Skill 的 Codex 插件、启动后台服务，并输出 `worker_running` 和 `socket_present`。打开新聊天即可使用。两项都为 `true` 表示观察进程已启动且控制套接字存在，实际投递回执可在聊天里查询。更新时在仓库执行 `git pull`，再运行安装器。它保留个人配置和已登记的作业。

如果 `socket_present` 为 `false`，检查当前聊天所属 Codex App Server 的 Unix WebSocket 地址。默认使用 `~/.codex/app-server-control/app-server-control.sock`。使用 Codex 托管 daemon 的主机可用 `codex app-server daemon start` 启动它。自定义地址填入下面的 `control_socket`。聊天所在的服务必须支持 `thread/queue/add` 和 `thread/queue/start`。只有 stdio 连接的桌面会话可使用手动查询或 Webhook。

## 使用

| 对 Codex 说 | 行为 |
| --- | --- |
| 帮我盯着 8123 | 作业进入终态后回本聊天提醒 |
| 8123 和 8124 任意一个结束就告诉我 | 首个作业进入终态时提醒一次 |
| 把刚才的提醒改成“列出退出码和日志位置” | 修改尚在等待的这组作业的提醒词 |
| 看看我在等哪些作业 | 显示作业号、调度状态、退出码和投递状态 |
| 打开作业监控面板 | 在聊天旁查看作业并刷新 |
| 取消刚才的提醒 | 取消订阅，Slurm 作业继续运行 |

不同聊天可以订阅同一作业。同一聊天重复登记相同主机、作业组、条件和投递方式，会得到原订阅。数组任务请给出具体成员（例如 `8123_4`）或其 allocation ID；整组数组可传入各成员编号。每次订阅只触发一次。

## 自定义提醒

默认提醒只有结构化事件，包含作业号、状态、退出码和事件 ID。直接在聊天里说要如何处理，Codex 会将你的原话作为这组任务的提醒词。也可在 `~/.config/codex-slurm-monitor/config.toml` 设置个人通用文字：

```toml
prompt = ""
```

每组显式设置的文字优先，显式空字符串只发送事件。全局文字在登记时保存，之后修改全局文字只影响新登记。Skill 指引在插件的 [SKILL.md](plugins/slurm-monitor/skills/slurm-monitor/SKILL.md)，需要改变登记习惯时可在源码中编辑并重新安装。

## 可选设置

其余配置见 [config.toml](config.toml)。默认每 30 秒查看一次本机 Slurm。要在一个 Codex 主机上监控多个集群，在 `ssh_hosts` 中填写已有的 SSH 别名，聊天中说清在哪个集群。例如设为 `["cluster-a"]` 后可说「盯一下 cluster-a 的 8123」。SSH 需要能免交互执行队列查询。更改主机、轮询间隔、套接字或 Webhook 后重启后台。

默认投递方式为 `codex`。用户也可以选择 `pull` 手动查，或选择 `webhook` 向个人配置中的 `webhook_url` 发送 JSON。可设置 `webhook_secret` 对原始请求体作 HMAC-SHA256 签名，签名头为 `X-Slurm-Monitor-Signature: sha256=…`。Webhook 对明确返回的暂时性 HTTP 错误进行有限次数重试。发送后连接中断时标记为 `uncertain`，接收方应按 `event_id` 去重。URL、签名密钥和代理地址存放在个人配置文件中。

## 运行与恢复

- **Linux** 查看 `systemctl --user status codex-slurm-monitor`，重启用 `systemctl --user restart codex-slurm-monitor`。需要退出 SSH 后继续运行时，按集群规则启用用户 linger。日志用 `journalctl --user -u codex-slurm-monitor` 查看。
- **macOS** 重启用 `launchctl kickstart -k "gui/$(id -u)/org.codex.slurm-monitor"`。日志在 `~/.local/share/codex-slurm-monitor/worker.log`。
- **无用户服务管理器** 将安装器顶部的 `manage_service` 改为 `False`，用现有进程管理器运行安装器打印的 worker 命令。
- **离线节点** 可在相同系统、CPU 架构和 Python 版本的联网机器准备 wheel 目录（包括本包依赖、setuptools 和 wheel），再设置 `PIP_NO_INDEX=1` 与 `PIP_FIND_LINKS` 指向该目录运行安装器。

MCP 工具和 worker 共用当前用户的 SQLite 数据库。队列消失后要等 `sacct` 返回匹配记录才产生终态事件，记账延迟或查询错误会显示为 `UNKNOWN`。调度终态和退出码描述作业执行情况，科研收敛需查看实际输出。[Slurm 状态说明](https://slurm.schedmd.com/job_state_codes.html) · [sacct 字段](https://slurm.schedmd.com/sacct.html)

事件、投递回执和用户确认分别持久化。聊天忙碌时等待，重启后用消息 ID、队列和聊天历史核对投递。若回执丢失且无法确认是否送达，会显示 `uncertain`，留给用户核查。服务没有提交、重跑或取消 Slurm 作业的接口。

## 卸载

Linux 先执行 `systemctl --user disable --now codex-slurm-monitor`，macOS 执行 `launchctl bootout "gui/$(id -u)/org.codex.slurm-monitor"`。随后执行：

```sh
codex plugin remove slurm-monitor@slurm-tools
codex plugin marketplace remove slurm-tools
```

个人配置在 `~/.config/codex-slurm-monitor/`，运行环境和历史记录在 `~/.local/share/codex-slurm-monitor/`。同一用户在集群各节点共享 home 时，应由一台选定的主机运行该 worker。迁移旧服务时，在新插件里登记仍需追踪的作业，并取消旧服务中对应的提醒，以免两套同时投递。

## 测试

在已安装依赖和 pytest 的环境中，从仓库根目录运行 `python -m pytest -q data/tests`。测试使用临时数据库和隔离的调度器、聊天及 Webhook 响应。
