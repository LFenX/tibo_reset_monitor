# Adding a channel

The core turns an AIHOT snapshot into neutral `Notice` values and stores one outbox row per target. It does not know how a chat product works. A channel is one module that:

1. `render(notice, target) -> dict` turns a `Notice` into the payload you will POST.
2. `send(message, target, idempotency_key) -> Receipt` performs the HTTP call.
3. Validates its own config shape via the loader in `config.py` (kind is `group` or `personal`).

Language is not channel-specific. `zh` and `en` are required for every channel, including ones added later. Copy lives in `tibo_reset_monitor.core.i18n`: `compose(notice, language)` returns an `OutboundMessage` whose title, tags, scope, body, quote line, link label, and calendar caption are already in that language. A channel only formats that model (Telegram HTML, a Feishu card, webhook JSON, or whatever the new API wants).

`language` is configured in three places, and the later one wins:

1. The root `language` field.
2. `channels.<name>.language`.
3. `channels.<name>.targets[].language`.

Omitted values inherit. The default is `zh`. The loader rejects anything other than `zh` or `en` with the field path. Resolved values are stored on `target.options["language"]` and `ChannelSpec.settings["language"]`, so a channel reads the target and does not reimplement the cascade.

`en` uses the post's original text (`fullOriginalText`, otherwise `originalText`), not the Chinese translation, and does not include the original-post quote card. A quoted or replied-to post is one line on `OutboundMessage.quote_line` (`Quoting @handle: …` or `Replying to @handle: …`). `zh` keeps the existing Chinese wording. Raw ids, the canonical `stage`, timestamps, and URLs stay on the `Notice` and must not be translated.

`timezone` uses the same three levels as `language`: root, channel, then target. Pass `target.options["timezone"]` into `compose(..., timezone_name=...)`. Stamps are rendered in that zone with the offset at that instant (`UTC+8`, `UTC-7`, `UTC+5:30`). Do not write a city name into the copy. If a channel draws the calendar, call `png(language, timezone_name)` and `month(timezone_name)` so two targets in different zones do not share one image. A date-only source field stays on that day; a timestamp is converted.

`Receipt.outcome` is one of:

| Outcome | Meaning |
| --- | --- |
| `sent` | Provider accepted it. Include `provider_message_id` when there is one |
| `retryable` | Clear failure. The same outbox row and idempotency key will be retried |
| `permanent` | The request will not succeed until a person changes something (unknown chat, blocked bot) |
| `ambiguous` | Usually a timeout. The same idempotency key is reused, then the row becomes `needs_review` after about 55 minutes |

Read secrets from environment variables named in the config. Do not log tokens or put them in exception text. Use `tibo_reset_monitor.core.http.UrllibTransport`, which refuses cross-host redirects and strips bot tokens from errors.

The built-in webhook documents its body, `Idempotency-Key`, and optional HMAC in [webhook.md](webhook.md). A new channel that signs requests should sign the bytes it actually sends and keep the secret out of logs, the state database, and error text.

## Chat control

Subscriptions made by talking to the bot live in the state database (`subscription`) and are merged with static `targets` at send time. Do not rewrite the user's config file. A new channel joins this by:

1. Setting `supports_chat = True` on the channel class.
2. Turning each inbound event into `tibo_reset_monitor.core.chat.Inbound` with only `event_id`, chat id, user id, text, and optional membership or migration fields. Do not store the raw payload or any token.
3. Calling `ChatController.handle(inbound)` and sending the `Reply` yourself.
4. Supplying `group_catalog()` when the platform can list groups the bot is in. Otherwise the controller uses chats recorded from membership updates.
5. Implementing `send_text(recipient, text, *, idempotency_key) -> Receipt` if the channel should deliver the startup usage guide. The guide text comes from `chat_text(language, "guide")`. Bump `GUIDE_VERSION` in `core/i18n.py` when that copy changes.

Command names, the one-minute picker, admin checks, and subscription rows are shared. Put new reply sentences in `core/i18n.py` (`chat_text`) for both `zh` and `en`. `tests/test_chat.py` renders help for every registered channel that sets `supports_chat`.

## Steps

1. Add `src/tibo_reset_monitor/channels/your_channel.py`.
2. Register the class in `REGISTRY` inside `src/tibo_reset_monitor/channels/__init__.py`.
3. Teach `config.py` the channel's allowed keys and how a group target differs from a personal target.
4. Add a mocked HTTP test that asserts the request for both kinds, in `zh` and in `en`.
5. Document the setup in `README.md` and `README.en.md`.
6. Allow `language` on the channel object and on each target in `config.py`. Do not invent a second language key.

`tests/test_i18n.py` constructs every class in `REGISTRY` and renders one notice in both languages. A new channel that still emits Chinese copy when `target.options["language"]` is `en` fails that test. Keep localized strings out of the raw `notice` object so the test can tell them apart.

Skeleton:

```python
from tibo_reset_monitor.core.i18n import compose, normalize_language

class ExampleChannel:
    name = "example"

    def __init__(self, spec, transport, clock):
        self.targets = list(spec.targets)
        self._transport = transport
        self._language = normalize_language(spec.settings.get("language"))

    def render(self, notice, target):
        language = normalize_language(target.options.get("language"), self._language)
        message = compose(notice, language)
        return {
            "language": message.language,
            "title": message.title,
            "tags": list(message.tags),
            "scope": message.scope,
            "text": message.text,
            "link_label": message.link_label,
            "audience": target.kind,
        }

    def send(self, message, target, idempotency_key):
        # POST message. Put idempotency_key in the provider's dedupe field
        # when it has one; otherwise the outbox is the only dedupe.
        ...
```

Group and personal targets both go through this class. Put the difference in the request (chat id versus user id), not in a second code path in the core.

Chinese Feishu cards still use `notice_lines(notice)` inside `compose` for `zh`, including the note that the timestamp is the source post time. English cards use `OutboundMessage.card_body`. New channels should prefer `compose` over calling `notice_lines` directly, so `en` does not leak Chinese labels.

The month grid lives in `core/calendar.py` (`calendar_model`, `render_calendar_png`). Pass `language="en"` for the English grid. Chinese images need the optional `calendar` extra (`Pillow`) and a CJK font. English images use a Latin face (DejaVu, Liberation, Arial, or Noto Sans) and still render when no CJK font is installed. If Pillow or the face is missing, `render_calendar_png` raises `CalendarImageUnavailable` and the channel should send the text without failing the delivery. Bind `HistoryCalendar.png` with `bind_calendar` if the channel wants the image; `png(language)` selects the face.
