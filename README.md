# QQ 群聊与私聊机器人

Python 3.12+，使用 uv 管理依赖，通过 QQ 官方 Webhook 接收事件、OpenAPI v2 回复文本。
已实现帮助、ping、复读、北京时间和服务状态命令。群里 @ 机器人，私聊直接输入。

仓库：[Aemeath233/qqbot](https://github.com/Aemeath233/qqbot)。真实凭证只配置在本机或服务器 `.env`。

## 本地开发（Windows）

首次获取代码，在 PowerShell 中执行：

```powershell
git clone https://github.com/Aemeath233/qqbot.git
cd qqbot
uv sync --locked
uv run qqbot demo
```

`demo` 不需要 AppID、密钥或网络，可直接输入 `/ping`、`/复读 你好`、`/帮助`；输入 `exit` 退出。

开发真实机器人时，在 `.env` 填写开放平台的凭证。如果文件不存在，先复制模板：

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

```dotenv
QQ_APP_ID=你的机器人AppID
QQ_APP_SECRET=你的机器人AppSecret
```

填写 AppSecret，旧的 Token 不使用。`.env` 已加入 `.gitignore`；密钥保存在你自己的电脑和服务器。
进程环境变量优先于 `.env`。

```powershell
uv run qqbot check
uv run qqbot serve
```

`check` 只获取凭证和机器人资料，用于检查鉴权，不发送消息。
服务默认监听 `http://127.0.0.1:8080`，QQ 回调路径 `/qqbot`，健康检查路径 `/healthz`。
也可以用 `uv run qqbot` 启动服务。

没有凭证时也可运行本地 Webhook 模拟服务：

```powershell
uv run qqbot serve --dry-run
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

模拟服务仍会验签，缺省 AppID 为 `local-demo`、Secret 为 `local-demo-secret`，只在终端输出回复。
它只允许本机监听，并使用独立的 `*.dry-run.sqlite3` 数据库。命令体验用 `demo` 更方便。

## 在 QQ 开放平台配置

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，取得 AppID 和 AppSecret。
2. 配置允许测试的 QQ 用户与群，将机器人添加到测试场景。
3. 配置服务器的公网出口 IP 白名单，并开通需要的群聊、单聊及文本消息权限。
4. 在事件订阅中配置公网 HTTPS 回调地址，例如 `https://bot.example.com/qqbot`。
5. 勾选 `GROUP_AT_MESSAGE_CREATE` 和 `C2C_MESSAGE_CREATE` 事件，完成回调地址验证。
6. 群中发送 `@机器人 /ping`，私聊发送 `/ping`，收到 `pong` 即完成真实收发联调。

公网 HTTPS 地址需能访问你的服务。仅在本机执行 `uv run`，QQ 平台无法访问 `127.0.0.1`。
QQ 文档允许回调端口 80、443、8080、8443；下面部署示例使用 HTTPS 443。

事件验签使用 AppSecret 派生的 Ed25519 公钥，并严格验证原始请求体。
官方 `op=13` 地址验证示例不带签名头，所以仅该验证请求允许无签名；普通消息始终要求有效签名。
所有回调还会核对 `X-Bot-Appid`。

## 命令

| 命令 | 功能 |
| --- | --- |
| `/帮助`、`/help`、`/菜单` | 命令列表 |
| `/ping` | 回复 pong |
| `/复读 文本`、`/echo 文本` | 重复文本，最多 1000 字 |
| `/时间`、`/time` | 北京时间，UTC+8 |
| `/状态`、`/status` | 版本、运行时间、本次启动已发送回复数 |

私聊和群 @ 消息可以省略 `/`。图片、语音和卡片消息暂不处理；机器人发出的消息会被忽略。
普通文本如果不是已支持的命令，会提示查看帮助。

如果开放平台已开通群全量消息，可额外订阅 `GROUP_MESSAGE_CREATE`，并在 `.env` 设置：

```dotenv
QQ_ACCEPT_GROUP_MESSAGES=true
```

全量模式只响应 `/` 开头且已支持的命令，普通聊天不回复。

## 服务器部署示例（Linux + Caddy）

将公开仓库克隆到 `/opt/qqbot`，在服务器上用 uv 创建运行环境。
公开仓库无需 GitHub 登录凭证即可拉取。第一次克隆之前，该目标目录应不存在。

服务器安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后：

```bash
git clone https://github.com/Aemeath233/qqbot.git /opt/qqbot
cd /opt/qqbot
uv sync --locked --no-dev
cp -n .env.example .env
# 编辑 .env，填写 AppID、AppSecret，保持 QQ_HOST=127.0.0.1、QQ_PORT=8080
uv run --no-dev qqbot check
uv run --no-dev qqbot serve
```

`/opt` 通常需要管理员写入权限，可先由管理员创建目标目录的权限，再以部署用户克隆。
只在服务器复制和编辑 `.env`；仓库中的 `.env.example` 仅提供配置项。
第一次可以在前台运行服务，确认没有启动错误，再配置常驻服务。

安装 [Caddy](https://caddyserver.com/docs/install)，把域名 DNS 记录指向服务器，开放入站 80、443。
将 `deploy/Caddyfile.example` 中的 `bot.example.com` 改成你的域名，配置到 `/etc/caddy/Caddyfile`。
如果服务器已有其他站点，把这段站点配置合并到现有文件。

```caddyfile
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl https://你的域名/healthz
```

Caddy 根据域名申请 HTTPS 证书并转发请求。无需把 Python 服务的 8080 端口暴露到公网。
回调地址填写 `https://你的域名/qqbot`。Nginx 也可使用相同的后端地址，需保留请求体和验签请求头。

停止前台服务后，使用 `deploy/qqbot.service.example` 配置 systemd，默认使用普通用户 `qqbot`：

```bash
sudo useradd --system --home /opt/qqbot --shell /usr/sbin/nologin qqbot
sudo chown -R qqbot:qqbot /opt/qqbot
sudo chmod 600 /opt/qqbot/.env
sudo cp deploy/qqbot.service.example /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
sudo journalctl -u qqbot -f
```

用户已存在时跳过 `useradd`。若项目目录不同，调整模板里的工作目录和启动路径。
本项目每个数据库只运行一个机器人进程，适用于单服务器初始部署。

## 后续代码更新

本机修改并检查代码后提交、推送：

```powershell
uv run pytest -q
uv run ruff check .
git add .
git commit -m "Describe the change"
git push
```

服务器以项目目录拥有者的身份拉取，并同步依赖，再重启服务：

```bash
cd /opt/qqbot
git pull --ff-only
uv sync --locked --no-dev
sudo systemctl restart qqbot
curl --fail http://127.0.0.1:8080/healthz
```

如果使用上面的 `qqbot` 服务用户，需要以该用户执行 Git 和 uv，且 uv 必须位于该用户可执行的路径：

```bash
sudo -u qqbot -H git -C /opt/qqbot pull --ff-only
# 示例路径；按服务器实际 uv 安装位置调整。
sudo -u qqbot -H /usr/local/bin/uv sync --project /opt/qqbot --locked --no-dev
sudo systemctl restart qqbot
```

`.env` 和运行数据库未纳入 Git，正常拉取更新会保留服务器自己的配置和数据。
`--ff-only` 在服务器代码有分叉时停止更新，先处理本地修改，再重新拉取。

## Git 中的配置与数据

提交源码、测试、`uv.lock`、配置模板和部署模板。
`.gitignore` 排除了真实 `.env`、虚拟环境、运行数据、日志、数据库和证书私钥。
测试中的演示字符串和官方公开验签样例只用于本地验证，不是你的机器人凭证。

## 收发与恢复机制

- 访问凭证在服务端缓存，并在过期前按需刷新；遇到 HTTP 401 会刷新后重试一次。
- 回调验签后先写入 SQLite，再立即返回确认，后台发送回复。
- 一条原始消息只回复一次；去重记录至少保留 24 小时，接收新消息时清理旧记录，服务重启后仍有效。
- 服务重启会恢复未完成任务；网络错误、限流和服务端错误最多尝试 4 次。
- 权限、内容或消息过期等明确的业务错误不反复重试，错误码与 TraceID 会写入日志。
- 每次回复固定使用 `msg_id` 和 `msg_seq=1`，配合 QQ 平台避免超时重试造成重复回复。

SQLite 位于 `data/qqbot.sqlite3`，包含待发回复和目标 OpenID，应保存在服务器本地可写磁盘。
健康检查 `ok` 表示本地服务及后台处理正常；真实 QQ 接入是否成功以平台验证和消息联调为准。
当前实现的是收到消息后回复，未实现主动群发、定时提醒、AI 聊天或群管理。

## 开发与验证

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

测试使用本地 HTTP 服务模拟 QQ，无需真实凭证，也不会给真实 QQ 用户发送消息。
覆盖官方回调验证与公钥样例、原始字节验签、群聊/私聊回复、鉴权缓存、去重、重启恢复和错误处理。

新增命令改 `src/qqbot/commands.py` 的 `CommandRouter.reply()`。
`api.py` 负责 OpenAPI，`server.py` 负责回调，`inbox.py` 负责持久化任务与去重。

## 常见问题

| 现象 | 排查 |
| --- | --- |
| 缺少凭证，无法启动 | 在项目 `.env` 填写 AppID 和 AppSecret；演示用 `demo` |
| `check` 返回 `100016` | 检查 AppID/AppSecret 是否对应同一个机器人 |
| 平台回调验证失败 | 检查域名、证书、反向代理 `/qqbot`、AppID 和 Secret |
| `check` 成功但没有消息事件 | 检查事件订阅、测试成员/群、机器人是否已加入群、是否 @ 机器人 |
| 收到事件但回复失败 | 根据日志的错误码与 TraceID 检查出口 IP 白名单、文本消息权限、消息有效期 |
| 发送含网址的文本失败 | 检查平台消息 URL 配置与内容审核结果 |
| 8080 端口被占用 | 修改 QQ_PORT，同时修改反向代理后端端口 |

## 官方资料

- [接入指南](https://bot.q.qq.com/wiki/develop/api-v2/)
- [访问凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [Webhook 接入](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html)
- [签名算法](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)
- [群聊发送接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)
- [单聊发送接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)
- [官方 SDK 对旧 WebSocket 接入方式的提示](https://github.com/tencent-connect/botgo)
- [uv 项目管理](https://docs.astral.sh/uv/guides/projects/)
- [Caddy HTTPS 反向代理](https://caddyserver.com/docs/quick-starts/reverse-proxy)
