/* Orbwise – Planer: Routinen und Briefing-Einstellungen */

import { escapeHtml } from "./chat.js";
import { $, L, S, api, getJSON, ico } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { IV, renderImagePill } from "./images.js";
import { confirmLeaveSandbox } from "./project.js";
import { greet, loadReminders } from "./reminders.js";
import { refresh } from "./state.js";

let DAYS, R, B;

export function loadPlanner() {
  loadRoutines();
  loadReminders();
  if ($("brief-box").open) loadBriefing();
}

function renderDayChips() {
  const box = $("rt-days");
  box.innerHTML = "";
  DAYS.forEach((d, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "rt-day" + (R.days.has(i) ? " on" : "");
    b.textContent = d;
    b.onclick = () => { R.days.has(i) ? R.days.delete(i) : R.days.add(i); renderDayChips(); };
    box.appendChild(b);
  });
  const hint = document.createElement("span");
  hint.className = "rt-day-hint";
  hint.textContent = R.days.size ? "" : L("täglich", "daily");
  box.appendChild(hint);
}

function openRoutineForm(r) {
  R.edit = r ? r.id : null;
  R.days = new Set(r ? r.days : []);
  $("rt-name").value = r ? r.name : "";
  $("rt-task").value = r ? r.task : "";
  $("rt-time").value = r ? r.time : "08:00";
  $("rt-date").value = r ? r.date : "";
  $("rt-error").textContent = "";
  renderDayChips();
  $("rt-form").classList.remove("hidden");
  $("rt-new").classList.add("hidden");
  $("rt-task").focus();
}

function closeRoutineForm() {
  R.edit = null;
  $("rt-form").classList.add("hidden");
  $("rt-new").classList.remove("hidden");
}

export async function loadRoutines() {
  try { R.items = await getJSON("/api/routines"); } catch { return; }
  const ul = $("rt-list");
  ul.innerHTML = "";
  if (!R.items.length) {
    ul.innerHTML = `<li class="empty">${L("Noch keine Routinen – z. B. „Werktags um 8 Linux-News suchen“.",
                                           "No routines yet – e.g. “Search Linux news on weekdays at 8”.")}</li>`;
    return;
  }
  for (const r of R.items) {
    const li = document.createElement("li");
    li.className = "rt-item" + (r.enabled ? "" : " off");
    const status = r.last_status || "none";
    li.innerHTML = `<input type="checkbox" ${r.enabled ? "checked" : ""} title="${L("aktiv / pausiert", "active / paused")}">
        <div class="rt-main"><div class="rt-name"><span class="rt-dot ${status}"></span><span class="n"></span></div>
          <div class="rt-meta"></div></div>
        <button class="ghost" data-a="run" title="${L("jetzt ausführen", "run now")}">${ico("play")}</button>
        <button class="ghost" data-a="edit" title="${L("bearbeiten", "edit")}">${ico("edit")}</button>
        <button class="ghost" data-a="del" title="${L("löschen", "delete")}">${ico("trash")}</button>`;
    li.querySelector(".n").textContent = r.name;
    // „morgen 08:00“ → „morgen“, wenn die Uhrzeit ohnehin im Zeitplan steht
    const nextShort = r.next.endsWith(" " + r.time) ? r.next.slice(0, -r.time.length - 1) : r.next;
    const next = r.enabled && r.next !== "–" ? ` · ${nextShort}` : r.enabled ? "" : ` · ${L("pausiert", "paused")}`;
    li.querySelector(".rt-meta").textContent = r.schedule + next;
    li.querySelector(".rt-meta").title = r.enabled && r.next !== "–" ? `${L("nächste Ausführung", "next run")}: ${r.next}` : "";
    li.querySelector(".rt-main").title = r.task + (r.last_summary ? "\n\n" + L("Zuletzt: ", "Last: ") + r.last_summary : "");
    li.querySelector(".rt-main").onclick = async () => {
      if (!r.chat_id) { toast(L("Noch kein Ergebnis – „Jetzt ausführen“ startet die Routine.", "No result yet – “run now” starts the routine.")); return; }
      if (!await confirmLeaveSandbox()) return;
      api("POST", `/api/chats/${r.chat_id}/activate`).catch(() => {});
    };
    li.querySelector("input").onchange = (e) => api("PUT", `/api/routines/${r.id}`, { enabled: e.target.checked })
      .then(loadRoutines).catch(() => toast(L("Speichern fehlgeschlagen", "Saving failed")));
    li.querySelectorAll("button").forEach((b) => b.onclick = async () => {
      if (b.dataset.a === "edit") return openRoutineForm(r);
      if (b.dataset.a === "del") {
        if (!await ask(L(`Routine „${r.name}“ löschen? Ihr Chat bleibt im Verlauf.`, `Delete routine “${r.name}”? Its chat stays in the history.`))) return;
        await api("DELETE", `/api/routines/${r.id}`).catch(() => {});
      } else {
        await api("POST", `/api/routines/${r.id}/run`).catch(() => {});
        toast(L(`Routine „${r.name}“ startet …`, `Starting routine “${r.name}” …`));
      }
      loadRoutines();
    });
    ul.appendChild(li);
  }
}

function renderBriefing() {
  const s = B.settings;
  const order = [...s.sections, ...B.sections.map((x) => x.id).filter((id) => !s.sections.includes(id))];
  const list = $("brief-list");
  list.innerHTML = "";
  order.forEach((id, i) => {
    const info = B.sections.find((x) => x.id === id);
    const on = s.sections.includes(id);
    const li = document.createElement("li");
    li.className = "brief-item" + (on ? "" : " off");
    li.innerHTML = `<label><input type="checkbox" ${on ? "checked" : ""}> ${escapeHtml(info.label)}`
      + (info.note ? ` <span class="brief-note">– ${escapeHtml(info.note)}</span>` : "") + `</label>`
      + `<button class="ghost" data-move="-1" title="${L("nach oben", "move up")}" ${i === 0 ? "disabled" : ""}>▲</button>`
      + `<button class="ghost" data-move="1" title="${L("nach unten", "move down")}" ${i === order.length - 1 ? "disabled" : ""}>▼</button>`;
    li.querySelector("input").onchange = (e) => {
      const cur = order.filter((x) => x === id ? e.target.checked : s.sections.includes(x));
      saveBriefing({ sections: cur });
    };
    li.querySelectorAll("button").forEach((b) => b.onclick = () => {
      const j = i + Number(b.dataset.move);
      [order[i], order[j]] = [order[j], order[i]];
      saveBriefing({ sections: order.filter((x) => s.sections.includes(x)) });
    });
    list.appendChild(li);
  });
  $("brief-days").value = s.lookahead_days;
  $("brief-topics").value = s.news_topics.join(", ");
  $("brief-count").value = s.news_count;
  $("brief-inbox").value = s.inbox_tag;
  $("brief-instr").value = s.instructions;
}

async function loadBriefing() {
  try {
    const data = await getJSON("/api/briefing");
    B.sections = data.sections;
    B.settings = data.settings;
    $("brief-status").textContent = data.customized ? L("im Dashboard angepasst", "customised in the dashboard")
                                                   : L("aus der Config", "from the config file");
    renderBriefing();
  } catch { $("brief-status").textContent = L("Laden fehlgeschlagen", "Loading failed"); }
}

async function saveBriefing(patch) {
  B.settings = { ...B.settings, ...patch };
  renderBriefing();
  try {
    const r = await fetch("/api/briefing", { method: "PUT", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify(B.settings) });
    if (!r.ok) throw new Error(r.status);
    B.settings = (await r.json()).settings;
    $("brief-status").textContent = L("✔ gespeichert", "✔ saved");
  } catch { $("brief-status").textContent = L("Speichern fehlgeschlagen", "Saving failed"); }
}

function briefingFieldsChanged() {
  clearTimeout(B.timer);
  B.timer = setTimeout(() => saveBriefing({
    lookahead_days: Number($("brief-days").value) || 0,
    news_topics: $("brief-topics").value.split(",").map((t) => t.trim()).filter(Boolean),
    news_count: Number($("brief-count").value) || 3,
    inbox_tag: $("brief-inbox").value.trim(),
    instructions: $("brief-instr").value.trim(),
  }), 600);
}

// Nicht (mehr) angemeldet (Cookie gelöscht, Token neu): einmal neu laden – der Server zeigt die Anmeldeseite
export function signedOut() {
  if (!signedOut.done) { signedOut.done = true; location.reload(); }
}

export function setPill(id, cls, text) {
  const el = $(id);
  el.classList.remove("ok", "warn", "bad", "off");
  el.classList.add(cls);
  el.querySelector("em").textContent = text;
}

export async function loadStatus() {
  const asked = Date.now();
  try {
    const st = await getJSON("/api/status");
    S.status = st;
    greet();
    const l = st.llm;
    const name = l.label && l.label !== l.model ? `${l.label} · ${l.model}` : l.model;
    // Nur eine Antwort, die nach dem letzten „Modell aktiv“ angefragt wurde, darf „lädt“ setzen – sonst bliebe
    // „Lade Modell“ nach dem Start stehen, bis man neu lädt
    if (l.switching && asked > S.modelDoneAt) { S.modelSwitching = S.modelSwitching || l.switching; refresh(); }
    else if (!l.switching && S.modelSwitching && asked > S.modelDoneAt) { S.modelSwitching = null; refresh(); }
    if (IV.open) renderImagePill();  // Bild-Modus: die Pille zeigt das Bildmodell
    else setPill("pill-llm", l.switching ? "warn" : l.unloaded ? "off" : !l.online ? "bad" : l.model_available ? "ok" : "warn",
      l.switching ? L("lädt …", "loading …") : l.unloaded ? `${l.label || l.model} · ${L("entladen", "unloaded")}` : !l.online ? `${l.label || l.model} offline` : l.model_available ? name : l.model + L(" fehlt", " missing"));
    const v = st.voice;
    const vCls = v.stt && v.tts ? "ok" : v.stt || v.tts ? "warn" : "bad";
    setPill("pill-voice", vCls, [v.stt ? "STT" : null, v.tts ? "TTS" : "TTS(Browser)", v.wake ? "Wake-Word" : null].filter(Boolean).join(" · "));
    setPill("pill-mem", "ok", `${st.memory.days} ${L("Tage", "days")} · ${st.memory.facts} ${L("Fakten", "facts")}`);
    const tg = st.telegram || {};
    setPill("pill-tg", tg.running ? "ok" : tg.error ? "bad" : "warn",
      tg.running ? (tg.bot || L("aktiv", "active")) : tg.error || (tg.configured === false ? L("nicht eingerichtet", "not set up") : L("startet …", "starting …")));
    if (tg.error && tg.error !== S.telegramError) toast("Telegram: " + tg.error);  // jedes Problem einmal melden
    S.telegramError = tg.error || "";
    return st;
  } catch {
    setPill("pill-llm", "bad", "?");
    return null;
  }
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // ---------------------------------------------------------------- Planer: Routinen
  DAYS = L("Mo Di Mi Do Fr Sa So", "Mo Tu We Th Fr Sa Su").split(" ");
  R = { items: [], edit: null, days: new Set() };
  $("brief-box").addEventListener("toggle", () => { if ($("brief-box").open) loadBriefing(); });

  $("rt-new").onclick = () => openRoutineForm(null);
  $("rt-cancel").onclick = closeRoutineForm;
  $("rt-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = { name: $("rt-name").value.trim(), task: $("rt-task").value.trim(), time: $("rt-time").value,
                   days: [...R.days].sort(), date: $("rt-date").value };
    if (!body.task) { $("rt-error").textContent = L("Bitte eine Aufgabe eintragen.", "Please enter a task."); return; }
    const r = await fetch(R.edit ? `/api/routines/${R.edit}` : "/api/routines", {
      method: R.edit ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      $("rt-error").textContent = (await r.json().catch(() => ({}))).detail || L("Speichern fehlgeschlagen", "Saving failed");
      return;
    }
    closeRoutineForm();
    loadRoutines();
  };

  // ---------------------------------------------------------------- Briefing-Einstellungen
  B = { sections: [], settings: null, timer: null };
  ["brief-days", "brief-topics", "brief-count", "brief-inbox", "brief-instr"].forEach((id) => {
    $(id).addEventListener("input", briefingFieldsChanged);
  });
  $("brief-reset").onclick = async () => {
    await fetch("/api/briefing", { method: "DELETE" });
    await loadBriefing();
  };
  $("brief-preview").onclick = async () => {
    const out = $("brief-out");
    out.classList.remove("hidden");
    out.textContent = L("Briefing wird zusammengestellt …", "Putting the briefing together …");
    try {
      const r = await fetch("/api/briefing/preview", { method: "POST" });
      out.textContent = (await r.json()).text;
    } catch { out.textContent = L("Vorschau fehlgeschlagen", "Preview failed"); }
  };
  setInterval(() => { if (S.connected) loadStatus(); }, 20000);
}
