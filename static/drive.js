/* Drive UI: folders, any-file uploads (chunked + resumable), previews,
 * rename/move/star/trash, and search over names + document text. */
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

  // ------------------------------------------------------------------ utils
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function fmtSize(n) {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
  }
  // Timestamps are naive UTC; mark them Z so the browser converts to local.
  function relTime(iso) {
    if (!iso) return "";
    const then = new Date(String(iso).replace(" ", "T") + "Z");
    if (isNaN(then)) return "";
    const s = (Date.now() - then.getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 7 * 86400) return Math.floor(s / 86400) + "d ago";
    const o = { month: "short", day: "numeric" };
    if (then.getFullYear() !== new Date().getFullYear()) o.year = "numeric";
    return then.toLocaleDateString(undefined, o);
  }
  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 3500);
  }
  async function api(url, opts = {}) {
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers = { ...(opts.headers || {}), "Content-Type": "application/json" };
      delete opts.json;
    }
    const res = await fetch(url, opts);
    if (res.status === 401) { location = "/login"; throw new Error("unauthorized"); }
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) { const e = new Error(data.error || res.statusText); e.status = res.status; e.data = data; throw e; }
    return data;
  }
  const fail = (e) => { if (e.message !== "unauthorized") toast(e.message || "Something went wrong.", "error"); };

  const ICON = {
    folder: "📁", note: "📝", image: "🖼️", video: "🎬", audio: "🎵", pdf: "📕",
    doc: "📘", sheet: "📗", slides: "📙", archive: "🗜️", text: "📄", other: "📦",
  };
  const kindOf = (it) => it.type === "folder" ? "folder" : it.type === "note" ? "note" : it.kind;
  const keyOf = (it) => it.type + ":" + it.id;

  // ------------------------------------------------------------------ theme
  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
  const themeIcon = () => { $("#theme-toggle").textContent = isDark() ? "☀️" : "🌙"; };
  $("#theme-toggle").addEventListener("click", () => {
    const dark = !isDark();
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (_) {}
    themeIcon();
  });
  themeIcon();

  // ------------------------------------------------------------------ state
  const pref = (k, d) => { try { return localStorage.getItem(k) || d; } catch (_) { return d; } };
  const setPref = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
  const state = {
    view: "drive",        // drive | recent | starred | trash | search
    folder: null,         // current folder id (drive view)
    crumbs: [],
    items: [],
    query: "",
    layout: pref("driveLayout", "grid"),
    sort: pref("driveSort", "name"),
    selected: new Set(),
  };
  $("#sort").value = state.sort;

  // ------------------------------------------------------------------ routing
  function route() {
    const h = location.hash.replace(/^#\/?/, "");
    const [head, ...rest] = h.split("/");
    state.selected.clear();
    closeMenus();
    $("#sidebar").classList.remove("open");
    if (head === "folder" && rest[0]) { state.view = "drive"; state.folder = parseInt(rest[0], 10); }
    else if (["recent", "starred", "trash"].includes(head)) { state.view = head; state.folder = null; }
    else if (head === "tag" && rest[0]) { state.view = "tag"; state.tag = decodeURIComponent(rest.join("/")); }
    else if (head === "search") { state.view = "search"; state.query = decodeURIComponent(rest.join("/")); }
    else { state.view = "drive"; state.folder = null; }
    if (state.view !== "search") $("#search-input").value = "";
    else if (document.activeElement !== $("#search-input")) $("#search-input").value = state.query;
    $$(".d-nav a").forEach((a) => a.classList.toggle("active",
      a.dataset.nav === state.view && !(state.view === "drive" && state.folder !== null)));
    $$("#tag-nav a").forEach((a) => a.classList.toggle("active", state.view === "tag" && a.dataset.tag === state.tag));
    load();
  }
  window.addEventListener("hashchange", route);
  const goFolder = (id) => { location.hash = id == null ? "#/" : "#/folder/" + id; };

  // ------------------------------------------------------------------ loading
  let loadSeq = 0;
  async function load() {
    const seq = ++loadSeq;
    const box = $("#items");
    box.innerHTML = '<p class="muted d-pad">Loading…</p>';
    $("#content-results").classList.add("hidden");
    $("#trash-note").classList.toggle("hidden", state.view !== "trash");
    try {
      let data;
      if (state.view === "drive") {
        data = await api("/api/drive" + (state.folder != null ? "?folder=" + state.folder : ""));
        state.crumbs = data.breadcrumbs;
      } else if (state.view === "tag") {
        data = await api("/api/drive/tag?t=" + encodeURIComponent(state.tag));
      } else if (state.view === "search") {
        data = await api("/api/drive/search?q=" + encodeURIComponent(state.query));
        searchContent(state.query, seq);
      } else {
        data = await api("/api/drive/" + state.view);
        if (data.retention_days) $("#trash-days").textContent = data.retention_days;
      }
      if (seq !== loadSeq) return;
      state.items = data.items;
      render();
    } catch (e) {
      if (seq !== loadSeq) return;
      if (e.status === 404) { toast("That folder no longer exists.", "error"); goFolder(null); return; }
      box.innerHTML = '<p class="status error d-pad">Could not load. ' + esc(e.message) + "</p>";
    }
    loadStorage();
    loadTags();
  }

  async function loadTags() {
    try {
      const d = await api("/tags");
      const box = $("#tag-nav");
      box.innerHTML = d.tags.length
        ? '<h4>Tags</h4>' + d.tags.map((t) =>
            `<a href="#/tag/${encodeURIComponent(t.tag)}" data-tag="${esc(t.tag)}"><span># ${esc(t.tag)}</span><span class="muted">${t.count}</span></a>`).join("")
        : "";
      $$("#tag-nav a").forEach((a) => a.classList.toggle("active", state.view === "tag" && a.dataset.tag === state.tag));
    } catch (_) { /* non-critical */ }
  }

  async function loadStorage() {
    try {
      const s = await api("/api/storage");
      const cap = s.used + s.disk_free;
      $("#storage-fill").style.width = Math.max(1, Math.min(100, (s.used / cap) * 100)) + "%";
      $("#storage-text").textContent =
        `${fmtSize(s.used)} used · ${fmtSize(s.disk_free)} free · ${s.files} file${s.files === 1 ? "" : "s"}`;
    } catch (_) { /* non-critical */ }
  }

  // ------------------------------------------------------------------ rendering
  const SORTS = {
    name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }),
    updated: (a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""),
    size: (a, b) => (b.size || 0) - (a.size || 0),
  };
  function sortedItems() {
    if (state.view === "recent" || state.view === "trash") return state.items.slice();
    const s = SORTS[state.sort] || SORTS.name;
    return state.items.slice().sort((a, b) =>
      (a.type === "folder") !== (b.type === "folder") ? (a.type === "folder" ? -1 : 1) : s(a, b));
  }

  function renderCrumbs() {
    const c = $("#crumbs");
    const titles = { recent: "Recent", starred: "Starred", trash: "Trash" };
    if (state.view === "tag") { c.innerHTML = `<span># ${esc(state.tag)}</span>`; return; }
    if (state.view === "search") { c.innerHTML = `<span>Search results for “${esc(state.query)}”</span>`; return; }
    if (titles[state.view]) { c.innerHTML = `<span>${titles[state.view]}</span>`; return; }
    const parts = [`<a href="#/" data-drop-folder="">My Drive</a>`];
    state.crumbs.forEach((f, i) => {
      parts.push(i === state.crumbs.length - 1
        ? `<span>${esc(f.name)}</span>`
        : `<a href="#/folder/${f.id}" data-drop-folder="${f.id}">${esc(f.name)}</a>`);
    });
    c.innerHTML = parts.join('<span class="sep">›</span>');
  }

  function thumbHtml(it) {
    const k = kindOf(it);
    if (it.type === "file" && it.has_thumb) {
      return `<img loading="lazy" src="/files/${it.id}/thumb" alt="">` +
        (k === "pdf" ? `<span class="d-kind-chip">PDF</span>` : "");
    }
    if (it.type === "note" && it.excerpt) {
      return `<div class="d-note-excerpt">${esc(it.excerpt)}</div>`;
    }
    return `<span class="d-big-icon">${ICON[k] || ICON.other}</span>`;
  }

  function metaText(it) {
    if (state.view === "trash") return "Trashed " + relTime(it.trashed_at);
    if (it.type === "folder") return it.item_count != null ? `${it.item_count} item${it.item_count === 1 ? "" : "s"}` : "Folder";
    if (it.type === "note") return relTime(it.updated_at);
    return fmtSize(it.size) + " · " + relTime(it.updated_at);
  }

  function render() {
    renderCrumbs();
    const box = $("#items");
    const items = sortedItems();
    state.visible = items;
    box.className = "d-items " + (state.layout === "list" ? "d-list" : "d-grid");
    $("#view-toggle").textContent = state.layout === "list" ? "▦" : "☷";
    if (!items.length) {
      const empty = {
        drive: state.folder == null
          ? "Your drive is empty. Drop files here, or use <b>＋ New</b> to upload photos, videos, PDFs, or write a note."
          : "This folder is empty. Drop files here or use <b>＋ New</b>.",
        recent: "Nothing yet.", tag: "Nothing has this tag.", starred: "Star files and folders to find them here quickly.",
        trash: "Trash is empty.", search: "No files or folders match that name.",
      };
      box.className = "d-items";
      box.innerHTML = `<div class="d-empty">${empty[state.view] || ""}</div>`;
      updateSelbar();
      return;
    }
    if (state.layout === "list") {
      box.innerHTML = `<div class="d-row d-row-head"><span></span><span>Name</span><span>Modified</span><span>Size</span><span></span></div>` +
        items.map((it) => `
        <div class="d-row d-item${state.selected.has(keyOf(it)) ? " sel" : ""}" data-key="${keyOf(it)}" tabindex="0"
             draggable="${state.view !== "trash"}" ${it.type === "folder" ? `data-drop-folder="${it.id}"` : ""}>
          <span class="d-row-icon">${ICON[kindOf(it)] || ICON.other}</span>
          <span class="d-row-name">${esc(it.name)}${it.starred ? ' <span class="d-star">★</span>' : ""}</span>
          <span class="muted">${state.view === "trash" ? relTime(it.trashed_at) : relTime(it.updated_at)}</span>
          <span class="muted">${it.type === "file" ? fmtSize(it.size) : "—"}</span>
          <button class="d-more" aria-label="More actions">⋮</button>
        </div>`).join("");
    } else {
      const card = (it) => `
        <div class="d-card d-item d-k-${kindOf(it)}${state.selected.has(keyOf(it)) ? " sel" : ""}" data-key="${keyOf(it)}" tabindex="0"
             draggable="${state.view !== "trash"}" ${it.type === "folder" ? `data-drop-folder="${it.id}"` : ""}>
          <div class="d-thumb">${thumbHtml(it)}</div>
          <div class="d-card-foot">
            <span class="d-check" aria-hidden="true">✓</span>
            <span class="d-card-text">
              <span class="d-card-name" title="${esc(it.name)}">${it.type === "folder" ? "" : (ICON[kindOf(it)] || "") + " "}${esc(it.name)}</span>
              <span class="d-card-meta">${it.starred ? '<span class="d-star">★</span> ' : ""}${esc(metaText(it))}${(it.tags || []).map((t) => ` <a class="d-tag" href="#/tag/${encodeURIComponent(t)}">#${esc(t)}</a>`).join("")}</span>
            </span>
            <button class="d-more" aria-label="More actions">⋮</button>
          </div>
        </div>`;
      // Drive-style: compact folder tiles on top, then files and notes.
      const folders = items.filter((i) => i.type === "folder");
      const rest = items.filter((i) => i.type !== "folder");
      box.className = "d-items";
      box.innerHTML =
        (folders.length ? `${rest.length ? '<h4 class="d-section">Folders</h4>' : ""}<div class="d-grid d-grid-folders">${folders.map(card).join("")}</div>` : "") +
        (rest.length ? `${folders.length ? '<h4 class="d-section">Files</h4>' : ""}<div class="d-grid">${rest.map(card).join("")}</div>` : "");
    }
    updateSelbar();
  }

  const itemByKey = (k) => state.items.find((it) => keyOf(it) === k);

  // ------------------------------------------------------------------ item interactions
  $("#items").addEventListener("click", (e) => {
    const el = e.target.closest(".d-item");
    if (!el) return;
    const it = itemByKey(el.dataset.key);
    if (!it) return;
    if (e.target.closest(".d-tag")) return;   // tag chip: follow its link
    if (e.target.closest(".d-more")) { e.stopPropagation(); openCtx(it, e.target.closest(".d-more")); return; }
    if (e.target.closest(".d-check") || e.ctrlKey || e.metaKey || e.shiftKey || state.selected.size) {
      toggleSelect(it); return;
    }
    openItem(it);
  });
  $("#items").addEventListener("keydown", (e) => {
    const el = e.target.closest(".d-item");
    if (el && e.key === "Enter") { const it = itemByKey(el.dataset.key); if (it) openItem(it); }
    if (el && e.key === " ") { e.preventDefault(); const it = itemByKey(el.dataset.key); if (it) toggleSelect(it); }
  });
  $("#items").addEventListener("contextmenu", (e) => {
    const el = e.target.closest(".d-item");
    if (!el) return;
    e.preventDefault();
    const it = itemByKey(el.dataset.key);
    if (it) openCtx(it, null, e.clientX, e.clientY);
  });

  function openItem(it) {
    if (state.view === "trash") { toast("Restore it from the trash to open it."); return; }
    if (it.type === "folder") goFolder(it.id);
    else if (it.type === "note") location = "/editor/" + it.id;
    else openPreview(it);
  }

  function toggleSelect(it) {
    const k = keyOf(it);
    state.selected.has(k) ? state.selected.delete(k) : state.selected.add(k);
    const el = $(`.d-item[data-key="${CSS.escape(k)}"]`);
    if (el) el.classList.toggle("sel", state.selected.has(k));
    updateSelbar();
  }
  function selectedItems() { return [...state.selected].map(itemByKey).filter(Boolean); }
  function updateSelbar() {
    const n = state.selected.size;
    $("#selbar").classList.toggle("hidden", !n);
    $("#sel-count").textContent = `${n} selected`;
    const trash = state.view === "trash";
    $$("#selbar [data-sel]").forEach((b) => {
      const a = b.dataset.sel;
      const show = a === "clear" || (trash ? ["restore", "forever"].includes(a) : !["restore", "forever"].includes(a));
      b.classList.toggle("hidden", !show);
    });
  }
  $("#selbar").addEventListener("click", async (e) => {
    const a = e.target.closest("[data-sel]")?.dataset.sel;
    if (!a) return;
    const items = selectedItems();
    if (a === "clear") { state.selected.clear(); render(); return; }
    if (a === "download") {
      const files = items.filter((i) => i.type === "file");
      if (!files.length) { toast("Select files to download (folders and notes aren't included)."); return; }
      files.forEach((f, i) => setTimeout(() => triggerDownload(f), i * 400));
      return;
    }
    if (a === "move") { moveItems(items); return; }
    if (a === "star") {
      const to = !items.every((i) => i.starred);
      await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}`, { method: "PATCH", json: { starred: to } }))).catch(fail);
    }
    if (a === "trash") await trashItems(items);
    if (a === "restore") await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}/restore`, { method: "POST" }))).catch(fail);
    if (a === "forever") {
      if (!(await confirmDlg(`Delete ${items.length} item${items.length === 1 ? "" : "s"} forever?`, "This can't be undone.", "Delete forever"))) return;
      await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}`, { method: "DELETE" }))).catch(fail);
    }
    state.selected.clear();
    load();
  });

  function triggerDownload(f) {
    const a = document.createElement("a");
    a.href = `/files/${f.id}/download`;
    a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ------------------------------------------------------------------ context menu
  function closeMenus() {
    $("#ctx").classList.add("hidden");
    $("#new-menu").classList.add("hidden");
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".d-menu") && !e.target.closest("#new-btn") && !e.target.closest(".d-more")) closeMenus();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });

  function openCtx(it, anchor, x, y) {
    const m = $("#ctx");
    const acts = [];
    if (state.view === "trash") {
      acts.push(["restore", "↺ Restore"], ["forever", "✕ Delete forever"]);
    } else {
      acts.push(["open", it.type === "folder" ? "📂 Open" : it.type === "note" ? "✏️ Open in editor" : "👁 Preview"]);
      if (it.type === "file") acts.push(["download", "⬇ Download"]);
      if (it.type === "note") acts.push(["pdf", "⬇ Download as PDF"], ["docx", "⬇ Download as .docx"]);
      acts.push(["rename", "✎ Rename"], ["move", "➜ Move to…"],
        ["star", it.starred ? "☆ Remove star" : "★ Add star"], ["select", "☑ Select"],
        ["sep"], ["trash", "🗑 Move to trash"]);
      if (state.view !== "drive") acts.splice(1, 0, ["locate", "📍 Show file location"]);
    }
    m.innerHTML = acts.map(([a, label]) => a === "sep" ? "<hr>" : `<button data-act="${a}" role="menuitem">${label}</button>`).join("");
    m.classList.remove("hidden");
    const r = anchor ? anchor.getBoundingClientRect() : { left: x, right: x, bottom: y, top: y };
    const mw = m.offsetWidth, mh = m.offsetHeight;
    let left = anchor ? r.right - mw : r.left;
    let top = r.bottom + 4;
    if (top + mh > innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    left = Math.max(8, Math.min(left, innerWidth - mw - 8));
    m.style.left = left + "px"; m.style.top = top + "px";
    m.onclick = (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      closeMenus();
      itemAction(it, act);
    };
  }

  async function itemAction(it, act) {
    try {
      if (act === "open") openItem(it);
      else if (act === "download") triggerDownload(it);
      else if (act === "pdf" || act === "docx") location = `/export/${it.id}/${act}`;
      else if (act === "rename") {
        const name = await promptDlg("Rename", it.name);
        if (name && name !== it.name) {
          await api(`/api/item/${it.type}/${it.id}`, { method: "PATCH", json: { name } });
          load();
        }
      } else if (act === "move") moveItems([it]);
      else if (act === "star") {
        await api(`/api/item/${it.type}/${it.id}`, { method: "PATCH", json: { starred: !it.starred } });
        load();
      } else if (act === "select") toggleSelect(it);
      else if (act === "trash") { await trashItems([it]); load(); }
      else if (act === "locate") goFolder(it.type === "folder" ? it.parent_id : it.folder_id);
      else if (act === "restore") {
        const r = await api(`/api/item/${it.type}/${it.id}/restore`, { method: "POST" });
        toast(r.folder_id == null && (it.folder_id || it.parent_id) ? "Restored to My Drive (its folder is gone)." : "Restored.", "ok");
        load();
      } else if (act === "forever") {
        if (await confirmDlg(`Delete “${it.name}” forever?`, "This can't be undone.", "Delete forever")) {
          await api(`/api/item/${it.type}/${it.id}`, { method: "DELETE" });
          load();
        }
      }
    } catch (e) { fail(e); }
  }

  async function trashItems(items) {
    try {
      await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}/trash`, { method: "POST" })));
      const t = document.createElement("div");
      t.className = "toast show";
      t.innerHTML = `Moved ${items.length === 1 ? "“" + esc(items[0].name) + "”" : items.length + " items"} to trash. <button class="link-btn">Undo</button>`;
      $("#toasts").appendChild(t);
      $("button", t).onclick = async () => {
        t.remove();
        await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}/restore`, { method: "POST" }))).catch(fail);
        load();
      };
      setTimeout(() => t.remove(), 6000);
    } catch (e) { fail(e); }
  }

  $("#empty-trash").addEventListener("click", async () => {
    if (!(await confirmDlg("Empty trash?", "Everything in the trash will be deleted forever.", "Empty trash"))) return;
    try { const r = await api("/api/drive/trash/empty", { method: "POST" }); toast(`Deleted ${r.purged} item${r.purged === 1 ? "" : "s"} forever.`, "ok"); load(); }
    catch (e) { fail(e); }
  });

  // ------------------------------------------------------------------ dialogs
  function dialog({ title, bodyHtml, okLabel = "OK", danger = false, onOpen, collect }) {
    return new Promise((resolve) => {
      const wrap = $("#dialog");
      $("#dlg-title").textContent = title;
      $("#dlg-body").innerHTML = bodyHtml || "";
      $("#dlg-ok").textContent = okLabel;
      $("#dlg-ok").classList.toggle("danger", danger);
      wrap.classList.remove("hidden");
      const done = (v) => {
        wrap.classList.add("hidden");
        $("#dialog-form").onsubmit = null; $("#dlg-cancel").onclick = null; wrap.onclick = null;
        document.removeEventListener("keydown", onKey);
        resolve(v);
      };
      const onKey = (e) => { if (e.key === "Escape") done(null); };
      document.addEventListener("keydown", onKey);
      $("#dialog-form").onsubmit = (e) => { e.preventDefault(); done(collect ? collect() : true); };
      $("#dlg-cancel").onclick = () => done(null);
      wrap.onclick = (e) => { if (e.target === wrap) done(null); };
      if (onOpen) onOpen();
      else $("#dlg-ok").focus();
    });
  }
  function promptDlg(title, value = "", okLabel = "Save") {
    return dialog({
      title, okLabel,
      bodyHtml: `<input type="text" id="dlg-input" value="${esc(value)}" maxlength="255">`,
      onOpen: () => {
        const i = $("#dlg-input"); i.focus();
        const dot = value.lastIndexOf(".");
        i.setSelectionRange(0, dot > 0 ? dot : value.length);
      },
      collect: () => $("#dlg-input").value.trim(),
    });
  }
  function confirmDlg(title, text, okLabel = "OK") {
    return dialog({ title, okLabel, danger: true, bodyHtml: `<p class="muted">${esc(text)}</p>` });
  }

  // Folder picker: browse the tree and pick a destination.
  async function moveItems(items) {
    if (!items.length) return;
    const moving = new Set(items.filter((i) => i.type === "folder").map((i) => i.id));
    let cur = state.view === "drive" ? state.folder : null;
    const renderPicker = async () => {
      const box = $("#mv-list");
      box.innerHTML = '<p class="muted">Loading…</p>';
      try {
        const d = await api("/api/drive" + (cur != null ? "?folder=" + cur : ""));
        const crumbs = [`<a href="#" data-go="">My Drive</a>`].concat(
          d.breadcrumbs.map((c) => `<a href="#" data-go="${c.id}">${esc(c.name)}</a>`));
        $("#mv-crumbs").innerHTML = crumbs.join(" › ");
        const folders = d.items.filter((i) => i.type === "folder")
          .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
        box.innerHTML = folders.length
          ? folders.map((f) => moving.has(f.id)
            ? `<div class="mv-row disabled">📁 ${esc(f.name)} <span class="muted">(moving)</span></div>`
            : `<button type="button" class="mv-row" data-go="${f.id}">📁 ${esc(f.name)} <span class="muted">›</span></button>`).join("")
          : '<p class="muted">No subfolders.</p>';
      } catch (e) { box.innerHTML = `<p class="status error">${esc(e.message)}</p>`; }
    };
    const target = await dialog({
      title: `Move ${items.length === 1 ? "“" + items[0].name + "”" : items.length + " items"}`,
      okLabel: "Move here",
      bodyHtml: `<div id="mv-crumbs" class="mv-crumbs"></div><div id="mv-list" class="mv-list"></div>
        <button type="button" id="mv-new" class="link-btn">＋ New folder here</button>`,
      onOpen: () => {
        renderPicker();
        $("#dlg-body").onclick = async (e) => {
          const go = e.target.closest("[data-go]");
          if (go) { e.preventDefault(); cur = go.dataset.go === "" ? null : parseInt(go.dataset.go, 10); renderPicker(); return; }
          if (e.target.id === "mv-new") {
            const name = window.prompt("Folder name", "New folder");
            if (!name) return;
            try { const f = await api("/api/folders", { method: "POST", json: { name, parent_id: cur } }); cur = f.id; renderPicker(); }
            catch (err) { fail(err); }
          }
        };
      },
      collect: () => ({ folder: cur }),
    });
    $("#dlg-body").onclick = null;
    if (!target) return;
    try {
      await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}`, { method: "PATCH", json: { folder_id: target.folder } })));
      toast(`Moved ${items.length} item${items.length === 1 ? "" : "s"}.`, "ok");
      state.selected.clear();
      load();
    } catch (e) { fail(e); load(); }
  }

  // ------------------------------------------------------------------ new menu
  $("#new-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#ctx").classList.add("hidden");
    $("#new-menu").classList.toggle("hidden");
  });
  const currentDest = () => (state.view === "drive" ? state.folder : null);
  $("#new-menu").addEventListener("click", async (e) => {
    const a = e.target.closest("[data-new]")?.dataset.new;
    if (!a) return;
    closeMenus();
    if (a === "folder") {
      const name = await promptDlg("New folder", "Untitled folder", "Create");
      if (!name) return;
      try { await api("/api/folders", { method: "POST", json: { name, parent_id: currentDest() } }); load(); }
      catch (err) { fail(err); }
    } else if (a === "note") {
      location = "/editor" + (currentDest() != null ? "?folder=" + currentDest() : "");
    } else if (a === "files") $("#pick-files").click();
    else if (a === "dir") $("#pick-dir").click();
  });
  $("#pick-files").addEventListener("change", (e) => {
    enqueue([...e.target.files].map((f) => ({ file: f, path: "" })), currentDest());
    e.target.value = "";
  });
  $("#pick-dir").addEventListener("change", (e) => {
    enqueue([...e.target.files].map((f) => ({ file: f, path: (f.webkitRelativePath || "").split("/").slice(0, -1).join("/") })), currentDest());
    e.target.value = "";
  });

  // ------------------------------------------------------------------ drag & drop (upload + move)
  let dragDepth = 0;
  let draggingItems = null;   // internal move drag
  const main = $("#main");
  main.addEventListener("dragenter", (e) => {
    if (draggingItems || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    dragDepth++;
    $("#drop-target").textContent = state.view === "drive" && state.crumbs.length ? state.crumbs[state.crumbs.length - 1].name : "My Drive";
    $("#drop-overlay").classList.remove("hidden");
  });
  main.addEventListener("dragleave", () => {
    if (draggingItems) return;
    if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").classList.add("hidden"); }
  });
  main.addEventListener("dragover", (e) => {
    if (draggingItems) {
      const t = e.target.closest("[data-drop-folder]");
      $$(".drop-hover").forEach((el) => el !== t && el.classList.remove("drop-hover"));
      if (t) { e.preventDefault(); t.classList.add("drop-hover"); }
      return;
    }
    if (e.dataTransfer.types.includes("Files")) e.preventDefault();
  });
  main.addEventListener("drop", async (e) => {
    $$(".drop-hover").forEach((el) => el.classList.remove("drop-hover"));
    if (draggingItems) {
      const t = e.target.closest("[data-drop-folder]");
      if (!t) return;
      e.preventDefault();
      const dest = t.dataset.dropFolder === "" ? null : parseInt(t.dataset.dropFolder, 10);
      const items = draggingItems.filter((i) => !(i.type === "folder" && i.id === dest));
      draggingItems = null;
      if (!items.length) return;
      try {
        await Promise.all(items.map((i) => api(`/api/item/${i.type}/${i.id}`, { method: "PATCH", json: { folder_id: dest } })));
        toast(`Moved ${items.length} item${items.length === 1 ? "" : "s"}.`, "ok");
        state.selected.clear();
      } catch (err) { fail(err); }
      load();
      return;
    }
    e.preventDefault();
    dragDepth = 0;
    $("#drop-overlay").classList.add("hidden");
    if (state.view === "trash") { toast("Can't upload into the trash.", "error"); return; }
    const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    let list;
    if (entries.length) list = (await Promise.all(entries.map((en) => walkEntry(en, "")))).flat();
    else list = [...e.dataTransfer.files].map((f) => ({ file: f, path: "" }));
    enqueue(list, currentDest());
  });
  // Recursively read a dropped folder.
  async function walkEntry(entry, prefix) {
    if (entry.isFile) {
      return new Promise((res) => entry.file((f) => res([{ file: f, path: prefix }]), () => res([])));
    }
    if (!entry.isDirectory) return [];
    const dirPath = prefix ? prefix + "/" + entry.name : entry.name;
    const reader = entry.createReader();
    const all = [];
    for (;;) {
      const batch = await new Promise((res) => reader.readEntries(res, () => res([])));
      if (!batch.length) break;
      all.push(...batch);
    }
    const nested = await Promise.all(all.map((en) => walkEntry(en, dirPath)));
    const out = nested.flat();
    if (!out.length) out.push({ dir: dirPath });   // keep empty folders
    return out;
  }
  // Internal drag: move items onto folders / breadcrumbs.
  $("#items").addEventListener("dragstart", (e) => {
    const el = e.target.closest(".d-item");
    if (!el) return;
    const it = itemByKey(el.dataset.key);
    if (!it) return;
    draggingItems = state.selected.has(keyOf(it)) ? selectedItems() : [it];
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", it.name);
  });
  document.addEventListener("dragend", () => {
    draggingItems = null;
    $$(".drop-hover").forEach((el) => el.classList.remove("drop-hover"));
  });
  $("#crumbs").addEventListener("dragover", (e) => {
    const t = e.target.closest("[data-drop-folder]");
    if (draggingItems && t) { e.preventDefault(); t.classList.add("drop-hover"); }
  });
  $("#crumbs").addEventListener("dragleave", (e) => e.target.closest?.("[data-drop-folder]")?.classList.remove("drop-hover"));

  // ------------------------------------------------------------------ uploads (chunked, resumable)
  const queue = [];
  let active = 0;
  const MAX_PARALLEL = 3;
  let nextTaskId = 1;
  let upHideTimer = null;
  const folderCache = new Map();   // "<dest>|a/b" -> folder id

  async function ensureFolderPath(dest, path) {
    if (!path) return dest;
    let parent = dest;
    let acc = "";
    for (const part of path.split("/").filter(Boolean)) {
      acc = acc ? acc + "/" + part : part;
      const key = (dest ?? "") + "|" + acc;
      if (!folderCache.has(key)) {
        folderCache.set(key, api("/api/folders", { method: "POST", json: { name: part, parent_id: parent, reuse: true } }).then((f) => f.id));
      }
      try { parent = await folderCache.get(key); }
      catch (e) { folderCache.delete(key); throw e; }
    }
    return parent;
  }

  function enqueue(list, dest) {
    const files = list.filter((x) => x.file);
    const dirs = list.filter((x) => x.dir);
    dirs.forEach((d) => ensureFolderPath(dest, d.dir).then(refreshSoon).catch(fail));
    if (!files.length) { refreshSoon(); return; }
    $("#uploads").classList.remove("hidden", "collapsed");
    for (const { file, path } of files) {
      const t = { id: nextTaskId++, file, path, dest, sent: 0, status: "queued", xhr: null, uploadId: null };
      queue.push(t);
      $("#up-list").insertAdjacentHTML("beforeend", `
        <div class="up-row" id="up-${t.id}">
          <span class="up-icon">${ICON[guessKind(file)] || ICON.other}</span>
          <span class="up-main">
            <span class="up-name" title="${esc((path ? path + "/" : "") + file.name)}">${esc(file.name)}</span>
            <span class="up-bar"><span></span></span>
            <span class="up-meta muted">Waiting… · ${fmtSize(file.size)}</span>
          </span>
          <button class="d-x up-cancel" aria-label="Cancel">✕</button>
        </div>`);
      $(`#up-${t.id} .up-cancel`).onclick = () => cancelTask(t);
    }
    updateUpTitle();
    pump();
  }
  function guessKind(f) {
    const t = f.type || "";
    if (t.startsWith("image/")) return "image";
    if (t.startsWith("video/")) return "video";
    if (t.startsWith("audio/")) return "audio";
    if (t === "application/pdf") return "pdf";
    return "other";
  }
  function pump() {
    while (active < MAX_PARALLEL) {
      const t = queue.find((x) => x.status === "queued");
      if (!t) break;
      active++;
      t.status = "uploading";
      runTask(t).finally(() => { active--; updateUpTitle(); pump(); });
    }
  }
  function setRow(t, pct, meta, cls) {
    const row = $("#up-" + t.id);
    if (!row) return;
    if (pct != null) $(".up-bar span", row).style.width = pct + "%";
    if (meta != null) $(".up-meta", row).textContent = meta;
    if (cls) row.className = "up-row " + cls;
  }
  function updateUpTitle() {
    const left = queue.filter((t) => t.status === "queued" || t.status === "uploading").length;
    const failed = queue.filter((t) => t.status === "error").length;
    const done = queue.filter((t) => t.status === "done").length;
    clearTimeout(upHideTimer);
    if (!left && !failed && done) upHideTimer = setTimeout(() => $("#uploads").classList.add("collapsed"), 4000);
    $("#up-title").textContent = left
      ? `Uploading ${left} item${left === 1 ? "" : "s"}…`
      : `${done} upload${done === 1 ? "" : "s"} complete` + (failed ? ` · ${failed} failed` : "");
  }

  function putChunk(t, blob, offset) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      t.xhr = xhr;
      xhr.open("PUT", `/api/uploads/${t.uploadId}?offset=${offset}`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => {
        const sent = offset + e.loaded;
        const now = Date.now();
        if (!t.lastTick || now - t.lastTick > 400) {
          const rate = t.lastTick ? (sent - t.lastSent) / ((now - t.lastTick) / 1000) : 0;
          t.lastTick = now; t.lastSent = sent;
          t.rate = rate ? (t.rate ? t.rate * 0.7 + rate * 0.3 : rate) : t.rate;
        }
        const eta = t.rate ? Math.round((t.file.size - sent) / t.rate) : null;
        setRow(t, (sent / Math.max(1, t.file.size)) * 100,
          `${fmtSize(sent)} of ${fmtSize(t.file.size)}` + (t.rate ? ` · ${fmtSize(t.rate)}/s` : "") +
          (eta != null && eta > 2 ? ` · ${eta > 90 ? Math.round(eta / 60) + " min" : eta + " s"} left` : ""));
      };
      xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status === 401) { location = "/login"; return reject(new Error("unauthorized")); }
        if (xhr.status === 409 && typeof d.received === "number") return resolve(d.received);
        if (xhr.status >= 200 && xhr.status < 300) return resolve(d.received);
        const err = new Error(d.error || `HTTP ${xhr.status}`); err.status = xhr.status; reject(err);
      };
      xhr.onerror = () => reject(Object.assign(new Error("Network error"), { retry: true }));
      xhr.onabort = () => reject(Object.assign(new Error("Cancelled"), { cancelled: true }));
      xhr.send(blob);
    });
  }

  async function runTask(t) {
    try {
      const folder = await ensureFolderPath(t.dest, t.path);
      if (t.status === "cancelled") return;
      const init = await api("/api/uploads", { method: "POST", json: { name: t.file.name, size: t.file.size, folder_id: folder } });
      t.uploadId = init.upload_id;
      const chunk = init.chunk_size;
      let offset = 0, tries = 0;
      while (offset < t.file.size) {
        if (t.status === "cancelled") return;
        try {
          offset = await putChunk(t, t.file.slice(offset, offset + chunk), offset);
          tries = 0;
        } catch (e) {
          if (e.cancelled || t.status === "cancelled") return;
          if (!(e.retry || e.status >= 500) || ++tries > 6) throw e;
          setRow(t, null, `Connection hiccup — retrying (${tries}/6)…`);
          await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** tries)));
          try { offset = (await api(`/api/uploads/${t.uploadId}`)).received; } catch (_) { /* retry loop */ }
        }
      }
      setRow(t, 100, "Finishing…");
      const info = await api(`/api/uploads/${t.uploadId}/complete`, { method: "POST" });
      t.status = "done";
      setRow(t, 100, `${fmtSize(t.file.size)} · done` + (info.indexed_paragraphs ? " · text searchable" : ""), "done");
      $(`#up-${t.id} .up-cancel`).textContent = "✓";
      refreshSoon();
    } catch (e) {
      if (t.status === "cancelled") return;
      t.status = "error";
      setRow(t, null, e.message === "unauthorized" ? "Signed out" : "Failed: " + e.message, "error");
    }
  }

  function cancelTask(t) {
    if (t.status === "done") { $("#up-" + t.id)?.remove(); return; }
    t.status = "cancelled";
    if (t.xhr) t.xhr.abort();
    if (t.uploadId) api(`/api/uploads/${t.uploadId}`, { method: "DELETE" }).catch(() => {});
    $("#up-" + t.id)?.remove();
    updateUpTitle();
  }
  $("#up-close").addEventListener("click", () => {
    if (queue.some((t) => t.status === "uploading" || t.status === "queued")) {
      if (!confirm("Cancel the uploads still in progress?")) return;
      queue.filter((t) => t.status === "uploading" || t.status === "queued").forEach(cancelTask);
    }
    queue.length = 0;
    $("#up-list").innerHTML = "";
    $("#uploads").classList.add("hidden");
  });
  $("#up-collapse").addEventListener("click", () => $("#uploads").classList.toggle("collapsed"));
  window.addEventListener("beforeunload", (e) => {
    if (queue.some((t) => t.status === "uploading" || t.status === "queued")) { e.preventDefault(); e.returnValue = ""; }
  });
  let refreshTimer = null;
  function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(load, 500); }

  // ------------------------------------------------------------------ preview
  let pvList = [], pvIndex = -1;
  function openPreview(it) {
    pvList = (state.visible || state.items).filter((i) => i.type === "file");
    pvIndex = pvList.findIndex((i) => i.id === it.id);
    if (pvIndex < 0) { pvList = [it]; pvIndex = 0; }
    showPreview();
    $("#preview").classList.remove("hidden");
    document.body.classList.add("no-scroll");
  }
  function closePreview() {
    $("#preview").classList.add("hidden");
    $("#pv-body").innerHTML = "";   // stops video/audio
    document.body.classList.remove("no-scroll");
  }
  async function showPreview() {
    const it = pvList[pvIndex];
    const body = $("#pv-body");
    $("#pv-name").textContent = it.name;
    $("#pv-download").href = `/files/${it.id}/download`;
    $("#pv-open").href = `/files/${it.id}/view`;
    $("#pv-prev").classList.toggle("hidden", pvIndex <= 0);
    $("#pv-next").classList.toggle("hidden", pvIndex >= pvList.length - 1);
    const src = `/files/${it.id}/view`;
    const noPreview = (why) => `<div class="pv-none"><span class="d-big-icon">${ICON[it.kind] || ICON.other}</span>
      <p>${esc(why)}</p><p class="muted">${esc(it.mime)} · ${fmtSize(it.size)}</p>
      <a class="btn-primary" href="/files/${it.id}/download">⬇ Download</a></div>`;
    if (it.kind === "image" && it.inline) body.innerHTML = `<img src="${src}" alt="${esc(it.name)}">`;
    else if (it.kind === "video" && it.inline) {
      body.innerHTML = `<video src="${src}" controls autoplay playsinline preload="metadata"></video>`;
      $("video", body).onerror = () => { body.innerHTML = noPreview("This video format can't play in the browser. Download it to watch."); };
    } else if (it.kind === "audio" && it.inline) body.innerHTML = `<div class="pv-audio"><span class="d-big-icon">🎵</span><audio src="${src}" controls autoplay></audio></div>`;
    else if (it.kind === "pdf") body.innerHTML = `<iframe src="${src}" title="${esc(it.name)}"></iframe>`;
    else if (["text", "doc"].includes(it.kind)) {
      body.innerHTML = '<p class="muted">Loading…</p>';
      try {
        const d = await api(`/files/${it.id}/text`);
        if (pvList[pvIndex] !== it) return;
        body.innerHTML = d.text != null
          ? `<pre class="pv-text">${esc(d.text)}</pre>`
          : noPreview("This file is too large to preview.");
      } catch (e) { body.innerHTML = noPreview(e.message); }
    } else body.innerHTML = noPreview(it.kind === "image" ? "This image format can't be shown in the browser." : "No preview available for this file type.");
  }
  $("#pv-close").addEventListener("click", closePreview);
  $("#pv-prev").addEventListener("click", () => { if (pvIndex > 0) { pvIndex--; showPreview(); } });
  $("#pv-next").addEventListener("click", () => { if (pvIndex < pvList.length - 1) { pvIndex++; showPreview(); } });
  $("#preview").addEventListener("click", (e) => { if (e.target.id === "pv-body" || e.target.id === "preview") closePreview(); });
  document.addEventListener("keydown", (e) => {
    if ($("#preview").classList.contains("hidden")) return;
    if (e.key === "Escape") closePreview();
    else if (e.key === "ArrowLeft") $("#pv-prev").click();
    else if (e.key === "ArrowRight") $("#pv-next").click();
  });

  // ------------------------------------------------------------------ search
  const MARK_OPEN = String.fromCharCode(2), MARK_CLOSE = String.fromCharCode(3);
  const snippetHtml = (s) => esc(s).split(MARK_OPEN).join("<mark>").split(MARK_CLOSE).join("</mark>");
  async function searchContent(q, seq) {
    try {
      const d = await api("/search?q=" + encodeURIComponent(q));
      if (seq !== loadSeq) return;
      const box = $("#content-results");
      if (!d.count) { box.classList.add("hidden"); return; }
      box.classList.remove("hidden");
      $("#content-list").innerHTML = d.results.map((r, i) => `
        <article class="result" data-i="${i}" tabindex="0">
          <div class="result-meta">
            <span class="badge badge-${esc(r.source_type)}">${esc(r.source_type)}</span>
            <span class="result-title">${esc(r.title)}</span>
          </div>
          <p class="result-snippet clamp">${snippetHtml(r.snippet)}</p>
        </article>`).join("");
      $$("#content-list .result").forEach((el) => el.addEventListener("click", () => {
        const r = d.results[+el.dataset.i];
        if (r.file_id) api(`/api/file/${r.file_id}`).then(openPreview).catch(fail); else location = "/editor/" + r.doc_id;
      }));
    } catch (_) { /* name results still shown */ }
  }
  let searchTimer = null;
  $("#search-input").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(() => {
      if (q) location.hash = "#/search/" + encodeURIComponent(q);
      else if (state.view === "search") location.hash = "#/";
    }, 350);
  });
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#search-input").value.trim();
    if (q) location.hash = "#/search/" + encodeURIComponent(q);
  });
  document.addEventListener("keydown", (e) => {
    const inField = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !inField && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); $("#search-input").focus(); $("#search-input").select();
    }
  });

  // ------------------------------------------------------------------ toolbar
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; setPref("driveSort", state.sort); render(); });
  $("#view-toggle").addEventListener("click", () => {
    state.layout = state.layout === "grid" ? "list" : "grid";
    setPref("driveLayout", state.layout);
    render();
  });
  $("#menu-toggle").addEventListener("click", (e) => { e.stopPropagation(); $("#sidebar").classList.toggle("open"); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#sidebar") && !e.target.closest("#menu-toggle")) $("#sidebar").classList.remove("open");
  });

  route();
})();
