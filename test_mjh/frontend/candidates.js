"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("screening");
const orders = byId("orders");
const exam = byId("exam");
const run = byId("run");
const message = byId("message");
const outcome = byId("outcome");
const optimization = byId("optimization");
const optimizationMode = location.pathname.replace(/\/$/, "") === "/optimize";
if (optimizationMode) {
  document.title = "CP-SAT 检查路线优化";
  byId("page-title").textContent = document.title;
  byId("page-description").textContent = "选择检查项目，用同一份模拟队列快照比较各条路线的预计耗时。";
  byId("switch-page").href = "/";
  byId("switch-page").textContent = "返回候选集";
  run.textContent = "计算最短路线";
}
let prerequisites = [];
let requiredConditions = [];

function orderCodes() {
  return [...new Set(orders.value.trim().toUpperCase().split(/[\s,，、;；]+/).filter(Boolean))];
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function updateRecords() {
  const active = new Set(orderCodes());
  const needed = new Set(requiredConditions.filter((link) => active.has(link.exam_id))
    .map((link) => link.condition_id));
  for (const select of byId("conditions").querySelectorAll("select")) {
    select.parentElement.hidden = !needed.has(select.id);
    select.disabled = !needed.has(select.id);
  }
  byId("condition-panel").hidden = needed.size === 0;
  byId("records").querySelectorAll("label").forEach((label) => {
    label.hidden = !active.has(label.dataset.exam);
    if (label.hidden) label.querySelector("input").checked = false;
  });
  byId("prerequisites").hidden = !prerequisites.some((rule) => active.has(rule.after_exam_id));
  outcome.hidden = true;
  optimization.hidden = true;
  message.textContent = "";
}

function render({ results, edges, sequences, truncated, reason, rejected, rejected_truncated, deferred_count }) {
  const names = new Map(results.map((result) => [result.exam_id, result.name]));
  const summary = reason || (
    truncated ? `仅显示前 ${sequences.length} 条候选顺序。` : `共 ${sequences.length} 条候选顺序。`
  );
  byId("sequence-summary").textContent = deferred_count && sequences.length
    ? `已暂缓 ${deferred_count} 项，以下顺序仅包含剩余项目。${summary}` : summary;
  const dependencyList = byId("dependencies");
  dependencyList.replaceChildren();
  for (const edge of edges) {
    const item = document.createElement("li");
    const gap = edge.recommended_gap_minutes;
    const timing = Number.isInteger(gap) ? `建议间隔 ${gap} 分钟（${names.get(edge.before_exam_id)} 结束至 ${names.get(edge.after_exam_id)} 开始）。` : "";
    item.textContent = `${edge.rule_id} 要求 ${names.get(edge.before_exam_id)} 先于 ${names.get(edge.after_exam_id)}，逆序会被排除。${timing}`;
    if (edge.note) {
      const note = document.createElement("p");
      note.textContent = edge.note;
      item.append(note);
    }
    dependencyList.append(item);
  }
  const sequenceList = byId("sequences");
  sequenceList.replaceChildren();
  for (const sequence of sequences) {
    const item = document.createElement("li");
    item.textContent = sequence.map((code) => names.get(code)).join(" → ");
    sequenceList.append(item);
  }
  byId("rejected-summary").textContent = rejected_truncated
    ? `仅显示 ${rejected.length} 条被排除的顺序。`
    : `共 ${rejected.length} 条顺序在当前条件下被排除。`;
  const rejectedList = byId("rejected-sequences");
  rejectedList.replaceChildren();
  for (const entry of rejected) {
    const item = document.createElement("li");
    const order = document.createElement("strong");
    order.textContent = entry.order.map((code) => names.get(code)).join(" → ");
    const reasons = document.createElement("ul");
    for (const reason of entry.reasons) {
      const detail = document.createElement("li");
      detail.textContent = reason;
      reasons.append(detail);
    }
    item.append(order, reasons);
    rejectedList.append(item);
  }
  const container = byId("results");
  container.replaceChildren();
  for (const status of ["当前可选", "暂不可选", "待确认"]) {
    const entries = results.filter((result) => result.status === status);
    const group = document.createElement("div");
    group.className = "group";
    const title = document.createElement("h3");
    title.textContent = `${status === "当前可选" ? "条件通过" : status}（${entries.length}）`;
    const list = document.createElement("ul");
    for (const result of entries) {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = `${result.exam_id} ${result.name}`;
      item.append(name);
      for (const reason of result.reasons) {
        const detail = document.createElement("p");
        detail.textContent = reason;
        item.append(detail);
      }
      for (const suggestion of result.suggestions) {
        const detail = document.createElement("p");
        detail.textContent = `建议 · ${suggestion}`;
        item.append(detail);
      }
      list.append(item);
    }
    group.append(title, list);
    container.append(group);
  }
  outcome.hidden = false;
  message.textContent = `已评估 ${results.length} 项医嘱`;
}

function formatTime(seconds) {
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function appendTimeline(container, route) {
  const wrapper = document.createElement("div");
  wrapper.className = "timeline";
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const label of ["检查", "科室", "移动", "到达", "等待", "开始", "结束"]) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    head.append(cell);
  }
  const body = table.createTBody();
  for (const step of route.steps) {
    const row = body.insertRow();
    const values = [step.name, step.department, ...["travel", "arrival", "wait", "start", "finish"]
      .map((field) => formatTime(step[field]))];
    for (const value of values) row.insertCell().textContent = value;
  }
  wrapper.append(table);
  container.append(wrapper);
}

function routeLabel(route) {
  return route.steps.map((step) => step.name).join(" → ");
}

function renderOptimization({ optimization: data, reason, truncated }) {
  const best = byId("optimal-route");
  const routes = byId("timed-routes");
  best.replaceChildren();
  routes.replaceChildren();
  byId("snapshot-label").textContent = data
    ? `快照 ${data.snapshot_time.replace("T", " ")} · 到达、开始和结束均从快照时刻计` : "";
  const summary = document.createElement("strong");
  if (data?.route) {
    const label = data.status === "OPTIMAL" ? "最短预计耗时" : "当前可行解预计耗时";
    summary.textContent = `${label} ${formatTime(data.route.total_seconds)}`;
    const path = document.createElement("p");
    path.textContent = routeLabel(data.route);
    best.append(summary, path);
    if (data.status !== "OPTIMAL") {
      const status = document.createElement("p");
      status.textContent = "求解时限内尚未证明此路线最优。";
      best.append(status);
    }
    appendTimeline(best, data.route);
  } else {
    summary.textContent = data ? `本次未返回路线（${data.status}）` : reason;
    best.append(summary);
  }
  byId("enumerated-title").hidden = !data?.routes.length;
  if (truncated) {
    const note = document.createElement("p");
    note.textContent = "这里只展示已枚举的前 100 条路线，CP-SAT 按全部可安排项目和先后规则求解。";
    routes.append(note);
  }
  for (const route of data?.routes || []) {
    const detail = document.createElement("details");
    const label = document.createElement("summary");
    label.textContent = `${formatTime(route.total_seconds)} · ${routeLabel(route)}`;
    detail.append(label);
    appendTimeline(detail, route);
    routes.append(detail);
  }
  optimization.hidden = false;
}

async function load() {
  try {
    const catalog = await request("/api/candidates/catalog");
    prerequisites = catalog.prerequisites;
    requiredConditions = catalog.required_conditions;
    for (const item of catalog.exams) {
      exam.add(new Option(`${item.exam_id} ${item.name}`, item.exam_id));
    }
    for (const condition of catalog.conditions) {
      const wrapper = document.createElement("div");
      const label = document.createElement("label");
      const select = document.createElement("select");
      label.htmlFor = select.id = condition.condition_id;
      label.textContent = condition.name;
      select.title = condition.assessment;
      select.add(new Option("待确认", ""));
      select.add(new Option("已满足", "true"));
      select.add(new Option("未满足", "false"));
      wrapper.append(label, select);
      byId("conditions").append(wrapper);
    }
    for (const rule of prerequisites) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = rule.rule_id;
      label.dataset.exam = rule.after_exam_id;
      label.append(checkbox, ` ${rule.rule_id} ${rule.before_node}（${rule.timing}）`);
      byId("records").append(label);
    }
    updateRecords();
    run.disabled = false;
  } catch (error) {
    message.textContent = error.message;
  }
}

orders.addEventListener("input", updateRecords);
byId("add").addEventListener("click", () => {
  if (!exam.value) return;
  orders.value = [...new Set([...orderCodes(), exam.value])].join(", ");
  updateRecords();
});
byId("example").addEventListener("click", () => {
  orders.value = optimizationMode ? "EX01, EX06, EX05, EX18" : "EX01, EX06, EX05";
  byId("conditions").querySelectorAll("select").forEach((select) => {
    select.value = select.id === "S01" || (optimizationMode && select.id === "S02") ? "true" : "";
  });
  byId("records").querySelectorAll("input").forEach((input) => { input.checked = false; });
  updateRecords();
});
form.addEventListener("change", () => {
  outcome.hidden = true;
  optimization.hidden = true;
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  outcome.hidden = true;
  optimization.hidden = true;
  const states = {};
  for (const select of byId("conditions").querySelectorAll("select")) {
    if (!select.disabled && select.value) states[select.id] = select.value === "true";
  }
  const confirmed = Array.from(byId("records").querySelectorAll("input:checked"), (input) => input.value);
  const input = { orders: orderCodes(), states, confirmed };
  const controls = Array.from(form.elements).filter((control) => !control.disabled);
  controls.forEach((control) => { control.disabled = true; });
  message.textContent = "正在核对规则…";
  try {
    const data = await request(optimizationMode ? "/api/optimize" : "/api/candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    render(data);
    if (optimizationMode) renderOptimization(data);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    controls.forEach((control) => { control.disabled = false; });
  }
});

load();
