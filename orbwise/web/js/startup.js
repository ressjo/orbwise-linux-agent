/* Orbwise – Start: was lädt gerade? */

import { A, initAudio, initMic, updateMicStreaming } from "./audio.js";
import { $, L, S, orb, store } from "./core.js";
import { refresh } from "./state.js";
import { connect, send } from "./ws.js";

export let Boot;
let BOOT_ICON;

function bootRows() {
  const rows = [{
    key: "conn", label: L("Verbindung", "Connection"), state: S.connected ? "ok" : "running",
    text: S.connected ? L("mit dem Orbwise-Server verbunden", "connected to the Orbwise server")
      : L("verbinde mit dem Orbwise-Server …", "connecting to the Orbwise server …"),
  }];
  if (Boot.server) rows.push(...Boot.server.steps);
  else rows.push({ key: "srv", label: L("Dienste", "Services"), state: "pending", text: L("warte auf den Server …", "waiting for the server …") });
  rows.push({
    key: "chat", label: "Chat", state: S.historyLoaded ? "ok" : S.connected ? "running" : "pending",
    text: S.historyLoaded ? ($("chat-title").textContent || L("neuer Chat", "new chat")) : L("lade den letzten Chat …", "loading the last chat …"),
  });
  return rows;
}
export function renderBoot() {
  const model = Boot.server && Boot.server.steps.find((x) => x.key === "model");
  const loading = model && model.state === "running";
  if (S.startupModel && !loading) { S.modelSwitching = null; S.modelDoneAt = Date.now(); }
  S.startupModel = loading ? model.text : null;  // auch nach dem Start im Orb anzeigen
  Boot.startupLoad = !!loading;
  if (Boot.entered) { refresh(); return; }
  const rows = bootRows();
  const ul = $("boot-steps");
  ul.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.className = "boot-step " + r.state;
    li.innerHTML = `<span class="bs-icon">${BOOT_ICON[r.state] || ""}</span><span class="bs-label"></span><span class="bs-text"></span>`;
    li.querySelector(".bs-label").textContent = r.label;
    li.querySelector(".bs-text").textContent = r.text || "";
    li.title = r.text || "";
    ul.appendChild(li);
  }
  const done = rows.filter((r) => !["running", "pending"].includes(r.state)).length;
  $("boot-bar").style.width = Math.round((done / rows.length) * 100) + "%";
  const ready = done === rows.length;
  const btn = $("boot-btn");
  btn.disabled = !S.connected;
  btn.classList.toggle("primary", ready);
  btn.textContent = ready ? L("Orbwise starten", "Start Orbwise")
    : L("Schon starten – der Rest lädt im Hintergrund", "Start now – the rest keeps loading");
  const problems = rows.filter((r) => r.state === "error").length;
  $("boot-sub").textContent = !S.connected ? L("Warte auf den Orbwise-Server …", "Waiting for the Orbwise server …")
    : ready ? (problems ? L("Bereit – mit Hinweisen (siehe oben)", "Ready – with notes (see above)") : L("Alles bereit", "All set"))
      : L("Orbwise startet …", "Orbwise is starting …");
  $("boot").classList.toggle("ready", ready);
  // Alles geladen → ohne Klick weiter (Ton/Mikrofon gibt der Browser dann beim ersten Klick/Tastendruck frei)
  if (ready && !Boot.autoTimer) Boot.autoTimer = setTimeout(() => { if (!Boot.entered) enter(false); }, 700);
}

async function enter(byClick) {
  if (Boot.entered) return;
  Boot.entered = true;
  await initAudio().catch(() => {});
  if (!byClick) {  // ohne Klick blockt der Browser Ton/Mikrofon bis zur ersten Interaktion – dann freigeben
    const unlock = () => { if (A.ctx && A.ctx.state === "suspended") A.ctx.resume().catch(() => {}); };
    document.addEventListener("pointerdown", unlock, { once: true });
    document.addEventListener("keydown", unlock, { once: true });
  }
  $("boot").classList.add("hidden");
  orb.boot();
  if (S.wake) {
    if (await initMic()) send({ type: "wake", enabled: true });
    else { S.wake = false; store.set("wake", false); }
    updateMicStreaming();
  }
  $("input").focus();
  refresh();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Der Server meldet seine Schritte live (Ereignis „startup“); Verbindung und Chat-Verlauf prüft die Oberfläche selbst.
  Boot = { server: null, entered: false };
  BOOT_ICON = { ok: "✓", warn: "!", error: "✕", off: "–", running: "", pending: "" };
  setInterval(() => { if (!Boot.entered) renderBoot(); }, 1000);
  $("boot-btn").onclick = () => enter(true);

  renderBoot();
  connect();
  refresh();
}
