/* Orbwise – Navigation: Sheets, Seitenleiste, Aktivität */

import { $, L, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { IMS } from "./images.js";
import { renderModelsPage } from "./models.js";
import { loadPlanner, loadStatus } from "./planner.js";
import { greet, loadMemory } from "./reminders.js";
import { loadSkills } from "./skills.js";
import { openVoiceMenu } from "./voice.js";

export let app, openSheetName;
let main, updateStage;

function isCompact() { return main.classList.contains("has-messages"); }
export function openSheet(name, section) {
  closeSheet();
  openSheetName = name;
  $("sheet-" + name).classList.remove("hidden");
  $("scrim").classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.sheet === name));
  app.classList.remove("side-open");
  if (name === "planner") loadPlanner();
  if (name === "memory") loadMemory();
  if (name === "settings") showSection(section || "general");
  if (name === "models") {
    IMS.q = ""; IMS.repo = "";  // Bildmodelle: beim Öffnen die Übersicht, nicht die letzte Suche
    const box = document.querySelector("#mp-image .ivm-search input");
    if (box) box.value = "";
    renderModelsPage();
  }
}
export function closeSheet() {
  if (!openSheetName) return false;
  $("sheet-" + openSheetName).classList.add("hidden");
  $("scrim").classList.add("hidden");
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
  openSheetName = null;
  return true;
}
export function showSection(section) {
  document.querySelectorAll(".set-tab").forEach((t) => t.classList.toggle("active", t.dataset.section === section));
  document.querySelectorAll(".set-section").forEach((el) => el.classList.toggle("hidden", el.id !== "set-" + section));
  if (section === "voice") openVoiceMenu();
  if (section === "status") loadStatus();
  if (section === "skills") loadSkills();
  if (section === "general") { renderHfToken(); renderAutostart(); renderSteps(); renderStorage(); }
}

// Beim Anmelden starten (Autostart-Eintrag in ~/.config/autostart) – standardmäßig aus
async function renderAutostart() {
  const sw = $("btn-autostart");
  const show = (on) => { sw.classList.toggle("on", on); sw.textContent = on ? L("An", "On") : L("Aus", "Off"); };
  let st;
  try { st = await getJSON("/api/autostart"); } catch { sw.disabled = true; return; }
  sw.disabled = false;
  show(st.enabled);
  sw.title = st.path;
  sw.onclick = async () => {
    sw.disabled = true;
    try {
      const r = await api("POST", "/api/autostart", { enabled: !sw.classList.contains("on") });
      show(r.enabled);
      toast(r.enabled ? L("Orbwise startet ab jetzt beim Anmelden.", "Orbwise now starts at login.")
                      : L("Kein Autostart mehr – Orbwise startet über das Anwendungsmenü.",
                          "No more autostart – start Orbwise from the application menu."));
    } catch { /* api meldet selbst */ }
    sw.disabled = false;
  };
}

// Schritt-Limit je Antwort (Tools-/Coding-Modus), 0 = unbegrenzt
const STEP_CHOICES = [10, 25, 50, 100, 200, 500, 0];
async function renderSteps() {
  let st;
  try { st = await getJSON("/api/steps"); } catch { return; }
  // Denken bei Aufgaben: vor jedem Schritt (all) oder nur zu Beginn und nach einem Fehlschlag (lean)
  const seg = $("set-think-steps");
  const mark = (value) => seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.steps === value));
  mark(st.think_steps);
  seg.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      if (b.classList.contains("on")) return;
      try {
        const r = await api("POST", "/api/steps", { think_steps: b.dataset.steps });
        mark(r.think_steps);
        toast(r.think_steps === "lean"
          ? L("Denkt nur noch zu Beginn und nach einem Fehlschlag.", "Now thinks only at the start and after a failed step.")
          : L("Denkt wieder vor jedem Schritt.", "Thinks before every step again."));
      } catch { renderSteps(); }
    };
  });
  for (const mode of ["tools", "coding"]) {
    const sel = $("steps-" + mode);
    const values = STEP_CHOICES.includes(st[mode]) ? STEP_CHOICES
      : [...STEP_CHOICES.slice(0, -1), st[mode]].sort((a, b) => a - b).concat(0);
    sel.replaceChildren(...values.map((v) => {
      const o = document.createElement("option");
      o.value = String(v);
      o.textContent = v ? String(v) : L("Unbegrenzt", "Unlimited");
      o.selected = v === st[mode];
      return o;
    }));
    sel.onchange = async () => {
      const n = Number(sel.value);
      try {
        await api("POST", "/api/steps", { [mode]: n });
        toast(n ? L(`Schritt-Limit (${mode === "coding" ? "Coding" : "Tools"}): ${n}`, `Step limit (${mode === "coding" ? "coding" : "tools"}): ${n}`)
                : L("Ohne Schritt-Limit – der Stopp-Knopf hält eine Antwort jederzeit an.",
                    "No step limit – the stop button halts an answer at any time."));
      } catch { renderSteps(); }
    };
  }
}

// Speicherplatz: was Orbwise belegt (Modelle, Programme, Zwischenspeicher …) – anzeigen, einzeln löschen bzw. leeren
function bytesText(n) {
  if (!n) return L("leer", "empty");
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + units[i];
}
function freedOf(r) {
  return r?.freed ?? Math.round((r?.freed_gb || 0) * 1e9);
}
const label = (o) => L(o.label, o.label_en || o.label);
const note = (o) => L(o.note || "", o.note_en || o.note || "");

async function renderStorage() {
  const total = $("storage-total"), btn = $("btn-storage-details"), list = $("storage-list");
  btn.onclick = () => {
    const open = list.classList.toggle("hidden") === false;
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? L("Schließen", "Close") : "Details";
  };
  let st;
  try { st = await getJSON("/api/storage"); } catch { total.textContent = "–"; return; }
  total.textContent = st.total ? bytesText(st.total) : bytesText(0);
  list.replaceChildren(...st.groups.map((g) => storageGroup(g, st.total)));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function storageGroup(g, total) {
  const box = el("div", "storage-group");
  const head = el("div", "storage-head");
  const name = el("b", "", label(g));
  if (g.path) name.title = g.path;
  head.append(name, el("span", "artifacts-size", bytesText(g.bytes)));
  if (g.clear) {
    const b = el("button", "btn small", L("Leeren", "Empty"));
    b.type = "button";
    b.disabled = !g.bytes;
    b.onclick = () => removeStorage(b, g.clear, L(
      `${label(g)} leeren (${bytesText(g.bytes)})?\n\n${note(g)}`,
      `Empty ${label(g).toLowerCase()} (${bytesText(g.bytes)})?\n\n${note(g)}`), L("Leeren", "Empty"));
    head.append(b);
  }
  const bar = el("div", "storage-bar");
  const fill = el("i");
  fill.style.width = (total ? Math.max(g.bytes ? 1 : 0, Math.round(100 * g.bytes / total)) : 0) + "%";
  bar.append(fill);
  box.append(head, bar);
  if (g.note) box.append(el("small", "storage-note", note(g)));
  if (!g.clear) {  // Gruppen mit „Leeren“ haben nur einen Eintrag (sich selbst)
    if (!g.items.length) box.append(el("small", "storage-note", L("Nichts gespeichert.", "Nothing stored.")));
    for (const it of g.items) box.append(storageItem(it));
  }
  return box;
}

function storageItem(it) {
  const row = el("div", "storage-item");
  const text = el("div");
  const name = el("span", "", label(it));
  if (it.path) name.title = it.path;
  text.append(name);
  if (it.active) text.append(el("em", "storage-tag", L("aktiv", "active")));
  if (it.note) text.append(el("small", "", note(it)));
  row.append(text, el("span", "artifacts-size", bytesText(it.bytes)));
  if (it.delete) {
    const b = el("button", "btn small", L("Löschen", "Delete"));
    b.type = "button";
    const active = it.active ? L("\n\nEs ist gerade aktiv – Orbwise wechselt dafür auf ein anderes.",
                                 "\n\nIt is active right now – Orbwise switches to another one.") : "";
    b.onclick = () => removeStorage(b, it.delete, L(
      `„${label(it)}“ löschen (${bytesText(it.bytes)})?${active}\n\nDie Dateien werden gelöscht. Modelle lassen sich später neu laden.`,
      `Delete “${label(it)}” (${bytesText(it.bytes)})?${active}\n\nThe files are deleted. Models can be downloaded again later.`),
      L("Löschen", "Delete"));
    row.append(b);
  } else if (it.open) {
    const b = el("button", "btn-link", L("Verwalten", "Manage"));
    b.type = "button";
    b.onclick = () => showSection(it.open);
    row.append(b);
  }
  return row;
}

async function removeStorage(btn, url, question, ok) {
  if (!await ask(question, { ok, danger: true })) return;
  btn.disabled = true;
  try {
    const r = await api("DELETE", url);
    const freed = freedOf(r);
    toast(freed ? L(`${bytesText(freed)} freigegeben.`, `${bytesText(freed)} freed.`)
                : L("Erledigt.", "Done."));
  } catch { /* api meldet selbst */ }
  renderStorage();
}

// Hugging-Face-Token (für zugangsbeschränkte Modelle wie Llama oder Gemma) – liegt im Schlüsselbund
async function renderHfToken() {
  const row = $("set-hf");
  let st = { set: false, source: "saved" };
  try { st = await getJSON("/api/hf/token"); } catch { /* offline: Formular trotzdem zeigen */ }
  row.innerHTML = `<div><b></b><small></small></div><form class="hf-token"><input type="password" autocomplete="off" spellcheck="false">
      <button type="submit" class="btn small"></button></form>`;
  row.querySelector("b").textContent = "Hugging Face";
  const small = row.querySelector("small");
  small.textContent = st.source === "env" ? L("Token kommt aus der Umgebung (HF_TOKEN).", "Token comes from the environment (HF_TOKEN).")
    : st.set ? L("Token gespeichert – zugangsbeschränkte Modelle lassen sich laden (Lizenz auf der Modellseite akzeptieren).",
                 "Token saved – gated models can be downloaded (accept the license on the model page).")
    : L("Token für zugangsbeschränkte Modelle.",
        "Token for gated models.");
  const form = row.querySelector("form");
  const input = form.querySelector("input");
  const btn = form.querySelector("button");
  form.classList.toggle("hidden", st.source === "env");
  input.placeholder = st.set ? "hf_••••••••" : "hf_…";
  btn.textContent = st.set ? L("Ersetzen", "Replace") : L("Speichern", "Save");
  if (st.set && st.source !== "env") {
    const del = document.createElement("button");
    del.type = "button"; del.className = "btn small"; del.textContent = L("Entfernen", "Remove");
    del.onclick = async () => { try { await api("POST", "/api/hf/token", { token: "" }); renderHfToken(); } catch {} };
    form.appendChild(del);
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/hf/token", { token: input.value.trim() });
      toast(r.user ? L(`Angemeldet als ${r.user}.`, `Signed in as ${r.user}.`) : L("Token gespeichert.", "Token saved."));
      renderHfToken();
    } catch { btn.disabled = false; }
  };
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  app = $("app");
  main = $("main");
  // Kompakter Orb, sobald das Gespräch Nachrichten hat (Begrüßung + großer Orb nur im leeren Chat)
  updateStage = () => {
    const has = !!$("chat").querySelector(".msg.user, .msg.assistant");
    if (has !== isCompact()) {
      main.classList.toggle("has-messages", has);
      if (!has) greet(true);  // zurück auf der Startseite (neuer Chat): ein neuer Spruch
    }
  };
  new MutationObserver(updateStage).observe($("chat"), { childList: true });

  openSheetName = null;
}
