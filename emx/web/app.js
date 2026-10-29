"use strict";

const POLL_MS = 3000;
const ROUTINES_MS = 30000;
const SUGGEST_DEBOUNCE_MS = 150;
const TOAST_MS = 4000;
const CONFIRM_MS = 2000;
const FOLDED_KEY = "emx.collapsed";
const VERSION_KEY = "emx.version";
const MAJOR_KEY = "emx.major";
const OFFERED_MAX = 10;
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
let readerTimer = null;
let routinesPlace = 0;
let reading = { place: "", ids: [], at: 0, live: false };
let suggestTimer = null;
let suggestSeq = 0;
let toastTimer = null;
let armTimer = null;
let frameTimer = null;
let frameTimerMs = 0;
let folded = loadFolded();
let findText = "";
let findTimer = null;
let loadedVersion = null;
let folderSheet = null;
let peekTimer = null;
let noticesTimer = null;
let updateSeen = null;
let updateAsked = false;
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

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    // No storage: this device asks again next time.
  }
}

function stored(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (error) {
    return "";
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

function findGroup(key) {
  return data.groups.find((group) => group.key === key) || null;
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
  const isFolded = folded.has(group.key) && !data.finding;
  const bar = el("div", "folder-bar");
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
  header.addEventListener("click", () => toggleFolder(group.key));
  bar.append(header);
  if (!group.pinned) {
    const more = el("button", "more", glyphText("⋯"));
    more.type = "button";
    more.setAttribute("aria-label", "More for " + group.label);
    more.addEventListener("click", () => openFolderSheet(group.key));
    bar.append(more);
  }
  return bar;
}

function toggleFolder(key) {
  if (data.finding) return;
  if (!folded.delete(key)) folded.add(key);
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
  main.addEventListener("click", () => markRead(session.id));
  const text = el("span", "text");
  const names = el("span", "nameline");
  names.append(el("b", "name", session.name));
  if (session.worktree) names.append(el("span", "branch tone-path", glyphText(data.look.worktree_mark.trimEnd()) + " " + session.worktree));
  text.append(names);
  const line = statusLine(session, linked);
  if (line) text.append(line);
  const spinning = session.spin && data.look && !reducedMotion();
  const glyph = el("span", "glyph tone-" + session.glyph_tone, glyphText(spinning ? currentFrame() : session.glyph));
  if (spinning) glyph.dataset.spin = "";
  main.append(glyph, text);
  if (session.unread) main.append(el("span", "dot tone-" + session.unread, glyphText(data.look.dot)));
  main.append(el("span", "age", session.age));
  const more = el("button", "more", glyphText("⋯"));
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

// A session opened from the page is read: the server clears its notice, and the row and the Unread entry go at once.
function markRead(id) {
  const found = findSession(id);
  if (!found || !found.session.unread) return;
  found.session.unread = null;
  data.unread_list = data.unread_list.filter((item) => item.id !== id);
  data.unread = Math.max(0, data.unread - 1);
  api("/api/read", { id });
  // After the tap's own default action: a link being followed must not be pulled out of the page first.
  setTimeout(render, 0);
}

function unreadEntry(item) {
  const found = findSession(item.id);
  const linked = found && found.session.link && APP_LINK.test(found.session.link);
  let entry;
  if (linked) {
    entry = el("a", "unread-entry");
    entry.href = found.session.link;
    entry.target = "_blank";
    entry.rel = "noopener";
  } else {
    entry = el("button", "unread-entry");
    entry.type = "button";
    entry.addEventListener("click", () => {
      if (!found) clearFind();
      else if (found.session.running) openSheet(item.id);
      else resume(item.id);
    });
  }
  entry.addEventListener("click", () => markRead(item.id));
  entry.append(
    el("span", "unread-mark tone-" + item.tone, glyphText(item.mark)),
    el("b", "unread-name", item.name),
    el("span", "unread-text tone-" + item.tone, item.text),
  );
  return entry;
}

function renderUnread() {
  const box = el("section", "unread");
  const title = el("div", "unread-title");
  title.append(el("b", "", "Unread"));
  box.append(title);
  for (const item of data.unread_list) box.append(unreadEntry(item));
  return box;
}

function bottomRow(label, count, open) {
  const row = el("button", "routines-row");
  row.type = "button";
  row.append(label);
  if (count) row.append(" ", el("span", "tone-accent", String(count)));
  row.append(" ›");
  row.addEventListener("click", open);
  return row;
}

function renderList() {
  const list = $("list");
  clear(list);
  if (data.error) {
    list.append(el("p", "empty", data.error));
    return;
  }
  if (!data.groups.length) {
    list.append(el("p", "empty", data.finding ? data.look.find_none : "No sessions yet. Tap + New."));
  }
  if (data.unread_list && data.unread_list.length) list.append(renderUnread());
  for (const group of data.groups) {
    list.append(renderFolder(group));
    if (folded.has(group.key) && !data.finding) continue;
    for (const session of group.sessions) list.append(renderRow(session));
  }
  list.append(bottomRow("Notifications", data.unread, openNotices));
  list.append(bottomRow("Routines", 0, openRoutines));
}

function render() {
  renderCounts();
  renderList();
  syncFrameTimer();
  if (sheet && !findSession(sheet.id)) closeSheet();
  if (folderSheet && !findGroup(folderSheet.key)) closeFolderSheet();
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
  const answer = await api("/api/state" + (findText ? "?find=" + encodeURIComponent(findText) : ""));
  if (seq !== pollSeq) return;
  $("offline").hidden = answer !== null;
  if (answer === null || !Array.isArray(answer.counts)) return;
  data = answer;
  render();
  noteVersion(answer.version);
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
  $("sheet-pin").textContent = session.pinned ? "Unpin" : "Pin to top";
  $("sheet-close").textContent = "Close session";
  showSheetPart("sheet-menu");
  $("sheet-layer").hidden = false;
  $("sheet-close").focus({ preventScroll: true });
}

function showSheetPart(id) {
  for (const part of ["sheet-menu", "sheet-edit", "sheet-peekview"]) $(part).hidden = part !== id;
  if (id !== "sheet-peekview") {
    clearInterval(peekTimer);
    peekTimer = null;
  }
}

function disarmClose() {
  clearTimeout(armTimer);
  if (sheet) sheet.armed = false;
  $("sheet-close").textContent = "Close session";
}

function closeSheet() {
  disarmClose();
  sheet = null;
  clearInterval(peekTimer);
  peekTimer = null;
  $("sheet-layer").hidden = true;
}

function startRename() {
  const found = sheet && findSession(sheet.id);
  if (!found) return;
  disarmClose();
  $("rename-field").value = found.session.name;
  sheet.offered = [];
  showSheetPart("sheet-edit");
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

async function loadPeek() {
  if (!sheet) return;
  const id = sheet.id;
  const answer = await api("/api/peek?id=" + encodeURIComponent(id));
  if (!sheet || sheet.id !== id || $("sheet-peekview").hidden) return;
  const title = $("peek-title");
  const box = $("peek-lines");
  clear(box);
  if (!answer || !answer.ok) {
    title.textContent = "";
    box.append(el("p", "muted", answer ? answer.message : "Can't reach emx"));
    return;
  }
  title.textContent = answer.title;
  title.className = "tone-" + answer.tone;
  for (const line of answer.lines) {
    box.append(el("div", "peek-line tone-" + line.tone + (line.code ? " code" : "") + (line.dim ? " dim" : ""), line.text));
  }
}

function startPeek() {
  if (!sheet) return;
  disarmClose();
  showSheetPart("sheet-peekview");
  $("peek-title").textContent = "";
  clear($("peek-lines"));
  loadPeek();
  peekTimer = setInterval(loadPeek, POLL_MS);
}

async function pinFromSheet() {
  if (!sheet) return;
  const id = sheet.id;
  const answer = await api("/api/pin", { id });
  toast(answer ? answer.message : "emx didn't answer.");
  closeSheet();
  poll();
}

async function suggestName() {
  if (!sheet) return;
  const id = sheet.id;
  const button = $("rename-suggest");
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "Asking Haiku…";
  const answer = await api("/api/suggest-name", { id, offered: sheet.offered });
  button.disabled = false;
  button.textContent = label;
  if (!sheet || sheet.id !== id) return;
  if (answer && answer.ok) {
    $("rename-field").value = answer.name;
    sheet.offered = sheet.offered.concat([answer.name]).slice(-OFFERED_MAX);
  } else {
    toast(answer ? answer.message : "emx didn't answer.");
  }
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
  if (answer && answer.ok) {
    selectedId = id;
    markRead(id);
  }
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
    const row = el("div", "suggestion-row");
    const button = el("button", "suggestion");
    button.type = "button";
    button.append(el("b", "", folder.base), el("span", "path", folder.shown));
    button.addEventListener("click", () => {
      $("new-folder").value = folder.shown;
      box.hidden = true;
      clear(box);
    });
    row.append(button);
    box.append(row);
  }
}

async function suggest() {
  const seq = ++suggestSeq;
  const answer = await api("/api/folders?q=" + encodeURIComponent($("new-folder").value));
  if (seq !== suggestSeq || !answer) return;
  renderSuggestions(answer.folders || []);
}

function openNew(folder) {
  $("new-layer").hidden = false;
  $("new-folder").value = typeof folder === "string" ? folder : "";
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
  if (started && folded.delete(started.group.key)) {
    saveFolded();
    renderList();
  }
  const row = document.querySelector('.row[data-id="' + answer.id + '"]');
  if (row) row.scrollIntoView({ block: "center" });
}

function openFolderSheet(key) {
  const group = findGroup(key);
  if (!group) return;
  folderSheet = { key, armed: false, sent: null };
  $("folder-name").textContent = group.label;
  $("folder-count").textContent = group.sessions.length + (group.sessions.length === 1 ? " session" : " sessions");
  $("folder-new").hidden = !group.new_folder;
  $("folder-close").hidden = !group.closable;
  $("folder-close").disabled = false;
  $("folder-close").textContent = closeFolderLabel(group, false);
  $("folder-layer").hidden = false;
  $("folder-close").focus({ preventScroll: true });
}

function closeFolderLabel(group, armed) {
  const count = group.sessions.length;
  const what = count + (count === 1 ? " session" : " sessions");
  return (armed ? "Tap again to close " : "Close ") + what;
}

function disarmFolder() {
  clearTimeout(armTimer);
  if (!folderSheet) return;
  folderSheet.armed = false;
  folderSheet.sent = null;
  const group = findGroup(folderSheet.key);
  if (group) $("folder-close").textContent = closeFolderLabel(group, false);
}

function closeFolderSheet() {
  disarmFolder();
  folderSheet = null;
  $("folder-layer").hidden = true;
}

function newInFolder() {
  const group = folderSheet && findGroup(folderSheet.key);
  if (!group || !group.new_folder) return;
  const folder = group.new_folder;
  closeFolderSheet();
  openNew(folder);
}

async function closeFolder() {
  const group = folderSheet && findGroup(folderSheet.key);
  if (!group) return;
  const button = $("folder-close");
  if (!folderSheet.armed) {
    folderSheet.armed = true;
    folderSheet.sent = group.sessions.map((session) => ({ id: session.id, running: session.running }));
    button.textContent = closeFolderLabel(group, true);
    armTimer = setTimeout(disarmFolder, CONFIRM_MS);
    return;
  }
  clearTimeout(armTimer);
  const sent = folderSheet.sent;
  button.disabled = true;
  for (const entry of sent) closing.add(entry.id);
  render();
  const answer = await api("/api/close-group", { key: group.key, find: findText, sessions: sent });
  for (const entry of sent) closing.delete(entry.id);
  toast(answer ? answer.message : "emx didn't answer.");
  closeFolderSheet();
  render();
  poll();
}

function onFindInput() {
  const text = $("find").value.trim();
  $("find-clear").hidden = $("find").value === "";
  clearTimeout(findTimer);
  findTimer = setTimeout(() => {
    findText = text;
    poll();
  }, SUGGEST_DEBOUNCE_MS);
}

function clearFind() {
  $("find").value = "";
  onFindInput();
  $("find").focus();
}

function openScreen(id) {
  $(id).hidden = false;
}

function noticeRow(entry) {
  const found = findSession(entry.id);
  const linked = found && found.session.link && APP_LINK.test(found.session.link);
  let row;
  if (linked) {
    row = el("a", "notice");
    row.href = found.session.link;
    row.target = "_blank";
    row.rel = "noopener";
    row.addEventListener("click", closeNotices);
  } else {
    row = el("button", "notice");
    row.type = "button";
    row.addEventListener("click", () => openFromNotice(entry));
  }
  if (!entry.read) row.addEventListener("click", () => markRead(entry.id));
  const tone = entry.read ? "muted" : entry.tone;
  const head = el("span", "notice-head");
  head.append(el("b", "tone-" + tone, glyphText(entry.mark) + " " + entry.title), el("span", "age muted", entry.age));
  row.append(head, el("span", "notice-text" + (entry.read ? " muted" : ""), entry.text));
  return row;
}

function openFromNotice(entry) {
  const found = findSession(entry.id);
  if (!entry.listed || !found) {
    toast(data.look ? data.look.open_gone : "That session isn't listed any more.");
    return;
  }
  closeNotices();
  if (found.session.running) openSheet(entry.id);
  else resume(entry.id);
}

async function loadNotices() {
  const answer = await api("/api/notifications");
  const box = $("notices-list");
  clear(box);
  if (!answer) {
    box.append(el("p", "empty", "Can't reach emx"));
    return;
  }
  $("notices-title").textContent = answer.title;
  if (!answer.entries.length) box.append(el("p", "empty", answer.empty));
  for (const entry of answer.entries) box.append(noticeRow(entry));
}

function openNotices() {
  openScreen("notices-layer");
  loadNotices();
  clearInterval(noticesTimer);
  noticesTimer = setInterval(loadNotices, POLL_MS);
}

function closeNotices() {
  $("notices-layer").hidden = true;
  clearInterval(noticesTimer);
}

function placeChips(answer) {
  const box = $("routines-chips");
  clear(box);
  answer.places.forEach((place, i) => {
    const chip = el("button", "chip" + (i === routinesPlace ? " selected" : ""));
    chip.type = "button";
    chip.append(el("span", "", place.name));
    if (place.problem) chip.append(el("span", "tone-error", " ●"));
    chip.addEventListener("click", () => {
      routinesPlace = i;
      renderPlaces(answer);
    });
    box.append(chip);
  });
}

function runCard(place, run) {
  const found = run.sessions;
  const card = el("button", "run-card" + (run.problem ? " problem" : "") + (found.length ? "" : " idle"));
  card.type = "button";
  const word = el("span", "tone-" + run.tone, run.at + " " + run.word);
  card.append(word);
  if (run.label) card.append(el("span", "muted", " · " + run.label));
  if (found.length) {
    card.append(el("span", "read" + (run.problem ? " accent" : " muted"), "Read ›"));
    card.addEventListener("click", () => openReader(place, found.map((one) => one.id)));
  } else {
    card.disabled = true;
  }
  return card;
}

function renderPlaces(answer) {
  const box = $("routines-list");
  clear(box);
  if (!answer) {
    clear($("routines-chips"));
    box.append(el("p", "empty", "Can't reach emx"));
    return;
  }
  $("routines-title").textContent = answer.title;
  routinesPlace = Math.max(0, Math.min(routinesPlace, answer.places.length - 1));
  placeChips(answer);
  if (answer.error) box.append(el("p", "empty tone-error", answer.error));
  if (answer.problems) {
    const cut = answer.problems.indexOf(" ·");
    const line = el("p", "problems muted");
    line.append(el("span", "tone-error", cut < 0 ? answer.problems : answer.problems.slice(0, cut)));
    if (cut >= 0) line.append(answer.problems.slice(cut));
    box.append(line);
  }
  const place = answer.places[routinesPlace];
  if (!place) return;
  if (place.error) box.append(el("p", "empty tone-error", place.error));
  else if (!place.routines.length) box.append(el("p", "empty", answer.none));
  for (const routine of place.routines) {
    const item = el("div", "routine");
    const head = el("span", "head");
    head.append(el("b", "", routine.name), el("span", "tone-" + routine.tone, routine.tag));
    item.append(head);
    if (routine.detail) item.append(el("span", "next", routine.detail));
    if (routine.error) item.append(el("span", "next tone-error", routine.error));
    for (const day of routine.days) {
      item.append(el("div", "day small muted", day.day));
      for (const run of day.runs) item.append(runCard(place.name, run));
    }
    if (routine.plain) item.append(el("span", "next", "No Claude session · a plain script"));
    if (routine.next) item.append(el("span", "next", routine.next));
    box.append(item);
  }
}

async function loadRoutines() {
  renderPlaces(await api("/api/routines"));
}

function openRoutines() {
  openScreen("routines-layer");
  loadRoutines();
  clearInterval(routinesTimer);
  routinesTimer = setInterval(loadRoutines, ROUTINES_MS);
}

function closeRoutines() {
  closeReader();
  $("routines-layer").hidden = true;
  clearInterval(routinesTimer);
}

function renderRun(answer) {
  const box = $("reader-steps");
  clear(box);
  if (!answer || !answer.ok) {
    $("reader-end").replaceChildren(el("p", "empty tone-error", answer ? answer.message : "Can't reach emx"));
    reading.live = false;
    return;
  }
  $("reader-title").textContent = answer.title;
  $("reader-sub").textContent = answer.sub;
  $("reader-other").hidden = reading.ids.length < 2;
  const earlier = $("reader-earlier");
  earlier.hidden = !answer.earlier;
  earlier.textContent = "↑ " + answer.earlier + " earlier step" + (answer.earlier === 1 ? "" : "s") + " · scroll up";
  for (const step of answer.steps) {
    if (step.kind === "tool") {
      const card = el("div", "step-card");
      const space = step.text.indexOf(" ");
      const name = space < 0 ? step.text : step.text.slice(0, space);
      const head = el("div", "step-head");
      head.append(el("span", "tone-accent", name));
      if (step.mark) head.append(el("span", step.tone === "error" ? "tone-error" : "tone-done", step.mark));
      card.append(head);
      if (space >= 0) card.append(el("div", "small muted", step.text.slice(space + 1)));
      box.append(card);
    } else if (step.kind === "error") {
      box.append(el("div", "small tone-error step-error", step.text.replace(/^! /, "")));
    } else {
      box.append(el("p", "step-text" + (step.kind === "cut" ? " muted" : ""), step.kind === "prompt" ? step.text.replace(/^Prompt: /, "") : step.text));
    }
  }
  const end = $("reader-end");
  clear(end);
  if (answer.end.text) end.append(el("div", "end-line tone-" + answer.end.tone, answer.end.text));
  reading.live = answer.live && !answer.remote;
}

async function loadRun(stay) {
  const at = reading.at;
  const answer = await api("/api/run?place=" + encodeURIComponent(reading.place) + "&id=" + encodeURIComponent(reading.ids[at]));
  if (reading.at !== at || $("reader-layer").hidden) return;
  const screen = $("reader-layer");
  const atEnd = !stay || screen.scrollHeight - screen.scrollTop - screen.clientHeight < 80;
  renderRun(answer);
  if (atEnd) screen.scrollTop = screen.scrollHeight;
  clearInterval(readerTimer);
  if (reading.live) readerTimer = setInterval(() => loadRun(true), POLL_MS);
}

function openReader(place, ids) {
  reading = { place, ids, at: ids.length - 1, live: false };
  clear($("reader-steps"));
  clear($("reader-end"));
  openScreen("reader-layer");
  loadRun(false);
}

function otherSession() {
  reading.at = (reading.at + 1) % reading.ids.length;
  loadRun(false);
}

function closeReader() {
  clearInterval(readerTimer);
  $("reader-layer").hidden = true;
}

// A new version: reload once so a home-screen app runs the new script, then say what changed since this device last saw emx.
function noteVersion(version) {
  if (typeof version !== "string") return;
  if (loadedVersion === null) loadedVersion = version;
  else if (version !== loadedVersion) location.reload();
  if (!updateAsked) {
    updateAsked = true;
    askUpdate();
  }
}

async function askUpdate() {
  const answer = await api("/api/whats-new?since=" + encodeURIComponent(stored(VERSION_KEY)));
  if (!answer || !answer.ok) {
    updateAsked = false;
    return;
  }
  const major = answer.major && stored(MAJOR_KEY) !== answer.major ? answer : null;
  if (!answer.show && !major) {
    if (answer.record) store(VERSION_KEY, answer.version);
    return;
  }
  updateSeen = answer;
  $("update-heading").textContent = answer.show ? answer.heading : answer.major_line;
  const box = $("update-text");
  clear(box);
  if (answer.show) box.append(el("div", "", answer.text));
  if (major && answer.show) box.append(el("div", "tone-needs", answer.major_line));
  $("update-layer").hidden = false;
}

function dismissUpdate() {
  if (updateSeen) {
    store(VERSION_KEY, updateSeen.version);
    if (updateSeen.major) store(MAJOR_KEY, updateSeen.major);
  }
  updateSeen = null;
  $("update-layer").hidden = true;
}

$("list").addEventListener("touchstart", onPullStart, { passive: true });
$("list").addEventListener("touchmove", onPullMove, { passive: false });
$("list").addEventListener("touchend", onPullEnd);
$("list").addEventListener("touchcancel", onPullEnd);
$("sheet-resume").addEventListener("click", resumeFromSheet);
$("sheet-open").addEventListener("click", () => sheet && markRead(sheet.id));
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => { frameTimerMs = -1; render(); });
$("open-new").addEventListener("click", () => openNew());
$("new-cancel").addEventListener("click", closeNew);
$("new-start").addEventListener("click", startSession);
$("new-folder").addEventListener("input", () => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(suggest, SUGGEST_DEBOUNCE_MS);
});
$("sheet-backdrop").addEventListener("click", closeSheet);
$("sheet-grab").addEventListener("click", closeSheet);
$("sheet-rename").addEventListener("click", startRename);
$("rename-back").addEventListener("click", () => { disarmClose(); showSheetPart("sheet-menu"); });
$("peek-back").addEventListener("click", () => showSheetPart("sheet-menu"));
$("sheet-peek").addEventListener("click", startPeek);
$("sheet-pin").addEventListener("click", pinFromSheet);
$("rename-suggest").addEventListener("click", suggestName);
$("sheet-edit").addEventListener("submit", saveRename);
$("sheet-close").addEventListener("click", closeSession);
$("routines-back").addEventListener("click", closeRoutines);
$("reader-back").addEventListener("click", closeReader);
$("reader-other").addEventListener("click", otherSession);
$("notices-back").addEventListener("click", closeNotices);
$("find").addEventListener("input", onFindInput);
$("find").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("find").blur();
});
$("find-clear").addEventListener("click", clearFind);
$("find-clear").textContent = glyphText("✕");
$("folder-backdrop").addEventListener("click", closeFolderSheet);
$("folder-grab").addEventListener("click", closeFolderSheet);
$("folder-new").addEventListener("click", newInFolder);
$("folder-close").addEventListener("click", closeFolder);
$("update-backdrop").addEventListener("click", dismissUpdate);
$("update-grab").addEventListener("click", dismissUpdate);
$("update-ok").addEventListener("click", dismissUpdate);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (sheet) closeSheet();
  else if (folderSheet) closeFolderSheet();
});
document.addEventListener("visibilitychange", () => { syncFrameTimer(); poll(); });
// iOS Safari ignores user-scalable=no in a tab, so pinch is stopped here.
for (const name of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(name, (event) => event.preventDefault());
}

startPolling();
