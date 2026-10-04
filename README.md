# QQ 宿舍电量查询机器人

Python 3.12+、uv。默认使用 WebSocket 主动连接 QQ，不需要公网域名、HTTPS 证书、反向代理或消息回调地址。机器人通过兼容 OpenAI Chat Completions、支持函数调用的模型理解自然语言，查询当前剩余电量，或提出默认宿舍的绑定/更换确认请求。

例如在 QQ 群里 @机器人，发送“查一下33号楼2035室还有多少电”；也可私聊，或发送 `/电费 33#2035`。未绑定默认宿舍时，需要提供完整楼号和房号；房间有多个区域候选时会提示确认。自然语言模型未配置时，不进行模型意图判断。

消息直接交给模型，由 `tool_choice=auto` 决定调用工具或回复文本。没有正则关键词意图拦截；“看看33楼4032宿舍电费”“我住33幢4032，帮忙瞅瞅电还剩多少”等说法都交给模型理解。程序只校验工具名、参数结构、宿舍编号格式、目录匹配和请求频率。模型决定是否追问或拒绝无关问题；每条消息独立处理，补充信息时请提供完整楼号和房号。

电量请求由程序校验宿舍映射、缓存 30 秒，并通过共享 SQLite 冷却门限限制校园请求频率；不查询历史、不估算耗电、不生成图片或网页。消息队列、默认宿舍和短期按钮操作保存在本地 SQLite。

## Markdown 和电费按钮

查询成功后显示 Markdown 结果，突出剩余电量。严格低于 50 度时，附带“电量已经不多了，记得及时充值电费，避免停电”的提醒；50 度及以上不提醒。结果下方有三个回调按钮，点击直接触发操作：

- 再次查询：直接调用同一宿舍的电费查询，不经过模型，仍遵守缓存和冷却。
- 绑定此宿舍：先展示“是/否”确认按钮，点“是”才保存，点“否”不修改。已绑定其他宿舍时提示是否更换。也可以说“更换绑定宿舍为33楼2004室”发起确认；按钮不显示时可回复“确认更换”或“取消更换”。绑定后可以直接说“查一下电费”。
- 使用帮助：说明支持的查询方式、绑定规则、缓存和低电量提醒，不查询电费。

普通查询和再次查询不修改默认宿舍。模型只能调用 `query_electricity` 或 `request_dorm_binding`；后者仅提出请求，没有任何直接保存绑定的模型工具。保存必须经当前用户的按钮确认或明确确认指令，并校验待确认请求和原绑定状态，避免过期确认覆盖新绑定。

每个按钮都使用独立 ID 和操作令牌，后端校验按钮 ID、动作、原群/会话、操作用户，并在执行任务前再次核对来源。默认宿舍按“AppID + 用户 OpenID”保存：同一用户在 A 群绑定，在 B 群直接说“查一下电费”也能查询。身份匹配使用完整 OpenID，日志指纹仅用于查看，不能作为身份键。群号、昵称不参与默认宿舍身份识别；两个不同的 OpenID 不会被猜测合并。私聊返回相同用户 OpenID 时也共用绑定；返回不同标识时保持分别保存。

共用默认宿舍不等于共用按钮路由：A 群卡片只在 A 群接受操作并回复，B 群不能确认 A 群的请求。确认有效期 10 分钟；同一用户的新请求使旧请求失效。点击“是”，或在发起确认的会话回复“确认绑定”“确认更换”才保存；单独的“是”“确认”“确定”不触发绑定。查询按钮令牌有效期 24 小时。同一互动 ID 重复投递不会重复执行，若同一互动 ID 携带不同群/用户/动作数据则拒绝并记录冲突。

升级到 0.15.0 后，旧版默认宿舍会迁移到用户共用记录。此前同一用户在不同群绑定不同宿舍时，以最近一次保存的绑定为准；已有新格式绑定不会被旧记录覆盖。原记录保留。旧版卡片和未校验来源的旧按钮任务不继续执行，请重新查询后使用新卡片。更新不删除 `.env` 或运行数据。

默认开启 `QQ_MARKDOWN_ENABLED=true`、`QQ_BUTTONS_ENABLED=true`，已有 `.env` 不填也会启用。实际权限需用当前 AppID 测试：QQ 拒绝按钮格式时尝试只发 Markdown；明确拒绝 Markdown 权限/格式时回退纯文本，不重新查询电费。WebSocket 若拒绝新增按钮事件订阅，会关闭按钮并重连基础消息。网络超时不会盲目切换消息格式重发。可手动设置上述开关为 `false`，恢复纯文本或禁用按钮。参考 [群消息 Markdown 与键盘](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、[按钮点击事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html) 和 [事件确认接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/interactions_interaction_id.put.html)。

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

## 操作日志与排查

默认 `LOG_LEVEL=INFO` 会把操作摘要输出到 systemd 日志，同时把详细审计写入 `QQ_DB_PATH` 指定的本地数据库（默认 `data/qqbot.sqlite3` 的 `bot_operations` 表）。审计定期清理超过 7 天或超过 50000 条的记录；队列原有任务记录仍只保留约 1 天。升级前没有记录的字段不能补回。

每条操作有追踪号，串联接收消息/回调、按钮来源及 ID 校验、模型选中的工具、查询缓存或冷却结果、绑定确认/保存、回复目标、格式降级与发送重试。回调记录收到的按钮 ID/data 摘要和对应本地按钮摘要，可判断上报数据与本地映射是否一致。`binding_requested` 仅提出确认，`binding_saved` 才表示绑定和审计在同一事务内保存成功。每次进程启动有独立实例标识，便于识别同时运行或重启的服务。

终端默认仅显示群/用户/按钮指纹，不输出密钥、回调令牌或完整消息正文。审计表保留本地平台 OpenID，可显式使用 `--show-ids` 查看；这是平台 ID，并非 QQ 号。

```bash
journalctl -u qqbot -f
cd /home/qqbot
.venv/bin/python scripts/inspect_electricity_events.py --operations --hours 24 --limit 200
.venv/bin/python scripts/inspect_electricity_events.py --operations --user 0f58acb570 --hours 24
```

还可使用 `--group 群指纹` 或 `--trace 追踪号` 筛选；不加 `--operations` 时只读查看原有任务列表。收到回调不能证明客户端屏幕上实际点击的位置；需要把收到的字段、匹配的卡片和实际执行结果一起核对。

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

脚本删除成功只代表对应 API 面板删除成功，不能证明客户端所有指令来源已清空。电脑和手机重新登录后仍出现同一组旧指令时，请核对机器人应用身份、管理端的“发布设置 → 功能配置 → 指令配置”和测试/已发布版本；腾讯的 [管理端接入说明](https://github.com/tencent-connect/bot-docs/blob/main/docs/README.md) 也描述了这些配置入口。不能仅凭空列表就认定是本机缓存。

当前管理端可能仅显示“通过 API 配置”，旧资料中的网页编辑入口未必可用。此时可执行 `uv run --no-dev python scripts/clear_qq_panels.py --scope all --diagnose`，只读核对机器人名称、AppID 后四位、各场景的面板和最近删除备份；不要假定新增 API 与旧配置的迁移规则。脚本支持 `--version`，用于确认服务器已上传修复版本。
