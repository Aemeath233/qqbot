# QQ 宿舍电量查询机器人

Python 3.12+、uv。默认使用 WebSocket 主动连接 QQ，不需要公网域名、HTTPS 证书、反向代理或消息回调地址。机器人只做一件事：通过兼容 OpenAI Chat Completions、支持函数调用的模型理解自然语言，并调用唯一的 `query_electricity` 工具查询明确宿舍当前的剩余电量。

例如在 QQ 群里 @机器人，发送“查一下33号楼2035室还有多少电”；也可私聊，或发送 `/电费 33#2035`。楼号或房号不明确、房间存在多个候选时，机器人会先询问，不猜测、不调用电费接口。自然语言模型未配置时，机器人不会尝试猜意图或请求校园接口。

消息直接交给模型，由 `tool_choice=auto` 决定调用工具或回复文本。没有正则关键词意图拦截；“看看33楼4032宿舍电费”“我住33幢4032，帮忙瞅瞅电还剩多少”等说法都交给模型理解。程序只校验工具名、参数结构、宿舍编号格式、目录匹配和请求频率。模型决定是否追问或拒绝无关问题；每条消息独立处理，补充信息时请提供完整楼号和房号。

电量请求由程序校验宿舍映射、缓存 30 秒，并通过共享 SQLite 冷却门限限制校园请求频率；不查询历史、不估算耗电、不保存个人资料、不生成图片或网页。用于回调去重和可靠回复的 QQ 消息队列仍保存在本地 SQLite。

## 安装和配置

```bash
uv sync --locked --no-dev
cp .env.example .env
nano .env
```

填写 QQ 开放平台的 `QQ_APP_ID`、`QQ_APP_SECRET`，以及兼容 OpenAI 的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。确认 `QQ_TRANSPORT=websocket`、`LLM_ENABLED=true`、`ELECTRICITY_ENABLED=true` 和学校编号。API Key、AppSecret、电费会话凭证只放在服务器 `.env`，不要提交到 GitHub。

`ELECTRICITY_SCHOOL_CODE=1402` 使用内置宿舍目录；其他学校请配置对应的学校编号及映射文件。

使用 DeepSeek 官方服务时配置：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=你的DeepSeek密钥
```

DeepSeek 的 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/) 使用兼容接口。连接官方 `api.deepseek.com` 时，程序按 [思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/) 显式关闭思考，避免简单查询消耗长推理；其他兼容服务不发送此扩展参数。成功查询后直接发送工具返回的真实电量，不再调用模型改写结果。

## 启动

```bash
uv run qqbot check --websocket
uv run qqbot serve
```

`check --websocket` 实际连接网关并检查鉴权和消息订阅，成功后立即断开，不调用模型或电费接口。检查前先停止同一 AppID 的其他机器人实例。

启动日志出现“QQ WebSocket 鉴权成功”后，可在群里 @机器人或私聊查询。程序自动发送心跳，断线后以 5～60 秒退避重连，优先恢复会话；消息先持久化再推进网关序号，避免补发导致重复查询。WebSocket 模式不监听 HTTP 端口，原来的 `QQ_HOST`、`QQ_PORT` 配置无需修改。

服务器需要能访问 QQ 和模型接口；如 QQ 平台要求 IP 白名单，填写服务器的公网出口 IP。未上线机器人需在平台配置测试群和测试成员。AppID 的 WebSocket 是否开放以实际鉴权结果为准；如收到权限或连接方式拒绝，日志会给出提示，不会自行切换 Webhook。Ubuntu root systemd 文件见 [deploy/qqbot-root.service](deploy/qqbot-root.service)。永久鉴权或权限错误退出码为 78，systemd 不会无限重试；修正配置后手动重启。

## 可选 Webhook 模式

仅在应用需要 Webhook 时设置 `QQ_TRANSPORT=webhook`，或运行 `uv run qqbot serve --transport webhook`。该模式监听 `127.0.0.1:8080`，QQ 回调为 `/qqbot`，健康检查为 `/healthz`；需要公网 HTTPS 转发和平台回调配置。两种模式共用同一电费查询和消息去重逻辑，只运行一个实例。

连接协议参考 QQ 官方的 [事件订阅与通知](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)、[WSS 接入点](https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html) 和 [网关错误码](https://bot.q.qq.com/wiki/develop/api-v2/openapi/error/error.html)。

## 清理 QQ 平台旧指令面板

QQ 平台的指令面板独立保存；移除本地功能不会自动删除平台上的指令入口。一次性维护脚本读取当前目录 `.env` 的 QQ 凭证，不连接模型或电费服务，也不需要停止机器人：

```bash
uv run --no-dev python scripts/clear_qq_panels.py --scope all
uv run --no-dev python scripts/clear_qq_panels.py --scope all --apply
```

第一条只查看，第二条备份后删除当前 AppID 四个场景的全部指令面板。只清理群聊可以使用 `--scope group`。删除间隔至少 6.1 秒，遇到错误即停止；面板完整详情保存在忽略的 `data/qq-panels-backup/`，不打印凭证或关联对象 ID。这个脚本处理 [指令面板](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/)，不修改单聊底部的自定义菜单；接口无记录而客户端仍有指令时，需进一步检查旧平台配置或客户端缓存。
