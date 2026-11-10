/* Orbwise – Modi: Tools (Assistent) und Coding */

import { A, stopSpeech } from "./audio.js";
import { renderGoal } from "./chat.js";
import { $, L, S, api } from "./core.js";
import { IV, closeImageView, openImageView } from "./images.js";
import { app, openSheetName } from "./nav.js";
import { confirmLeaveSandbox, loadProjectInfo, renderSandboxChip } from "./project.js";
import { greet } from "./reminders.js";
import { refresh } from "./state.js";
import { renderTokenLine } from "./telemetry.js";
import { send } from "./ws.js";

export let MODE_TEXT;

export function applyMode(ev) {
  if (!ev || !ev.mode) return;
  const was = S.mode;
  S.mode = ev.mode;
  S.chatId = ev.chat_id || ev.id || S.chatId;
  S.project = ev.project || "";
  S.sandbox = ev.sandbox || null;
  S.goal = ev.goal || null;
  renderGoal();
  app.classList.toggle("coding", S.mode === "coding");
  renderSandboxChip();
  markModeTabs();
  $("input").placeholder = MODE_TEXT[S.mode].placeholder;
  renderTokenLine();
  const chip = $("project-chip");
  chip.classList.toggle("unset", !S.project);
  $("project-label").textContent = S.project ? S.project.replace(/^\/home\/[^/]+/, "~") : L("Projektordner wählen", "Choose project folder");
  $("project-branch").classList.add("hidden");
  chip.title = L("Projektordner für diesen Coding-Chat – Shell und relative Pfade starten dort", "Project folder for this coding chat – shell and relative paths start there");
  loadProjectInfo();
  if (was !== S.mode) {
    if (S.mode === "coding") {  // keine Sprache: Vorlesen stoppen, Wake-Word ruhen lassen
      stopSpeech(true);
      if (S.wake) send({ type: "wake", enabled: false });
    } else if (was === "coding" && S.wake && A.micReady) send({ type: "wake", enabled: true });
    greet();
  }
  refresh();
}
export function markModeTabs() {
  const current = IV.open ? "image" : S.mode;
  document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === current);
    b.setAttribute("aria-selected", String(b.dataset.mode === current));
  });
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Getrennte Chat-Verläufe; Coding lädt nur Dateien, Shell, Web und Gedächtnis und hat keine Sprache.
  MODE_TEXT = {
    get tools() {
      return { placeholder: L(`Frag ${S.name} oder sag „${S.wakePhrase}“ …`, `Ask ${S.name} or say “${S.wakePhrase}” …`),
               hint: L("Leertaste: sprechen · Esc: abbrechen", "Space: talk · Esc: stop") };
    },
    coding: { placeholder: L("Beschreib die Aufgabe – Code, Fehler, Idee …", "Describe the task – code, bug, idea …"),
              hint: L("Esc: abbrechen", "Esc: stop") },
  };
  document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
    b.onclick = async () => {
      if (app.classList.contains("side-open")) {  // Handy: Seitenleiste nach der Wahl schließen, sonst verdeckt sie den Modus
        app.classList.remove("side-open");
        if (!openSheetName) $("scrim").classList.add("hidden");
      }
      if (b.dataset.mode === "image") { openImageView(); return; }
      if (IV.open) closeImageView();
      if (b.dataset.mode === S.mode || !await confirmLeaveSandbox()) return;
      api("POST", "/api/mode", { mode: b.dataset.mode })
        .catch(() => {});  // Fehler zeigt api() schon an
    };
  });
}
