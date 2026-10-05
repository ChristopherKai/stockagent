// stockagent WebUI：少量原生 JS（无第三方库）。CSP 只允许本站脚本，所以不写内联事件处理。
(function () {
  "use strict";
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  // 危险操作二次确认：<form data-confirm="…">
  document.addEventListener("submit", (e) => {
    const msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // 页内锚点（如修正页的依据编号）：展开目标所在的折叠块，再滚动过去并闪一下
  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#"]');
    const target = a && a.getAttribute("href").length > 1 && document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1)));
    if (!target) return;
    e.preventDefault();
    for (let d = target; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
    target.scrollIntoView({ behavior: "smooth", block: target.matches("h1, h2, h3, h4") ? "start" : "center" });
    target.classList.remove("target-hit");
    void target.offsetWidth;
    target.classList.add("target-hit");
    history.replaceState(null, "", a.getAttribute("href"));
  });

  // 选择后立即提交：<select data-autosubmit>
  document.querySelectorAll("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));

  // 全选：<input type="checkbox" data-select-all="name">
  document.querySelectorAll("[data-select-all]").forEach((box) => {
    box.addEventListener("change", () => {
      document.querySelectorAll(`input[type=checkbox][name="${box.dataset.selectAll}"]`).forEach((b) => { b.checked = box.checked && !b.closest("tr")?.hidden && !b.closest("tbody")?.hidden; });
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

  // 列表筛选（仪表盘、观察清单）：<div data-filter-bar="#表格">；每家公司一个 tbody[data-group]，每个上市地一行 tr[data-market][data-stance]。
  // 隐藏的行不占位；每家公司第一条可见的行标 .lead（公司级的单元格只在它里面显示）。
  document.querySelectorAll("[data-filter-bar]").forEach((bar) => {
    const table = document.querySelector(bar.dataset.filterBar);
    if (!table) return;
    const groups = [...table.querySelectorAll("tbody[data-group]")];
    const ctl = {};
    bar.querySelectorAll("[data-f]").forEach((c) => { ctl[c.dataset.f] = c; });
    const count = bar.querySelector("[data-filter-count]");
    const empty = document.querySelector("[data-filter-empty]");
    const KEY = `stockagent.filter.${location.pathname}`;
    const value = (c) => (c ? (c.type === "checkbox" ? (c.checked ? "1" : "") : c.value.trim().toLowerCase()) : "");
    const save = () => {
      try { localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(Object.entries(ctl).map(([k, c]) => [k, c.type === "checkbox" ? c.checked : c.value])))); } catch { /* 不记忆 */ }
    };
    const apply = () => {
      const q = value(ctl.q), market = ctl.market?.value || "", stance = ctl.stance?.value || "";
      const tracked = ctl.tracked?.value || "", pinned = value(ctl.pinned);
      // 排序：置顶永远在前；默认（服务端给的）顺序 = 情景立场 → 空间 → 多空比
      const mode = ctl.sort?.value || "";
      table.querySelectorAll("th.sortable").forEach((th) => th.classList.toggle("sorted", th.dataset.sort === mode));
      const num = (g, k) => Number(g.dataset[k]) || 0;
      const by = {
        "": (a, b) => num(a, "kStance") - num(b, "kStance") || num(b, "kUpside") - num(a, "kUpside") || num(b, "kRatio") - num(a, "kRatio"),
        upside: (a, b) => (num(a, "kStance") === 9) - (num(b, "kStance") === 9) || num(b, "kUpside") - num(a, "kUpside"),
        ratio: (a, b) => (num(a, "kStance") === 9) - (num(b, "kStance") === 9) || num(b, "kRatio") - num(a, "kRatio"),
        date: (a, b) => (b.dataset.kDate || "").localeCompare(a.dataset.kDate || ""),
      }[mode];
      [...groups].sort((a, b) => (b.dataset.pinned === "1") - (a.dataset.pinned === "1") || by(a, b) || num(a, "i") - num(b, "i"))
        .forEach((g) => table.appendChild(g));
      let shown = 0;
      groups.forEach((g) => {
        const gOk = (!q || g.dataset.text.includes(q)) && (!tracked || g.dataset.tracked === tracked) && (!pinned || g.dataset.pinned === "1");
        let first = true, any = false;
        g.querySelectorAll("tr").forEach((r) => {
          const ok = gOk && (!market || r.dataset.market === market) && (!stance || r.dataset.stance === stance);
          r.hidden = !ok;
          r.classList.toggle("lead", ok && first);
          if (ok) { first = false; any = true; }
        });
        g.hidden = !any;
        if (any) shown += 1;
      });
      if (count) count.textContent = `${shown} / ${groups.length} 家`;
      if (empty) empty.hidden = shown > 0;
    };
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      Object.entries(saved).forEach(([k, v]) => { const c = ctl[k]; if (!c) return; if (c.type === "checkbox") c.checked = !!v; else c.value = v; });
    } catch { /* 不记忆 */ }
    Object.values(ctl).forEach((c) => c.addEventListener(c.tagName === "INPUT" && c.type !== "checkbox" ? "input" : "change", () => { apply(); save(); }));
    table.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
      if (!ctl.sort) return;
      ctl.sort.value = th.dataset.sort === ctl.sort.value ? "" : th.dataset.sort; // 再点一次回到默认
      apply(); save();
    }));
    bar.querySelector("[data-filter-reset]")?.addEventListener("click", () => {
      Object.values(ctl).forEach((c) => { if (c.type === "checkbox") c.checked = false; else c.value = ""; });
      apply(); save();
    });
    apply();
  });
})();


// 报告研读对话：报告页右侧栏（可全屏；窄屏为全屏弹层）。
// 回答过程用 SSE 实时接收：思考、工具调用与结果、正文片段一律以纯文本（textContent）显示；
// 结束后换成服务端净化过的 Markdown HTML，并附引文核对与数字溯源结果。
(function () {
  "use strict";
  const root = document.querySelector("[data-chat]");
  if (!root) return;
  const $ = (sel) => root.querySelector(sel);
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
  const layout = document.querySelector("[data-chat-layout]");
  const log = $("[data-chat-log]");
  const empty = $("[data-chat-empty]");
  const select = $("[data-chat-select]");
  const input = $("[data-chat-input]");
  const send = $("[data-chat-send]");
  const del = $("[data-chat-delete]");
  const fullBtn = $("[data-chat-full]");
  const webBox = $("[data-chat-web]");
  const quoteBox = $("[data-chat-quote]");
  const report = document.querySelector("[data-report-body]");
  const askBtn = document.querySelector("[data-chat-ask]");
  const narrow = window.matchMedia("(max-width: 900px)");
  const unavailable = input.disabled;
  const KEY_OFF = "stockagent.chat.off";
  const KEY_WEB = "stockagent.chat.web";
  let chatId = null;
  let quote = "";
  let busy = false;
  let source = null; // 当前的 EventSource
  let timer = null; // 轮询（事件流不可用时）

  const load = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 隐私模式等：不记忆 */ } };

  // ---------------------------------------------------------------- 展开、收起、全屏
  if (load(KEY_OFF) === "1") layout.classList.add("chat-off");
  const isFull = () => layout.classList.contains("chat-full");
  const isOpen = () => isFull() || (narrow.matches ? layout.classList.contains("chat-open") : !layout.classList.contains("chat-off"));
  const setFull = (on) => {
    layout.classList.toggle("chat-full", on);
    document.body.classList.toggle("chat-lock", on);
    fullBtn.textContent = on ? "⤡" : "⤢";
    fullBtn.title = on ? "退出全屏（Esc）" : "全屏（Esc 退出）";
    fullBtn.setAttribute("aria-pressed", String(on));
  };
  const open = () => {
    if (narrow.matches) layout.classList.add("chat-open");
    else { layout.classList.remove("chat-off"); save(KEY_OFF, "0"); }
  };
  const close = () => {
    setFull(false);
    if (narrow.matches) layout.classList.remove("chat-open");
    else { layout.classList.add("chat-off"); save(KEY_OFF, "1"); }
  };
  document.querySelectorAll("[data-chat-toggle]").forEach((b) => b.addEventListener("click", () => {
    if (isOpen()) close();
    else { open(); input.focus(); }
  }));
  $("[data-chat-close]").addEventListener("click", close);
  fullBtn.addEventListener("click", () => { if (!isFull()) open(); setFull(!isFull()); input.focus(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isFull()) setFull(false); });

  const web = load(KEY_WEB);
  if (web !== null) webBox.checked = web === "1";
  webBox.addEventListener("change", () => save(KEY_WEB, webBox.checked ? "1" : "0"));

  // ---------------------------------------------------------------- 工具函数
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const short = (s, n = 60) => {
    s = s.replace(/[*_`]+/g, ""); // 引文里的 Markdown 符号不展示
    return s.length > n ? `${s.slice(0, n)}…` : s;
  };
  const nearBottom = () => log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  const scrollDown = () => { log.scrollTop = log.scrollHeight; };

  async function api(url, body) {
    const opts = { headers: { "X-CSRF-Token": csrf() } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const text = await resp.text();
    let data = {};
    try { data = JSON.parse(text); } catch { /* 错误页是纯文本 */ }
    if (!resp.ok) throw new Error(data.error || text || `HTTP ${resp.status}`);
    return data;
  }

  // 在报告正文中定位引文：找到包含引文开头的最内层段落，滚动过去并高亮
  const norm = (s) => s.replace(/[\s*_`#>|~\\\-“”‘’『』「」"']+/g, "");
  function locate(text) {
    if (!report) return;
    const probe = text.split(/……|…|\.{3,}/).map(norm).find(Boolean) || "";
    const blocks = [...report.querySelectorAll("p, li, td, th, blockquote, h1, h2, h3, h4, h5, h6, pre, .ev-title, .ev-note, .ev-more div, .ev-row div")];
    for (const len of [probe.length, 24, 12]) {
      const key = probe.slice(0, len);
      const hits = key ? blocks.filter((b) => norm(b.textContent).includes(key)) : [];
      const hit = hits.sort((a, b) => a.textContent.length - b.textContent.length)[0];
      if (hit) {
        for (let d = hit; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
        setFull(false);
        if (narrow.matches) close();
        hit.scrollIntoView({ behavior: "smooth", block: "center" });
        hit.classList.add("chat-hit");
        setTimeout(() => hit.classList.remove("chat-hit"), 2500);
        return;
      }
    }
  }

  // ---------------------------------------------------------------- 回答过程
  // 事件合并成步骤（与服务端 chat.segments 相同的规则）
  function applyEvent(segs, ev) {
    const last = segs[segs.length - 1];
    if (ev.type === "restart") {
      const kept = segs.filter((s) => s.type === "note" || s.type === "tool");
      segs.length = 0;
      segs.push(...kept, { type: "note", text: `模型不可用，改用备用模型 ${ev.profile || ""} 重新回答` });
    } else if (ev.type === "thinking" || ev.type === "text") {
      if (last && last.type === ev.type) last.text += ev.text;
      else segs.push({ type: ev.type, text: ev.text });
    } else if (ev.type === "tool") {
      segs.push({ type: "tool", name: ev.name, label: ev.label, args: ev.args, result: null });
    } else if (ev.type === "tool_result") {
      const t = [...segs].reverse().find((s) => s.type === "tool" && s.result === null);
      if (t) { t.result = ev.result; t.error = ev.error; }
    } else if (ev.type === "note") {
      segs.push({ type: "note", text: ev.text });
    }
  }

  // 过程步骤 = 除最后一段正文（那是答案）之外的所有步骤
  const splitSegs = (segs) => {
    const last = segs[segs.length - 1];
    return last && last.type === "text" ? [segs.slice(0, -1), last.text] : [segs, ""];
  };

  function stepNode(s) {
    if (s.type === "tool") {
      const d = el("details", "step tool");
      const sum = el("summary");
      sum.appendChild(el("span", "step-icon", "🔧"));
      sum.appendChild(el("span", "step-label", s.label || s.name));
      sum.appendChild(el("span", "step-state"));
      d.appendChild(sum);
      d.appendChild(el("pre", "step-body"));
      return d;
    }
    const d = el("div", `step ${s.type}`);
    if (s.type === "thinking") d.appendChild(el("div", "step-h", "💭 思考"));
    d.appendChild(el("div", "step-body"));
    return d;
  }

  function fillStep(node, s, live) {
    const body = node.querySelector(".step-body");
    if (s.type === "tool") {
      const state = node.querySelector(".step-state");
      state.textContent = s.result === null ? (live ? "进行中…" : "未完成") : s.error ? "出错" : "完成";
      state.className = `step-state ${s.result === null ? "run" : s.error ? "bad" : "ok"}`;
      const args = JSON.stringify(s.args || {}, null, 1);
      body.textContent = `参数：${args}\n\n结果：${s.result === null ? "（等待中）" : s.result}`;
    } else {
      body.textContent = s.text;
    }
  }

  function summaryText(steps, live) {
    const tools = steps.filter((s) => s.type === "tool").length;
    const think = steps.some((s) => s.type === "thinking");
    const parts = [think ? "思考" : null, tools ? `${tools} 次工具调用` : null].filter(Boolean);
    return `${live ? "回答过程（进行中）" : "回答过程"}${parts.length ? "：" + parts.join(" · ") : ""}`;
  }

  // 一条回答的视图：过程（可折叠）+ 答案 + 状态行 + 核对结果
  function assistantView(m) {
    const node = el("div", "chat-msg assistant");
    node.dataset.mid = m.id;
    const trace = el("details", "chat-trace");
    trace.appendChild(el("summary"));
    const steps = el("div", "chat-steps");
    trace.appendChild(steps);
    node.appendChild(trace);
    const answer = el("div", "chat-answer");
    node.appendChild(answer);
    const status = el("div", "chat-progress");
    node.appendChild(status);
    return { node, trace, steps, answer, status, stepEls: [], segs: [], live: true };
  }

  function paint(v) {
    const [steps, answer] = splitSegs(v.segs);
    const stick = nearBottom();
    // 步骤类型对不上（换用备用模型后）就整体重建
    if (v.stepEls.some((n, i) => !steps[i] || n.dataset.type !== steps[i].type)) {
      v.steps.textContent = "";
      v.stepEls = [];
    }
    steps.forEach((s, i) => {
      if (!v.stepEls[i]) {
        const n = stepNode(s);
        n.dataset.type = s.type;
        v.steps.appendChild(n);
        v.stepEls[i] = n;
      }
      fillStep(v.stepEls[i], s, v.live);
    });
    v.trace.hidden = steps.length === 0;
    v.trace.querySelector("summary").textContent = summaryText(steps, v.live);
    if (v.live) {
      v.answer.textContent = answer;
      const last = steps[steps.length - 1];
      v.status.hidden = false;
      v.status.textContent = answer ? "正在输出…"
        : !v.segs.length ? "排队中…"
          : last && last.type === "tool" && last.result === null ? `正在${last.label || last.name}…`
            : last && last.type === "thinking" ? "思考中…" : "处理中…";
      if (!answer) v.trace.open = true; // 还在思考、查资料时展开过程
    }
    if (stick) scrollDown();
  }

  function checksNode(c) {
    const box = el("div", "chat-checks");
    const quotes = c.quotes || [];
    if (quotes.length) {
      const found = quotes.filter((q) => q.found).length;
      box.appendChild(el("div", null, `引文核对：${found}/${quotes.length} 条在原文中逐字找到`));
      quotes.forEach((q) => {
        const where = q.source === "revision" ? "修正记录" : "报告原文";
        if (q.found && q.source === "report" && root.dataset.page !== "report") {
          // 报告不在本页：在新标签页打开报告，用文本片段（#:~:text=）定位引文开头
          const start = q.text.split(/……|…|\.{3,}/)[0].trim().slice(0, 20);
          const a = el("a", "link", `✓ 「${short(q.text)}」→ 在报告中查看`);
          a.href = `${root.dataset.reportUrl}#:~:text=${encodeURIComponent(start)}`;
          a.target = "_blank";
          a.rel = "noopener";
          const row = el("div", "ok");
          row.appendChild(a);
          box.appendChild(row);
        } else if (q.found) {
          const b = el("button", "link", `✓ 「${short(q.text)}」→ 定位到${where}`);
          b.type = "button";
          b.addEventListener("click", () => locate(q.text));
          const row = el("div", "ok");
          row.appendChild(b);
          box.appendChild(row);
        } else {
          box.appendChild(el("div", "warn", `⚠ 「${short(q.text)}」未在${where}中找到，可能是转述，请以原文为准`));
        }
      });
    }
    if (c.unverified && c.unverified.length) {
      const d = el("details", "warn");
      d.appendChild(el("summary", null, `⚠ ${c.unverified.length} 个数字未能在报告、研究笔记或工具结果中找到出处，请核对`));
      d.appendChild(el("div", null, c.unverified.join("、")));
      box.appendChild(d);
    } else if (c.numbers_total) {
      box.appendChild(el("div", "ok", `✓ 回答中的 ${c.numbers_total} 个数字都能找到出处`));
    }
    return box.childNodes.length ? box : null;
  }

  function place(node) {
    const old = log.querySelector(`[data-mid="${node.dataset.mid}"]`);
    if (old) old.replaceWith(node);
    else log.appendChild(node);
  }

  // 已结束的消息（或用户消息）
  function render(m) {
    if (m.role === "user") {
      const node = el("div", "chat-msg user");
      node.dataset.mid = m.id;
      if (m.quote) node.appendChild(el("span", "qt", m.quote));
      node.appendChild(document.createTextNode(m.text || ""));
      place(node);
      return;
    }
    const v = assistantView(m);
    v.live = m.status === "pending";
    v.segs = (m.trace || []).map((s) => ({ ...s }));
    paint(v);
    v.trace.open = false;
    if (m.status === "done") {
      v.status.hidden = true;
      v.answer.classList.add("md");
      v.answer.innerHTML = m.html; // 服务端 md_html（nh3 白名单）净化过
      const checks = m.checks && checksNode(m.checks);
      if (checks) v.node.appendChild(checks);
    } else if (m.status === "error") {
      v.status.hidden = true;
      v.node.classList.add("error");
      v.answer.textContent = m.text || "回答失败";
    }
    place(v.node);
    return v;
  }

  // ---------------------------------------------------------------- 事件流
  function stopFollow() {
    if (source) { source.close(); source = null; }
    clearTimeout(timer);
    timer = null;
  }

  function setBusy(b) {
    busy = b;
    send.disabled = b || unavailable;
  }

  function finish(m) {
    render(m);
    setBusy(false);
    scrollDown();
  }

  // 事件流不可用（例如回答在另一个 stock web 进程里进行）时，退回轮询
  function poll(mid) {
    const mine = chatId;
    timer = setTimeout(async () => {
      try {
        const m = await api(`/chats/msg/${mid}`);
        if (mine !== chatId) return;
        if (m.status === "pending") { render(m); poll(mid); } else finish(m);
      } catch {
        if (mine === chatId) poll(mid);
      }
    }, 2000);
  }

  function follow(mid) {
    stopFollow();
    const mine = chatId;
    const v = assistantView({ id: mid });
    place(v.node);
    paint(v);
    let frame = 0;
    const es = new EventSource(`/chats/msg/${mid}/events`);
    source = es;
    es.onmessage = (e) => {
      if (mine !== chatId) { es.close(); return; }
      applyEvent(v.segs, JSON.parse(e.data));
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; paint(v); });
    };
    es.addEventListener("final", (e) => {
      es.close();
      if (source === es) source = null;
      if (mine !== chatId) return;
      const m = JSON.parse(e.data);
      if (m.status === "pending") poll(mid);
      else finish(m);
    });
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED && source === es) { source = null; poll(mid); }
      // 否则浏览器会带着 Last-Event-ID 自动重连，服务端从断点继续发送
    };
  }

  function showError(msg) {
    log.appendChild(el("div", "chat-msg error", msg));
    scrollDown();
  }

  async function loadChat(id) {
    stopFollow();
    chatId = id ? Number(id) : null;
    log.querySelectorAll(".chat-msg").forEach((n) => n.remove());
    empty.hidden = chatId !== null;
    del.hidden = chatId === null;
    setBusy(false);
    if (chatId === null) return;
    try {
      const data = await api(`/chats/${chatId}`);
      data.messages.forEach((m) => { if (m.status !== "pending") render(m); });
      scrollDown();
      const pending = data.messages.find((m) => m.status === "pending");
      if (pending) { setBusy(true); follow(pending.id); }
    } catch (e) {
      showError(`读取对话失败：${e.message}`);
    }
  }

  // ---------------------------------------------------------------- 提问
  function setQuote(text) {
    quote = text.slice(0, 3000);
    $("[data-chat-quote-text]").textContent = quote;
    quoteBox.hidden = !quote;
  }
  $("[data-chat-quote-clear]").addEventListener("click", () => setQuote(""));

  async function ask(question) {
    question = question.trim();
    if (!question || busy || unavailable) return;
    setBusy(true);
    try {
      const body = { question, quote, web_search: webBox.checked };
      let data;
      if (chatId === null) {
        data = await api(`/c/${encodeURIComponent(root.dataset.cid)}/chats`,
          { ...body, dossier_id: Number(root.dataset.dossier), revision_id: Number(root.dataset.revision) || null });
        select.add(new Option(question.slice(0, 30), data.chat_id), 1);
        select.value = String(data.chat_id);
        chatId = data.chat_id;
        empty.hidden = true;
        del.hidden = false;
      } else {
        data = await api(`/chats/${chatId}/ask`, body);
      }
      render({ id: `q${data.message_id}`, role: "user", text: question, quote });
      input.value = "";
      setQuote("");
      scrollDown();
      follow(data.message_id);
    } catch (e) {
      setBusy(false);
      showError(e.message);
    }
  }

  $("[data-chat-form]").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  root.querySelectorAll("[data-chat-quick]").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  select.addEventListener("change", () => loadChat(select.value));
  del.addEventListener("click", async () => {
    if (chatId === null || !window.confirm("删除这个对话？")) return;
    try {
      await api(`/chats/${chatId}/delete`, {});
      select.querySelector(`option[value="${chatId}"]`)?.remove();
      select.value = "";
      loadChat("");
    } catch (e) {
      showError(`删除失败：${e.message}`);
    }
  });

  // 在正文中选中文字 →「问这段」
  if (report && askBtn) {
    let t = null;
    const check = () => {
      const sel = window.getSelection();
      const text = sel && !sel.isCollapsed ? sel.toString().trim() : "";
      if (text.length < 4 || !report.contains(sel.anchorNode) || !report.contains(sel.focusNode)) {
        askBtn.hidden = true;
        return;
      }
      const r = sel.getRangeAt(0).getBoundingClientRect();
      askBtn.style.left = `${Math.max(8, window.scrollX + r.left + r.width / 2 - 32)}px`;
      askBtn.style.top = `${window.scrollY + r.top - 36}px`;
      askBtn.dataset.text = text;
      askBtn.hidden = false;
    };
    document.addEventListener("selectionchange", () => { clearTimeout(t); t = setTimeout(check, 250); });
    askBtn.addEventListener("mousedown", (e) => e.preventDefault()); // 保留选区
    askBtn.addEventListener("click", () => {
      setQuote(askBtn.dataset.text || "");
      askBtn.hidden = true;
      window.getSelection()?.removeAllRanges();
      open();
      input.focus();
    });
  }
})();


// 报告页：目录（从正文的标题生成，支持页内跳转与滚动高亮）、悬浮按钮（目录 / 回到顶部）。
(function () {
  "use strict";
  const body = document.querySelector("[data-report-body]");
  const bar = document.querySelector("[data-fab-bar]");
  const top = document.querySelector("[data-to-top]");
  if (top) {
    top.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    const sync = () => { top.hidden = window.scrollY < 600; };
    window.addEventListener("scroll", sync, { passive: true });
    sync();
  }
  const containers = document.querySelectorAll("[data-toc]");
  if (!body || !bar || !containers.length) return;
  const heads = [...body.querySelectorAll("h1, h2, h3")].filter((h) => h.textContent.trim());
  if (heads.length < 3) return;
  const lowest = Math.min(...heads.map((h) => Number(h.tagName[1])));
  const links = [];
  heads.forEach((h, i) => {
    h.id = `rp-${i + 1}`; // 标题的 id 由这里统一生成（服务端净化后的正文不带 id）
    h.classList.add("rp-head");
  });
  containers.forEach((box) => {
    heads.forEach((h) => {
      const a = document.createElement("a");
      a.href = `#${h.id}`;
      a.className = `toc-l${Math.min(Number(h.tagName[1]) - lowest + 1, 3)}`;
      a.textContent = h.textContent.replace(/\s+/g, " ").trim();
      box.appendChild(a);
      links.push([h, a]);
    });
  });
  document.querySelector("[data-toc-card]").hidden = false;
  const pop = document.querySelector("[data-toc-pop]");
  const toggle = document.querySelector("[data-toc-toggle]");
  toggle.hidden = false;
  const setPop = (on) => { pop.hidden = !on; toggle.setAttribute("aria-expanded", String(on)); };
  toggle.addEventListener("click", (e) => { e.stopPropagation(); setPop(pop.hidden); });
  document.querySelector("[data-toc-close]").addEventListener("click", () => setPop(false));
  pop.addEventListener("click", (e) => { if (e.target.closest("a")) setPop(false); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) setPop(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setPop(false); });
  // 滚动高亮：取最后一个已经滚过视口上沿的标题
  const spy = () => {
    let cur = null;
    for (const [h] of links.slice(0, heads.length)) { if (h.getBoundingClientRect().top <= 90) cur = h; else break; }
    links.forEach(([h, a]) => a.classList.toggle("on", h === cur));
  };
  window.addEventListener("scroll", spy, { passive: true });
  spy();
})();
