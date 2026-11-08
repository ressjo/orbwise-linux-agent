/* Orbwise – Navigation: Sheets, Seitenleiste, Aktivität */

import { $, L, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { IMS } from "./images.js";
import { renderModelsPage } from "./models.js";
import { loadPlanner, loadStatus } from "./planner.js";
import { loadMemory } from "./reminders.js";
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
  if (section === "general") { renderHfToken(); renderArtifacts(); }
}

// Artefakte: Temp-Ordner für Diagramme, Exporte & Co. – Platz anzeigen, leeren
function bytesText(n) {
  if (!n) return L("leer", "empty");
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + units[i];
}
async function renderArtifacts() {
  const size = $("artifacts-size"), btn = $("btn-artifacts-clear");
  let st;
  try { st = await getJSON("/api/artifacts"); } catch { size.textContent = "–"; return; }
  size.textContent = st.files ? `${bytesText(st.bytes)} · ${st.files} ${st.files === 1 ? L("Datei", "file") : L("Dateien", "files")}`
                              : bytesText(0);
  size.title = st.path;
  btn.disabled = !st.files;
  btn.onclick = async () => {
    if (!await ask(L(`Alle Artefakte löschen (${bytesText(st.bytes)})?\n\nDiagramme und Exporte im Ordner ${st.path} werden gelöscht. Bilder im Chat bleiben sichtbar.`,
                     `Delete all artifacts (${bytesText(st.bytes)})?\n\nCharts and exports in ${st.path} are deleted. Images in the chat stay visible.`),
                   { ok: L("Leeren", "Empty"), danger: true })) return;
    btn.disabled = true;
    try {
      const r = await api("DELETE", "/api/artifacts");
      toast(L(`${bytesText(r.freed)} freigegeben.`, `${bytesText(r.freed)} freed.`));
    } catch { /* api meldet selbst */ }
    renderArtifacts();
  };
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
    : L("Nur für zugangsbeschränkte Modelle (z. B. Llama, Gemma): Token mit Leserecht von huggingface.co/settings/tokens.",
        "Only for gated models (e.g. Llama, Gemma): a read token from huggingface.co/settings/tokens.");
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
    }
  };
  new MutationObserver(updateStage).observe($("chat"), { childList: true });

  openSheetName = null;
}
