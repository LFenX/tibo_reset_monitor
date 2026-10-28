# 用对话开关通知

`chat_control` 默认是 `true`。`targets` 可以留空，订阅写在状态库里，不会改你的配置文件。静态写在配置里的目标和对话订阅会合并；新订阅只接收订阅之后出现的新通知，不会补发旧消息。

最小配置见仓库里的 [`config.minimal.example.json`](../config.minimal.example.json)。密钥只放环境变量。

`welcome_message` 默认是 `true`。进程拿到锁并且对话管理就绪后，会给配置里的管理员各发一条使用指南：Telegram 用数字 chat id，飞书用 `open_id`。飞书的 `admins` 为空时，改发给应用协作者；协作者也拿不到就跳过，不反复请求。Telegram 在对方还没给机器人发过消息时发不出去，这时记一条日志并跳过，等对方第一次 `/start` 或配对成功再发。同一份指南对同一个人只发一次，记在状态库里；指南内容版本变化后才会再发。`welcome_message` 设为 `false`，或启动时加 `run --no-welcome`，都不会发。发送失败会按通知一样稍后重试，不会挡住额度轮询，日志里也不会出现 token 或配对码。

## 指令

| 作用 | Telegram | 飞书 / 中文别名 |
| --- | --- | --- |
| 帮助 | `/start` `/help` | 任意不是指令的文字，或 `/help` |
| 状态（仅管理员） | `/status` | `#状态` |
| 开启自己的单聊 | `/subscribe` | `#开启单聊通知` |
| 关闭自己的单聊 | `/unsubscribe` | `#关闭单聊通知` |
| 列出可添加的群 | `/addgroup` | `#添加群聊` |
| 列出已订阅的群 | `/removegroup` | `#移除群聊`、`#取消群聊`、`#取消通知` |
| 退出选择 | `/cancel` | `#取消` |
| 在群里直接订阅该群 | 在群里发 `/subscribe` | 在群里发 `/subscribe` |
| 首次成为管理员 | `/pair <配对码>` | 见下方飞书管理员 |
| 添加其他管理员 | `/addadmin <用户 id>` | `/addadmin <open_id>` |

选择群时要在私聊里回复编号或完整群名，1 分钟内有效，一次一个群。在群里发数字不会选中。超时要重新发指令。重名必须用编号。只有最新一次列出的名单有效；如果这次列群失败，上一份名单作废。

`allow_self_subscribe` 默认 `false`：只有管理员能给自己开单聊。设为 `true` 后，任何人都能给自己开单聊，但不能管理群。

连续 3 次永久失败（例如机器人被移出群、用户停用了机器人）会停止该订阅，`/status` 里能看到原因。

界面以各平台当时的页面为准，下面不假设具体的菜单层级。

## Telegram

1. 在 Telegram 里打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示取一个名字和用户名。BotFather 会给你 bot token。
2. 把 token 放进环境变量 `TIBO_TELEGRAM_BOT_TOKEN`，不要写进配置文件或聊天记录。
3. Privacy mode：如果机器人要在群里看到 `/subscribe` 这类以 `/` 开头的指令，BotFather 的默认设置通常就够用。如果它还要看到不以 `/` 开头的中文指令（如 `#添加群聊`），需要在 BotFather 里关闭 privacy mode。以 BotFather 当前说明为准。
4. 把机器人加进要通知的群。程序会通过 `my_chat_member` 记住这些群，因为 Bot API 不能列出机器人所在的全部群。
5. 配置里 `admins` 可以预先写上你的数字用户 id。如果 `admins` 为空，并且状态库里也还没有管理员，`run` 会在标准输出打印一次性配对码（约 10 分钟内有效，用过即失效）。在与机器人的单聊里发送 `/pair <码>`。配对码不会写入状态库。
6. 之后用 `/subscribe` 开单聊，或 `/addgroup` 选群。也可以在群里直接发 `/subscribe`。

`getUpdates` 和 `setWebhook` 不能同时使用。如果日志里出现 webhook 冲突，在 BotFather 或 `deleteWebhook` 里删掉 webhook，或把 `chat_control` 设为 `false`。

常见问题：单聊收不到，先在机器人里点 Start。群里指令没反应，看 privacy mode，以及你是不是管理员。

## 飞书

1. 在飞书开放平台创建企业自建应用。具体入口以开放平台当前页面为准。
2. 打开机器人能力。
3. 开通权限：`im:message:send_as_bot`（发消息）、接收消息所需的 `im:message.receive_v1` 相关权限、读取机器人所在群所需的权限（用于列出群）。如果 `admins` 留空，还需要能读取应用协作者的权限；更省事的做法是在 `admins` 里写上管理员的 `open_id`。
4. 事件订阅选择「使用长连接接收事件」，并订阅 `im.message.receive_v1`。以开放平台当前的事件订阅页面为准。
5. 发布版本，把机器人加进群，并把要管理通知的人设为应用协作者，或写入 `admins`。
6. 安装可选依赖：`pip install 'tibo-reset-monitor[feishu-bot]'`（包名 `lark-oapi`）。没安装时，对话管理关闭，配置里的 targets 仍然会收到通知，日志里会写原因。
7. App ID 写入 `app_id`。App Secret 放进 `TIBO_FEISHU_APP_SECRET`。

飞书管理员：配置了 `admins` 时只认这份 open_id 名单。名单为空时，改认应用协作者接口返回的用户。两者都没有时，管理指令会被拒绝。

在与机器人的单聊里发送 `#添加群聊`。机器人列出它已加入、尚未订阅的群。回复编号或完整群名。`#移除群聊` 列出已订阅的群。

常见问题：没有收到事件，确认事件订阅是长连接并且已发布版本。列不出群，确认机器人已在群里，以及读取群列表的权限已开通。

## Chat control

`chat_control` defaults to `true`. `targets` may be empty. Subscriptions are stored in the state database and are not written back into the config file. Static targets and chat subscriptions are merged. A new subscription receives only notices detected after it was added.

The smallest file is [`config.minimal.example.json`](../config.minimal.example.json). Secrets stay in environment variables.

`welcome_message` defaults to `true`. After the process holds the lock and chat control is ready, each configured admin receives one usage guide: a Telegram numeric chat id, or a Feishu `open_id`. When Feishu `admins` is empty, the guide goes to the app collaborators instead; if those cannot be listed, it is skipped. Telegram cannot open a direct chat until that person has messaged the bot, so the send is skipped with one log line and is delivered on their first `/start` or successful `/pair`. The same guide version is sent once per person and recorded in the state database. A new guide version is sent again. Set `welcome_message` to `false`, or start with `run --no-welcome`, to turn this off. A failed send is retried on a later poll, the same way notifications are, and does not stop quota polling. Logs do not include the token or a pairing code.

### Commands

| Action | Telegram | Feishu / Chinese alias |
| --- | --- | --- |
| Help | `/start` `/help` | any non-command text, or `/help` |
| Status (admins) | `/status` | `#状态` |
| Turn on your direct messages | `/subscribe` | `#开启单聊通知` |
| Turn them off | `/unsubscribe` | `#关闭单聊通知` |
| List groups you can add | `/addgroup` | `#添加群聊` |
| List subscribed groups | `/removegroup` | `#移除群聊`, `#取消群聊`, `#取消通知` |
| Leave the picker | `/cancel` | `#取消` |
| Subscribe the current group | `/subscribe` in the group | `/subscribe` in the group |
| Become the first admin | `/pair <code>` | see Feishu admins below |
| Add another admin | `/addadmin <user id>` | `/addadmin <open_id>` |

Reply in the direct chat with a number or the full group name. A number sent inside a group does not select anything. The choice lasts one minute and selects one group. When it expires, send the command again. Duplicate names require the number. Only the latest list counts. If that list fails to load, the previous list is discarded.

`allow_self_subscribe` defaults to `false`, so only an admin can turn on their own direct messages. Set it to `true` to let anyone do that. They still cannot manage groups.

Three permanent delivery failures in a row (the bot was removed, or the user blocked it) stop that subscription. `/status` shows the reason.

Menu labels follow the current platform UI. This document does not invent click-paths.

### Telegram

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. BotFather gives you the bot token.
2. Put the token in `TIBO_TELEGRAM_BOT_TOKEN`. Do not put it in the config file or in a chat.
3. Privacy mode: BotFather's default is usually enough for commands that start with `/`. To see Chinese commands such as `#添加群聊` inside a group, turn privacy mode off in BotFather. Follow BotFather's current text.
4. Add the bot to the groups you care about. The program remembers them from `my_chat_member`, because the Bot API cannot list every group a bot belongs to.
5. You can put numeric user ids in `admins`. If `admins` is empty and the state database has no admin yet, `run` prints a one-time pairing code on stdout. It lasts about 10 minutes and works once. Send `/pair <code>` in a direct chat. The code is not stored in the database.
6. Then `/subscribe` for direct messages, or `/addgroup` to pick a group. `/subscribe` inside a group subscribes that group.

`getUpdates` and `setWebhook` cannot both be active. If the log reports a webhook conflict, delete the webhook or set `chat_control` to `false`.

If a direct chat stays silent, press Start. If a group command does nothing, check privacy mode and that you are an admin.

### Feishu

1. Create a custom enterprise app on the Feishu open platform. Use the entry point shown on the current site.
2. Enable the bot.
3. Grant `im:message:send_as_bot`, the permission that backs `im.message.receive_v1`, and the permission to read chats the bot is in. If `admins` is empty, the app also needs permission to read collaborators. Putting `open_id`s in `admins` avoids that.
4. Subscribe to events with the long-connection option, and add `im.message.receive_v1`. Follow the current event-subscription page.
5. Publish a version, add the bot to groups, and make the people who will manage notifications collaborators, or list them in `admins`.
6. Install the optional extra: `pip install 'tibo-reset-monitor[feishu-bot]'` (`lark-oapi`). Without it, chat control stays off, configured targets still get notifications, and the log says why.
7. Put the App ID in `app_id`. Put the App Secret in `TIBO_FEISHU_APP_SECRET`.

Admins: a non-empty `admins` list is the only allowlist. When it is empty, the app's collaborator API is used instead. If both are empty, management commands are refused.

In a direct chat, send `#添加群聊`. The bot lists groups it has joined that are not subscribed yet. Reply with a number or the full name. `#移除群聊` lists subscribed groups.

If events never arrive, confirm the subscription uses the long connection and that a version is published. If the group list is empty, confirm the bot is in the group and that the chat-list permission is granted.

The Feishu long connection is covered by unit tests of the field mapping and the chat-list HTTP calls. A live check needs a test Feishu app; this repository does not call a production tenant.
