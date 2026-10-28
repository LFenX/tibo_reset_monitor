<p align="center">
  <img src="docs/readme-cover.png" alt="Tibo, patron saint of resets, pressing RESET while programmers await the signal" width="100%">
</p>

<h1 align="center">tibo-reset-monitor</h1>

<p align="center">
  <strong>He resets. We report.</strong><br>
  Track Tibo's Codex / ChatGPT quota-reset updates, with notifications to Feishu, Telegram, or your own channels via webhooks.
</p>

<p align="center">
  <a href="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml"><img src="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI on main"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-D4AF37" alt="MIT License"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/%E4%B8%AD%E6%96%87-3776AB" alt="中文"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#notification-channels">Channels</a> ·
  <a href="#deployment">Deployment</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

## Features

| Capability | What it does |
| --- | --- |
| Reset tracking | Poll the public AIHOT feed for announcements, confirmations, revisions, and retractions |
| Multiple destinations | Send to Feishu and Telegram groups or individuals, or your own webhook |
| Independent delivery | Persist state in SQLite, deduplicate and retry per target, and isolate destination failures |
| Flexible deployment | Run once, run continuously, or use cron, systemd, Windows Task Scheduler, or Docker |

> The first successful fetch builds a baseline without sending historical posts. This project tracks public announcements: a confirmation time is the source post's publication time. Check the product itself to see whether your account's quota has been restored.

## Quick start

Requires **Python 3.10+** and Git. The core and built-in notification channels use only the Python standard library.

### 1. Install

These commands are for Linux / macOS. PowerShell instructions are available below.

```bash
git clone https://github.com/LFenX/tibo_reset_monitor.git
cd tibo_reset_monitor
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config.example.json config.json
cp .env.example .env
```

### 2. Configure notifications

Edit your local `config.json` using [the example](config.example.json):

- Enable only the channels you use; set the others to `"enabled": false`.
- Replace the placeholder IDs in `targets` and remove unused example targets.
- Put the enabled channels' secrets in `.env`. The program reads environment variables and **does not load `.env` automatically**.

See [notification channels](#notification-channels) for setup. All App IDs, chat IDs, and user IDs in the example are placeholders. Keep secrets out of JSON and do not commit `config.json` or `.env`.

### 3. Start

```bash
set -a
. ./.env
set +a

tibo-reset-monitor --config config.json poll
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json run
```

`poll` runs one cycle, `status` shows the saved state, and `run` polls continuously at the configured interval (10 minutes by default). Press `Ctrl+C` to stop. The global `--config` option must come before the subcommand.

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

Edit `config.json` as described above. Set the enabled channels' variables in your current PowerShell session before running:

```powershell
$env:TIBO_FEISHU_APP_SECRET = 'replace-with-feishu-app-secret'
$env:TIBO_TELEGRAM_BOT_TOKEN = 'replace-with-telegram-bot-token'

tibo-reset-monitor --config config.json poll
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json run
```

For webhooks, also set the environment variable named by the target's `url_env`. Set only the credentials required by your enabled channels.

</details>

## Notification channels

Choose your channels in [config.example.json](config.example.json). Each channel can have multiple group or personal targets.

| Channel | Destinations | Credentials |
| --- | --- | --- |
| Feishu / Lark | Groups and individuals | App ID + `TIBO_FEISHU_APP_SECRET` |
| Telegram | Groups, channels, and individuals | `TIBO_TELEGRAM_BOT_TOKEN` |
| Webhook | Your HTTPS endpoint | Environment variable named by `url_env` |

<details>
<summary><strong>Feishu / Lark: create an app and find destination IDs</strong></summary>

1. Create a custom app in the [Feishu open platform](https://open.feishu.cn/app) or [Lark](https://open.larksuite.com/app), and enable its bot capability.
2. Grant `im:message:send_as_bot`, create a version, and publish it within your tenant.
3. Put the App ID in `app_id` and the App Secret in `TIBO_FEISHU_APP_SECRET`.
4. **Group:** add the bot to the group, set `receive_id` to its `chat_id` (usually starting with `oc_`), and use `receive_id_type: chat_id`. The app can call `GET /open-apis/im/v1/chats` with a tenant token to list its chats.
5. **Personal:** choose `open_id`, `user_id`, or `email` as `receive_id_type` and provide the matching `receive_id`. Email targets also require permission to resolve tenant users by email.

For international Lark, set `api_base` to `https://open.larksuite.com`. The monitor calls the Open API directly; `lark-cli` is not required. Notifications use Card JSON 2.0 with the stage, author, body, and original post link.

</details>

<details>
<summary><strong>Telegram: create a bot and find chat_id</strong></summary>

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and store the token in `TIBO_TELEGRAM_BOT_TOKEN`.
2. **Personal:** open the bot and press Start, then request updates:

   ```bash
   curl -s "https://api.telegram.org/bot${TIBO_TELEGRAM_BOT_TOKEN}/getUpdates"
   ```

   Use the positive `message.chat.id` as a string in `chat_id`, with `kind: personal`.
3. **Group / channel:** add the bot, grant posting permission, send it a visible message, and query `getUpdates`. Group IDs are negative; supergroup IDs usually start with `-100`. Public channels can also use `@channelusername`. Set `kind` to `group`.
4. With privacy mode enabled, send a message the bot can see, such as a mention. Once you have the ID, privacy mode can be restored; continuous chat reading is not required.

403 (blocked or not allowed to post) and 400 `chat not found` are stored as permanent failures. 429 follows `retry_after`.

</details>

<details>
<summary><strong>Webhook: connect your own receiver</strong></summary>

Enable `channels.webhook` and give each target a `name`, `kind`, and `url_env`. The latter names an environment variable whose value is the actual HTTPS URL:

```bash
export TIBO_WEBHOOK_EXAMPLE_URL='https://example.com/your-webhook'
```

Requests include `Idempotency-Key`, which your receiver can use to deduplicate deliveries. Keep the actual URL out of the JSON configuration.

</details>

## Configuration and commands

See [config.example.json](config.example.json) for the complete configuration and [.env.example](.env.example) for credential variable names.

| Setting | Default / purpose |
| --- | --- |
| `source.poll_interval_seconds` | `600`, minimum `60`; polling interval for `run` |
| `source.url` | AIHOT recent endpoint; HTTPS is required |
| `state.path` | `state/monitor.sqlite3`; overridden by `TIBO_STATE_PATH` |
| `logging.level` / `logging.file` | `INFO` / `logs/monitor.log` in the example configuration |
| `channels.*.enabled` | Whether the channel is enabled |
| `channels.*.targets` | Destination list; each target has a `name` and `kind` |

Commands read `config.json` from the current directory by default. To choose another file, use `tibo-reset-monitor --config PATH <command>`.

| Subcommand | Purpose |
| --- | --- |
| `poll` | Fetch once and deliver due notifications |
| `run` | Poll continuously at the configured interval |
| `status --json` | Show baseline, snapshot, and outbox counts |
| `list --limit 20` | Show notices and per-target delivery state; also supports `--json` |
| `send-test --channel telegram --target example-person` | Send a test message to one destination |
| `manual --channel feishu --target example-group --event 3` | Resend a saved notice with a new idempotency key |
| `manual --channel telegram --target example-person --text "hello" --preview` | Preview custom text without sending it |

Target names must match your configuration; get event IDs from `list`. `poll` exits `1` if the source fetch fails and `2` for invalid configuration. Individual delivery failures still exit `0`; inspect them with `list`.

## Deployment

Choose one execution method. Use a single `poll` / `run` process for each state database.

<details>
<summary><strong>Docker</strong></summary>

Configure your local `config.json` and `.env` first. Set `logging.file` to `/data/monitor.log` so the container user can write logs in the mounted volume.

```bash
docker build -t tibo-reset-monitor .
docker run --rm -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor
```

The image runs `run` by default and uses `TIBO_STATE_PATH` to write the database to `/data/monitor.sqlite3`. The named `tibo-state` volume persists the state.

</details>

<details>
<summary><strong>Linux / macOS: cron</strong></summary>

Assuming the project is installed at `/opt/tibo-reset-monitor` with a virtual environment and a configured `.env`:

```cron
*/10 * * * * cd /opt/tibo-reset-monitor && set -a && . ./.env && set +a && .venv/bin/python -m tibo_reset_monitor --config /opt/tibo-reset-monitor/config.json poll
```

Run once every 10 minutes. Relative state paths resolve from the project directory.

</details>

<details>
<summary><strong>Linux: systemd</strong></summary>

See the example [service](deploy/systemd/tibo-reset-monitor.service) and [timer](deploy/systemd/tibo-reset-monitor.timer). Before installing them, update the example service's `ExecStart` to put the arguments in this order:

```ini
ExecStart=/opt/tibo-reset-monitor/.venv/bin/python -m tibo_reset_monitor --config /opt/tibo-reset-monitor/config.json poll
```

Check that `WorkingDirectory`, `EnvironmentFile`, and the installation paths match your setup, then install:

```bash
sudo cp deploy/systemd/tibo-reset-monitor.service deploy/systemd/tibo-reset-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tibo-reset-monitor.timer
```

The service runs one `poll`; the timer triggers it every 10 minutes. If you switch to a continuous `run`, disable the timer.

</details>

<details>
<summary><strong>Windows: Task Scheduler</strong></summary>

For a project installed at `C:\tibo_reset_monitor`, create a task that repeats every 10 minutes with these action settings:

| Field | Value |
| --- | --- |
| Program/script | `C:\tibo_reset_monitor\.venv\Scripts\python.exe` |
| Add arguments | `-m tibo_reset_monitor --config C:\tibo_reset_monitor\config.json poll` |
| Start in | `C:\tibo_reset_monitor` |

Set the enabled channels' credential variables for the account that runs the task. The task does not load `.env` automatically. A fixed working directory keeps relative state paths consistent.

</details>

## FAQ

<details>
<summary><strong>Why did startup send no notifications?</strong></summary>

The first successful fetch only records a baseline; it does not backfill the past 7 days. New targets also receive only updates detected after they are added. Check the fetch state with `status` and your channel with `send-test`. Use `manual --event` when you need to resend a saved notice.

</details>

<details>
<summary><strong>Why did the group receive a message but the direct message fail?</strong></summary>

Check each destination with `list`. A Telegram user must press Start. A Feishu personal ID must match `receive_id_type`, and the bot must belong to the group. One destination's failure does not affect the others.

</details>

<details>
<summary><strong>How do deduplication, retries, and retractions work?</strong></summary>

- A new update is identified by post ID + stage. A post appearing in both the event list and the activity feed notifies once. Related chatter (`related`) and outage hints (`outage`) do not notify.
- Each event and destination has its own outbox entry, idempotency key, receipt, and retry state. Clear failures are retried with backoff.
- Ambiguous outcomes such as timeouts retain the same idempotency key. About 55 minutes after the first ambiguous outcome, the entry becomes `needs_review`. Use `manual` only after confirming the message never arrived.
- A retraction cancels pending, unsent notices. Delivered messages remain in place, and the retraction itself is still announced.
- `304 Not Modified` leaves the snapshot unchanged. Fetch failures, rate limits, and malformed responses preserve the last valid snapshot.
- The short database lock skips overlapping polls; it does not make running multiple continuous processes a supported deployment.

</details>

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development conventions and [the channel guide](docs/adding-a-channel.md) to add a destination.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

CI runs tests on Python 3.10–3.13 with mocked HTTP transports. Tests do not call live Feishu or Telegram services.

## Data source and license

Public updates come from [AIHOT](https://aihot.news)'s [recent endpoint](https://aihot.news/api/v1/codex-resets/recent), covering the last 7 days. It supports `ETag` / `If-None-Match`, gzip, and rate-limit `Retry-After`. See [OpenAPI](https://aihot.news/openapi-v1.json) for the schema and [AIHOT Terms](https://aihot.news/terms) for terms of use. Announcements are authored by [Tibo (@thsottiaux)](https://x.com/thsottiaux).

[MIT License](LICENSE) · Copyright © 2026 LFenX
