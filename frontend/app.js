/* 门诊检查路线规划 · 前端逻辑（医生端 + 患者端） */

const $ = (id) => document.getElementById(id);

const DEPT_COLOR = {
  "检验科": "#2563EB",
  "心电科": "#7C3AED",
  "超声科": "#0D9488",
  "放射科": "#D97706",
  "呼吸科": "#DB2777",
};

let CATALOG = { exams: [], departments: [], snapshot_time: "" };
const BY_CODE = {};
let BASE_DATE = new Date();

let live = true;
let pollTimer = null;
let currentView = "user";

/* ==================== 工具 ==================== */

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function fmtClock(m) {
  const t = new Date(BASE_DATE.getTime() + m * 60000);
  return String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ==================== 启动 ==================== */

async function boot() {
  try {
    const r = await fetch("api/catalog");
    CATALOG = await r.json();
  } catch (e) {
    toast("读取配置失败：" + e.message);
    return;
  }

  (CATALOG.exams || []).forEach((e) => (BY_CODE[e.code] = e));
  BASE_DATE = parseBase(CATALOG.snapshot_time);

  renderDepts(CATALOG.departments || []);
  refreshSubtle();

  await loadPatients();
  await refreshUser();
  pollSnapshot();
}

function parseBase(iso) {
  if (iso) {
    const d = new Date(iso);
    if (!isNaN(d.getTime())) return d;
  }
  const d = new Date();
  d.setHours(9, 5, 0, 0);
  return d;
}

function refreshSubtle() {
  const st = CATALOG.snapshot_time ? new Date(CATALOG.snapshot_time) : BASE_DATE;
  const hh = String(st.getHours()).padStart(2, "0");
  const mm = String(st.getMinutes()).padStart(2, "0");
  $("subtle").textContent =
    hh + ":" + mm + " 快照 · " + (CATALOG.departments || []).length +
    " 个科室候检 · 共 " + (CATALOG.exams || []).length + " 项可开检查";
}

/* ==================== 视图切换 ==================== */

function setView(v) {
  currentView = v;
  $("viewUser").hidden = v !== "user";
  $("viewDoctor").hidden = v !== "doctor";
  document.querySelectorAll("#viewTabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.view === v);
  });
  if (v === "doctor") loadPatients();
  else refreshUser();
}

/* ==================== 实时科室卡片 ==================== */

async function pollSnapshot() {
  clearTimeout(pollTimer);
  if (live) {
    try {
      const r = await fetch("api/snapshot", { cache: "no-store" });
      if (r.ok) {
        const d = await r.json();
        renderDepts(d.departments);
        updateDeptHint(d);
      }
    } catch (e) {
      /* 下一轮再试 */
    }
  }
  pollTimer = setTimeout(pollSnapshot, 3000);
}

function renderDepts(list) {
  if (!list || !list.length) return;
  const maxWait = Math.max(1, ...list.map((d) => d.wait_minutes));
  const sig = list.map((d) => d.name).join("|");
  const box = $("depts");

  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = list.map((d) => {
      const color = DEPT_COLOR[d.name] || "#8899AA";
      const pct = Math.max(4, (d.wait_minutes / maxWait) * 100);
      return `<div class="dept" data-dept="${esc(d.name)}">
        <div class="name"><i class="dot" style="background:${color}"></i>${esc(d.name)}</div>
        <div class="num">${Math.round(d.wait_minutes)}<small>分钟</small></div>
        <div class="hint">${d.queue_length} 人排队 · 叫到 ${d.serving_no == null ? "–" : d.serving_no} 号</div>
        <div class="bar"><i style="width:${pct}%;background:${color}"></i></div>
      </div>`;
    }).join("");
  } else {
    list.forEach((d) => {
      const el = box.querySelector('[data-dept="' + d.name + '"]');
      if (!el) return;
      el.querySelector(".num").innerHTML = Math.round(d.wait_minutes) + "<small>分钟</small>";
      el.querySelector(".hint").textContent =
        d.queue_length + " 人排队 · 叫到 " + (d.serving_no == null ? "–" : d.serving_no) + " 号";
      const bar = el.querySelector(".bar i");
      if (bar) bar.style.width = Math.max(4, (d.wait_minutes / maxWait) * 100) + "%";
    });
  }
}

function updateDeptHint(d) {
  const s = d.status || {};
  const el = $("deptHint");
  if (s.ok) el.textContent = "来自叫号系统 · 更新于 " + (s.fetched_at || "—");
  else el.textContent = "数据源异常，显示上次快照" +
    (s.age_seconds != null ? "（" + s.age_seconds + " 秒前）" : "");
}

/* ==================== 医生端 ==================== */

let patients = [];
let currentPatient = null;
let picked = new Set();

async function loadPatients() {
  try {
    const r = await fetch("api/patients");
    const d = await r.json();
    patients = d.patients || [];
  } catch (e) {
    patients = [];
  }
  if (currentPatient && !patients.some((p) => p.patient_id === currentPatient)) {
    currentPatient = null;
    picked.clear();
  }
  renderPatients();
  renderExamPick();
}

function renderPatients() {
  const box = $("patients");
  if (!patients.length) {
    box.innerHTML = '<div class="empty"><b>暂无患者</b><p>演示数据未加载</p></div>';
    return;
  }
  box.innerHTML = patients.map((p) => {
    const on = p.patient_id === currentPatient;
    const st = p.status === "issued"
      ? '<span class="st done">已开单</span>'
      : '<span class="st wait">待开单</span>';
    const line = p.prescription
      ? "医嘱：" + p.prescription.items.map((c) => (BY_CODE[c] || {}).name || c).join("、")
      : p.chief_complaint;
    return `<button class="patient-card ${on ? "on" : ""}" data-pid="${p.patient_id}">
      <div class="row1">
        <span class="pname">${esc(p.name)}</span>
        <span class="pmeta">${p.age} 岁 · ${esc(p.gender)} · ${esc(p.department)}</span>
        ${st}
      </div>
      <div class="chief">${esc(line)}</div>
    </button>`;
  }).join("");

  box.querySelectorAll("[data-pid]").forEach((el) => {
    el.onclick = () => {
      currentPatient = el.dataset.pid;
      const p = patients.find((x) => x.patient_id === currentPatient);
      picked = new Set(p && p.prescription ? p.prescription.items : []);
      renderPatients();
      renderExamPick();
    };
  });
}

function renderExamPick() {
  const grid = $("examPick");
  const enabled = !!currentPatient;

  grid.innerHTML = (CATALOG.exams || []).map((e) => {
    const on = picked.has(e.code);
    const color = DEPT_COLOR[e.department] || "#8899AA";
    const tags = [];
    if (e.requires_fasting) tags.push('<span class="tag">需空腹</span>');
    if (e.requires && e.requires.length) {
      const names = e.requires.map((r) => (BY_CODE[r.code] || {}).name || r.code).join("、");
      tags.push(`<span class="tag dep">需先做 ${names}</span>`);
    }
    return `<button class="exam ${on ? "on" : ""}" data-code="${e.code}" ${enabled ? "" : "disabled"}>
      <span class="tick"></span>
      <span class="name">${esc(e.name)}</span>
      <span class="meta"><i class="dot" style="background:${color}"></i>${esc(e.department)} · ${e.service_minutes} 分钟</span>
      <span>${tags.join("")}</span>
    </button>`;
  }).join("");

  grid.querySelectorAll("[data-code]").forEach((el) => {
    el.onclick = () => {
      const c = el.dataset.code;
      if (picked.has(c)) picked.delete(c);
      else {
        picked.add(c);
        autoPrereq(c);
      }
      renderExamPick();
    };
  });

  $("pickHint").textContent = enabled ? "勾选本次需要做的检查" : "先在上方选择一位患者";
  $("pickCount").textContent = "已选 " + picked.size + " 项";
  $("issue").disabled = !enabled || picked.size === 0;
}

function autoPrereq(code) {
  const e = BY_CODE[code];
  (e.requires || []).forEach((r) => {
    if (!picked.has(r.code)) {
      picked.add(r.code);
      autoPrereq(r.code);
    }
  });
}

async function issuePrescription() {
  if (!currentPatient || !picked.size) return;
  const btn = $("issue");
  btn.disabled = true;
  btn.textContent = "开单中…";
  try {
    const r = await fetch("api/prescription", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_id: currentPatient, items: [...picked] }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
    toast("已开具检查单，患者端已同步");
    await loadPatients();
    setView("user");
  } catch (e) {
    toast("开单失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开具检查单";
  }
}

async function resetDemo() {
  try {
    await fetch("api/prescription/reset", { method: "POST" });
    currentPatient = null;
    picked.clear();
    await loadPatients();
    showEmpty();
    toast("演示已重置");
  } catch (e) {
    toast("重置失败：" + e.message);
  }
}

/* ==================== 患者端 ==================== */

let rx = null;

function activePatient() {
  const issued = patients.filter((p) => p.status === "issued" && p.prescription);
  return issued.length ? issued[issued.length - 1] : null;
}

async function refreshUser() {
  const p = activePatient();
  if (!p) {
    rx = null;
    showEmpty();
    return;
  }
  rx = p.prescription;
  renderRx();
  await run();
}

function showEmpty() {
  $("emptyPanel").style.display = "block";
  $("rxPanel").style.display = "none";
  $("result").classList.remove("show");
  $("result").innerHTML = "";
}

function renderRx() {
  $("emptyPanel").style.display = "none";
  $("rxPanel").style.display = "block";
  $("rxItems").innerHTML = (rx.items || []).map((c) => {
    const e = BY_CODE[c] || { name: c, department: "—" };
    const color = DEPT_COLOR[e.department] || "#8899AA";
    return `<div class="rx-item">
      <span class="nm">${esc(e.name)}</span>
      <span class="dp"><i class="dot" style="background:${color};margin-right:4px"></i>${esc(e.department)}</span>
    </div>`;
  }).join("");
  $("rxMeta").textContent =
    rx.patient_name + " · " + rx.department + " · " + rx.doctor +
    (rx.created_at ? " · 开单于 " + rx.created_at : "");
}

async function run() {
  if (!rx) return;
  const pid = rx.patient_id;
  try {
    const r = await fetch("api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_id: pid }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
    if (!rx || rx.patient_id !== pid) return;
    renderResult(d);
  } catch (e) {
    $("result").innerHTML = '<div class="alert"><b>生成路线失败</b>' + esc(e.message) + "</div>";
    $("result").classList.add("show");
  }
}

/* ---------- 结果渲染 ---------- */

function segmentsOf(d) {
  const segs = [];
  let cursor = 0;
  d.steps.forEach((s) => {
    const travel = s.travel_minutes || 0;
    if (travel > 0.01) segs.push({ type: "travel", m: travel, label: "移动" });
    const idle = s.arrive_at - cursor - travel;
    if (idle > 0.01) segs.push({ type: "idle", m: idle, label: "等待结果" });
    if (s.wait_minutes > 0.01) segs.push({ type: "wait", m: s.wait_minutes, label: "排队" });
    segs.push({ type: "svc", m: s.service_minutes, label: s.name, dept: s.department });
    cursor = s.finish_at;
  });
  return segs;
}

function renderResult(d) {
  const box = $("result");

  if (!d.feasible) {
    const msg = (d.conflicts || []).map((c) => "<li>" + esc(c) + "</li>").join("");
    box.innerHTML = '<div class="alert"><b>暂时排不出路线</b><ul>' + msg + "</ul></div>";
    box.classList.add("show");
    return;
  }

  const steps = d.steps || [];
  const segs = segmentsOf(d);
  const total = Math.max(0.1, d.total_minutes);
  const travelTotal = steps.reduce((a, s) => a + (s.travel_minutes || 0), 0);
  const serviceTotal = steps.reduce((a, s) => a + s.service_minutes, 0);

  const segHtml = segs.map((s) => {
    const pct = (s.m / total) * 100;
    let style = "flex:0 0 " + pct + "%";
    if (s.type === "svc") {
      style += ";background:" + (DEPT_COLOR[s.dept] || "#8899AA");
    }
    const label = s.type === "svc" ? s.label : (pct >= 7 ? s.label : "");
    return '<div class="seg ' + s.type + '" style="' + style + '" title="' +
      esc(s.label) + " " + Math.round(s.m) + ' 分钟">' +
      (label ? "<span>" + esc(label) + "</span>" : "") + "</div>";
  }).join("");

  const legend = [...new Set(steps.map((s) => s.department))].map((dept) =>
    '<span><i style="background:' + (DEPT_COLOR[dept] || "#8899AA") + '"></i>' +
    esc(dept) + "</span>").join("");

  const stepsHtml = steps.map((s, i) => {
    const bits = [];
    if (s.travel_minutes > 0.01) bits.push("移动 " + Math.round(s.travel_minutes) + " 分");
    if (s.wait_minutes > 0.5) bits.push("排队 " + Math.round(s.wait_minutes) + " 分");
    else bits.push("到点即做");
    bits.push(fmtClock(s.start_at) + "–" + fmtClock(s.finish_at));

    let badge = "";
    if (s.ticket) badge = '<span class="tk-tag">' + s.ticket + " 号</span>";
    else if (s.needs_ticket) badge = '<span class="est-tag">待取号</span>';
    const srcTag = s.wait_source === "ticket" ? "" : '<span class="est-tag">预估</span>';

    return `<li class="step">
      <div class="idx">${i + 1}</div>
      <div class="body">
        <div class="l1"><b>${esc(s.name)}</b><span class="dept">${esc(s.department)}</span>${badge}${srcTag}</div>
        <div class="l2">${fmtClock(s.arrive_at)} 到达 · ${bits.join(" · ")}</div>
        ${s.reason ? '<div class="reason">' + esc(s.reason) + "</div>" : ""}
      </div>
    </li>`;
  }).join("");

  const reminders = d.reminders || [];
  const remindHtml = reminders.length
    ? '<div class="remind"><h3>取号提醒</h3><ul>' +
      reminders.map((r) => "<li>" + esc(r.reason) + "</li>").join("") + "</ul></div>"
    : "";

  const notes = (d.notes || []).length
    ? '<ul class="notes">' + d.notes.map((n) => "<li>" + esc(n) + "</li>").join("") + "</ul>"
    : "";

  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:14px">
      <h2>推荐路线（预估）</h2>
      <span class="spacer"></span>
      <span class="hint">按医嘱生成，时间随队列变化</span>
    </div>

    <div class="stat">
      <div class="big">${Math.round(d.total_minutes)}<small>分钟</small></div>
      <div class="substat">
        其中排队 <b>${Math.round(d.total_wait)}</b> 分钟 ·
        移动 <b>${Math.round(travelTotal)}</b> 分钟 ·
        检查 <b>${Math.round(serviceTotal)}</b> 分钟
      </div>
    </div>

    <div class="timeline">
      <div class="tl-bar">${segHtml}</div>
      <div class="tl-legend">${legend}</div>
    </div>

    <ul class="steps">${stepsHtml}</ul>
    ${remindHtml}
    ${notes}
  `;

  box.classList.add("show");
}

/* ==================== 事件绑定 ==================== */

document.querySelectorAll("#viewTabs button").forEach((b) => {
  b.onclick = () => setView(b.dataset.view);
});

$("issue").onclick = issuePrescription;
$("resetRx").onclick = resetDemo;

$("live").onclick = () => {
  live = !live;
  $("live").classList.toggle("paused", !live);
  $("liveText").textContent = live ? "实时" : "已暂停";
  if (live) pollSnapshot();
};

boot();
