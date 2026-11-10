/* Orbwise – Zustand: Orb, Statusanzeige, bereit/denke nach */

import { num } from "./activity.js";
import { renderThink } from "./attach.js";
import { $, AUTO_LABEL, L, LABELS, S, orb } from "./core.js";
import { Say, saying, stopSaying } from "./ws.js";

export function refresh() {
  let s = S.serverState;
  if (!S.connected) s = "offline";
  else if (S.confirm) s = "confirm";
  else if (S.recording) s = "listening";
  else if (S.transcribing) s = "thinking";
  else if (S.playing) s = "speaking";
  else if (Date.now() < S.errorUntil) s = "error";
  const loadingModel = S.modelSwitching || S.startupModel;
  if (loadingModel && S.connected) s = "thinking";
  // Vorwärmen (Chat einlesen) läuft im Hintergrund – sonst stünde „Bereit“, obwohl das Modell beschäftigt ist
  const warming = !loadingModel && s === "idle" && S.prewarm && Date.now() - S.prewarm.at < 600000;
  if (warming) s = "thinking";
  orb.setState(s);
  const label = $("state-label");
  label.textContent = loadingModel && S.connected ? L("Lade Modell", "Loading model")
    : warming ? L("Lese Chat ein", "Reading chat") : (LABELS[s] || s);
  label.style.color = { listening: "var(--success)", executing: "var(--warn)", confirm: "var(--warn)", error: "var(--danger)",
                        offline: "var(--muted)", thinking: "var(--reason)" }[s] || "";
  let sub = S.substate || (S.startupModel && !S.modelSwitching ? S.startupModel : "");
  if (S.recording) sub = S.recordingMode === "ptt" ? L("Loslassen zum Senden", "Release to send") : L("Sprich jetzt …", "Speak now …");
  else if (S.transcribing) sub = L("Transkribiere …", "Transcribing …");
  else if (s === "idle" && S.wake) sub = L(`Sag „${S.wakePhrase}“`, `Say “${S.wakePhrase}”`);
  if (warming && !sub) {
    sub = S.prewarm.tokens ? L(`${num(S.prewarm.tokens)} Token`, `${num(S.prewarm.tokens)} tokens`) : "";
    if (S.prewarm.eta) sub += (sub ? " · " : "") + L(`höchstens ~${Math.ceil(S.prewarm.eta)} s`, `at most ~${Math.ceil(S.prewarm.eta)} s`);
    }
  const working = (s === "thinking" || s === "executing") && !loadingModel && !warming;
  if (working && !sub) sub = saying();
  else if (!working && Say.timer) stopSaying();
  $("substate-label").textContent = sub || "";
  $("btn-mic").classList.toggle("recording", S.recording);
  $("btn-wake").classList.toggle("on", S.wake);
  $("btn-tts").classList.toggle("on", S.tts);
  renderThink();
  $("btn-auto").classList.toggle("on", S.autoMode !== "off");
  $("btn-auto").classList.toggle("files", S.autoMode === "files");
  $("btn-auto").classList.toggle("full", S.autoMode === "auto");
  $("auto-label").textContent = AUTO_LABEL[S.autoMode];
  // „Lesen + Bearbeiten“ lässt Bauen/Tests im Käfig (bubblewrap) laufen – fehlt er, fragen sie nach
  const noJail = S.jail && !S.jail.ok && !S.root && S.autoMode === "files";
  const hint = $("auto-jail-hint");
  if (hint) hint.classList.toggle("hidden", !noJail);
  if (hint && noJail) hint.textContent = L("Ohne Isolation fragen Bauen und Tests hier nach: ",
    "Without isolation, builds and tests ask here: ") + S.jail.reason;
  $("btn-auto").classList.toggle("root", !!S.root);
  $("btn-auto").title = S.root
    ? L("Orbwise läuft als root – darum gibt der Auto-Modus nichts ohne Rückfrage frei. Besser als normaler Benutzer starten.",
        "Orbwise runs as root – so Auto mode approves nothing without asking. Better run it as a normal user.")
    : L("Auto-Modus: was ohne Rückfrage laufen darf", "Auto mode: what may run without asking");
  document.querySelectorAll("[data-auto]").forEach((b) => b.classList.toggle("on", b.dataset.auto === S.autoMode));
  $("btn-plan").classList.toggle("on", S.plan);
  // Senden wird während einer Anfrage zum Stopp-Knopf
  const busy = S.connected && (["thinking", "executing", "confirm", "speaking"].includes(s) || S.playing);
  $("btn-send").classList.toggle("hidden", busy);
  $("btn-stop").classList.toggle("hidden", !busy);
  // Schalter in den Einstellungen spiegeln die Knöpfe in Kopf- und Eingabezeile
  document.querySelectorAll("[data-mirror]").forEach((sw) => {
    const on = $(sw.dataset.mirror).classList.contains("on");
    sw.classList.toggle("on", on);
    sw.textContent = on ? L("An", "On") : L("Aus", "Off");
  });
}
