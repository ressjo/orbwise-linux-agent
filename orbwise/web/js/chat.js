/* Orbwise – Chat: Nachrichten, Markdown, Aktionen an Nachrichten, Planmodus */

import { liveLine, statsLine } from "./activity.js";
import { fileChip } from "./attach.js";
import { $, L, LOCALE, S, api, ico } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { send } from "./ws.js";

export let chat, assistants, Thought, isBusy;
let follow, userScrollAt, pinQueued, atBottom, toBottom, dirty, renderQueued, GOAL_MARK, PLAN_OUTCOME;

export function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function renderMarkdown(src) {
  const blocks = [];
  let text = src.replace(/```[\w-]*\n?([\s\S]*?)(```|$)/g, (_, code) => {
    blocks.push(`<div class="code"><button class="copy" type="button" title="${L("Kopieren", "Copy")}">${ico("copy")}</button>`
      + `<pre>${escapeHtml(code.replace(/\n$/, ""))}</pre></div>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  // Inline-Code und Links vorab als Platzhalter – darin wird nichts formatiert und kein „|“ trennt Tabellenzellen
  const inl = [];
  const keep = (html) => { inl.push(html); return `\u0002${inl.length - 1}\u0002`; };
  text = escapeHtml(text)
    .replace(/\\\|/g, "\u0001")
    .replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, t, u) => keep(`<a href="${u}" target="_blank" rel="noopener">${t}</a>`))
    .replace(/(^|[\s(])(https?:\/\/[^\s<)|]+)/g, (_, pre, u) => pre + keep(`<a href="${u}" target="_blank" rel="noopener">${u}</a>`))
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_(?!\s)([^_\n]+?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  // Tabellen (GitHub-Markdown): Kopfzeile, direkt darunter die Trennzeile |---|:--:|
  const SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const table = (rows) => {
    const head = cells(rows[0]);
    const align = cells(rows[1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
    const td = (tag, c, i) => `<${tag}${align[i] ? ` style="text-align:${align[i]}"` : ""}>${c}</${tag}>`;
    const fit = (r) => head.map((_, i) => r[i] ?? "");
    const body = rows.slice(2).map((r) => `<tr>${fit(cells(r)).map((c, i) => td("td", c, i)).join("")}</tr>`).join("");
    return `<div class="md-table"><table><thead><tr>${head.map((c, i) => td("th", c, i)).join("")}</tr></thead>`
      + `<tbody>${body}</tbody></table></div>`;
  };
  const html = text.split(/\n{2,}/).map((para) => {
    if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
    // Zeilenweise: Überschriften (## …), Listen (- … / 1. …), Tabellen, Zitate, Trennlinien und normaler Text
    const out = [];
    let list = null, text = [], quote = [];
    const flushText = () => { if (text.length) out.push(`<p>${text.join("<br>")}</p>`); text = []; };
    const flushQuote = () => { if (quote.length) out.push(`<blockquote>${quote.join("<br>")}</blockquote>`); quote = []; };
    // nummerierte Listen behalten ihre Nummer – auch wenn Leerzeilen sie in mehrere Absätze teilen („1. … 2. …“)
    const flushList = () => {
      if (list) out.push(`<${list.tag}${list.start > 1 ? ` start="${list.start}"` : ""}>${list.items.join("")}</${list.tag}>`);
      list = null;
    };
    const flushAll = () => { flushText(); flushList(); flushQuote(); };
    const lines = para.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (l.includes("|") && i + 1 < lines.length && SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        let j = i + 2;
        while (j < lines.length && lines[j].includes("|") && lines[j].trim()) j++;
        flushAll();
        out.push(table(lines.slice(i, j)));
        i = j - 1;
        continue;
      }
      const h = /^\s*#{1,4}\s+(.+)$/.exec(l);
      const li = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(l);
      const q = /^\s*&gt;\s?(.*)$/.exec(l);
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { flushAll(); out.push("<hr>"); }
      else if (h) { flushAll(); out.push(`<div class="md-h">${h[1]}</div>`); }
      else if (q) { flushText(); flushList(); quote.push(q[1]); }
      else if (li) {
        const tag = /\d/.test(li[1]) ? "ol" : "ul";
        flushText(); flushQuote();
        if (list && list.tag !== tag) flushList();
        list = list || { tag, items: [], start: tag === "ol" ? parseInt(li[1], 10) : 1 };
        list.items.push(`<li>${li[2]}</li>`);
      } else if (l.trim()) { flushList(); flushQuote(); text.push(l); }
    }
    flushAll();
    return out.join("");
  }).join("");
  return html.replace(/\u0002(\d+)\u0002/g, (_, i) => inl[+i]).replace(/\u0001/g, "|")
    .replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}
export function scrollChat() { follow = true; toBottom(); }
export function pinChat() {
  if (!follow || pinQueued) return;
  pinQueued = true;
  requestAnimationFrame(() => { pinQueued = false; if (follow) toBottom(); });
}

// Ecken eines Panels kurz aufleuchten lassen (neue Nachricht / neue Aktivität)
export function addMsg(cls, who, html) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.innerHTML = `<div class="who">${who}</div><div class="tools"></div><div class="body">${html}</div>`;
  chat.appendChild(el);
  scrollChat();
  return el;
}

function imageBox(names, cls = "") {
  const box = document.createElement("div");
  box.className = "msg-images" + (cls ? " " + cls : "");
  for (const name of names) {
    const img = document.createElement("img");
    img.src = `/api/attachments/${encodeURIComponent(name)}`;
    img.alt = "";
    img.loading = "lazy";
    img.onclick = () => window.open(img.src, "_blank");
    box.appendChild(img);
  }
  return box;
}
// Screenshots des Agenten: unter dem Antworttext (nicht in .body – die wird beim Streamen neu gerendert)
export function addShots(el, names) {
  let box = el.querySelector(".msg-shots");
  if (!box) { box = imageBox([], "msg-shots"); el.querySelector(".body").after(box); }
  for (const img of [...imageBox(names).children]) box.appendChild(img);
}
export function addUser(text, source, images, turn, editable, files) {
  const el = addMsg("user", source === "voice" ? L("Du · Sprache", "You · voice") : L("Du", "You"), escapeHtml(text));
  el._raw = text;
  if (turn) el.dataset.turn = turn;
  userActions(el, editable !== false && !!turn);
  if (images && images.length) el.querySelector(".body").prepend(imageBox(images));
  if (files && files.length) {
    const box = document.createElement("div");
    box.className = "msg-files";
    for (const d of files) {
      box.appendChild(fileChip(d.title, d, () => window.open(`/api/attachments/${encodeURIComponent(d.name)}`, "_blank", "noopener")));
    }
    el.querySelector(".body").prepend(box);
  }
}
// Ziel-Modus (/goal): Chip über der Eingabe, solange ein Ziel läuft; Meldungen bei Ende/Grenze/Rückfrage
export function renderGoal() {
  const chip = $("goal-chip");
  chip.classList.toggle("hidden", !S.goal);
  if (!S.goal) return;
  chip.querySelector(".goal-text").textContent = L("Ziel: ", "Goal: ") + S.goal.text;
  chip.querySelector(".goal-steps").textContent = S.goal.steps ? L(` · Schritt ${S.goal.steps}`, ` · step ${S.goal.steps}`) : "";
  chip.title = S.goal.text;
}
export function goalEvent(ev) {
  const text = {
    started: L(`Ziel-Modus: ${ev.text}`, `Goal mode: ${ev.text}`),
    done: L(`Ziel erreicht (${ev.steps} Schritte).`, `Goal reached (${ev.steps} steps).`),
    question: L("Ziel-Modus wartet auf deine Antwort.", "Goal mode is waiting for your answer."),
    stopped: L("Ziel-Modus beendet.", "Goal mode ended."),
    limit: L("Ziel-Modus angehalten (Grenze) – „/goal weiter“ setzt fort.", "Goal mode paused (limit) – “/goal continue” carries on."),
    active: L(`Aktives Ziel: ${ev.text}`, `Active goal: ${ev.text}`),
  }[ev.state];
  S.goal = ["done", "stopped"].includes(ev.state) ? null : { text: ev.text, steps: ev.steps || 0 };
  renderGoal();
  if (ev.state !== "progress" && text) toast(text);
  if (ev.state === "progress" && S.currentMsg && assistants[S.currentMsg]) {
    const n = document.createElement("div");
    n.className = "goal-nudge";
    n.textContent = L("↻ weiter am Ziel", "↻ continuing with the goal");
    assistants[S.currentMsg].el.querySelector(".body").after(n);
  }
}
export function addSystem(text) {
  const el = document.createElement("div");
  el.className = "msg system";
  el.textContent = text;
  chat.appendChild(el);
  scrollChat();
}
export function addError(text) { addMsg("assistant error", "System", escapeHtml(text)); }

// Trenner, wo der Kontext zusammengefasst wurde – darüber bleibt alles sichtbar, das Modell arbeitet ab hier
// mit der (aufklappbaren) Zusammenfassung weiter
export function compactDivider(summary, ts) {
  const el = document.createElement("details");
  el.className = "compact-divider";
  const when = ts ? " · " + new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "";
  el.innerHTML = `<summary><span>${L("Kontext zusammengefasst", "Context summarised")}${when}</span></summary>
      <div class="body"></div>`;
  el.querySelector(".body").innerHTML = renderMarkdown(summary || "");
  return el;
}
export function addCompactDivider(ev) {
  const el = compactDivider(ev.summary, Date.now() / 1000);
  // Mitten in einer Antwort: vor die laufende Antwort setzen (sie geht danach weiter)
  const current = S.currentMsg && assistants[S.currentMsg] ? assistants[S.currentMsg].el : null;
  if (current && current.parentNode === chat) chat.insertBefore(el, current);
  else chat.appendChild(el);
  scrollChat();
}

export function startAssistant(id, plan) {
  const el = addMsg("assistant streaming" + (plan ? " plan" : ""), S.name, "");
  el.dataset.msg = id;
  if (S.turns) el.dataset.turn = S.turns;
  // statt eines blinkenden Cursors: was das Modell gerade macht (Einlesen, Denken, Aufruf schreiben …)
  const live = liveLine(L("wartet auf das Modell", "waiting for the model"));
  el.appendChild(live);
  assistants[id] = { el, raw: "", live, steps: [], started: performance.now() };
  S.currentMsg = id;
}
function renderAssistant(a) {
  // nur Leerraum: leer lassen (der Plankasten erscheint erst mit dem ersten Wort)
  const raw = a.raw.replace(GOAL_MARK, "");
  a.el.querySelector(".body").innerHTML = raw.trim() ? renderMarkdown(raw.replace(/\n{3,}/g, "\n\n")) : "";
}
function flushTokens() {
  renderQueued = false;
  if (!dirty.size) return;
  for (const a of dirty) renderAssistant(a);
  dirty.clear();
  pinChat();
}
export function appendToken(id, text) {
  const a = assistants[id] || (startAssistant(id), assistants[id]);
  a.raw += text;
  dirty.add(a);
  if (!renderQueued) {
    renderQueued = true;
    // rAF, mit Timeout-Fallback falls der Tab im Hintergrund ist
    let done = false;
    const go = () => { if (!done) { done = true; flushTokens(); } };
    requestAnimationFrame(go);
    setTimeout(go, 100);
  }
}

export function finishAssistant(id, cancelled) {
  const a = assistants[id];
  if (!a) return;
  if (dirty.has(a)) { dirty.delete(a); renderAssistant(a); pinChat(); }
  a.el.classList.remove("streaming");
  a.live.remove();
  a.el._raw = a.raw.trim();
  assistantActions(a.el);
  if (a.steps.length) ensureActions(a.el).appendChild(statsLine(a, cancelled));
  if (!a.raw.trim()) {
    a.el.querySelector(".body").innerHTML = cancelled ? "<em>(abgebrochen)</em>" : "";
    if (!cancelled && !a.el.querySelector(".tool-chip")) a.el.remove();
  }
  delete assistants[id];
  markLast();
}

// ---------------------------------------------------------------- Aktionen an Nachrichten
// Kopieren, Bearbeiten (eigene Nachricht), Neu generieren (letzte Antwort), 👍/👎, Statistik – erscheinen beim
// Darüberfahren bzw. an der letzten Antwort; am Handy immer (dezent).
function ensureActions(el) {
  let bar = el.querySelector(":scope > .msg-actions");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "msg-actions";
    el.appendChild(bar);
  }
  return bar;
}
function actBtn(icon, title, onclick, cls = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ma-btn " + cls;
  b.title = title;
  b.setAttribute("aria-label", title);
  b.innerHTML = `<svg class="i"><use href="#i-${icon}"/></svg>`;
  b.onclick = (e) => { e.stopPropagation(); onclick(b); };
  return b;
}
export async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  if (btn) { btn.classList.add("ok"); setTimeout(() => btn.classList.remove("ok"), 1200); }
  toast(L("Kopiert.", "Copied."));
}
function userActions(el, editable) {
  const bar = ensureActions(el);
  bar.appendChild(actBtn("copy", L("Kopieren", "Copy"), (b) => copyText(el._raw || "", b)));
  if (editable) bar.appendChild(actBtn("edit", L("Bearbeiten und neu senden", "Edit and resend"), () => editUser(el)));
}
export function assistantActions(el) {
  const bar = ensureActions(el);
  if (bar.querySelector(".ma-copy")) return;
  bar.prepend(actBtn("copy", L("Antwort kopieren (Markdown)", "Copy answer (Markdown)"), (b) => copyText(el._raw || "", b), "ma-copy"));
  if (el.dataset.turn && !el.classList.contains("locked")) {
    bar.insertBefore(actBtn("redo", L("Neu generieren", "Regenerate"), () => rewind(Number(el.dataset.turn)), "ma-regen"),
                     bar.querySelector(".ma-copy").nextSibling);
  }
}
// „Letzte Antwort“: dort bleiben die Aktionen sichtbar und nur dort gibt es „Neu generieren“
export function markLast() {
  chat.querySelectorAll(".msg.last").forEach((x) => x.classList.remove("last"));
  const msgs = chat.querySelectorAll(".msg.assistant:not(.streaming):not(.error)");
  if (msgs.length) msgs[msgs.length - 1].classList.add("last");
}
async function rewind(turn, text) {
  if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return false; }
  try {
    await api("POST", "/api/chats/active/rewind", text == null ? { turn } : { turn, text });
    return true;
  } catch { return false; }  // Meldung kommt von api()
}
function editUser(el) {
  if (el.querySelector(".msg-edit")) return;
  if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
  const turn = Number(el.dataset.turn);
  const body = el.querySelector(".body");
  const form = document.createElement("form");
  form.className = "msg-edit";
  form.innerHTML = `<textarea rows="3"></textarea><div class="me-btns"><span class="me-hint"></span>`
    + `<button type="button" class="btn small ghost me-cancel"></button><button type="submit" class="btn small primary"></button></div>`;
  const ta = form.querySelector("textarea");
  ta.value = el._raw || "";
  form.querySelector(".me-cancel").textContent = L("Abbrechen", "Cancel");
  form.querySelector("[type=submit]").textContent = L("Senden", "Send");
  const later = (S.turns || turn) - turn;
  form.querySelector(".me-hint").textContent = later > 0
    ? L(`Die ${later + 1} Antworten ab hier werden ersetzt.`, `The ${later + 1} answers from here are replaced.`)
    : L("Enter sendet · Esc bricht ab", "Enter sends · Esc cancels");
  const close = () => { form.remove(); body.hidden = false; el.classList.remove("editing"); };
  form.querySelector(".me-cancel").onclick = close;
  ta.onkeydown = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    if (later > 0 && !await ask(L(`Ab dieser Nachricht neu beginnen? Die ${later + 1} Antworten danach werden ersetzt.`,
                                `Restart from this message? The ${later + 1} answers after it are replaced.`))) return;
    if (await rewind(turn, text)) close();
  };
  body.hidden = true;
  el.classList.add("editing");
  el.insertBefore(form, el.querySelector(".msg-actions"));
  ta.style.height = Math.min(320, Math.max(72, body.scrollHeight + 24)) + "px";
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}
export function cutChat(turn) {
  if (!turn) return;
  const first = [...chat.children].find((el) => Number(el.dataset.turn) >= turn);
  if (first) {
    let el = first;
    while (el) { const next = el.nextElementSibling; el.remove(); el = next; }
  }
  S.turns = turn - 1;  // die neu gestellte Frage bekommt gleich wieder diese Nummer
  markLast();
}
export function showPlan(ev) {
  const el = chat.querySelector(`.msg[data-msg="${ev.id}"]`);
  if (!el || el.querySelector(".plan-bar")) return;
  const bar = document.createElement("div");
  bar.className = "plan-bar";
  bar.innerHTML = `<div class="plan-actions">
        <button class="btn plan-run">${ico("play")}${L("Ausführen", "Run")}</button>
        <button class="btn plan-edit">${ico("edit")}${L("Ändern", "Change")}</button>
        <button class="btn plan-drop">${ico("x")}${L("Verwerfen", "Discard")}</button></div>
      <form class="plan-revise hidden"><input type="text" maxlength="4000"
        placeholder="${L("Was soll anders sein? (Enter sendet)", "What should be different? (Enter sends)")}"></form>
      <div class="plan-status"></div>`;
  el.appendChild(bar);
  const input = bar.querySelector("input");
  bar.querySelector(".plan-run").onclick = () => send({ type: "plan_accept", id: ev.id });
  bar.querySelector(".plan-drop").onclick = () => send({ type: "plan_discard", id: ev.id });
  bar.querySelector(".plan-edit").onclick = () => {
    bar.querySelector(".plan-revise").classList.toggle("hidden");
    input.focus();
  };
  bar.querySelector(".plan-revise").onsubmit = (e) => {
    e.preventDefault();
    if (input.value.trim()) send({ type: "plan_revise", id: ev.id, text: input.value.trim() });
  };
  scrollChat();
}
export function closePlan(id, outcome) {
  const bar = chat.querySelector(`.msg[data-msg="${id}"] .plan-bar`);
  if (!bar) return;
  bar.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
  bar.querySelector(".plan-revise").classList.add("hidden");
  bar.querySelector(".plan-status").textContent = PLAN_OUTCOME[outcome] || outcome;
  bar.classList.add("closed", outcome);
}

export function showTranscript(text) {
  const el = $("live-transcript");
  el.textContent = text ? L(`„${text}“`, `“${text}”`) : "";
  el.style.opacity = 1;
  clearTimeout(showTranscript.t);
  showTranscript.t = setTimeout(() => { el.style.opacity = 0; }, 5000);
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  chat = $("chat");
  assistants = {};

  // Mitlaufen beim Generieren: unten bleiben, solange der Nutzer nicht selbst hochscrollt. Jede Änderung im Chat
  // (Text, Gedankenblock, Werkzeug-Zeilen, Bilder) zieht nach – nicht nur die Stellen, die selbst scrollen.
  follow = true;
  userScrollAt = 0;
  pinQueued = false;
  atBottom = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
  // sofort, nicht „smooth“ (CSS): eine Animation je Token hinkt beim Streamen hinterher und bricht ab
  toBottom = () => chat.scrollTo({ top: chat.scrollHeight, behavior: "instant" });
  for (const ev of ["wheel", "touchmove", "keydown", "pointerdown"]) {
    chat.addEventListener(ev, () => { userScrollAt = performance.now(); }, { passive: true });
  }
  // nur eine Bewegung des Nutzers entscheidet (eigenes Nachziehen oder Scroll-Anker des Browsers zählen nicht)
  chat.addEventListener("scroll", () => { if (performance.now() - userScrollAt < 1000) follow = atBottom(); }, { passive: true });
  new MutationObserver(pinChat).observe(chat, { childList: true, subtree: true, characterData: true,
                                                attributes: true, attributeFilter: ["open", "class"] });
  chat.addEventListener("load", pinChat, true);
  chat.addEventListener("transitionend", pinChat, true);

  // Kopieren-Knopf an Code-Blöcken (Ereignis-Delegation, auch für später gerenderte Nachrichten)
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".code .copy");
    if (!btn) return;
    const text = btn.parentElement.querySelector("pre").textContent;
    try { await navigator.clipboard.writeText(text); } catch {
      const r = document.createRange(); r.selectNodeContents(btn.parentElement.querySelector("pre"));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); document.execCommand("copy"); sel.removeAllRanges();
    }
    btn.innerHTML = ico("check"); btn.classList.add("done");
    setTimeout(() => { btn.innerHTML = ico("copy"); btn.classList.remove("done"); }, 1200);
  });
  $("goal-chip").querySelector(".goal-stop").onclick = () => send({ type: "user_message", text: "/goal stop" });
  // Token werden gesammelt und höchstens einmal pro Bild (~16–60 ms) als Markdown gerendert –
  // vorher wurde bei jedem Token die ganze Antwort neu aufgebaut (bei langen Antworten sehr teuer).
  dirty = new Set();
  renderQueued = false;
  GOAL_MARK = /\s*\[(ZIEL ERREICHT|GOAL REACHED|FRAGE|QUESTION)\]\s*$/i;
  // Denkmodus: Gedankengang läuft direkt in der Antwort mit (aufgeklappt) und klappt zu, sobald das Modell etwas
  // ausgibt (Antwort-Token oder Werkzeugaufruf). Denkt es nach einem Werkzeug weiter, geht derselbe Block wieder auf.
  Thought = {
    byMsg: {}, warned: false,
    block(id) {
      let t = this.byMsg[id];
      if (t) return t;
      const a = assistants[id] || (startAssistant(id), assistants[id]);
      const el = document.createElement("details");
      el.className = "thought-log";
      el.innerHTML = "<summary></summary><pre></pre>";
      a.el.insertBefore(el, a.el.querySelector(".tools") || a.el.querySelector(".body"));  // zuerst gedacht, dann gehandelt
      t = this.byMsg[id] = { el, pre: el.querySelector("pre"), text: "", ms: 0, since: 0, queued: false, own: false, auto: false, msg: a };
      // Hat der Nutzer selbst auf- oder zugeklappt, bleibt es so
      el.addEventListener("toggle", () => { if (t.auto) t.auto = false; else t.own = true; });
      return t;
    },
    setOpen(t, open) {
      if (t.own || t.el.open === open) return;
      t.auto = true;
      t.el.open = open;
    },
    summary(t) {
      const secs = Math.max(1, Math.round(t.ms / 1000));
      t.el.querySelector("summary").textContent = t.since ? L("Denkt nach …", "Thinking …")
        : L(`Gedankengang · ${secs} s`, `Thoughts · ${secs} s`);
    },
    add(id, text) {
      if (!text) return;
      const t = this.block(id);
      if (!t.since) {  // (wieder) am Denken
        // solange gedacht wird, steht der Block ganz unten – nach Text oder Werkzeug sonst oben außer Sicht
        t.msg.el.insertBefore(t.el, t.msg.live && t.msg.live.parentNode === t.msg.el ? t.msg.live : null);
        t.since = performance.now();
        t.el.classList.add("live");
        this.summary(t);
        this.setOpen(t, true);
      }
      t.text += text;
      if (t.queued) return;
      t.queued = true;
      let done = false;
      const go = () => {
        if (done) return;
        done = true;
        t.queued = false;
        t.pre.textContent = t.text.trim();
        t.pre.scrollTop = t.pre.scrollHeight;
        pinChat();
      };
      requestAnimationFrame(go);
      setTimeout(go, 120);
    },
    collapse(id) {
      const t = this.byMsg[id];
      if (!t || !t.since) return;
      t.ms += performance.now() - t.since;
      t.since = 0;
      t.pre.textContent = t.text.trim();
      t.el.classList.remove("live");
      this.summary(t);
      this.setOpen(t, false);
      t.msg.el.insertBefore(t.el, t.msg.el.querySelector(".tools"));  // zugeklappt wieder oben: zuerst gedacht, dann gehandelt
    },
    finish(id) {
      const t = this.byMsg[id];
      this.collapse(id);
      delete this.byMsg[id];
      if (!t && S.think !== "off" && !this.warned) {
        this.warned = true;
        toast(L("Denkmodus an, aber das Modell hat keinen Gedankengang geliefert – ggf. '--reasoning-budget 0' aus dem Startbefehl entfernen.",
              "Thinking mode is on, but the model returned no reasoning – for llama-server remove '--reasoning-budget 0' from the start command."));
      }
    },
  };
  isBusy = () => !!S.serverState && S.serverState !== "idle";

  // ---------------------------------------------------------------- Planmodus
  PLAN_OUTCOME = {
    accepted: L("wird ausgeführt", "being carried out"), revised: L("wird überarbeitet", "being revised"),
    discarded: L("verworfen", "discarded"), replaced: L("ersetzt", "replaced"),
  };
}
