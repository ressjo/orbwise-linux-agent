/* Orbwise – Mikrofon: Aufnahme, Push-to-talk */

import { initMic, stopSpeech, updateMicStreaming } from "./audio.js";
import { $, L, S } from "./core.js";
import { refresh } from "./state.js";
import { send } from "./ws.js";

export let pttActive;
let pttStart, mic;

export async function startPtt() {
  if (pttActive || S.mode === "coding") return;  // Coding-Modus: keine Spracheingabe
  if (!(await initMic())) return;
  pttActive = true;
  pttStart = performance.now();
  stopSpeech(true);
  S.recording = true;
  S.recordingMode = "ptt";
  S.voiceUsed = true;
  updateMicStreaming();
  send({ type: "ptt_start" });
  refresh();
}

export function endPtt() {
  if (!pttActive) return;
  pttActive = false;
  if (performance.now() - pttStart < 280) {
    // Kurz angetippt → zuhören, bis Stille erkannt wird
    startListen();
  } else {
    send({ type: "ptt_stop" });
  }
}

export async function startListen() {
  if (!(await initMic())) return;
  S.recording = true;
  S.recordingMode = "vad";
  updateMicStreaming();
  send({ type: "listen" });
  if (S.confirm) {
    $("confirm-voice").textContent = L("Ich höre … sag „Ja“ oder „Nein“", "Listening … say “yes” or “no”");
    $("confirm-voice").classList.add("listening");
  }
  refresh();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  pttStart = 0;
  pttActive = false;

  mic = $("btn-mic");
  mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startPtt(); });
  mic.addEventListener("pointerup", endPtt);
  mic.addEventListener("pointerleave", () => { if (pttActive) endPtt(); });
}
