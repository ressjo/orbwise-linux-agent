/* Orbwise – WebSocket: Verbindung zum Server und alle Ereignisse von dort */

import { modelPhase, newQuestionGroup, showTodos, toolCall, toolOutput, toolResult } from "./activity.js";
import { applySettings, loadThinkCaps, sudoCached } from "./attach.js";
import { A, enqueueSpeech, stopSpeech, updateMicStreaming } from "./audio.js";
import { Thought, addCompactDivider, addError, addShots, addSystem, addUser, appendToken, assistants, chat, closePlan, cutChat, finishAssistant, goalEvent, showPlan, showTranscript, startAssistant } from "./chat.js";
import { closeConfirm, closeQuestion, jobEvent, openConfirm, openQuestion, setChangesUndone, showChanges } from "./coding.js";
import { $, L, S, api, getJSON, orb, store } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { loadChats } from "./history.js";
import { IV, imageDone, imageError, imageInstallEvent, imageProgress, openImageView } from "./images.js";
import { modelPullEvent } from "./model_add.js";
import { showApiStatus } from "./model_api.js";
import { MP, refreshModelViews } from "./models.js";
import { applyMode } from "./modes.js";
import { openSheetName } from "./nav.js";
import { closePassword, openPassword, prewarmSoon } from "./password.js";
import { loadRoutines, loadStatus, setPill, signedOut } from "./planner.js";
import { loadHistory, loadProjectInfo, openChatView, renderSandboxChip } from "./project.js";
import { loadReminders, showReminder } from "./reminders.js";
import { loadSkills } from "./skills.js";
import { Boot, renderBoot } from "./startup.js";
import { refresh } from "./state.js";
import { T, fmt, loadCtxMemory, pushSpark, renderTokenLine, setTile, showContext, showMetrics } from "./telemetry.js";
import { UPD, loadVersion, renderVersion, updateDone } from "./update.js";
import { openVoiceMenu, piperProgress, voiceMenu } from "./voice.js";

export let SAYINGS, Say;

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  S.ws = ws;
  ws.onopen = () => {
    S.connected = true;
    S.retry = 0;
    send({ type: "tts", enabled: S.tts });
    send({ type: "plan_mode", enabled: S.plan });  // Denken und Auto-Modus kommen vom Server (hello)
    if (S.wake && A.micReady) send({ type: "wake", enabled: true });
    refresh();
    loadStatus();
    getJSON("/api/metrics").then(showMetrics).catch(() => {});
    loadCtxMemory();
    if (!S.historyLoaded) loadHistory();
    loadChats();  // Seitenleiste gleich füllen (vorher erst nach NEU oder der ersten Antwort)
    renderBoot();
    loadVersion(true);  // Version unten in der Seitenleiste; der Server fragt höchstens alle 3 Stunden nach Updates
  };
  ws.onclose = (ev) => {
    if (ev.code === 4401) { signedOut(); return; }  // Zugangs-Token fehlt/abgelaufen → Anmeldeseite
    if (S.shutdown) { showShutdown(); return; }  // „Beenden“: nicht neu verbinden
    S.connected = false; S.prewarm = null;
    S.recording = false;
    S.transcribing = false;
    updateMicStreaming();
    refresh();
    // auf der Startseite zügig neu versuchen (der Server startet evtl. gerade), danach mit Abstand
    const delay = Boot.entered ? Math.min(10000, 800 * 2 ** S.retry++) : 1000;
    setTimeout(connect, delay);
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") return;
    handle(JSON.parse(e.data));
  };
}

// „Beenden“ unten in der Seitenleiste: Server fährt herunter (wie Strg+C), danach ein ruhiger Schlussbildschirm
function showShutdown() {
  S.connected = false;
  $("shutdown").classList.remove("hidden");
  $("shutdown-reload").onclick = () => location.reload();
}

export function send(obj) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
}
export function sayNext(ctx) {
  if (ctx && ctx !== Say.ctx) { Say.ctx = ctx; Say.text = ""; }
  const pool = SAYINGS[Say.ctx] || SAYINGS.think;
  const choices = pool.length > 1 ? pool.filter((t) => t !== Say.text) : pool;
  Say.text = choices[Math.floor(Math.random() * choices.length)];
}
export function saying() {
  if (!Say.text) sayNext();
  if (!Say.timer) Say.timer = setInterval(() => { sayNext(); refresh(); }, 3500);
  return Say.text;
}
export function stopSaying() {
  clearInterval(Say.timer);
  Say.timer = null;
  Say.ctx = "think";
  Say.text = "";
}

function handle(ev) {
  switch (ev.type) {
    case "startup":
      Boot.server = ev;
      renderBoot();
      return;
    case "hello":
      S.name = ev.name || "Orbwise";
      S.wakePhrase = ev.wake_phrase || S.wakePhrase;
      S.serverState = ev.busy ? "thinking" : "idle";  // tatsächlichen Zustand übernehmen (auch nach Neuverbinden)
      S.transcribing = false;  // neue Verbindung = neue Audio-Sitzung, eine alte Transkription meldet sich nie mehr
      if (ev.context) showContext(ev.context);
      applyMode(ev);
      // Bildmodus nur innerhalb desselben Serverlaufs wiederherstellen – nach einem Neustart von Orbwise: leerer Chat
      if (UPD.restarting && ev.boot && ev.boot !== store.get("boot", "")) { store.set("boot", ev.boot); location.reload(); return; }  // neue Version läuft
      if (ev.boot && ev.boot !== store.get("boot", "")) { store.set("boot", ev.boot); store.set("view", "chat"); }
      else if (store.get("view", "chat") === "image" && !IV.open) setTimeout(openImageView, 0);
      applySettings(ev, true);
      loadThinkCaps();
      loadChats();  // Chats des aktuellen Modus
      break;
    case "update_progress":
      UPD.running = true; UPD.line = ev.text || ""; renderVersion();
      return;
    case "update_done":
      updateDone(ev);
      return;
    case "settings_sync":  // ein anderes Fenster (oder dieses) hat Denken/Auto-Modus geändert
      applySettings(ev, false);
      return;
    case "context":
      showContext(ev);
      return;
    case "sandbox_state":  // z. B. der Code-Check hat die Sandbox eingeschaltet
      S.sandbox = ev.sandbox || null;
      renderSandboxChip();
      loadProjectInfo();
      return;
    case "usage":  // Token dieses Chats (Sitzung) – nach jedem Modellschritt
      if (!ev.routine) { S.usage = ev; renderTokenLine(); }
      if (Date.now() - (T.memAt || 0) > 5000) { T.memAt = Date.now(); loadCtxMemory(); }  // Prompt-Cache ändert sich
      return;
    case "shutdown":
      S.shutdown = true;
      break;
    case "state":
      if (ev.routine) orb.addSatellite("rt:" + ev.routine, "⟳ " + ev.routine.toUpperCase());
      if (ev.state === "idle") {  // abgebrochene Werkzeuge nicht hängen lassen
        orb.clearSatellites("rt:");
      }
      S.serverState = ev.state === "confirm" ? S.serverState : ev.state;
      S.substate = "";
      if (ev.state === "idle") stopSaying();
      break;
    case "user":
      if (!ev.routine) newQuestionGroup(ev.text || L("Frage", "Question"));
      S.turns = (S.turns || 0) + 1;
      addUser(ev.text, ev.source, ev.images, S.turns, true, ev.files);
      if (ev.source === "voice") S.voiceUsed = true;
      break;
    case "chat_reload":  // Nachricht bearbeitet / neu generiert: Chat ab dieser Frage abschneiden (gleich folgt sie neu)
      cutChat(ev.turn);
      break;
    case "assistant_start":
      startAssistant(ev.id, ev.plan);
      if (ev.plan) S.substate = L("plant …", "planning …");
      break;
    case "plan":
      showPlan(ev);
      break;
    case "plan_closed":
      closePlan(ev.id, ev.outcome);
      break;
    case "plan_mode":  // Server schaltet den Planmodus aus (z. B. nach „Ausführen“)
      S.plan = !!ev.enabled;
      store.set("plan", S.plan);
      if (!S.plan) toast(L("Plan angenommen – Planmodus ist jetzt aus.", "Plan accepted – plan mode is now off."));
      break;
    case "token":
      appendToken(ev.id, ev.text);
      orb.token();
      T.tokenTimes.push(performance.now());
      T.streamed = (T.streamed || 0) + 1;
      Thought.collapse(ev.id);  // das Modell gibt aus → Gedankengang zuklappen
      return;  // Zustand ändert sich pro Token nicht – kein refresh() nötig
    case "reasoning":
      // Denkkette: als aufgeklappter Block in der Antwort, zu beim ersten ausgegebenen Token
      Thought.add(ev.id, ev.text || "");
      T.tokenTimes.push(performance.now());  // Token/s und Kontext laufen auch beim Denken mit
      T.streamed = (T.streamed || 0) + 1;
      orb.token();  // Denk-Puls folgt auch dem Gedankengang
      return;
    case "segment_end":
      appendToken(ev.id, "\n\n");
      break;
    case "assistant_end":
      Thought.finish(ev.id);
      if (S.mode === "coding") loadProjectInfo();  // der Agent kann den Branch gewechselt haben
      finishAssistant(ev.id, ev.cancelled);
      S.serverState = "idle";  // Antwort fertig = bereit, auch wenn das „idle“ des Servers noch aussteht
      stopSaying();
      setTimeout(loadStatus, 300);
      if (!$("chat-title").textContent) {
        getJSON("/api/chats").then((l) => {
          const c = l.find((x) => x.active);
          if (c && c.messages) $("chat-title").textContent = c.title;
        }).catch(() => {});
      }
      if (!$("tab-chats").classList.contains("hidden")) loadChats();
      if (!$("tab-memory").classList.contains("hidden")) loadReminders();
      break;
    case "llm_phase":
      modelPhase(ev);
      break;
    case "todos":
      showTodos(ev.items || []);
      break;
    case "tool_call":
      if (S.currentMsg) Thought.collapse(S.currentMsg);  // Werkzeugaufruf = Ausgabe → Gedankengang zuklappen
      toolCall(ev);
      break;
    case "tool_output":
      toolOutput(ev);
      break;
    case "tool_images":
      if (S.currentMsg && assistants[S.currentMsg]) addShots(assistants[S.currentMsg].el, ev.images || []);
      break;
    case "tool_result":
      toolResult(ev);
      sayNext("think");
      break;
    case "confirm_request":
      openConfirm(ev);
      break;
    case "confirm_done":
      closeConfirm(ev.id);
      break;
    case "sandbox_progress":
      $("sandbox-label").textContent = L("Sandbox …", "Sandbox …");
      toast(ev.text);
      break;
    case "question_request":
      openQuestion(ev);
      break;
    case "question_done":
      closeQuestion(ev.id);
      break;
    case "changes":
      if (assistants[ev.id]) showChanges(assistants[ev.id].el, ev.turn, ev.files, false);
      break;
    case "reverted":
      chat.querySelectorAll(`.msg-changes[data-turn="${ev.turn}"]`).forEach((c) => setChangesUndone(c, ev.undone));
      break;
    case "job":
      jobEvent(ev);
      break;
    case "sudo_cached":
      sudoCached(ev);
      break;
    case "password_request":
      openPassword(ev);
      break;
    case "image_progress":
      if (IV.open) imageProgress(ev);
      break;
    case "image_done":
      if (IV.open) imageDone(ev);
      else toast(L("Bild fertig – im Bild-Modus ansehen.", "Image ready – see image mode."));
      break;
    case "image_error":
      if (IV.open) imageError(ev);
      break;
    case "image_install":
      imageInstallEvent(ev);
      break;
    case "piper_progress":
      piperProgress(ev);
      break;
    case "image_llm_unloaded":
      if (IV.open) toast(L("Sprachmodell entladen – der Grafikspeicher gehört jetzt dem Bildmodell.",
                           "Language model unloaded – the video memory now belongs to the image model."));
      break;
    case "password_done":
      closePassword(ev.id);
      break;
    case "speak":
      enqueueSpeech(ev);
      break;
    case "voice_changed":  // Stimme/Sprache geändert (auch aus einem anderen Fenster)
      if (voiceMenu.offsetParent) openVoiceMenu();
      loadStatus();
      break;
    case "audio_stop":
      stopSpeech(false);
      break;
    case "voice":
      voiceEvent(ev);
      break;
    case "transcript":
      showTranscript(ev.text);
      break;
    case "goal":
      goalEvent(ev);
      break;
    case "toast":
      toast(ev.text);
      break;
    case "error":
      addError(ev.text);
      S.errorUntil = Date.now() + 2500;
      setTimeout(refresh, 2600);
      break;
    case "memory":
      addSystem(ev.text);
      break;
    case "compacted":
      if (!ev.chat || ev.chat === S.chatId) addCompactDivider(ev);
      break;
    case "compact_skipped":
      toast(L("Noch nichts zum Zusammenfassen – der Chat ist kurz genug.", "Nothing to summarise yet – the chat is short enough."));
      break;
    case "reminder":
      showReminder(ev);
      break;
    case "model_switching":
      S.modelSwitching = ev.label || ev.name;
      S.substate = L("Wechsle zu ", "Switching to ") + `${ev.label || ev.name} …`;
      setPill("pill-llm", "warn", L("lädt …", "loading …"));
      refreshModelViews();
      break;
    case "model_progress":
      S.substate = ev.text;
      break;
    case "model_active":
      if (S.modelSwitching && Boot.entered && !Boot.startupLoad) addSystem(L("Modell aktiv: ", "Model active: ") + S.modelSwitching);
      S.modelSwitching = null;
      S.modelDoneAt = Date.now();
      S.substate = "";
      loadStatus();
      refreshModelViews();
      loadThinkCaps();
      setTimeout(loadCtxMemory, 1500);
      break;
    case "model_unloaded":
      S.substate = "";
      toast(L("Modell entladen – der Grafikspeicher ist frei. Die nächste Frage lädt es wieder.",
              "Model unloaded – video memory is free. The next question loads it again."));
      loadStatus();
      refreshModelViews();
      return;
    case "skills_changed":
      if (!$("set-skills").classList.contains("hidden")) loadSkills();
      return;
    case "models_changed":
      refreshModelViews();
      loadStatus();
      loadThinkCaps();
      return;
    case "model_pull":
      modelPullEvent(ev);
      return;
    case "api_activity":
      if (openSheetName === "models" && MP.tab === "api") getJSON("/api/apiserver").then(showApiStatus).catch(() => {});
      return;
    case "model_error":
      S.modelSwitching = null;
      S.modelDoneAt = Date.now();
      S.substate = "";
      addError(L("Modellwechsel fehlgeschlagen: ", "Model switch failed: ") + ev.text);
      loadStatus();
      refreshModelViews();
      break;
    case "metrics":
      showMetrics(ev);
      break;
    case "llm_stats":
      T.exactTps = ev.tps;
      T.lastExact = Date.now();
      T.tokenTimes = [];
      setTile("tps", ev.tps, { sub: L(`Einlesen ${fmt(ev.prompt_tps, 0)}/s`, `prompt ${fmt(ev.prompt_tps, 0)}/s`),
        title: L(`${ev.tokens || "?"} Token geschrieben · Einlesen ${fmt(ev.prompt_tps, 0)} Token/s`,
                 `${ev.tokens || "?"} tokens written · prompt ${fmt(ev.prompt_tps, 0)} tokens/s`) });
      pushSpark("tps", ev.tps);
      break;
    case "conversation_reset":
      break;  // Anzeige erledigt chat_switched
    case "chat_switched":
      applyMode(ev);
      openChatView(ev);
      break;
    case "chats_changed":
      if (!$("tab-chats").classList.contains("hidden")) loadChats();
      return;
    case "routines_changed":
      if (!$("tab-planner").classList.contains("hidden")) loadRoutines();
      return;
    case "routine_done":
      orb.removeSatellite("rt:" + ev.name, ev.status === "error");
      toast(ev.status === "error" ? L(`Routine „${ev.name}“ fehlgeschlagen`, `Routine “${ev.name}” failed`)
        : L(`Routine „${ev.name}“ erledigt – im VERLAUF`, `Routine “${ev.name}” done – see HISTORY`));
      if (!$("tab-planner").classList.contains("hidden")) loadRoutines();
      return;
  }
  refresh();
}

function voiceEvent(ev) {
  switch (ev.state) {
    case "wake":
      stopSpeech(true);
      showTranscript("…");
      break;
    case "recording":
      S.recording = true;
      S.transcribing = false;
      prewarmSoon();  // während du sprichst, liest das Modell den Chat schon ein
      break;
    case "transcribing":
      S.recording = false;
      S.transcribing = true;
      break;
    case "timeout":
    case "idle":
      S.recording = false;
      S.transcribing = false;
      if (ev.state === "timeout") showTranscript("");
      break;
    case "error":
      S.recording = false;
      S.transcribing = false;
      toast(ev.text);
      break;
    case "wake_off":
      if (S.wake) { S.wake = false; store.set("wake", false); }
      break;
  }
  updateMicStreaming();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  $("btn-quit").onclick = async () => {
    if (!await ask(L("Orbwise beenden? Modell, Sandbox-Container und Hintergrund-Jobs werden angehalten. Wieder starten mit „orbwise serve --open“ in der Konsole.",
                     "Quit Orbwise? The model, sandbox containers and background jobs are stopped. Start again with \"orbwise serve --open\" in a terminal."),
                   { ok: L("Beenden", "Quit"), danger: true })) return;
    S.shutdown = true;
    try { await api("POST", "/api/shutdown"); } catch { S.shutdown = false; return; }
    setTimeout(showShutdown, 1500);  // falls die Verbindung nicht sauber schließt
  };

  // Unter dem Orb, solange Orbwise arbeitet: wechselnde Sprüche passend zur Tätigkeit (die technischen
  // Modell-Schritte stehen in der Antwort selbst)
  SAYINGS = {
    think: [L("Einen Moment, Sir …", "One moment, sir …"), L("Analysiere …", "Analysing …"),
            L("Ich gehe das kurz durch …", "Running through it …"), L("Verknüpfe die Fakten …", "Connecting the dots …"),
            L("Werte aus …", "Evaluating …"), L("Fast so weit …", "Almost there …")],
    shell: [L("Bemühe die Kommandozeile …", "Consulting the command line …"), L("Erteile Befehle …", "Issuing commands …"),
            L("Spreche mit dem System …", "Talking to the system …")],
    sysadmin: [L("Prüfe die Systemwerte …", "Checking the vitals …"), L("Sehe nach dem Rechten …", "Inspecting the system …"),
               L("Werfe einen Blick unter die Haube …", "Looking under the hood …")],
    packages: [L("Sichte die Pakete …", "Reviewing packages …"), L("Prüfe auf Neuigkeiten …", "Checking for updates …")],
    web: [L("Durchforste das Netz …", "Scouring the web …"), L("Befrage das Internet …", "Consulting the internet …"),
          L("Lese quer …", "Skimming the sources …")],
    files: [L("Durchsuche die Dateien …", "Searching the files …"), L("Blättere im Dateisystem …", "Leafing through the file system …"),
            L("Sortiere Akten …", "Sorting records …")],
    mail: [L("Sichte die Post …", "Going through the mail …"), L("Öffne die Umschläge …", "Opening envelopes …")],
    paperless: [L("Blättere in Ihren Unterlagen …", "Leafing through your documents …"), L("Ziehe die Akte …", "Pulling the file …")],
    calendar_tools: [L("Konsultiere den Kalender …", "Consulting the calendar …"), L("Prüfe Ihre Termine …", "Checking your schedule …")],
    homeassistant: [L("Spreche mit dem Haus …", "Talking to the house …"), L("Lege Schalter um …", "Flipping switches …")],
    portainer: [L("Schaue nach den Containern …", "Checking the containers …"), L("Rede mit Portainer …", "Talking to Portainer …")],
    ssh: [L("Arbeite auf dem anderen Rechner …", "Working on the other machine …"), L("Tippe aus der Ferne …", "Typing remotely …")],
    memory_tools: [L("Krame in meinem Gedächtnis …", "Searching my memory …"), L("Erinnere mich …", "Recalling …")],
    vision: [L("Sehe genau hin …", "Taking a close look …"), L("Betrachte das Bild …", "Studying the image …")],
    compress: [L("Ordne meine Gedanken …", "Gathering my thoughts …"), L("Fasse zusammen …", "Summarising …")],
  };
  Say = { ctx: "think", text: "", timer: null };
}
