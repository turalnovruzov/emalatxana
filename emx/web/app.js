"use strict";

const POLL_MS = 3000;
const ROUTINES_MS = 30000;
const SUGGEST_DEBOUNCE_MS = 150;
const TOAST_MS = 4000;
const CONFIRM_MS = 2000;
const FOLDED_KEY = "emx.collapsed";
const RESUMING = "resuming…";
const PULL_ARM_PX = 60;
const PULL_MAX_PX = 80;
const PULL_HOLD_MS = 400;
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
let frameTimer = null;
let frameTimerMs = 0;
let folded = loadFolded();
const closing = new Set();
const resuming = new Set();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
}

// U+FE0E asks for the text form: without it iOS can draw an emoji-capable mark such as the Working star as a green emoji square.
function glyphText(character) {
  return character + "\uFE0E";
}

function loadFolded() {
  try {
    const saved = JSON.parse(localStorage.getItem(FOLDED_KEY));
    return new Set(Array.isArray(saved) ? saved.filter((path) => typeof path === "string") : []);
  } catch (error) {
    return new Set();
  }
}

function saveFolded() {
  try {
    localStorage.setItem(FOLDED_KEY, JSON.stringify([...folded]));
  } catch (error) {
    // No storage: folds last until the page is reloaded.
  }
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// One index for every row, from the clock, as the panel draws its frames.
function currentFrame() {
  const frames = Array.from(data.look.frames);
  return frames[Math.floor(Date.now() / data.look.frame_ms) % frames.length];
}

function tickFrames() {
  if (!data.look) return;
  const text = glyphText(currentFrame());
  for (const node of document.querySelectorAll("[data-spin]")) {
    if (node.textContent !== text) node.textContent = text;
  }
}

function syncFrameTimer() {
  const ms = data.look && document.visibilityState === "visible" && !reducedMotion() ? data.look.frame_ms : 0;
  if (ms === frameTimerMs) return;
  clearInterval(frameTimer);
  frameTimer = ms ? setInterval(tickFrames, ms) : null;
  frameTimerMs = ms;
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
  const isFolded = folded.has(group.folder);
  const header = el("button", "folder");
  header.type = "button";
  header.setAttribute("aria-expanded", String(!isFolded));
  const arrow = data.look ? data.look.fold[isFolded ? 1 : 0] : "";
  header.append(el("span", "muted fold", glyphText(arrow)));
  const path = el("span", "folder-path");
  const cut = group.label.lastIndexOf("/");
  if (cut >= 0 && group.label.slice(cut + 1) === group.base) {
    path.append(group.label.slice(0, cut + 1), el("span", "base", group.base));
  } else {
    path.textContent = group.label;
  }
  header.append(path);
  if (isFolded) header.append(el("span", "muted count", String(group.sessions.length)));
  header.addEventListener("click", () => toggleFolder(group.folder));
  return header;
}

function toggleFolder(folder) {
  if (!folded.delete(folder)) folded.add(folder);
  saveFolded();
  renderList();
}

function renderRow(session) {
  const row = el("div", "row" + (session.stopped ? " stopped" : "") + (session.id === selectedId ? " selected" : ""));
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
    main.addEventListener("click", () => (session.running ? openSheet(session.id) : resume(session.id)));
  }
  const text = el("span", "text");
  text.append(el("b", "name", session.name));
  const line = statusLine(session, linked);
  if (line) text.append(line);
  const spinning = session.spin && data.look && !reducedMotion();
  const glyph = el("span", "glyph tone-" + session.glyph_tone, glyphText(spinning ? currentFrame() : session.glyph));
  if (spinning) glyph.dataset.spin = "";
  main.append(glyph, text, el("span", "age", session.age));
  const more = el("button", "more", "⋯");
  more.type = "button";
  more.setAttribute("aria-label", "More for " + session.name);
  more.addEventListener("click", () => openSheet(session.id));
  row.append(main, more);
  row.dataset.id = session.id;
  return row;
}

function statusLine(session, linked) {
  const line = el("span", "line");
  if (closing.has(session.id)) {
    line.append(el("b", "tone-error", data.look.closing));
  } else if (resuming.has(session.id)) {
    line.append(el("span", "muted", RESUMING));
  } else if (session.status) {
    line.append(el("span", "tone-" + session.tone, session.word));
    const detail = session.detail || (session.id === selectedId && session.running && !linked ? "waiting for the app link" : "");
    if (detail) line.append(el("span", "muted detail-text", " · " + detail));
  } else {
    return null;
  }
  return line;
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
    if (folded.has(group.folder)) continue;
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
  syncFrameTimer();
  if (sheet && !findSession(sheet.id)) closeSheet();
}

// Pull down at the top of the list: the indicator follows the finger at half speed, arms past PULL_ARM_PX, and holds while the state is fetched again.
const pull = { startY: null, height: 0, busy: false };

function showPull(height, text, character) {
  const box = $("pull");
  box.style.height = height + "px";
  $("pull-glyph").textContent = glyphText(character);
  $("pull-text").textContent = text;
}

function pullGlyph(offset) {
  const frames = Array.from(data.look.frames);
  return frames[Math.floor(offset) % frames.length];
}

function onPullStart(event) {
  const list = $("list");
  pull.startY = data.look && !pull.busy && list.scrollTop <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
  pull.height = 0;
}

function onPullMove(event) {
  if (pull.startY === null) return;
  const distance = event.touches[0].clientY - pull.startY;
  if (distance <= 0 || $("list").scrollTop > 0) {
    if (pull.height) { pull.height = 0; showPull(0, "", pullGlyph(0)); }
    return;
  }
  if (event.cancelable) event.preventDefault();
  pull.height = Math.min(distance / 2, PULL_MAX_PX);
  const armed = pull.height >= PULL_ARM_PX;
  $("pull").classList.add("dragging");
  showPull(pull.height, armed ? "Release to refresh" : "Pull to refresh", pullGlyph(reducedMotion() ? 0 : pull.height / 8));
}

async function onPullEnd() {
  if (pull.startY === null) return;
  pull.startY = null;
  const box = $("pull");
  box.classList.remove("dragging");
  if (pull.height < PULL_ARM_PX) {
    showPull(0, "", pullGlyph(0));
    pull.height = 0;
    return;
  }
  pull.busy = true;
  showPull(PULL_ARM_PX, "Refreshing…", pullGlyph(0));
  const spin = reducedMotion() ? null : setInterval(() => { $("pull-glyph").textContent = glyphText(currentFrame()); }, data.look.frame_ms);
  await Promise.all([poll(), new Promise((resolve) => setTimeout(resolve, PULL_HOLD_MS))]);
  clearInterval(spin);
  showPull(0, "", pullGlyph(0));
  pull.height = 0;
  pull.busy = false;
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
  $("sheet-resume").hidden = session.running;
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
  const id = sheet.id;
  button.disabled = true;
  closing.add(id);
  render();
  const answer = await api("/api/close", { id, running: found.session.running });
  closing.delete(id);
  button.disabled = false;
  toast(answer ? answer.message : "emx didn't answer.");
  closeSheet();
  render();
  poll();
}

async function resume(id) {
  if (resuming.has(id) || closing.has(id)) return;
  resuming.add(id);
  render();
  const answer = await api("/api/resume", { id });
  resuming.delete(id);
  toast(answer ? answer.message : "emx didn't answer.");
  if (answer && answer.ok) selectedId = id;
  render();
  poll();
}

function resumeFromSheet() {
  if (!sheet) return;
  const id = sheet.id;
  closeSheet();
  resume(id);
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
  const started = findSession(answer.id);
  if (started && folded.delete(started.group.folder)) {
    saveFolded();
    renderList();
  }
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

$("list").addEventListener("touchstart", onPullStart, { passive: true });
$("list").addEventListener("touchmove", onPullMove, { passive: false });
$("list").addEventListener("touchend", onPullEnd);
$("list").addEventListener("touchcancel", onPullEnd);
$("sheet-resume").addEventListener("click", resumeFromSheet);
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => { frameTimerMs = -1; render(); });
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
document.addEventListener("visibilitychange", () => { syncFrameTimer(); poll(); });
// iOS Safari ignores user-scalable=no in a tab, so pinch is stopped here.
for (const name of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(name, (event) => event.preventDefault());
}

startPolling();
