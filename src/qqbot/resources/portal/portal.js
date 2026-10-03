"use strict";
const mode = document.body.dataset.mode;
const id = document.body.dataset.id;
const token = location.hash.slice(1);
const node = id => document.getElementById(id);
const number = value => Number(value).toLocaleString("zh-CN", {maximumFractionDigits:3});
const date = value => new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value));
const ns = "http://www.w3.org/2000/svg";
function svgNode(name,attrs,text) {
  const element = document.createElementNS(ns,name);
  for(const [key,value] of Object.entries(attrs)) element.setAttribute(key,String(value));
  if(text !== undefined) element.textContent = text;
  return element;
}
function render(data) {
  node("curve-panel").hidden = false;
  for(const option of node("days").options) option.disabled = Number(option.value)>data.maximum_days && option.value!=="365";
  node("days").value = [...node("days").options].some(option=>option.value===String(data.days)) ? String(data.days) : "365";
  node("metrics").replaceChildren();
  const last = data.points.at(-1);
  for(const [label,value] of [["最新记录电量",last ? number(last.kwh)+" 度" : "暂无读数"],["独立读数",data.sample_count+" 次"],[Number(data.net_decrease_kwh)<0 ? "余额净增加" : "余额净减少",data.net_decrease_kwh === null ? "数据不足" : number(Math.abs(Number(data.net_decrease_kwh)))+" 度"]]) {
    const box = document.createElement("article"); box.className = "metric";
    const caption = document.createElement("span"); caption.textContent = label;
    const amount = document.createElement("strong"); amount.textContent = value;
    box.append(caption,amount); node("metrics").append(box);
  }
  node("chart-note").textContent = `${data.dormitory || "尚无记录"} ${data.area_name} · ${data.days}天内的已存记录。${data.note}${data.has_increase ? " 检测到电量上升，可能有充值或修正。" : ""}`;
  if(data.points.length) node("chart-note").textContent += ` 实际记录覆盖：${date(data.points[0].at)} 至 ${date(data.points.at(-1).at)}。`;
  node("chart").replaceChildren();
  if(!data.points.length) node("chart").textContent = "暂无查询读数。以后成功查询后，刷新页面即可看到新记录。";
  else {
    const points = data.points.map(point => ({time:Date.parse(point.at),value:Number(point.kwh),label:date(point.at)}));
    const xmin = points[0].time, xmax = points.at(-1).time;
    const ymin = Math.min(...points.map(p => p.value)), ymax = Math.max(...points.map(p => p.value));
    const low = Math.max(0,ymin-(ymax-ymin || 1)*.15), high = ymax+(ymax-ymin || 1)*.15;
    const x = value => 60+(xmax===xmin ? .5 : (value-xmin)/(xmax-xmin))*800;
    const y = value => 255-(value-low)/(high-low)*210;
    const svg = svgNode("svg",{viewBox:"0 0 900 310",role:"img","aria-label":"实际查询时间与剩余电量"});
    for(let index=0; index<=4; index++) {
      const value = low+(high-low)*index/4;
      svg.append(svgNode("line",{x1:60,y1:y(value),x2:860,y2:y(value),stroke:"#e2eeea"}),svgNode("text",{x:50,y:y(value)+4,"text-anchor":"end"},number(value)));
    }
    svg.append(svgNode("text",{x:60,y:20},"剩余电量（度）"));
    svg.append(svgNode("polyline",{points:points.map(p => `${x(p.time)},${y(p.value)}`).join(" "),fill:"none",stroke:"#138b7b","stroke-width":3}));
    for(const point of points) {
      const circle = svgNode("circle",{cx:x(point.time),cy:y(point.value),r:4,fill:"#138b7b",tabindex:0});
      circle.append(svgNode("title",{},`${point.label} · ${number(point.value)}度`)); svg.append(circle);
    }
    svg.append(svgNode("text",{x:60,y:284},points[0].label),svgNode("text",{x:860,y:284,"text-anchor":"end"},points.at(-1).label));
    node("chart").append(svg);
  }
  const table = document.createElement("table");
  const heading = document.createElement("tr");
  for(const text of ["读数时间（北京时间）","剩余电量（度）"]) {const th=document.createElement("th");th.textContent=text;heading.append(th);} table.append(heading);
  for(const point of [...data.points].reverse().slice(0,100)) {
    const row=document.createElement("tr");for(const text of [date(point.at),point.kwh]) {const td=document.createElement("td");td.textContent=text;row.append(td);} table.append(row);
  }
  node("readings").replaceChildren(table);
}
async function load() {
  if(!["curve","preview"].includes(mode)) return;
  if(!token) {node("portal-state").textContent="请使用机器人发来的完整访问链接（含#后的令牌）。";return;}
  try {
    const payload = {token}; if(mode==="curve") payload.days=Number(node("days").value);
    const response = await fetch(`/_portal/${mode==="curve" ? "curve" : "page"}/${encodeURIComponent(id)}`,{method:"POST",credentials:"omit",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const data = await response.json(); if(!response.ok) throw new Error(data.message || "链接无法访问。");
    node("portal-state").textContent = `访问链接有效至 ${date(data.expires_at*1000)}（北京时间）。`;
    if(mode==="curve") render(data);
    else {node("preview-frame").hidden=false;node("preview-frame").srcdoc=data.html;document.querySelector("h1").textContent=data.title;}
  } catch(error) {node("portal-state").textContent=error.message;node("curve-panel").hidden=true;node("preview-frame").hidden=true;}
}
if(node("days")) node("days").addEventListener("change",load);
load();
