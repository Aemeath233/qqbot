let accessRevision = "";
const accessFields = new Map();
async function refreshAccess() {
  const data = await api("/api/access"); accessRevision = data.revision;
  accessFields.clear(); el("access-rules").replaceChildren();
  for (const tool of data.catalog) {
    const rule = data.rules[tool.name];
    const row = document.createElement("article"); row.className = "access-rule";
    const heading = document.createElement("strong"); heading.textContent = tool.name;
    const description = document.createElement("p"); description.className = "small muted"; description.textContent = tool.description;
    row.append(heading,description);
    const modeLabel = document.createElement("label"); modeLabel.textContent = "调用方式";
    const mode = document.createElement("select");
    mode.setAttribute("aria-label","调用方式");
    for(const [value,text] of [["inherit","继承上一级规则"],["all","所有人"],["admin-only","仅管理测试"],["allowlist","指定群或用户"],["disabled","禁用"]]) {
      if(tool.name === "*" && value === "inherit") continue;
      const option = document.createElement("option"); option.value = value; option.textContent = text; mode.append(option);
    }
    mode.value = rule ? rule.mode : tool.name === "*" ? "all" : "inherit";
    modeLabel.append(mode); row.append(modeLabel);
    const scopes = document.createElement("div"); scopes.className = "access-scopes";
    const usersLabel = document.createElement("label"); usersLabel.textContent = "用户标识（每行一个）";
    const users = document.createElement("textarea"); users.rows = 2; users.placeholder = "/我的标识 返回的64位标识"; users.value = (rule?.users || []).join("\n"); usersLabel.append(users);
    const groupsLabel = document.createElement("label"); groupsLabel.textContent = "群标识（每行一个）";
    const groups = document.createElement("textarea"); groups.rows = 2; groups.placeholder = "群OpenID"; groups.value = (rule?.groups || []).join("\n"); groupsLabel.append(groups);
    scopes.append(usersLabel,groupsLabel); row.append(scopes);
    const update = () => { scopes.hidden = mode.value !== "allowlist"; }; mode.addEventListener("change",update); update();
    accessFields.set(tool.name,{mode,users,groups}); el("access-rules").append(row);
  }
}
el("access-form").addEventListener("submit", async event => {
  event.preventDefault(); const rules = Object.create(null);
  const split = value => [...new Set(value.split(/[\r\n,]+/).map(item => item.trim()).filter(Boolean))];
  for(const [name,fields] of accessFields) {
    if(fields.mode.value === "inherit") continue;
    rules[name] = {mode:fields.mode.value};
    if(fields.mode.value === "allowlist") { rules[name].users = split(fields.users.value); rules[name].groups = split(fields.groups.value); }
  }
  el("save-access").disabled = true;
  try { const data = await api("/api/access",{rules,revision:accessRevision}); message("access-message",data.message); await refreshAccess(); }
  catch(error) { message("access-message",error.message,true); }
  finally { el("save-access").disabled = false; }
});
el("reload-access").addEventListener("click",()=>refreshAccess().catch(error=>message("access-message",error.message,true)));
async function refreshPages() {
  const data = await api("/api/pages"); el("pages-list").replaceChildren();
  if(!data.pages.length) {const empty=document.createElement("p");empty.className="muted";empty.textContent="尚未生成网页。配置域名后可在聊天里发送 /做网页 需求。";el("pages-list").append(empty);}
  for(const page of data.pages) {
    const card=document.createElement("article");card.className="card skill-card";
    const title=document.createElement("strong");title.textContent=page.title;
    const note=document.createElement("p");note.className="small muted";note.textContent=`${page.id} · ${page.visibility} · ${new Date(page.created_at*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})}`;
    const actions=document.createElement("div");actions.className="quick-actions";
    if(page.visibility==="public" && data.public_base_url) {const link=document.createElement("a");link.href=data.public_base_url+"/p/"+encodeURIComponent(page.id);link.target="_blank";link.rel="noopener noreferrer";link.textContent="打开公开网页";link.className="secondary";actions.append(link);}
    if(page.visibility!=="draft") {const button=document.createElement("button");button.type="button";button.className="secondary";button.textContent="撤回网页";button.addEventListener("click",async()=>{button.disabled=true;try{const result=await api(`/api/pages/${encodeURIComponent(page.id)}/retract`,{});message("pages-message",result.message);await refreshPages();}catch(error){message("pages-message",error.message,true);button.disabled=false;}});actions.append(button);}
    card.append(title,note,actions);el("pages-list").append(card);
  }
}
el("refresh-pages").addEventListener("click",()=>refreshPages().catch(error=>message("pages-message",error.message,true)));
