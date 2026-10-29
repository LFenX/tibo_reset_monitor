"""Small HTTPS client: gzip, same-host redirects, and URLs that are safe to log."""

from __future__ import annotations

import gzip
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse, urlunparse

_BOT_TOKEN_IN_PATH = re.compile(r"/bot[^/\s]+")


class TransportError(Exception):
    """A request failed before a response body was available, or the transport refused it."""

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class CrossHostRedirect(TransportError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ambiguous=False)


def redact_url(url: str) -> str:
    """Remove bot tokens, userinfo, and query strings so a URL can be logged."""
    cleaned = _BOT_TOKEN_IN_PATH.sub("/bot<redacted>", url)
    parsed = urlparse(cleaned)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = _redact_path(parsed.path)
    redacted_query = "redacted=1" if parsed.query else ""
    return urlunparse((parsed.scheme, host, path, "", redacted_query, ""))


def _redact_path(path: str) -> str:
    pieces = []
    for piece in path.split("/"):
        if len(piece) >= 24 and re.search(r"[A-Za-z]", piece) and re.search(r"\d", piece):
            pieces.append("<redacted>")
        else:
            pieces.append(piece)
    return "/".join(pieces)


def _header_map(headers: Mapping[str, str] | urllib.request.addinfourl) -> dict[str, str]:
    mapping = getattr(headers, "items", None)
    if mapping is None:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    return {str(key).lower(): str(value) for key, value in headers.items()}


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        old = urlparse(req.full_url)
        new = urlparse(newurl)
        if not new.hostname or new.hostname != old.hostname:
            raise CrossHostRedirect(
                f"refused redirect from {old.hostname} to {new.hostname or 'unknown host'}"
            )
        if old.scheme == "https" and new.scheme != "https":
            raise CrossHostRedirect("refused redirect that would downgrade HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    """stdlib transport used by the source client and by channels."""

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        if opener is None:
            context = ssl.create_default_context()
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context),
                urllib.request.HTTPHandler(),
                _SameHostRedirectHandler(),
            )
        self._opener = opener

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        safe_url = redact_url(url)
        mutating = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        response_received = False
        try:
            response = self._opener.open(request, timeout=timeout)
            response_received = True
            status = getattr(response, "status", response.getcode())
            payload = response.read()
            header_map = _header_map(response.headers)
            final_url = response.geturl()
        except CrossHostRedirect:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read()
            header_map = _header_map(exc.headers)
            final_url = exc.geturl() if hasattr(exc, "geturl") else url
        except (TimeoutError, socket.timeout) as exc:
            raise TransportError(f"timeout talking to {safe_url}: {exc}", ambiguous=True) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            definitely_not_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError))
            ambiguous = isinstance(reason, (TimeoutError, socket.timeout)) or (mutating and not definitely_not_sent)
            raise TransportError(
                f"request to {safe_url} failed: {reason}",
                ambiguous=ambiguous,
            ) from exc
        except ssl.SSLError as exc:
            ambiguous = mutating and (response_received or not isinstance(exc, ssl.SSLCertVerificationError))
            raise TransportError(f"TLS error talking to {safe_url}: {exc}", ambiguous=ambiguous) from exc
        except OSError as exc:
            ambiguous = mutating and (response_received or not isinstance(exc, (socket.gaierror, ConnectionRefusedError)))
            raise TransportError(f"network error talking to {safe_url}: {exc}", ambiguous=ambiguous) from exc

        encoding = header_map.get("content-encoding", "")
        if "gzip" in encoding.lower() and payload:
            try:
                payload = gzip.decompress(payload)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                raise TransportError(f"gzip body from {safe_url} could not be read", ambiguous=mutating) from exc
        return HttpResponse(status=status, headers=header_map, body=payload, url=redact_url(final_url))
