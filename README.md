<p align="center">
  <img src="docs/readme-cover.png" alt="Tibo, patron saint of resets, pressing RESET while programmers await the signal" width="100%">
</p>

<h1 align="center">tibo-reset-monitor</h1>

<p align="center">
  <strong>He resets. We report.</strong><br>
  Get notified when Tibo posts Codex or ChatGPT quota-reset updates.<br>Feishu, Telegram, or your own notification channels via Webhook.
</p>

<p align="center">
  <a href="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml"><img src="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI on main"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-D4AF37" alt="MIT License"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/%E4%B8%AD%E6%96%87-3776AB" alt="中文"></a>
</p>

<p align="center">
  <a href="#agent-setup">Agent setup</a> · <a href="docs/usage.md">Get started</a> · <a href="docs/chat-setup.md#chat-control">Bot setup</a> · <a href="docs/webhook.md">Webhook</a>
</p>

## Agent setup

Copy this prompt to your agent:

> Set up https://github.com/LFenX/tibo_reset_monitor on this machine. Read its README and user guide, inspect the environment, and reuse any existing configuration. Ask only for missing channel, language and time zone choices; guide me through account steps and setting required secrets in local environment variables. Install the dependencies, configure and start the monitor, verify its status, and explain how to subscribe.

## Get started

1. **Connect your bot.** Create a Telegram bot or Feishu app and add its credentials. [Bot setup →](docs/chat-setup.md#chat-control)
2. **Start the monitor.** Choose Docker or Python, enable the channels you use, and keep the service running. [Installation guide →](docs/usage.md#start-the-monitor)
3. **Subscribe in chat.** Pair as an admin if needed, then turn on personal notifications or select a group through your bot. Admin access is required by default. [Subscription guide →](docs/chat-setup.md#commands)

The first fetch establishes a baseline. You receive new updates from then on, without a flood of old posts.

## Features

- Announcements, confirmations, revisions, and retractions.
- Personal and group subscriptions managed in chat.
- Optional calendar images in Telegram messages and Feishu cards.
- Language and time zone per recipient, with persistent state and automatic delivery retries.

## Contributing

Issues and pull requests are welcome. See [contributing](CONTRIBUTING.md) or [add a notification channel](docs/adding-a-channel.md).

## Source & license

Updates come from [AIHOT](https://aihot.news/) and [Tibo's public posts](https://x.com/thsottiaux). Account quota availability is shown in the product itself.

[MIT License](LICENSE) · © 2026 LFenX.
