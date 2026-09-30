(() => {
  const button = document.getElementById("refresh");
  const toggle = document.getElementById("live-refresh");
  const status = document.getElementById("refresh-status");
  let refreshing = false;

  function initializeJournal() {
    for (const controls of document.querySelectorAll(".journal-fold-controls")) controls.hidden = false;
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    button.disabled = true;
    try {
      const response = await fetch(window.location.href, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const document = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = document.getElementById("main");
      if (!replacement) throw new Error("Dashboard content is missing");
      const current = window.document.getElementById("main");
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
      const nextAnchor = anchor && window.document.getElementById(anchor.id);
      window.scrollTo(0, nextAnchor ? scroll + nextAnchor.getBoundingClientRect().top - anchorTop : scroll);
      status.textContent = `Updated ${new Date().toISOString().slice(11, 19)} UTC`;
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
  document.addEventListener("click", event => {
    const action = event.target.closest("[data-journal-action]");
    if (!action) return;
    for (const entry of document.querySelectorAll(".journal-entry")) {
      entry.open = action.dataset.journalAction === "expand";
    }
  });
  button.addEventListener("click", refresh);
  if (toggle) {
    setInterval(() => {
      const editing = document.activeElement.closest("form, details");
      if (toggle.checked && !document.hidden && !editing) refresh();
    }, 15000);
  }
})();
