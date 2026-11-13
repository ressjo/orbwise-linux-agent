/* Orbwise – Chat-Historie: Liste, Suche, umbenennen, löschen */

import { $, L, LOCALE, S, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { confirmLeaveSandbox, newChat } from "./project.js";

let chatSearchTimer, openMenu;

export function chatWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
  if (d.toDateString() === new Date(Date.now() - 86400000).toDateString()) return `${L("gestern", "yesterday")} ${time}`;
  return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "2-digit" }) + " " + time;
}
export async function loadChats() {
  const q = $("chat-search").value.trim();
  let list;
  try { list = await getJSON("/api/chats" + (q ? "?q=" + encodeURIComponent(q) : "")); } catch { return; }
  const ul = $("chat-list");
  ul.innerHTML = "";
  if (!list.length) {
    ul.innerHTML = `<li class="empty">${q ? L("Nichts gefunden.", "Nothing found.") : L("Noch keine Chats.", "No chats yet.")}</li>`;
    return;
  }
  let group = "";
  for (const c of list) {
    const g = chatGroup(c);
    if (g !== group) {
      group = g;
      const h = document.createElement("li");
      h.className = "chat-group";
      h.textContent = g;
      ul.appendChild(h);
    }
    const li = document.createElement("li");
    li.className = "chat-item" + (c.active ? " active" : "") + (c.starred ? " pinned" : "");
    li.title = c.preview || c.title;
    li.innerHTML = `<div class="ci-main"><div class="t"></div></div><span class="ci-when"></span>`
      + `<button class="ci-more" type="button" aria-label="${L("Aktionen", "Actions")}"><svg class="i"><use href="#i-more"/></svg></button>`;
    li.querySelector(".t").textContent = c.title;
    li.querySelector(".ci-when").textContent = chatShortWhen(c.updated);
    li.onclick = async () => {
      if (c.active || li.querySelector("input") || !await confirmLeaveSandbox()) return;
      api("POST", `/api/chats/${c.id}/activate`).catch(() => {});
    };
    li.querySelector(".ci-more").onclick = (e) => { e.stopPropagation(); chatMenu(e.currentTarget, c, li); };
    ul.appendChild(li);
  }
}

// Gruppen wie in LM Studio/ChatGPT: Angeheftet · Heute · Gestern · Letzte 7 Tage · Letzte 30 Tage · Älter
function chatGroup(c) {
  if (c.starred) return L("Angeheftet", "Pinned");
  const day = (ts) => { const d = new Date(ts); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const diff = Math.round((day(Date.now()) - day((c.updated || 0) * 1000)) / 86400000);
  if (diff <= 0) return L("Heute", "Today");
  if (diff === 1) return L("Gestern", "Yesterday");
  if (diff < 7) return L("Letzte 7 Tage", "Previous 7 days");
  if (diff < 30) return L("Letzte 30 Tage", "Previous 30 days");
  return L("Älter", "Older");
}
function chatShortWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (d.toDateString() === new Date().toDateString()) return d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" });
}
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
// Kleines Menü an einem Knopf. Einträge: {icon, label, run, sub?, checked?, disabled?, danger?, title?} oder {sep: true}
export function popMenu(anchor, items) {
  closeMenu();
  const m = document.createElement("div");
  m.className = "pop-menu";
  m.setAttribute("role", "menu");
  for (const it of items) {
    if (it.sep) { m.appendChild(Object.assign(document.createElement("div"), { className: "pm-sep" })); continue; }
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pm-item" + (it.danger ? " danger" : "") + (it.checked ? " on" : "");
    b.setAttribute("role", "menuitem");
    b.innerHTML = `<svg class="i"><use href="#i-${it.icon}"/></svg><span class="pm-text"><span></span></span>`;
    b.querySelector(".pm-text span").textContent = it.label;
    if (it.sub) b.querySelector(".pm-text").appendChild(Object.assign(document.createElement("small"), { textContent: it.sub }));
    b.disabled = !!it.disabled;
    if (it.title) b.title = it.title;
    b.onclick = (e) => { e.stopPropagation(); closeMenu(); it.run(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
  m.style.top = (r.bottom + h + 8 > window.innerHeight ? r.top - h - 4 : r.bottom + 4) + "px";
  openMenu = m;
  (m.querySelector("button:not(:disabled)") || m).focus();
}
function chatMenu(anchor, c, li) {
  popMenu(anchor, [
    { icon: "edit", label: L("Umbenennen", "Rename"), run: () => renameInline(li.querySelector(".t"), c) },
    { icon: "pin", label: c.starred ? L("Lösen", "Unpin") : L("Anheften", "Pin"),
      run: () => api("POST", `/api/chats/${c.id}/star`, { starred: !c.starred }).then(loadChats).catch(() => {}) },
    { icon: "download", label: L("Als Markdown exportieren", "Export as Markdown"), run: () => exportChat(c.id) },
    { icon: "trash", label: L("Löschen", "Delete"), danger: true, run: () => deleteChat(c) },
  ]);
}
function renameInline(target, c) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-input";
  input.value = c.title;
  input.maxLength = 120;
  const old = target.textContent;
  target.textContent = "";
  target.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    input.remove();
    target.textContent = save && title ? title : old;
    if (save && title && title !== c.title) {
      api("PATCH", `/api/chats/${c.id}`, { title }).then(() => {
        loadChats();
        if (c.active || c.id === S.chatId) $("chat-title").textContent = title;
      }).catch(() => {});
    }
  };
  input.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
  };
  input.onblur = () => finish(true);
}
function exportChat(id) {
  const a = document.createElement("a");
  a.href = `/api/chats/${encodeURIComponent(id)}/export`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
async function deleteChat(c) {
  if (c.active && !await confirmLeaveSandbox(true)) return;
  const box = c.sandbox ? L("\n\nDie Arbeitskopie der Sandbox wird mitgelöscht – nicht übernommene Änderungen gehen verloren.",
    "\n\nThe sandbox working copy is deleted too – changes not applied are lost.") : "";
  const extra = box + (c.legacy ? L("\n\nHinweis: Dieser Chat stammt von vor der Chat-Historie – ältere Tagebuch-Einträge "
    + "daraus lassen sich nicht zuordnen und bleiben im Gedächtnis.",
    "\n\nNote: this chat predates the chat history – older journal entries from it cannot be attributed and stay in memory.") : "");
  if (await ask(L(`„${c.title}“ löschen?\n\nDer Chat wird auch aus Orbwise' Gedächtnis entfernt (Tagebuch, Suche, `
      + `Tageszusammenfassung). Gelernte Fakten bleiben.`, `Delete “${c.title}”?\n\nThe chat is also removed from `
      + `Orbwise' memory (journal, search, daily summary). Learned facts are kept.`) + extra)) {
    api("DELETE", `/api/chats/${c.id}`).then(() => { toast(L("Chat gelöscht.", "Chat deleted.")); loadChats(); }).catch(() => {});
  }
}
// Kopfzeile: Titel anklicken benennt um, ⋯ bietet Exportieren/Löschen für den offenen Chat
async function activeChat() {
  try { return (await getJSON("/api/chats")).find((x) => x.active) || null; } catch { return null; }
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  chatSearchTimer = null;

  // Kleines Menü an einem Knopf (Chatliste, Kopfzeile)
  openMenu = null;
  document.addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  $("chat-title").onclick = async () => {
    const c = await activeChat();
    if (c) renameInline($("chat-title"), c);
  };
  $("chat-more").onclick = async (e) => {
    e.stopPropagation();
    const anchor = e.currentTarget;
    const c = await activeChat();
    if (!c) return;
    popMenu(anchor, [
      { icon: "edit", label: L("Umbenennen", "Rename"), run: () => renameInline($("chat-title"), c) },
      { icon: "download", label: L("Als Markdown exportieren", "Export as Markdown"), run: () => exportChat(c.id) },
      { icon: "trash", label: L("Löschen", "Delete"), danger: true, run: () => deleteChat(c) },
    ]);
  };
  $("chat-search").addEventListener("input", () => {
    clearTimeout(chatSearchTimer);
    chatSearchTimer = setTimeout(loadChats, 250);
  });
  $("chat-new").onclick = () => newChat();
  // Privater Chat an/aus: noch einmal drücken verlässt ihn (dann ist er weg) und öffnet einen neuen normalen Chat
  $("chat-private").onclick = () => api("POST", S.private ? "/api/chats" : "/api/chats/private").catch(() => {});
}
