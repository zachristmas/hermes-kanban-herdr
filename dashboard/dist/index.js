// kanban-herdr: a "Herdr panes" section in the kanban task drawer.
//
// Lists the Herdr terminal panes linked to the open task (pane tokens kanban_task / kanban_board,
// or a manual link) as an accordion. Opening one shows its terminal text live, read-only, over a
// WebSocket served by this plugin's backend. One pane is open at a time. Nothing from a pane is
// ever interpreted as HTML: text goes in through textContent.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const API = "/api/plugins/kanban-herdr";
  const LS_BOARD_KEY = "hermes.kanban.selectedBoard"; // same key the kanban plugin writes
  const LIST_MS = 5000;
  const STICK_PX = 24;
  const RETRIES = 6;

  function board() {
    try { return (window.localStorage.getItem(LS_BOARD_KEY) || "").trim() || ""; } catch (_e) { return ""; }
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function btn(label, testid, title) {
    const b = el("button", "khd-btn", label);
    b.type = "button";
    if (testid) b.setAttribute("data-testid", testid);
    if (title) b.title = title;
    return b;
  }

  function postJSON(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  const panels = new WeakMap(); // drawer element -> panel

  // --- panel ---------------------------------------------------------------
  function makePanel(drawer, taskId) {
    const section = el("div", "hermes-kanban-section khd-section");
    section.setAttribute("data-khd", "1");
    const row = el("div", "hermes-kanban-section-head-row");
    const head = el("span", "hermes-kanban-section-head", "Herdr panes");
    const link = btn("link a pane", "khd-link", "Link a Herdr pane to this task");
    const refresh = btn("refresh", "khd-refresh", "Refresh the pane list");
    row.append(head, link, refresh);
    const note = el("div", "khd-note");
    note.setAttribute("data-testid", "khd-note");
    const list = el("div", "khd-list");
    list.setAttribute("data-testid", "khd-list");
    const picker = el("div", "khd-picker");
    picker.hidden = true;
    picker.setAttribute("data-testid", "khd-picker");
    section.append(row, note, picker, list);

    const p = {
      drawer: drawer, taskId: taskId, section: section, head: head, note: note, list: list, picker: picker,
      rows: new Map(), openId: null, ws: null, seq: 0, timer: 0, retry: 0, dead: false,
    };

    link.addEventListener("click", function (e) { e.stopPropagation(); togglePicker(p); });
    refresh.addEventListener("click", function (e) { e.stopPropagation(); load(p); });
    p.timer = window.setInterval(function () {
      if (!p.drawer.isConnected) { destroy(p); return; }
      load(p);
    }, LIST_MS);
    load(p);
    return p;
  }

  function destroy(p) {
    p.dead = true;
    closeStream(p);
    window.clearInterval(p.timer);
    p.section.remove();
  }

  // --- list ----------------------------------------------------------------
  function load(p) {
    const q = "?task=" + encodeURIComponent(p.taskId) + (board() ? "&board=" + encodeURIComponent(board()) : "");
    return SDK.fetchJSON(API + "/panes" + q).then(function (r) {
      if (p.dead) return;
      render(p, r);
    }).catch(function (e) {
      if (p.dead) return;
      p.note.textContent = "Could not load Herdr panes: " + ((e && e.message) || e);
      p.note.hidden = false;
    });
  }

  function statusText(pane) {
    return [pane.machine, pane.agent || "shell", pane.agent_status || "unknown"].filter(Boolean).join(" · ");
  }

  function render(p, r) {
    const panes = r.panes || [];
    const closed = r.closed || [];
    p.head.textContent = "Herdr panes (" + panes.length + ")";
    if (!r.available) {
      p.note.textContent = "Herdr isn't reachable from the dashboard host. " + (r.reason || "");
      p.note.hidden = false;
    } else if (!panes.length && !closed.length) {
      p.note.textContent = "No Herdr pane is linked to this task. Tag one with a kanban_task token, or use “link a pane”.";
      p.note.hidden = false;
    } else if (r.unreachable && r.unreachable.length) {
      p.note.textContent = "Not reachable: " + r.unreachable.join(", ") + ". Their panes aren't listed.";
      p.note.hidden = false;
    } else {
      p.note.hidden = true;
    }
    const want = new Set();
    panes.forEach(function (pane) {
      want.add(pane.pane_id);
      let row = p.rows.get(pane.pane_id);
      if (!row) { row = makeRow(p, pane); p.rows.set(pane.pane_id, row); p.list.appendChild(row.el); }
      updateRow(row, pane);
    });
    closed.forEach(function (c) {
      const id = "closed:" + c.terminal_id;
      want.add(id);
      let row = p.rows.get(id);
      if (!row) { row = makeClosedRow(c); p.rows.set(id, row); p.list.appendChild(row.el); }
    });
    p.rows.forEach(function (row, id) {
      if (want.has(id)) return;
      if (id === p.openId) { row.el.setAttribute("data-gone", "1"); return; } // keep what you're reading until you close it
      row.el.remove();
      p.rows.delete(id);
    });
  }

  function makeRow(p, pane) {
    const rowEl = el("div", "khd-pane");
    rowEl.setAttribute("data-testid", "khd-pane");
    rowEl.setAttribute("data-pane-id", pane.pane_id);
    const h = el("button", "khd-pane-head");
    h.type = "button";
    h.setAttribute("data-testid", "khd-pane-head");
    h.setAttribute("aria-expanded", "false");
    const dot = el("span", "khd-dot");
    const title = el("span", "khd-title");
    const meta = el("span", "khd-meta");
    const chev = el("span", "khd-chev", "▸");
    h.append(chev, dot, title, meta);
    const unlink = btn("unlink", "khd-unlink", "Remove this link (the pane keeps running)");
    unlink.classList.add("khd-unlink");
    rowEl.append(h, unlink);
    const row = { el: rowEl, head: h, dot: dot, title: title, meta: meta, chev: chev, body: null, pane: pane };
    h.addEventListener("click", function (e) { e.stopPropagation(); toggle(p, row); });
    unlink.addEventListener("click", function (e) {
      e.stopPropagation();
      if (!window.confirm("Unlink this pane from the task? The pane itself is not touched.")) return;
      if (p.openId === row.pane.pane_id) closeOpen(p);
      postJSON("/unlink", { task: p.taskId, board: board() || null, pane_id: row.pane.pane_id })
        .then(function () { return load(p); })
        .catch(function (err) { window.alert("Could not unlink: " + ((err && err.message) || err)); });
    });
    return row;
  }

  function updateRow(row, pane) {
    row.pane = pane;
    row.el.setAttribute("data-status", pane.agent_status || "unknown");
    row.dot.setAttribute("data-status", pane.agent_status || "unknown");
    row.title.textContent = pane.title || pane.pane_id;
    row.title.title = (pane.cwd || "") + "  " + pane.pane_id;
    row.meta.textContent = statusText(pane) + " · " + pane.pane_id.split("~").pop();
  }

  function makeClosedRow(c) {
    const rowEl = el("div", "khd-pane khd-closed");
    rowEl.setAttribute("data-testid", "khd-closed");
    const h = el("div", "khd-pane-head");
    h.append(el("span", "khd-chev", "·"), el("span", "khd-dot"), el("span", "khd-title", c.title || c.pane_id),
      el("span", "khd-meta", "closed · last " + (c.last_status || "unknown")));
    rowEl.appendChild(h);
    return { el: rowEl };
  }

  // --- accordion + live view -----------------------------------------------
  function toggle(p, row) {
    const id = row.pane.pane_id;
    if (p.openId === id) { closeOpen(p); return; }
    closeOpen(p);
    p.openId = id;
    row.el.setAttribute("data-open", "1");
    row.head.setAttribute("aria-expanded", "true");
    row.chev.textContent = "▾";
    const body = el("div", "khd-pane-body");
    const bar = el("div", "khd-bar");
    const live = el("span", "khd-live", "connecting…");
    live.setAttribute("data-testid", "khd-live");
    live.setAttribute("data-state", "connecting");
    const jump = btn("↓ latest", "khd-jump", "Scroll to the newest output");
    jump.hidden = true;
    bar.append(live, jump);
    const term = el("div", "khd-term");
    term.setAttribute("data-testid", "khd-term");
    term.tabIndex = 0;
    body.append(bar, term);
    row.el.appendChild(body);
    row.body = { el: body, live: live, jump: jump, term: term, stick: true };
    term.addEventListener("scroll", function () {
      const b = row.body; if (!b) return;
      b.stick = term.scrollHeight - term.scrollTop - term.clientHeight <= STICK_PX;
      b.jump.hidden = b.stick;
    });
    jump.addEventListener("click", function () { term.scrollTop = term.scrollHeight; });
    openStream(p, id, row);
  }

  function closeOpen(p) {
    closeStream(p);
    const id = p.openId;
    p.openId = null;
    const row = id && p.rows.get(id);
    if (!row) return;
    if (row.body) { row.body.el.remove(); row.body = null; }
    row.el.removeAttribute("data-open");
    if (row.head.setAttribute) { row.head.setAttribute("aria-expanded", "false"); row.chev.textContent = "▸"; }
    if (row.el.getAttribute("data-gone")) { row.el.remove(); p.rows.delete(id); }
  }

  function closeStream(p) {
    p.seq += 1;
    p.retry = 0;
    if (p.ws) { try { p.ws.close(); } catch (_e) { /* ignore */ } p.ws = null; }
  }

  function setLive(row, state, text) {
    if (!row.body) return;
    row.body.live.setAttribute("data-state", state);
    row.body.live.textContent = text;
  }

  function showText(row, text) {
    const b = row.body; if (!b) return;
    const wasStuck = b.stick;
    b.term.textContent = text;
    if (wasStuck) b.term.scrollTop = b.term.scrollHeight;
  }

  function openStream(p, paneId, row) {
    const seq = ++p.seq;
    setLive(row, "connecting", "connecting…");
    SDK.buildWsUrl(API + "/stream", { pane: paneId }).then(function (url) {
      if (p.seq !== seq || p.dead) return;
      let ended = false;
      const ws = new WebSocket(url);
      p.ws = ws;
      ws.onopen = function () { p.retry = 0; };
      ws.onmessage = function (ev) {
        if (p.seq !== seq) return;
        let msg; try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        if (msg.type === "snapshot") {
          showText(row, msg.text || "");
          setLive(row, "live", "live · " + (msg.agent_status || "unknown"));
          if (row.pane) { row.dot.setAttribute("data-status", msg.agent_status || "unknown"); }
        } else if (msg.type === "closed") {
          ended = true;
          setLive(row, "ended", "pane closed");
        } else if (msg.type === "error") {
          setLive(row, "error", msg.error || "error");
          if (/too many/.test(msg.error || "")) ended = true;
        }
      };
      ws.onclose = function (ev) {
        if (p.seq !== seq || p.dead || ended) return;
        if (ev && ev.code === 1008) { setLive(row, "error", "not authorised — reload the page"); return; }
        if (p.retry >= RETRIES) { setLive(row, "error", "disconnected"); return; }
        p.retry += 1;
        setLive(row, "connecting", "reconnecting…");
        window.setTimeout(function () { if (p.seq === seq && !p.dead) openStream(p, paneId, row); }, Math.min(500 * Math.pow(2, p.retry), 8000));
      };
    }).catch(function () { setLive(row, "error", "could not open the live view"); });
  }

  // --- manual link picker --------------------------------------------------
  function togglePicker(p) {
    if (!p.picker.hidden) { p.picker.hidden = true; return; }
    p.picker.hidden = false;
    p.picker.replaceChildren(el("div", "khd-note", "Loading panes…"));
    SDK.fetchJSON(API + "/all-panes").then(function (r) {
      if (p.dead) return;
      if (!r.available) { p.picker.replaceChildren(el("div", "khd-note", "Herdr isn't reachable from the dashboard host.")); return; }
      const items = (r.panes || []).map(function (pane) {
        const b = el("button", "khd-pick");
        b.type = "button";
        b.setAttribute("data-testid", "khd-pick");
        b.setAttribute("data-pane-id", pane.pane_id);
        b.append(el("span", "khd-pick-title", pane.title || pane.pane_id),
          el("span", "khd-meta", statusText(pane) + " · " + (pane.cwd || "") + (pane.task ? " · linked to " + pane.task : "")));
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          b.disabled = true;
          postJSON("/link", { task: p.taskId, board: board() || null, pane_id: pane.pane_id })
            .then(function () { p.picker.hidden = true; return load(p); })
            .catch(function (err) { b.disabled = false; window.alert("Could not link: " + ((err && err.message) || err)); });
        });
        return b;
      });
      p.picker.replaceChildren.apply(p.picker, items.length ? items : [el("div", "khd-note", "No panes found.")]);
    }).catch(function (e) { p.picker.replaceChildren(el("div", "khd-note", "Could not list panes: " + ((e && e.message) || e))); });
  }

  // --- drawer decoration ---------------------------------------------------
  function taskIdOf(drawer) {
    const s = drawer.querySelector(".hermes-kanban-drawer-head > span");
    return s ? s.textContent.trim() : "";
  }

  function decorate() {
    document.querySelectorAll(".hermes-kanban-drawer").forEach(function (drawer) {
      const taskId = taskIdOf(drawer);
      const body = drawer.querySelector(":scope > .hermes-kanban-drawer-body");
      if (!taskId || !body) return;
      let p = panels.get(drawer);
      if (p && p.taskId !== taskId) { destroy(p); panels.delete(drawer); p = null; }
      if (!p) { p = makePanel(drawer, taskId); panels.set(drawer, p); }
      // Keep it as the last section; React may have appended new ones after it.
      if (p.section.parentElement !== body || p.section.nextElementSibling) body.appendChild(p.section);
    });
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(function () { scheduled = false; decorate(); });
  }

  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  schedule();

  window.__HERMES_PLUGINS__.register("kanban-herdr", function () { return null; });
})();
