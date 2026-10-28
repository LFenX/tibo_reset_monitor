<p align="center">
  <img src="docs/readme-cover.png" alt="Tibo, patron saint of resets, pressing RESET while programmers await the signal" width="100%">
</p>

<h1 align="center">tibo-reset-monitor</h1>

<p align="center">
  <strong>He resets. We report.</strong><br>
  Tibo 发布 Codex / ChatGPT 额度重置动态，自动通知你。<br>支持飞书、Telegram，或通过 Webhook 接入自定义通知渠道。
</p>

<p align="center">
  <a href="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml"><img src="https://github.com/LFenX/tibo_reset_monitor/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI on main"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-D4AF37" alt="MIT License"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/English-3776AB" alt="English"></a>
</p>

<p align="center">
  <a href="#交给-agent">交给 Agent</a> · <a href="docs/usage.zh-CN.md">开始使用</a> · <a href="docs/chat-setup.md">机器人配置</a> · <a href="docs/webhook.md">Webhook</a>
</p>

## 交给 Agent

把下面的提示词发给你的 Agent：

> 请帮我在当前机器部署 https://github.com/LFenX/tibo_reset_monitor 。先阅读 README 和使用指南，检查环境并复用已有配置。仅补问缺失的通知渠道、语言和时区，引导我完成账号操作，并将必要密钥写入本地环境变量。完成依赖安装、配置和启动，验证监控状态，并告诉我如何在机器人中订阅。

## 开始使用

1. **连接机器人。** 创建 Telegram 机器人或飞书应用，填好凭据。[机器人配置 →](docs/chat-setup.md)
2. **启动监控。** 选择 Docker 或 Python，启用需要的通知渠道，保持服务运行。[安装指南 →](docs/usage.zh-CN.md#启动监控)
3. **在聊天中订阅。** 按提示完成管理员配对，通过机器人开启个人通知或选择群聊。默认仅管理员可管理订阅。[订阅指南 →](docs/chat-setup.md#指令)

首次拉取只建立基线，此后接收新动态，不补发历史帖子。

## 功能

- 跟踪预告、确认、补充和撤回。
- 在聊天中管理个人及群聊订阅。
- Telegram 图文消息、飞书卡片，可附重置日历。
- 按接收者设置语言和时区，持久化状态，失败自动重试。

## 参与贡献

欢迎提交 Issue 或 Pull Request。参阅[贡献指南](CONTRIBUTING.md)或[扩展通知渠道](docs/adding-a-channel.md)。

## 来源与许可

动态来自 [AIHOT](https://aihot.news/) 和 [Tibo 的公开帖子](https://x.com/thsottiaux)。账户额度是否到账，以产品内显示为准。

[MIT License](LICENSE) · © 2026 LFenX。
