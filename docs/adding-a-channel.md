# Adding a channel

The core turns an AIHOT snapshot into neutral `Notice` values and stores one outbox row per target. It does not know how a chat product works. A channel is one module that:

1. `render(notice, target) -> dict` turns a `Notice` into the payload you will POST.
2. `send(message, target, idempotency_key) -> Receipt` performs the HTTP call.
3. Validates its own config shape via the loader in `config.py` (kind is `group` or `personal`).

`Receipt.outcome` is one of:

| Outcome | Meaning |
| --- | --- |
| `sent` | Provider accepted it. Include `provider_message_id` when there is one |
| `retryable` | Clear failure. The same outbox row and idempotency key will be retried |
| `permanent` | The request will not succeed until a person changes something (unknown chat, blocked bot) |
| `ambiguous` | Usually a timeout. The same idempotency key is reused, then the row becomes `needs_review` after about 55 minutes |

Read secrets from environment variables named in the config. Do not log tokens or put them in exception text. Use `tibo_reset_monitor.core.http.UrllibTransport`, which refuses cross-host redirects and strips bot tokens from errors.

## Steps

1. Add `src/tibo_reset_monitor/channels/your_channel.py`.
2. Register the class in `REGISTRY` inside `src/tibo_reset_monitor/channels/__init__.py`.
3. Teach `config.py` the channel's allowed keys and how a group target differs from a personal target.
4. Add a mocked HTTP test that asserts the request for both kinds.
5. Document the setup in `README.md` and `README.en.md`.

Skeleton:

```python
class ExampleChannel:
    name = "example"

    def __init__(self, spec, transport, clock):
        self.targets = list(spec.targets)
        self._transport = transport

    def render(self, notice, target):
        return {"text": notice.headline(), "audience": target.kind}

    def send(self, message, target, idempotency_key):
        # POST message. Put idempotency_key in the provider's dedupe field
        # when it has one; otherwise the outbox is the only dedupe.
        ...
```

Group and personal targets both go through this class. Put the difference in the request (chat id versus user id), not in a second code path in the core.

`notice_lines(notice)` in `core/model.py` is the shared wording, including the note that the timestamp is the source post time. Reuse it so every channel says the same thing.
