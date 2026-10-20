"use strict";

const POLL_MS = 3000;
const ROUTINES_MS = 30000;
const SUGGEST_DEBOUNCE_MS = 150;
const TOAST_MS = 4000;
const CONFIRM_MS = 2000;
const APP_LINK = /^https:\/\/claude\.ai\/code\/[A-Za-z0-9_-]+$/;

const $ = (id) => document.getElementById(id);
let data = { groups: [], counts: [] };
let selectedId = null;
let sheet = null;
let pollSeq = 0;
let pollTimer = null;
let routinesTimer = null;
let suggestTimer = null;
let suggestSeq = 0;
let toastTimer = null;
let armTimer = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
}

async function api(path, body) {
  try {
    const options = body === undefined
      ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const response = await fetch(path, options);
    return await response.json();
  } catch (error) {
    return null;
  }
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, TOAST_MS);
}

function findSession(id) {
  for (const group of data.groups) {
    for (const session of group.sessions) {
      if (session.id === id) return { session, group };
    }
  }
  return null;
}

function renderCounts() {
  const box = $("counts");
  clear(box);
  data.counts.forEach((count, i) => {
    if (i) box.append(" · ");
    box.append(el("span", "tone-" + count.tone, count.number + " " + count.key));
  });
}

function renderFolder(group) {
  const header = el("div", "folder");
  const cut = group.label.lastIndexOf("/");
  if (cut >= 0 && group.label.slice(cut + 1) === group.base) {
    header.append(group.label.slice(0, cut + 1), el("span", "base", group.base));
  } else {
    header.textContent = group.label;
  }
  return header;
}

function renderRow(session) {
  const stopped = session.tone === "stopped";
  const row = el("div", "row" + (stopped ? " stopped" : "") + (session.id === selectedId ? " selected" : ""));
  const linked = session.link && APP_LINK.test(session.link);
  let main;
  if (linked) {
    main = el("a", "main");
    main.href = session.link;
    main.target = "_blank";
    main.rel = "noopener";
  } else {
    main = el("button", "main");
    main.type = "button";
    main.addEventListener("click", () => openSheet(session.id));
  }
  const text = el("span", "text");
  const detail = session.detail || (session.id === selectedId && session.running && !linked ? "waiting for the app link" : "");
  const line = el("span", "line");
  line.append(el("span", "tone-" + session.tone, session.word));
  if (detail) line.append(el("span", "muted detail-text", " · " + detail));
  text.append(el("b", "name", session.name), line);
  main.append(el("span", "glyph tone-" + session.tone, session.glyph), text, el("span", "age", session.age));
  const more = el("button", "more", "⋯");
  more.type = "button";
  more.setAttribute("aria-label", "More for " + session.name);
  more.addEventListener("click", () => openSheet(session.id));
  row.append(main, more);
  row.dataset.id = session.id;
  return row;
}

function renderList() {
  const list = $("list");
  clear(list);
  if (data.error) {
    list.append(el("p", "empty", data.error));
    return;
  }
  if (!data.groups.length) list.append(el("p", "empty", "No sessions yet. Tap + New."));
  for (const group of data.groups) {
    list.append(renderFolder(group));
    for (const session of group.sessions) list.append(renderRow(session));
  }
  const routines = el("button", "routines-row", "Routines ›");
  routines.type = "button";
  routines.addEventListener("click", openRoutines);
  list.append(routines);
}

function render() {
  renderCounts();
  renderList();
  if (sheet && !findSession(sheet.id)) closeSheet();
}

async function poll() {
  if (document.visibilityState !== "visible") return;
  const seq = ++pollSeq;
  const answer = await api("/api/state");
  if (seq !== pollSeq) return;
  $("offline").hidden = answer !== null;
  if (answer === null || !Array.isArray(answer.counts)) return;
  data = answer;
  render();
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, POLL_MS);
  poll();
}

function openSheet(id) {
  const found = findSession(id);
  if (!found) return;
  sheet = { id, armed: false };
  const { session, group } = found;
  $("sheet-name").textContent = session.name;
  const word = $("sheet-word");
  word.textContent = session.word;
  word.className = "tone-" + session.tone;
  $("sheet-where").textContent = "· " + group.label;
  const open = $("sheet-open");
  const linked = session.link && APP_LINK.test(session.link);
  open.hidden = !linked;
  if (linked) open.href = session.link;
  $("sheet-close").textContent = "Close session";
  $("sheet-menu").hidden = false;
  $("sheet-edit").hidden = true;
  $("sheet-layer").hidden = false;
  $("sheet-close").focus({ preventScroll: true });
}

function disarmClose() {
  clearTimeout(armTimer);
  if (sheet) sheet.armed = false;
  $("sheet-close").textContent = "Close session";
}

function closeSheet() {
  disarmClose();
  sheet = null;
  $("sheet-layer").hidden = true;
}

function startRename() {
  const found = sheet && findSession(sheet.id);
  if (!found) return;
  disarmClose();
  $("rename-field").value = found.session.name;
  $("sheet-menu").hidden = true;
  $("sheet-edit").hidden = false;
  $("rename-field").focus();
  $("rename-field").select();
}

async function saveRename(event) {
  event.preventDefault();
  if (!sheet) return;
  const button = $("rename-save");
  button.disabled = true;
  button.textContent = "Renaming…";
  const answer = await api("/api/rename", { id: sheet.id, name: $("rename-field").value });
  button.disabled = false;
  button.textContent = "Save";
  toast(answer ? answer.message : "emx didn't answer.");
  if (answer && answer.ok) closeSheet();
  poll();
}

async function closeSession() {
  if (!sheet) return;
  const found = findSession(sheet.id);
  if (!found) return;
  const button = $("sheet-close");
  if (!sheet.armed) {
    sheet.armed = true;
    button.textContent = "Tap again to close";
    armTimer = setTimeout(disarmClose, CONFIRM_MS);
    return;
  }
  clearTimeout(armTimer);
  button.disabled = true;
  const answer = await api("/api/close", { id: sheet.id, running: found.session.running });
  button.disabled = false;
  toast(answer ? answer.message : "emx didn't answer.");
  closeSheet();
  poll();
}

function renderSuggestions(folders) {
  const box = $("new-suggestions");
  clear(box);
  box.hidden = folders.length === 0;
  for (const folder of folders) {
    const button = el("button", "suggestion");
    button.type = "button";
    button.append(el("b", "", folder.base), el("span", "path", folder.shown));
    button.addEventListener("click", () => {
      $("new-folder").value = folder.shown;
      box.hidden = true;
      clear(box);
    });
    box.append(button);
  }
}

async function suggest() {
  const seq = ++suggestSeq;
  const answer = await api("/api/folders?q=" + encodeURIComponent($("new-folder").value));
  if (seq !== suggestSeq || !answer) return;
  renderSuggestions(answer.folders || []);
}

function openNew() {
  $("new-layer").hidden = false;
  $("new-folder").value = "";
  $("new-name").value = "";
  $("new-message").value = "";
  suggest();
  $("new-folder").focus();
}

function closeNew() {
  $("new-layer").hidden = true;
}

async function startSession() {
  const button = $("new-start");
  button.disabled = true;
  button.textContent = "Starting…";
  const answer = await api("/api/new", {
    folder: $("new-folder").value, name: $("new-name").value, message: $("new-message").value,
  });
  button.disabled = false;
  button.textContent = "Start";
  toast(answer ? answer.message : "emx didn't answer.");
  if (!answer || !answer.ok) return;
  closeNew();
  selectedId = answer.id;
  await poll();
  const row = document.querySelector('.row[data-id="' + answer.id + '"]');
  if (row) row.scrollIntoView({ block: "center" });
}

function renderRoutines(answer) {
  const box = $("routines-list");
  clear(box);
  if (!answer) {
    box.append(el("p", "empty", "Can't reach emx"));
    return;
  }
  if (answer.error) box.append(el("p", "empty tone-error", answer.error));
  if (!answer.routines.length && !answer.error) box.append(el("p", "empty", "No routines."));
  for (const routine of answer.routines) {
    const item = el("div", "routine");
    const head = el("div", "head");
    head.append(el("b", "", routine.name), el("span", "tone-" + routine.tone, routine.tag));
    item.append(head);
    if (routine.error) item.append(el("div", "error", routine.error));
    if (routine.runs.length) item.append(el("div", "runs", routine.runs.join("   ")));
    if (routine.next) item.append(el("div", "next", routine.next));
    box.append(item);
  }
}

async function loadRoutines() {
  renderRoutines(await api("/api/routines"));
}

function openRoutines() {
  $("routines-layer").hidden = false;
  loadRoutines();
  clearInterval(routinesTimer);
  routinesTimer = setInterval(loadRoutines, ROUTINES_MS);
}

function closeRoutines() {
  $("routines-layer").hidden = true;
  clearInterval(routinesTimer);
}

$("open-new").addEventListener("click", openNew);
$("new-cancel").addEventListener("click", closeNew);
$("new-start").addEventListener("click", startSession);
$("new-folder").addEventListener("input", () => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(suggest, SUGGEST_DEBOUNCE_MS);
});
$("sheet-backdrop").addEventListener("click", closeSheet);
$("sheet-grab").addEventListener("click", closeSheet);
$("sheet-rename").addEventListener("click", startRename);
$("rename-back").addEventListener("click", () => { disarmClose(); $("sheet-edit").hidden = true; $("sheet-menu").hidden = false; });
$("sheet-edit").addEventListener("submit", saveRename);
$("sheet-close").addEventListener("click", closeSession);
$("routines-back").addEventListener("click", closeRoutines);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sheet) closeSheet();
});
document.addEventListener("visibilitychange", poll);
// iOS Safari ignores user-scalable=no in a tab, so pinch is stopped here.
for (const name of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(name, (event) => event.preventDefault());
}

startPolling();
