"use strict";
const $ = (id) => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem("labgoblinInspectorToken") || "";
if (location.hash) sessionStorage.setItem("labgoblinInspectorToken", token);
history.replaceState(null, "", location.pathname);
let historyOffset = 0;
let nextPage = null;
let polling = null;
let busy = false;
let latestRun = null;

async function api(path, args) {
  const response = await fetch(path, {
    method: args === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: args === undefined ? undefined : JSON.stringify(args),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function showError(error) { $("error").textContent = error.message || String(error); }
function run(action) { $("error").textContent = ""; return action().catch(showError); }
function element(tag, value, parent) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (parent) parent.append(node);
  return node;
}
function picture(source, parent) {
  if (!["image/png", "image/jpeg"].includes(source.mime)) throw new Error("Unsupported image type");
  const figure = element("figure", undefined, parent);
  const img = element("img", undefined, figure);
  img.alt = source.path || "Captured research image";
  img.src = `data:${source.mime};base64,${source.image}`;
  element("figcaption", source.path, figure);
}
function display(source) {
  $("evidence").replaceChildren();
  $("source-facts").textContent =
    `${source.scope || source.run || "Captured evidence"} | ${source.live ? "Mutable/live read" : "Captured read"}\n` +
    `Observed ${new Date(source.observed_at * 1000).toLocaleString()}\n` +
    `${source.coverage || ""}${source.truncated ? "\nBounded / incomplete coverage." : ""}` +
    (source.sha256 ? `\nSHA256 ${source.sha256}` : "");
  if (source.image) picture(source, $("evidence"));
  else if (source.entries) {
    for (const item of source.entries) {
      const button = element("button", item.path + (item.directory ? " [directory]" : ""), $("evidence"));
      button.onclick = () => run(async () => {
        $("path").value = item.path;
        const action = item.directory ? "listing" : "read";
        await browse(action, false, 0, { scope: source.scope, path: item.path, offset: 0,
          ...(action === "read" ? { image: /\.(png|jpe?g)$/i.test(item.path) } : {}) });
      });
    }
  } else if (source.hits) {
    element("p", `${source.scanned_files} files / ${source.scanned_bytes} bytes searched; ${source.skipped_files.length} skipped.`, $("evidence"));
    for (const hit of source.hits) {
      const button = element("button", `${hit.path}:${hit.line} - ${hit.text}`, $("evidence"));
      button.onclick = () => run(async () => {
        $("path").value = hit.path;
        await browse("read", false, 0, { scope: source.scope, path: hit.path, offset: hit.offset });
      });
    }
  } else element("pre", source.text === undefined ? JSON.stringify(source, null, 2) : source.text, $("evidence"));
}
async function browse(action, image = false, offset = 0, savedArgs = null) {
  let args = savedArgs || { scope: $("scope").value, path: $("path").value, offset };
  if (!savedArgs) {
    if (action === "read") args.image = image;
    if (action === "search") args.query = $("search").value;
    if (action === "execution" || action === "session") {
      args = { run: ["latest", "workspace"].includes($("scope").value) ? latestRun : $("scope").value, offset };
      if (action === "session") args.role = $("role").value;
    }
  }
  const source = await api(`/api/${action}`, args);
  display(source);
  nextPage = source.next_offset === null || source.next_offset === undefined
    ? null : () => browse(action, image, source.next_offset, {
      ...args, offset: source.next_offset,
      ...(source.scope ? { scope: source.scope } : {}),
      ...(action === "read" ? { sha256: source.sha256 } : {}),
    });
  $("next").hidden = nextPage === null;
}
async function messages(reset = true) {
  if (reset) $("messages").replaceChildren();
  const value = await api(reset ? "/api/history" :
    `/api/history?offset=${Math.max(0, historyOffset - 40)}&limit=${Math.min(40, historyOffset)}`);
  const fragment = document.createDocumentFragment();
  for (const item of value.messages) {
    const article = element("article", undefined, fragment);
    element("h3", item.question, article);
    element("p", `${new Date(item.created * 1000).toLocaleString()} - ${item.status}`, article);
    const answer = element("div", item.answer || item.reason || "Inspecting existing evidence...", article);
    answer.className = "answer";
    if (item.history_omitted) element("p", `${item.history_omitted} earlier questions were outside this answer's bounded chat context.`, article);
    const references = element("div", undefined, article);
    references.className = "references";
    for (const source of item.sources) {
      const button = element("button", `[${source.citation}] ${source.path || "Operational / session evidence"}`, references);
      button.onclick = () => run(async () => {
        const captured = await api(`/api/source?run=${encodeURIComponent(item.id)}&citation=${source.citation}`);
        display({ ...captured.result, coverage: (captured.result.coverage || "") +
          (captured.receipt_verified ? "\nCapture verified against its inspection receipt." :
            "\nUnsettled/interrupted capture: not verified against a terminal inspection receipt.") });
        $("next").hidden = true;
        if (captured.result.image && !article.querySelector(`[data-citation="${source.citation}"]`)) {
          const holder = element("div", undefined, article);
          holder.dataset.citation = source.citation;
          picture(captured.result, holder);
        }
        $("evidence-heading").scrollIntoView({ block: "nearest" });
      });
    }
  }
  $("messages").prepend(fragment);
  historyOffset = value.offset;
  $("more-history").hidden = historyOffset === 0;
}
async function refresh() {
  const value = await api("/api/status");
  const research = value.research;
  const latest = research.runs.at(-1);
  latestRun = latest ? latest.id : null;
  $("execution").disabled = latestRun === null;
  $("session").disabled = latestRun === null;
  $("project").textContent = research.root;
  const budget = research.budget;
  const observer = value.inspection.budget;
  $("status").textContent =
    `Observed ${new Date(research.observed_at * 1000).toLocaleString()}\n` +
    `Research: ${research.state}${latest ? `; recorded owner ${latest.owner}; ${latest.phase}` : ""}\n` +
    `Research usage: ${budget.used.toFixed(3)} credits (includes ${(budget.partial_nano / 1e9).toFixed(3)} partial lower bound); cap ${budget.configured}; unknown bills ${budget.uncovered_unknown.length}\n` +
    `Research reserved: ${(budget.reserved_nano / 1e9).toFixed(3)}; estimates: ${budget.estimated.toFixed(3)}\n` +
    (observer ? `Separate inspection usage: ${observer.used.toFixed(3)} / ${observer.configured}; reserved ${(observer.reserved_nano / 1e9).toFixed(3)}; estimates ${observer.estimated.toFixed(3)}; unknown bills ${observer.uncovered_unknown.length}` :
      "Inspection allowance: not authorized");
  $("chat-policy").textContent = value.chat_enabled
    ? "Submitting a question spends from the separate inspection allowance. Evidence browsing and refresh do not call a model."
    : "View-only. Start with an explicit --credits allowance to enable questions; browsing remains model-free.";
  $("ask").disabled = !value.chat_enabled || value.busy;
  $("question").disabled = !value.chat_enabled || value.busy;
  $("cancel").hidden = !value.busy;
  for (const item of research.runs) {
    if (!Array.from($("scope").options).some((option) => option.value === item.id) && item.phase === "quiescent") {
      const option = element("option", `${item.id.slice(0, 8)}: ${item.status}`, $("scope"));
      option.value = item.id;
    }
  }
  if (value.error) showError(value.error);
  if (busy && !value.busy) await messages();
  busy = value.busy;
  if (busy && polling === null) polling = setInterval(() => run(refresh), 1500);
  if (!busy && polling !== null) { clearInterval(polling); polling = null; }
}
$("refresh").onclick = () => run(async () => { await refresh(); await messages(); });
$("read").onclick = () => run(() => browse("read"));
$("image").onclick = () => run(() => browse("read", true));
$("list").onclick = () => run(() => browse("listing"));
$("find").onclick = () => run(() => browse("search"));
$("execution").onclick = () => run(() => browse("execution"));
$("session").onclick = () => run(() => browse("session"));
$("next").onclick = () => run(nextPage);
$("more-history").onclick = () => run(() => messages(false));
$("cancel").onclick = () => run(async () => { await api("/api/cancel", {}); await refresh(); });
$("question-form").onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    $("ask").disabled = true;
    try {
      await api("/api/question", { question: $("question").value });
      $("question").value = "";
      await messages();
    } finally {
      await refresh();
    }
  });
};
run(async () => {
  if (!token) throw new Error("Open the exact local URL printed by labgoblin inspect. Its fragment authorizes this browser.");
  await refresh();
  await messages();
});
