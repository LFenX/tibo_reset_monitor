# tibo-reset-monitor

Poll the public [AIHOT](https://aihot.news) API and notify you when Tibo (X: [@thsottiaux](https://x.com/thsottiaux), OpenAI Codex lead) posts a Codex or ChatGPT quota-reset update. A new source post is pushed to every configured Feishu, Telegram, or webhook target.

The feed is `https://aihot.news/api/v1/codex-resets/recent` (last 7 days, `ETag` / `If-None-Match`, gzip, `Retry-After` on rate limit). The schema is the `CodexResetsResponse` in the [OpenAPI document](https://aihot.news/openapi-v1.json). **The confirmation time is the source post time, not the moment credit lands on an account.** AIHOT terms: [aihot.news/terms](https://aihot.news/terms).

中文说明见 [README.md](README.md).

## Behavior

- Default poll interval is 10 minutes. `304 Not Modified` leaves the saved snapshot untouched.
- The **first successful fetch only builds a baseline**. Historical posts are not announced. After that, every new source post (a new post id + stage) notifies.
- Stages are announcement/preview, confirmation, revision, and retraction. A post that shows up in both the event post list and the activity feed notifies once.
- Related chatter (`kind: related`) and the outage hint (`outage`, which the API says is not a reset fact) do not notify.
- State lives in SQLite. Each (event, target) delivery has its own outbox row, idempotency key, receipt, and retry state. One failing target never blocks the others and never causes a successful target to be sent again.
- Clear failures are retried with backoff. An ambiguous outcome (timeout) keeps the same idempotency key. About 55 minutes after the first ambiguous outcome the row becomes `needs_review` instead of being retried blindly. That stays inside Feishu's roughly one-hour `uuid` dedupe window.
- A retracted event that has not been sent yet is cancelled. A message already delivered is left as sent, and the retraction itself is still announced.
- A fetch failure, rate limit, or malformed body never overwrites the last valid snapshot.
- Targets added later only receive events detected after they were added. There is no history backfill.

Run a single `poll` or `run` process. A short lock in the state database skips overlapping cron runs.

## Quick start

Python 3.10 or newer. The core and the v1 channels use the standard library only.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.json config.json
cp .env.example .env
```

Replace the placeholder `receive_id` and `chat_id` values in `config.json`. Put secrets only in the environment:

```bash
export TIBO_FEISHU_APP_SECRET='your feishu app secret'
export TIBO_TELEGRAM_BOT_TOKEN='your telegram bot token'
```

```bash
tibo-reset-monitor poll --config config.json
tibo-reset-monitor status --config config.json
tibo-reset-monitor list --config config.json
```

Without installing:

```bash
PYTHONPATH=src python -m tibo_reset_monitor poll --config config.json
```

Every id in `config.example.json` is a fake placeholder. Do not put real secrets in the config file, and do not commit `config.json` or `.env`.

## Configuration

JSON is the config format because `tomllib` is not in the Python 3.10 standard library (it arrived in 3.11). This project reads JSON only.

| Field | Meaning |
| --- | --- |
| `source.poll_interval_seconds` | Interval for `run`. Default 600, minimum 60 |
| `source.url` | AIHOT recent URL by default. Must be https |
| `state.path` | SQLite path. `TIBO_STATE_PATH` overrides it |
| `channels.*.enabled` | Disabled channels are not contacted |
| `channels.*.targets[]` | Each target has a `name` and a `kind`: `group` or `personal` |

Feishu groups use `receive_id_type: chat_id`. Personal Feishu targets use `open_id`, `user_id`, or `email`. Telegram groups use a negative chat id or a public `@channelusername`. A personal Telegram target is the user's positive numeric chat id. You can mix group and personal targets on both channels.

The webhook channel is optional. Its URL comes from the environment variable named by `url_env`. Requests send an `Idempotency-Key` header.

For Lark outside Feishu China, set Feishu `api_base` to `https://open.larksuite.com`.

## Create a Feishu app

1. Open the [Feishu open platform](https://open.feishu.cn/app) (or [Lark](https://open.larksuite.com/app)) and create a custom app.
2. Enable the bot capability.
3. Grant permission to send messages as the bot (`im:message:send_as_bot`). Email personal targets also need the app to resolve a user in the tenant by email.
4. Publish a version in your tenant. Put the App ID in `app_id`. Put the App Secret in `TIBO_FEISHU_APP_SECRET`.
5. **Group:** add the bot to the group. `chat_id` usually starts with `oc_`. With the tenant token, `GET /open-apis/im/v1/chats` lists chats the bot is in. `receive_id_type` must be `chat_id`.
6. **Personal:** `open_id` usually starts with `ou_`, `user_id` is the tenant user id, and `email` is the user's email in the tenant. Pick one and set `receive_id_type`.

The monitor calls the Feishu Open API directly. It does not use `lark-cli`. Messages are Card JSON 2.0: stage, author, body, and a link to the original post. The outbox idempotency key is sent as the `uuid` field.

## Create a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and store the token in `TIBO_TELEGRAM_BOT_TOKEN`.
2. **Personal:** the user must open the bot and press Start, otherwise sends return 403. Then call `getUpdates` with the token in an environment variable (do not paste the token into chat or logs):

   ```bash
   curl -s "https://api.telegram.org/bot${TIBO_TELEGRAM_BOT_TOKEN}/getUpdates"
   ```

   Use your positive `message.chat.id` as the `chat_id` of a `personal` target.
3. **Group:** add the bot to the group or channel and send a message it can see (mention it, or temporarily disable privacy mode, if you need `getUpdates` to observe the message). Group ids are negative; supergroups often start with `-100`. A public channel can use `@channelusername`. Set `kind` to `group`.
4. Sending notifications does not require the bot to keep reading the group. After you have the chat id you can turn privacy mode back on.

403 (blocked, or not allowed to post) and 400 `chat not found` are stored as permanent failures with a readable reason. 429 follows `retry_after`.

### What a Telegram message looks like

Each notice is **one** message per target. When the calendar image can be drawn, that message is `sendPhoto`: the HTML caption is above the PNG. The original post is a link at the end of the caption. There is no second message that only contains the URL, and there is no avatar. A photo message can carry only one image, and that image is the calendar.

The caption is:

- A bold author. Posts on x.com / twitter.com by `thsottiaux` say **Tibo**. Other X accounts say `@handle`. Any other link says the hostname.
- An italic line: `@thsottiaux · X`, or just `X` / the hostname.
- `【event label】【stage】`. The label is the event `displayLabel`, or `重置卡` for `reset_credit` when that label is absent, or `重置（形式未明确）` otherwise. The stage is still 预告 / 确认 / 补充 / 撤回.
- The body (`fullText`, otherwise `text`). A photo caption is at most 1024 characters after HTML is parsed. A long body is cut and ends with `…`, leaving the tags, the time, and the post link in place.
- Audience (`audienceZh`, otherwise `scopeLabel`, otherwise `帖子未明确适用范围`; omitted when the event type is `activity`), `productsZh` when present, and `帖子发布 · YYYY-MM-DD HH:MM:SS（北京时间）`.
- A “查看原帖” link.

A manual text uses the same shell: title `手动提醒`, caption line `手动发送`, and the calendar when it can be drawn.

Calendar marks come from the full snapshot `https://aihot.news/api/v1/codex-resets` (the same `CodexResetsResponse` as `recent`; OpenAPI operation `codexResets`) overlaid with the recent feed. The full snapshot is fetched at most every 6 hours by default, also when the Beijing month changes, and never more often than once per 10 minutes. A failed history fetch does not block the notification and does not erase a cached copy. The grid then uses the recent feed alone. A day is confirmed from `occurredOn` / `confirmedAt`. An estimate is not drawn as a confirmation. A cell says 发重置卡 when the event type is `reset_credit`, and 额度重置 otherwise. 补充确认 is unchanged: it is only used when the form was not explicit and the post quotes another event's 补充说明. The bottom of the image is only the three color swatches: 已确认, 预计已生效, and 等待确认.

Drawing is optional:

```bash
pip install '.[calendar]'
```

That installs Pillow. Fonts are searched in this order: `calendar.font_path` (and optional `font_bold_path`), Windows Microsoft YaHei `msyh.ttc` / `msyhbd.ttc`, macOS PingFang or Hiragino, then Linux Noto Sans CJK, WenQuanYi, and Source Han Sans. A Noto CJK collection uses face index 2 (Simplified Chinese) unless `calendar.font_index` is set. If no font is found, Pillow is missing, or drawing fails, Telegram sends **one** text message instead (limit 4096, same truncation) and logs a warning. The notification is not failed because the image could not be produced.

Sample grid for October 2026, drawn from fake events: [`docs/calendar-sample.png`](docs/calendar-sample.png).

## Commands

| Command | Purpose |
| --- | --- |
| `poll` | One cycle. Use this from cron, a systemd timer, or Windows Task Scheduler |
| `run` | Loop at the configured interval |
| `status` | Baseline, latest snapshot, outbox counts. `--json` prints JSON |
| `list` | Detected notices and per-target delivery state |
| `send-test --channel telegram --target example-person` | Send a test message. It does not mean a reset happened |
| `manual --channel feishu --target example-group --event 3` | Re-send a stored notice with a new idempotency key |
| `manual --text "hello" --channel telegram --target example-person` | Send custom text |
| `manual ... --preview` | Print the payload and do not send |

`poll` exits 1 when the source fetch fails and 2 when the config is invalid. A failed target still exits 0 because the failure is stored and retried. `needs_review` means an ambiguous send stopped being retried; use `manual` if the chat never got it.

## Scheduling

### Linux / macOS cron

```cron
*/10 * * * * cd /opt/tibo-reset-monitor && . .venv/bin/activate && set -a && . ./.env && set +a && python -m tibo_reset_monitor poll --config /opt/tibo-reset-monitor/config.json
```

### systemd

Unit files live in `deploy/systemd/`. The service runs one `poll`; the timer fires it every 10 minutes.

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

Set `TIBO_FEISHU_APP_SECRET` and `TIBO_TELEGRAM_BOT_TOKEN` in the task environment. Set the task's working directory to the project directory so the relative state path stays stable.

### Docker

```bash
docker build -t tibo-reset-monitor .
docker run --rm -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor
```

The image runs `run` by default. The database is `/data` via `TIBO_STATE_PATH`. The image installs Pillow and WenQuanYi Micro Hei so the Telegram calendar can be drawn. Without those, notifications fall back to a single text message.

## Adding a channel

See [docs/adding-a-channel.md](docs/adding-a-channel.md). `src/tibo_reset_monitor/core/` does not import channels. A new channel is one module under `channels/` plus one registry entry.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

GitHub Actions runs those tests on Python 3.10 through 3.13. HTTP is mocked. The tests do not call Feishu or Telegram.

## FAQ

**Why were the resets from the past week not pushed on startup?**  
The first successful fetch is a baseline. That avoids dumping history into a chat.

**I added a personal target. Why was nothing backfilled?**  
A target only receives events detected after it is in the config. Use `manual --event` for an older notice.

**The group got it and the direct message did not.**  
Check `list`. A Telegram user has to press Start. A Feishu bot has to be in the group, and a personal id has to match `receive_id_type`. One target's failure does not cancel the others.

**What is `needs_review`?**  
After a timeout the program cannot tell whether the provider accepted the message. It retries with the same idempotency key, then stops after about 55 minutes so a later retry cannot slip past Feishu's dedupe window and send a duplicate. Use `manual` if you confirmed it never arrived.

**Is the confirmation time when credit hits the account?**  
No. AIHOT's `confirmedAt`, and the time shown here, are the source post's publish time. When credit appears on an account is whatever the product shows. `estimate` and `schedule` are windows from the feed, not an account receipt.

## License

MIT. Copyright 2026 LFenX. See [LICENSE](LICENSE).
