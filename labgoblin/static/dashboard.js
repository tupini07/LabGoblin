(() => {
  const button = document.getElementById("refresh");
  const toggle = document.getElementById("live-refresh");
  const status = document.getElementById("refresh-status");
  let refreshing = false;

  function initializeJournal() {
    for (const controls of document.querySelectorAll(".journal-fold-controls")) controls.hidden = false;
  }

  function frame() {
    return JSON.parse(document.getElementById("main").dataset.snapshot);
  }

  function initializeCheckpoint() {
    const controls = document.querySelector(".catchup-controls");
    if (!controls) return;
    controls.hidden = false;
    const current = frame();
    const key = `labgoblin-checkpoint-${current.campaign}-${current.generation}`;
    const note = document.getElementById("catchup-status");
    const link = document.getElementById("catchup-link");
    try {
      const saved = JSON.parse(localStorage.getItem(key) ?? "null");
      const valid = saved && saved.campaign === current.campaign && saved.generation === current.generation
        && Number.isFinite(saved.read_at) && saved.read_at >= 0 && saved.read_at <= 8640000000000
        && ["source_cutoff", "event_cutoff"].every(name =>
          Number.isSafeInteger(saved[name]) && saved[name] >= 0 && saved[name] <= current[name]);
      if (saved && !valid) throw new Error("Saved checkpoint is incompatible with this retained snapshot");
      note.textContent = saved
        ? `Since your saved checkpoint: ${new Date(saved.read_at * 1000).toISOString()}. Inspect the full change interval below.`
        : "Recent changes. No saved viewing checkpoint in this browser for this campaign generation.";
      note.classList.remove("error");
      document.getElementById("catchup-clear").disabled = !saved;
      const query = new URLSearchParams({
        campaign: current.campaign, generation: current.generation,
        source_cutoff: current.source_cutoff, event_cutoff: current.event_cutoff,
        after_source: saved?.source_cutoff || 0, after_event: saved?.event_cutoff || 0,
      });
      link.href = `/changes?${query}`;
      link.textContent = saved ? "Inspect changes since checkpoint" : "Inspect all recorded changes";
    } catch (error) {
      note.textContent = `Viewing checkpoint unavailable: ${error.message}. Recent records remain available.`;
      note.classList.add("error");
      document.getElementById("catchup-clear").disabled = false;
      link.href = "/changes";
      link.textContent = "Inspect all recorded changes";
    }
  }

  async function refresh(checkOnly = false) {
    if (refreshing) return;
    refreshing = true;
    button.disabled = true;
    try {
      const response = await fetch(window.location.href, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = parsed.getElementById("main");
      if (!replacement) throw new Error("Dashboard content is missing");
      const current = window.document.getElementById("main");
      if (checkOnly) {
        const previous = JSON.parse(current.dataset.snapshot);
        const next = JSON.parse(replacement.dataset.snapshot);
        if (["campaign", "generation", "source_cutoff", "event_cutoff", "change_token"].some(key => previous[key] !== next[key])) {
          status.textContent = "New recorded data is available. Refresh to inspect it; your reading snapshot is unchanged.";
          status.classList.remove("error");
        }
        return;
      }
      const states = new Map(Array.from(current.querySelectorAll("details[data-key]"),
        details => [details.dataset.key, details.open]));
      for (const next of replacement.querySelectorAll("details[data-key]")) {
        if (states.has(next.dataset.key)) next.open = states.get(next.dataset.key);
      }
      const anchor = Array.from(current.querySelectorAll(".journal-entry")).find(entry => {
        const bounds = entry.getBoundingClientRect();
        return bounds.top <= 150 && bounds.bottom > 150;
      });
      const anchorTop = anchor?.getBoundingClientRect().top;
      const scroll = window.scrollY;
      current.replaceWith(replacement);
      initializeJournal();
      initializeCheckpoint();
      window.document.dispatchEvent(new Event("dashboard:refresh"));
      const nextAnchor = anchor && window.document.getElementById(anchor.id);
      window.scrollTo(0, nextAnchor ? scroll + nextAnchor.getBoundingClientRect().top - anchorTop : scroll);
      status.textContent = `Page read at ${new Date(frame().read_at * 1000).toISOString()} · Not a live process probe`;
      status.classList.remove("error");
    } catch (error) {
      status.textContent = `Refresh failed: ${error.message}. Showing the previous snapshot.`;
      status.classList.add("error");
    } finally {
      refreshing = false;
      button.disabled = false;
    }
  }

  initializeJournal();
  initializeCheckpoint();
  window.addEventListener("storage", initializeCheckpoint);
  document.addEventListener("click", async event => {
    const action = event.target.closest("[data-journal-action]");
    if (action) {
      for (const entry of document.querySelectorAll(".journal-entry")) {
        entry.open = action.dataset.journalAction === "expand";
      }
    }
    if (event.target.closest("#catchup-save, #catchup-clear")) {
      const current = frame();
      const key = `labgoblin-checkpoint-${current.campaign}-${current.generation}`;
      try {
        if (event.target.closest("#catchup-clear")) localStorage.removeItem(key);
        else localStorage.setItem(key, JSON.stringify(current));
        initializeCheckpoint();
      } catch (error) {
        const note = document.getElementById("catchup-status");
        note.textContent = `Could not save viewing preference: ${error.message}`;
        note.classList.add("error");
      }
    }
    const copy = event.target.closest("[data-copy-command]");
    if (copy) {
      const note = copy.parentElement.querySelector(".copy-status");
      try {
        await navigator.clipboard.writeText(copy.parentElement.querySelector(".copy-source").textContent);
        note.textContent = "Copied. These commands were not executed.";
        note.classList.remove("error");
      } catch (error) {
        note.textContent = `Copy unavailable: ${error.message}. Select the command text to copy it manually.`;
        note.classList.add("error");
      }
    }
  });
  button.addEventListener("click", () => refresh());
  if (toggle) {
    setInterval(() => {
      const editing = document.activeElement.closest("form, details");
      if (toggle.checked && !document.hidden && !editing) refresh(true);
    }, 15000);
  }
})();
