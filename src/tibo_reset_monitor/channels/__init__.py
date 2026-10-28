"""Channel registry. Add a channel by adding one module and one entry in REGISTRY."""

from __future__ import annotations

from typing import Any

from tibo_reset_monitor.channels.feishu import FeishuChannel
from tibo_reset_monitor.channels.telegram import TelegramChannel
from tibo_reset_monitor.channels.webhook import WebhookChannel
from tibo_reset_monitor.config import AppConfig

REGISTRY = {
    FeishuChannel.name: FeishuChannel,
    TelegramChannel.name: TelegramChannel,
    WebhookChannel.name: WebhookChannel,
}


def build_channels(config: AppConfig, transport: Any, clock: Any) -> list[Any]:
    channels = []
    for spec in config.channels:
        if not spec.enabled:
            continue
        factory = REGISTRY.get(spec.name)
        if factory is None:
            raise KeyError(spec.name)
        channels.append(factory(spec, transport, clock))
    return channels
