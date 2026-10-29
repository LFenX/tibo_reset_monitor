# 使用指南

[返回首页](../README.zh-CN.md) · [English](usage.md) · [机器人配置](chat-setup.md) · [Webhook](webhook.md)

## 1. 准备机器人

参照[机器人配置](chat-setup.md)创建 Telegram 机器人或飞书自建应用。每份部署使用自己的机器人及凭据。

下载仓库，创建本地配置文件：

```bash
git clone https://github.com/LFenX/tibo_reset_monitor.git
cd tibo_reset_monitor
cp config.minimal.example.json config.json
cp .env.example .env
```

PowerShell 也可使用 `Copy-Item` 复制文件。

编辑 **config.json**：

- 最小示例同时启用 Telegram 和飞书。将不用的渠道设为 `"enabled": false`。
- Telegram 保留 `bot_token_env` 为 `TIBO_TELEGRAM_BOT_TOKEN`。
- 飞书替换占位 `app_id`。在 `admins` 填管理员的 `open_id`；留空时使用应用协作者名单。
- 使用聊天订阅时，`targets` 可留空。静态目标、语言、时区与 Webhook 配置参阅[完整示例](../config.example.json)。

编辑 **.env**，填入已启用渠道的真实密钥。配置文件只保存环境变量名称，不保存密钥。两个本地文件均不提交到 Git。

## 启动监控

选择一种方式。需要聊天管理订阅时，保持 **run** 常驻；单次 **poll** 不监听机器人指令。

### Docker

需要 Docker。镜像已包含日历绘图、中英文字体、时区数据和飞书长连接依赖。

```bash
docker build -t tibo-reset-monitor .
docker run --rm --name tibo-reset-monitor -v "$PWD/config.json:/config/config.json:ro" -v tibo-state:/data --env-file .env tibo-reset-monitor --config /config/config.json run
```

启动命令将全局参数放在子命令前。保留状态卷，其中保存订阅与投递历史。最小配置只输出控制台日志；启用文件日志时使用 `/data/monitor.log` 等可写路径。

### Python：Linux / macOS

需要 Python 3.10+ 和 Git。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[calendar,feishu-bot]'
set -a
. ./.env
set +a
tibo-reset-monitor --config config.json run
```

两个可选扩展分别安装日历所需的 Pillow 和飞书对话监听所需的 lark-oapi。仅安装 `python -m pip install -e .` 也能运行核心功能；图片和飞书对话管理需安装对应扩展。中文日历还需要中文字体，未自动找到时通过 `calendar.font_path` 指定。

### Python：Windows / PowerShell

先将两个示例文件复制为本地配置，再执行：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[calendar,feishu-bot]'
$env:TIBO_TELEGRAM_BOT_TOKEN = 'replace-with-your-bot-token'
$env:TIBO_FEISHU_APP_SECRET = 'replace-with-your-app-secret'
tibo-reset-monitor --config config.json run
```

只设置已启用渠道使用的环境变量。Python **不会自动加载 .env**：Linux 示例显式导出变量，Docker 通过 `--env-file` 加载，PowerShell 显式设置环境变量。

## 3. 在聊天中订阅

### Telegram

1. 与机器人单聊，点击 **Start**。
2. 尚未配置或保存管理员时，从监控终端找到一次性配对码，在单聊发送 `/pair CODE`。配对码 10 分钟有效且只能使用一次；已有管理员时使用名单中的账号。
3. 发送 `/subscribe` 开启个人通知。群聊先加入机器人，再在私聊发送 `/addgroup`，于一分钟内回复列表编号或完整群名。管理员也可在群中直接发送 `/subscribe`。
4. 使用 `/unsubscribe`、`/removegroup`、`/status` 管理订阅，使用 `/help` 查看指南。

Telegram 对话管理使用轮询。已设置 Telegram webhook 时，需先移除 webhook，或关闭对话管理。

### 飞书

1. 启用机器人、消息权限和长连接事件，发布应用版本，并将使用者加入应用可用范围。
2. 配置管理员 `open_id`；`admins` 留空时使用应用协作者账号。
3. 与机器人单聊，发送 `#开启单聊通知`，或发送 `#添加群聊` 后回复列表编号或完整群名。你和机器人都需先加入目标群；应用需有[读取群成员权限](https://open.feishu.cn/document/server-docs/group/chat-member/get)，成员查询失败时拒绝群管理操作。
4. 使用 `#关闭单聊通知`、`#移除群聊`、`#状态` 管理订阅，使用 `#帮助` 查看指南。

默认只有管理员能管理订阅。将 `allow_self_subscribe` 设为 `true` 后，其他人可开关自己的个人通知，群聊管理仍仅限管理员。完整[权限和指令](chat-setup.md#谁能用权限与可用范围)见配置指南。

服务就绪后，每位管理员会收到一份当前版本的使用指南。Telegram 需先与机器人开始对话或成功配对才能收到。首次拉取只建立基线；新订阅不补发历史帖子。

自动通知以原帖发布时间判断，不使用事件更新时间。首次响应即使为空也建立基线；发布时间下界取基线、最近七天、来源窗口下界、上次本机成功检查前一小时中的最大值，未来时间容差为五分钟。历史回填只进入 `list`，供明确的 `manual --event` 请求使用，不自动发送。同一原帖重新分类不会产生一套新通知。

来源失败或检查中断会阻止自动投递，直到有效响应或校验通过的缓存 304 恢复。每个目标独立保留已尝试的正文与幂等键，未尝试目标使用最新分类。撤回会取消关联的未完成自动投递，同帖改为撤回、历史撤回帖本身不触发新通知时也适用；来源移除原帖同样取消其自动待发。无法核实的旧队列或目标变化转为 `needs_review`。可信迁群更新未尝试任务的目标，已尝试任务的目标变化等待人工核验。提交请求后断连也视为结果不明；结果不明的发送在 55 分钟后停止盲目重试。Telegram 无服务端幂等键，结果不明时直接等待人工核验。

自动队列项固定入队时的原帖发布时间、来源链接、时间下界和目标。正在发送的任务保存持久租约，另一轮轮询不会将其误判为中断发送。重启后等待租约到期再恢复，默认十分钟；请求超时配置更长时同步延长。

## 语言、时区和日历

通知默认中文。`language` 支持 `zh`、`en`，可设置在全局、渠道或单个接收目标，越具体的配置优先级越高。英文通知使用原帖内容。`timezone` 按相同顺序生效，接受 `Asia/Shanghai`、`America/Los_Angeles` 等 IANA 时区名称，消息时间包含接收者当时的 UTC 偏移。

能绘图时，Telegram 发送一条带文字说明的日历图片；不能绘图时发送一条文字消息。飞书发送卡片并可附日历，图片上传失败仍发送文字卡片。日历采用接收者所在时区的月份，仅使用一份校验通过的完整历史快照，整份替换新增、更正和删除，不与旧 recent 数据合并。刷新失败保留有效缓存；recent ETag 变化会在十分钟最小间隔后触发历史检查，最长每 24 小时重新校验。

## 查看投递状态

在与服务一致的 Python 环境、配置目录和状态库下执行：

```bash
tibo-reset-monitor --config config.json status
tibo-reset-monitor --config config.json list --limit 20
```

Docker 容器运行时：

```bash
docker exec tibo-reset-monitor python -m tibo_reset_monitor --config /config/config.json status
docker exec tibo-reset-monitor python -m tibo_reset_monitor --config /config/config.json list --limit 20
```

| 现象 | 检查项 |
| --- | --- |
| 刚启动没有消息 | 首次成功拉取只建立基线；使用机器人状态或帮助确认订阅 |
| Telegram 单聊无消息 | 点击 Start，确认管理员或自助订阅设置，再开启订阅 |
| 飞书找不到机器人 | 应用可用范围、版本发布、权限与长连接事件订阅 |
| 群列表缺少目标群 | 先将机器人加群；Telegram 需由运行中的进程收到加群事件 |
| 没有日历图片 | Pillow 与字体；飞书还需图片上传权限 |
| 订阅停止 | 连续三次永久投递失败会停用；修复接收端后重新订阅 |

## 其他运行方式

聊天管理使用常驻 **run**。定时执行单次拉取时，先配置静态目标，再使用：

```bash
tibo-reset-monitor --config config.json poll
```

同一状态库只运行一个监控进程。使用现有 systemd 模板时，服务的 `ExecStart` 需将 `--config` 放在 `poll` 前；定时器的单次拉取不监听对话。改用常驻服务时使用 `run` 并停用定时器。现有模板见[服务文件](../deploy/systemd/)。

自定义通知渠道与可选 HMAC 签名见 [Webhook 文档](webhook.md)，实现新渠道见[扩展指南](adding-a-channel.md)。
