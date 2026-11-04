/* 门诊检查路线规划 · 前端逻辑
 *
 * 路由：
 *   /                患者端（挂号 → 就诊 → 路线）
 *   /doctor          医生端科室列表
 *   /doctor/<code>   某科室工作台（只显示本科室患者）
 */

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
let tickCount = 0;

let DEPTS = [];                       // 挂号科室（含待诊人数）
let route = { view: "patient" };

/* ---------------- 患者端状态 ---------------- */
const LS_KEY = "exam_patient_id";
let myId = localStorage.getItem(LS_KEY) || "";
let myPatient = null;                 // 当前挂号记录
let rx = null;                        // 医嘱
let regDept = "";                     // 表单里选中的科室
let planSig = "";                     // 上次渲染的方案指纹，避免无谓重绘

/* ---------------- 医生端状态 ---------------- */
let doctorDept = null;
let patients = [];
let currentPatient = null;
let picked = new Set();

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

async function api(path, options) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.error) || ("HTTP " + res.status));
    err.status = res.status;
    throw err;
  }
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/* ==================== 路由 ==================== */

function parseRoute() {
  const raw = location.pathname.replace(/\/+$/, "");
  const p = raw === "" ? "/" : raw;
  if (p === "/" || p === "/patient") return { view: "patient" };
  if (p === "/doctor") return { view: "doctor", dept: "" };
  const m = p.match(/^\/doctor\/([\w-]+)$/);
  if (m) return { view: "doctor", dept: m[1] };
  return { view: "notfound" };
}

async function navigate(path) {
  if (path !== location.pathname) history.pushState({}, "", path);
  await renderRoute();
}

window.addEventListener("popstate", () => renderRoute());

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a) return;
  const href = a.getAttribute("href");
  if (!href || !href.startsWith("/") || a.target === "_blank") return;
  e.preventDefault();
  navigate(href);
});

async function renderRoute() {
  route = parseRoute();
  $("viewPatient").hidden = route.view !== "patient";
  $("viewDoctor").hidden = route.view !== "doctor";
  $("viewNotFound").hidden = route.view !== "notfound";

  document.querySelectorAll("#viewTabs a").forEach((a) => {
    const isUserTab = a.dataset.view === "user";
    a.classList.toggle("on", isUserTab ? route.view === "patient" : route.view === "doctor");
  });

  if (route.view === "patient") {
    document.title = "患者端 · 门诊检查路线规划";
    await renderPatient();
  } else if (route.view === "doctor") {
    document.title = "医生端 · 门诊检查路线规划";
    await renderDoctor();
  } else {
    document.title = "页面不存在";
  }
}

/* ==================== 数据加载 ==================== */

async function loadDepartments() {
  try {
    const d = await api("/api/departments", { cache: "no-store" });
    DEPTS = d.departments || [];
  } catch (e) {
    DEPTS = [];
  }
}

async function boot() {
  try {
    const r = await fetch("/api/catalog");
    CATALOG = await r.json();
  } catch (e) {
    toast("读取配置失败：" + e.message);
    return;
  }
  (CATALOG.exams || []).forEach((e) => (BY_CODE[e.code] = e));
  BASE_DATE = parseBase(CATALOG.snapshot_time);
  refreshSubtle();

  await loadDepartments();
  await renderRoute();
  poll();
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

/* ==================== 轮询 ==================== */

async function poll() {
  clearTimeout(pollTimer);
  tickCount += 1;

  if (live) {
    try {
      const d = await api("/api/snapshot", { cache: "no-store" });
      renderDepts(d.departments);
      updateDeptHint(d);
    } catch (e) { /* 下一轮再试 */ }

    if (route.view === "patient" && myId) {
      await refreshMyStatus();
    }
    if (route.view === "doctor" && route.dept) {
      // 医生端低频刷新，能看到新挂号的患者
      if (tickCount % 2 === 0) await loadDeptPatients(true);
    }
  }

  pollTimer = setTimeout(poll, 3000);
}

function renderDepts(list) {
  const box = $("depts");
  if (!box || !list || !list.length) return;
  const maxWait = Math.max(1, ...list.map((d) => d.wait_minutes));
  const sig = list.map((d) => d.name).join("|");

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
  if (!el) return;
  if (s.ok) el.textContent = "来自叫号系统 · 更新于 " + (s.fetched_at || "—");
  else el.textContent = "数据源异常，显示上次快照" +
    (s.age_seconds != null ? "（" + s.age_seconds + " 秒前）" : "");
}

/* ==================== 患者端：挂号 ==================== */

async function renderPatient() {
  $("deptPanel").hidden = false;
  document.body.dataset.view = "patient";

  if (!DEPTS.length) await loadDepartments();
  renderRegDepts();

  if (!myId) {
    showReg();
    return;
  }
  try {
    const d = await api("/api/patient/" + encodeURIComponent(myId), { cache: "no-store" });
    myPatient = d.patient;
  } catch (e) {
    if (e.status === 404) {
      myId = "";
      localStorage.removeItem(LS_KEY);
      showReg();
      return;
    }
    toast("读取就诊记录失败：" + e.message);
    showReg();
    return;
  }
  renderVisit();
}

function showReg() {
  $("regPanel").hidden = false;
  $("visitPanel").hidden = true;
  $("rxPanel").hidden = true;
  $("result").classList.remove("show");
  $("result").innerHTML = "";
  rx = null;
  planSig = "";
  updateRegButton();
}

function renderRegDepts() {
  const box = $("regDepts");
  if (!box) return;
  if (!DEPTS.length) {
    box.innerHTML = '<div class="empty">科室信息加载失败</div>';
    return;
  }
  box.innerHTML = DEPTS.map((d) => {
    const color = DEPT_COLOR[d.name] || "#8899AA";
    const on = d.code === regDept;
    return `<button class="dept-pick ${on ? "on" : ""}" data-dept="${d.code}">
      <div class="dn"><i class="dot" style="background:${color}"></i>${esc(d.name)}
        ${d.waiting ? `<span class="badge">待诊 ${d.waiting}</span>` : ""}</div>
      <div class="dd">${esc(d.desc)}</div>
      <div class="dm"><span>检查队列 <b>${Math.round(d.wait_minutes)}</b> 分</span>
        <span>排队 <b>${d.queue_length}</b> 人</span></div>
    </button>`;
  }).join("");

  box.querySelectorAll("[data-dept]").forEach((el) => {
    el.onclick = () => {
      regDept = el.dataset.dept;
      renderRegDepts();
      updateRegButton();
    };
  });
}

function updateRegButton() {
  const btn = $("regSubmit");
  if (!btn) return;
  btn.disabled = !regDept;
  btn.textContent = regDept ? "确认挂号" : "请先选择科室";
}

async function submitRegister() {
  const btn = $("regSubmit");
  const name = $("regName").value.trim();
  if (!regDept) { toast("请先选择科室"); return; }
  if (!name) { toast("请填写姓名"); $("regName").focus(); return; }

  btn.disabled = true;
  btn.textContent = "挂号中…";
  try {
    const d = await postJSON("/api/register", {
      name: name,
      age: Number($("regAge").value) || 0,
      gender: $("regGender").value,
      chief_complaint: $("regChief").value.trim(),
      department: regDept,
    });
    myId = d.patient.patient_id;
    myPatient = d.patient;
    localStorage.setItem(LS_KEY, myId);
    toast("挂号成功，已进入 " + d.patient.department + " 队列");
    renderVisit();
    await loadDepartments();
  } catch (e) {
    toast("挂号失败：" + e.message);
  } finally {
    updateRegButton();
  }
}

/* ==================== 患者端：就诊与医嘱 ==================== */

function renderVisit() {
  if (!myPatient) { showReg(); return; }
  $("regPanel").hidden = true;
  $("visitPanel").hidden = false;

  const issued = myPatient.status === "issued" && myPatient.prescription;
  $("visitMeta").textContent = "挂号科室：" + myPatient.department;

  $("visitCard").innerHTML = `
    <div class="visit-no">${esc(myPatient.visit_no || "—")}<small>就诊号</small></div>
    <div class="visit-info">
      <b>${esc(myPatient.name)}</b> · ${myPatient.age} 岁 · ${esc(myPatient.gender)}<br>
      ${esc(myPatient.department)} · 主诉：${esc(myPatient.chief_complaint)}
    </div>
    <div class="visit-state">
      <span class="pill ${issued ? "done" : ""}">${issued ? "已开单" : "待医生开单"}</span>
    </div>`;

  $("visitWaiting").hidden = !!issued;

  if (issued) {
    rx = myPatient.prescription;
    renderRx();
    run();
  } else {
    rx = null;
    planSig = "";
    $("rxPanel").hidden = true;
    $("result").classList.remove("show");
    $("result").innerHTML = "";
  }
}

async function refreshMyStatus() {
  let d;
  try {
    d = await api("/api/patient/" + encodeURIComponent(myId), { cache: "no-store" });
  } catch (e) {
    return;
  }
  const p = d.patient;
  const before = myPatient ? myPatient.status : "";
  const hadRx = !!(myPatient && myPatient.prescription);
  myPatient = p;

  if (!hadRx && p.prescription) {
    renderVisit();
    toast("医生已开具检查单");
    return;
  }
  if (before !== p.status) {
    renderVisit();
    return;
  }
  // 已开单：队列在变，隔一次轮询重算，避免频繁重绘
  if (p.prescription && rx && tickCount % 2 === 0) run();
}

function renderRx() {
  $("rxPanel").hidden = false;
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
    const d = await postJSON("/api/recommend", { patient_id: pid });
    if (!rx || rx.patient_id !== pid) return;
    const sig = (d.feasible ? (d.order || []).join("|") : "x") + "@" + Math.round(d.total_minutes);
    if (sig === planSig) return;          // 方案没变就不重绘，避免每 3 秒闪一下
    planSig = sig;
    renderResult(d);
  } catch (e) {
    $("result").innerHTML = '<div class="alert"><b>生成路线失败</b>' + esc(e.message) + "</div>";
    $("result").classList.add("show");
  }
}

/* ==================== 医生端 ==================== */

async function renderDoctor() {
  document.body.dataset.view = "doctor";

  if (!DEPTS.length) await loadDepartments();

  if (!route.dept) {
    doctorDept = null;
    currentPatient = null;
    picked.clear();
    $("doctorHome").hidden = false;
    $("doctorDept").hidden = true;
    renderDoctorDepts();
    return;
  }

  const cfg = DEPTS.find((d) => d.code === route.dept);
  if (!cfg) {
    $("viewDoctor").hidden = true;
    $("viewNotFound").hidden = false;
    return;
  }

  doctorDept = cfg;
  $("doctorHome").hidden = true;
  $("doctorDept").hidden = false;
  $("crumbName").textContent = cfg.name;
  $("crumbHint").textContent = cfg.desc;
  document.title = cfg.name + " · 医生端";

  await loadDeptPatients();
}

function renderDoctorDepts() {
  const box = $("doctorDepts");
  box.innerHTML = DEPTS.map((d) => {
    const color = DEPT_COLOR[d.name] || "#8899AA";
    const waiting = d.waiting || 0;
    return `<a class="dept-pick" href="/doctor/${d.code}">
      <div class="dn"><i class="dot" style="background:${color}"></i>${esc(d.name)}
        <span class="badge">${waiting ? "待诊 " + waiting : "暂无待诊"}</span></div>
      <div class="dd">${esc(d.desc)}</div>
      <div class="dm"><span>本科室可开 <b>${(d.exams || []).length}</b> 项</span>
        <span>检查队列 <b>${Math.round(d.wait_minutes)}</b> 分</span></div>
    </a>`;
  }).join("");
}

async function loadDeptPatients(quiet) {
  if (!doctorDept) return;
  try {
    const d = await api("/api/doctor/" + doctorDept.code + "/patients", { cache: "no-store" });
    patients = d.patients || [];
  } catch (e) {
    if (!quiet) toast("读取患者失败：" + e.message);
    patients = [];
  }
  if (currentPatient && !patients.some((p) => p.patient_id === currentPatient)) {
    currentPatient = null;
    picked.clear();
  }
  renderPatients();
  renderExamPick();
  $("patientsTitle").textContent = "待诊患者（" + patients.filter((p) => p.status !== "issued").length + "）";
}

function renderPatients() {
  const box = $("patients");
  if (!patients.length) {
    box.innerHTML = '<div class="empty"><b>本科室暂无待诊患者</b>' +
      '<p>患者在患者端挂「' + esc(doctorDept ? doctorDept.name : "") + '」后，才会出现在这里。</p></div>';
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
        <span class="pmeta">${p.visit_no || ""} · ${p.age} 岁 · ${esc(p.gender)}</span>
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
  const own = doctorDept ? (doctorDept.exams || []) : [];

  // 本科室能开的项目排前面，方便医生快速定位
  const exams = (CATALOG.exams || []).slice().sort((a, b) => {
    const ao = own.includes(a.code) ? 0 : 1;
    const bo = own.includes(b.code) ? 0 : 1;
    if (ao !== bo) return ao - bo;
    return 0;
  });

  grid.innerHTML = exams.map((e) => {
    const on = picked.has(e.code);
    const color = DEPT_COLOR[e.department] || "#8899AA";
    const isOwn = own.includes(e.code);
    const tags = [];
    if (isOwn) tags.push('<span class="own-tag">本科室</span>');
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
      else { picked.add(c); autoPrereq(c); }
      renderExamPick();
    };
  });

  $("pickHint").textContent = !enabled
    ? "先在上方选择一位患者"
    : (doctorDept ? "本科室：" + doctorDept.name + "，也可开其他科室项目" : "勾选本次需要做的检查");
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
  if (!currentPatient || !picked.size || !doctorDept) return;
  const btn = $("issue");
  btn.disabled = true;
  btn.textContent = "开单中…";
  try {
    await postJSON("/api/prescription", {
      patient_id: currentPatient,
      items: [...picked],
      department: doctorDept.code,        // 服务端校验归属，跨科室会被拒
      doctor: doctorDept.name + "医生",
    });
    toast("已开具检查单，患者端已同步");
    await loadDeptPatients();
  } catch (e) {
    toast("开单失败：" + e.message);
  } finally {
    btn.textContent = "开具检查单";
    btn.disabled = !picked.size;
  }
}

async function resetDemo() {
  try {
    await postJSON("/api/prescription/reset");
    currentPatient = null;
    picked.clear();
    myId = "";
    myPatient = null;
    rx = null;
    localStorage.removeItem(LS_KEY);
    await loadDepartments();
    if (route.dept) await loadDeptPatients();
    else renderDoctorDepts();
    toast("演示已重置");
  } catch (e) {
    toast("重置失败：" + e.message);
  }
}

/* ==================== 结果渲染 ==================== */

function segmentsOf(d) {
  const segs = [];
  let cursor = 0;
  d.steps.forEach((s, i) => {
    const travel = s.travel_minutes || 0;
    if (travel > 0.01) segs.push({ type: "travel", m: travel, label: "移动" });
    const idle = s.arrive_at - cursor - travel;
    if (idle > 0.01) segs.push({ type: "idle", m: idle, label: "等待结果" });
    if (s.wait_minutes > 0.01) segs.push({ type: "wait", m: s.wait_minutes, label: "排队" });
    segs.push({ type: "svc", m: s.service_minutes, label: s.name, dept: s.department, idx: i + 1 });
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
    // 每个检查段都带步骤序号：
    //   够宽  → 「1.肺功能」，与下方列表的编号对得上
    //   放不下 → 只留序号「2」，避免截断成「2.肺…」
    let label = "";
    if (s.type === "svc") {
      if (s.idx) label = pct >= 9 ? s.idx + "." + s.label : String(s.idx);
    } else {
      label = pct >= 7 ? s.label : "";
    }
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
    // 已标「待取号」时不再重复标「预估」，两者说的是同一件事
    const srcTag = (s.wait_source === "ticket" || s.needs_ticket)
      ? "" : '<span class="est-tag">预估</span>';

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
    ${remindHtml}
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
    ${notes}
  `;

  box.classList.add("show");
}

/* ==================== 事件绑定 ==================== */

$("regSubmit").onclick = submitRegister;
$("regName").addEventListener("keydown", (e) => { if (e.key === "Enter") submitRegister(); });
$("regChief").addEventListener("keydown", (e) => { if (e.key === "Enter") submitRegister(); });

$("regAgain").onclick = () => {
  myId = "";
  myPatient = null;
  rx = null;
  localStorage.removeItem(LS_KEY);
  regDept = "";
  showReg();
  toast("已退出当前就诊，可重新挂号");
};

$("issue").onclick = issuePrescription;
$("resetRx").onclick = resetDemo;

$("live").onclick = () => {
  live = !live;
  $("live").classList.toggle("paused", !live);
  $("liveText").textContent = live ? "实时" : "已暂停";
  if (live) poll();
};

boot();
