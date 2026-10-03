"use strict";

const groups = [
  { id: "personality", page: "personality", title: "人格", wide: true,
    description: "选择说话风格。昵称与宿舍只在用户明确登记后记住；查询台账按用户隔离，统计直接读取本地数据。", fields: [
    ["BOT_NAME", "机器人在对话中的名字", "text", "小电"],
    ["BOT_PERSONA", "默认人格", "select", "", {cat:"猫猫电费管家", friend:"校园损友", gentle:"温柔助手", custom:"自定义人格"}],
    ["BOT_REPLY_LENGTH", "回复长度偏好", "select", "", {short:"简短", balanced:"适中", detailed:"详细"}],
    ["BOT_CATCHPHRASE", "口头禅（可选）", "text", "偶尔使用，不必每句都说"],
    ["BOT_PERSONA_CUSTOM", "自定义人设说明", "textarea", "描述角色背景、语气和说话习惯，最多2000字"],
    ["BOT_GROUP_PERSONAS", "按群覆盖人格（可选）", "textarea", '{"群标识":"friend"}；在该群发送 /人设 获取标识', null, true],
  ]},
  { id: "qq", page: "connect", title: "QQ 机器人", description: "填写 QQ 开放平台的 AppID 与 AppSecret。", test: "测试 QQ 鉴权", fields: [
    ["QQ_APP_ID", "AppID", "text", "机器人应用 ID"],
    ["QQ_APP_SECRET", "AppSecret", "secret", "留空保留已配置的密钥"],
  ]},
  { id: "llm", page: "connect", title: "AI 模型服务", description: "一个兼容服务与一个默认模型；连接测试会产生一次模型调用。", test: "测试模型连接", fields: [
    ["LLM_ENABLED", "启用 AI 聊天", "bool"],
    ["LLM_BASE_URL", "API 地址", "text", "https://你的服务地址/v1"],
    ["LLM_MODEL", "模型名称", "text", "服务商提供的模型名"],
    ["LLM_API_KEY", "API Key", "secret", "留空保留已配置的密钥"],
    ["LLM_TIMEOUT", "请求超时（秒）", "number", "30", null, true],
  ]},
  { id: "electricity", page: "electricity", title: "宿舍电费", description: "查询剩余电量。连接测试只请求一次区域列表，不遍历宿舍；两次测试至少间隔 60 秒。", test: "测试电费接口", wide: true, fields: [
    ["ELECTRICITY_ENABLED", "启用电费查询", "bool"],
    ["ELECTRICITY_DEFAULT_AREA", "默认区域", "text", "留空时按查询结果确认区域"],
    ["ELECTRICITY_HISTORY_ENABLED", "保存个人电费查询历史", "bool"],
    ["ELECTRICITY_HISTORY_RETENTION_DAYS", "历史保留天数", "number", "365"],
    ["ELECTRICITY_ENDPOINT", "接口地址", "text", "HTTPS 查询地址", null, true],
    ["ELECTRICITY_MAP_PATH", "宿舍目录路径", "text", "留空使用内置目录", null, true],
    ["ELECTRICITY_SCHOOL_CODE", "学校代码", "text", "1402", null, true],
    ["ELECTRICITY_PAY_PROJECT", "缴费项目编号", "number", "953", null, true],
    ["ELECTRICITY_TOKEN", "会话 Token（可选）", "secret", "当前查询不要求时可留空", null, true],
    ["ELECTRICITY_COOKIE", "Cookie（可选）", "secret", "当前查询不要求时可留空", null, true],
    ["ELECTRICITY_TAPP_ID", "应用标识（可选）", "secret", "当前查询不要求时可留空", null, true],
  ]},
  { id: "tools", page: "tools", title: "按需开启", wide: true,
    description: "内置功能以函数模块维护。需要什么就开启什么，日常聊天只使用当前启用的工具。", fields: [
    ["MEMORY_ENABLED", "允许主动登记昵称和宿舍", "bool"],
    ["GAMES_ENABLED", "启用骰子与抽签", "bool"],
    ["SKILLS_ENABLED", "启用文档技能", "bool"],
  ]},
];
const controls = new Map();
const sections = new Map();
const pageInfo = {
  overview: ["概览", "连接状态与本地数据一目了然。"],
  chat: ["网页聊天", "先用网页验证人格和工具，再到 QQ 里完成真实收发联调。"],
  connect: ["连接配置", "保存后手动测试鉴权；密钥框留空保留已有值。"],
  personality: ["人格", "选一个口吻，或写下你自己的角色设定。"],
  electricity: ["电费", "当前电量、个人台账与用电估算集中配置。"],
  tools: ["功能开关", "保留日常用得上的功能，按需关闭小互动。"],
  skills: ["技能", "按通用 Agent Skills 结构导入与管理，使用现有工具完成任务。"],
};
let currentPage = "overview";
let csrf = "", revision = "", timer = null;
const el = id => document.getElementById(id);

function message(id, text, error = false) {
  el(id).textContent = text;
  el(id).classList.toggle("error", error);
}

async function api(path, payload) {
  const options = { credentials: "same-origin", headers: {} };
  if (payload !== undefined) {
    options.method = "POST";
    if (csrf) options.headers["X-CSRF-Token"] = csrf;
    if (payload instanceof FormData) options.body = payload;
    else { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(data.message || "请求失败，请稍后重试。");
  }
  return data;
}

function showLogin() {
  csrf = "";
  clearInterval(timer);
  el("login-panel").hidden = false;
  el("dashboard").hidden = true;
  el("logout").hidden = true;
  document.body.classList.remove("dashboard-mode");
  for (const control of controls.values()) {
    if (control.type === "secret") control.input.value = "";
  }
}

function buildForm() {
  for (const group of groups) {
    const section = document.createElement("section");
    section.className = "card config-section" + (group.wide ? " wide" : "");
    sections.set(group.id, {element:section, page:group.page});
    const heading = document.createElement("div");
    heading.className = "section-heading";
    const title = document.createElement("h2");
    title.textContent = group.title;
    heading.append(title);
    const description = document.createElement("p");
    description.className = "section-description";
    description.textContent = group.description;
    const fields = document.createElement("div");
    fields.className = "section-fields";
    const advanced = document.createElement("details"); advanced.className = "advanced-settings";
    const advancedTitle = document.createElement("summary"); advancedTitle.textContent = "高级设置";
    const advancedFields = document.createElement("div"); advancedFields.className = "section-fields";
    advanced.append(advancedTitle, advancedFields);
    for (const [key, label, type, placeholder, options, isAdvanced] of group.fields) {
      const wrapper = document.createElement("div");
      wrapper.className = "field";
      const labelNode = document.createElement("label");
      labelNode.htmlFor = key;
      labelNode.className = type === "bool" ? "toggle" : "field-heading";
      const labelText = document.createElement("span");
      labelText.textContent = label;
      const input = document.createElement(type === "textarea" ? "textarea" : type === "select" ? "select" : "input");
      input.id = key;
      if (type !== "select" && type !== "textarea") input.type = type === "secret" ? "password" : type === "bool" ? "checkbox" : type;
      if (type === "select") {
        for (const [value, text] of Object.entries(options)) {
          const option = document.createElement("option"); option.value = value; option.textContent = text; input.append(option);
        }
      }
      if (type === "textarea") { input.rows = 4; input.maxLength = key === "BOT_PERSONA_CUSTOM" ? 2000 : 8000; }
      if (key === "BOT_NAME") input.maxLength = 32;
      if (key === "BOT_CATCHPHRASE") input.maxLength = 80;
      input.autocomplete = type === "secret" ? "new-password" : "off";
      if (placeholder) input.placeholder = placeholder;
      if (key === "LLM_TIMEOUT") { input.min = "1"; input.max = "120"; input.step = "any"; }
      if (key === "ELECTRICITY_PAY_PROJECT") input.min = "1";
      if (key === "ELECTRICITY_HISTORY_RETENTION_DAYS") { input.min = "1"; input.max = "3650"; }
      let badge = null, clear = null;
      if (type === "bool") { labelNode.append(input, labelText); wrapper.append(labelNode); }
      else { labelNode.append(labelText); wrapper.append(labelNode, input); }
      if (type === "secret") {
        badge = document.createElement("span"); badge.className = "secret-status";
        labelNode.append(badge);
        const clearLabel = document.createElement("label"); clearLabel.className = "secret-actions";
        clear = document.createElement("input"); clear.type = "checkbox";
        clear.addEventListener("change", () => { if (clear.checked) input.value = ""; });
        input.addEventListener("input", () => { if (input.value) clear.checked = false; });
        clearLabel.append(clear, document.createTextNode("清除已有值"));
        wrapper.append(clearLabel);
      }
      controls.set(key, { input, type, badge, clear });
      (isAdvanced ? advancedFields : fields).append(wrapper);
    }
    section.append(heading, description, fields);
    if (advancedFields.childElementCount) section.append(advanced);
    el("config-groups").append(section);
    if (!group.test) continue;
    const row = document.createElement("div"); row.className = "test-row";
    const button = document.createElement("button"); button.type = "button";
    button.className = "secondary"; button.textContent = group.test;
    const output = document.createElement("output"); output.className = "test-result";
    button.addEventListener("click", async () => {
      button.disabled = true; output.className = "test-result"; output.textContent = "正在测试，请稍候…";
      try {
        const result = await api(`/api/test/${group.id}`, {});
        output.textContent = result.message;
        output.classList.add(result.ok ? "good" : "error");
      } catch (error) { output.textContent = error.message; output.classList.add("error"); }
      finally { button.disabled = false; }
    });
    row.append(button, output); section.append(row);
  }
}

function showPage(page) {
  if (!pageInfo[page]) return;
  currentPage = page;
  el("page-title").textContent = pageInfo[page][0];
  el("page-description").textContent = pageInfo[page][1];
  el("overview-panel").hidden = page !== "overview";
  el("chat-panel").hidden = page !== "chat";
  el("skills-panel").hidden = page !== "skills";
  el("settings-form").hidden = page === "overview" || page === "chat" || page === "skills";
  el("module-list").hidden = page !== "tools";
  for (const section of sections.values()) section.element.hidden = section.page !== page;
  for (const button of document.querySelectorAll(".sidebar [data-page]")) {
    button.classList.toggle("active", button.dataset.page === page);
    button.setAttribute("aria-current", button.dataset.page === page ? "page" : "false");
  }
}

function fill(data) {
  revision = data.revision;
  if (data.csrf) csrf = data.csrf;
  for (const [key, control] of controls) {
    const locked = data.locked_fields.includes(key);
    control.input.disabled = locked;
    control.input.title = locked ? "此项由部署环境提供，需要在服务器环境中修改" : "";
    if (control.type === "secret") {
      control.input.value = ""; control.clear.checked = false; control.clear.disabled = locked;
      control.badge.textContent = locked ? "由部署环境提供" : data.secrets[key] ? "已配置" : "未配置";
      control.badge.classList.toggle("configured", data.secrets[key]);
    } else if (control.type === "bool") control.input.checked = data.values[key] === "true";
    else control.input.value = data.values[key] || "";
  }
}

async function refreshSkills() {
  const data = await api("/api/skills"); el("skills-list").replaceChildren();
  if (!data.skills.length) {
    const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "尚未导入技能。上传后可以查看说明并启用。"; el("skills-list").append(empty);
  }
  for (const skill of data.skills) {
    const card = document.createElement("article"); card.className = "card skill-card";
    const title = document.createElement("strong"); title.textContent = skill.name;
    const status = document.createElement("span"); status.className = skill.enabled ? "module-enabled" : "muted"; status.textContent = skill.enabled ? "已启用" : "停用";
    const heading = document.createElement("div"); heading.className = "section-heading"; heading.append(title,status);
    const description = document.createElement("p"); description.textContent = skill.description; description.className = "small muted";
    const actions = document.createElement("div"); actions.className = "quick-actions";
    const view = document.createElement("button"); view.type = "button"; view.className = "secondary"; view.textContent = "查看说明";
    view.addEventListener("click", async () => {
      try { const detail = await api(`/api/skills/${encodeURIComponent(skill.name)}`); el("skill-detail").hidden = false; el("skill-detail-title").textContent = detail.name; el("skill-content").textContent = detail.content; }
      catch (error) { message("skill-message",error.message,true); }
    });
    const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "secondary"; toggle.textContent = skill.enabled ? "停用" : "启用";
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      try { const result = await api(`/api/skills/${encodeURIComponent(skill.name)}`, {enabled:!skill.enabled}); message("skill-message",result.message); await refreshSkills(); }
      catch (error) { message("skill-message",error.message,true); toggle.disabled = false; }
    });
    actions.append(view,toggle); card.append(heading,description);
    for (const warning of skill.warnings || []) { const note = document.createElement("p"); note.className = "small muted"; note.textContent = warning; card.append(note); }
    card.append(actions); el("skills-list").append(card);
  }
}

el("skill-upload-form").addEventListener("submit", async event => {
  event.preventDefault(); const file = el("skill-file").files[0]; if (!file) return;
  if (file.size > 1024 * 1024) { message("skill-message","技能文件不能超过1 MiB。",true); return; }
  const form = new FormData(); form.append("file",file); el("upload-skill").disabled = true;
  try { const result = await api("/api/skills/upload",form); message("skill-message",result.message); el("skill-file").value = ""; await refreshSkills(); }
  catch (error) { message("skill-message",error.message,true); }
  finally { el("upload-skill").disabled = false; }
});

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const names = { live: "运行中", "dry-run": "模拟运行", unavailable: "未运行 / 暂不可达" };
    el("service-status").textContent = names[data.service];
    el("service-status").classList.toggle("good", data.service === "live");
    el("service-hint").textContent = "表示本地服务健康，不代表 QQ 联调完成";
    for (const [id, ok, yes, no] of [
      ["qq-status", data.qq_configured, "已配置", "待配置"],
      ["llm-status", data.llm_enabled, "已启用", "未启用"],
      ["electricity-status", data.electricity_enabled, "已启用", "未启用"],
    ]) { el(id).textContent = ok ? yes : no; el(id).classList.toggle("good", ok); }
    el("version").textContent = `· v${data.version}`;
    for (const [id, key] of [["profile-count","profiles"],["bound-count","bound_dorms"],["query-count","queries"]]) el(id).textContent = data.counts ? data.counts[key] : "暂不可读";
    el("module-list").replaceChildren();
    for (const module of data.modules || []) {
      const item = document.createElement("article"); item.className = "card module-card";
      const name = document.createElement("strong"); name.textContent = module.name;
      const status = document.createElement("span"); status.textContent = module.enabled ? "已启用" : "未启用";
      status.className = module.enabled ? "module-enabled" : "muted";
      item.append(name,status); el("module-list").append(item);
    }
  } catch (error) { if (csrf) message("save-message", error.message, true); }
}

async function loadDashboard() {
  fill(await api("/api/settings"));
  el("login-panel").hidden = true; el("dashboard").hidden = false; el("logout").hidden = false;
  document.body.classList.add("dashboard-mode"); showPage(currentPage);
  if (currentPage === "skills") await refreshSkills();
  await refreshStatus(); clearInterval(timer); timer = setInterval(refreshStatus, 30000);
}

el("login-form").addEventListener("submit", async event => {
  event.preventDefault(); el("login-button").disabled = true;
  try {
    const data = await api("/api/login", { password: el("password").value });
    csrf = data.csrf; message("login-message", ""); await loadDashboard();
  } catch (error) { message("login-message", error.message, true); }
  finally { el("password").value = ""; el("login-button").disabled = false; }
});

el("settings-form").addEventListener("submit", async event => {
  event.preventDefault(); el("save-button").disabled = true;
  const values = {}, clear_secrets = [];
  for (const [key, control] of controls) {
    if (control.input.disabled) continue;
    if (control.type === "secret") {
      if (control.input.value) values[key] = control.input.value;
      if (control.clear.checked) clear_secrets.push(key);
    } else values[key] = control.type === "bool" ? control.input.checked : control.input.value;
  }
  try {
    const data = await api("/api/settings", { revision, values, clear_secrets });
    fill(data); message("save-message", data.message); await refreshStatus();
  } catch (error) { message("save-message", error.message, true); }
  finally { el("save-button").disabled = false; }
});

el("logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); showLogin(); }
  catch (error) { message("save-message", error.message, true); }
});
el("reload-settings").addEventListener("click", async () => {
  try { fill(await api("/api/settings")); message("save-message", "已重新读取保存的配置。"); }
  catch (error) { message("save-message", error.message, true); }
});
el("refresh-status").addEventListener("click", refreshStatus);
for (const button of document.querySelectorAll("[data-page]")) button.addEventListener("click", () => { showPage(button.dataset.page); if (button.dataset.page === "skills") refreshSkills().catch(error => message("skill-message",error.message,true)); });
for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => { el("chat-message").value = button.dataset.prompt; el("chat-message").focus(); });

function bubble(role, text) {
  el("chat-log").querySelector(".chat-empty")?.remove();
  const item = document.createElement("article"); item.className = "chat-bubble " + role;
  const label = document.createElement("span"); label.className = "small muted"; label.textContent = role === "user" ? "你" : "机器人";
  const content = document.createElement("div"); content.textContent = text;
  item.append(label, content); el("chat-log").append(item);
  while (el("chat-log").childElementCount > 50) el("chat-log").firstElementChild.remove();
  el("chat-log").scrollTop = el("chat-log").scrollHeight;
}
el("chat-form").addEventListener("submit", async event => {
  event.preventDefault(); const text = el("chat-message").value.trim();
  if (!text) return;
  el("send-chat").disabled = true; el("reset-chat").disabled = true;
  bubble("user",text); message("chat-status","正在处理，请稍候…");
  try {
    const result = await api("/api/chat", {message:text});
    bubble("assistant",result.reply); el("chat-message").value = "";
    message("chat-status",result.message);
  } catch (error) { message("chat-status",error.message,true); }
  finally { el("send-chat").disabled = false; el("reset-chat").disabled = false; }
});
el("chat-message").addEventListener("keydown", event => { if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); if (!el("send-chat").disabled) el("chat-form").requestSubmit(); } });
el("reset-chat").addEventListener("click", async () => {
  try { const result = await api("/api/chat/reset", {}); el("chat-log").replaceChildren(); message("chat-status",result.message); }
  catch (error) { message("chat-status",error.message,true); }
});
buildForm();
loadDashboard().catch(() => showLogin());
