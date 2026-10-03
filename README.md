# QQ 群聊与私聊机器人

Python 3.12+，使用 uv 管理依赖，通过 QQ 官方 Webhook 接收事件、OpenAPI v2 回复文本。
已实现基础命令、完美校园电量查询，以及兼容 OpenAI 的 LLM 聊天和函数工具调用。
提供独立的管理网页，可登录后配置凭据、查看本地服务状态并手动测试连接。
管理页采用轻量工作台布局，分为概览、网页聊天、连接配置、人格、电费、功能开关和技能。
支持可配置人格、主动登记昵称/宿舍、个人电费台账与耗电估算，以及掷骰子和抽签。
群里 @ 机器人，私聊直接输入；LLM 与电费功能由管理员配置后启用。

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

## 管理网页

在项目目录启动，无须先填写 QQ 或 LLM 凭据：

```powershell
uv run qqbot admin
```

首次启动会在终端要求设置管理密码，输入不显示；然后打开
[本机管理页](http://127.0.0.1:8081)。密码哈希保存在 `data/admin/password.json`，
不保存明文管理密码，不纳入 Git。需要重设密码时运行：

```powershell
uv run qqbot admin --set-password
# 默认8081被占用时，可改管理端口：
uv run qqbot admin --port 8082
```

管理页支持：

- QQ AppID、AppSecret；模型启用开关、API地址、Key、模型名、超时。
- 电费启用开关、学校及项目、查询地址、默认区域、宿舍目录路径、可选会话信息。
- 查看本地机器人服务是否健康及配置状态；手动测试 QQ 鉴权、模型文本返回和电费区域接口。
- 网页聊天直接调用同一套机器人命令、人格和函数工具，不向 QQ 发送消息。
- 概览显示资料、宿舍绑定和查询台账的数量，不枚举用户昵称、OpenID或房间明细。
- 功能开关可关闭骰子/抽签和主动记忆；关闭小游戏也会从模型工具列表移除。
- 上传通用 `SKILL.md` 或单技能 ZIP，查看说明并启停；技能状态在下一条请求时生效。

常用参数直接显示，超时、按群覆盖、学校项目、查询地址和会话字段放在折叠的高级设置里。
界面参考 [AstrBot 的工作台管理方式](https://docs.astrbot.app/use/webui.html)，
当前聚焦一个 QQ 机器人、一个兼容模型服务和内置功能模块，没有第三方插件市场或多代理编排。
AstrBot 插件仍需适配后才能接入本项目，不能原样导入。

网页聊天可先试 `/人设`、`/掷骰子 2d6`，或配置模型后输入普通文字。
输入框支持 Ctrl+Enter 发送；同一时间只处理一条网页消息，最多2000字。
网页测试具有独立于真实QQ用户的身份和上下文；「重置对话」只清除网页上下文，保留其已登记资料。
「我的记忆」和历史请求仍只读取该网页身份自己的记录。
网页电费请求与诊断脚本共用60秒冷却，缓存命中不产生校园请求，HTTP429后按返回时间延长等待。

页面不回显已保存的密钥。密钥框留空保留原值，需要清除时明确勾选「清除已有值」。
由进程环境变量提供的字段在页面中只读，仍需在部署环境修改。
点击保存会原子更新项目 `.env`，保留其他字段与注释；并发或外部修改导致版本冲突时会停止覆盖。
**保存后需要重启机器人**：本地重新运行 `uv run qqbot serve`，
服务器执行 `sudo systemctl restart qqbot`。管理服务可以继续运行。
网页测试在下一条消息时使用已保存的新配置；配置变化会重新建立其模型对话上下文。

连接测试使用已保存的配置，只在点击按钮时调用外部服务。
QQ测试不发送消息，成功后仍需验证官方回调和实际聊天；模型测试产生一次调用，
只验证文本响应，不表示函数工具能力已完成真实联调。
电费测试只读取一次区域列表，不扫描宿舍，与诊断脚本共用60秒冷却；
HTTP429后至少等待10分钟，并尊重更长的Retry-After。
页面每30秒检查一次**本机**健康接口，不会轮询校园电费或模型服务。

管理服务使用密码登录、HttpOnly会话Cookie、来源与CSRF校验、登录尝试限制和主机名白名单。
会话8小时后失效，服务重启或重设密码后需重新登录。
它始终监听127.0.0.1，与机器人回调的8080端口独立。
在服务器上可直接通过SSH隧道访问，或按下面部署段落配置专用HTTPS管理域名。

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
| `/电费 33#2035`、`/electricity 33#2035` | 直接查电量，无须 LLM |
| `/电费 19#312 1` | 指定区域/主菜单，消除重名楼的歧义 |
| `/聊天 内容`、`/chat 内容` | 与已配置的 LLM 聊天 |
| `/人设` | 查看当前人格；群内也显示管理页配置所需的群标识 |
| `/昵称 阿明`、`以后叫我阿明` | 明确登记自己在当前聊天范围的昵称 |
| `/绑定宿舍 33#2035 [区域]` | 用本地目录确认并绑定宿舍，不请求校园接口 |
| `/电费` | 已绑定时直接查询自己的宿舍 |
| `/我的记忆`、`/解绑宿舍` | 查看个人资料或解除宿舍绑定 |
| `/忘记我` | 清除当前聊天范围的资料、台账和对话上下文 |
| `/电费历史 3 [楼号#房号] [区域]` | 查看自己最近3天的查询记录，最多展示10条 |
| `/用电统计 3 [楼号#房号] [区域]` | 按真实读数计算余额净变化及条件性耗电估算 |
| `/清除电费历史` | 只清除自己的台账，保留昵称和宿舍绑定 |
| `/掷骰子`、`/掷骰子 2d6` | 程序生成骰子点数，最多20枚、每枚2～1000面 |
| `/抽签`、`/抽签 面条\|米饭\|饺子` | 随机娱乐签或从用户选项中选择 |
| `/技能列表`、`/skills` | 查看已启用的文档技能，不需要模型 |
| `/技能 meal-choice 从米饭和面条中选一个` | 明确选择技能完成任务，需要模型 |

私聊和群 @ 消息可以省略 `/`。图片、语音和卡片消息暂不处理；机器人发出的消息会被忽略。
普通文本如果不是已支持的命令，会提示查看帮助。

如果开放平台已开通群全量消息，可额外订阅 `GROUP_MESSAGE_CREATE`，并在 `.env` 设置：

```dotenv
QQ_ACCEPT_GROUP_MESSAGES=true
```

全量模式只响应 `/` 开头且已支持的命令，普通聊天不回复。

## 通用 Agent Skills

Skill 是给模型的任务说明和参考资料，函数是机器人真正能执行的动作。
本项目按 [Agent Skills 开放规范](https://agentskills.io/specification)读取技能目录，
[OpenAI Skills](https://developers.openai.com/api/docs/guides/tools-skills)和
[Claude Code Skills](https://code.claude.com/docs/en/skills)采用相同的核心目录与Markdown结构；
平台扩展字段、可调用工具和脚本执行环境仍有差异，不能保证每个包都能完整运行。

在管理页的「技能」选择 `.md` 或 `.zip` 上传，查看说明，再点击「启用」。
上传后默认停用。启停即时影响下一条请求，无须重启；全局 `SKILLS_ENABLED` 开关属于运行配置，
修改后QQ服务仍需重启。模型必须支持函数调用，才能根据普通聊天自动加载技能。

```text
meal-choice/
  SKILL.md
  references/ideas.md
  scripts/             # 可保留，但不会执行
  assets/              # 可保留二进制资源，但不会解析
  agents/openai.yaml   # 可保留平台配置，但不会应用权限
```

`SKILL.md` 推荐带YAML头部：

```markdown
---
name: meal-choice
description: 用户纠结吃什么、想随机选饭时，使用已有抽签工具帮助选择。
---

先确认用户的候选，再调用 draw_lots。options 是用 | 分隔的字符串。
结果以工具返回为准，不编造随机结果。
```

支持大小写不同的 `SKILL.md`。Claude式省略元数据时，可从目录或Markdown文件名生成名称，
从正文第一行生成描述；单独上传没有元数据的 `SKILL.md` 无法推断名称，需要补上头部。
名称最多64字，使用小写字母/数字或汉字及单个连字符；不接受Windows保留名称。
正文最多12000字，描述最多1024字，头部最多4096字。

ZIP可以包含一个外层技能目录，也可以直接以 `SKILL.md` 为根。
每包只能有一个技能说明，所有文件都应在它所在的目录内。
上传最多1 MiB，解压最多2 MiB、80个条目，单个资源最多256 KiB，说明文件最多64 KiB。
拒绝越界路径、隐藏路径、链接、加密文件、大小写重复文件和同名覆盖。
最多保存32个技能，同时启用16个。当前版本没有同名更新和删除界面；维护已上传包时，
先在管理页停用，再在服务器上维护 `data/skills/` 和该目录的 `.registry.json`，保留备份。
上传包保存在 `SKILLS_DIR`（默认 `data/skills`），不会提交Git；部署更新代码时请保留整个 `data/`。

模型先看到启用技能的名称与描述，通过 `load_skill` 读取正文，
再用 `read_skill_file` 分段读取该技能内的UTF-8资料。技能内容属于参考数据，不是系统指令。
`disable-model-invocation: true` 限制为手动调用；`user-invocable: false` 禁止手动调用。
`allowed-tools`、`context`、`model`、`agent` 等平台字段保留在文件里，不增加权限，也不启用子代理。
包内脚本不执行、依赖不安装、任意Python模块不导入；需要Shell、浏览器或新接口的技能应先适配工具。
现有电费、台账和抽签工具仍遵循原有开关、用户隔离及真实结果展示规则。
正文和读取到的引用会发送给管理员配置的模型，请不要在技能里放密钥或私人资料。

仓库提供 `examples/skills/meal-choice/` 示例。可以上传其中的 `SKILL.md`，
也可以把整个目录压成ZIP，以便保留参考资料。

```text
/技能列表
/技能 meal-choice 从米饭和面条中选一个
今晚吃米饭还是面条？帮我抽一个。
```

最后一句由模型判断是否使用技能；电量数字和抽签结果仍由程序展示。
这属于文档型技能支持，不能直接导入AstrBot可执行插件。

## 人格与明确登记的记忆

在管理页的「人格与个人记忆」中设置名字、预设、自定义说明、口头禅和回复长度。
默认是猫猫电费管家，也可选择校园损友、温柔助手或自定义人格。
口头禅用于模型聊天；电量与统计始终由程序按工具结果展示，预设可以添加固定的风格句。
自定义描述可以分段，最多2000字；不会给模型增加执行代码、修改配置或访问他人数据的权限。

```dotenv
BOT_NAME=小电
BOT_PERSONA=cat
BOT_PERSONA_CUSTOM=
BOT_CATCHPHRASE=喵
BOT_REPLY_LENGTH=balanced
BOT_GROUP_PERSONAS={}
MEMORY_ENABLED=true
GAMES_ENABLED=true
```

`BOT_REPLY_LENGTH` 可选 `short`、`balanced`、`detailed`，作为表达长度偏好。
需要不同群使用不同人格时，在对应群发 `/人设`，把返回的群标识填入管理页的覆盖JSON，
例如 `{"该群标识":"friend"}`。群消息不能修改全局或他群人设，只能由管理页配置。
修改配置后重启机器人；自定义人格被全局或群覆盖选中时必须填写描述。

昵称和宿舍只在用户明确要求时登记，例如“以后叫我阿明”“记住我的宿舍是33号楼2035”。
绑定前检查真实本地目录及区域，歧义时不保存。普通聊天、临时查询不会自动更改绑定信息。
绑定后 `/电费` 无须重复房号，自然语言模型也可以使用已确认的绑定。
昵称和绑定资料会作为用户数据提供给配置的模型，不作为系统指令。
关闭 `MEMORY_ENABLED` 后停止使用和登记这些资料；仍可用 `/忘记我` 清除已有本地记录。

身份来自已验签的 QQ 事件：私聊用 `user_openid`，群聊用 `member_openid`（缺少时可用事件的 `author.id`）。
本机内部用户ID由机器人AppID和当前场景中的用户标识派生，不把手输QQ号当作身份验证。
昵称、宿舍、上下文和台账按私聊用户或“群＋群成员”隔离；不会擅自合并群聊与私聊身份。
缺少可靠作者标识的群消息仅有本条消息的临时上下文，不能登记长期记忆或台账。
官方事件中的 `union_openid` 可能为空，当前版本不依赖它做跨场景合并。
参见 [私聊事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)
和 [群聊事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html)。

资料和台账放在主数据库旁的 `*.userdata.sqlite3`，默认 `data/qqbot.userdata.sqlite3`，未纳入Git。
`/忘记我` 清除本机当前范围的长期资料、查询台账和内存对话，不撤回已经发送的QQ消息。
本地控制台 `uv run qqbot chat` 使用独立的 `local-console` 身份，可测试人格、记忆和小游戏；
基础互动与登记不要求LLM，普通AI聊天仍需要模型配置。QQ发送重试复用已经生成的骰子或抽签结果。

## 个人电费台账与用电估算

默认 `ELECTRICITY_HISTORY_ENABLED=true`。每次机器人成功查询电量后记录当前用户、
电表标识、宿舍/区域、剩余电量、原读数时间、本次请求时间和缓存标记，失败不记为零。
同一原始消息重放更新同一条记录，QQ发送重试不会重新查询或重复登记。
缓存访问仍记在查询日志中，但相同读数不会增加统计的独立采样数。
这些写入不产生额外校园请求，历史与统计仅查本地数据库。

```text
/电费历史 3
/用电统计 3
我最近三天用了多少电？
/用电统计 7 33#2035 2
```

天数为1～365，“最近3天”按最近72小时筛选已存读数。
目标选择顺序为明确指定宿舍、当前绑定宿舍、自己历史中的唯一电表；存在多个目标时要求确认。
不会混合其他用户、不同宿舍、区域或缴费项目的数据，也不会补造部署前的历史。
至少两次独立读数才能估算，回复明确列出真实覆盖时间、首末电量及余额净变化。

**余额净减少不是无条件的准确耗电量。** 没有充值和余额修正时，可作为覆盖时段的耗电估算。
读数上升时可能有充值或电表修正，程序不输出准确耗电量；
即使余额持续下降，抵消消耗的未记录充值仍无法从查询点中识别，回复会说明这个前提。
数据只有一小时就只报实际覆盖一小时，不能声称完整三天。金额不换算为电量或充值量。

历史默认保留365天，可在管理页调整 `ELECTRICITY_HISTORY_RETENTION_DAYS`（1～3650）。
成功写入时清理过期日志，每个用户最多保留20000条；查询接口每次最多读取这些记录。
关闭历史开关不会继续登记或统计，已有本地台账可用 `/清除电费历史` 或 `/忘记我` 清除。
之前的独立诊断脚本没有QQ用户身份，不会把其报告自动归给某个用户。

## LLM 与自然语言调用工具

电量查询在代码中注册为 `query_electricity` 函数工具。模型识别用户意图、提供楼号和房号，
后台 Python 执行查询，再将工具结果交给模型完成对话。电量、区域和宿舍号由程序按真实结果展示。
模型输出的其他电量数字不会替代工具返回值；查询失败也不会显示为 0 度。

在服务器 `.env` 配置支持 **Chat Completions + tools/function calling** 的模型服务：

```dotenv
LLM_ENABLED=true
LLM_BASE_URL=https://你的模型服务地址/v1
LLM_API_KEY=你自己的密钥
LLM_MODEL=支持函数调用的模型名
LLM_TIMEOUT=30
ELECTRICITY_ENABLED=true
```

`LLM_BASE_URL` 填到 `/v1` 这一层，程序会拼接 `/chat/completions`；按服务商实际 API 基址填写。
“兼容 OpenAI”不保证所有模型都支持函数调用，需选服务商明确支持 tools 的型号。
本实现使用 Chat Completions，不适用于只在 Responses API 提供函数调用的模型。

用户可以发送“帮我查33号楼2035宿舍还剩多少电”，或者先说“查电费”，再回答机器人询问的楼号和房号。
对话上下文保留最近 4 轮，30 分钟无交互后失效；群内按发送者和群隔离，私聊独立，服务重启后清空。
群全量模式下用 `/聊天 帮我查电费`，日常聊天不会自动触发模型。
聊天文字与本次工具结果会发给你配置的模型服务；QQ 凭证、电费 Cookie/Token 不会发给模型。

不需要 QQ 凭证即可单独测试模型（会调用真实模型服务）：

```bash
uv run qqbot chat
uv run qqbot chat "帮我查33号楼2035还有多少电"
```

每次对话最多 5 轮模型请求、4 次函数执行，后台任务总时限 90 秒。
普通帮助、ping 等命令不消耗模型调用。`serve --dry-run` 禁用外部 LLM 和电费请求，只测试本地 QQ 回调。

新增函数在 `src/qqbot/tools.py` 注册 `FunctionTool`，提供参数结构和异步 Python 函数。
模型只能调用注册的函数，不能自行执行 shell、修改接口地址或传入认证请求头。
当前还注册 `electricity_history`、`electricity_usage`、`roll_dice` 和 `draw_lots`。
查询统计所用的当前用户身份由服务器绑定，不让模型传入用户ID；结果由代码展示，
模型的虚构数字不能覆盖实际计算或随机结果。长期资料写入采用明确登记指令，而非任意模型工具调用。
函数调用格式依据 [OpenAI Docs](https://developers.openai.com/api/docs/guides/function-calling)。

## 电费接口与宿舍号

电费客户端查询的是剩余电量，单位为度。内置旧映射对应 `schoolcode=1402`、`payproid=953`，
从提供的资料导入并去重为 4097 条宿舍记录；这些记录和接口的当前有效性需要实际核对。
2026-10-02 用户实测默认 HTTPS 443 接口：区域查询及单间宿舍电量查询均成功，
当次请求未设置 Token、Cookie 或 TappID；这份旧目录的其他房间仍需按实际情况核对。

```dotenv
ELECTRICITY_ENABLED=true
ELECTRICITY_SCHOOL_CODE=1402
ELECTRICITY_PAY_PROJECT=953
ELECTRICITY_ENDPOINT=https://cloudpaygateway.59wanmei.com/paygateway/smallpaygateway/trade
ELECTRICITY_TOKEN=
ELECTRICITY_COOKIE=
ELECTRICITY_TAPP_ID=
ELECTRICITY_DEFAULT_AREA=
```

有需要时填写当前有效会话的 Token、Cookie 和应用标识，原压缩包的会话数据不会自动复制。
接口地址可以用 `ELECTRICITY_ENDPOINT` 改为当前有效的 HTTPS 查询地址。
更新代码后，如果本机或服务器旧 `.env` 仍将该地址配置为 `:8087`，
请同步改为上述不含端口的443地址；`.env` 不会随着 `git pull` 自动更新。
其他学校必须配置自己的学校代码与宿舍目录，不能继续使用这份内置映射。

**房号通过目录匹配，不按数字位数或后缀猜测。**
目录每条记录保存区域、楼号、实际房号、别名和不可拆解的 `roomverify`。
例如系统显示 `19312`、学生使用 `312` 时，可把房号设为 `312`，别名设为 `19312`，
但对应 roomverify 必须来自已核对的真实房间记录。当前旧表没有 `19#19312`，不会自动为它编造编号。
`4032` 与 `432` 等写法也通过明确配置关联，不能把所有 `312` 都匹配为以 `312` 结尾的记录。

导出完整 JSON 目录，保留已有记录后按实际情况补别名：

```bash
uv run qqbot export-dorms data/room_catalog.json
```

导出已有文件时会停止，避免覆盖人工维护的目录。在其中某条已确认记录补充 `aliases`，例如：

```json
{
  "area": "2",
  "area_name": "7—10、30—33号楼",
  "building": "33",
  "room": "4032",
  "aliases": ["432"],
  "roomverify": "2-11--4-4032"
}
```

```dotenv
ELECTRICITY_MAP_PATH=data/room_catalog.json
```

`deploy/room_catalog.example.json` 是结构示例，仅包含这一条记录；需要查询其他宿舍时使用完整目录。
如果同一区域的两条房间记录共享一个别名，或者不同区域都存在同名楼和房号，程序返回候选区域并询问，
不会取第一个结果。只有“312”而没有楼号时，机器人会询问楼号。
可设置 `ELECTRICITY_DEFAULT_AREA` 固定本机器人的区域，也可让自然语言工具调用提供用户确认的 `area`。

单独验证上游接口，无须 QQ 或模型凭证：

```bash
uv run qqbot electricity 33#2035
uv run qqbot electricity "19#312" --area "1"
```

客户端检查 HTTP 和业务状态，设置 10 秒超时，缓存有效结果 30 秒。
实时测试中若出现 TLS 握手/网络失败，说明还没有取得业务响应，应在服务器网络环境复测，
再核对当前有效请求的地址和会话；不能将这种失败判断为请求参数错误或电量为零。

## 低频接口测试脚本

在项目目录运行 `scripts/test_electricity.py`。它不需要 QQ/LLM 凭证，也不要求开启
`ELECTRICITY_ENABLED`；仅从本地 `.env` 读取电费学校、项目、目录和会话配置。
默认使用参考插件的 443 端口，`--port 8087` 可以单独测试旧地址；脚本不使用
`ELECTRICITY_ENDPOINT`，不会自动尝试多个地址。

先检查配置，这一步完全不联网，也不占用冷却时间：

```powershell
uv run python scripts/test_electricity.py --dry-run
```

需要测试时，每次手动执行一次：

```powershell
uv run python scripts/test_electricity.py
```

默认只请求一次区域列表，不读取楼栋、楼层或完整房间目录，不查询电量。
没有自动重试或重定向；超时12秒，两次请求开始时间至少间隔60秒。
冷却记录保存在 `data/electricity-test/cooldown.sqlite3`，同一项目目录下重新运行、
切换端口或代理模式都不会跳过冷却。HTTP 429 后至少冷却10分钟；
服务返回更长的 `Retry-After` 时按它等待。
这些限制用于减少测试流量，不能保证接口一定不会触发访问限制。

排查 TUN 时，先在当前网络状态执行一次，等待至少60秒，手动关闭 TUN 后再执行相同命令。
`--proxy direct` 是默认值：忽略 Python 能识别的显式代理设置；它**不能绕过 TUN**。
`--proxy env` 使用 aiohttp/Python 能识别的环境或系统 HTTP(S) 代理设置：

```powershell
uv run python scripts/test_electricity.py --proxy env
# 仅在需要时，等冷却结束后单独测试旧端口：
uv run python scripts/test_electricity.py --port 8087
```

无需把这些命令全部运行一遍；先对比 TUN 开/关的同一个请求，成功后再考虑其他测试。
如果需要验证一间已核对的宿舍，使用：

```powershell
uv run python scripts/test_electricity.py --dormitory "33#2035"
# 区域有歧义时可补 --area "2"，房号必须存在于当前配置的目录中。
```

指定宿舍时也只发一次电量请求，楼号、房号和 roomverify 先在本地目录确认。
脚本与机器人共用业务成功状态、单位及电量数值校验，不会把金额或异常值报成电量。

结果区分 DNS、TLS、证书、超时、HTTP、业务错误及查询成功。
每次实际测试保存一个带时间的 `data/electricity-test/report-*.json`，
不保存 Cookie、Token、代理密码、完整请求头、原始响应或异常文本；可以提供这份报告排查。
日志和冷却文件均已被 Git 忽略。退出码：0成功，1请求/业务失败，2配置或冷却阻止，130手动中止。
整个测试过程保持证书验证，不向 QQ、LLM 或微信发送请求。

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

### 在服务器运行管理页

先在项目目录、以服务用户设置个人管理密码。以下沿用前面的 `/opt/qqbot` 和 `qqbot` 用户：

```bash
cd /opt/qqbot
sudo -u qqbot -H /opt/qqbot/.venv/bin/qqbot admin --set-password
sudo cp deploy/qqbot-admin.service.example /etc/systemd/system/qqbot-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot-admin
```

管理端口只监听服务器回环地址。可在自己电脑建立隧道，把用户名和地址换成你的服务器：

```bash
ssh -L 8081:127.0.0.1:8081 你的SSH用户@你的服务器
```

保持SSH窗口连接，再在本机浏览器打开 `http://127.0.0.1:8081`。
若需要使用公网管理域名，在服务器 `.env` 加入该域名：

```dotenv
ADMIN_ALLOWED_HOSTS=localhost,127.0.0.1,::1,admin.example.com
```

将 `deploy/admin.Caddyfile.example` 的管理域名替换为自己的域名，并合并到已有Caddy配置。
管理页面使用单独域名，原机器人回调配置保留。域名指向服务器后：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl restart qqbot-admin
```

公网域名登录要求HTTPS，并自动使用Secure会话Cookie；无需把8081直接开放到公网。
代码更新涉及管理服务时，同步依赖后另行执行 `sudo systemctl restart qqbot-admin`。
管理页只维护配置与连接检查，不执行服务器shell或自行重启服务。

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
当前实现的是收到消息后回复，未实现主动群发、定时提醒或群管理。

## 开发与验证

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

测试使用本地 HTTP 服务模拟 QQ，无需真实凭证，也不会给真实 QQ 用户发送消息。
覆盖官方回调验证与公钥样例、原始字节验签、群聊/私聊回复、鉴权缓存、去重、重启恢复和错误处理。

新增命令改 `src/qqbot/commands.py` 的 `CommandRouter.reply()`。
联网任务通过 `CommandRouter.plan()` 入队；`assistant.py` 负责 LLM 工具循环，`tools.py` 注册函数，
`electricity.py` 负责查询，`dorms.py` 负责目录和别名。
`admin.py`、`admin_config.py` 和 `admin_auth.py` 分别负责管理服务、配置保存和登录，
页面静态资源位于 `src/qqbot/resources/admin/`；不依赖前端构建工具或CDN。
`api.py` 负责 QQ OpenAPI，`server.py` 负责回调，`inbox.py` 负责持久化任务与去重。
旧数据库首次启动时自动添加任务字段，保留旧的待发回复。
LLM/查询生成的回复在 QQ 发送前写入数据库；QQ 发送重试不会再次调用模型或重复查询电量。

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
