"use strict";

const status = document.getElementById("queue-status");
const departments = document.getElementById("queue-departments");
const selected = new URLSearchParams(location.search).get("department");
const controls = document.getElementById("queue-controls");
const callButton = document.getElementById("call-next");
const feedback = document.getElementById("call-feedback");
let calling = false;
let revision = 0;

async function refreshQueues() {
  const currentRevision = revision;
  try {
    const response = await fetch("/api/queues", { cache: "no-store" });
    if (!response.ok) throw new Error("读取失败");
    const snapshot = await response.json();
    if (currentRevision !== revision) return;
    const visible = snapshot.departments.filter((department) => !selected || selected === department.department);
    controls.hidden = !selected || !visible.length;
    callButton.disabled = calling || !visible[0]?.queue_length;
    departments.replaceChildren(...visible.map((department) => {
      const card = document.createElement("section");
      const title = document.createElement("h2");
      const count = document.createElement("p");
      const details = document.createElement("p");
      if (selected) {
        title.textContent = department.department;
      } else {
        const link = document.createElement("a");
        link.href = `/queues?department=${encodeURIComponent(department.department)}`;
        link.textContent = department.department;
        title.append(link);
      }
      count.className = "queue-count";
      count.textContent = `${department.queue_length} 人排队`;
      const seconds = department.next_call_in_seconds;
      const countdown = Number.isFinite(seconds)
        ? `距下次自动叫号约 ${Math.ceil(seconds)} 秒`
        : "倒计时数据暂不可用";
      details.textContent = `预计等待 ${department.wait_minutes} 分钟 · 当前叫到 ${department.serving_no} 号 · ${countdown}`;
      card.append(title, count, details);
      return card;
    }));
    status.textContent = `模拟快照 ${snapshot.snapshot_time.replace("T", " ")} · 每 2 秒刷新`;
    if (!visible.length) status.textContent = "找不到所选科室。";
  } catch {
    status.textContent = "暂时无法读取模拟队列，正在重试。";
  }
}

async function pollQueues() {
  await refreshQueues();
  setTimeout(pollQueues, 2000);
}

if (selected) {
  const back = document.querySelector("header a");
  back.href = "/queues";
  back.textContent = "返回科室列表";
}

callButton.addEventListener("click", async () => {
  if (calling) return;
  calling = true;
  callButton.disabled = true;
  revision++;
  feedback.textContent = "正在叫号…";
  try {
    const response = await fetch("/api/queues/call-next", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ department: selected }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "叫号失败。");
    feedback.textContent = `已叫到 ${result.serving_no} 号，剩余 ${result.queue_length} 人排队。`;
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    calling = false;
    revision++;
    await refreshQueues();
  }
});

pollQueues();
