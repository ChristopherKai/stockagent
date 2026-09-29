// stockagent WebUI：少量原生 JS（无第三方库）。CSP 只允许本站脚本，所以不写内联事件处理。
(function () {
  "use strict";
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  // 危险操作二次确认：<form data-confirm="…">
  document.addEventListener("submit", (e) => {
    const msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // 全选：<input type="checkbox" data-select-all="name">
  document.querySelectorAll("[data-select-all]").forEach((box) => {
    box.addEventListener("change", () => {
      document.querySelectorAll(`input[type=checkbox][name="${box.dataset.selectAll}"]`).forEach((b) => { b.checked = box.checked; });
    });
  });

  // 观察清单表单：显示下一行上市地
  document.querySelectorAll("[data-show-row]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("fieldset").querySelector("[data-row].hidden-row");
      if (row) row.classList.remove("hidden-row");
      if (!btn.closest("fieldset").querySelector("[data-row].hidden-row")) btn.remove();
    });
  });

  // 提交任务：显示所选任务的说明，只显示它适用的选项
  const taskSelect = document.querySelector("[data-task-select]");
  if (taskSelect) {
    const form = taskSelect.form;
    const sync = () => {
      const opt = taskSelect.selectedOptions[0];
      const flags = (opt.dataset.flags || "").split(" ").filter(Boolean);
      form.querySelector("[data-task-desc]").textContent = opt.dataset.desc || "";
      form.querySelectorAll("[data-flag]").forEach((el) => {
        const on = flags.includes(el.dataset.flag);
        el.hidden = !on;
        if (!on) el.querySelector("input").checked = false;
      });
      form.querySelector('[data-field="flags"]').hidden = flags.length === 0;
      const companies = opt.dataset.companies;
      form.querySelector('[data-field="companies"]').hidden = companies === "none";
      form.querySelector("[data-companies-hint]").textContent =
        companies === "many" ? "必须选择公司（按住 ⌘ 可多选）" : "不选 = 全部公司（按住 ⌘ 可多选）";
      const profile = form.querySelector('[data-field="profile"]');
      profile.hidden = opt.dataset.profile !== "1";
      profile.querySelector("select").disabled = profile.hidden;
    };
    taskSelect.addEventListener("change", sync);
    sync();
  }

  // LLM 设置：获取模型列表
  document.querySelectorAll("[data-models-url]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const msg = btn.parentElement.querySelector(".models-msg");
      const list = document.getElementById(btn.dataset.target);
      msg.textContent = "正在获取…";
      try {
        const resp = await fetch(btn.dataset.modelsUrl, { headers: { "X-CSRF-Token": csrf() } });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || resp.status);
        list.innerHTML = "";
        data.models.forEach((m) => { const o = document.createElement("option"); o.value = m; list.appendChild(o); });
        msg.textContent = `共 ${data.models.length} 个模型，在输入框里选择或输入`;
      } catch (err) {
        msg.textContent = `获取失败：${err.message}`;
      }
    });
  });

  // 任务日志：运行中时每 2 秒追加新输出
  const log = document.getElementById("joblog");
  if (log) {
    const label = { queued: "排队中", running: "运行中", done: "完成", failed: "失败", cancelled: "已取消", interrupted: "中断" };
    let offset = Number(log.dataset.offset || 0);
    const poll = async () => {
      try {
        const resp = await fetch(`${log.dataset.url}?offset=${offset}`);
        const data = await resp.json();
        if (data.text) {
          const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
          log.textContent += data.text;
          if (atBottom) log.scrollTop = log.scrollHeight;
        }
        offset = data.offset;
        const badge = document.getElementById("jobstatus");
        if (badge) { badge.textContent = label[data.status] || data.status; badge.className = `badge job-${data.status}`; }
        if (data.status !== "queued") document.getElementById("queuednote")?.remove();
        if (data.status === "queued" || data.status === "running") setTimeout(poll, 2000);
      } catch (err) {
        setTimeout(poll, 5000);
      }
    };
    if (log.dataset.status === "queued" || log.dataset.status === "running") setTimeout(poll, 1000);
    log.scrollTop = log.scrollHeight;
  }
})();
