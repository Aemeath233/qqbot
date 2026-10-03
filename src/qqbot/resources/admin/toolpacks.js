async function refreshToolpacks() {
  const data = await api("/api/toolpacks");
  el("toolpacks-list").replaceChildren();
  if (!data.enabled) message("toolpack-message", "MCP 工具总开关已关闭，请到功能开关中启用。", true);
  if (!data.packages.length) {
    const empty = document.createElement("p"); empty.className = "muted";
    empty.textContent = "尚未上传工具包。仓库 examples/toolpacks/campus-utils 可作为模板。";
    el("toolpacks-list").append(empty);
  }
  const statuses = {disabled:"停用", starting:"启动中", running:"运行中", failed:"失败", stopped:"已停止"};
  for (const pack of data.packages) {
    const card = document.createElement("article"); card.className = "card skill-card";
    const heading = document.createElement("div"); heading.className = "section-heading";
    const title = document.createElement("strong"); title.textContent = pack.name;
    const status = document.createElement("span"); status.textContent = statuses[pack.status] || pack.status;
    status.className = pack.status === "running" ? "module-enabled" : "muted";
    heading.append(title, status); card.append(heading);
    for (const text of [pack.description, pack.message,
      `额外依赖：${pack.requirements.join("、") || "无"}`,
      `模型工具：${pack.tools.join("、") || "尚未就绪"}`]) {
      const note = document.createElement("p"); note.className = "small muted";
      note.textContent = text; card.append(note);
    }
    const actions = document.createElement("div"); actions.className = "quick-actions";
    const view = document.createElement("button"); view.type = "button"; view.className = "secondary";
    view.textContent = "查看清单与源码";
    view.addEventListener("click", async () => {
      try {
        const result = await api(`/api/toolpacks/${encodeURIComponent(pack.name)}`);
        el("toolpack-detail").hidden = false; el("toolpack-detail-title").textContent = result.name;
        el("toolpack-source").textContent = result.manifest + "\n\n" + result.entrypoint + "\n\n" + result.source;
      } catch (error) { message("toolpack-message", error.message, true); }
    }); actions.append(view);
    const trust = document.createElement("input"); trust.type = "checkbox";
    if (!pack.enabled) {
      const label = document.createElement("label"); label.className = "check-label small";
      label.append(trust, document.createTextNode("我已审查并信任此包，允许机器人执行代码、安装依赖并供 QQ 聊天用户调用"));
      card.append(label);
    }
    const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "secondary";
    toggle.textContent = pack.enabled ? "停用工具包" : "信任并启用";
    toggle.addEventListener("click", async () => {
      if (!pack.enabled && !trust.checked) { message("toolpack-message", "请先查看源码并勾选信任确认。", true); return; }
      toggle.disabled = true;
      try {
        const result = await api(`/api/toolpacks/${encodeURIComponent(pack.name)}`, {
          enabled: !pack.enabled, trusted: !pack.enabled && trust.checked,
        });
        message("toolpack-message", result.message); await refreshToolpacks();
      } catch (error) { message("toolpack-message", error.message, true); toggle.disabled = false; }
    }); actions.append(toggle);
    if (pack.enabled) {
      const restart = document.createElement("button"); restart.type = "button"; restart.className = "secondary";
      restart.textContent = "重启工具包";
      restart.addEventListener("click", async () => {
        restart.disabled = true;
        try {
          const result = await api(`/api/toolpacks/${encodeURIComponent(pack.name)}/restart`, {});
          message("toolpack-message", result.message); await refreshToolpacks();
        } catch (error) { message("toolpack-message", error.message, true); restart.disabled = false; }
      }); actions.append(restart);
    }
    card.append(actions);
    if (pack.env_keys.length) {
      const form = document.createElement("form"); form.className = "tool-env-form";
      const fields = [];
      for (const key of pack.env_keys) {
        const field = document.createElement("label");
        field.textContent = `${key}（${pack.env_configured[key] ? "已配置" : "未配置"}）`;
        const input = document.createElement("input"); input.type = "password";
        input.autocomplete = "new-password"; input.placeholder = "留空保留原值"; input.maxLength = 2000;
        field.append(input); form.append(field);
        const clear = document.createElement("input"); clear.type = "checkbox";
        const clearLabel = document.createElement("label"); clearLabel.className = "check-label small";
        clearLabel.append(clear, document.createTextNode(`清除 ${key} 已有值`)); form.append(clearLabel);
        fields.push({key, input, clear});
      }
      const save = document.createElement("button"); save.type = "submit"; save.className = "secondary";
      save.textContent = "保存工具专用配置"; form.append(save);
      form.addEventListener("submit", async event => {
        event.preventDefault(); const values = {}, clear = [];
        for (const field of fields) {
          if (field.input.value) values[field.key] = field.input.value;
          if (field.clear.checked) clear.push(field.key);
        }
        save.disabled = true;
        try {
          const result = await api(`/api/toolpacks/${encodeURIComponent(pack.name)}/env`, {values, clear});
          for (const field of fields) field.input.value = "";
          message("toolpack-message", result.message); await refreshToolpacks();
        } catch (error) { message("toolpack-message", error.message, true); save.disabled = false; }
      }); card.append(form);
    }
    el("toolpacks-list").append(card);
  }
}

el("toolpack-upload-form").addEventListener("submit", async event => {
  event.preventDefault(); const file = el("toolpack-file").files[0]; if (!file) return;
  if (file.size > 1024 * 1024) { message("toolpack-message", "工具包不能超过1 MiB。", true); return; }
  const form = new FormData(); form.append("file", file); el("upload-toolpack").disabled = true;
  try {
    const result = await api("/api/toolpacks/upload", form);
    message("toolpack-message", result.message); el("toolpack-file").value = ""; await refreshToolpacks();
  } catch (error) { message("toolpack-message", error.message, true); }
  finally { el("upload-toolpack").disabled = false; }
});
el("refresh-toolpacks").addEventListener("click", () => refreshToolpacks().catch(error => message("toolpack-message", error.message, true)));
