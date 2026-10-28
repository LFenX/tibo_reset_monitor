"""JSON configuration. Secrets are environment variable names, never values."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tibo_reset_monitor import __version__
from tibo_reset_monitor.core.calendar import default_history_url
from tibo_reset_monitor.core.model import Target

DEFAULT_SOURCE_URL = "https://aihot.news/api/v1/codex-resets/recent"
DEFAULT_USER_AGENT = f"tibo-reset-monitor/{__version__} (+https://github.com/LFenX/tibo_reset_monitor)"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
TARGET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOKEN_LIKE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
TELEGRAM_PERSONAL = re.compile(r"^[1-9]\d{0,18}$")
TELEGRAM_GROUP_ID = re.compile(r"^-[1-9]\d{0,18}$")
TELEGRAM_GROUP_NAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")

_ROOT_KEYS = {"language", "source", "state", "logging", "channels", "calendar"}
_SOURCE_KEYS = {"url", "poll_interval_seconds", "timeout_seconds", "user_agent", "history_url"}
_CALENDAR_KEYS = {"font_path", "font_bold_path", "font_index", "history_refresh_seconds"}
_STATE_KEYS = {"path"}
_LOGGING_KEYS = {"level", "file"}
_CHANNEL_KEYS = {
    "feishu": {"enabled", "api_base", "app_id", "app_id_env", "app_secret_env", "language", "targets"},
    "telegram": {"enabled", "api_base", "bot_token_env", "language", "targets"},
    "webhook": {"enabled", "language", "targets"},
}
_FEISHU_TARGET_KEYS = {"name", "kind", "receive_id", "receive_id_type", "language"}
_TELEGRAM_TARGET_KEYS = {"name", "kind", "chat_id", "language"}
_LANGUAGES = {"zh", "en"}
_WEBHOOK_TARGET_KEYS = {"name", "kind", "url_env", "language"}
_FORBIDDEN_KEYS = {"app_secret", "bot_token", "token", "secret", "password", "access_token", "webhook_url"}
_PERSONAL_ID_TYPES = {"open_id", "user_id", "email"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class ChannelSpec:
    name: str
    enabled: bool
    settings: dict[str, str]
    targets: list[Target] = field(default_factory=list)


@dataclass
class AppConfig:
    source_url: str
    poll_interval_seconds: int
    timeout_seconds: float
    user_agent: str
    state_path: str
    log_level: str
    log_file: str | None
    channels: list[ChannelSpec]
    history_url: str = ""
    history_refresh_seconds: int = 6 * 60 * 60
    font_path: str = ""
    font_bold_path: str = ""
    font_index: int | None = None
    language: str = "zh"

    @property
    def stale_lock_seconds(self) -> float:
        return max(900.0, float(self.poll_interval_seconds) + 180.0)


def load_config(path: str | Path, *, environ: dict[str, str] | None = None) -> AppConfig:
    raw_path = Path(path)
    if not raw_path.is_file():
        raise ConfigError([f"config file not found: {raw_path}"])
    try:
        loaded = json.loads(raw_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"config is not valid JSON: {exc}"]) from exc
    if not isinstance(loaded, dict):
        raise ConfigError(["config root must be a JSON object"])
    errors: list[str] = []
    _reject_secrets(loaded, "$", errors)
    _unknown(loaded, _ROOT_KEYS, "$", errors)
    if "language" in loaded:
        global_language = _language(loaded.get("language"), "$.language", errors)
    else:
        global_language = "zh"
    source = _object(loaded.get("source", {}), "$.source", errors)
    state = _object(loaded.get("state", {}), "$.state", errors)
    logging_block = _object(loaded.get("logging", {}), "$.logging", errors)
    channels_block = loaded.get("channels", {})
    if not isinstance(channels_block, dict):
        errors.append("$.channels must be an object")
        channels_block = {}
    calendar_block = _object(loaded.get("calendar", {}), "$.calendar", errors)
    _unknown(source, _SOURCE_KEYS, "$.source", errors)
    _unknown(state, _STATE_KEYS, "$.state", errors)
    _unknown(logging_block, _LOGGING_KEYS, "$.logging", errors)
    _unknown(calendar_block, _CALENDAR_KEYS, "$.calendar", errors)

    source_url = str(source.get("url") or DEFAULT_SOURCE_URL)
    if not source_url.startswith("https://"):
        errors.append("$.source.url must be an https URL")
    poll_interval = _int(source.get("poll_interval_seconds", 600), "$.source.poll_interval_seconds", errors)
    if poll_interval < 60:
        errors.append("$.source.poll_interval_seconds must be at least 60")
    timeout = source.get("timeout_seconds", 20)
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError):
        errors.append("$.source.timeout_seconds must be a number")
        timeout_seconds = 20
    else:
        if not 1 <= timeout_seconds <= 120:
            errors.append("$.source.timeout_seconds must be between 1 and 120")
    user_agent = str(source.get("user_agent") or DEFAULT_USER_AGENT)
    if len(user_agent) > 2048:
        errors.append("$.source.user_agent must be at most 2048 characters")

    env = environ if environ is not None else os.environ
    state_path = str(env.get("TIBO_STATE_PATH") or state.get("path") or "state/monitor.sqlite3")
    log_level = str(logging_block.get("level") or "INFO").upper()
    if log_level not in _LOG_LEVELS:
        errors.append("$.logging.level must be DEBUG, INFO, WARNING, or ERROR")
    log_file = logging_block.get("file")
    if log_file is not None and not isinstance(log_file, str):
        errors.append("$.logging.file must be a string or omitted")
        log_file = None

    history_url = str(source.get("history_url") or default_history_url(source_url))
    if not history_url.startswith("https://"):
        errors.append("$.source.history_url must be an https URL")
    refresh_raw = calendar_block.get("history_refresh_seconds", 6 * 60 * 60)
    history_refresh_seconds = _int(refresh_raw, "$.calendar.history_refresh_seconds", errors)
    if history_refresh_seconds < 600:
        errors.append("$.calendar.history_refresh_seconds must be at least 600")
    font_path = calendar_block.get("font_path") or ""
    font_bold_path = calendar_block.get("font_bold_path") or ""
    if not isinstance(font_path, str):
        errors.append("$.calendar.font_path must be a string")
        font_path = ""
    if not isinstance(font_bold_path, str):
        errors.append("$.calendar.font_bold_path must be a string")
        font_bold_path = ""
    font_index: int | None = None
    if "font_index" in calendar_block and calendar_block.get("font_index") is not None:
        font_index = _int(calendar_block.get("font_index"), "$.calendar.font_index", errors)
        if font_index < 0:
            errors.append("$.calendar.font_index must be zero or greater")

    channels: list[ChannelSpec] = []
    for name, raw_channel in channels_block.items():
        path_prefix = f"$.channels.{name}"
        if name not in _CHANNEL_KEYS:
            errors.append(f"{path_prefix} is not a known channel (feishu, telegram, webhook)")
            continue
        if not isinstance(raw_channel, dict):
            errors.append(f"{path_prefix} must be an object")
            continue
        _unknown(raw_channel, _CHANNEL_KEYS[name], path_prefix, errors)
        enabled = raw_channel.get("enabled", False)
        if not isinstance(enabled, bool):
            errors.append(f"{path_prefix}.enabled must be true or false")
            enabled = False
        settings, targets = _channel(name, raw_channel, path_prefix, global_language, errors)
        channels.append(ChannelSpec(name=name, enabled=enabled, settings=settings, targets=targets))

    if errors:
        raise ConfigError(errors)
    return AppConfig(
        source_url=source_url,
        poll_interval_seconds=poll_interval,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        state_path=state_path,
        log_level=log_level,
        log_file=log_file if isinstance(log_file, str) and log_file else None,
        channels=channels,
        history_url=history_url,
        history_refresh_seconds=history_refresh_seconds,
        font_path=font_path,
        font_bold_path=font_bold_path,
        font_index=font_index,
        language=global_language if global_language in _LANGUAGES else "zh",
    )


def _channel(
    name: str,
    raw: dict[str, Any],
    prefix: str,
    inherited: str,
    errors: list[str],
) -> tuple[dict[str, str], list[Target]]:
    settings: dict[str, str] = {}
    if name == "feishu":
        settings["api_base"] = _https_base(raw.get("api_base") or "https://open.feishu.cn", f"{prefix}.api_base", errors)
        app_id = raw.get("app_id")
        app_id_env = raw.get("app_id_env")
        if app_id is not None and not isinstance(app_id, str):
            errors.append(f"{prefix}.app_id must be a string")
        if app_id_env is not None:
            _env_name(app_id_env, f"{prefix}.app_id_env", errors)
            settings["app_id_env"] = str(app_id_env)
        if isinstance(app_id, str) and app_id:
            settings["app_id"] = app_id
        if not settings.get("app_id") and not settings.get("app_id_env"):
            errors.append(f"{prefix} needs app_id or app_id_env")
        secret_env = raw.get("app_secret_env")
        _env_name(secret_env, f"{prefix}.app_secret_env", errors)
        if isinstance(secret_env, str):
            settings["app_secret_env"] = secret_env
    elif name == "telegram":
        settings["api_base"] = _https_base(raw.get("api_base") or "https://api.telegram.org", f"{prefix}.api_base", errors)
        token_env = raw.get("bot_token_env")
        _env_name(token_env, f"{prefix}.bot_token_env", errors)
        if isinstance(token_env, str):
            settings["bot_token_env"] = token_env
    if "language" in raw:
        settings["language"] = _language(raw.get("language"), f"{prefix}.language", errors)
    else:
        settings["language"] = inherited if inherited in _LANGUAGES else "zh"
    targets_raw = raw.get("targets", [])
    if not isinstance(targets_raw, list):
        errors.append(f"{prefix}.targets must be an array")
        return settings, []
    targets: list[Target] = []
    seen_names: set[str] = set()
    for index, item in enumerate(targets_raw):
        item_path = f"{prefix}.targets[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path} must be an object")
            continue
        allowed = {"feishu": _FEISHU_TARGET_KEYS, "telegram": _TELEGRAM_TARGET_KEYS, "webhook": _WEBHOOK_TARGET_KEYS}[name]
        _unknown(item, allowed, item_path, errors)
        target_name = item.get("name")
        if not isinstance(target_name, str) or not TARGET_NAME.match(target_name):
            errors.append(f"{item_path}.name must match {TARGET_NAME.pattern}")
            continue
        if target_name in seen_names:
            errors.append(f"{item_path}.name {target_name!r} is duplicated")
        seen_names.add(target_name)
        kind = item.get("kind")
        if kind not in ("group", "personal"):
            errors.append(f"{item_path}.kind must be 'group' or 'personal'")
            continue
        address, options = _target_address(name, kind, item, item_path, errors)
        options = dict(options)
        if "language" in item:
            options["language"] = _language(item.get("language"), f"{item_path}.language", errors)
        else:
            options["language"] = settings.get("language", "zh")
        targets.append(Target(channel=name, key=target_name, kind=kind, address=address, options=options))
    return settings, targets


def _target_address(
    channel: str,
    kind: str,
    item: dict[str, Any],
    path: str,
    errors: list[str],
) -> tuple[str, dict[str, str]]:
    if channel == "feishu":
        receive_id = item.get("receive_id")
        if not isinstance(receive_id, str) or not receive_id.strip():
            errors.append(f"{path}.receive_id is required")
            receive_id = ""
        id_type = item.get("receive_id_type")
        if id_type is None:
            id_type = "chat_id" if kind == "group" else "open_id"
        if kind == "group" and id_type != "chat_id":
            errors.append(f"{path}.receive_id_type for a group must be chat_id")
        if kind == "personal" and id_type not in _PERSONAL_ID_TYPES:
            errors.append(f"{path}.receive_id_type for a personal target must be open_id, user_id, or email")
        if id_type == "email" and isinstance(receive_id, str) and "@" not in receive_id:
            errors.append(f"{path}.receive_id must look like an email when receive_id_type is email")
        return str(receive_id).strip(), {"receive_id_type": str(id_type)}
    if channel == "telegram":
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            errors.append(f"{path}.chat_id is required and must be a string")
            return "", {}
        chat_id = chat_id.strip()
        if kind == "personal" and not TELEGRAM_PERSONAL.match(chat_id):
            errors.append(
                f"{path}.chat_id for a personal target must be the user's positive numeric id "
                "(the user has to press Start in the bot first)"
            )
        if kind == "group" and not (TELEGRAM_GROUP_ID.match(chat_id) or TELEGRAM_GROUP_NAME.match(chat_id)):
            errors.append(
                f"{path}.chat_id for a group target must be a negative id like -100... or a public @channelusername"
            )
        return chat_id, {}
    url_env = item.get("url_env")
    _env_name(url_env, f"{path}.url_env", errors)
    return "", {"url_env": str(url_env or "")}


def _reject_secrets(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                errors.append(f"{path}.{key} is not allowed; reference an environment variable name instead")
            _reject_secrets(item, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]", errors)
    elif isinstance(value, str) and TOKEN_LIKE.search(value):
        errors.append(f"{path} looks like a bot token; put tokens in the environment, not the config file")


def _unknown(block: dict[str, Any], allowed: set[str], path: str, errors: list[str]) -> None:
    for key in block:
        if key not in allowed:
            errors.append(f"{path}.{key} is not a recognized setting")


def _object(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return {}
    return value


def _int(value: Any, path: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{path} must be an integer")
        return 0
    return value


def _language(value: Any, path: str, errors: list[str]) -> str:
    if value in _LANGUAGES:
        return str(value)
    errors.append(f'{path} must be "zh" or "en"')
    return "zh"


def _env_name(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not ENV_NAME.match(value):
        errors.append(f"{path} must be an environment variable name like TIBO_FEISHU_APP_SECRET")


def _https_base(value: Any, path: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or value.endswith("/"):
        errors.append(f"{path} must be an https origin without a trailing slash")
        return "https://invalid.invalid"
    return value
