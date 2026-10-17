// Runs the served index.html + app.js in jsdom with canned API responses, then
// performs steps and prints snapshots of what a user would see, as JSON.
// Input (stdin): {html, script, hash, responses: {path: {status, body} | {fail}}, steps: [...]}
// Steps: {wait}, {click: selector}, {key: "Escape"}, {eval: "js" (window.__app has the loaders)},
//        {respond: path, with: {...}}, {open: selector}, {type: selector, text}, {snapshot: name}.
// usage: NODE_PATH=<dir containing jsdom> node dom_harness.js < input.json
"use strict";
const { JSDOM, VirtualConsole } = require("jsdom");

const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => errors.push(String(e && e.message || e)));
virtualConsole.on("error", (e) => errors.push(String(e)));

const html = input.html.replace(/<script[^>]*src="\/app\.js"[^>]*><\/script>/, "");
const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/" + (input.hash || ""), runScripts: "outside-only",
  pretendToBeVisual: true, virtualConsole });
const { window } = dom;
const responses = input.responses;

window.fetch = async (path) => {
  const r = responses[String(path).split("?")[0]];
  if (!r || r.fail) throw new window.TypeError("Failed to fetch");
  return { ok: r.status === undefined || r.status < 400, status: r.status || 200, json: async () => r.body };
};
// With input.fakeTimers, page intervals never run on their own: {eval: "__tick(3000)"} runs one tick
// and {eval: "__clock(ms)"} moves the page's Date.now() forward.
const fetches = {};
const requests = []; // every non-GET request: {path, method, body}
const realFetch = window.fetch;
window.fetch = (path, options) => {
  const key = String(path).split("?")[0];
  fetches[key] = (fetches[key] || 0) + 1;
  if (options && options.method && options.method !== "GET") {
    requests.push({ path: key, method: options.method, body: options.body ? JSON.parse(options.body) : null });
  }
  return realFetch(path);
};
if (input.fakeTimers) {
  const intervals = {};
  let offset = 0;
  const realNow = window.Date.now.bind(window.Date);
  window.setInterval = (fn, ms) => { intervals[ms] = fn; return 0; };
  window.Date.now = () => realNow() + offset;
  window.__tick = (ms) => intervals[ms]();
  window.__clock = (ms) => { offset += ms; };
}
window.HTMLDialogElement.prototype.showModal ||= function () { this.open = true; };
window.HTMLDialogElement.prototype.close ||= function () { this.open = false; };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const text = (node) => (node ? node.textContent.replace(/\s+/g, " ").trim() : null);
const visible = (id) => !window.document.getElementById(id).hidden;

function snapshot() {
  const doc = window.document;
  const overview = doc.getElementById("overview");
  const inspect = doc.getElementById("inspect-body");
  const active = doc.activeElement;
  return {
    hash: window.location.hash,
    fetches: { ...fetches },
    visible: { observability: visible("panel-observability"), kanban: visible("panel-kanban"), inspect: visible("inspect") },
    selected: [...doc.querySelectorAll('[role="tab"]')].filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.id),
    tabs: doc.querySelectorAll('[role="tab"]').length,
    tabLabels: [...doc.querySelectorAll('[role="tab"]')].map((t) => text(t)),
    current: text(overview.querySelector(".current h2")),
    phase: text(overview.querySelector(".phase-line")),
    overviewInspectHref: overview.querySelector("a[data-inspect]")?.getAttribute("href") || null,
    overviewInspectText: text(overview.querySelector("a[data-inspect]")),
    overview: text(overview),
    order: ["quota", ...[...overview.children].map((n) => n.className)],
    overviewCharts: overview.querySelectorAll("svg").length,
    overviewTables: overview.querySelectorAll("table, details, ol.phases, dl").length,
    quota: Object.fromEntries([...doc.querySelectorAll("#quota .quota-card")].map((c) => [c.dataset.provider, {
      state: c.dataset.state, text: text(c), big: text(c.querySelector(".big-number")),
      meter: c.querySelector('[role="meter"]')?.getAttribute("aria-valuenow") ?? null,
      reset: text(c.querySelector(".reset")), measured: text(c.querySelector(".measured")),
      note: text(c.querySelector(".quota-note")),
    }])),
    quotaOrder: [...doc.querySelectorAll("#quota .quota-card")].map((c) => c.dataset.provider),
    chart: (() => {
      const fig = overview.querySelector("figure.chart");
      if (!fig) return null;
      return {
        title: text(fig.querySelector(".chart-head")),
        legend: [...fig.querySelectorAll(".legend li")].map((li) => [li.dataset.provider, text(li)]),
        series: Object.fromEntries([...fig.querySelectorAll("g.series")].map((g) => [g.dataset.provider,
          [...g.querySelectorAll("circle")].map((c) => [Number(c.getAttribute("cx")), Number(c.getAttribute("cy"))])])),
        xTicks: [...fig.querySelectorAll(".x-tick")].map((t) => [text(t), Number(t.getAttribute("x"))]),
        yTicks: [...fig.querySelectorAll(".y-tick")].map((t) => text(t)),
        scope: text(fig.querySelector(".chart-scope")),
        label: fig.querySelector("svg").getAttribute("aria-label"),
        viewBox: fig.querySelector("svg").getAttribute("viewBox"),
      };
    })(),
    usage: [...overview.querySelectorAll(".usage-row")].map((r) => ({ provider: r.dataset.provider,
      who: text(r.querySelector(".usage-provider")), model: text(r.querySelector(".usage-model")),
      total: text(r.querySelector(".usage-total")), parts: text(r.querySelector(".usage-parts")) })),
    usageCaption: text(overview.querySelector(".usage-caption")),
    usageCache: text(overview.querySelector(".usage-cache")),
    usageBars: [...overview.querySelectorAll(".usage-row")].map((r) => ({
      share: text(r.querySelector(".usage-share")), width: r.querySelector(".usage-fill")?.style.width ?? null,
      label: r.querySelector(".usage-bar")?.getAttribute("aria-label") ?? null, title: r.title })),
    active: [...overview.querySelectorAll(".active-runs li")].map((li) => text(li)),
    runState: { state: doc.getElementById("run-state").dataset.state, text: text(doc.getElementById("run-state-text")) },
    obsMeta: text(doc.getElementById("obs-meta")),
    back: text(doc.getElementById("inspect-back")),
    inspectMeta: text(doc.getElementById("inspect-meta")),
    inspect: text(inspect),
    inspectTables: inspect.querySelectorAll("table").length,
    approvals: [...inspect.querySelectorAll(".approval")].map((a) => ({ id: a.dataset.request, state: a.dataset.state,
      buttons: [...a.querySelectorAll("button.decide")].map((b) => text(b)) })),
    approvalPanels: [...inspect.querySelectorAll(".approval")].map((a) => {
      const area = a.querySelector(".approval-feedback textarea");
      const error = a.querySelector(".approval-error");
      return { id: a.dataset.request, state: a.dataset.state, text: text(a),
        feedback: area ? { value: area.value, required: area.required, maxLength: area.maxLength,
          label: text(a.querySelector(`label[for="${area.id}"]`)) } : null,
        error: error && !error.hidden ? text(error) : null,
        disabled: [...a.querySelectorAll("button")].filter((b) => b.disabled).map((b) => text(b)),
        decided: text(a.querySelector(".approval-decided")) };
    }),
    approvalResult: (() => {
      const r = inspect.querySelector(".approval-result");
      return r ? { role: r.getAttribute("role"), text: text(r) } : null;
    })(),
    inspectDetails: [...inspect.querySelectorAll("details.run-telemetry")].map((d) => ({ id: d.dataset.runId, open: d.open })),
    inspectSparks: inspect.querySelectorAll("svg").length,
    focus: { id: active?.id || null, inspect: active?.dataset?.inspect || null, tag: active?.tagName || null,
      inPanel: active?.closest?.(".panel")?.id || null },
    status: (() => {
      const node = doc.getElementById("status");
      let hidden = false;
      for (let n = node; n; n = n.parentElement) hidden ||= n.hidden;
      return { count: doc.querySelectorAll("#status, .status").length, inPanel: node.closest(".panel")?.id || null,
        text: text(node), hidden };
    })(),
    kanban: { cards: doc.querySelectorAll("#lanes .card").length, inspectLinks: doc.querySelectorAll("#lanes a[data-inspect]").length,
      edit: doc.querySelectorAll("#lanes .edit").length, move: doc.querySelectorAll("#lanes .move-select").length,
      filters: Boolean(doc.getElementById("only-verified") && doc.getElementById("verified-first")),
      lifecycle: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id, text(c.querySelector(".lifecycle"))])),
      lanes: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id, c.closest(".lane").dataset.stage])),
      laneTitles: [...doc.querySelectorAll("#lanes .lane > h2")].map((h) => h.firstChild.textContent),
      flags: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id, text(c.querySelector(".flags"))])),
      flagTitles: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id,
        Object.fromEntries([...c.querySelectorAll(".flags .tag")].map((t) => [text(t), t.title]))])),
      fieldsHidden: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id, c.querySelector(".fields").hidden])),
      archive: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id,
        c.querySelector(".archive").hidden ? null : text(c.querySelector(".archive"))])),
      archivedToggle: text(doc.getElementById("show-archived")),
      bulk: { hidden: doc.getElementById("archive-done").hidden, disabled: doc.getElementById("archive-done").disabled },
      bulkLane: doc.getElementById("archive-done").closest(".lane")?.dataset.stage || null,
      fields: Object.fromEntries([...doc.querySelectorAll("#lanes .card")].map((c) => [c.dataset.id, {
        owner: text(c.querySelector(".owner")), next: text(c.querySelector(".next-action")),
        gate: text(c.querySelector("dd.gate")), nextSource: c.querySelector(".next-action").title }])),
      addTask: { text: text(doc.getElementById("new-card")), disabled: doc.getElementById("new-card").disabled } },
    setup: { open: doc.getElementById("setup-guide").open,
      note: text(doc.getElementById("setup-note")),
      checks: [...doc.querySelectorAll("#setup-checks li")].map((node) => ({
        state: node.dataset.state, label: text(node.querySelector("strong")),
        detail: text(node.querySelector("span")) })) },
    editor: (() => {
      const status = doc.getElementById("f-status");
      return { open: doc.getElementById("editor").open, title: doc.getElementById("f-title").value,
        status: status.value, statusOptions: [...status.options].map((o) => o.value), statusDisabled: status.disabled,
        stage: doc.getElementById("f-stage").value, details: doc.getElementById("f-details").value,
        titleNote: doc.getElementById("f-title-note").hidden ? null : text(doc.getElementById("f-title-note")),
        error: doc.getElementById("editor-error").hidden ? null : text(doc.getElementById("editor-error")) };
    })(),
    requests: requests.slice(),
  };
}

(async () => {
  const out = { snapshots: {}, errors };
  try {
    // app.js is strict, so its functions stay inside this eval; expose the loaders for {eval} steps.
    window.eval(input.script + "\n;window.__app = { loadInspect, loadObservability, loadQuota, quotaReset: () => { quotaAttemptAt = 0; } };");
    await sleep(60);
    for (const step of input.steps || []) {
      if (step.wait) await sleep(step.wait);
      if (step.respond) responses[step.respond] = step.with;
      if (step.click) {
        const node = window.document.querySelector(step.click);
        if (!node) throw new Error("no element for " + step.click);
        node.focus();
        node.click();
        await sleep(60);
      }
      if (step.type) {
        const node = window.document.querySelector(step.type);
        if (!node) throw new Error("no element for " + step.type);
        node.focus();
        node.value = step.text;
        node.dispatchEvent(new window.Event("input", { bubbles: true }));
        await sleep(30);
      }
      if (step.open) {
        window.document.querySelector(step.open).open = true;
      }
      if (step.key) {
        const target = window.document.activeElement || window.document.body;
        target.dispatchEvent(new window.KeyboardEvent("keydown", { key: step.key, bubbles: true }));
        await sleep(60);
      }
      if (step.eval) {
        await window.eval(step.eval);
        await sleep(30);
      }
      if (step.snapshot) out.snapshots[step.snapshot] = snapshot();
    }
  } catch (error) {
    errors.push(String(error && error.stack || error));
  }
  process.stdout.write(JSON.stringify(out));
  window.close();
})();
