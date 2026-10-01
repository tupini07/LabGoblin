(() => {
  const panel = document.getElementById("chat-panel");
  const open = document.getElementById("chat-open");
  const close = document.getElementById("chat-close");
  // Client-side layout controls let running dashboards keep their existing chats.
  const maximize = document.createElement("button");
  maximize.id = "chat-maximize";
  maximize.type = "button";
  const windowActions = document.createElement("div");
  windowActions.className = "chat-window-actions";
  close.before(windowActions);
  windowActions.append(maximize, close);
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-question");
  const send = document.getElementById("chat-send");
  const cancel = document.getElementById("chat-cancel");
  const clear = document.getElementById("chat-clear");
  const messages = document.getElementById("chat-messages");
  messages.setAttribute("role", "region");
  messages.tabIndex = 0;
  const status = document.getElementById("chat-status");
  const token = document.querySelector('meta[name="xgenius-chat-token"]').content;
  let conversation = "";
  let expanded = false;
  let maximized = false;
  let ready = false;
  let busy = false;
  let timer = null;
  let pendingRequest = null;
  const entries = new Map();
  const storageKey = `xgenius-chat-${token}`;
  const expandedKey = `${storageKey}-expanded`;
  const maximizedKey = `${storageKey}-maximized`;
  function pageContext() {
    return JSON.parse(document.getElementById("main").dataset.chatContext);
  }
  function contextLabel(context) {
    const recorded = context.read_at ? ` · Page read ${new Date(context.read_at * 1000).toISOString()}` : "";
    return `About: ${context.label || "this campaign"}${recorded}. Exact citations retain their source scope; current state is queried separately.`;
  }
  function updateContext() {
    document.getElementById("chat-context").textContent = contextLabel(pageContext());
  }
  updateContext();
  document.addEventListener("dashboard:refresh", updateContext);
  try {
    conversation = sessionStorage.getItem(storageKey) || "";
    expanded = sessionStorage.getItem(expandedKey) === "true";
    maximized = sessionStorage.getItem(maximizedKey) === "true";
  }
  catch (error) { setStatus(`Browser storage unavailable: ${error.message}. Chat will not survive navigation.`, true); }

  function setStatus(text, error = false) {
    status.textContent = text;
    status.classList.toggle("error", error);
  }

  function controls() {
    send.disabled = !ready || busy;
    input.disabled = !ready || busy;
    cancel.disabled = !busy || !conversation;
    clear.disabled = busy;
  }

  async function request(path, data) {
    const response = await fetch(path, data === undefined ? { cache: "no-store" } : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Xgenius-Token": token },
      body: JSON.stringify(data),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function render(state) {
    conversation = state.conversation_id;
    try { sessionStorage.setItem(storageKey, conversation); }
    catch (error) { setStatus(`Could not retain chat across navigation: ${error.message}`, true); }
    busy = state.busy;
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    if (state.messages.length) document.getElementById("chat-empty")?.remove();
    for (const message of state.messages) {
      let entry = entries.get(message.request_id);
      if (!entry) {
        entry = document.createElement("section");
        entry.className = "chat-exchange";
        const question = document.createElement("p");
        question.className = "chat-question";
        question.textContent = message.question;
        const context = document.createElement("p");
        context.className = "chat-exchange-context";
        context.textContent = contextLabel(message.context || {});
        const answer = document.createElement("div");
        answer.className = "chat-answer markdown";
        const evidence = document.createElement("div");
        evidence.className = "chat-sources";
        const usage = document.createElement("p");
        usage.className = "chat-usage";
        const error = document.createElement("p");
        error.className = "chat-error";
        entry.append(question, context, answer, evidence, usage, error);
        messages.append(entry);
        entries.set(message.request_id, entry);
      }
      const answer = entry.querySelector(".chat-answer");
      if (answer.dataset.content !== message.html) {
        answer.innerHTML = message.html; // Server renders Markdown with HTML/images and external links disabled.
        answer.dataset.content = message.html;
      }
      const evidence = entry.querySelector(".chat-sources");
      evidence.replaceChildren();
      for (const source of message.sources) {
        const link = document.createElement("a");
        link.href = source.url;
        link.textContent = source.label;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        evidence.append(link);
      }
      const reported = message.usage.filter(item => item.input_tokens !== null && item.output_tokens !== null);
      const usage = entry.querySelector(".chat-usage");
      usage.textContent = reported.length
        ? `Reported tokens: ${reported.reduce((n, u) => n + u.input_tokens, 0).toLocaleString()} in / ${reported.reduce((n, u) => n + u.output_tokens, 0).toLocaleString()} out · ${reported[reported.length - 1].model}. Financial usage is not capped here.`
        : "Provider usage not reported yet; not zero. Separate from research-turn accounting.";
      entry.querySelector(".chat-error").textContent = message.error || (message.state === "cancelled" ? "Answer cancelled; partial text may be incomplete." : "");
    }
    const last = state.messages[state.messages.length - 1];
    if (last) setStatus(last.status, Boolean(last.error));
    if (nearBottom) messages.scrollTop = messages.scrollHeight;
    controls();
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const result = await request("/chat/status" + (conversation ? `?conversation_id=${encodeURIComponent(conversation)}` : ""));
      ready = result.ready;
      document.getElementById("chat-notice").textContent = result.notice;
      document.getElementById("chat-model").textContent = `Model: ${result.model}${result.reasoning_effort ? ` · Effort: ${result.reasoning_effort}` : ""} · Deadline: ${result.timeout_seconds}s`;
      if (result.conversation) render(result.conversation);
      if (!ready) setStatus(result.reason, true);
      controls();
      if (busy) timer = setTimeout(poll, 700);
    } catch (error) {
      setStatus(`${error.message} No request was retried automatically. Reopen chat to reconnect.`, true);
    }
  }

  function updateLayout() {
    panel.hidden = !expanded;
    panel.classList.toggle("is-maximized", maximized);
    open.setAttribute("aria-expanded", String(expanded));
    maximize.textContent = maximized ? "Restore" : "Maximize";
    maximize.setAttribute("aria-label", maximized ? "Restore sidebar" : "Maximize chat");
    maximize.title = maximized ? "Return to the sidebar (Esc)" : "Use the full window for reading";
    const modal = expanded && maximized;
    panel.setAttribute("aria-modal", String(modal));
    document.body.classList.toggle("chat-maximized", modal);
    for (const background of document.querySelectorAll(".sidebar, .workspace, .skip-link")) {
      background.inert = modal;
    }
  }

  function setExpanded(value) {
    expanded = value;
    updateLayout();
    try { sessionStorage.setItem(expandedKey, String(value)); }
    catch (error) { setStatus(`Could not retain the sidebar state across navigation: ${error.message}`, true); }
  }

  function setMaximized(value) {
    const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight <= 4;
    const top = messages.getBoundingClientRect().top;
    const anchor = Array.from(messages.querySelectorAll(
      ".chat-question, .chat-answer > *, .chat-sources, .chat-usage, .chat-error"
    )).find(element => {
      const bounds = element.getBoundingClientRect();
      return bounds.height > 0 && bounds.bottom > top;
    });
    const bounds = anchor?.getBoundingClientRect();
    // Preserve the visible paragraph's reading position when its lines reflow.
    const fraction = bounds ? Math.max(0, (top - bounds.top) / bounds.height) : 0;
    const gap = bounds ? Math.max(0, bounds.top - top) : 0;
    maximized = value;
    updateLayout();
    if (atBottom) {
      messages.scrollTop = messages.scrollHeight;
    } else if (anchor) {
      const next = anchor.getBoundingClientRect();
      messages.scrollTop += next.top - messages.getBoundingClientRect().top + fraction * next.height - gap;
    }
    try { sessionStorage.setItem(maximizedKey, String(value)); }
    catch (error) { setStatus(`Could not retain the chat size across navigation: ${error.message}`, true); }
  }

  controls();
  setExpanded(expanded);
  if (expanded && maximized) maximize.focus({ preventScroll: true });
  if (expanded) poll();
  open.addEventListener("click", () => {
    setExpanded(true);
    poll().then(() => { if (!input.disabled) input.focus(); });
  });
  close.addEventListener("click", () => {
    setExpanded(false);
    open.focus();
  });
  maximize.addEventListener("click", () => setMaximized(!maximized));
  panel.addEventListener("keydown", event => {
    if (!maximized) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setMaximized(false);
      maximize.focus({ preventScroll: true });
    } else if (event.key === "Tab") {
      const focusable = Array.from(panel.querySelectorAll(
        "a[href], button:not(:disabled), textarea:not(:disabled), [tabindex='0']"
      )).filter(element => element.getClientRects().length);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!ready || busy) return;
    const question = input.value.trim();
    if (!question) return;
    if (!pendingRequest || pendingRequest.message !== question) {
      pendingRequest = { conversation_id: conversation, request_id: crypto.randomUUID().replaceAll("-", ""),
        message: question, context: pageContext() };
    }
    busy = true;
    controls();
    setStatus("Starting restricted Copilot observer");
    try {
      const state = await request("/chat/message", pendingRequest);
      pendingRequest = null;
      input.value = "";
      render(state);
      poll();
    } catch (error) {
      busy = false;
      controls();
      setStatus(`${error.message} Sending the same question again reuses its request ID.`, true);
    }
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  cancel.addEventListener("click", async () => {
    try {
      render(await request("/chat/cancel", { conversation_id: conversation }));
      poll();
    } catch (error) { setStatus(error.message, true); }
  });
  clear.addEventListener("click", async () => {
    try {
      await request("/chat/clear", { conversation_id: conversation });
      conversation = "";
      try { sessionStorage.removeItem(storageKey); }
      catch (error) { setStatus(`Could not clear the saved conversation handle: ${error.message}`, true); }
      pendingRequest = null;
      entries.clear();
      messages.replaceChildren();
      setStatus("New conversation. Earlier messages are no longer held by this dashboard.");
      await poll();
      if (!input.disabled) input.focus();
    } catch (error) { setStatus(error.message, true); }
  });
})();
