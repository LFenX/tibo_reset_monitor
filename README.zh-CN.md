<p align="center">
  <img src="docs/readme-cover.png" alt="Tibo, patron saint of resets, pressing RESET while programmers await the signal" width="100%">
</p>

<h1 align="center">tibo-reset-monitor</h1>

<p align="center">
  <strong>重置有消息，自动通知你。</strong><br>
  追踪 Tibo 的 Codex / ChatGPT 额度重置动态，推送到飞书、Telegram，或通过 Webhook 接入自定义通知渠道。
</p>

<p align="center">
  <a href="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml"><img src="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI on main"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-D4AF37" alt="MIT License"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/English-3776AB" alt="English"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#通知渠道">通知渠道</a> ·
  <a href="#部署">部署</a> ·
  <a href="#常见问题">常见问题</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a>
</p>

---

## 功能

| 能力 | 说明 |
| --- | --- |
| 重置动态追踪 | 轮询 AIHOT 公开接口，识别预告、确认、补充和撤回 |
| 多渠道通知 | 飞书与 Telegram 支持群聊、个人；Webhook 支持自定义接收端 |
| 独立投递 | SQLite 保存状态，按目标去重和重试，一个目标失败不阻塞其他目标 |
| 灵活运行 | 单次执行、常驻进程、cron、systemd、Windows 任务计划程序或 Docker |

> 首次成功拉取只建立基线，不推送历史帖子。项目跟踪的是公开公告；“确认时间”是来源帖子的发布时间，账户额度是否到账以产品内显示为准。

## 快速开始

需要 **Python 3.10+** 和 Git。核心与内置通知渠道仅使用 Python 标准库。

### 1. 安装

以下命令适用于 Linux / macOS；Windows 命令见下方折叠说明。

```bash
git clone https://github.com/LFenX/tibo_reset_monitor.git
cd tibo_reset_monitor
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config.example.json config.json
cp .env.example .env
```

### 2. 配置通知

编辑本地 `config.json`（从[示例配置](config.example.json)复制）：

- 只启用要使用的渠道：将其余渠道的 `enabled` 设为 `false`。
- 替换 `targets` 中的占位 ID，并删除不用的示例目标。
- 在 `.env` 中填入启用渠道的密钥。程序从环境变量读取密钥，**不会自动加载 `.env`**。

渠道获取方式见[通知渠道](#通知渠道)。示例配置中的 App ID、群 ID 和用户 ID 均为占位值；真实密钥不写进 JSON，`config.json` 和 `.env` 不提交到仓库。

### 3. 启动

```bash
set -a
. ./.env
set +a

tibo-reset-monitor --config config.json poll
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json run
```

`poll` 执行一轮，`status` 查看状态，`run` 按配置间隔持续运行（默认 10 分钟），按 `Ctrl+C` 退出。全局参数 `--config` 必须放在子命令前。

<details>
<summary><strong>Windows / PowerShell</strong></summary>

```powershell
git clone https://github.com/LFenX/tibo_reset_monitor.git
cd tibo_reset_monitor
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item config.example.json config.json
Copy-Item .env.example .env
```

按上面的要求编辑 `config.json`。在当前 PowerShell 会话中设置启用渠道所需的变量，再运行：

```powershell
$env:TIBO_FEISHU_APP_SECRET = 'replace-with-feishu-app-secret'
$env:TIBO_TELEGRAM_BOT_TOKEN = 'replace-with-telegram-bot-token'

tibo-reset-monitor --config config.json poll
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json run
```

Webhook 用户还需设置目标 `url_env` 指向的环境变量。这里只需要设置已启用渠道的密钥。

</details>

## 通知渠道

在 [config.example.json](config.example.json) 中选择渠道，同一渠道可配置多个群聊或个人目标。

| 渠道 | 目标 | 凭据 |
| --- | --- | --- |
| 飞书 / Lark | 群聊、个人 | App ID + `TIBO_FEISHU_APP_SECRET` |
| Telegram | 群组、频道、个人 | `TIBO_TELEGRAM_BOT_TOKEN` |
| Webhook | 自定义 HTTPS 接收端 | `url_env` 指向的环境变量 |

<details>
<summary><strong>飞书 / Lark：创建应用与获取目标 ID</strong></summary>

1. 在[飞书开放平台](https://open.feishu.cn/app)或 [Lark](https://open.larksuite.com/app) 创建企业自建应用，启用机器人。
2. 开通 `im:message:send_as_bot` 权限，创建版本并在企业内发布。
3. 将 App ID 填入 `app_id`，App Secret 放入 `TIBO_FEISHU_APP_SECRET`。
4. **群聊**：将机器人加入群聊，填写群 `chat_id`（通常以 `oc_` 开头），`receive_id_type` 使用 `chat_id`。应用可用 tenant token 调用 `GET /open-apis/im/v1/chats` 查询机器人所在群。
5. **个人**：`receive_id_type` 使用 `open_id`、`user_id` 或 `email`，并填入匹配的 `receive_id`。使用邮箱时，应用还需拥有按邮箱解析租户用户的权限。

国际版 Lark 将 `api_base` 设置为 `https://open.larksuite.com`。程序直接调用 Open API，无需安装 `lark-cli`。通知采用 Card JSON 2.0，包含阶段、作者、正文和原帖链接。

</details>

<details>
<summary><strong>Telegram：创建机器人与获取 chat_id</strong></summary>

1. 向 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，将 token 放入 `TIBO_TELEGRAM_BOT_TOKEN`。
2. **个人**：用户先打开机器人并点击 Start，再查询更新：

   ```bash
   curl -s "https://api.telegram.org/bot${TIBO_TELEGRAM_BOT_TOKEN}/getUpdates"
   ```

   将 `message.chat.id` 的正整数值以字符串填入 `chat_id`，`kind` 使用 `personal`。
3. **群组 / 频道**：将机器人加入并授予发送权限，让它收到一条消息，再查询 `getUpdates`。群 ID 是负数，超级群通常以 `-100` 开头；公开频道也可使用 `@channelusername`，`kind` 使用 `group`。
4. 隐私模式下，需向机器人发送它可见的消息（如 @ 机器人）。获得 ID 后可恢复隐私模式，无需持续读取聊天记录。

403（被拉黑或无权发言）与 400 `chat not found` 会记录为永久失败；429 按 `retry_after` 退避。

</details>

<details>
<summary><strong>Webhook：连接自定义接收端</strong></summary>

启用 `channels.webhook`，为每个目标填写 `name`、`kind` 和 `url_env`。后者是环境变量名称，变量值为实际的 HTTPS 地址：

```bash
export TIBO_WEBHOOK_EXAMPLE_URL='https://example.com/your-webhook'
```

请求携带 `Idempotency-Key`。接收端可据此去重；不要将实际地址直接写入配置文件。

</details>

## 配置与命令

完整配置见 [config.example.json](config.example.json)，密钥变量示例见 [.env.example](.env.example)。

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `source.poll_interval_seconds` | `600`，最小 `60`；控制 `run` 的轮询间隔 |
| `source.url` | AIHOT recent 接口，必须使用 HTTPS |
| `state.path` | `state/monitor.sqlite3`；可用 `TIBO_STATE_PATH` 覆盖 |
| `logging.level` / `logging.file` | 示例配置为 `INFO` / `logs/monitor.log` |
| `channels.*.enabled` | 是否启用该渠道 |
| `channels.*.targets` | 通知目标列表；每个目标包含 `name` 与 `kind` |

命令默认读取当前目录的 `config.json`；指定其他文件时使用 `tibo-reset-monitor --config PATH <command>`。

| 子命令 | 用途 |
| --- | --- |
| `poll` | 拉取一轮并投递到期通知 |
| `run` | 按配置间隔持续运行 |
| `status --json` | 查看基线、快照和 outbox 计数 |
| `list --limit 20` | 查看通知及各目标的投递状态；支持 `--json` |
| `send-test --channel telegram --target example-person` | 向指定目标发送测试消息 |
| `manual --channel feishu --target example-group --event 3` | 以新幂等键重发已保存的通知 |
| `manual --channel telegram --target example-person --text "hello" --preview` | 预览自定义消息，不发送 |

示例中的目标名称需与配置一致；事件 ID 从 `list` 获取。`poll` 拉取失败时退出码为 `1`，配置错误为 `2`；单个目标投递失败仍为 `0`，失败状态可通过 `list` 查看。

## 部署

选择一种运行方式。使用同一个状态库时，只运行一个 `poll` / `run` 进程。

<details>
<summary><strong>Docker</strong></summary>

先完成本地 `config.json` 和 `.env` 配置，并将 `logging.file` 设为 `/data/monitor.log`，使容器用户能在挂载卷中写日志。

```bash
docker build -t tibo-reset-monitor .
docker run --rm -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor
```

镜像默认执行 `run`，通过 `TIBO_STATE_PATH` 将数据库写入 `/data/monitor.sqlite3`，命名卷 `tibo-state` 保存状态。

</details>

<details>
<summary><strong>Linux / macOS：cron</strong></summary>

假设项目位于 `/opt/tibo-reset-monitor`，已安装虚拟环境并填好 `.env`：

```cron
*/10 * * * * cd /opt/tibo-reset-monitor && set -a && . ./.env && set +a && .venv/bin/python -m tibo_reset_monitor --config /opt/tibo-reset-monitor/config.json poll
```

每 10 分钟执行一轮。相对状态路径以项目目录为基准。

</details>

<details>
<summary><strong>Linux：systemd</strong></summary>

单元文件见 [service](deploy/systemd/tibo-reset-monitor.service) 和 [timer](deploy/systemd/tibo-reset-monitor.timer)。安装前，将示例 service 中的 `ExecStart` 调整为以下参数顺序：

```ini
ExecStart=/opt/tibo-reset-monitor/.venv/bin/python -m tibo_reset_monitor --config /opt/tibo-reset-monitor/config.json poll
```

确认 `WorkingDirectory`、`EnvironmentFile` 和安装路径匹配，再安装：

```bash
sudo cp deploy/systemd/tibo-reset-monitor.service deploy/systemd/tibo-reset-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tibo-reset-monitor.timer
```

service 每次执行一次 `poll`，timer 每 10 分钟触发。使用常驻 `run` 时，不再同时启用 timer。

</details>

<details>
<summary><strong>Windows：任务计划程序</strong></summary>

假设项目位于 `C:\tibo_reset_monitor`，创建每 10 分钟运行一次的任务，操作项设置如下：

| 字段 | 值 |
| --- | --- |
| 程序或脚本 | `C:\tibo_reset_monitor\.venv\Scripts\python.exe` |
| 添加参数 | `-m tibo_reset_monitor --config C:\tibo_reset_monitor\config.json poll` |
| 起始于 | `C:\tibo_reset_monitor` |

为执行任务的账户设置启用渠道所需的环境变量。任务不会自动加载 `.env`；固定工作目录可避免相对状态路径漂移。

</details>

## 常见问题

<details>
<summary><strong>启动后没收到通知，是配置有问题吗？</strong></summary>

首次成功拉取只记录基线，不补发过去 7 天的帖子。新增目标也只接收加入后发现的动态。使用 `status` 确认拉取状态，用 `send-test` 检查渠道；确需重发旧通知时使用 `manual --event`。

</details>

<details>
<summary><strong>群聊收到了，个人消息没收到，怎么排查？</strong></summary>

使用 `list` 查看各目标状态。Telegram 用户需先点击 Start；飞书个人 ID 必须与 `receive_id_type` 匹配，群聊中需有机器人。一个目标失败不影响其他目标。

</details>

<details>
<summary><strong>去重、重试与撤回如何处理？</strong></summary>

- 以“帖子 ID + 阶段”识别新动态，同一帖子同时出现在事件列表和活动流时只通知一次。无关闲聊 `related` 与故障提示 `outage` 不发送。
- 每个事件与目标有独立的 outbox 记录、幂等键、回执和重试状态。明确失败按退避策略重试。
- 超时等结果不确定的投递继续使用同一个幂等键；从首次不确定结果起约 55 分钟后转为 `needs_review`。确认对方未收到后再用 `manual` 重发。
- 撤回会取消尚未发出的通知；已送达的消息不收回，撤回本身仍会通知。
- `304 Not Modified` 不修改快照。拉取失败、限流或响应格式错误时，保留上一份有效快照。
- 状态库中的短锁用于跳过重叠轮询，不能据此部署多个常驻进程。

</details>

## 参与贡献

欢迎提交 Issue 或 Pull Request。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，扩展通知渠道见[渠道开发指南](docs/adding-a-channel.md)。

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

CI 在 Python 3.10–3.13 上运行测试。测试使用模拟 HTTP 传输，不调用真实飞书或 Telegram 服务。

## 数据来源与许可

公开动态来自 [AIHOT](https://aihot.news) 的 [recent 接口](https://aihot.news/api/v1/codex-resets/recent)，覆盖最近 7 天。接口支持 `ETag` / `If-None-Match`、gzip 与限流 `Retry-After`；结构见 [OpenAPI](https://aihot.news/openapi-v1.json)，使用条款见 [AIHOT Terms](https://aihot.news/terms)。公告作者为 [Tibo（@thsottiaux）](https://x.com/thsottiaux)。

[MIT License](LICENSE) · Copyright © 2026 LFenX
