/* Orbwise – Passwortdialog für sudo und Bedienelemente der Eingabe */

import { stopSpeech } from "./audio.js";
import { answerConfirm, answerQuestion } from "./coding.js";
import { $, L, S } from "./core.js";
import { toast } from "./dialogs.js";
import { endPtt, pttActive, startPtt } from "./mic.js";
import { app, closeSheet } from "./nav.js";
import { setDrawer } from "./skills.js";
import { send } from "./ws.js";

let pwId, lastPrewarm;

export function openPassword(ev) {
  pwId = ev.id;
  const login = ev.kind === "login";  // Anmeldung an einem anderen Rechner (SSH): Benutzer + Passwort
  $("pw-title").textContent = login ? L("Anmeldung benötigt", "Login required") : L("Root-Rechte benötigt", "Root privileges required");
  $("pw-prompt").textContent = ev.retry ? L("Falsches Passwort – bitte erneut eingeben", "Wrong password – please try again")
                                     : login ? ev.title : L("Root-Passwort (sudo)", "Root password (sudo)");
  $("pw-cmd").textContent = ev.command || "";
  $("pw-user").classList.toggle("hidden", !(login && ev.ask_user));
  $("pw-user").value = ev.user || "";
  const remember = login ? ev.remember_text : ev.remember;
  $("pw-remember-row").classList.toggle("hidden", !remember);
  $("pw-remember").checked = !login;  // Anmeldedaten nur auf ausdrücklichen Wunsch merken
  $("pw-remember-text").textContent = login ? (ev.remember_text || "")
    : L(`${ev.remember} Minuten merken (nur hier am Rechner)`, `Remember for ${ev.remember} minutes (only on this computer)`);
  $("pw-note").textContent = login
    ? L("Geht direkt an ssh – nie auf die Platte und nie an das Sprachmodell.", "Goes straight to ssh – never to disk and never to the language model.")
    : L("Geht direkt an sudo – nie auf die Platte und nie an das Sprachmodell.", "Goes straight to sudo – never to disk and never to the language model.");
  $("pw-input").value = "";
  $("pw-modal").classList.remove("hidden");
  setTimeout(() => (login && ev.ask_user && !ev.user ? $("pw-user") : $("pw-input")).focus(), 30);
  if (ev.retry) toast(L("Falsches Passwort – bitte erneut eingeben.", "Wrong password – please try again."));
}
export function closePassword(id) {
  if (id && id !== pwId) return;
  pwId = null;
  $("pw-input").value = "";
  $("pw-modal").classList.add("hidden");
}

// Leertaste = Push-to-talk – aber nie, während man tippt (Chat, Planer, Briefing …) oder ein Knopf den Fokus hat
function typingTarget(el) {
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(el.tagName));
}
export function prewarmSoon() {
  if (!S.connected || Date.now() - lastPrewarm < 20000) return;
  lastPrewarm = Date.now();
  send({ type: "prewarm" });
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  pwId = null;
  $("pw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!pwId) return;
    const password = $("pw-input").value;
    const user = $("pw-user").value.trim();
    $("pw-input").value = "";  // nicht im DOM stehen lassen
    send(password ? { type: "password", id: pwId, password, user, remember: $("pw-remember").checked }
                  : { type: "password_cancel", id: pwId });
    closePassword();
  });
  $("pw-cancel").onclick = () => {
    if (pwId) send({ type: "password_cancel", id: pwId });
    closePassword();
  };
  for (const id of ["pw-input", "pw-user"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); $("pw-cancel").click(); }
    });
  }

  document.addEventListener("keydown", (e) => {
    if (!$("boot").classList.contains("hidden")) return;
    if (pwId) return;  // Passwortfeld hat Vorrang (kein Push-to-talk mit Leertaste)
    if (S.question) {
      if (e.key === "Escape") { e.preventDefault(); answerQuestion(null); }
      return;
    }
    if (S.confirm) {
      // im bearbeitbaren Fenster: Enter schreibt (neue Zeile), Strg/Cmd+Enter sendet
      const inForm = $("confirm-form").contains(document.activeElement);
      if (e.key === "Enter" && inForm && !(e.ctrlKey || e.metaKey)) return;
      if (e.key === "Enter") { e.preventDefault(); answerConfirm(true); }
      if (e.key === "Escape") { e.preventDefault(); answerConfirm(false); }
      return;
    }
    if (e.key === "Escape") {
      if (!$("day-modal").classList.contains("hidden")) { $("day-modal").classList.add("hidden"); return; }
      if (closeSheet()) return;
      if (app.classList.contains("side-open")) { app.classList.remove("side-open"); $("scrim").classList.add("hidden"); return; }
      // schmales Fenster: die Aktivität liegt über dem Chat – Esc schließt sie wie die anderen Einblendungen
      if (app.classList.contains("drawer-open") && window.innerWidth < 900) { setDrawer(false, true); return; }
      send({ type: "stop" });
      stopSpeech(true);
      return;
    }
    if (e.code === "Space" && !e.repeat && !typingTarget(document.activeElement)) {
      e.preventDefault();
      startPtt();
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && pttActive) { e.preventDefault(); endPtt(); }
  });

  // ---------------------------------------------------------------- Bedienelemente
  // Sobald du tippst oder sprichst: der Server liest den Chat schon ein (falls er gerade etwas anderes im Cache
  // hat, z. B. nach Telegram oder einer Routine) – beim Absenden ist dann nur noch deine Frage neu
  lastPrewarm = 0;
  $("input").addEventListener("input", prewarmSoon);
}
