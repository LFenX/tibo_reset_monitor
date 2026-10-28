<p align="center">
  <img src="docs/assets/banner.jpg" alt="tibo-reset-monitor mascot: a character with headphones holding a glowing calendar beside a small robot and a bell" width="100%">
</p>

<h1 align="center">tibo-reset-monitor</h1>

<p align="center">
Poll the public AIHOT feed and notify Feishu, Telegram, or a webhook when Tibo posts a new Codex or ChatGPT quota-reset update.
</p>

<p align="center">
  <a href="README.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml"><img src="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/LFenX/tibo_reset_monitor/blob/main/.github/workflows/ci.yml"><img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-3776AB" alt="Python 3.10–3.13"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

Tibo is [@thsottiaux](https://x.com/thsottiaux) on X (OpenAI Codex lead). The feed is the public AIHOT endpoint [`/api/v1/codex-resets/recent`](https://aihot.news/api/v1/codex-resets/recent). The confirmation time is the source post's publish time, not the moment credit lands on an account. Terms: [aihot.news/terms](https://aihot.news/terms).

## Contents

- [Features](#features)
- [Preview](#preview)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Channels](#channels)
- [Docker and background runs](#docker-and-background-runs)
- [How it works](#how-it-works)
- [FAQ](#faq)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Features

- Polls the last 7 days every 10 minutes by default, with `ETag` / `If-None-Match` and gzip.
- The first successful fetch only builds a baseline. History is not pushed. Later, each new source post (post id + stage) notifies.
- Stages are announcement, confirmation, revision, and retraction. A post that appears in both the event list and the activity feed notifies once.
- Feishu, Telegram, and Webhook can be enabled together. Each channel can mix `group` and `personal` targets.
- Telegram sends one message per target: caption text, then the calendar image. Without an image it sends one text message.
- One failing target does not block the others and does not resend a target that already succeeded.
- State is a local SQLite file. Secrets are read only from the environment.

## Preview

A Telegram `sendPhoto` caption looks like this. The calendar image follows the last line, `重置日历`. Without Pillow or a CJK font, the same text is one `sendMessage` and there is no second image.

```html
<b>Tibo</b>
<i>@thsottiaux · X</i>

<code>重置（形式未明确，默认额度重置）</code> <code>预告</code>

搞定了。明天见，会有好消息。

<i>帖子未明确适用范围</i>
<i>帖子发布 · 2026-10-28 14:29:38 UTC+8</i>

<blockquote>原帖 · Tibo @thsottiaux
And it's done. See you tomorrow for some good news.
引用 @thsottiaux
Lands by midnight PST.
2026-10-28 14:29:38 UTC+8</blockquote>
<a href="https://x.com/thsottiaux/status/123">查看原帖</a>

重置日历 · 2026 年 10 月
```

When the stored tag is 「重置（形式未明确）」, the Chinese message shows 「重置（形式未明确，默认额度重置）」. Other tags, such as 「重置（额度重置）」 and 「发重置卡」, stay as they are. The English original comes from `fullOriginalText`, or `originalText` when that field is absent. A `context` item with `relation: quote` is shown as `引用 @author`; `reply` is `回复 @author`. Context posts have no publish time of their own, so the time is the main post's `publishedAt`. The blockquote is omitted when both the original text and `context` are empty.

With `language` set to `en`, the same notice is English. The body is the original post, not the Chinese translation, and there is no original-post card. A quoted post is one line, `Quoting @handle: …`. A reply is `Replying to @handle: …`. The link says View original post.

```html
<b>Tibo</b>
<i>@thsottiaux · X</i>

<code>Reset (form unspecified, defaults to quota reset)</code> <code>Preview</code>

And it's done. See you tomorrow for some good news.

<i>Scope not specified in the post</i>
<i>Posted · 2026-10-28 14:29:38 UTC+8</i>

Quoting @thsottiaux: Lands by midnight PST.
<a href="https://x.com/thsottiaux/status/123">View original post</a>

Reset calendar · October 2026
```

The calendar month is the target's own time zone. A `reset_credit` cell says 发重置卡; every other event says 额度重置. 补充确认 is still used when the form was not explicit and the post quotes another event's 补充说明. The header counts only confirmed events in that month. Grey cells at the edges belong to the neighboring month. November 2026 below is six week rows. December 2026 and February 2027 are in `docs/` as well.

<p align="center">
  <img src="docs/calendar-sample-2026-11.png" alt="Sample November 2026 calendar with late-October events in the grey cells" width="100%">
</p>

| Sample | Week rows |
| --- | --- |
| [November 2026](docs/calendar-sample-2026-11.png) | 6 |
| [December 2026](docs/calendar-sample-2026-12.png) | 5 |
| [February 2027](docs/calendar-sample-2027-02.png) | 4, not a leap year |
| [November 2026, English](docs/calendar-sample-en-2026-11.png) | 6 |
| [December 2026, English](docs/calendar-sample-en-2026-12.png) | 5 |
| [February 2027, English](docs/calendar-sample-en-2027-02.png) | 4, not a leap year |

These images are drawn from fake events. They are not a screenshot of a live notification. The English grid uses DejaVu, Liberation, Arial, or Noto Sans and does not need a CJK font.

<p align="center">
  <img src="docs/calendar-sample-en-2026-11.png" alt="English November 2026 calendar" width="100%">
</p>

## Quick start

Python 3.10 or newer. The core and the three channels use the standard library only. The calendar image needs an optional extra:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[calendar]'
```

Skip the extra with `pip install -e .` if you do not want the image. Telegram then sends one text message and logs a warning.

```bash
cp config.example.json config.json
cp .env.example .env
```

Replace the placeholder `receive_id` and `chat_id` values. Put secrets in the environment, not in the config file:

```bash
export TIBO_FEISHU_APP_SECRET='your Feishu app secret'
export TIBO_TELEGRAM_BOT_TOKEN='your Telegram bot token'
```

The first run builds a baseline and does not push historical posts:

```bash
tibo-reset-monitor poll --config config.json
tibo-reset-monitor status --config config.json
```

Without installing the package:

```bash
PYTHONPATH=src python -m tibo_reset_monitor poll --config config.json
```

Every id and token in `config.example.json` and `.env.example` is a placeholder. `config.json` and `.env` are listed in `.gitignore`.

## Commands

Global flags: `--config` (default `config.json`), `--log-level`, and `--version`.

| Command | What it does |
| --- | --- |
| `poll` | Fetch once, enqueue new posts, and deliver due outbox rows. Use this from cron, a systemd timer, or Windows Task Scheduler |
| `run` | Loop every `poll_interval_seconds`. Default 600 |
| `status` | Baseline, latest snapshot, and outbox counts. `--json` prints JSON |
| `list` | Detected notices and per-target delivery state. `--limit` defaults to 20. `--json` is optional |
| `send-test --channel telegram --target example-person` | Send a test message. It does not mean a reset happened |
| `manual --channel feishu --target example-group --event 3` | Re-send one notice from `list` with a new idempotency key |
| `manual --text "hello" --channel telegram --target example-person` | Send custom text. The title is 手动提醒 |
| `manual ... --preview` | Print the payload and do not send |

`poll` exits 2 when the config is invalid and 1 when the source fetch fails. A failed target still exits 0 because the failure is stored and retried.

## Configuration

The config file is JSON. `tomllib` is not in the Python 3.10 standard library, so this project does not read TOML.

The full example is [`config.example.json`](config.example.json).

| Field | Meaning |
| --- | --- |
| `source.url` | Default `https://aihot.news/api/v1/codex-resets/recent`. Must be https |
| `source.history_url` | Optional. Default is the recent URL with `/recent` removed: `/api/v1/codex-resets` |
| `source.poll_interval_seconds` | Interval for `run`. Default 600, minimum 60 |
| `source.timeout_seconds` | HTTP timeout. Default 20, range 1–120 |
| `state.path` | SQLite path. `TIBO_STATE_PATH` overrides the file |
| `logging.level` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `logging.file` | Optional log file. The console is always used |
| `calendar.font_path` | Optional CJK font path. Empty means search the usual locations |
| `calendar.font_bold_path` | Optional bold font. The regular face is used when it is absent |
| `calendar.font_index` | Optional. Noto / Source Han CJK collections default to index 2 (Simplified Chinese) |
| `calendar.history_refresh_seconds` | Full-history refresh interval. Default 21600 (6 hours), minimum 600 |
| `language` | Global default, `zh` or `en`. Omitted means `zh` |
| `timezone` | Global IANA time zone, such as `Asia/Shanghai`. Omitted means the machine's local zone. If that cannot be read, the process uses UTC and logs it once |
| `channels.<name>.language` | Default for that channel. Overrides the global value. Feishu, Telegram, and Webhook share this meaning |
| `channels.<name>.timezone` | Default time zone for that channel. Overrides the global value. All three channels share it |
| `channels.<name>.targets[].language` | One target. Overrides the channel and the global value |
| `channels.<name>.targets[].timezone` | One target's time zone. Overrides the channel and the global value |
| `channels.<name>.enabled` | Disabled channels are not contacted |
| `channels.<name>.targets[]` | `name` plus `kind`: `group` or `personal` |

Secrets are environment variable names such as `app_secret_env`, `bot_token_env`, and `url_env`. A field named `app_secret` or `bot_token` is rejected.

`language` is `zh` or `en`. `timezone` is an IANA name. Either may be set on the root, on a channel, or on a target. The later value wins, and every channel uses that same cascade. An unknown value fails at startup with the field path, for example `$.channels.telegram.targets[0].language must be "zh" or "en"` or `$.timezone must be an IANA time zone name like Asia/Shanghai`.

Times in a message use that target's zone and the offset in effect at that instant, such as `帖子发布 · 2026-10-28 14:29:38 UTC+8` or `Posted · 2026-10-28 14:29:38 UTC+8`. Daylight-saving changes use the new offset. A zone such as `Asia/Kolkata` is written `UTC+5:30`. A date-only field stays on that calendar day.

`zh` is the current Chinese wording. `en` uses the original post (`fullOriginalText`, otherwise `originalText`). Stage names, tags, scope, the timestamp, and the calendar are English, and the original-post card is omitted. A quote or reply is one line: `Quoting @handle: …` or `Replying to @handle: …`.

## Channels

All three channels can be on at once. A target added later only receives events detected after it was added. There is no history backfill.

### Telegram

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and store the token in `TIBO_TELEGRAM_BOT_TOKEN`.
2. Personal targets: the user opens the bot and presses Start, or sends return 403. Then read the chat id with the token in an environment variable. Do not paste the token into chat or logs:

   ```bash
   curl -s "https://api.telegram.org/bot${TIBO_TELEGRAM_BOT_TOKEN}/getUpdates"
   ```

   `message.chat.id` is a positive integer. Set `kind` to `personal`.
3. Groups and channels: add the bot and read the negative chat id from a message it can see. Supergroups often start with `-100`. A public channel can use `@channelusername`. Set `kind` to `group`.
4. After you have the id you can turn privacy mode back on. Sending notifications does not require the bot to keep reading the group.

One message per target. `sendPhoto` puts the caption above the calendar when the image can be drawn; otherwise one `sendMessage` is sent. A photo caption is at most 1024 characters after HTML is parsed. Plain text is at most 4096. A long body or preview is cut with `…`. Tags, times, and the link stay. There is no avatar, because the photo is the calendar.

403 (blocked, or not allowed to post) and 400 `chat not found` are permanent failures. 429 follows `retry_after`.

### Feishu

The Feishu channel in this repository sends one interactive card (Card JSON 2.0). It calls the Feishu Open API directly and does not use `lark-cli`. The outbox idempotency key is the `uuid` field. The default Chinese card keeps the previous title, body, and View-post button. `en` switches the title, body, and button to English, uses the original post as the body, and omits the original-post card. When a calendar PNG can be drawn it is uploaded and attached after the button. A failed upload is only a warning; the text card is still sent. The app needs permission to upload images (`im:resource`). Without Pillow or a usable font, the card has no image.

1. Create a custom app on the [Feishu open platform](https://open.feishu.cn/app). For Lark, set `api_base` to `https://open.larksuite.com`.
2. Enable the bot and grant permission to send messages as the bot (`im:message:send_as_bot`). Email personal targets also need the app to resolve a user in the tenant by email.
3. Publish a version. Put the App ID in `app_id` (or `app_id_env`). Put the App Secret in `TIBO_FEISHU_APP_SECRET`.
4. Groups: add the bot to the group. `receive_id_type` must be `chat_id`. The id usually starts with `oc_`. `GET /open-apis/im/v1/chats` with the tenant token lists chats the bot is in.
5. Personal targets: `receive_id_type` is `open_id`, `user_id`, or `email`.

### Webhook

Optional. `enabled` defaults to false. Each target URL comes from the environment variable named by `url_env` and must be https. Omitting `secret_env` leaves the request exactly as before: no signature headers. Field details and receiver checks are in [docs/webhook.md](docs/webhook.md).

The request is a `POST` with these headers:

| Header | Meaning |
| --- | --- |
| `Content-Type` | `application/json; charset=utf-8` |
| `Idempotency-Key` | Dedup key for this delivery. After success the same notice is not sent to that target again under a new key. Receivers should store this value |
| `X-Tibo-Timestamp` | Present only when signing is configured. Unix seconds |
| `X-Tibo-Signature` | Present only when signing is configured. `v1=` plus the hex HMAC-SHA256 |

The JSON body has `idempotency_key` (the same value as the header), `language`, `target.name` / `target.kind`, localized `title`, `tags`, `scope`, `text`, and `posted`, plus the original `notice` object. `notice.stage`, `notice.url`, `notice.published_at`, and `notice.text` stay raw. 2xx is success. 429 and 5xx are retried. Other 4xx results are permanent failures.

Signing is optional. `secret_env` is an environment variable name; the config file never holds the secret. If the field is set and the variable is empty, the delivery is retryable and is not sent unsigned. The signed bytes are `<timestamp>.<raw request body>`, and the secret is UTF-8. Receivers should reject timestamps outside a window and compare signatures in constant time.

```python
import hashlib, hmac, time

def verify(secret: str, body: bytes, timestamp: str, signature: str, window: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > window:
        return False
    expected = "v1=" + hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

```javascript
const crypto = require("crypto");

function verify(secret, body, timestamp, signature, windowSec = 300) {
  const ts = Number(timestamp);
  if (!Number.isFinite(ts) || Math.abs(Date.now() / 1000 - ts) > windowSec) return false;
  const expected = "v1=" + crypto.createHmac("sha256", secret).update(`${ts}.`).update(body).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(String(signature));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

## Docker and background runs

The image is Python 3.12 and installs Pillow, WenQuanYi Micro Hei, and DejaVu Sans. Chinese calendars use the CJK font. English calendars use DejaVu. The database is `/data`.

```bash
docker build -t tibo-reset-monitor .
docker run --rm \
  -v "$PWD/config.json:/config/config.json:ro" \
  -v tibo-state:/data \
  --env-file .env \
  tibo-reset-monitor
```

The default command is `run --config /config/config.json`.

### Linux / macOS cron

```cron
*/10 * * * * cd /opt/tibo-reset-monitor && . .venv/bin/activate && set -a && . ./.env && set +a && python -m tibo_reset_monitor poll --config /opt/tibo-reset-monitor/config.json
```

### systemd

Unit files are in [`deploy/systemd/`](deploy/systemd/). The service runs one `poll`. The timer fires it every 10 minutes:

```bash
sudo cp deploy/systemd/tibo-reset-monitor.service deploy/systemd/tibo-reset-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tibo-reset-monitor.timer
```

You can change `ExecStart` to `run` for a long-lived process. Do not run the timer and the daemon together.

### Windows Task Scheduler

```bat
schtasks /Create /TN TiboResetMonitor /SC MINUTE /MO 10 /TR "py -m tibo_reset_monitor poll --config C:\tibo-reset-monitor\config.json"
```

Set the secrets in the task environment. Set the start directory to the project directory so the relative `state/` path stays stable.

## How it works

1. `poll` or `run` requests the recent feed and sends the previous `ETag`. A `304` leaves the saved snapshot untouched.
2. The first successful response records the current posts as a baseline and does not notify.
3. Later posts are deduped by (post id, stage). The stage comes from a post's `stage` and an activity's `action` (`announce`, `confirm`, `amend`, `withdraw`). Activity `kind: related` and the `outage` hint do not notify.
4. Each (notice, target) row in the SQLite outbox has its own idempotency key and retry state. Feishu sends that key as `uuid`. Telegram has no equivalent field; the outbox is what prevents a second send after success.
5. Clear failures are retried with backoff. An ambiguous outcome such as a timeout keeps the same key. About 55 minutes after the first ambiguous outcome the row becomes `needs_review` and is not retried automatically. Use `manual --event` if you confirmed it never arrived.
6. A retraction cancels deliveries that have not been sent. A message already delivered stays sent, and the retraction itself is still announced.
7. A failed, rate-limited, or unreadable fetch does not overwrite the last valid snapshot.
8. The calendar uses the full snapshot `/api/v1/codex-resets` overlaid with recent. It is fetched at most every 6 hours by default, also after a configured time zone enters a new month, and never more often than once every 10 minutes. A failed history fetch does not block the notice and does not erase the cache. Each time zone gets its own image.

Run a single `poll` or `run`. A short lock in the state database skips overlapping cron runs.

## FAQ

**Why were the past week's resets not pushed on startup?**  
The first successful fetch is a baseline. That avoids dumping history into a chat.

**I added a personal target. Why was nothing backfilled?**  
A target only receives events detected after it is in the config. Re-send an older notice with `manual --event`.

**The group got it and the direct message did not.**  
Check `list`. A Telegram user has to press Start. A Feishu bot has to be in the group, and a personal id has to match `receive_id_type`. One target's failure does not cancel the others.

**What is `needs_review`?**  
After a timeout the program cannot tell whether the provider accepted the message. It retries with the same idempotency key, then stops after about 55 minutes so a later retry cannot slip past Feishu's roughly one-hour dedupe window. Use `manual` if you confirmed it never arrived.

**Is the confirmation time when credit hits the account?**  
No. The time shown is the source post's publish time. When credit appears on an account is whatever the product shows.

**There is no calendar image.**  
Install `pip install -e '.[calendar]'`. Chinese images need a CJK font, or `calendar.font_path`. English images use a common Latin face (DejaVu, Liberation, Arial, or Noto Sans) and still render when no CJK font is installed. If Pillow is missing, or neither face can be found, Telegram still sends one text message and the Feishu card has no image.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

GitHub Actions runs Python 3.10, 3.11, 3.12, and 3.13 twice: once without Pillow, and once with Pillow, WenQuanYi Micro Hei, and DejaVu Sans. Tests use a fake HTTP transport. They do not call Feishu or Telegram. Every registered channel is rendered in both `zh` and `en`, so a new channel that skips a language fails CI.

To add a channel, see [docs/adding-a-channel.md](docs/adding-a-channel.md). `src/tibo_reset_monitor/core/` does not import any channel.

## Contributing

Issues and changes are welcome on the [GitHub repository](https://github.com/LFenX/tibo_reset_monitor). The short rules are in [CONTRIBUTING.md](CONTRIBUTING.md): stay compatible with Python 3.10, do not commit secrets or real chat ids, and keep the core importable without Pillow.

## License

[MIT License](LICENSE). Copyright (c) 2026 LFenX.
