# User guide

[Home](../README.md) · [中文](usage.zh-CN.md) · [Bot setup](chat-setup.md#chat-control) · [Webhook](webhook.md)

## 1. Prepare your bot

Create a Telegram bot or a Feishu custom app using the [bot setup guide](chat-setup.md#chat-control). Each deployment uses its own bot and credentials.

Download the repository and create local configuration files:

```bash
git clone https://github.com/LFenX/tibo_reset_monitor.git
cd tibo_reset_monitor
cp config.minimal.example.json config.json
cp .env.example .env
```

In PowerShell, use `Copy-Item` instead of `cp` if preferred.

Edit **config.json**:

- The minimal example enables both Telegram and Feishu. Set channels you do not use to `"enabled": false`.
- For Telegram, keep `bot_token_env` set to `TIBO_TELEGRAM_BOT_TOKEN`.
- For Feishu, replace the placeholder `app_id`. Add administrator `open_id`s to `admins`, or use the app's collaborators when that list is empty.
- Leave `targets` empty to add subscriptions through chat. Use [the full example](../config.example.json) for static targets, language, time zone, or Webhook settings.

Edit **.env** and replace the enabled channels' placeholders with your secrets. Configuration stores environment variable names, never the secrets themselves. Keep both local files out of Git.

## Start the monitor

Choose one method. Keep **run** active for chat subscriptions; a one-off **poll** does not listen for bot commands.

### Docker

Requires Docker. The image includes calendar rendering, Chinese and English fonts, time zone data, and the Feishu listener.

```bash
docker build -t tibo-reset-monitor .
docker run --rm --name tibo-reset-monitor -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor --config /config/config.json run
```

The start command passes global options before the subcommand. Keep the state volume: it holds subscriptions and delivery history. The minimal configuration logs to the console; if you enable file logging, use a writable path such as `/data/monitor.log`.

### Python: Linux / macOS

Requires Python 3.10+ and Git.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[calendar,feishu-bot]'
set -a
. ./.env
set +a
tibo-reset-monitor --config config.json run
```

The optional extras install Pillow for calendars and lark-oapi for Feishu chat control. With `python -m pip install -e .`, the core still works; calendar images and the Feishu listener need their respective extras. Chinese calendars also need a CJK font; set `calendar.font_path` if one is not found automatically.

### Python: Windows / PowerShell

Create local copies of the two example files, then run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[calendar,feishu-bot]'
$env:TIBO_TELEGRAM_BOT_TOKEN = 'replace-with-your-bot-token'
$env:TIBO_FEISHU_APP_SECRET = 'replace-with-your-app-secret'
tibo-reset-monitor --config config.json run
```

Set only the variables used by your enabled channels. Python does **not** load `.env` automatically; the Linux example exports it, Docker reads it with `--env-file`, and PowerShell sets the environment explicitly.

## 3. Subscribe through your bot

### Telegram

1. Open a direct chat with your bot and press **Start**.
2. If no administrators are configured or stored, find the one-time code in the monitor's terminal and send `/pair CODE` in that direct chat. It expires after 10 minutes and works once. Otherwise, use an account already listed as an admin.
3. Send `/subscribe` to receive personal notifications. To use a group, add the bot first, then send `/addgroup` in your direct chat and reply with a listed number or full group name within one minute. An admin can also send `/subscribe` inside the group.
4. Use `/unsubscribe`, `/removegroup`, or `/status` to manage subscriptions. `/help` shows the guide.

Telegram chat control uses polling. An existing Telegram webhook must be removed, or chat control disabled, before polling can work.

### Feishu

1. Enable the bot, message permissions, and long-connection events, then publish the app. Include the intended users in its availability scope.
2. Add your admin `open_id` to configuration, or use an app collaborator if `admins` is empty.
3. Open a direct chat and send `#开启单聊通知`, or `#添加群聊` and reply with a listed number or full group name. Both you and the bot must be in the group. Grant [group-member read access](https://open.feishu.cn/document/server-docs/group/chat-member/get); a failed membership check keeps group management closed.
4. Use `#关闭单聊通知`, `#移除群聊`, or `#状态` to manage subscriptions. `#帮助` shows the guide.

Admins can manage subscriptions by default. Set `allow_self_subscribe` to `true` to allow other users to manage their own direct messages; group management stays restricted to admins. See the full [permissions and commands](chat-setup.md#who-can-use-it).

Once ready, admins receive one usage guide per guide version. Telegram can deliver it after the user starts the bot or pairs successfully. A first fetch establishes the baseline; new subscriptions do not receive old posts.

Automatic notifications use the original post's publication time, not an event's update time. Even an empty first response establishes the baseline. Publication must be at or after the latest of the baseline, seven days ago, the source history boundary, and one hour before the last successful local check; future clock skew is limited to five minutes. Historical backfills appear in `list` for an explicit `manual --event` request without being automatically sent. A classification change keeps the same post identity.

Source failures and interrupted checks block automatic delivery until a valid response or a validated cached 304 recovers it. Legal retries keep their original time boundary, target, rendered message and idempotency key. Removed source posts cancel their automatic pending work; unsafe legacy or changed targets become `needs_review`. Unknown send results stop blind retries after 55 minutes. Telegram has no provider idempotency key, so an ambiguous Telegram delivery needs review immediately.

## Language, time zone, and calendars

Notifications default to Chinese. Set `language` to `en` or `zh` globally, per channel, or per target; the most specific value wins. English notifications use the original post text. `timezone` follows the same order and accepts an IANA name such as `Asia/Shanghai` or `America/Los_Angeles`. Times include the recipient's current UTC offset.

Telegram sends one calendar photo with a caption when rendering is available, otherwise one text message. Feishu sends one card with an optional calendar; an image upload failure still allows the text card. The calendar uses the recipient's local month and one validated full-history snapshot. It replaces deleted and corrected events without merging old recent data. A failed refresh keeps valid cached history; a recent ETag change triggers another history check after the ten-minute minimum interval, with revalidation at least every 24 hours.

## Check delivery

Run these in the same Python environment, configuration directory, and state database as the service:

```bash
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json list --limit 20
```

For Docker, while the named container is running:

```bash
docker exec tibo-reset-monitor python -m tibo_reset_monitor --config /config/config.json status
docker exec tibo-reset-monitor python -m tibo_reset_monitor --config /config/config.json list --limit 20
```

| Symptom | Check |
| --- | --- |
| No message immediately after startup | The first successful fetch creates a baseline. Use the bot's status/help to confirm your subscription |
| Telegram direct chat is silent | Press Start, confirm admin access or self-subscription settings, then subscribe |
| Feishu bot cannot be found | App availability scope, publication, permissions, and long-connection event subscription |
| A group is missing from the picker | Add the bot to the group; for Telegram, confirm the running process has seen its membership event |
| No calendar image | Pillow and fonts; Feishu also needs image-upload permission |
| A subscription stops | Three consecutive permanent delivery failures stop it; fix the destination and subscribe again |

## Other run modes

Use continuous **run** for chat control. For a scheduled one-off fetch, configure static targets and use:

```bash
tibo-reset-monitor --config config.json poll
```

Only one polling or continuous process should use the same state database. On systemd, the supplied service's `ExecStart` must place `--config` before `poll`; a timer-based poll does not listen for chat commands. For a persistent service, use `run` and turn off the timer. See [service files](../deploy/systemd/) for the existing templates.

For custom notification channels and optional HMAC signing, see [Webhook](webhook.md). For channel implementation, see [adding a channel](adding-a-channel.md).
