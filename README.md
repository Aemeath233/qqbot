# QQ 电费机器人

Python 3.12+、uv。机器人只做宿舍电量查询和用电分析，支持 QQ 群聊及私聊。自然语言问答可选用兼容 OpenAI Chat Completions、支持函数调用的模型。

查询成功后按 QQ 应用和当前发送者的 OpenID 将电量保存到本地 SQLite。不会批量查询宿舍，不会定时访问校园接口，也不会虚构历史。缓存命中的读数不会被当成新读数；充值、电表校正或样本不足时，会在分析中说明限制。

用电曲线由服务器直接绘制为 PNG，先发送分析文字，再作为 QQ 图片消息发送。无需公网网页、图床、浏览器或单独子域名。曲线仅使用当前用户自己查过的记录。

## 安装与配置

```bash
uv sync --locked --no-dev
cp .env.example .env
nano .env
```

填写 QQ 开放平台的 `QQ_APP_ID`、`QQ_APP_SECRET`；将 `ELECTRICITY_ENABLED` 设为 `true`。学校编号不是 `1402` 时，也要配置相应的学校编号和宿舍目录。`.env` 存放密钥，不要上传到 GitHub。

可选配置兼容 OpenAI Chat Completions 的 `LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_API_KEY`，并将 `LLM_ENABLED` 设为 `true`。这让用户可以用自然语言查电量、问历史和估算耗电。未配置模型时，使用下方斜线命令。

## 运行

```bash
uv run qqbot check
uv run qqbot serve
```

默认只监听 `127.0.0.1:8080`。QQ 回调路径为 `/qqbot`，健康检查为 `/healthz`。线上通过你自己的 HTTPS 反向代理把域名转发到 `127.0.0.1:8080`，并在 QQ 开放平台配置同一回调地址。Ubuntu root systemd 模板见 [`deploy/qqbot-root.service`](deploy/qqbot-root.service)。

## 可用命令

- `/电费 33#2035 [区域]`：查当前电量。必须明确楼号和房号；有多个区域时先确认区域。
- `/绑定宿舍 33#2035 [区域]`：以后查询可省略房间；绑定本身不会请求校园接口。
- `/用电统计 [天数]`：按最近一次查询读数分析余额变化。
- `/电费历史 [天数]`：查看自己的实际读数及时间。
- `/用电曲线 [天数]`：将自己的历史绘成图片发回 QQ。
- `/我的宿舍`、`/解绑宿舍`、`/忘记我`、`/帮助`。

群聊中 @机器人 后使用命令；私聊可直接发命令。可在 `.env` 中开启 `QQ_ACCEPT_GROUP_MESSAGES=true` 来接收群内未 @ 的消息。

历史从用户第一次查询后开始积累。自动采样和主动推送默认不启用，因此只有实际记录覆盖的区间能用于估算耗电。余额净减少只有在期间没有充值或电表修正时，才可作为耗电估算。

## 服务端更新

```bash
cd /home/qqbot
git pull --ff-only
uv sync --locked --no-dev
sudo systemctl restart qqbot
```
