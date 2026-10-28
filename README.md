# tibo-reset-monitor

轮询 [AIHOT](https://aihot.news) 公开接口，跟踪 Tibo（X：[@thsottiaux](https://x.com/thsottiaux)，OpenAI Codex 负责人）发布的 Codex / ChatGPT 额度重置动态。出现**新的**来源帖时，向已配置的飞书、Telegram 或 Webhook 目标各发一条通知。

数据来自 AIHOT 公开接口 `https://aihot.news/api/v1/codex-resets/recent`（最近 7 天，支持 `ETag` / `If-None-Match`、gzip，限流时返回 `Retry-After`）。接口说明见 [OpenAPI](https://aihot.news/openapi-v1.json)。**确认时间是来源帖子的发布时间，不是账户额度到账的时刻。** AIHOT 的使用条款见 [aihot.news/terms](https://aihot.news/terms)。

英文说明：[README.en.md](README.en.md)。

## 它怎么工作

- 默认每 10 分钟拉一次。`304 Not Modified` 不会动本地快照。
- **第一次成功拉取只建立基线**，历史帖子不发通知。之后每条新的来源帖（新的「帖子 + 阶段」）才通知。
- 阶段包括预告、确认、补充、撤回。同一条帖子若同时出现在事件帖子列表和活动流里，只通知一次。
- 无关闲聊（接口里的 `related`）和故障提示（`outage`，接口写明它不是重置事实）不发通知。
- 状态在 SQLite。每个「事件 × 目标」有自己的 outbox 行、幂等键、回执和重试状态。一个目标失败不会挡住其他目标，也不会让已成功的目标重发。
- 明确失败会退避重试。超时这类结果不确定的发送会沿用同一个幂等键；距第一次不确定结果约 55 分钟后改为 `needs_review`，不再盲目重试（飞书的 `uuid` 大约 1 小时内去重）。
- 尚未发出的通知如果对应事件被撤回，会被取消。已经发出的不会收回，撤回本身仍会通知。
- 拉取失败、限流或响应格式不对时，**不会覆盖**上一份有效快照。
- 后来新增的目标只接收新增之后发现的事件，不补发历史。

请只跑一个 `poll` / `run` 进程。状态库里有一把短锁，避免 cron 重叠时重复发送。

## 快速开始

需要 Python 3.10 或更高版本。核心和 v1 渠道只用标准库。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.json config.json
cp .env.example .env
```

编辑 `config.json` 里的占位 `receive_id` / `chat_id`（这些不是密钥，但请换成你自己的）。密钥只放在环境变量里：

```bash
export TIBO_FEISHU_APP_SECRET='你的飞书 app secret'
export TIBO_TELEGRAM_BOT_TOKEN='你的 Telegram bot token'
```

```bash
tibo-reset-monitor poll --config config.json
tibo-reset-monitor status --config config.json
tibo-reset-monitor list --config config.json
```

不安装也可以：

```bash
PYTHONPATH=src python -m tibo_reset_monitor poll --config config.json
```

`config.example.json` 里的 app id、chat id、open id 都是假的占位符。不要把真实密钥写进配置文件，也不要提交 `config.json` 或 `.env`。

## 配置

使用 JSON，因为 Python 3.10 的标准库没有 `tomllib`（那是 3.11 才有的）。程序只读 JSON。

| 字段 | 含义 |
| --- | --- |
| `source.poll_interval_seconds` | `run` 的间隔，默认 600，最小 60 |
| `source.url` | 默认 AIHOT recent 接口，必须是 https |
| `state.path` | SQLite 路径，也可用环境变量 `TIBO_STATE_PATH` 覆盖 |
| `channels.*.enabled` | `false` 的渠道不会发送 |
| `channels.*.targets[]` | 每个目标有 `name` 和 `kind`：`group`（群）或 `personal`（个人） |

飞书群目标使用 `receive_id_type: chat_id`。个人目标使用 `open_id`、`user_id` 或 `email`。Telegram 群是负数 chat id 或公开频道的 `@channelusername`；个人目标是用户的正整数 chat id。两种渠道都可以同时配置群和个人，数量不限。

Webhook 可选。把 URL 放在 `url_env` 指向的环境变量里，请求会带 `Idempotency-Key`。

国际版 Lark 把飞书的 `api_base` 改成 `https://open.larksuite.com`。

## 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)（国际版用 [Lark](https://open.larksuite.com/app)），创建企业自建应用。
2. 启用机器人能力。
3. 开通权限「以应用的身份发消息」（`im:message:send_as_bot`）。用邮箱作为个人目标时，还要能按邮箱识别租户内用户。
4. 创建版本并在本企业发布。把 App ID 填进配置的 `app_id`，把 App Secret 放进 `TIBO_FEISHU_APP_SECRET`。
5. **群**：把机器人拉进群。`chat_id` 一般以 `oc_` 开头。可用该应用的 tenant token 调用 `GET /open-apis/im/v1/chats` 查看机器人所在的群。群目标的 `receive_id_type` 必须是 `chat_id`。
6. **个人**：`open_id` 一般以 `ou_` 开头，`user_id` 是企业内用户 id，`email` 是该用户在租户里的邮箱。三选一，写到 `receive_id_type`。

本程序直接请求飞书 Open API，不依赖 `lark-cli`。消息是卡片 JSON 2.0：阶段、作者、正文、原帖链接。幂等键放在发送接口的 `uuid` 字段。

## 创建 Telegram 机器人

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，拿到 token，放进 `TIBO_TELEGRAM_BOT_TOKEN`。
2. **个人**：用户先打开机器人并按下 Start，否则发送会返回 403。然后在浏览器或 curl 里请求 `getUpdates`（把 token 放在环境变量里，不要贴到聊天或日志）：

   ```bash
   curl -s "https://api.telegram.org/bot${TIBO_TELEGRAM_BOT_TOKEN}/getUpdates"
   ```

   在返回的 `message.chat.id` 里找到自己的**正整数** id，填到 `kind: personal` 的 `chat_id`。
3. **群**：把机器人加进群或频道，在群里发一条消息（若开了隐私模式，需要 @ 机器人或暂时关闭隐私模式，这样 `getUpdates` 才看得到）。群和超级群的 id 是负数，超级群经常是 `-100` 开头。公开频道也可以填 `@channelusername`。`kind` 用 `group`。
4. 只负责发通知时，机器人不需要一直读取群消息。查到 chat id 之后可以重新打开隐私模式。

403（被拉黑或无权发言）和 400 `chat not found` 记为永久失败，并在 `status` / `list` 里给出可读原因。429 会按 `retry_after` 退避。

## 命令

| 命令 | 作用 |
| --- | --- |
| `poll` | 跑一轮。适合 cron、systemd timer、Windows 任务计划程序 |
| `run` | 按配置间隔循环 |
| `status` | 基线、最近快照、outbox 计数。加 `--json` 输出 JSON |
| `list` | 已发现的通知及每个目标的投递状态 |
| `send-test --channel telegram --target example-person` | 给一个目标发测试消息，不代表发生了重置 |
| `manual --channel feishu --target example-group --event 3` | 重发某条已存储通知（使用新的幂等键） |
| `manual --text "自定义" --channel telegram --target example-person` | 发送自定义文本 |
| `manual ... --preview` | 只打印将要发送的内容，不发送 |

`poll` 在源站拉取失败时返回退出码 1，配置错误返回 2。某个目标投递失败仍返回 0，因为失败已写入 outbox，下一轮会重试。`needs_review` 表示某次发送结果不确定且已停止自动重试；确认对方没收到后，用 `manual` 重发。

## 定时运行

### Linux / macOS cron

```cron
*/10 * * * * cd /opt/tibo-reset-monitor && . .venv/bin/activate && set -a && . ./.env && set +a && python -m tibo_reset_monitor poll --config /opt/tibo-reset-monitor/config.json
```

### systemd

示例单元在 `deploy/systemd/`。`tibo-reset-monitor.service` 是一次性的 `poll`，由 `tibo-reset-monitor.timer` 每 10 分钟触发：

```bash
sudo cp deploy/systemd/tibo-reset-monitor.service deploy/systemd/tibo-reset-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tibo-reset-monitor.timer
```

也可以把 `ExecStart` 改成 `run`，做成常驻服务。不要同时跑 timer 和常驻进程。

### Windows 任务计划程序

```bat
schtasks /Create /TN TiboResetMonitor /SC MINUTE /MO 10 /TR "py -m tibo_reset_monitor poll --config C:\tibo-reset-monitor\config.json"
```

在任务的环境里设置 `TIBO_FEISHU_APP_SECRET` 和 `TIBO_TELEGRAM_BOT_TOKEN`。工作目录设为项目目录，这样相对的 `state/` 路径才稳定。

### Docker

```bash
docker build -t tibo-reset-monitor .
docker run --rm -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor
```

镜像默认执行 `run`。状态库在 `/data`（由 `TIBO_STATE_PATH` 指定）。

## 增加一个渠道

看 [docs/adding-a-channel.md](docs/adding-a-channel.md)。核心（`src/tibo_reset_monitor/core/`）不导入任何渠道。新渠道是 `channels/` 里的一个文件，加上 `channels/__init__.py` 里的一行注册。

## 开发

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

GitHub Actions 在 Python 3.10–3.13 上跑同一套测试。测试使用假的 HTTP 传输，不访问飞书或 Telegram。

## 常见问题

**为什么启动后没有把过去一周的重置都推送出来？**  
第一次成功拉取只记基线。这是故意的，避免把历史记录刷进群里。

**新增了一个个人目标，为什么没有补发？**  
目标只接收它加入配置之后新发现的事件。需要旧消息时用 `manual --event`。

**群里收到了，私聊没有？**  
看 `list`。Telegram 个人聊天要先按 Start。飞书机器人要在群里，个人 id 的类型要和 `receive_id_type` 一致。一个目标失败不会取消其他目标。

**`needs_review` 是什么？**  
发送超时后，程序无法确定对方是否已经收下。它会用同一个幂等键再试一段时间，大约 55 分钟后停止，避免飞书去重窗口结束后变成重复消息。确认没收到再用 `manual` 重发。

**确认时间是到账时间吗？**  
不是。AIHOT 的 `confirmedAt` 和这里展示的时间都是来源帖子的发布时间。账户里额度何时到账，以产品内显示为准。估计落地窗口来自接口里的 `estimate` / `schedule`，也不是账户回执。

## 许可

MIT，版权所有者 LFenX，2026。见 [LICENSE](LICENSE)。
