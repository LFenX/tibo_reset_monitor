# Webhook

The webhook channel is optional. Each target's URL comes from the environment variable named by `url_env` and must be `https`. Leave `secret_env` out and the request is unsigned, the same as before this field existed.

`config.example.json` shows both shapes: `example-hook` has only `url_env`, and `example-hook-signed` also names `secret_env`. The value of that variable is the HMAC secret. The config file stores the variable name, never the secret.

## Request

`POST` the target URL.

| Header | When | Meaning |
| --- | --- | --- |
| `Content-Type` | always | `application/json; charset=utf-8` |
| `Idempotency-Key` | always | Dedup key for this delivery. The same string is `idempotency_key` in the JSON. After a 2xx, that notice is not sent to this target again |
| `X-Tibo-Timestamp` | signed only | Unix time in seconds, as decimal text |
| `X-Tibo-Signature` | signed only | `v1=` plus lowercase hex HMAC-SHA256 |

A missing `secret_env` means those two signature headers are absent. If `secret_env` is set and the variable is empty, nothing is sent; the delivery stays retryable.

2xx is success. 429 and 5xx are retried. Other 4xx results are permanent.

## JSON body

| Field | Meaning |
| --- | --- |
| `idempotency_key` | Same value as the `Idempotency-Key` header |
| `language` | `zh` or `en`, the language used for the copy fields |
| `target.name` | Target name from the config |
| `target.kind` | `group` or `personal` |
| `title` | Localized title |
| `tags` | Localized tag and stage labels |
| `scope` | Localized scope line, or empty |
| `text` | Localized body. In `en` this is the original post, plus one quote line when the post quotes or replies |
| `posted` | Localized publish time with the target's UTC offset, or empty for a manual message |
| `notice` | Raw notice. `stage`, `url`, `published_at`, and `text` are unchanged source fields. `text` here can still be the Chinese translation when `language` is `en` |

## Idempotency-Key

Store the header value when you accept a delivery. If the same key arrives again, return 2xx and do not apply the notice a second time. The monitor reuses the key when a timeout leaves the outcome unknown, and stops retrying that row after about 55 minutes.

## Signature

When signing is on, the MAC is HMAC-SHA256 over the UTF-8 secret and the bytes `<timestamp>.<raw body>`. `timestamp` is the `X-Tibo-Timestamp` header. `raw body` is the exact HTTP body, not a re-serialized JSON object.

1. Read the raw body before parsing JSON.
2. Reject the request when `X-Tibo-Timestamp` is not an integer, or when it is outside your window (five minutes is a reasonable default).
3. Recompute `v1=<hex>` and compare with `hmac.compare_digest` or `crypto.timingSafeEqual`. Compare length first in Node, because `timingSafeEqual` throws when the buffers differ in length.
4. Then dedupe on `Idempotency-Key`.

```python
import hashlib
import hmac
import time


def verify(secret: str, body: bytes, timestamp: str, signature: str, window: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > window:
        return False
    expected = "v1=" + hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

```javascript
const crypto = require("crypto");

function verify(secret, body, timestamp, signature, windowSec = 300) {
  const ts = Number(timestamp);
  if (!Number.isFinite(ts) || Math.abs(Date.now() / 1000 - ts) > windowSec) return false;
  const expected =
    "v1=" + crypto.createHmac("sha256", secret).update(`${ts}.`).update(body).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(String(signature));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

`body` in the Node example must be a `Buffer` of the raw request, not a string that has already been parsed and printed again.
