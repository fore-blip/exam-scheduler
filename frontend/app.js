/* 门诊检查路线规划 · 前端逻辑 */

const DEPT_COLORS = ["#2563EB","#7C3AED","#0D9488","#D97706","#DB2777","#0891B2"];
let CATALOG = null;
let BASE = null;
let DEPT_COLOR = {};
let BY_CODE = {};
const selected = new Set();

const $ = (id) => document.getElementById(id);

function fmtClock(offset){
  const d = new Date(BASE.getTime() + Math.round(offset) * 60000);
  return String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
}

let toastTimer = null;
function toast(msg){
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
}

/* ---------------- 加载 ---------------- */
async function boot(){
  let data;
  try{
    const res = await fetch("api/catalog", {cache:"no-store"});
    if(!res.ok) throw new Error("HTTP " + res.status);
    data = await res.json();
  }catch(err){
    document.querySelector("main").innerHTML =
      '<div class="offline"><h2>没有连上本地服务</h2>' +
      '<p style="color:#6B7785;margin:10px 0 14px">这个页面需要后台算法服务提供数据。请先在项目目录运行：</p>' +
      '<p><code>python webapp.py</code></p>' +
      '<p style="color:#9AA5B1;font-size:12px;margin-top:14px">然后打开终端里打印的地址。</p></div>';
    document.querySelector(".topbar").style.display = "none";
    return;
  }

  CATALOG = data;
  BASE = new Date(data.snapshot_time);

  const depts = data.departments.map(d => d.name);
  depts.forEach((d,i) => { DEPT_COLOR[d] = DEPT_COLORS[i % DEPT_COLORS.length]; });
  data.exams.forEach(e => { BY_CODE[e.code] = e; });

  $("subtle").textContent =
    `${fmtClock(0)} 快照 · ${data.departments.length} 个科室候检 · 共 ${data.exams.length} 项可开检查`;

  renderDepts(data.departments);
  renderExams();
  sync();
}

function renderDepts(list){
  const grid = $("depts");
  const sig = list.map(d => d.name).join("|");

  if(grid.dataset.sig !== sig){
    grid.dataset.sig = sig;
    grid.innerHTML = list.map(d => {
      const color = DEPT_COLOR[d.name] || "#8899AA";
      return `<div class="dept" data-dept="${d.name}">
        <div class="name"><i class="dot" style="background:${color}"></i>${d.name}</div>
        <div class="num"><span class="v">–</span><small>分钟</small></div>
        <div class="hint"><span class="q">–</span> 人排队 · 叫到 <span class="sno">–</span> 号</div>
        <div class="bar"><i style="width:0%;background:${color}"></i></div>
      </div>`;
    }).join("");
  }

  const maxWait = Math.max(1, ...list.map(d => d.wait_minutes));
  list.forEach(d => {
    const el = grid.querySelector(`[data-dept="${d.name}"]`);
    if(!el) return;
    const vEl = el.querySelector(".v");
    const next = String(Math.round(d.wait_minutes));
    if(vEl.textContent !== next){
      const had = vEl.textContent !== "–";
      vEl.textContent = next;
      if(had){
        el.classList.add("changed");
        setTimeout(() => el.classList.remove("changed"), 750);
      }
    }
    el.querySelector(".q").textContent = d.queue_length;
    const sno = el.querySelector(".sno");
    if(sno) sno.textContent = (d.serving_no === null || d.serving_no === undefined) ? "–" : d.serving_no;
    el.querySelector(".bar i").style.width = Math.max(4, (d.wait_minutes / maxWait) * 100) + "%";
  });
}

function renderExams(){
  $("exams").innerHTML = CATALOG.exams.map(e => {
    const on = selected.has(e.code);
    const color = DEPT_COLOR[e.department] || "#8899AA";
    const tags = [];
    if(e.requires_fasting) tags.push('<span class="tag">需空腹</span>');
    if(tickets[e.code]) tags.push(`<span class="tag tk-on">已取 ${tickets[e.code]} 号</span>`);
    if(e.requires && e.requires.length){
      const names = e.requires.map(r => (BY_CODE[r.code] || {}).name || r.code).join("、");
      tags.push(`<span class="tag dep">需先做 ${names}</span>`);
    }
    const win = e.window ? `<span class="win">开放 ${e.window.open}–${e.window.close}</span>` : "";
    return `<button class="exam ${on ? "on" : ""}" data-code="${e.code}" aria-pressed="${on}">
      <span class="tick"></span>
      <span class="name">${e.name}</span>
      <span class="meta"><i class="dot" style="background:${color}"></i>${e.department} · ${e.service_minutes} 分钟</span>
      <span>${tags.join("")}${win}</span>
    </button>`;
  }).join("");

  document.querySelectorAll(".exam").forEach(el => {
    el.addEventListener("click", () => toggle(el.dataset.code));
  });
}

const tickets = {};        // code -> 取到的号
const ticketMeta = {};     // code -> {ahead, wait, serving, department, sharedWith}

/* ---------------- 取号 ---------------- */
async function apiTake(department){
  try{
    const r = await fetch("api/take", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({department})
    });
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }catch(err){
    toast("取号失败：" + err.message);
    return null;
  }
}

/** 为某个项目取号；同科室已有号时直接复用（如检验科一次采血做两项）。*/
async function takeFor(code){
  const e = BY_CODE[code];
  const same = [...selected].find(c =>
    c !== code && BY_CODE[c] && BY_CODE[c].department === e.department && tickets[c]);
  if(same){
    tickets[code] = tickets[same];
    ticketMeta[code] = Object.assign({}, ticketMeta[same], {sharedWith: same});
    return;
  }
  const d = await apiTake(e.department);
  if(!d) return;
  tickets[code] = d.ticket;
  ticketMeta[code] = {ahead:d.ahead, wait:d.wait_minutes, serving:d.serving_no, department:e.department};
}

/** 重新取号：同科室所有共用项一起换成新号。*/
async function retake(code){
  const e = BY_CODE[code];
  const d = await apiTake(e.department);
  if(!d) return;
  [...selected].forEach(c => {
    if(BY_CODE[c].department !== e.department) return;
    tickets[c] = d.ticket;
    ticketMeta[c] = {ahead:d.ahead, wait:d.wait_minutes, serving:d.serving_no,
                     department:e.department, sharedWith: c === code ? null : code};
  });
  renderExams();
  renderTickets();
  afterSelectionChange();
}

function renderTickets(){
  const panel = $("ticketPanel");
  if(!selected.size){ panel.style.display = "none"; return; }
  panel.style.display = "block";

  const grid = $("tickets");
  const sig = [...selected].sort().join("|") + "#" + JSON.stringify(tickets) + JSON.stringify(ticketMeta);
  if(grid.dataset.sig !== sig){
    grid.dataset.sig = sig;
    grid.innerHTML = [...selected].map(code => {
      const e = BY_CODE[code];
      const tk = tickets[code];
      const m = ticketMeta[code] || {};
      let sub = "";
      if(m.sharedWith && selected.has(m.sharedWith)){
        sub = `与${BY_CODE[m.sharedWith].name}同号`;
      }else if(m.ahead !== undefined){
        sub = `前方 ${m.ahead} 人 · ${Math.round(m.wait || 0)} 分`;
      }
      return `<div class="ticket-row ${tk ? "filled" : ""}" data-row="${code}">
        <span class="nm">${e.name}</span>
        <span class="dp">${e.department}</span>
        <span class="sub">${sub}</span>
        <span class="tk">${tk ? tk + " 号" : "未取号"}</span>
        <button class="refresh" data-take="${code}" title="重新取号">重取</button>
      </div>`;
    }).join("");
    grid.querySelectorAll("[data-take]").forEach(b => {
      b.onclick = ev => { ev.stopPropagation(); retake(b.dataset.take); };
    });
  }
  $("ticketCount").textContent = `已取号 ${Object.keys(tickets).length} / ${selected.size} 项`;
}

/* ---------------- 选择逻辑 ---------------- */
function collectPrereqs(code, bag){
  const e = BY_CODE[code];
  if(!e) return;
  (e.requires || []).forEach(r => {
    if(!selected.has(r.code) && !bag.includes(r.code)){
      bag.push(r.code);
      collectPrereqs(r.code, bag);
    }
  });
}

function collectDependents(code, bag){
  CATALOG.exams.forEach(e => {
    if(selected.has(e.code) && (e.requires || []).some(r => r.code === code) && !bag.includes(e.code)){
      bag.push(e.code);
      collectDependents(e.code, bag);
    }
  });
}

async function toggle(code){
  if(selected.has(code)){
    selected.delete(code);
    delete tickets[code];
    delete ticketMeta[code];
    const dependents = [];
    collectDependents(code, dependents);
    if(dependents.length){
      dependents.forEach(c => { selected.delete(c); delete tickets[c]; delete ticketMeta[c]; });
      toast(`「${BY_CODE[code].name}」是 ${dependents.map(c => BY_CODE[c].name).join("、")} 的前置，已一并取消`);
    }
    Object.keys(ticketMeta).forEach(c => {
      const m = ticketMeta[c];
      if(m && m.sharedWith && !selected.has(m.sharedWith)) delete m.sharedWith;
    });
  }else{
    const bag = [];
    collectPrereqs(code, bag);
    for(const c of [...bag, code]){
      if(selected.has(c)) continue;
      await takeFor(c);          // 点一下就是取一次号
      selected.add(c);
    }
    if(bag.length){
      toast(`已自动加入前置检查并取号：${bag.map(c => BY_CODE[c].name).join("、")}`);
    }
  }
  Object.keys(tickets).forEach(c => { if(!selected.has(c)) delete tickets[c]; });
  renderExams();
  afterSelectionChange();
}

function sync(){
  $("count").textContent = `已选 ${selected.size} 项`;
  $("run").disabled = selected.size === 0;
}

function hideResult(){
  const box = $("result");
  box.classList.remove("show");
  box.classList.remove("stale");
  box.innerHTML = "";
}

/* ---------------- 求解 ---------------- */
let runToken = 0;
let lastPlanKey = "";
let lastTotal = 0;
let running = false;

function markStale(){
  const box = $("result");
  if(box.classList.contains("show")) box.classList.add("stale");
}

function afterSelectionChange(){
  sync();
  lastPlanKey = "";          // 选择变了，下一次必定重绘
  lastTotal = 0;
  renderTickets();
  if(!selected.size){ hideResult(); return; }
  markStale();
}

async function run(auto){
  if(!selected.size){ hideResult(); return; }
  if(auto && running) return;

  running = true;
  const token = ++runToken;
  const btn = $("run");
  if(!auto){ btn.disabled = true; btn.textContent = "计算中…"; }

  try{
    const res = await fetch("api/recommend", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({codes:[...selected], fasting:$("fasting").checked, tickets})
    });
    const data = await res.json();
    if(token !== runToken) return;          // 丢弃过期响应，避免旧结果覆盖新选择
    if(!res.ok) throw new Error(data.error || ("HTTP " + res.status));

    const order = data.order || (data.steps || []).map(s => s.code);
    const key = data.feasible
      ? order.join("|") + "@" + Math.round(data.total_minutes)
      : "infeasible";

    // 队列刷新触发的自动重算：方案没变就不重绘，避免页面每 3 秒闪一下
    if(auto && key === lastPlanKey) return;

    let note = "";
    if(auto && lastPlanKey && data.feasible){
      note = `队列已变化，方案自动重算：${Math.round(lastTotal)} → ${Math.round(data.total_minutes)} 分钟`;
    }
    lastPlanKey = key;
    lastTotal = data.total_minutes;

    renderResult(data, note, auto);
  }catch(err){
    if(token !== runToken) return;
    if(!auto){
      $("result").innerHTML = `<div class="alert"><b>求解失败</b>${err.message}</div>`;
      $("result").classList.remove("stale");
      $("result").classList.add("show");
    }
  }finally{
    running = false;
    if(token === runToken){
      btn.disabled = selected.size === 0;
      btn.textContent = "生成最优路线";
    }
  }
}

function segmentsOf(d){
  const segs = [];
  let cursor = 0;
  d.steps.forEach((s,i) => {
    if(s.arrive_at > cursor + 0.01) segs.push({type:"idle", m:s.arrive_at - cursor, label:"等结果"});
    if(s.wait_minutes > 0.01) segs.push({type:"wait", m:s.wait_minutes, label:"排队"});
    segs.push({type:"svc", m:s.service_minutes, label:s.name, dept:s.department, idx:i+1});
    cursor = s.finish_at;
  });
  return segs;
}

function renderResult(d, note, auto){
  const box = $("result");

  if(!d.feasible){
    box.innerHTML = `<div class="alert">
      <b>这个组合目前排不出来</b>
      <ul>${d.conflicts.map(c => `<li>${c}</li>`).join("")}</ul>
    </div>`;
    box.classList.remove("stale");
    box.classList.add("show");
    box.scrollIntoView({behavior:"smooth", block:"nearest"});
    return;
  }

  const total = d.total_minutes || 1;
  const segs = segmentsOf(d);
  const barHtml = segs.map(s => {
    const pct = (s.m / total) * 100;
    if(pct < 0.4) return "";
    let style = `width:${pct}%`;
    let cls = "seg";
    if(s.type === "svc"){
      style += `;background:${DEPT_COLOR[s.dept] || "#8899AA"}`;
    }else{
      cls += " " + s.type;
    }
    const label = pct >= 8
      ? `<span>${s.label}</span>`
      : (s.idx && pct >= 2.5 ? `<span class="num">${s.idx}</span>` : "");
    return `<div class="${cls}" style="${style}" title="${s.label} ${Math.round(s.m)} 分钟">${label}</div>`;
  }).join("");

  const deptLegend = [...new Set(d.steps.map(s => s.department))].map(dept =>
    `<span><i style="background:${DEPT_COLOR[dept] || "#8899AA"}"></i>${dept}</span>`).join("");

  const stepsHtml = d.steps.map((s,i) => {
    const bits = [];
    if(s.missed) bits.push("号已过 · 现场重取");
    else if(s.shared_with) bits.push("同科室顺带完成");
    else if(s.call_at !== null && s.call_at !== undefined) bits.push(`约 ${fmtClock(s.call_at)} 叫到`);
    if(s.wait_minutes > 0.5) bits.push(`等待 ${Math.round(s.wait_minutes)} 分`);
    else bits.push("到点即做");
    bits.push(`${fmtClock(s.start_at)}–${fmtClock(s.finish_at)}`);
    const idle = i > 0 ? s.arrive_at - d.steps[i-1].finish_at : 0;
    if(idle > 0.5) bits.unshift(`空转 ${Math.round(idle)} 分`);
    const badge = s.ticket
      ? `<span class="tk-badge${s.missed ? " miss" : ""}">${s.ticket} 号${s.missed ? " 已过" : ""}</span>`
      : "";
    return `<li class="step">
      <div class="idx">${i+1}</div>
      <div class="body">
        <div class="l1"><b>${s.name}</b><span class="dept">${s.department}</span>${badge}</div>
        <div class="l2">${fmtClock(s.arrive_at)} 到达 · ${bits.join(" · ")}</div>
        ${s.reason ? `<div class="reason">${s.reason}</div>` : ""}
      </div>
    </li>`;
  }).join("");

  const cmp = d.comparison || {};
  const cmpVals = Object.values(cmp).filter(v => typeof v === "number");
  const maxCmp = Math.max(1, ...cmpVals);
  const cmpHtml = Object.entries(cmp).map(([name,val]) => {
    if(val === null || val === undefined){
      return `<div class="cmp-row"><span class="cmp-name">${name}</span>
        <div class="cmp-track"><i style="width:0"></i></div>
        <span class="cmp-val">不可行</span></div>`;
    }
    const isBest = name === "本方案";
    const diff = val - d.total_minutes;
    const note = (!isBest && diff > 0.5) ? `<small>+${Math.round(diff)}</small>` : "";
    return `<div class="cmp-row">
      <span class="cmp-name">${name}</span>
      <div class="cmp-track"><i class="${isBest ? "best" : ""}" style="width:${(val/maxCmp)*100}%"></i></div>
      <span class="cmp-val">${Math.round(val)} 分${note}</span>
    </div>`;
  }).join("");

  box.innerHTML = `
    <div class="stale-bar">选择已变更 · 点右上角「生成最优路线」更新结果</div>
    ${note ? `<div class="plan-note">${note}</div>` : ""}
    <div class="panel-head" style="margin-bottom:14px">
      <h2>推荐路线</h2>
      <span class="spacer"></span>
      <span class="hint">${d.status === "OPTIMAL" ? "已证明为最短方案" : d.status} · ${d.nodes} 个搜索节点 · ${d.elapsed_ms} ms</span>
    </div>

    <div class="stat">
      <span class="big">${Math.round(d.total_minutes)}</span>
      <span class="unit">分钟完成全部检查</span>
      <span class="range">${fmtClock(0)} → ${fmtClock(d.total_minutes)}</span>
    </div>
    <div class="substat">
      其中排队 <b>${Math.round(d.total_wait)}</b> 分钟${d.total_idle > 0.5 ? ` · 等结果空转 <b>${Math.round(d.total_idle)}</b> 分钟` : ""}
      · 实际检查 <b>${Math.round(d.total_minutes - d.total_wait - d.total_idle)}</b> 分钟
    </div>

    <div class="scope">本次纳入 <b>${d.steps.length}</b> 项：${d.steps.map(s => s.name).join("、")}</div>

    <div class="timeline">
      <div class="bar">${barHtml}</div>
      <div class="axis"><span>${fmtClock(0)}</span><span>${fmtClock(d.total_minutes/2)}</span><span>${fmtClock(d.total_minutes)}</span></div>
      <div class="legend">
        ${deptLegend}
        <span><i style="background:repeating-linear-gradient(45deg,#D3E3F5,#D3E3F5 4px,#E3EDF9 4px,#E3EDF9 8px)"></i>排队</span>
      </div>
    </div>

    <ul class="steps">${stepsHtml}</ul>

    <div style="margin-top:22px;border-top:1px solid #F1F4F8;padding-top:16px">
      <div class="panel-head" style="margin-bottom:6px"><h2 style="font-size:14px">与其他排法的对比</h2></div>
      <div class="cmp">${cmpHtml}</div>
    </div>

    ${d.notes && d.notes.length ? `<ul class="notes">${d.notes.map(n => `<li>${n}</li>`).join("")}</ul>` : ""}
  `;

  box.classList.remove("stale");
  box.classList.add("show");
  if(!auto) box.scrollIntoView({behavior:"smooth", block:"nearest"});
}

async function loadDemo(){
  selected.clear();
  Object.keys(tickets).forEach(c => delete tickets[c]);
  Object.keys(ticketMeta).forEach(c => delete ticketMeta[c]);

  (CATALOG.default_codes || []).forEach(c => selected.add(c));
  const bag = [];
  [...selected].forEach(c => collectPrereqs(c, bag));
  bag.forEach(c => selected.add(c));

  for(const c of [...selected]){
    delete tickets[c];
    delete ticketMeta[c];
    await takeFor(c);          // 每项都真的去取一次号
  }
  renderExams();
  afterSelectionChange();
  run(false);
}

/* ---------------- 实时快照 ---------------- */
let live = true;
let pollTimer = null;

function setLive(on){
  live = on;
  $("live").classList.toggle("paused", !on);
  $("liveText").textContent = on ? "实时" : "已暂停";
}

function updateDeptHint(d){
  const st = d.status || {};
  if(st.ok === false){
    const age = st.age_seconds == null ? "" : `（${Math.round(st.age_seconds)} 秒前）`;
    $("deptHint").textContent = `数据源异常${age}，显示最后一次拿到的快照`;
  }else if(st.source === "local"){
    $("deptHint").textContent = "未连接叫号系统，使用本地兜底数据";
  }else{
    $("deptHint").textContent = `来自叫号系统 · 更新于 ${d.server_time || "--:--:--"}`;
  }
}

function maybeRecompute(){
  const box = $("result");
  if(!box.classList.contains("show")) return;
  if(box.classList.contains("stale")) return;   // 选择变了，等用户点按钮
  if(!selected.size) return;
  run(true);
}

async function pollSnapshot(){
  clearTimeout(pollTimer);
  if(live){
    try{
      const r = await fetch("api/snapshot", {cache:"no-store"});
      if(r.ok){
        const d = await r.json();
        renderDepts(d.departments);
        updateDeptHint(d);
        maybeRecompute();
      }
    }catch(e){ /* 下一轮再试 */ }
  }
  pollTimer = setTimeout(pollSnapshot, 3000);
}

/* ---------------- 事件 ---------------- */
$("run").addEventListener("click", () => run(false));
$("clear").addEventListener("click", () => {
  selected.clear(); runToken++; lastPlanKey = "";
  Object.keys(tickets).forEach(c => delete tickets[c]);
  Object.keys(ticketMeta).forEach(c => delete ticketMeta[c]);
  renderExams(); sync(); hideResult(); renderTickets();
});
$("demo").addEventListener("click", loadDemo);
$("fasting").addEventListener("change", afterSelectionChange);
$("live").addEventListener("click", () => {
  setLive(!live);
  if(live) pollSnapshot();
});

boot().then(() => pollSnapshot());
