/* Orbwise – Bestätigungen und Coding: Diffs, Rückfragen, Änderungen, Hintergrund-Jobs */

import { actStep, acts, finishStep } from "./activity.js";
import { A, onSpeechDrained } from "./audio.js";
import { chat, escapeHtml, isBusy, scrollChat } from "./chat.js";
import { $, L, S, STATUS_TEXT, api, ico } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { refresh } from "./state.js";
import { send } from "./ws.js";

let EDIT_FIELDS, APPROVE_HTML;

// ---------------------------------------------------------------- Coding: Diffs, Rückfragen, Änderungen, Jobs
export function renderDiff(text) {
  return String(text || "").split("\n").map((ln) => {
    const cls = ln.startsWith("+++") || ln.startsWith("---") ? "d-file" : ln.startsWith("@@") ? "d-hunk"
      : ln.startsWith("+") ? "d-add" : ln.startsWith("-") ? "d-del" : "";
    return `<span class="${cls}">${escapeHtml(ln) || " "}</span>`;
  }).join("");
}

// Werkzeug question: Karte im Gespräch – Optionen anklicken (bei multiple mehrere) oder frei antworten
export function openQuestion(ev) {
  closeQuestion();
  const wrap = document.createElement("section");
  wrap.className = "confirm-wrap question-wrap";
  wrap.setAttribute("role", "dialog");
  const card = document.createElement("div");
  card.className = "confirm-card question-card";
  const blocks = (ev.questions || []).map((q, qi) => {
    const box = document.createElement("div");
    box.className = "q-block";
    box.innerHTML = `<div class="q-head">${q.header ? `<span class="q-tag"></span>` : ""}<span class="q-text"></span></div>
        <div class="q-options"></div><input class="q-other" type="text">`;
    if (q.header) box.querySelector(".q-tag").textContent = q.header;
    box.querySelector(".q-text").textContent = q.question;
    const other = box.querySelector(".q-other");
    other.placeholder = L("Andere Antwort …", "Other answer …");
    const opts = box.querySelector(".q-options");
    (q.options || []).forEach((o, oi) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "q-opt";
      b.innerHTML = `<span class="q-label"></span>${o.description ? `<span class="q-desc"></span>` : ""}`;
      b.querySelector(".q-label").textContent = o.label;
      if (o.description) b.querySelector(".q-desc").textContent = o.description;
      b.onclick = () => {
        if (!q.multiple) opts.querySelectorAll(".q-opt").forEach((x) => x.classList.remove("on"));
        b.classList.toggle("on");
        if (!q.multiple && ev.questions.length === 1) answerQuestion(collect());
      };
      if (oi === 0 && qi === 0) setTimeout(() => b.focus({ preventScroll: true }), 50);
      opts.appendChild(b);
    });
    other.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); answerQuestion(collect()); } };
    return box;
  });
  const collect = () => blocks.map((box) => {
    const text = box.querySelector(".q-other").value.trim();
    if (text) return text;
    return [...box.querySelectorAll(".q-opt.on .q-label")].map((x) => x.textContent);
  });
  const title = document.createElement("div");
  title.className = "confirm-title";
  title.innerHTML = `<span class="warn-dot"></span><span></span>`;
  title.lastChild.textContent = L("Orbwise hat eine Rückfrage", "Orbwise has a question");
  const actions = document.createElement("div");
  actions.className = "modal-actions";
  actions.innerHTML = `<button type="button" class="btn ghost q-skip"></button><button type="button" class="btn approve q-send"></button>`;
  actions.querySelector(".q-skip").textContent = L("Überspringen", "Skip");
  actions.querySelector(".q-send").textContent = L("Antworten", "Answer");
  actions.querySelector(".q-skip").onclick = () => answerQuestion(null);
  actions.querySelector(".q-send").onclick = () => answerQuestion(collect());
  card.append(title, ...blocks, actions);
  wrap.appendChild(card);
  chat.appendChild(wrap);
  S.question = { id: ev.id, el: wrap };
  const act = acts[ev.id];
  if (act) { act.classList.add("waiting"); act.querySelector(".act-status").textContent = STATUS_TEXT.waiting; }
  scrollChat();
  refresh();
}

export function answerQuestion(answers) {
  if (!S.question) return;
  const id = S.question.id;
  const empty = !answers || answers.every((a) => !a || !a.length);
  send({ type: "question_answer", id, answers: empty ? null : answers });
  closeQuestion(id);
}

export function closeQuestion(id) {
  if (!S.question || (id && S.question.id !== id)) return;
  S.question.el.remove();
  S.question = null;
  const act = acts[id];
  if (act) { act.classList.remove("waiting"); act._start = performance.now(); }
  refresh();
}

// Coding: geänderte Dateien unter der Antwort – aufklappbar, mit „Rückgängig“ (Schnappschuss, siehe snapshot.py)
export function showChanges(msgEl, turn, files, undone) {
  if (!msgEl || !files || !files.length) return;
  let box = msgEl.querySelector(".msg-changes");
  if (!box) {
    box = document.createElement("div");
    box.className = "msg-changes";
    msgEl.insertBefore(box, msgEl.querySelector(".msg-actions"));
  }
  box.dataset.turn = turn;
  const add = files.reduce((n, f) => n + (f.add || 0), 0);
  const del = files.reduce((n, f) => n + (f.del || 0), 0);
  const n = files.length;
  box.innerHTML = `<div class="mc-head"><button type="button" class="mc-toggle">${ico("file")}<span class="mc-sum"></span>
      <span class="mc-add">+${add}</span><span class="mc-del">−${del}</span></button><button type="button" class="btn small ghost mc-undo"></button></div>
      <ul class="mc-files"></ul>`;
  box.querySelector(".mc-sum").textContent = n === 1 ? L("1 Datei geändert", "1 file changed") : L(`${n} Dateien geändert`, `${n} files changed`);
  const ul = box.querySelector(".mc-files");
  for (const f of files) {
    const li = document.createElement("li");
    const tag = f.status === "added" ? L("neu", "new") : f.status === "deleted" ? L("gelöscht", "deleted") : "";
    li.innerHTML = `<span class="mc-path"></span>${tag ? `<span class="mc-tag"></span>` : ""}<span class="mc-add">${f.add == null ? "" : "+" + f.add}</span><span class="mc-del">${f.del == null ? "" : "−" + f.del}</span>`;
    li.querySelector(".mc-path").textContent = f.path;
    if (tag) li.querySelector(".mc-tag").textContent = tag;
    ul.appendChild(li);
  }
  box.querySelector(".mc-toggle").onclick = () => box.classList.toggle("open");
  box.querySelector(".mc-undo").onclick = async () => {
    const redo = box.classList.contains("undone");
    if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
    if (!redo && !await ask(L(`Die Änderungen dieser Antwort an ${n === 1 ? "1 Datei" : n + " Dateien"} zurücksetzen? Spätere Änderungen an denselben Dateien gehen dabei verloren.`,
                            `Undo this answer's changes to ${n === 1 ? "1 file" : n + " files"}? Later changes to the same files are lost.`))) return;
    try {
      const r = await api("POST", "/api/chats/active/revert", { turn: Number(turn), redo });
      setChangesUndone(box, r.undone);
      toast(redo ? L("Änderungen wiederhergestellt.", "Changes restored.") : L("Änderungen zurückgesetzt.", "Changes undone."));
    } catch { /* Meldung kommt von api() */ }
  };
  setChangesUndone(box, undone);
}

export function setChangesUndone(box, undone) {
  box.classList.toggle("undone", !!undone);
  const b = box.querySelector(".mc-undo");
  b.innerHTML = undone ? `${ico("redo")}${L("Wiederherstellen", "Restore")}` : `${ico("undo")}${L("Rückgängig", "Undo")}`;
}

// Hintergrund-Befehle (shell mit background=true): eigener Schritt mit Live-Ausgabe und „Stoppen“
export function jobEvent(ev) {
  const key = "job-" + ev.id;
  let el = acts[key];
  if (ev.phase === "start") {
    el = actStep("tool job", "code", L("Hintergrund-Job", "Background job"), ev.command || "", null);
    el.querySelector(".act-args").textContent = `${ev.id}  ${ev.cwd || ""}`;
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "btn small ghost act-stop";
    stop.textContent = L("Stoppen", "Stop");
    stop.onclick = (e) => { e.stopPropagation(); api("POST", `/api/jobs/${ev.id}/stop`).catch(() => {}); };
    el.querySelector(".act-state").prepend(stop);
    acts[key] = el;
    return;
  }
  if (!el) return;
  if (ev.phase === "output") {
    const out = el.querySelector(".act-out");
    out.textContent = (out.textContent + ev.text).slice(-20000);
    out.scrollTop = out.scrollHeight;
  } else if (ev.phase === "done") {
    el.querySelector(".act-stop")?.remove();
    const ok = ev.status === "exit" && ev.rc === 0;
    finishStep(el, ok ? "ok" : ev.status === "stopped" ? "denied" : "error",
               ev.ended && ev.started ? ev.ended - ev.started : undefined);
    if (ev.status === "stopped") el.querySelector(".act-status").textContent = L("gestoppt", "stopped");
    else if (!ok) el.querySelector(".act-status").textContent = `Exit ${ev.rc}`;
    delete acts[key];
  }
}

export function openConfirm(ev) {
  S.confirm = ev;
  S.confirmListenSent = false;
  const editable = (ev.editable || []).filter((k) => EDIT_FIELDS[k]);
  const form = $("confirm-form");
  form.innerHTML = "";
  for (const key of editable) {
    const [label, kind] = EDIT_FIELDS[key];
    const row = document.createElement("label");
    row.innerHTML = `<span>${label}</span>`;
    const field = document.createElement(kind);
    if (kind === "input") field.type = "text";
    field.dataset.key = key;
    field.value = ev.args[key] ?? "";
    field.spellcheck = key === "body" || key === "subject";
    row.appendChild(field);
    form.appendChild(row);
  }
  form.classList.toggle("hidden", !editable.length);
  $("confirm-cmd").classList.toggle("hidden", !!editable.length);
  $("confirm").classList.toggle("editing", !!editable.length);
  $("confirm-yes").innerHTML = editable.length && ev.name === "mail_send"
    ? `${L("Senden", "Send")} <kbd>${L("Strg", "Ctrl")}+Enter</kbd>` : editable.length ? `${L("Ausführen", "Run")} <kbd>${L("Strg", "Ctrl")}+Enter</kbd>` : APPROVE_HTML;
  $("confirm-summary").textContent = ev.name === "mail_send"
    ? L("Mail prüfen, bei Bedarf ändern und senden:", "Check the e-mail, edit it if needed and send it:")
    : L(`Soll ich ${ev.summary} ausführen?`, `Shall I run ${ev.summary}?`);
  const cmd = $("confirm-cmd");
  const preview = S.previews[ev.id];
  cmd.classList.toggle("diff", !!preview);
  if (preview) cmd.innerHTML = renderDiff(preview);
  else cmd.textContent = ev.name === "run_shell" || ev.name === "shell"
    ? (ev.args.workdir ? `(${ev.args.workdir}) ` : "") + ev.args.command : S.commands[ev.id] || argLines(ev.args);
  // ohne Befehl, Diff oder Angaben (z. B. Systemupdate) sagt die Frage schon alles – kein leerer Kasten
  if (!editable.length) cmd.classList.toggle("hidden", !preview && !cmd.textContent.trim());
  $("confirm-reason").textContent = ev.reason && !editable.length ? L("Grund: ", "Reason: ") + ev.reason : "";
  $("confirm-trust").classList.toggle("hidden", !ev.trustable || !!editable.length);
  $("confirm-voice").textContent = editable.length ? L("Senden nur per Klick – „Nein“ bricht ab.", "Send only by click – “no” cancels.")
    : A.micReady ? L("oder sag „Ja“ bzw. „Nein“", "or say “yes” or “no”") : "";
  $("confirm-voice").classList.remove("listening");
  placeConfirm();
  const act = acts[ev.id];
  if (act) { act.classList.add("waiting"); act.querySelector(".act-status").textContent = STATUS_TEXT.waiting; }
  setTimeout(() => {
    (form.querySelector("input, textarea") || $("confirm-yes")).focus({ preventScroll: true });
    scrollChat();
  }, 50);
  refresh();
  if (!S.tts) onSpeechDrained();
}

// Angaben eines Aufrufs lesbar untereinander („Ort: Berlin“) statt als name({json})
function argLines(args) {
  return Object.entries(args || {}).filter(([, v]) => v !== "" && v != null)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n");
}

function editedFields() {
  const out = {};
  for (const f of $("confirm-form").querySelectorAll("[data-key]")) out[f.dataset.key] = f.value;
  return out;
}

// Die Karte steht am Ende des Gesprächs – auch nachdem der Chat neu gezeichnet wurde
export function placeConfirm() {
  const card = $("confirm");
  card.classList.remove("hidden");
  if (card.parentElement !== chat || chat.lastElementChild !== card) chat.appendChild(card);
}

export function parkConfirm() {
  const card = $("confirm");
  card.classList.add("hidden");
  $("confirm-home").appendChild(card);
}

export function closeConfirm(id) {
  if (S.confirm && (!id || S.confirm.id === id)) {
    S.confirm = null;
    parkConfirm();
    if (S.recording) send({ type: "cancel_listen" });
    refresh();
  }
}

export function answerConfirm(approved, trust = false) {
  if (!S.confirm) return;
  const editing = !$("confirm-form").classList.contains("hidden");
  const args = editing && approved ? editedFields() : null;
  if (args && "to" in args && !args.to.trim()) {
    const to = $("confirm-form").querySelector('[data-key="to"]');
    to.classList.add("invalid");
    to.focus();
    toast(L("Bitte einen Empfänger eintragen.", "Please enter a recipient."));
    return;
  }
  send(args ? { type: "confirm", id: S.confirm.id, approved, args }
    : { type: "confirm", id: S.confirm.id, approved, ...(trust ? { trust: true } : {}) });
  if (trust) toast(L("In diesem Chat fragt fremder Inhalt nicht mehr nach – bis neuer dazukommt.",
    "Outside content no longer asks in this chat – until new content arrives."));
  const act = acts[S.confirm.id];
  if (act && approved) { act.classList.remove("waiting"); act._start = performance.now(); act.querySelector(".act-status").textContent = ""; }
  closeConfirm();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Felder, die der Nutzer vor dem Bestätigen noch ändern darf (z. B. mail_send)
  EDIT_FIELDS = {
    to: [L("AN", "TO"), "input"], cc: [L("CC", "CC"), "input"],
    subject: [L("Betreff", "Subject"), "input"], body: [L("Text", "Text"), "textarea"],
  };
  APPROVE_HTML = $("confirm-yes").innerHTML;
  $("confirm-yes").onclick = () => answerConfirm(true);
  $("confirm-trust").onclick = () => answerConfirm(true, true);
  $("confirm-no").onclick = () => answerConfirm(false);
}
