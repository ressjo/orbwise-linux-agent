/* Orbwise – Web-Client: WebSocket, Chat, Tool-Aktivität, Bestätigungen, Mikrofon und Sprachausgabe. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const orb = new window.Orb($("orb"), $("orb-overlay"));
  // Sprache: der Server liefert index.html bereits mit lang="de" bzw. lang="en" aus
  const EN = document.documentElement.lang === "en";
  const L = (de, en) => (EN ? en : de);
  const LOCALE = EN ? "en-GB" : "de-DE";

  const LABELS = {
    offline: "OFFLINE", idle: L("BEREIT", "READY"), listening: L("HÖRE ZU", "LISTENING"),
    thinking: L("DENKE NACH", "THINKING"), speaking: L("SPRECHE", "SPEAKING"), executing: L("FÜHRE AUS", "EXECUTING"),
    confirm: L("WARTE AUF FREIGABE", "AWAITING APPROVAL"), error: L("FEHLER", "ERROR"),
  };
  const STATUS_TEXT = EN
    ? { running: "running", waiting: "waiting", ok: "done", denied: "denied", blocked: "blocked", error: "error",
        planned: "planned" }
    : { running: "läuft", waiting: "wartet", ok: "fertig", denied: "abgelehnt", blocked: "blockiert", error: "fehler",
        planned: "geplant" };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("orbwise." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("orbwise." + k, JSON.stringify(v)); } catch { /* egal */ } },
  };

  // Denkstufe: off | low | medium | high (früher gespeichert als true/false)
  function thinkLevel(v) { return v === true ? "medium" : ["low", "medium", "high"].includes(v) ? v : "off"; }
  const THINK_LABEL = { off: L("Denken", "Think"), low: L("Denken · kurz", "Think · brief"),
                        medium: L("Denken · normal", "Think · normal"), high: L("Denken · gründlich", "Think · thorough") };
  const AUTO_LABEL = { off: L("Auto aus", "Auto off"), read: L("Lesen", "Read"), files: L("Dateien", "Files"), auto: "Auto" };
  const S = {
    ws: null, connected: false, retry: 0,
    serverState: "idle", substate: "",
    tts: store.get("tts", true), wake: store.get("wake", false), think: thinkLevel(store.get("think", "off")), autoMode: store.get("autoMode", store.get("auto", true) === false ? "off" : "read"), plan: store.get("plan", false),
    voiceName: store.get("voice", ""), fxOn: store.get("fx", true), fxAmount: store.get("fxAmount", 0.6),
    recording: false, transcribing: false, streamMic: false,
    playing: false, confirm: null, confirmListenSent: false,
    historyLoaded: false, status: null, errorUntil: 0, modelDoneAt: 0, startupModel: null,
  };

  // ---------------------------------------------------------------- Audio
  const A = { ctx: null, outAnalyser: null, micAnalyser: null, micNode: null, micReady: false,
              queue: [], gen: 0, current: null, micLevel: 0, spec: null, synthLevel: 0 };

  async function initAudio() {
    if (A.ctx) return;
    A.ctx = new (window.AudioContext || window.webkitAudioContext)();
    A.outAnalyser = A.ctx.createAnalyser();
    A.outAnalyser.fftSize = 256;
    A.outAnalyser.connect(A.ctx.destination);
    A.spec = new Uint8Array(A.outAnalyser.frequencyBinCount);
    if (window.VoiceFX) {
      A.fx = new window.VoiceFX(A.ctx, A.outAnalyser);
      A.fx.set(S.fxOn, S.fxAmount);
    }
    // ohne vorherigen Klick bleibt der Kontext pausiert, bis der Nutzer etwas anklickt (nicht darauf warten)
    if (A.ctx.state === "suspended") A.ctx.resume().catch(() => {});
  }

  async function initMic() {
    if (A.micReady) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast(L("Mikrofon nicht verfügbar – Seite über http://localhost öffnen.", "Microphone unavailable – open the page via http://localhost."));
      return false;
    }
    try {
      await initAudio();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      await A.ctx.audioWorklet.addModule("/static/mic-worklet.js");
      const src = A.ctx.createMediaStreamSource(stream);
      A.micNode = new AudioWorkletNode(A.ctx, "pcm-downsampler");
      A.micNode.port.onmessage = (e) => {
        A.micLevel = e.data.level;
        if (S.streamMic && S.ws && S.ws.readyState === 1) S.ws.send(e.data.pcm);
      };
      src.connect(A.micNode);
      // Worklet muss an einen Ausgang hängen, damit er läuft – stumm geschaltet
      const mute = A.ctx.createGain();
      mute.gain.value = 0;
      A.micNode.connect(mute).connect(A.ctx.destination);
      A.micReady = true;
      return true;
    } catch (err) {
      toast(L("Kein Mikrofonzugriff: ", "No microphone access: ") + err.message);
      return false;
    }
  }

  function updateMicStreaming() {
    S.streamMic = A.micReady && (S.wake || S.recording);
  }

  function b64ToBuffer(b64) {
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }

  let germanVoice = null;
  function pickVoice() {
    if (!window.speechSynthesis) return;
    const voices = speechSynthesis.getVoices();
    germanVoice = voices.find((v) => /de[-_]DE/i.test(v.lang) && /male|mann|stefan|markus/i.test(v.name))
      || voices.find((v) => /^de/i.test(v.lang)) || null;
  }
  if (window.speechSynthesis) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }

  function enqueueSpeech(ev) {
    if (!S.tts) return;
    A.queue.push(ev);
    if (!S.playing) playNext();
  }

  async function playNext() {
    const gen = A.gen;
    const ev = A.queue.shift();
    if (!ev) { S.playing = false; A.current = null; refresh(); onSpeechDrained(); return; }
    S.playing = true;
    refresh();
    const done = () => { if (gen === A.gen) playNext(); };
    try {
      if (ev.audio && A.ctx) {
        const buf = await A.ctx.decodeAudioData(b64ToBuffer(ev.audio));
        if (gen !== A.gen) return;
        const src = A.ctx.createBufferSource();
        src.buffer = buf;
        src.playbackRate.value = fxRate();
        src.connect(A.fx ? A.fx.input : A.outAnalyser);
        src.onended = done;
        A.current = src;
        src.start();
      } else if (window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(ev.text);
        u.lang = EN ? "en-GB" : "de-DE";
        if (germanVoice) u.voice = germanVoice;
        u.rate = 1.05;
        u.pitch = S.fxOn ? 1 - 0.3 * S.fxAmount : 1;
        u.onend = done;
        u.onerror = done;
        u.onboundary = () => { A.synthLevel = 0.6 + Math.random() * 0.4; };
        speechSynthesis.speak(u);
      } else {
        done();
      }
    } catch (err) {
      console.warn("Audio-Fehler", err);
      done();
    }
  }

  function stopSpeech(notifyServer) {
    A.gen++;
    A.queue.length = 0;
    try { if (A.current) A.current.stop(); } catch { /* schon gestoppt */ }
    A.current = null;
    if (window.speechSynthesis) speechSynthesis.cancel();
    S.playing = false;
    if (notifyServer) send({ type: "speech_interrupt" });
    refresh();
  }

  function onSpeechDrained() {
    // Nach der gesprochenen Rückfrage automatisch auf „Ja/Nein“ hören
    // nicht bei bearbeitbaren Fenstern (Mail): dort wird per Klick gesendet
    if (S.confirm && !(S.confirm.editable || []).length && !S.confirmListenSent && A.micReady && (S.wake || S.voiceUsed)) {
      S.confirmListenSent = true;
      startListen();
    }
  }

  // Pegel → Orb
  function levelLoop() {
    let level = 0;
    let spec = null;
    if (S.recording) {
      level = Math.min(1, A.micLevel * 6);
    } else if (S.playing && A.current && A.outAnalyser) {
      A.outAnalyser.getByteFrequencyData(A.spec);
      spec = A.spec;
      let sum = 0;
      for (let i = 2; i < 60; i++) sum += A.spec[i];
      level = Math.min(1, sum / (58 * 160));
    } else if (S.playing) {
      A.synthLevel *= 0.93;
      level = 0.25 + A.synthLevel * 0.5;
    }
    orb.setLevel(level);
    orb.setSpectrum(spec);
    requestAnimationFrame(levelLoop);
  }
  requestAnimationFrame(levelLoop);

  // ---------------------------------------------------------------- Zustand
  function refresh() {
    let s = S.serverState;
    if (!S.connected) s = "offline";
    else if (S.confirm) s = "confirm";
    else if (S.recording) s = "listening";
    else if (S.transcribing) s = "thinking";
    else if (S.playing) s = "speaking";
    else if (Date.now() < S.errorUntil) s = "error";
    const loadingModel = S.modelSwitching || S.startupModel;
    if (loadingModel && S.connected) s = "thinking";
    orb.setState(s);
    const label = $("state-label");
    label.textContent = loadingModel && S.connected ? L("LADE MODELL", "LOADING MODEL") : (LABELS[s] || s.toUpperCase());
    label.style.color = { listening: "#3ddc97", executing: "#f5b14c", confirm: "#f5b14c", error: "#ff5f6d", offline: "#8a92a5", thinking: "#a9b3ff" }[s] || "";
    let sub = S.substate || (S.startupModel && !S.modelSwitching ? S.startupModel : "");
    if (S.recording) sub = S.recordingMode === "ptt" ? L("Loslassen zum Senden", "Release to send") : L("Sprich jetzt …", "Speak now …");
    else if (S.transcribing) sub = L("Transkribiere …", "Transcribing …");
    else if (s === "idle" && S.wake) sub = L("Sag „Hey Jarvis“", "Say “Hey Jarvis”");
    const working = (s === "thinking" || s === "executing") && !loadingModel;
    if (working && !sub) sub = saying();
    else if (!working && Say.timer) stopSaying();
    $("substate-label").textContent = sub || "";
    $("btn-mic").classList.toggle("recording", S.recording);
    $("btn-wake").classList.toggle("on", S.wake);
    $("btn-tts").classList.toggle("on", S.tts);
    $("btn-think").classList.toggle("on", S.think !== "off");
    $("think-label").textContent = THINK_LABEL[S.think];
    document.querySelectorAll("[data-think]").forEach((b) => b.classList.toggle("on", b.dataset.think === S.think));
    $("btn-auto").classList.toggle("on", S.autoMode !== "off");
    $("btn-auto").classList.toggle("files", S.autoMode === "files");
    $("btn-auto").classList.toggle("full", S.autoMode === "auto");
    $("auto-label").textContent = AUTO_LABEL[S.autoMode];
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

  // ---------------------------------------------------------------- WebSocket
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    S.ws = ws;
    ws.onopen = () => {
      S.connected = true;
      S.retry = 0;
      send({ type: "tts", enabled: S.tts });
      send({ type: "think", enabled: S.think !== "off", level: S.think });
      send({ type: "auto_mode", mode: S.autoMode });
      send({ type: "plan_mode", enabled: S.plan });
      sendVoiceSettings();
      if (S.wake && A.micReady) send({ type: "wake", enabled: true });
      refresh();
      loadStatus();
      getJSON("/api/metrics").then(showMetrics).catch(() => {});
      loadCtxMemory();
      if (!S.historyLoaded) loadHistory();
      loadChats();  // Seitenleiste gleich füllen (vorher erst nach NEU oder der ersten Antwort)
      renderBoot();
    };
    ws.onclose = () => {
      S.connected = false;
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

  function send(obj) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  }

  // Unter dem Orb, solange Jarvis arbeitet: wechselnde Sprüche passend zur Tätigkeit (die technischen
  // Modell-Schritte stehen in der Antwort selbst)
  const SAYINGS = {
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
  const Say = { ctx: "think", text: "", timer: null };
  function sayNext(ctx) {
    if (ctx && ctx !== Say.ctx) { Say.ctx = ctx; Say.text = ""; }
    const pool = SAYINGS[Say.ctx] || SAYINGS.think;
    const choices = pool.length > 1 ? pool.filter((t) => t !== Say.text) : pool;
    Say.text = choices[Math.floor(Math.random() * choices.length)];
  }
  function saying() {
    if (!Say.text) sayNext();
    if (!Say.timer) Say.timer = setInterval(() => { sayNext(); refresh(); }, 3500);
    return Say.text;
  }
  function stopSaying() {
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
        S.serverState = ev.busy ? "thinking" : "idle";  // tatsächlichen Zustand übernehmen (auch nach Neuverbinden)
        S.transcribing = false;  // neue Verbindung = neue Audio-Sitzung, eine alte Transkription meldet sich nie mehr
        if (ev.context) showContext(ev.context);
        applyMode(ev);
        loadChats();  // Chats des aktuellen Modus
        break;
      case "context":
        showContext(ev);
        return;
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
        addUser(ev.text, ev.source, ev.images);
        if (ev.source === "voice") S.voiceUsed = true;
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
        if (Thought.active) Thought.zoomOut();
        return;  // Zustand ändert sich pro Token nicht – kein refresh() nötig
      case "reasoning":
        // Denkkette: nicht in die Antwort, sondern in den Orb (hineinzoomen) und später aufklappbar im Chat
        Thought.add(ev.id, ev.text || "");
        orb.token();  // Denk-Puls folgt auch dem Gedankengang
        return;
      case "segment_end":
        appendToken(ev.id, "\n\n");
        break;
      case "assistant_end":
        Thought.finish(ev.id);
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
        toolCall(ev);  // im Denkmodus bleibt der Zoom – das Werkzeug erscheint als Chip im Gedankenkasten
        break;
      case "tool_output":
        toolOutput(ev);
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
      case "password_done":
        closePassword(ev.id);
        break;
      case "speak":
        enqueueSpeech(ev);
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
        setTimeout(loadCtxMemory, 1500);
        break;
      case "models_changed":
        refreshModelViews();
        loadStatus();
        return;
      case "model_pull":
        modelPullEvent(ev);
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
        setTile("tps", ev.tps, { sub: `${ev.tokens || "?"} Tok · Prompt ${fmt(ev.prompt_tps, 0)}/s` });
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

  // ---------------------------------------------------------------- Chat
  const chat = $("chat");
  const assistants = {};

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMarkdown(src) {
    const blocks = [];
    let text = src.replace(/```[\w-]*\n?([\s\S]*?)(```|$)/g, (_, code) => {
      blocks.push(`<div class="code"><button class="copy" type="button" title="${L("Kopieren", "Copy")}">⧉</button>`
        + `<pre>${escapeHtml(code.replace(/\n$/, ""))}</pre></div>`);
      return `\u0000${blocks.length - 1}\u0000`;
    });
    text = escapeHtml(text)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    const html = text.split(/\n{2,}/).map((para) => {
      if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
      // Zeilenweise: Überschriften (## …), Listen (- … / 1. …) und normaler Text, auch gemischt in einem Absatz
      const out = [];
      let list = null, text = [];
      const flushText = () => { if (text.length) out.push(`<p>${text.join("<br>")}</p>`); text = []; };
      // nummerierte Listen behalten ihre Nummer – auch wenn Leerzeilen sie in mehrere Absätze teilen („1. … 2. …“)
      const flushList = () => {
        if (list) out.push(`<${list.tag}${list.start > 1 ? ` start="${list.start}"` : ""}>${list.items.join("")}</${list.tag}>`);
        list = null;
      };
      for (const l of para.split("\n")) {
        const h = /^\s*#{1,4}\s+(.+)$/.exec(l);
        const li = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(l);
        if (h) { flushText(); flushList(); out.push(`<div class="md-h">${h[1]}</div>`); }
        else if (li) {
          const tag = /\d/.test(li[1]) ? "ol" : "ul";
          flushText();
          if (list && list.tag !== tag) flushList();
          list = list || { tag, items: [], start: tag === "ol" ? parseInt(li[1], 10) : 1 };
          list.items.push(`<li>${li[2]}</li>`);
        } else if (l.trim()) { flushList(); text.push(l); }
      }
      flushText(); flushList();
      return out.join("");
    }).join("");
    return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  function scrollChat() { chat.scrollTop = chat.scrollHeight; }

  // Kopieren-Knopf an Code-Blöcken (Ereignis-Delegation, auch für später gerenderte Nachrichten)
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".code .copy");
    if (!btn) return;
    const text = btn.parentElement.querySelector("pre").textContent;
    try { await navigator.clipboard.writeText(text); } catch {
      const r = document.createRange(); r.selectNodeContents(btn.parentElement.querySelector("pre"));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); document.execCommand("copy"); sel.removeAllRanges();
    }
    btn.textContent = "✔"; btn.classList.add("done");
    setTimeout(() => { btn.textContent = "⧉"; btn.classList.remove("done"); }, 1200);
  });

  // Ecken eines Panels kurz aufleuchten lassen (neue Nachricht / neue Aktivität)
  function flashPanel(el) {
    const panel = el && el.closest(".panel");
    if (!panel) return;
    panel.classList.remove("flash");
    void panel.offsetWidth;  // Animation neu starten
    panel.classList.add("flash");
  }

  function addMsg(cls, who, html) {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.innerHTML = `<div class="who">${who}</div><div class="tools"></div><div class="body">${html}</div>`;
    chat.appendChild(el);
    flashPanel(chat);
    scrollChat();
    return el;
  }

  function addUser(text, source, images) {
    const el = addMsg("user", source === "voice" ? L("DU · SPRACHE", "YOU · VOICE") : L("DU", "YOU"), escapeHtml(text));
    if (images && images.length) {
      const box = document.createElement("div");
      box.className = "msg-images";
      for (const name of images) {
        const img = document.createElement("img");
        img.src = `/api/attachments/${encodeURIComponent(name)}`;
        img.alt = "";
        img.loading = "lazy";
        img.onclick = () => window.open(img.src, "_blank");
        box.appendChild(img);
      }
      el.querySelector(".body").prepend(box);
    }
  }
  function addSystem(text) {
    const el = document.createElement("div");
    el.className = "msg system";
    el.textContent = text;
    chat.appendChild(el);
    scrollChat();
  }
  function addError(text) { addMsg("assistant error", "SYSTEM", escapeHtml(text)); }

  // Trenner, wo der Kontext zusammengefasst wurde – darüber bleibt alles sichtbar, das Modell arbeitet ab hier
  // mit der (aufklappbaren) Zusammenfassung weiter
  function compactDivider(summary, ts) {
    const el = document.createElement("details");
    el.className = "compact-divider";
    const when = ts ? " · " + new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "";
    el.innerHTML = `<summary><span>${L("Kontext zusammengefasst", "Context summarised")}${when}</span></summary>
      <div class="body"></div>`;
    el.querySelector(".body").innerHTML = renderMarkdown(summary || "");
    return el;
  }
  function addCompactDivider(ev) {
    const el = compactDivider(ev.summary, Date.now() / 1000);
    // Mitten in einer Antwort: vor die laufende Antwort setzen (sie geht danach weiter)
    const current = S.currentMsg && assistants[S.currentMsg] ? assistants[S.currentMsg].el : null;
    if (current && current.parentNode === chat) chat.insertBefore(el, current);
    else chat.appendChild(el);
    scrollChat();
  }

  function startAssistant(id, plan) {
    const el = addMsg("assistant streaming" + (plan ? " plan" : ""), "JARVIS", "");
    el.dataset.msg = id;
    // statt eines blinkenden Cursors: was das Modell gerade macht (Einlesen, Denken, Aufruf schreiben …)
    const live = liveLine(L("wartet auf das Modell", "waiting for the model"));
    el.appendChild(live);
    assistants[id] = { el, raw: "", live, steps: [], started: performance.now() };
    S.currentMsg = id;
  }
  // Token werden gesammelt und höchstens einmal pro Bild (~16–60 ms) als Markdown gerendert –
  // vorher wurde bei jedem Token die ganze Antwort neu aufgebaut (bei langen Antworten sehr teuer).
  const dirty = new Set();
  let renderQueued = false;
  function renderAssistant(a) {
    a.el.querySelector(".body").innerHTML = renderMarkdown(a.raw.replace(/\n{3,}/g, "\n\n"));
  }
  function flushTokens() {
    renderQueued = false;
    if (!dirty.size) return;
    const stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
    for (const a of dirty) renderAssistant(a);
    dirty.clear();
    if (stick) scrollChat();
  }
  function appendToken(id, text) {
    const a = assistants[id] || (startAssistant(id), assistants[id]);
    a.raw += text;
    dirty.add(a);
    if (!renderQueued) {
      renderQueued = true;
      // rAF, mit Timeout-Fallback falls der Tab im Hintergrund ist
      let done = false;
      const go = () => { if (!done) { done = true; flushTokens(); } };
      requestAnimationFrame(go);
      setTimeout(go, 100);
    }
  }
  // Denkmodus: Gedankengang im Orb anzeigen (Zoom per CSS), danach aufklappbar an der Antwort
  const Thought = {
    active: false, text: "", byMsg: {}, queued: false, since: 0, outTimer: null, warned: false,
    add(id, text) {
      if (!text) return;
      this.byMsg[id] = (this.byMsg[id] || "") + text;
      if (!this.active) {
        this.active = true;
        this.since = performance.now();
        this.text = "";
        clearTimeout(this.outTimer);
        $("thought-text").textContent = "";
        $("stage").classList.add("zoomed");
        if (!isCompact()) orb.setZoom(true);  // Satelliten kreisen dann um den Gedankenkasten
        else requestAnimationFrame(scrollChat);  // Chat rückt über die Gedanken-Karte
      }
      this.text += text;
      if (!this.queued) {
        this.queued = true;
        let done = false;
        const go = () => {
          if (done) return;
          done = true;
          this.queued = false;
          const el = $("thought-text");
          el.textContent = this.text.length > 6000 ? "…" + this.text.slice(-6000) : this.text;
          el.scrollTop = el.scrollHeight;
        };
        requestAnimationFrame(go);
        setTimeout(go, 120);
      }
    },
    zoomOut() {
      if (!this.active) return;
      this.active = false;
      // kurz stehen lassen, damit das Zoomen nicht flackert
      const wait = Math.max(0, 700 - (performance.now() - this.since));
      clearTimeout(this.outTimer);
      this.outTimer = setTimeout(() => { $("stage").classList.remove("zoomed"); orb.setZoom(false); }, wait);
    },
    finish(id) {
      this.zoomOut();
      const text = (this.byMsg[id] || "").trim();
      delete this.byMsg[id];
      const a = assistants[id];
      if (text && a) {
        const d = document.createElement("details");
        d.className = "thought-log";
        d.innerHTML = `<summary>${L("Gedankengang", "Thoughts")}</summary><pre></pre>`;
        d.querySelector("pre").textContent = text;
        a.el.insertBefore(d, a.el.querySelector(".body"));
      } else if (!text && S.think !== "off" && !this.warned) {
        this.warned = true;
        toast(L("Denkmodus an, aber das Modell hat keinen Gedankengang geliefert – bei Bonsai '--reasoning-budget 0' aus dem Startbefehl entfernen.",
              "Thinking mode is on, but the model returned no reasoning – for llama-server remove '--reasoning-budget 0' from the start command."));
      }
    },
  };

  function finishAssistant(id, cancelled) {
    const a = assistants[id];
    if (!a) return;
    if (dirty.has(a)) { dirty.delete(a); renderAssistant(a); scrollChat(); }
    a.el.classList.remove("streaming");
    a.live.remove();
    if (a.steps.length) a.el.appendChild(statsLine(a, cancelled));
    if (!a.raw.trim()) {
      a.el.querySelector(".body").innerHTML = cancelled ? "<em>(abgebrochen)</em>" : "";
      if (!cancelled && !a.el.querySelector(".tool-chip")) a.el.remove();
    }
    delete assistants[id];
  }

  // ---------------------------------------------------------------- Planmodus
  const PLAN_OUTCOME = {
    accepted: L("▶ wird ausgeführt", "▶ being carried out"), revised: L("✎ wird überarbeitet", "✎ being revised"),
    discarded: L("✕ verworfen", "✕ discarded"), replaced: L("ersetzt", "replaced"),
  };
  function showPlan(ev) {
    const el = chat.querySelector(`.msg[data-msg="${ev.id}"]`);
    if (!el || el.querySelector(".plan-bar")) return;
    const bar = document.createElement("div");
    bar.className = "plan-bar";
    bar.innerHTML = `<div class="plan-actions">
        <button class="btn plan-run">▶ ${L("AUSFÜHREN", "RUN")}</button>
        <button class="btn plan-edit">✎ ${L("ÄNDERN", "CHANGE")}</button>
        <button class="btn plan-drop">✕ ${L("VERWERFEN", "DISCARD")}</button></div>
      <form class="plan-revise hidden"><input type="text" maxlength="4000"
        placeholder="${L("Was soll anders sein? (Enter sendet)", "What should be different? (Enter sends)")}"></form>
      <div class="plan-status"></div>`;
    el.appendChild(bar);
    const input = bar.querySelector("input");
    bar.querySelector(".plan-run").onclick = () => send({ type: "plan_accept", id: ev.id });
    bar.querySelector(".plan-drop").onclick = () => send({ type: "plan_discard", id: ev.id });
    bar.querySelector(".plan-edit").onclick = () => {
      bar.querySelector(".plan-revise").classList.toggle("hidden");
      input.focus();
    };
    bar.querySelector(".plan-revise").onsubmit = (e) => {
      e.preventDefault();
      if (input.value.trim()) send({ type: "plan_revise", id: ev.id, text: input.value.trim() });
    };
    scrollChat();
  }
  function closePlan(id, outcome) {
    const bar = chat.querySelector(`.msg[data-msg="${id}"] .plan-bar`);
    if (!bar) return;
    bar.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
    bar.querySelector(".plan-revise").classList.add("hidden");
    bar.querySelector(".plan-status").textContent = PLAN_OUTCOME[outcome] || outcome;
    bar.classList.add("closed", outcome);
  }

  function showTranscript(text) {
    const el = $("live-transcript");
    el.textContent = text ? L(`„${text}“`, `“${text}”`) : "";
    el.style.opacity = 1;
    clearTimeout(showTranscript.t);
    showTranscript.t = setTimeout(() => { el.style.opacity = 0; }, 5000);
  }

  // ---------------------------------------------------------------- Aktivität
  const activity = $("activity");
  const acts = {};

  function fmtArgs(name, args) {
    if (name === "run_shell") return args.command || "";
    const entries = Object.entries(args || {});
    return entries.map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
  }

  // ---------------------------------------------------------------- Modell-Schritte in der Aktivität
  // Zwischen den Werkzeugen arbeitet das Modell unsichtbar (Prompt einlesen, denken, Aufruf schreiben …) –
  // je Schritt eine Zeile mit Phase und mitlaufender Zeit, danach eine Zusammenfassung, wohin die Zeit ging.
  const num = (n) => Number(n || 0).toLocaleString(LOCALE);
  const secs = (s) => `${Number(s).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} s`;
  const kChars = (n) => (n >= 1000 ? `${(n / 1000).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} k` : String(n || 0));
  let phaseTimer = null;

  function phaseText(ev) {
    switch (ev.phase) {
      case "prompt": {
        const fresh = ev.new != null && ev.new < ev.tokens ? ev.new : ev.tokens;
        let t = L(`liest ${num(fresh)} Token ein`, `reading ${num(fresh)} tokens`);
        if (fresh < ev.tokens) t += L(` (von ${num(ev.tokens)}, Rest im Cache)`, ` (of ${num(ev.tokens)}, rest cached)`);
        if (ev.progress != null) t += ` · ${Math.round(ev.progress * 100)} %`;
        else if (ev.eta_s >= 1) t += L(` · ≈ ${secs(ev.eta_s)}`, ` · ≈ ${secs(ev.eta_s)}`);
        return t;
      }
      case "loading": return L("lädt das Modell neu (nach der Bildanalyse)", "reloading the model (after image analysis)");
      case "thinking": return L(`denkt nach · ${num(ev.tokens)} Token`, `thinking · ${num(ev.tokens)} tokens`);
      case "tool_args": return L(`schreibt Aufruf ${ev.name} · ${kChars(ev.chars)} Zeichen`,
                                 `writing call ${ev.name} · ${kChars(ev.chars)} chars`);
      case "writing": return L("antwortet", "answering");
      case "retry": return L("Kontext zu voll – kürzt den Verlauf und versucht es erneut",
                             "context too full – trimming the history and retrying");
      case "compress":
        if (ev.written != null) return L(`fasst den Chat zusammen · ${num(ev.written)} Token geschrieben`,
                                         `summarising the chat · ${num(ev.written)} tokens written`);
        if (ev.reason === "manual") return L("fasst den Chat zusammen (auf Wunsch)", "summarising the chat (on request)");
        if (ev.reason === "idle") return L(`Kontext ${ev.percent} % – fasst in Ruhe zusammen, damit die nächste Frage nicht wartet`,
                                           `context ${ev.percent} % – summarising now so the next question doesn't wait`);
        return L(`Kontext ${ev.percent} % voll – fasst den Chat zusammen, dann geht es weiter`,
                 `context ${ev.percent} % full – summarising the chat, then carrying on`);
      case "think_cut": return L(`genug überlegt (${num(ev.tokens)} Token) – handelt jetzt`,
                                 `enough thinking (${num(ev.tokens)} tokens) – acting now`);
      case "tools_added": return toolsChanged(ev.groups, ev.dropped) + L(" – einmal neu einlesen", " – one-off re-read");
      case "clear": return L(`blendet ${oldResults(ev.results)} aus – spart das Zusammenfassen`,
                             `hiding ${oldResults(ev.results)} – no summary needed`);
      case "prewarm": return L(`liest den Chat im Hintergrund vor · ${kTok(ev.tokens)} Token`,
                               `pre-reading the chat in the background · ${kTok(ev.tokens)} tokens`)
        + (ev.eta_s >= 2 ? L(` · höchstens ≈ ${secs(ev.eta_s)} (bereits Bekanntes kommt aus dem Cache)`,
                             ` · at most ≈ ${secs(ev.eta_s)} (anything already known comes from the cache)`) : "");
      default: return "";
    }
  }

  const GROUP_NAMES = {
    paperless: "Paperless", mail: "Mail", homeassistant: "Smart Home", calendar_tools: L("Kalender", "Calendar"),
    sysadmin: "System", packages: L("Pakete", "Packages"), routine_tools: L("Routinen", "Routines"),
    trilium: "Trilium", obsidian: "Obsidian", vision: L("Bildschirm", "Screen"), telegram_tools: "Telegram",
    weather: L("Wetter", "Weather"), power: L("Ausschalten", "Power"), apps: L("Programme", "Apps"),
    briefing: L("Tagesüberblick", "Briefing"), reminder_tools: L("Erinnerungen", "Reminders"),
    files: L("Dateien", "Files"), web: "Web", system: L("Systeminfo", "System info"),
    todo_tools: L("Aufgabenliste", "Task list"), memory_tools: L("Gedächtnis", "Memory"),
    portainer: "Docker", ssh: "SSH", image_tools: L("Bilder", "Images"),
  };
  const oldResults = (n) => n === 1 ? L("1 altes Werkzeug-Ergebnis", "1 old tool result")
    : L(`${num(n)} alte Werkzeug-Ergebnisse`, `${num(n)} old tool results`);
  // „paperless:write“ = der ändernde Teil einer Gruppe (kleines Kontextfenster)
  const groupName = (g) => g.endsWith(":write") ? `${GROUP_NAMES[g.slice(0, -6)] || g.slice(0, -6)} ${L("(ändern)", "(changes)")}`
    : GROUP_NAMES[g] || g;
  const groupNames = (groups) => (groups || []).map(groupName).join(", ");
  const toolsChanged = (added, dropped) => (added && added.length ? L("Werkzeuge dazugeladen: ", "tools added: ") + groupNames(added) : "")
    + (dropped && dropped.length ? (added && added.length ? " · " : "") + L("weggelassen: ", "dropped: ") + groupNames(dropped) : "");

  function phaseSummary(ev) {
    if (ev.compress) return L(`Chat zusammengefasst · ${num(ev.before)} → ${num(ev.after)} Token · Zusammenfassung ${num(ev.tokens)} Token`,
                              `chat summarised · ${num(ev.before)} → ${num(ev.after)} tokens · summary ${num(ev.tokens)} tokens`);
    if (ev.prewarm) return ev.error ? L("Vorlesen übersprungen", "pre-read skipped")
      : ev.new != null && ev.new < ev.tokens
        ? L(`Chat vorgelesen · ${num(ev.tokens)} Token, davon ${num(ev.tokens - ev.new)} aus dem Cache · ${num(ev.new)} neu in ${secs(ev.seconds)}`,
            `chat pre-read · ${num(ev.tokens)} tokens, ${num(ev.tokens - ev.new)} from the cache · ${num(ev.new)} new in ${secs(ev.seconds)}`)
        : L(`Chat vorgelesen · ${num(ev.tokens)} Token in ${secs(ev.seconds)} – die nächste Frage liest nur noch Neues`,
            `chat pre-read · ${num(ev.tokens)} tokens in ${secs(ev.seconds)} – the next question only reads what is new`);
    if (ev.cleared) return L(`Platz geschaffen · ${oldResults(ev.cleared)} ausgeblendet · ${num(ev.before)} → ${num(ev.after)} Token – ohne Zusammenfassen`,
                             `made room · ${oldResults(ev.cleared)} hidden · ${num(ev.before)} → ${num(ev.after)} tokens – no summary needed`);
    if ((ev.tools_added && ev.tools_added.length) || (ev.tools_dropped && ev.tools_dropped.length)) return toolsChanged(ev.tools_added, ev.tools_dropped);
    if (ev.error) return L("abgebrochen – Fehler beim Modell", "stopped – model error");
    const parts = [];
    if (ev.load_ms >= 1000) parts.push(L(`Modell geladen in ${secs(ev.load_ms / 1000)}`, `model loaded in ${secs(ev.load_ms / 1000)}`));
    if (ev.prompt_tokens) {
      let p = L(`${num(ev.prompt_tokens)} Token eingelesen`, `${num(ev.prompt_tokens)} tokens read`);
      if (ev.prompt_ms) p += L(` in ${secs(ev.prompt_ms / 1000)}`, ` in ${secs(ev.prompt_ms / 1000)}`);
      if (ev.prompt_cached) p += L(` (+${num(ev.prompt_cached)} aus Cache)`, ` (+${num(ev.prompt_cached)} cached)`);
      parts.push(p);
    }
    if (ev.tokens) parts.push(L(`${num(ev.tokens)} Token geschrieben`, `${num(ev.tokens)} tokens written`)
                              + (ev.tps ? `, ${ev.tps} tok/s` : ""));
    if (ev.calls && ev.calls.length) parts.push("→ " + ev.calls.join(", "));
    return parts.join(" · ") || L("fertig", "done");
  }

  function tickPhases() {
    const open = [...activity.querySelectorAll(".act.model.running"), ...chat.querySelectorAll(".live.on")];
    open.forEach((el) => {
      const s = Math.floor((performance.now() - el._start) / 1000);
      const t = el.querySelector(".act-status, .ls");
      if (t && (s >= 1 || t.classList.contains("act-status"))) t.textContent = secs(s);
    });
    if (!open.length) { clearInterval(phaseTimer); phaseTimer = null; }
  }

  // Aufgabenliste (todo_write) als kleine Checkliste in der laufenden Antwort – bleibt danach stehen
  function showTodos(items) {
    const a = S.currentMsg && assistants[S.currentMsg];
    if (!a) return;
    let box = a.el.querySelector(".todos");
    if (!box) {
      box = document.createElement("ul");
      box.className = "todos";
      a.el.insertBefore(box, a.live);
    }
    box.replaceChildren(...items.map((t) => {
      const li = document.createElement("li");
      li.className = t.state;
      li.textContent = t.text;
      return li;
    }));
    scrollChat();
  }

  // Live-Zeile in der Antwort (ersetzt den Cursor) – Sekunden zählt tickPhases
  function liveLine(text) {
    const el = document.createElement("div");
    el.className = "live on";
    el.innerHTML = `<div class="live-row"><span class="dot"></span><span class="lt"></span><span class="ls"></span></div>
      <div class="act-bar"><i></i></div>`;
    el.querySelector(".lt").textContent = text;
    el._start = performance.now();
    if (!phaseTimer) phaseTimer = setInterval(tickPhases, 1000);
    return el;
  }

  // Fortschrittsbalken beim Einlesen: echter Fortschritt (llama-server) oder geschätzt aus der gelernten Geschwindigkeit
  function setBar(bar, ev) {
    if (ev.phase === "prompt" && ev.progress != null) {
      bar.style.transition = "width .4s";
      bar.style.width = `${Math.round(ev.progress * 100)}%`;
    } else if (ev.phase === "prompt" && ev.eta_s > 0) {
      bar.style.transition = "none";
      bar.style.width = "0";
      void bar.offsetWidth;
      bar.style.transition = `width ${ev.eta_s}s linear`;
      bar.style.width = "95%";
    } else if (ev.phase !== "prompt") {
      bar.style.transition = "none";
      bar.style.width = "0";
    }
  }


  // Schritt einer laufenden Antwort: Live-Zeile in der Nachricht, Kennzahlen für die Statistik danach
  function chatPhase(a, ev) {
    const live = a.live;
    if (ev.phase === "done") {
      a.steps.push(ev);
      live.dataset.phase = "";
      setBar(live.querySelector(".act-bar i"), ev);
      live._start = performance.now();
      live.querySelector(".ls").textContent = "";
      live.querySelector(".lt").textContent = ev.calls && ev.calls.length
        ? L(`führt ${ev.calls.join(", ")} aus`, `running ${ev.calls.join(", ")}`) : L("gleich weiter", "continuing");
      return;
    }
    if (live._phase !== ev.id) { live._phase = ev.id; live._start = performance.now(); live.querySelector(".ls").textContent = ""; }
    const text = phaseText(ev);
    live.querySelector(".lt").textContent = text;
    live.dataset.phase = ev.phase;
    setBar(live.querySelector(".act-bar i"), ev);
    if (ev.phase === "compress" && Say.ctx !== "compress") { sayNext("compress"); refresh(); }
  }

  // Zusammenfassen nach einer schon fertigen Antwort (in Ruhe): kleine Zeile unter dieser Antwort
  function afterPhase(msgEl, ev) {
    let live = msgEl.querySelector(".live");
    if (ev.phase === "done") {
      if (!live) return;
      live.classList.remove("on");
      live.querySelector(".lt").textContent = phaseSummary(ev);
      live.querySelector(".ls").textContent = secs(ev.seconds ?? 0);
      live.dataset.phase = "";
      return;
    }
    if (!live) { live = liveLine(""); msgEl.appendChild(live); }
    const text = phaseText(ev);
    live.querySelector(".lt").textContent = text;
    if (ev.phase === "compress" && Say.ctx !== "compress") { sayNext("compress"); refresh(); }
  }

  // Nach der Antwort: eine dezente Zeile – Gesamtzeit, eingelesen, geschrieben, tok/s; aufgeklappt je Schritt
  function statsLine(a, cancelled) {
    const steps = a.steps;
    const sum = (k) => steps.reduce((n, s) => n + (Number(s[k]) || 0), 0);
    const read = sum("prompt_tokens"), cached = sum("prompt_cached"), written = sum("tokens");
    const timed = steps.filter((s) => s.tps && s.tokens);
    const tokTimed = timed.reduce((n, s) => n + s.tokens, 0);
    const tps = tokTimed ? timed.reduce((n, s) => n + s.tps * s.tokens, 0) / tokTimed : 0;
    const parts = [secs(Math.round((performance.now() - a.started) / 100) / 10)];
    if (read) parts.push(L(`${num(read)} Token eingelesen`, `${num(read)} tokens read`)
                         + (cached ? L(` (+${num(cached)} Cache)`, ` (+${num(cached)} cached)`) : ""));
    if (written) parts.push(L(`${num(written)} geschrieben`, `${num(written)} written`));
    if (tps) parts.push(`${tps.toLocaleString(LOCALE, { maximumFractionDigits: 1 })} tok/s`);
    if (steps.some((s) => s.error)) parts.unshift(L("Fehler beim Modell", "model error"));
    else if (cancelled) parts.unshift(L("abgebrochen", "stopped"));
    const el = document.createElement("details");
    el.className = "stats";
    el.innerHTML = `<summary></summary><ol></ol>`;
    el.querySelector("summary").textContent = parts.join(" · ");
    for (const s of steps) {
      const li = document.createElement("li");
      li.textContent = phaseSummary(s) + (s.seconds != null ? ` · ${secs(s.seconds)}` : "");
      el.querySelector("ol").appendChild(li);
    }
    return el;
  }

  function modelPhase(ev) {
    const a = ev.msg && assistants[ev.msg];
    if (a) return chatPhase(a, ev);
    const compress = ev.phase === "compress" || ev.compress;
    const msgEl = ev.msg && compress && chat.querySelector(`.msg[data-msg="${ev.msg}"]`);
    if (msgEl) return afterPhase(msgEl, ev);
    let el = acts["m-" + ev.id];
    if (!el) {
      if (ev.phase === "done") return;
      const empty = activity.querySelector(".empty");
      if (empty) empty.remove();
      el = document.createElement("div");
      el.className = "act model running";
      el.innerHTML = `<div class="act-head"><span class="act-name">🧠 ${L("Modell", "Model")}</span>
        <span><span class="act-time">${new Date().toLocaleTimeString(LOCALE)}</span>
        <span class="act-status">0 s</span></span></div>
        <div class="act-args"></div><div class="act-bar"><i></i></div>`;
      el._start = performance.now();
      activity.prepend(el);
      acts["m-" + ev.id] = el;
      while (activity.children.length > 60) activity.lastChild.remove();
      if (!phaseTimer) phaseTimer = setInterval(tickPhases, 1000);
    }
    const bar = el.querySelector(".act-bar i");
    if (ev.phase === "done") {
      el.className = "act model " + (ev.error ? "error" : "ok");
      el.querySelector(".act-args").textContent = phaseSummary(ev);
      el.querySelector(".act-status").textContent = secs(ev.seconds ?? (performance.now() - el._start) / 1000);
      delete acts["m-" + ev.id];
      return;
    }
    const text = phaseText(ev);
    el.querySelector(".act-args").textContent = text;
    el.dataset.phase = ev.phase;
    setBar(bar, ev);
    if (ev.phase === "compress" && Say.ctx !== "compress") { sayNext("compress"); refresh(); }
  }

  // Kurzname für den Werkzeug-Satelliten am Orb
  const SAT_GROUPS = [
    [/^look_at_/, ["SEHEN", "VISION"]], [/^(web_search|fetch_url|open_website)$/, ["WEB", "WEB"]], [/^paperless_/, ["PAPERLESS", "PAPERLESS"]],
    [/^mail_/, ["MAIL", "MAIL"]], [/^obsidian_/, ["OBSIDIAN", "OBSIDIAN"]], [/^trilium_/, ["TRILIUM", "TRILIUM"]],
    [/^ha_/, ["SMART HOME", "SMART HOME"]], [/^calendar_/, ["KALENDER", "CALENDAR"]], [/^run_shell$/, ["SHELL", "SHELL"]],
    [/(package|system_update)/, ["PAKETE", "PACKAGES"]], [/(file|folder)/, ["DATEIEN", "FILES"]],
    [/^weather/, ["WETTER", "WEATHER"]], [/^routine_/, ["ROUTINEN", "ROUTINES"]], [/reminder/, ["ERINNERUNG", "REMINDER"]],
    [/(remember|recall|forget|memory)/, ["GEDÄCHTNIS", "MEMORY"]],
  ];
  function satLabel(name) {
    const hit = SAT_GROUPS.find(([rx]) => rx.test(name));
    return hit ? hit[1][EN ? 1 : 0] : name.split("_")[0].toUpperCase();
  }

  // Symbol je Aktionsart (weitere Symbole: ICONS in orb.js); Gedächtnis leuchtet im Kern statt als Satellit
  const ICON_GROUPS = { shell: "cli", packages: "cli", sysadmin: "cli", system: "cli", power: "cli", web: "cloud",
                        mail: "mail", paperless: "paperless", vision: "eye" };

  function toolCall(ev) {
    activityArrived();
    if (!ev.routine) { sayNext(SAYINGS[ev.group] ? ev.group : "think"); refresh(); }
    if (ev.group === "memory_tools") orb.memoryGlow(ev.id, true, satLabel(ev.name));
    else orb.addSatellite(ev.id, satLabel(ev.name), ICON_GROUPS[ev.group] || null);
    const empty = activity.querySelector(".empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    const waiting = ev.risk === "confirm";
    const status = ev.risk === "blocked" ? "blocked" : waiting ? "waiting" : "running";
    el.className = "act running";
    el.innerHTML = `<div class="act-head"><span class="act-name">${escapeHtml(ev.name)}</span>
      <span><span class="act-time">${new Date().toLocaleTimeString(LOCALE)}</span>
      <span class="act-status">${STATUS_TEXT[status]}</span>
      <button class="toggle-out" title="${L("Ausgabe ein-/ausblenden", "Show/hide output")}">▾</button></span></div>
      <div class="act-args"></div><pre class="act-out"></pre>`;
    el.querySelector(".act-args").textContent = fmtArgs(ev.name, ev.args);
    if (ev.routine) el.querySelector(".act-name").textContent = `⟳ ${ev.routine} · ${ev.name}`;
    el.querySelector(".toggle-out").onclick = () => el.classList.toggle("open");
    activity.prepend(el);
    flashPanel(activity);
    acts[ev.id] = el;
    while (activity.children.length > 60) activity.lastChild.remove();

    const a = !ev.routine && S.currentMsg && assistants[S.currentMsg];
    if (a) {  // über der Nachricht nur der aktuelle Aufruf – die früheren stehen in der Aktivität
      const tools = a.el.querySelector(".tools");
      a.toolCount = (a.toolCount || 0) + 1;
      tools.replaceChildren();
      const chip = document.createElement("span");
      chip.className = "tool-chip running";
      chip.id = "chip-" + ev.id;
      chip.textContent = "⚙ " + ev.name;
      tools.appendChild(chip);
      if (a.toolCount > 1) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "tool-more";
        more.textContent = L(`+${a.toolCount - 1} vorher`, `+${a.toolCount - 1} earlier`);
        more.title = L("Alle Werkzeug-Aufrufe in der Aktivität zeigen", "Show all tool calls in the activity panel");
        more.onclick = () => setDrawer(true, true);
        tools.appendChild(more);
      }
    }
  }

  function toolOutput(ev) {
    const el = acts[ev.id];
    if (!el) return;
    const out = el.querySelector(".act-out");
    out.textContent = (out.textContent + ev.text).slice(-20000);
    out.scrollTop = out.scrollHeight;
  }

  function toolResult(ev) {
    orb.removeSatellite(ev.id, ev.status === "error" || ev.status === "blocked");
    orb.memoryGlow(ev.id, false);
    const el = acts[ev.id];
    if (el) {
      el.className = "act " + ev.status;
      el.querySelector(".act-status").textContent = STATUS_TEXT[ev.status] || ev.status;
      const out = el.querySelector(".act-out");
      if (!out.textContent.trim() || ev.status !== "ok") out.textContent = ev.text;
    }
    const chip = $("chip-" + ev.id);
    if (chip) chip.className = "tool-chip " + ev.status;
  }

  // ---------------------------------------------------------------- Bestätigung
  // Felder, die der Nutzer vor dem Bestätigen noch ändern darf (z. B. mail_send)
  const EDIT_FIELDS = {
    to: [L("AN", "TO"), "input"], cc: [L("CC", "CC"), "input"],
    subject: [L("BETREFF", "SUBJECT"), "input"], body: [L("TEXT", "TEXT"), "textarea"],
  };
  const APPROVE_HTML = $("confirm-yes").innerHTML;

  function openConfirm(ev) {
    S.confirm = ev;
    S.confirmListenSent = false;
    const editable = (ev.editable || []).filter((k) => EDIT_FIELDS[k]);
    const form = $("confirm-form");
    form.innerHTML = "";
    for (const key of editable) {
      const [label, kind] = EDIT_FIELDS[key];
      const row = document.createElement("label");
      row.innerHTML = `<span>${label}</span>`;
      const field = document.createElement(kind);
      if (kind === "input") field.type = "text";
      field.dataset.key = key;
      field.value = ev.args[key] ?? "";
      field.spellcheck = key === "body" || key === "subject";
      row.appendChild(field);
      form.appendChild(row);
    }
    form.classList.toggle("hidden", !editable.length);
    $("confirm-cmd").classList.toggle("hidden", !!editable.length);
    document.querySelector(".confirm-modal").classList.toggle("editing", !!editable.length);
    $("confirm-yes").innerHTML = editable.length && ev.name === "mail_send"
      ? `${L("SENDEN", "SEND")} <kbd>Strg+Enter</kbd>` : editable.length ? `${L("AUSFÜHREN", "RUN")} <kbd>Strg+Enter</kbd>` : APPROVE_HTML;
    $("confirm-summary").textContent = ev.name === "mail_send"
      ? L("Mail prüfen, bei Bedarf ändern und senden:", "Check the e-mail, edit it if needed and send it:")
      : L(`Soll ich ${ev.summary} ausführen?`, `Shall I run ${ev.summary}?`);
    $("confirm-cmd").textContent = ev.name === "run_shell" ? ev.args.command : `${ev.name}(${JSON.stringify(ev.args, null, 2)})`;
    $("confirm-reason").textContent = ev.reason && !editable.length ? L("Grund: ", "Reason: ") + ev.reason : "";
    $("confirm-voice").textContent = editable.length ? L("Senden nur per Klick – „Nein“ bricht ab.", "Send only by click – “no” cancels.")
      : A.micReady ? L("oder sag „Ja“ bzw. „Nein“", "or say “yes” or “no”") : "";
    $("confirm-voice").classList.remove("listening");
    $("confirm").classList.remove("hidden");
    const act = acts[ev.id];
    if (act) act.querySelector(".act-status").textContent = STATUS_TEXT.waiting;
    setTimeout(() => (form.querySelector("input, textarea") || $("confirm-yes")).focus(), 50);
    refresh();
    if (!S.tts) onSpeechDrained();
  }

  function editedFields() {
    const out = {};
    for (const f of $("confirm-form").querySelectorAll("[data-key]")) out[f.dataset.key] = f.value;
    return out;
  }

  function closeConfirm(id) {
    if (S.confirm && (!id || S.confirm.id === id)) {
      S.confirm = null;
      $("confirm").classList.add("hidden");
      if (S.recording) send({ type: "cancel_listen" });
      refresh();
    }
  }

  function answerConfirm(approved) {
    if (!S.confirm) return;
    const editing = !$("confirm-form").classList.contains("hidden");
    const args = editing && approved ? editedFields() : null;
    if (args && "to" in args && !args.to.trim()) {
      const to = $("confirm-form").querySelector('[data-key="to"]');
      to.classList.add("invalid");
      to.focus();
      toast(L("Bitte einen Empfänger eintragen.", "Please enter a recipient."));
      return;
    }
    send(args ? { type: "confirm", id: S.confirm.id, approved, args } : { type: "confirm", id: S.confirm.id, approved });
    const act = acts[S.confirm.id];
    if (act && approved) act.querySelector(".act-status").textContent = STATUS_TEXT.running;
    closeConfirm();
  }
  $("confirm-yes").onclick = () => answerConfirm(true);
  $("confirm-no").onclick = () => answerConfirm(false);

  // ---------------------------------------------------------------- Mikrofon
  let pttStart = 0;
  let pttActive = false;

  async function startPtt() {
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

  function endPtt() {
    if (!pttActive) return;
    pttActive = false;
    if (performance.now() - pttStart < 280) {
      // Kurz angetippt → zuhören, bis Stille erkannt wird
      startListen();
    } else {
      send({ type: "ptt_stop" });
    }
  }

  async function startListen() {
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

  const mic = $("btn-mic");
  mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startPtt(); });
  mic.addEventListener("pointerup", endPtt);
  mic.addEventListener("pointerleave", () => { if (pttActive) endPtt(); });

  // ---------------------------------------------------------------- Passwort für sudo (Root-Rechte)
  let pwId = null;
  function openPassword(ev) {
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
  function closePassword(id) {
    if (id && id !== pwId) return;
    pwId = null;
    $("pw-input").value = "";
    $("pw-modal").classList.add("hidden");
  }
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

  // Leertaste = Push-to-talk – aber nie, während man tippt (Chat, Planer, Briefing …) oder ein Knopf den Fokus hat
  function typingTarget(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(el.tagName));
  }

  document.addEventListener("keydown", (e) => {
    if (!$("boot").classList.contains("hidden")) return;
    if (pwId) return;  // Passwortfeld hat Vorrang (kein Push-to-talk mit Leertaste)
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
  let lastPrewarm = 0;
  function prewarmSoon() {
    if (!S.connected || Date.now() - lastPrewarm < 20000) return;
    lastPrewarm = Date.now();
    send({ type: "prewarm" });
  }
  $("input").addEventListener("input", prewarmSoon);

  // ---------------------------------------------------------------- Bilder anhängen (Büroklammer, Strg+V, Ziehen)
  const ATT = { items: [] };  // { name, url, pending }
  function renderAttachments() {
    const row = $("attach-row");
    row.innerHTML = "";
    row.classList.toggle("hidden", !ATT.items.length);
    for (const it of ATT.items) {
      const chip = document.createElement("div");
      chip.className = "attach-chip" + (it.pending ? " loading" : "");
      chip.innerHTML = `<img alt=""><button type="button" aria-label="${L("Entfernen", "Remove")}">✕</button>`;
      chip.querySelector("img").src = it.preview || it.url;
      chip.querySelector("button").onclick = () => { ATT.items = ATT.items.filter((x) => x !== it); renderAttachments(); };
      row.appendChild(chip);
    }
    if (ATT.items.length && ATT.vision === false) {
      const hint = document.createElement("span");
      hint.className = "attach-hint";
      hint.textContent = L("Das Modell sieht keine Bilder – das Vision-Modell schaut es sich an.",
                           "This model can't see images – the vision model will look at it.");
      row.appendChild(hint);
    }
  }
  function attachFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      if (ATT.items.length >= 4) { toast(L("Höchstens 4 Bilder pro Nachricht.", "At most 4 images per message.")); break; }
      if (f.size > 20e6) { toast(L("Bild zu groß (höchstens 20 MB).", "Image too large (20 MB max).")); continue; }
      const it = { pending: true, preview: URL.createObjectURL(f) };
      ATT.items.push(it);
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const r = await api("POST", "/api/attachments", { data: reader.result });
          Object.assign(it, { name: r.name, url: r.url, pending: false });
          ATT.vision = r.vision;
        } catch { ATT.items = ATT.items.filter((x) => x !== it); }
        renderAttachments();
      };
      reader.readAsDataURL(f);
    }
    renderAttachments();
    $("input").focus();
  }
  $("btn-attach").onclick = () => $("attach-input").click();
  $("attach-input").onchange = (e) => { attachFiles([...e.target.files]); e.target.value = ""; };
  $("input").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); attachFiles(files); }
  });
  const composer = $("form");
  composer.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); composer.classList.add("dragging"); } });
  composer.addEventListener("dragleave", () => composer.classList.remove("dragging"));
  composer.addEventListener("drop", (e) => {
    composer.classList.remove("dragging");
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    attachFiles([...e.dataTransfer.files]);
  });

  $("form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("input").value.trim();
    if (ATT.items.some((it) => it.pending)) { toast(L("Bild wird noch hochgeladen …", "Image still uploading …")); return; }
    const images = ATT.items.map((it) => it.name).filter(Boolean);
    if (!text && !images.length) return;
    if (!S.connected) { toast(L("Keine Verbindung zum Server.", "No connection to the server.")); return; }
    send({ type: "user_message", text, images });
    $("input").value = "";
    ATT.items = [];
    renderAttachments();
  });

  $("btn-plan").onclick = () => {
    S.plan = !S.plan;
    store.set("plan", S.plan);
    send({ type: "plan_mode", enabled: S.plan });
    toast(S.plan ? L("Planmodus an – Jarvis legt erst einen Plan vor, ausgeführt wird nach deiner Freigabe.",
                     "Plan mode on – Jarvis presents a plan first and carries it out once you approve it.")
                 : L("Planmodus aus – Jarvis legt direkt los.", "Plan mode off – Jarvis gets going right away."));
    refresh();
  };

  // Denken: aufklappbar mit Stufen (Modelle ohne echte Stufen bekommen ein Denk-Budget)
  const thinkMenu = $("think-menu");
  function setThinkMenu(open) {
    thinkMenu.classList.toggle("hidden", !open);
    $("btn-think").setAttribute("aria-expanded", String(open));
  }
  function setThink(level) {
    S.think = thinkLevel(level);
    store.set("think", S.think);
    send({ type: "think", enabled: S.think !== "off", level: S.think });
    toast({ off: L("Denkmodus aus – schnelle Antworten.", "Thinking mode off – fast answers."),
            low: L("Denken: kurz – Jarvis überlegt knapp und legt dann los.", "Thinking: brief – Jarvis thinks briefly, then acts."),
            medium: L("Denken: normal – zügig durchdacht, der Gedankengang erscheint beim Orb.",
                      "Thinking: normal – thought through efficiently, the reasoning appears next to the orb."),
            high: L("Denken: gründlich – so lange wie nötig, Antworten dauern länger.",
                    "Thinking: thorough – as long as needed, answers take longer.") }[S.think]);
    refresh();
  }
  $("btn-think").onclick = (e) => { e.stopPropagation(); setThinkMenu(thinkMenu.classList.contains("hidden")); };
  document.querySelectorAll("[data-think]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); setThinkMenu(false); setThink(b.dataset.think); };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#think-wrap")) setThinkMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !thinkMenu.classList.contains("hidden")) setThinkMenu(false); });

  // Auto-Modus: aufklappbarer Knopf neben „Denken“ (und Auswahl in den Einstellungen)
  const autoMenu = $("auto-menu");
  function setAutoMenu(open) {
    autoMenu.classList.toggle("hidden", !open);
    $("btn-auto").setAttribute("aria-expanded", String(open));
  }
  function setAutoMode(mode) {
    S.autoMode = mode;
    store.set("autoMode", mode);
    send({ type: "auto_mode", mode });
    toast({ off: L("Auto aus – jeder Shell-Befehl fragt vorher.", "Auto off – every shell command asks first."),
            read: L("Auto: nur lesen – erkannte lesende Befehle laufen ohne Rückfrage, Veränderndes fragt weiter.",
                    "Auto: read only – recognised read-only commands run without asking, changes still ask."),
            files: L("Auto: lesen + Dateien – Dateien in deinem Home werden ohne Rückfrage angelegt und geändert (ohne Root, Löschen fragt weiter).",
                     "Auto: read + files – files in your home are created and changed without asking (no root, deleting still asks)."),
            auto: L("Auto – alles ohne Root läuft ohne Rückfrage. Löschen, sudo, Ausschalten, Senden ins Netz und Startdateien fragen weiter.",
                    "Auto – everything without root runs without asking. Deleting, sudo, shutting down, sending over the network and start-up files still ask.") }[mode]);
    refresh();
  }
  $("btn-auto").onclick = (e) => { e.stopPropagation(); setAutoMenu(autoMenu.classList.contains("hidden")); };
  document.querySelectorAll("[data-auto]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); setAutoMenu(false); setAutoMode(b.dataset.auto); };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#auto-wrap")) setAutoMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !autoMenu.classList.contains("hidden")) setAutoMenu(false); });

  // Gemerktes sudo-Passwort: Anzeige in den Einstellungen (Status) mit „Vergessen“
  let sudoUntil = null, sudoTimer = null;
  function showSudo() {
    const row = $("pill-sudo");
    const left = sudoUntil ? Math.ceil((sudoUntil * 1000 - Date.now()) / 60000) : 0;
    const on = left > 0;
    row.classList.toggle("warn", on);
    row.querySelector("em").textContent = on
      ? L(`gemerkt – noch ${left} min (bis ${new Date(sudoUntil * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })})`,
          `remembered – ${left} min left (until ${new Date(sudoUntil * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })})`)
      : L("nicht gemerkt", "not remembered");
    $("btn-sudo-forget").classList.toggle("hidden", !on);
    if (!on && sudoTimer) { clearInterval(sudoTimer); sudoTimer = null; }
  }
  function sudoCached(ev) {
    const was = sudoUntil;
    sudoUntil = ev.until || null;
    if (sudoUntil && !sudoTimer) sudoTimer = setInterval(showSudo, 30000);
    if (sudoUntil && !was) toast(L("Root-Passwort gemerkt – Einstellungen → Status zum Vergessen.",
                                   "Root password remembered – Settings → Status to forget it."));
    showSudo();
  }
  $("btn-sudo-forget").onclick = () => send({ type: "password_forget" });

  $("btn-tts").onclick = () => {
    S.tts = !S.tts;
    store.set("tts", S.tts);
    send({ type: "tts", enabled: S.tts });
    if (!S.tts) stopSpeech(true);
    refresh();
  };

  $("btn-wake").onclick = async () => {
    const enable = !S.wake;
    if (enable) {
      if (S.status && !S.status.voice.wake) { toast(S.status.voice.wake_error || L("Wake-Word ist nicht verfügbar.", "Wake word is not available.")); return; }
      if (!(await initMic())) return;
    }
    S.wake = enable;
    store.set("wake", enable);
    updateMicStreaming();
    send({ type: "wake", enabled: enable });
    refresh();
  };

  $("btn-stop").onclick = () => { send({ type: "stop" }); stopSpeech(true); };

  // ---------------------------------------------------------------- Navigation: Sheets, Seitenleiste, Aktivität
  const app = $("app");
  const main = $("main");
  function isCompact() { return main.classList.contains("has-messages"); }
  // Kompakter Orb, sobald das Gespräch Nachrichten hat (Begrüßung + großer Orb nur im leeren Chat)
  const updateStage = () => {
    const has = !!$("chat").querySelector(".msg.user, .msg.assistant");
    if (has !== isCompact()) {
      main.classList.toggle("has-messages", has);
      if (has) orb.setZoom(false);
    }
  };
  new MutationObserver(updateStage).observe($("chat"), { childList: true });

  let openSheetName = null;
  function openSheet(name, section) {
    closeSheet();
    openSheetName = name;
    $("sheet-" + name).classList.remove("hidden");
    $("scrim").classList.remove("hidden");
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.sheet === name));
    app.classList.remove("side-open");
    if (name === "planner") loadPlanner();
    if (name === "memory") loadMemory();
    if (name === "settings") showSection(section || "models");
  }
  function closeSheet() {
    if (!openSheetName) return false;
    $("sheet-" + openSheetName).classList.add("hidden");
    $("scrim").classList.add("hidden");
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
    openSheetName = null;
    return true;
  }
  function showSection(section) {
    document.querySelectorAll(".set-tab").forEach((t) => t.classList.toggle("active", t.dataset.section === section));
    document.querySelectorAll(".set-section").forEach((el) => el.classList.toggle("hidden", el.id !== "set-" + section));
    if (section === "models") { openModelMenu(); loadCtxMemory().then(renderCtxSettings); }
    if (section === "voice") openVoiceMenu();
    if (section === "status") loadStatus();
  }
  document.querySelectorAll(".nav-item").forEach((n) => { n.onclick = () => openSheet(n.dataset.sheet); });
  document.querySelectorAll(".set-tab").forEach((t) => { t.onclick = () => showSection(t.dataset.section); });
  document.querySelectorAll("[data-close-sheet]").forEach((b) => { b.onclick = closeSheet; });
  $("scrim").onclick = () => { closeSheet(); app.classList.remove("side-open"); };
  document.querySelectorAll("[data-mirror]").forEach((sw) => { sw.onclick = () => $(sw.dataset.mirror).click(); });

  // Seitenleiste ein-/ausklappen (Desktop) bzw. als Menü öffnen (schmal)
  app.classList.toggle("side-collapsed", !!store.get("sideCollapsed", false));
  $("btn-sidebar").onclick = () => {
    const c = !app.classList.contains("side-collapsed");
    app.classList.toggle("side-collapsed", c);
    store.set("sideCollapsed", c);
  };
  $("btn-menu").onclick = () => { app.classList.add("side-open"); $("scrim").classList.remove("hidden"); };

  // Aktivität: öffnet sich beim ersten Werkzeug von selbst – außer man hat sie bewusst geschlossen
  let actUnseen = 0;
  function setDrawer(open, byUser) {
    app.classList.toggle("drawer-open", open);
    if (byUser) store.set("drawer", open ? "open" : "closed");
    if (open) { actUnseen = 0; $("act-badge").classList.add("hidden"); }
    $("btn-activity").classList.toggle("on", open);
  }
  setDrawer(store.get("drawer", "") === "open" && window.innerWidth > 1200);
  $("btn-activity").onclick = () => setDrawer(!app.classList.contains("drawer-open"), true);
  $("drawer-close").onclick = () => setDrawer(false, true);
  function activityArrived() {
    if (app.classList.contains("drawer-open")) return;
    if (store.get("drawer", "") !== "closed" && window.innerWidth > 1200) { setDrawer(true); return; }
    actUnseen += 1;
    $("act-badge").textContent = actUnseen > 9 ? "9+" : String(actUnseen);
    $("act-badge").classList.remove("hidden");
  }

  // ---------------------------------------------------------------- Stimme & Effekt
  function fxRate() {
    return S.fxOn ? +(1 - 0.08 * S.fxAmount).toFixed(3) : 1;
  }

  function sendVoiceSettings() {
    send({ type: "voice_settings", voice: S.voiceName || undefined, rate: fxRate() });
  }

  function renderFx() {
    $("fx-toggle").textContent = S.fxOn ? L("An", "On") : L("Aus", "Off");
    $("fx-toggle").classList.toggle("on", S.fxOn);
    $("fx-amount").value = Math.round(S.fxAmount * 100);
    $("fx-value").textContent = Math.round(S.fxAmount * 100) + " %";
    if (A.fx) A.fx.set(S.fxOn, S.fxAmount);
  }

  $("fx-toggle").onclick = () => {
    S.fxOn = !S.fxOn;
    store.set("fx", S.fxOn);
    renderFx();
    sendVoiceSettings();
  };
  $("fx-amount").oninput = (e) => {
    S.fxAmount = e.target.value / 100;
    store.set("fxAmount", S.fxAmount);
    renderFx();
  };
  $("fx-amount").onchange = () => sendVoiceSettings();
  renderFx();

  // Stimmen-Menü an der VOICE-Pille: auswählen, anhören, löschen, hinzufügen (wie das Modell-Menü)
  const voiceMenu = $("voice-menu");
  async function openVoiceMenu(catalog = false) {
    closeModelMenu();
    const list = $("voice-list");
    let data;
    try { data = await getJSON("/api/voices"); } catch { toast(L("Stimmen nicht ladbar", "Could not load voices")); return; }
    const st = S.status && S.status.voice;
    list.innerHTML = `<div class="mm-title">${catalog ? L("STIMME HINZUFÜGEN", "ADD VOICE") : L("STIMME WÄHLEN", "CHOOSE VOICE")}</div>
      <div class="mm-hint"></div>`;
    list.querySelector(".mm-hint").textContent = st ? [
      st.stt ? L("Spracherkennung ✔", "Speech recognition ✔") : L("Spracherkennung aus", "Speech recognition off"),
      st.wake ? L("Wake-Word ✔", "Wake word ✔") : L("Wake-Word aus", "Wake word off")].join(" · ") : "";
    if (!data.available) {
      list.insertAdjacentHTML("beforeend", `<div class="mm-hint">${L("Piper-Sprachausgabe ist deaktiviert – es spricht der Browser.",
        "Piper speech output is disabled – the browser speaks instead.")}</div>`);
    } else if (!catalog) {
      if (!S.voiceName) S.voiceName = data.current;
      for (const v of data.voices.filter((x) => x.installed)) list.appendChild(voiceRow(v, data.current));
      const add = document.createElement("button");
      add.className = "model-item add";
      add.textContent = L("+ STIMME HINZUFÜGEN …", "+ ADD VOICE …");
      add.onclick = (e) => { e.stopPropagation(); openVoiceMenu(true); };
      list.appendChild(add);
      // eigene Stimme (z. B. von huggingface.co): .onnx + .onnx.json wählen oder aufs Menü ziehen
      const up = document.createElement("button");
      up.className = "model-item add vm-upload";
      up.innerHTML = `<div>${L("⬆ EIGENE STIMME HOCHLADEN …", "⬆ UPLOAD OWN VOICE …")}</div><div class="mi-note"></div>`;
      up.querySelector(".mi-note").textContent = L("Piper-Stimme, z. B. von huggingface.co – beide Dateien (.onnx + .onnx.json), auch per Drag & Drop",
        "Piper voice, e.g. from huggingface.co – both files (.onnx + .onnx.json), drag & drop works too");
      up.onclick = (e) => {
        e.stopPropagation();
        const input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.accept = ".onnx,.json";
        input.onchange = () => uploadVoice([...input.files]);
        input.click();
      };
      list.appendChild(up);
    } else {
      // ganzer Piper-Katalog: Auswahl (empfohlen) zuerst, dann alle weiteren nach Region, mit Suchfeld
      const missing = data.voices.filter((x) => !x.installed);
      const tools = document.createElement("div");
      tools.className = "vm-tools";
      tools.innerHTML = `<input type="search" class="vm-search" placeholder="${L("Stimme suchen …", "Search voices …")}">
        <a href="https://rhasspy.github.io/piper-samples/" target="_blank" rel="noopener">${L("Probehören ↗", "Listen to samples ↗")}</a>`;
      list.appendChild(tools);
      const box = document.createElement("div");
      list.appendChild(box);
      const render = (q) => {
        box.innerHTML = "";
        const hits = missing.filter((v) => !q || `${v.label} ${v.name} ${v.description}`.toLowerCase().includes(q));
        if (!hits.length) {
          box.innerHTML = `<div class="mm-hint">${missing.length ? L("Keine Treffer.", "No matches.")
            : L("Alle Stimmen sind installiert.", "All voices are installed.")}</div>`;
        }
        let group = null;
        for (const v of hits) {
          const g = v.recommended ? L("EMPFOHLEN", "RECOMMENDED") : (v.locale || "").toUpperCase();
          if (g !== group) {
            group = g;
            box.insertAdjacentHTML("beforeend", `<div class="mm-title vm-group"></div>`);
            box.lastElementChild.textContent = g;
          }
          box.appendChild(catalogItem(v));
        }
      };
      const search = tools.querySelector(".vm-search");
      search.onclick = (e) => e.stopPropagation();
      search.oninput = () => render(search.value.trim().toLowerCase());
      render("");
      const back = document.createElement("button");
      back.className = "model-item add";
      back.textContent = L("← ZURÜCK", "← BACK");
      back.onclick = (e) => { e.stopPropagation(); openVoiceMenu(); };
      list.appendChild(back);
    }
    voiceMenu.classList.toggle("catalog", catalog);
    voiceMenu.classList.remove("hidden");
  }

  function catalogItem(v) {
    const b = voiceItem(v, v.download_mb ? `~${v.download_mb} MB` : "");
    b.onclick = async (e) => {
      e.stopPropagation();
      b.disabled = true;
      b.querySelector(".mi-tag").textContent = L("LÄDT …", "LOADING …");
      try {
        await api("POST", `/api/voices/${encodeURIComponent(v.name)}/install`);
        toast(L(`✔ ${v.label} installiert`, `✔ ${v.label} installed`));
        loadStatus();
        openVoiceMenu();
      } catch { b.disabled = false; b.querySelector(".mi-tag").textContent = v.download_mb ? `~${v.download_mb} MB` : ""; }
    };
    return b;
  }

  function voiceItem(v, tag) {
    const b = document.createElement("button");
    b.className = "model-item";
    b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
    b.querySelector(".mi-name").textContent = v.label;
    b.querySelector(".mi-tag").textContent = tag;
    b.querySelector(".mi-sub").textContent = v.description + (v.size_mb ? ` · ${v.size_mb} MB` : "");
    return b;
  }

  function voiceRow(v, current) {
    const active = v.name === current;
    const b = voiceItem(v, active ? L("AKTIV", "ACTIVE") : v.male === null ? "" : v.male ? L("MÄNNLICH", "MALE") : L("WEIBLICH", "FEMALE"));
    if (active) b.classList.add("active");
    b.onclick = (e) => {
      e.stopPropagation();
      if (active) return;
      S.voiceName = v.name;
      store.set("voice", v.name);
      sendVoiceSettings();
      toast(L(`Stimme: ${v.label}`, `Voice: ${v.label}`));
      setTimeout(() => openVoiceMenu(), 150);
    };
    const row = document.createElement("div");
    row.className = "model-row";
    const play = document.createElement("button");
    play.className = "model-del voice-play";
    play.textContent = "▶";
    play.title = L("Anhören", "Preview");
    play.onclick = (e) => { e.stopPropagation(); previewVoice(v.name); };
    const del = document.createElement("button");
    del.className = "model-del";
    del.textContent = "🗑";
    del.disabled = active;
    del.title = active ? L("Aktive Stimme – erst eine andere wählen", "Active voice – choose another one first") : L("Stimme löschen", "Delete voice");
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(L(`Stimme ${v.label} löschen?`, `Delete voice ${v.label}?`))) return;
      try {
        await api("DELETE", `/api/voices/${encodeURIComponent(v.name)}`);
        toast(L(`✔ ${v.label} gelöscht`, `✔ ${v.label} deleted`));
        openVoiceMenu();
      } catch { /* Meldung kommt von api() */ }
    };
    row.append(b, play, del);
    return row;
  }

  // Upload: erst die Konfiguration, dann das Modell (roher Datenstrom, Fortschritt im Menü)
  function putFile(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) return resolve(JSON.parse(xhr.responseText || "{}"));
        let msg = `${L("Fehler", "Error")} ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* egal */ }
        reject(new Error(msg));
      };
      xhr.onerror = () => reject(new Error(L("Verbindung unterbrochen", "Connection lost")));
      xhr.send(file);
    });
  }

  async function uploadVoice(files) {
    const model = files.find((f) => f.name.toLowerCase().endsWith(".onnx"));
    const config = files.find((f) => f.name.toLowerCase().endsWith(".json"));
    if (!model || !config) {
      toast(L("Bitte beide Dateien wählen: .onnx und .onnx.json", "Please choose both files: .onnx and .onnx.json"));
      return;
    }
    const name = model.name.replace(/\.onnx$/i, "").replace(/[^A-Za-z0-9_.-]/g, "_").replace(/\.\.+/g, ".").replace(/^[^A-Za-z0-9]+/, "").slice(0, 80);
    const up = voiceMenu.querySelector(".vm-upload");
    const note = up && up.querySelector(".mi-note");
    const show = (text) => { if (note) note.textContent = text; };
    if (up) up.disabled = true;
    try {
      const base = `/api/voices/upload/${encodeURIComponent(name)}`;
      show(L("Lade Konfiguration hoch …", "Uploading configuration …"));
      await putFile(`${base}/config`, config, () => {});
      await putFile(`${base}/model`, model, (p) => show(L(`Lade ${name} hoch … ${Math.floor(p * 100)} %`, `Uploading ${name} … ${Math.floor(p * 100)} %`)));
      toast(L(`✔ Stimme ${name} hinzugefügt`, `✔ Voice ${name} added`));
      openVoiceMenu();
    } catch (err) {
      toast(err.message);
      if (up) up.disabled = false;
      show(L("Hochladen fehlgeschlagen – nochmal versuchen?", "Upload failed – try again?"));
    }
  }

  voiceMenu.addEventListener("dragover", (e) => { e.preventDefault(); voiceMenu.classList.add("drop"); });
  voiceMenu.addEventListener("dragleave", () => voiceMenu.classList.remove("drop"));
  voiceMenu.addEventListener("drop", (e) => {
    e.preventDefault();
    voiceMenu.classList.remove("drop");
    uploadVoice([...e.dataTransfer.files]);
  });

  function closeVoiceMenu() { /* Stimmen stehen jetzt dauerhaft in den Einstellungen */ }
  $("pill-voice").onclick = () => openSheet("settings", "voice");

  async function previewVoice(name) {
    await initAudio();
    sendVoiceSettings();
    const r = await fetch(`/api/voices/${encodeURIComponent(name)}/preview`);
    if (!r.ok) { toast(L("Probe nicht möglich", "Preview not possible")); return; }
    const buf = await A.ctx.decodeAudioData(await r.arrayBuffer());
    stopSpeech(true);
    const src = A.ctx.createBufferSource();
    src.buffer = buf;
    src.playbackRate.value = fxRate();
    src.connect(A.fx ? A.fx.input : A.outAnalyser);
    A.current = src;
    S.playing = true;
    refresh();
    src.onended = () => { S.playing = false; A.current = null; refresh(); };
    src.start();
  }

  // ---------------------------------------------------------------- Telemetrie
  const T = { tokenTimes: [], exactTps: null, lastExact: 0, spark: {} };
  const SPARK_MAX = { gpu: 100, power: null, vram: null, ram: null, tps: null, ctx: 100 };

  // Kontext-Budget: genutzte (geschätzte) Prompt-Token im Verhältnis zum Budget des Modells
  function kTok(n) {
    return n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : String(n);
  }
  function showContext(c) {
    if (!c || !c.window) return;
    // Feste Grenze: das Kontextfenster des Modells. Vorne die echte Prompt-Größe laut Server, sonst die Schätzung
    // (beide in echten Token) – so wandert die Grenze nicht mehr mit Reserve, Denkmodus oder gelernter Schätzung.
    const used = c.real || c.tokens || c.used;
    const pct = (100 * used) / c.window;
    const p = c.parts || {};
    const ratio = c.tokens && c.used ? c.tokens / c.used : 1;
    const real = (n) => (n == null ? "?" : Math.round(n * ratio));
    const at = c.compact_at || 0;
    setTile("ctx", used / 1000, {
      pct,
      digits: 1,
      sub: `${Math.round(pct)} %` + (c.trimmed ? L(" · gekürzt", " · trimmed") : c.summarized ? L(" · zusammengefasst", " · summarised")
        : c.cleared ? L(" · ausgeblendet", " · hidden") : ""),
      title: [
        L(`Prompt ${c.real ? "" : "ca. "}${used} von ${c.window} Token (Kontextfenster des Modells)`,
          `Prompt ${c.real ? "" : "approx. "}${used} of ${c.window} tokens (the model's context window)`),
        c.small == null ? "" : c.small
          ? L("Kleines Fenster (unter 16k) – Sparmodus: wenige Werkzeuge vorab, der Rest kommt bei Bedarf; kürzere Ergebnisse.",
              "Small window (below 16k) – lean mode: few tools up front, the rest on demand; shorter results.")
          : L("Großes Fenster (ab 16k) – volle Grundausstattung an Werkzeugen.", "Large window (16k or more) – full set of core tools."),
        at ? L(`Platz schaffen bei ~${kTok(at)} (${Math.round(100 * at / c.window)} %) – noch ~${kTok(Math.max(0, at - used))} Token. `
               + `Bis dahin wird nur Neues eingelesen; dann werden erst alte Werkzeug-Ergebnisse ausgeblendet, zusammengefasst nur, wenn das nicht reicht.`,
               `Making room at ~${kTok(at)} (${Math.round(100 * at / c.window)} %) – ~${kTok(Math.max(0, at - used))} tokens left. `
               + `Until then only new parts are read; then old tool results are hidden first, a summary only if that isn't enough.`) : "",
        c.capped ? L(`Begrenzt durch memory.context_budget_tokens = ${c.capped} in der Config – der Prompt bleibt darunter, auch wenn das Fenster größer ist (Ältestes wird gekürzt). Für das volle Fenster die Zeile entfernen.`,
                     `Limited by memory.context_budget_tokens = ${c.capped} in the config – the prompt stays below it even with a bigger window (oldest parts are trimmed). Remove the line for the full window.`) : "",
        c.ratio && Math.abs(c.ratio - 1) > 0.25 ? L(`Schätzung angepasst: dieses Modell zählt ${fmt(c.ratio, 1)}× so viele Token wie geschätzt.`,
                                                  `Estimate adjusted: this model counts ${fmt(c.ratio, 1)}× the estimated tokens.`) : "",
        c.cleared ? L(`Ausgeblendet: ${oldResults(c.cleared)} (vollständig in Dateien, das Modell kann sie nachlesen).`,
                      `Hidden: ${oldResults(c.cleared)} (complete in files, the model can read them again).`) : "",
        c.reserve ? L(`${c.reserve} Token bleiben frei für Antwort${c.reserve > 2000 ? " und Denkkette" : ""}.`,
                      `${c.reserve} tokens are kept free for the answer${c.reserve > 2000 ? " and the reasoning" : ""}.`) : "",
        `System ${real(p.system)} · Tools ${real(p.tools)} · ${L("Gedächtnis", "Memory")} ${real(p.memory)} · `
          + `${L("Verlauf", "History")} ${real(p.history)}`,
        c.real ? L("Laut Modell-Server: ", "According to the model server: ") + `${c.real} Token`
          + (c.cached ? L(`, davon ${c.cached} aus dem Cache (schneller)`, `, ${c.cached} of them from the cache (faster)`) : "") : "",
        c.summarized ? L("Älterer Verlauf steht als Zusammenfassung im Prompt (im Chat bleibt alles sichtbar) – Details holt das Gedächtnis bei Bedarf zurück.",
                         "Older history is in the prompt as a summary (the chat still shows everything) – memory brings back details when needed.") : "",
        L("Klick: jetzt zusammenfassen (wie /compact)", "Click: summarise now (like /compact)"),
        c.trimmed ? L("Ältere Teile/lange Tool-Ergebnisse wurden gekürzt, damit alles passt.",
                      "Older parts/long tool results were trimmed so everything fits.") : "",
      ].filter(Boolean).join("\n"),
    });
    $("tele-ctx").querySelector(".tele-num").textContent = kTok(used);
    $("tele-ctx").querySelector(".tele-unit").textContent = "/" + Math.round(c.window / 1024) + "k";
    $("tele-ctx").classList.toggle("warn", c.trimmed || (at > 0 && used >= 0.9 * at));
    orb.setContext(used / c.window, !!c.summarized);
    pushSpark("ctx", Math.min(100, pct));
  }
  // Klick auf die Kontext-Kachel: jetzt zusammenfassen (wie „/compact“ in der Eingabe)
  $("tele-ctx").onclick = () => {
    if (!S.connected) return;
    if (window.confirm(L("Chat jetzt zusammenfassen? Der volle Verlauf bleibt sichtbar; das Modell arbeitet danach mit der Zusammenfassung weiter.",
                         "Summarise the chat now? The full history stays visible; the model then continues with the summary."))) {
      send({ type: "compact" });
    }
  };

  function fmt(v, digits = 1) {
    return v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString(LOCALE, {
      minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function setTile(key, value, { sub = "", pct = null, digits = 1, title = "" } = {}) {
    const el = $("tele-" + key);
    if (!el) return;
    el.querySelector(".tele-num").textContent = fmt(value, digits);
    el.querySelector(".tele-sub").textContent = sub;
    el.classList.toggle("na", value === null || value === undefined);
    el.classList.toggle("warn", pct !== null && pct >= 80 && pct < 95);
    el.classList.toggle("crit", pct !== null && pct >= 95);
    el.querySelector(".tele-bar i").style.width = pct === null ? "0" : Math.min(100, pct) + "%";
    if (title) el.title = title;
  }

  function pushSpark(key, value) {
    if (value === null || value === undefined) return;
    const arr = (T.spark[key] = T.spark[key] || []);
    arr.push(value);
    if (arr.length > 30) arr.shift();
    const canvas = document.querySelector(`#tele-${key} .tele-spark`);
    if (!canvas || canvas.offsetParent === null) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    const max = SPARK_MAX[key] || Math.max(...arr) * 1.15 || 1;
    ctx.clearRect(0, 0, w, h);
    ctx.beginPath();
    arr.forEach((v, i) => {
      const x = (i / 29) * w, y = h - 2 - (v / max) * (h - 4);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    const tile = canvas.closest(".tele");
    const color = tile.classList.contains("crit") ? "#ff5d6c" : tile.classList.contains("warn") ? "#ffb347" : "#3fd0ff";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.lineTo(((arr.length - 1) / 29) * w, h);
    ctx.lineTo(0, h);
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  function showMetrics(m) {
    const g = m.gpu;
    const gb = (b) => (b === null || b === undefined ? null : b / 1024 ** 3);
    if (g) {
      setTile("gpu", g.util, { pct: g.util, digits: 0, sub: g.temp !== null && g.temp !== undefined ? `${fmt(g.temp, 0)} °C` : "", title: g.name });
      const vp = g.vram_total ? (100 * g.vram_used) / g.vram_total : null;
      setTile("vram", gb(g.vram_used), { pct: vp, sub: `${L("von", "of")} ${fmt(gb(g.vram_total))} GB`, title: g.name });
      setTile("power", g.power, { digits: 0, sub: g.name ? g.name.replace(/^(NVIDIA|AMD)\s+/i, "") : "", title: g.name });
      pushSpark("gpu", g.util);
      pushSpark("vram", gb(g.vram_used));
      pushSpark("power", g.power);
    } else {
      setTile("gpu", null, { sub: L("keine GPU erkannt", "no GPU detected") });
      setTile("vram", null);
      setTile("power", null);
    }
    if (m.ram) {
      setTile("ram", gb(m.ram.used), { pct: (100 * m.ram.used) / m.ram.total,
        sub: `${L("von", "of")} ${fmt(gb(m.ram.total))} GB` + (m.cpu !== null && m.cpu !== undefined ? ` · CPU ${fmt(m.cpu, 0)} %` : "") });
      pushSpark("ram", gb(m.ram.used));
    }
  }

  // Live-Token/s während des Streamens (gleitendes 2-s-Fenster), danach exakter Ollama-Wert
  setInterval(() => {
    const now = performance.now();
    T.tokenTimes = T.tokenTimes.filter((t) => now - t < 2000);
    const streaming = Object.keys(assistants).length > 0;
    if (streaming && T.tokenTimes.length >= 3 && Date.now() - T.lastExact > 1500) {
      const span = (now - T.tokenTimes[0]) / 1000 || 1;
      const live = T.tokenTimes.length / Math.max(span, 0.25);
      setTile("tps", live, { sub: "live" });
    }
  }, 500);
  setTile("tps", null, { sub: L("wartet auf Antwort", "waiting for an answer") });

  // ---------------------------------------------------------------- Kontext im Speicher (VRAM/RAM) + Größe einstellen
  // Zeigt, wie viel der KV-Cache belegt und ob er im Grafikspeicher liegt – und wie groß das Kontextfenster werden
  // darf (llama-server: genau aus seinem Log, Ollama: geschätzt).
  const gbs = (b) => `${(b / 1024 ** 3).toLocaleString(LOCALE, { maximumFractionDigits: b < 1024 ** 3 ? 2 : 1 })} GB`;
  let lastMem = null;
  const ctxLabel = (n) => (n % 1024 === 0 ? `${n / 1024}k` : kTok(n));
  async function loadCtxMemory() {
    try { lastMem = await getJSON("/api/llm/memory"); } catch { return; }
    showMemory(lastMem);
    if (!$("set-models").classList.contains("hidden")) renderCtxSettings();
  }
  function memText(m) {
    const share = m.kv_vram_share ?? 1;
    const where = share >= 0.99 ? L("komplett im Grafikspeicher", "fully in VRAM")
      : L(`${Math.round(share * 100)} % im Grafikspeicher, ${gbs(m.kv_ram)} im RAM (langsam)`,
          `${Math.round(share * 100)} % in VRAM, ${gbs(m.kv_ram)} in RAM (slow)`);
    const model = m.model_ram_offload > 64 * 1024 ** 2
      ? L(` · Modell ${Math.round((m.model_vram_share ?? 1) * 100)} % im VRAM`, ` · model ${Math.round((m.model_vram_share ?? 1) * 100)} % in VRAM`) : "";
    return where + model;
  }
  function recText(m) {
    const r = m.recommend || {};
    if (r.verdict === "increase") return L(`bis ~${ctxLabel(r.max_ctx)} möglich`, `up to ~${ctxLabel(r.max_ctx)} possible`);
    if (r.verdict === "reduce") return L(`besser ${ctxLabel(r.max_ctx)} – dann passt alles in den VRAM`, `better ${ctxLabel(r.max_ctx)} – then everything fits in VRAM`);
    return r.verdict ? L("passt", "fits") : "";
  }
  function showMemory(m) {
    const box = $("tele-mem");
    if (!m || (!m.available && !m.reason)) { box.hidden = true; return; }  // Demo: nichts zu zeigen
    box.hidden = false;
    if (!m.available) {  // echtes Modell, aber keine Angaben – den Grund nennen statt still zu verschwinden
      $("tm-val").textContent = m.ctx ? ctxLabel(m.ctx) : "–";
      box.querySelectorAll(".tm-bar i").forEach((i) => { i.style.width = "0"; });
      $("tm-sub").textContent = L("keine Speicherangaben: ", "no memory data: ") + m.reason;
      box.title = m.reason;
      box.classList.remove("warn");
      return;
    }
    const share = m.kv_vram_share ?? 1;
    $("tm-val").textContent = `${ctxLabel(m.ctx)} · KV ${gbs(m.kv_bytes)}`;
    box.querySelector(".tm-bar .vram").style.width = `${share * 100}%`;
    box.querySelector(".tm-bar .ram").style.width = `${(1 - share) * 100}%`;
    const rec = recText(m);
    $("tm-sub").textContent = memText(m) + (rec ? ` · ${rec}` : "");
    box.classList.toggle("warn", (m.recommend || {}).verdict === "reduce");
    box.title = [
      L(`Kontextfenster ${num(m.ctx)} Token · KV-Cache ${gbs(m.kv_bytes)} (${gbs(m.kv_per_token * 1000)} pro 1.000 Token)`,
        `Context window ${num(m.ctx)} tokens · KV cache ${gbs(m.kv_bytes)} (${gbs(m.kv_per_token * 1000)} per 1,000 tokens)`),
      L(`Modell ${gbs(m.model_bytes)}`, `Model ${gbs(m.model_bytes)}`) + memText(m).replace(/^[^·]*/, ""),
      m.source === "estimate" ? L("Ollama: KV-Cache aus der Modell-Architektur geschätzt (f16).", "Ollama: KV cache estimated from the model architecture (f16).")
        : L("Genau laut llama-server-Log.", "Exact, from the llama-server log."),
      L("Ändern: Einstellungen → Modelle → Kontextfenster.", "Change it: Settings → Models → Context window."),
    ].join("\n");
  }
  setInterval(() => { if (S.connected && (app.classList.contains("drawer-open") || !$("set-models").classList.contains("hidden"))) loadCtxMemory(); }, 10000);

  async function renderCtxSettings() {
    const box = $("ctx-set");
    let models;
    try { models = await getJSON("/api/models"); } catch { return; }
    const p = models.profiles.find((x) => x.active);
    if (!p || models.active === "demo") { box.innerHTML = ""; return; }
    const m = lastMem && lastMem.available && lastMem.profile === p.name ? lastMem : null;
    const opts = (m && m.recommend && m.recommend.options) || [4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536].map((c) => ({ ctx: c }));
    box.innerHTML = `<div class="mm-title"></div><div class="ctx-now"></div><div class="ctx-opts"></div><p class="set-hint"></p>`
      + `<button type="button" class="btn small ctx-test-btn"></button><div class="ctx-test"></div>`;
    const testBtn = box.querySelector(".ctx-test-btn");
    testBtn.textContent = L("Kontext testen", "Test context");
    testBtn.title = L("Prüft mit dem aktiven Modell: Fenstergröße, Speicher, Token-Schätzung, Prompt-Cache und ein volles Fenster (kann einige Minuten dauern).",
                      "Checks with the active model: window size, memory, token estimate, prompt cache and a full window (may take a few minutes).");
    testBtn.disabled = !!models.switching;
    testBtn.onclick = () => runContextTest(box.querySelector(".ctx-test"), testBtn);
    box.querySelector(".mm-title").textContent = L(`KONTEXTFENSTER · ${p.label}`, `CONTEXT WINDOW · ${p.label}`);
    const missing = lastMem && !lastMem.available && lastMem.reason ? lastMem.reason : "";
    box.querySelector(".ctx-now").textContent = L(`Aktuell ${num(p.num_ctx)} Token`, `Currently ${num(p.num_ctx)} tokens`)
      + (m ? ` · KV-Cache ${gbs(m.kv_bytes)}, ${memText(m)}` + (recText(m) ? ` · ${recText(m)}` : "")
        + (m.source === "estimate" ? L(" (geschätzt)", " (estimated)") : "")
        : missing ? L(` · keine Speicherangaben: ${missing}`, ` · no memory data: ${missing}`) : "");
    for (const o of opts) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctx-opt" + (o.ctx === p.num_ctx ? " on" : "") + (o.fits === false ? " big" : "");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = ctxLabel(o.ctx);
      const lean = o.ctx < 16384 ? L("Sparmodus", "lean mode") : "";
      b.querySelector("small").textContent = (o.kv_bytes ? `KV ${gbs(o.kv_bytes)}` + (o.fits === false ? L(" · zu groß", " · too big") : "") : "")
        + (lean && o.kv_bytes ? " · " : "") + lean;
      b.disabled = !!models.switching;
      b.onclick = async () => {
        if (o.ctx === p.num_ctx) return;
        if (o.fits === false && !confirm(L(`${ctxLabel(o.ctx)} passt voraussichtlich nicht in den Grafikspeicher – Teile landen im RAM und alles wird deutlich langsamer. Trotzdem?`,
                                          `${ctxLabel(o.ctx)} probably doesn't fit in VRAM – parts move to RAM and everything gets much slower. Continue?`))) return;
        box.querySelectorAll(".ctx-opt").forEach((x) => { x.disabled = true; });
        toast(p.managed ? L("Modell-Server startet mit neuem Kontextfenster neu …", "Restarting the model server with the new context window …")
                        : L("Modell wird mit neuem Kontextfenster geladen …", "Reloading the model with the new context window …"));
        const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/context`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ctx: o.ctx }) });
        if (!r.ok) toast(L("Ändern fehlgeschlagen: ", "Change failed: ") + ((await r.json().catch(() => ({}))).detail || r.status));
        else toast(L(`Kontextfenster jetzt ${num(o.ctx)} Token.`, `Context window now ${num(o.ctx)} tokens.`));
        await loadCtxMemory();
        renderCtxSettings();
      };
      box.querySelector(".ctx-opts").appendChild(b);
    }
    box.querySelector(".set-hint").textContent = (p.managed
      ? L("Zum Ändern startet der Modell-Server neu (dauert etwas). ", "Changing it restarts the model server (takes a while). ")
      : L("Zum Ändern lädt Ollama das Modell neu. ", "Changing it makes Ollama reload the model. "))
      + L("Größeres Fenster = längere Chats bis zum Zusammenfassen, braucht aber mehr Grafikspeicher. Liegt der KV-Cache im RAM, wird alles deutlich langsamer. "
          + "Unter 16k arbeitet Orbwise im Sparmodus: wenige Werkzeuge vorab (der Rest kommt bei Bedarf), kürzere Werkzeug-Ergebnisse, alte Ergebnisse werden früher ausgeblendet.",
          "A bigger window means longer chats before summarising but needs more VRAM. If the KV cache lands in RAM, everything gets much slower. "
          + "Below 16k Orbwise runs in lean mode: few tools up front (the rest on demand), shorter tool results, old results are hidden sooner.");
  }

  async function runContextTest(out, btn) {
    btn.disabled = true;
    out.textContent = L("Test läuft – das volle Fenster einzulesen kann einige Minuten dauern …",
                        "Test running – reading the full window can take a few minutes …");
    try {
      const r = await fetch("/api/context/test", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) { out.textContent = L("Test nicht möglich: ", "Test not possible: ") + (data.detail || r.status); return; }
      const marks = { ok: "✔", warn: "⚠", fail: "✘", info: "ℹ" };
      out.innerHTML = "";
      for (const c of data.checks || []) {
        const row = document.createElement("div");
        row.className = "ct-row";
        row.innerHTML = `<span></span><span><b></b> <span></span></span>`;
        row.children[0].textContent = marks[c.status] || "·";
        row.children[0].className = "ct-" + c.status;
        row.querySelector("b").textContent = c.title;
        row.children[1].lastElementChild.textContent = c.detail;
        out.appendChild(row);
      }
    } catch (e) {
      out.textContent = L("Test abgebrochen: ", "Test aborted: ") + e;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- Modellauswahl
  const modelMenu = $("model-menu");
  async function openModelMenu() {
    closeVoiceMenu();
    let data;
    try { data = await getJSON("/api/models"); } catch { toast(L("Modelle nicht ladbar", "Could not load models")); return; }
    modelMenu.innerHTML = `<div class="mm-title">${L("MODELL WÄHLEN", "CHOOSE MODEL")}</div>`;
    for (const d of data.pulls || []) modelMenu.appendChild(pullRow(d));
    for (const p of data.profiles) {
      const b = document.createElement("button");
      b.className = "model-item" + (p.active ? " active" : "");
      b.disabled = !!data.switching;
      b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
      b.querySelector(".mi-name").textContent = p.label;
      b.querySelector(".mi-tag").textContent = p.active ? L("AKTIV", "ACTIVE")
        : p.name === data.switching ? L("LÄDT …", "LOADING …")
        : p.managed ? L("STARTET SERVER", "STARTS SERVER") : p.backend.toUpperCase();
      b.querySelector(".mi-sub").textContent = `${p.backend} · ${p.model}` + (p.size_gb ? ` · ${p.size_gb} GB` : "");
      b.onclick = async () => {
        closeModelMenu();
        if (p.active) return;
        // sofort sperren und markieren – der Aufruf kehrt erst nach dem Wechsel zurück
        modelMenu.querySelectorAll("button").forEach((x) => { x.disabled = true; });
        b.querySelector(".mi-tag").textContent = L("LÄDT …", "LOADING …");
        const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/activate`, { method: "POST" });
        if (!r.ok && r.status !== 502) toast(L("Umschalten fehlgeschlagen", "Switching failed"));
      };
      if (!p.deletable) { modelMenu.appendChild(b); continue; }
      const row = document.createElement("div");
      row.className = "model-row";
      const del = document.createElement("button");
      del.className = "model-del";
      del.textContent = "🗑";
      del.disabled = p.active || !!data.switching;
      del.title = p.active ? L("Aktives Modell – erst ein anderes wählen", "Active model – choose another one first")
        : L("Modell löschen", "Delete model");
      del.onclick = (e) => { e.stopPropagation(); deleteModel(p); };
      row.append(b, del);
      modelMenu.appendChild(row);
    }
    if (data.active !== "demo") {
      const add = document.createElement("button");
      add.className = "model-item add";
      add.textContent = L("+ MODELL HINZUFÜGEN …", "+ ADD MODEL …");
      add.onclick = (e) => { e.stopPropagation(); openPresetMenu(); };
      modelMenu.appendChild(add);
    }
    modelMenu.classList.remove("hidden");
  }
  // Liste und Kontext-Block nach Wechsel/Änderung neu zeichnen (nicht, solange die Vorauswahl offen ist)
  function refreshModelViews() {
    if (openSheetName !== "settings" || $("set-models").classList.contains("hidden") || modelMenu.querySelector(".fit-ok, .fit-tight, .fit-big")) return;
    openModelMenu();
    loadCtxMemory().then(renderCtxSettings);
  }
  // Vorauswahl bekannter Ollama-Modelle: passend zum Grafikspeicher, Laden mit Fortschritt (model_pull-Events)
  async function openPresetMenu() {
    let data;
    try { data = await getJSON("/api/models/presets"); } catch { toast(L("Vorauswahl nicht ladbar", "Could not load presets")); return; }
    const g = data.gpu;
    const gpuText = g.vendor === "none" ? L("keine GPU erkannt", "no GPU detected") : `${g.name || g.vendor} · ${g.vram_gb} GB`;
    modelMenu.innerHTML = `<div class="mm-title">${L("MODELL HINZUFÜGEN", "ADD MODEL")}</div>
      <div class="mm-hint"></div>`;
    modelMenu.querySelector(".mm-hint").textContent = gpuText + " · " +
      L("✔ passt · ~ teils im RAM (langsamer) · ✘ zu groß", "✔ fits · ~ partly in RAM (slower) · ✘ too big");
    for (const tag of data.pulling) modelMenu.appendChild(pullRow({ tag }));
    const marks = { ok: "✔", tight: "~", big: "✘" };
    for (const p of data.presets) {
      const b = document.createElement("button");
      b.className = "model-item";
      const pulling = data.pulling.includes(p.tag);
      b.innerHTML = `<div class="mi-head"><span><span class="fit-${p.fit}">${marks[p.fit]}</span> <span class="mi-name"></span></span>
        <span class="mi-tag"></span></div><div class="mi-sub"></div><div class="mi-note"></div>`;
      b.querySelector(".mi-name").textContent = p.label;
      b.querySelector(".mi-tag").textContent = pulling ? L("LÄDT …", "LOADING …")
        : p.installed ? L("INSTALLIERT", "INSTALLED") : p.recommended ? L("EMPFOHLEN", "RECOMMENDED") : "";
      b.querySelector(".mi-sub").textContent = (p.kind === "ollama" ? p.tag : L("eigener Server · Terminal", "own server · terminal"))
        + ` · ~${p.download_gb} GB`;
      b.querySelector(".mi-note").textContent = p.note;
      b.disabled = pulling;
      b.onclick = async (e) => {
        e.stopPropagation();
        if (p.kind !== "ollama") {
          toast(L(`${p.label}: Einrichtung im Terminal mit „orbwise model add ${p.tag}“ (lädt ~7 GB und den passenden llama.cpp-Server).`,
                  `${p.label}: set it up in a terminal with “orbwise model add ${p.tag}” (downloads ~7 GB and the matching llama.cpp server).`));
          return;
        }
        if (p.fit === "big" && !confirm(L(`${p.label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem laden?`,
                                          `${p.label} is too big for your video memory and will be very slow. Download anyway?`))) return;
        closeModelMenu();
        try {
          await api("POST", "/api/models/pull", { tag: p.tag });
          toast(L(`Lade ${p.tag} …`, `Downloading ${p.tag} …`));
        } catch { /* Meldung kommt von api() */ }
      };
      modelMenu.appendChild(b);
    }
    const back = document.createElement("button");
    back.className = "model-item add";
    back.textContent = L("← ZURÜCK", "← BACK");
    back.onclick = (e) => { e.stopPropagation(); openModelMenu(); };
    modelMenu.appendChild(back);
  }

  async function deleteModel(p) {
    const size = p.size_gb ? L(` Gibt ~${p.size_gb} GB frei.`, ` Frees ~${p.size_gb} GB.`) : "";
    if (!confirm(L(`${p.label} löschen? Die Modelldateien werden entfernt.`, `Delete ${p.label}? The model files are removed.`) + size)) return;
    try {
      await api("DELETE", `/api/models/${encodeURIComponent(p.name)}`);
      toast(L(`✔ ${p.label} gelöscht`, `✔ ${p.label} deleted`));
      openModelMenu();
    } catch { /* Meldung kommt von api() */ }
  }

  function pullPct(d) {
    return d.total ? Math.floor((100 * (d.completed || 0)) / d.total) : null;
  }

  // Laufender Download im Modell-Menü: Fortschritt live (model_pull-Events) und Abbrechen
  function pullRow(d) {
    const row = document.createElement("div");
    row.className = "model-pull";
    row.dataset.tag = d.tag;
    row.innerHTML = `<div class="mp-head"><span class="mp-text"></span>
      <button class="mp-cancel">✕ ${L("ABBRECHEN", "CANCEL")}</button></div><div class="mp-bar"><i></i></div>`;
    row.querySelector(".mp-cancel").onclick = async (e) => {
      e.stopPropagation();
      e.target.disabled = true;
      try { await api("DELETE", `/api/models/pull/${encodeURIComponent(d.tag)}`); } catch { /* Meldung kommt von api() */ }
    };
    updatePullRow(row, d);
    return row;
  }

  function updatePullRow(row, d) {
    const pct = pullPct(d);
    row.querySelector(".mp-text").textContent = `⬇ ${d.tag}` + (pct !== null ? ` · ${pct} %` : d.status ? ` · ${d.status}` : " …");
    row.querySelector(".mp-bar i").style.width = (pct || 0) + "%";
  }

  function modelPullEvent(ev) {
    const row = [...modelMenu.querySelectorAll(".model-pull")].find((r) => r.dataset.tag === ev.tag);
    if (row && (ev.done || ev.error || ev.cancelled)) row.remove();
    else if (row) updatePullRow(row, ev);
    if (ev.cancelled) { toast(L(`Download von ${ev.tag} abgebrochen`, `Download of ${ev.tag} cancelled`)); return; }
    if (ev.error) { toast(L(`✘ ${ev.tag}: `, `✘ ${ev.tag}: `) + ev.error); return; }
    if (ev.done) {
      toast(L(`✔ ${ev.tag} geladen – jetzt im Modell-Menü auswählbar.`, `✔ ${ev.tag} downloaded – now selectable in the model menu.`));
      addSystem(L(`Modell ${ev.tag} ist bereit (Menü LLM oben).`, `Model ${ev.tag} is ready (LLM menu at the top).`));
      return;
    }
    if (row) return;  // Menü offen: Fortschritt steht dort
    const pct = pullPct(ev) !== null ? ` ${pullPct(ev)} %` : "";
    toast(L(`Lade ${ev.tag}: `, `Downloading ${ev.tag}: `) + (ev.status || "") + pct
      + L(" · abbrechen im LLM-Menü", " · cancel in the LLM menu"));
  }

  function closeModelMenu() { /* Modelle stehen jetzt dauerhaft in den Einstellungen */ }
  $("pill-llm").onclick = () => openSheet("settings", "models");

  let toastTimer;
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
  }

  // ---------------------------------------------------------------- REST
  // ---------------------------------------------------------------- Planer: Routinen
  const DAYS = L("Mo Di Mi Do Fr Sa So", "Mo Tu We Th Fr Sa Su").split(" ");
  const R = { items: [], edit: null, days: new Set() };

  function loadPlanner() {
    loadRoutines();
    loadReminders();
    if ($("brief-box").open) loadBriefing();
  }
  $("brief-box").addEventListener("toggle", () => { if ($("brief-box").open) loadBriefing(); });

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

  async function loadRoutines() {
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
        <button class="ghost" data-a="run" title="${L("jetzt ausführen", "run now")}">▶</button>
        <button class="ghost" data-a="edit" title="${L("bearbeiten", "edit")}">✎</button>
        <button class="ghost" data-a="del" title="${L("löschen", "delete")}">✕</button>`;
      li.querySelector(".n").textContent = r.name;
      // „morgen 08:00“ → „morgen“, wenn die Uhrzeit ohnehin im Zeitplan steht
      const nextShort = r.next.endsWith(" " + r.time) ? r.next.slice(0, -r.time.length - 1) : r.next;
      const next = r.enabled && r.next !== "–" ? ` · ${nextShort}` : r.enabled ? "" : ` · ${L("pausiert", "paused")}`;
      li.querySelector(".rt-meta").textContent = r.schedule + next;
      li.querySelector(".rt-meta").title = r.enabled && r.next !== "–" ? `${L("nächste Ausführung", "next run")}: ${r.next}` : "";
      li.querySelector(".rt-main").title = r.task + (r.last_summary ? "\n\n" + L("Zuletzt: ", "Last: ") + r.last_summary : "");
      li.querySelector(".rt-main").onclick = () => {
        if (!r.chat_id) { toast(L("Noch kein Ergebnis – ▶ startet die Routine jetzt.", "No result yet – ▶ runs it now.")); return; }
        api("POST", `/api/chats/${r.chat_id}/activate`).catch(() => {});
      };
      li.querySelector("input").onchange = (e) => api("PUT", `/api/routines/${r.id}`, { enabled: e.target.checked })
        .then(loadRoutines).catch(() => toast(L("Speichern fehlgeschlagen", "Saving failed")));
      li.querySelectorAll("button").forEach((b) => b.onclick = async () => {
        if (b.dataset.a === "edit") return openRoutineForm(r);
        if (b.dataset.a === "del") {
          if (!confirm(L(`Routine „${r.name}“ löschen? Ihr Chat bleibt im Verlauf.`, `Delete routine “${r.name}”? Its chat stays in the history.`))) return;
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
  const B = { sections: [], settings: null, timer: null };

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

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  function setPill(id, cls, text) {
    const el = $(id);
    el.classList.remove("ok", "warn", "bad");
    el.classList.add(cls);
    el.querySelector("em").textContent = text;
  }

  async function loadStatus() {
    const asked = Date.now();
    try {
      const st = await getJSON("/api/status");
      S.status = st;
      if (st.name) document.querySelector(".brand-name").textContent = st.name;  // Persona (assistant_name)
      greet();
      const l = st.llm;
      const name = l.label && l.label !== l.model ? `${l.label} · ${l.model}` : l.model;
      // Nur eine Antwort, die nach dem letzten „Modell aktiv“ angefragt wurde, darf „lädt“ setzen – sonst bliebe
      // „Lade Modell“ nach dem Start stehen, bis man neu lädt
      if (l.switching && asked > S.modelDoneAt) { S.modelSwitching = S.modelSwitching || l.switching; refresh(); }
      else if (!l.switching && S.modelSwitching && asked > S.modelDoneAt) { S.modelSwitching = null; refresh(); }
      setPill("pill-llm", l.switching ? "warn" : !l.online ? "bad" : l.model_available ? "ok" : "warn",
        l.switching ? L("lädt …", "loading …") : !l.online ? `${l.label || l.model} offline` : l.model_available ? name : l.model + L(" fehlt", " missing"));
      const v = st.voice;
      const vCls = v.stt && v.tts ? "ok" : v.stt || v.tts ? "warn" : "bad";
      setPill("pill-voice", vCls, [v.stt ? "STT" : null, v.tts ? "TTS" : "TTS(Browser)", v.wake ? "WAKE" : null].filter(Boolean).join(" · "));
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
  setInterval(() => { if (S.connected) loadStatus(); }, 20000);

  // ---------------------------------------------------------------- Chat-Historie
  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {},
                                 body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) {
      let msg = `${L("Fehler", "Error")} ${r.status}`;
      try { msg = (await r.json()).detail || msg; } catch { /* egal */ }
      toast(msg);
      throw new Error(msg);
    }
    return r.json();
  }

  function chatWhen(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const today = new Date();
    const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
    if (d.toDateString() === new Date(Date.now() - 86400000).toDateString()) return `${L("gestern", "yesterday")} ${time}`;
    return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "2-digit" }) + " " + time;
  }

  let chatSearchTimer = null;
  async function loadChats() {
    const q = $("chat-search").value.trim();
    let list;
    try { list = await getJSON("/api/chats" + (q ? "?q=" + encodeURIComponent(q) : "")); } catch { return; }
    const ul = $("chat-list");
    ul.innerHTML = "";
    if (!list.length) {
      ul.innerHTML = `<li class="empty">${q ? L("Nichts gefunden.", "Nothing found.") : L("Noch keine Chats.", "No chats yet.")}</li>`;
      return;
    }
    for (const c of list) {
      const li = document.createElement("li");
      li.className = "chat-item" + (c.active ? " active" : "");
      li.innerHTML = `<button class="star${c.starred ? " on" : ""}" title="${c.starred ? L("Markierung entfernen", "Remove mark") : L("Als wichtig markieren", "Mark as important")}">${c.starred ? "★" : "☆"}</button>
        <div><div class="t"></div><div class="m"></div><div class="p"></div></div>
        <button class="del" title="${L("Chat löschen (auch aus dem Gedächtnis)", "Delete chat (also from memory)")}">✕</button>`;
      li.querySelector(".t").textContent = c.title;
      li.querySelector(".m").textContent = `${chatWhen(c.updated)} · ${c.messages} ${L("Nachr.", "msgs")}` + (c.active ? L(" · aktiv", " · active") : "");
      li.querySelector(".p").textContent = c.preview || "";
      li.onclick = () => { if (!c.active) api("POST", `/api/chats/${c.id}/activate`).catch(() => {}); };
      li.querySelector(".t").ondblclick = (e) => {
        e.stopPropagation();
        const title = prompt(L("Neuer Titel:", "New title:"), c.title);
        if (title && title.trim()) api("PATCH", `/api/chats/${c.id}`, { title }).then(loadChats).catch(() => {});
      };
      li.querySelector(".star").onclick = (e) => {
        e.stopPropagation();
        api("POST", `/api/chats/${c.id}/star`, { starred: !c.starred }).then(loadChats).catch(() => {});
      };
      li.querySelector(".del").onclick = (e) => {
        e.stopPropagation();
        const extra = c.legacy ? L("\n\nHinweis: Dieser Chat stammt von vor der Chat-Historie – ältere Tagebuch-Einträge "
          + "daraus lassen sich nicht zuordnen und bleiben im Gedächtnis.",
          "\n\nNote: this chat predates the chat history – older journal entries from it cannot be attributed and stay in memory.") : "";
        if (confirm(L(`„${c.title}“ löschen?\n\nDer Chat wird auch aus Jarvis' Gedächtnis entfernt (Tagebuch, Suche, `
            + `Tageszusammenfassung). Gelernte Fakten bleiben.`, `Delete “${c.title}”?\n\nThe chat is also removed from `
            + `Jarvis' memory (journal, search, daily summary). Learned facts are kept.`) + extra)) {
          api("DELETE", `/api/chats/${c.id}`).then(() => { toast(L("Chat gelöscht.", "Chat deleted.")); loadChats(); }).catch(() => {});
        }
      };
      ul.appendChild(li);
    }
  }
  $("chat-search").addEventListener("input", () => {
    clearTimeout(chatSearchTimer);
    chatSearchTimer = setTimeout(loadChats, 250);
  });
  $("chat-new").onclick = () => newChat();

  // ---------------------------------------------------------------- Bild-Modus (Qwen-Image-2.1)
  // Eigene Ansicht statt Chat: Prompt, Format, Galerie. Läuft ohne Sprachmodell; der Server gibt den Grafikspeicher
  // ans Sprachmodell zurück, sobald es wieder gebraucht wird (oder beim Verlassen der Ansicht).
  const IV = { open: false, status: null, ref: null, busy: false };
  const SIZE_LABEL = { "1:1": L("Quadrat 1:1", "Square 1:1"), "4:3": L("Quer 4:3", "Landscape 4:3"),
                       "3:4": L("Hoch 3:4", "Portrait 3:4"), "16:9": L("Breit 16:9", "Wide 16:9"),
                       "9:16": L("Handy 9:16", "Phone 9:16"), "3:2": L("Foto 3:2", "Photo 3:2"), "2:3": L("Foto hoch 2:3", "Photo portrait 2:3") };
  function ivText() {
    $("iv-prompt").placeholder = L("Beschreib das Bild – z. B. „ein Fuchs im Schnee, Abendlicht, Fotografie“ (Englisch klappt meist am besten)",
                                   "Describe the image – e.g. “a fox in the snow, evening light, photograph”");
    $("iv-neg-label").textContent = L("Was nicht aufs Bild soll", "What should not be in the image");
    $("iv-steps-label").textContent = L("Schritte", "Steps");
    $("iv-count-label").textContent = L("Anzahl", "Count");
    $("iv-seed").placeholder = L("zufällig", "random");
    $("iv-upload-label").querySelector("span").textContent = L("Bild bearbeiten …", "Edit an image …");
    $("iv-cancel").textContent = L("Abbrechen", "Cancel");
    $("iv-go").textContent = L("Erzeugen", "Generate");
  }
  async function openImageView() {
    IV.open = true;
    store.set("view", "image");
    app.classList.add("imaging");
    markModeTabs();
    ivText();
    try { IV.status = await getJSON("/api/image/status"); } catch { IV.status = null; }
    const st = IV.status || {};
    $("iv-setup").classList.toggle("hidden", !!st.available);
    $("iv-form").classList.toggle("hidden", !st.available);
    if (!st.available) {
      $("iv-setup").innerHTML = L(
        "<b>Bild-Modus einrichten</b><br>Einmal im Terminal: <code>orbwise model add qwen-image</code> – lädt stable-diffusion.cpp und Qwen-Image-2.1 (je nach Grafikkarte ~10–15 GB). Danach Orbwise neu laden.",
        "<b>Set up image mode</b><br>Once in a terminal: <code>orbwise model add qwen-image</code> – downloads stable-diffusion.cpp and Qwen-Image-2.1 (~10–15 GB depending on the GPU). Then reload Orbwise.");
    } else {
      const sel = $("iv-size");
      if (!sel.options.length) {
        for (const k of st.sizes || ["1:1"]) sel.add(new Option(SIZE_LABEL[k] || k, k));
        sel.value = store.get("iv.size", st.size || "1:1");
        $("iv-steps").value = store.get("iv.steps", st.steps || 20);
      }
      $("iv-upload-label").classList.toggle("hidden", !st.can_edit);
      setImageBusy(!!st.busy);
    }
    loadGallery();
    setTimeout(() => $("iv-prompt").focus(), 30);
  }
  function closeImageView() {
    IV.open = false;
    store.set("view", "chat");
    app.classList.remove("imaging");
    markModeTabs();
    api("POST", "/api/image/release").catch(() => {});  // Sprachmodell zurück in den Grafikspeicher
  }
  function setImageBusy(busy) {
    IV.busy = busy;
    $("iv-go").disabled = busy;
    $("iv-cancel").classList.toggle("hidden", !busy);
    $("iv-progress").classList.toggle("hidden", !busy);
  }
  function imageProgress(ev) {
    setImageBusy(true);
    const p = $("iv-progress");
    const text = $("iv-progress-text");
    p.classList.toggle("indeterminate", !ev.steps);
    if (ev.phase === "unload_llm") text.textContent = L("nimmt das Sprachmodell aus dem Grafikspeicher …", "moving the language model out of VRAM …");
    else if (ev.phase === "loading") text.textContent = ev.text || L("lädt das Bildmodell (beim ersten Bild etwas länger) …", "loading the image model (takes longer the first time) …");
    else if (ev.phase === "queued") text.textContent = L("wartet …", "waiting …");
    else if (ev.steps) {
      $("iv-bar").style.width = `${Math.round(100 * ev.step / ev.steps)}%`;
      text.textContent = L(`Schritt ${ev.step} von ${ev.steps}`, `step ${ev.step} of ${ev.steps}`)
        + (ev.eta_s >= 1 ? ` · ≈ ${secs(ev.eta_s)}` : "");
    } else text.textContent = L("rechnet …", "generating …");
  }
  function imageDone(ev) {
    setImageBusy(false);
    $("iv-bar").style.width = "0";
    loadGallery((ev.images || []).map((i) => i.file));
  }
  function imageError(ev) {
    setImageBusy(false);
    toast(L("Bild: ", "Image: ") + (ev.text || L("fehlgeschlagen", "failed")));
  }
  async function loadGallery(fresh = []) {
    let data;
    try { data = await getJSON("/api/image/list"); } catch { return; }
    const box = $("iv-gallery");
    box.innerHTML = "";
    if (!data.images.length) {
      box.innerHTML = `<div class="iv-empty">${L("Noch keine Bilder.", "No images yet.")}</div>`;
      return;
    }
    for (const img of data.images) box.appendChild(imageCard(img, fresh.includes(img.file)));
  }
  function imageCard(img, fresh) {
    const card = document.createElement("div");
    card.className = "iv-card" + (fresh ? " new" : "");
    const url = `/api/image/file/${encodeURIComponent(img.file)}`;
    card.innerHTML = `<img loading="lazy" alt=""><div class="iv-cap"></div><div class="iv-meta"></div><div class="iv-actions"></div>`;
    card.querySelector("img").src = url;
    card.querySelector("img").alt = img.prompt || "";
    card.querySelector("img").onclick = () => window.open(url, "_blank");
    card.querySelector(".iv-cap").textContent = (img.edit ? "✎ " : "") + (img.prompt || "");
    card.querySelector(".iv-meta").textContent = `${img.width}×${img.height} · ${img.steps} ${L("Schritte", "steps")} · Seed ${img.seed}`
      + (img.seconds ? ` · ${secs(img.seconds)}` : "");
    const actions = card.querySelector(".iv-actions");
    const add = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn small"; b.textContent = label; b.title = title; b.onclick = fn;
      actions.appendChild(b);
    };
    add(L("Variante", "Variation"), L("Gleicher Prompt, neuer Seed", "Same prompt, new seed"), () => {
      $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = "";
      generateImage();
    });
    add(L("Gleich", "Reuse"), L("Prompt und Einstellungen übernehmen (Seed bleibt)", "Reuse prompt and settings (same seed)"), () => {
      $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = img.seed;
      $("iv-steps").value = img.steps; $("iv-prompt").focus();
    });
    if (IV.status && IV.status.can_edit) add(L("Bearbeiten", "Edit"), L("Dieses Bild als Vorlage verwenden – beschreib die Änderung", "Use this image as the base – describe the change"), () => {
      setRef({ file: img.file, url }); $("iv-prompt").value = ""; $("iv-prompt").focus();
    });
    if (IV.status && IV.status.telegram) add(L("Handy", "Phone"), L("Per Telegram aufs Handy", "Send to the phone via Telegram"), () =>
      api("POST", `/api/image/telegram/${encodeURIComponent(img.file)}`).then(() => toast(L("Aufs Handy geschickt.", "Sent to the phone."))).catch(() => {}));
    add("🗑", L("Löschen", "Delete"), () => {
      if (!confirm(L("Bild löschen?", "Delete image?"))) return;
      api("DELETE", `/api/image/file/${encodeURIComponent(img.file)}`).then(() => card.remove()).catch(() => {});
    });
    return card;
  }
  function setRef(ref) {
    IV.ref = ref;
    $("iv-ref").classList.toggle("hidden", !ref);
    if (ref) {
      $("iv-ref-img").src = ref.url || ref.data;
      $("iv-ref-text").textContent = L("Wird bearbeitet – beschreib die Änderung (z. B. „mach den Himmel rot“).",
                                       "Being edited – describe the change (e.g. “make the sky red”).");
    }
  }
  $("iv-ref-clear").onclick = () => setRef(null);
  $("iv-upload").onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    if (f.size > 20e6) { toast(L("Bild zu groß (höchstens 20 MB).", "Image too large (20 MB max).")); return; }
    const reader = new FileReader();
    reader.onload = () => { setRef({ data: reader.result }); $("iv-prompt").focus(); };
    reader.readAsDataURL(f);
  };
  async function generateImage() {
    const prompt = $("iv-prompt").value.trim();
    if (!prompt) { $("iv-prompt").focus(); return; }
    store.set("iv.size", $("iv-size").value);
    store.set("iv.steps", Number($("iv-steps").value) || 20);
    const seed = $("iv-seed").value === "" ? -1 : Number($("iv-seed").value);
    const body = { prompt, negative: $("iv-negative").value, size: $("iv-size").value, steps: Number($("iv-steps").value) || 0,
                   count: Number($("iv-count").value) || 1, seed };
    if (IV.ref && IV.ref.file) body.ref_files = [IV.ref.file];
    if (IV.ref && IV.ref.data) body.ref_images = [IV.ref.data];
    setImageBusy(true);
    imageProgress({ phase: "queued" });
    try { await api("POST", "/api/image/generate", body); } catch { setImageBusy(false); }
  }
  $("iv-form").addEventListener("submit", (e) => { e.preventDefault(); generateImage(); });
  $("iv-prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); generateImage(); }
  });
  $("iv-cancel").onclick = () => api("POST", "/api/image/cancel").catch(() => {});
  if (store.get("view", "chat") === "image") setTimeout(openImageView, 0);  // zuletzt offen → wieder öffnen

  // ---------------------------------------------------------------- Modi: Tools (Assistent) / Coding
  // Getrennte Chat-Verläufe; Coding lädt nur Dateien, Shell, Web und Gedächtnis und hat keine Sprache.
  const MODE_TEXT = {
    tools: { placeholder: L("Frag Jarvis oder sag „Hey Jarvis“ …", "Ask Jarvis or say “Hey Jarvis” …"),
             hint: L("Leertaste halten zum Sprechen · Esc bricht ab · Änderungen am System fragt Jarvis vorher",
                     "Hold the space bar to talk · Esc cancels · Jarvis asks before changing your system") },
    coding: { placeholder: L("Beschreib die Aufgabe – Code, Fehler, Idee …", "Describe the task – code, bug, idea …"),
              hint: L("Coding: Dateien, Shell, Web und Gedächtnis · Esc bricht ab · Auto-Modus regelt, was ohne Rückfrage läuft",
                      "Coding: files, shell, web and memory · Esc cancels · the auto mode decides what runs without asking") },
  };
  function applyMode(ev) {
    if (!ev || !ev.mode) return;
    const was = S.mode;
    S.mode = ev.mode;
    S.chatId = ev.chat_id || ev.id || S.chatId;
    S.project = ev.project || "";
    app.classList.toggle("coding", S.mode === "coding");
    markModeTabs();
    $("input").placeholder = MODE_TEXT[S.mode].placeholder;
    document.querySelector(".composer-hint").textContent = MODE_TEXT[S.mode].hint;
    const chip = $("project-chip");
    chip.classList.toggle("unset", !S.project);
    $("project-label").textContent = S.project ? S.project.replace(/^\/home\/[^/]+/, "~") : L("Projektordner wählen", "Choose project folder");
    if (was !== S.mode) {
      if (S.mode === "coding") {  // keine Sprache: Vorlesen stoppen, Wake-Word ruhen lassen
        stopSpeech(true);
        if (S.wake) send({ type: "wake", enabled: false });
      } else if (was === "coding" && S.wake && A.micReady) send({ type: "wake", enabled: true });
      greet();
    }
    refresh();
  }
  function markModeTabs() {
    const current = IV.open ? "image" : S.mode;
    document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
      b.classList.toggle("on", b.dataset.mode === current);
      b.setAttribute("aria-selected", String(b.dataset.mode === current));
    });
  }
  document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.mode === "image") { openImageView(); return; }
      if (IV.open) closeImageView();
      if (b.dataset.mode === S.mode) return;
      api("POST", "/api/mode", { mode: b.dataset.mode })
        .then(() => toast(b.dataset.mode === "coding"
          ? L("Coding-Modus – nur Dateien, Shell, Web und Gedächtnis, ohne Sprache. Eigene Chats.", "Coding mode – only files, shell, web and memory, no voice. Separate chats.")
          : L("Tools-Modus – alle Werkzeuge und Sprache.", "Tools mode – all tools and voice.")))
        .catch(() => {});  // Fehler zeigt api() schon an
    };
  });
  $("project-chip").onclick = () => {
    const path = window.prompt(L("Projektordner für diesen Chat (leer = keiner):", "Project folder for this chat (empty = none):"),
                               S.project || "~/");
    if (path === null || !S.chatId) return;
    api("POST", `/api/chats/${S.chatId}/project`, { path: path.trim() })
      .then((r) => toast(r.project ? L(`Projektordner: ${r.project}`, `Project folder: ${r.project}`) : L("Kein Projektordner.", "No project folder.")))
      .catch(() => {});  // Fehler zeigt api() schon an
  };

  function newChat() {
    api("POST", "/api/chats").catch(() => {});
  }

  function openChatView(ev) {
    for (const id of Object.keys(assistants)) delete assistants[id];
    $("chat").innerHTML = "";
    $("chat-title").textContent = ev.title || "";
    loadHistory();
    loadChats();
  }

  async function loadHistory() {
    try {
      const h = await getJSON("/api/history");
      S.historyLoaded = true;
      setTimeout(renderBoot, 0);
      $("chat-title").textContent = h.chat && h.chat.title ? h.chat.title : "";
      if (h.summary) addSystem(L("Frühere Gesprächsteile sind im Gedächtnis zusammengefasst.", "Earlier parts of this chat are summarised in memory."));
      const dividers = {};
      for (const ep of h.epochs || []) dividers[ep.at] = ep;
      h.messages.forEach((m, i) => {
        if (dividers[i]) { chat.appendChild(compactDivider(dividers[i].summary, dividers[i].ts)); }
        if (m.role === "user") addUser(m.content, "", m.images);
        else addMsg("assistant", "JARVIS", renderMarkdown(m.content));
      });
    } catch { /* egal */ }
  }

  // ---------------------------------------------------------------- Erinnerungen
  function reminderWhen(iso) {
    const d = new Date(iso);
    const today = new Date();
    const tomorrow = new Date(Date.now() + 86400000);
    const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
    if (d.toDateString() === tomorrow.toDateString()) return `${L("morgen", "tomorrow")} ${time}`;
    return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" }) + " " + time;
  }

  async function loadReminders() {
    const list = $("reminders");
    try {
      const items = await getJSON("/api/reminders");
      list.innerHTML = items.length ? "" : `<li class="empty">${L("Keine anstehenden Erinnerungen.", "No upcoming reminders.")}</li>`;
      for (const r of items) {
        const li = document.createElement("li");
        li.innerHTML = `<span class="rem-when"></span><span class="rem-text"></span><button class="ghost small" title="${L("Löschen", "Delete")}">✕</button>`;
        li.querySelector(".rem-when").textContent = (r.kind === "timer" ? "⏱ " : "") + reminderWhen(r.due);
        li.querySelector(".rem-text").textContent = r.text;
        li.querySelector("button").onclick = async () => {
          await fetch(`/api/reminders/${encodeURIComponent(r.id)}`, { method: "DELETE" });
          loadReminders();
        };
        list.appendChild(li);
      }
    } catch {
      list.innerHTML = `<li class="empty">${L("Erinnerungen nicht ladbar.", "Could not load reminders.")}</li>`;
    }
  }

  function chime() {
    if (!A.ctx) return;
    const t0 = A.ctx.currentTime;
    [880, 1318.5, 1760].forEach((freq, i) => {
      const osc = A.ctx.createOscillator();
      const gain = A.ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const t = t0 + i * 0.18;
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.25, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.9);
      osc.connect(gain).connect(A.ctx.destination);
      osc.start(t);
      osc.stop(t + 1);
    });
  }

  function showReminder(ev) {
    $("reminder-kind").textContent = ev.late ? L("VERPASSTE ERINNERUNG", "MISSED REMINDER")
      : ev.kind === "timer" ? L("TIMER ABGELAUFEN", "TIMER FINISHED") : L("ERINNERUNG", "REMINDER");
    $("reminder-text").textContent = ev.text;
    $("reminder-banner").classList.remove("hidden");
    chime();
    addSystem("🔔 " + ev.spoken);
    if (!$("tab-memory").classList.contains("hidden")) loadReminders();
  }
  $("reminder-ok").onclick = () => $("reminder-banner").classList.add("hidden");

  async function loadMemory() {
    try {
      const [facts, days] = await Promise.all([getJSON("/api/memory/facts"), getJSON("/api/memory/days")]);
      $("facts").innerHTML = facts.length
        ? facts.map((f) => `<li>${escapeHtml(f.fact)}<small>${f.day}</small></li>`).join("")
        : `<li class="empty">${L("Noch keine Fakten gespeichert.", "No facts stored yet.")}</li>`;
      $("days").innerHTML = days.length
        ? days.map((d) => `<li><button data-day="${d.day}">${formatDay(d.day)}<span>${d.summary ? L("ZUSAMMENFASSUNG", "SUMMARY") : L("PROTOKOLL", "LOG")}</span></button></li>`).join("")
        : `<li class="empty">${L("Noch keine Einträge.", "No entries yet.")}</li>`;
      $("days").querySelectorAll("button").forEach((b) => (b.onclick = () => openDay(b.dataset.day)));
    } catch (err) {
      toast(L("Gedächtnis nicht ladbar: ", "Could not load memory: ") + err.message);
    }
  }

  function formatDay(day) {
    const d = new Date(day + "T12:00:00");
    return d.toLocaleDateString(LOCALE, { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  }

  async function openDay(day) {
    const d = await getJSON("/api/memory/day/" + day);
    $("day-title").textContent = formatDay(day).toUpperCase();
    let html = "";
    if (d.summary) html += `<h3>${L("ZUSAMMENFASSUNG", "SUMMARY")}</h3><div class="body">${renderMarkdown(d.summary.replace(/^# .*\n/, ""))}</div>`;
    if (d.journal) html += `<h3>${L("PROTOKOLL", "LOG")}</h3><pre>${escapeHtml(d.journal.replace(/^# .*\n/, ""))}</pre>`;
    $("day-body").innerHTML = html || L("Keine Einträge.", "No entries.");
    $("day-modal").classList.remove("hidden");
  }
  $("day-close").onclick = () => $("day-modal").classList.add("hidden");

  // ---------------------------------------------------------------- Begrüßung (leerer Chat)
  function greet() {
    const h = new Date().getHours();
    const part = h < 5 ? L("Gute Nacht", "Good night") : h < 11 ? L("Guten Morgen", "Good morning")
      : h < 18 ? L("Guten Tag", "Good afternoon") : L("Guten Abend", "Good evening");
    const user = S.status && S.status.user ? ", " + S.status.user : "";
    $("greeting").textContent = S.mode === "coding" ? L("Woran arbeiten wir?", "What are we working on?")
      : `${part}${user} – ${L("wie kann ich helfen?", "how can I help?")}`;
  }
  setInterval(greet, 60000);
  greet();
  document.querySelectorAll(".suggestion").forEach((b) => {
    b.onclick = () => { $("input").value = b.dataset.text; $("form").requestSubmit(); };
  });

  // ---------------------------------------------------------------- Start: was lädt gerade?
  // Der Server meldet seine Schritte live (Ereignis „startup“); Verbindung und Chat-Verlauf prüft die Oberfläche selbst.
  const Boot = { server: null, entered: false };
  const BOOT_ICON = { ok: "✓", warn: "!", error: "✕", off: "–", running: "", pending: "" };
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
  function renderBoot() {
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
  setInterval(() => { if (!Boot.entered) renderBoot(); }, 1000);

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
  $("boot-btn").onclick = () => enter(true);

  renderBoot();
  connect();
  refresh();
})();
