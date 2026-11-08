/* Orbwise – Version und Update unten in der Seitenleiste */

import { $, L, LOCALE, S, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";

export let UPD;

export async function loadVersion(check, force) {
  try { UPD.info = await getJSON(`/api/update${check ? "?check=1" : ""}${force ? "&force=1" : ""}`); }
  catch { return; }
  UPD.running = !!UPD.info.running;
  renderVersion();
}
function versionLabel(i) {
  if (!i) return "Orbwise";
  const date = i.date ? new Date(i.date + "T12:00:00").toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "numeric" }) : "";
  return ["Orbwise" + (i.version ? " " + i.version : ""), date, i.commit].filter(Boolean).join(" · ");
}
export function renderVersion() {
  const btn = $("side-version");
  const i = UPD.info;
  const available = !!(i && i.behind);
  const text = btn.querySelector(".sv-text");
  btn.classList.toggle("has-update", available && !UPD.running);
  btn.querySelector(".sv-dot").classList.toggle("hidden", !(available || UPD.running));
  if (UPD.restarting) text.textContent = L("Orbwise startet neu …", "Orbwise is restarting …");
  else if (UPD.running) text.textContent = L("Aktualisiere … ", "Updating … ") + (UPD.line || "").replace(/^[✔•\s]+/, "").slice(0, 60);
  else if (available) text.textContent = L(`Update verfügbar (${i.behind}) · `, `Update available (${i.behind}) · `) + versionLabel(i).replace(/^Orbwise ?/, "");
  else text.textContent = versionLabel(i);
  btn.title = i ? `${versionLabel(i)}${i.subject ? "\n" + i.subject : ""}${i.branch ? "\nBranch " + i.branch : ""}` : "";
  $("btn-menu").classList.toggle("has-update", available);  // Handy: Punkt am Menü-Knopf
}
export function updateDone(ev) {
  UPD.running = false;
  if (!ev.ok) {
    renderVersion();
    ask(L("Update fehlgeschlagen:\n\n", "Update failed:\n\n") + (ev.text || ""), { ok: "OK" });
    loadVersion(true, true);
    return;
  }
  if (ev.restart) { UPD.restarting = true; renderVersion(); toast(L("Update geladen – Orbwise startet neu …", "Update installed – Orbwise is restarting …")); return; }
  toast(L("Orbwise ist auf dem neuesten Stand.", "Orbwise is up to date."));
  loadVersion(true, true);
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  UPD = { info: null, running: false, restarting: false, line: "" };
  $("side-version").onclick = async () => {
    if (UPD.running || UPD.restarting) { toast(L("Das Update läuft gerade …", "The update is running …")); return; }
    await loadVersion(true);
    const i = UPD.info;
    if (!i) { toast(L("Version nicht abrufbar.", "Could not read the version.")); return; }
    const head = versionLabel(i) + (i.subject ? `\n${i.subject}` : "");
    if (i.behind && i.can_update) {
      const list = i.changes.slice(0, 15).map((c) => "• " + c).join("\n") + (i.changes.length > 15 ? "\n…" : "");
      if (!await ask(L(`Update verfügbar – ${i.behind} ${i.behind === 1 ? "Änderung" : "Änderungen"}:\n\n${list}\n\nOrbwise lädt das Update, aktualisiert die Abhängigkeiten und startet neu (meist unter einer Minute). Chats, Gedächtnis und Einstellungen bleiben.`,
                       `Update available – ${i.behind} ${i.behind === 1 ? "change" : "changes"}:\n\n${list}\n\nOrbwise downloads the update, refreshes the dependencies and restarts (usually under a minute). Chats, memory and settings stay.`),
                     { ok: L("Jetzt aktualisieren", "Update now") })) return;
      try { await api("POST", "/api/update"); } catch { return; }
      UPD.running = true; UPD.line = ""; renderVersion();
      return;
    }
    if (i.behind) {
      await ask(L(`Update verfügbar (${i.behind}), aber nicht automatisch möglich: ${i.reason}.\n\nIm Terminal: orbwise update`,
                  `Update available (${i.behind}), but not possible automatically: ${i.reason}.\n\nIn a terminal: orbwise update`), { ok: "OK" });
      return;
    }
    const state = i.error ? L(`Prüfen fehlgeschlagen: ${i.error}`, `Check failed: ${i.error}`)
      : i.git ? L("Orbwise ist auf dem neuesten Stand.", "Orbwise is up to date.") : i.reason;
    if (await ask(`${head}\n\n${state}`, { ok: L("Erneut prüfen", "Check again") })) {
      await loadVersion(true, true);
      const n = UPD.info;
      toast(n && n.behind ? L(`Update verfügbar (${n.behind}) – unten auf die Version klicken.`, `Update available (${n.behind}) – click the version at the bottom.`)
        : n && n.error ? L(`Prüfen fehlgeschlagen: ${n.error}`, `Check failed: ${n.error}`) : L("Kein Update – alles aktuell.", "No update – all current."));
    }
  };
  setInterval(() => { if (S.connected && !UPD.running) loadVersion(true); }, 3 * 3600 * 1000);
}
