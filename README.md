# QQ 宿舍电量查询机器人

Python 3.12+、uv。机器人只做一件事：通过兼容 OpenAI Chat Completions、支持函数调用的模型理解自然语言，并调用唯一的 `query_electricity` 工具查询明确宿舍当前的剩余电量。

例如在 QQ 群里 @机器人，发送“查一下33号楼2035室还有多少电”；也可私聊，或发送 `/电费 33#2035`。楼号或房号不明确、房间存在多个候选时，机器人会先询问，不猜测、不调用电费接口。自然语言模型未配置时，机器人不会尝试猜意图或请求校园接口。

电量请求由程序校验宿舍映射、缓存 30 秒，并通过共享 SQLite 冷却门限限制校园请求频率；不查询历史、不估算耗电、不保存个人资料、不生成图片或网页。用于回调去重和可靠回复的 QQ 消息队列仍保存在本地 SQLite。

## 安装和配置

```bash
uv sync --locked --no-dev
cp .env.example .env
nano .env
```

填写 QQ 开放平台的 `QQ_APP_ID`、`QQ_APP_SECRET`，以及兼容 OpenAI 的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。再确认 `LLM_ENABLED=true`、`ELECTRICITY_ENABLED=true` 和学校编号。API Key、AppSecret、电费会话凭证只放在服务器 `.env`，不要提交到 GitHub。

`ELECTRICITY_SCHOOL_CODE=1402` 使用内置宿舍目录；其他学校请配置对应的学校编号及映射文件。

## 启动

```bash
uv run qqbot check
uv run qqbot serve
```

服务器默认监听 `127.0.0.1:8080`。QQ 消息回调为 `/qqbot`，健康检查为 `/healthz`。部署时用 Nginx 或 Caddy 配 HTTPS 并转发到本地端口，再在 QQ 开放平台填写回调 URL。Ubuntu root systemd 文件见 [deploy/qqbot-root.service](deploy/qqbot-root.service)。
