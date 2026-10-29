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

  // SVG-Symbol aus dem Sprite in index.html; Farbe/Wert einer CSS-Variablen (für Canvas)
  const ico = (name) => `<svg class="i"><use href="#i-${name}"/></svg>`;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  // Denkstufe: off | low | medium | high (früher gespeichert als true/false)
  // Denken: "off", "on" (Modelle ohne Stufen) oder eine Stufe, die das Modell selbst vorgibt (z. B. low/medium/high)
  function thinkLevel(v) { return v === true ? "on" : typeof v === "string" && /^[a-z][a-z-]{1,15}$/.test(v) ? v : "off"; }
  // Stufen heißen so, wie das Modell sie nennt (z. B. "low", "xhigh") – keine eigenen Namen oder Stufen
  function thinkName(k) { return k; }
  const AUTO_LABEL = { off: L("Auto aus", "Auto off"), read: L("Lesen", "Read"), files: L("Dateien", "Files"), auto: "Auto" };
  const S = {
    name: "Orbwise", wakePhrase: "Wake-Word",  // aus „hello“: assistant_name und der Satz des Wake-Word-Modells
    previews: {},  // Diff-Vorschau je Werkzeug-Aufruf (für die Freigabe-Karte)
    question: null,  // offene Rückfrage (Werkzeug question)
    ws: null, connected: false, retry: 0,
    serverState: "idle", substate: "",
    tts: store.get("tts", true), wake: store.get("wake", false), think: thinkLevel(store.get("think", "off")), autoMode: store.get("autoMode", store.get("auto", true) === false ? "off" : "read"), plan: store.get("plan", false),
    fxOn: store.get("fx", true), fxAmount: store.get("fxAmount", 0.6),
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

  const SPEECH_LOCALES = { de: "de-DE", en: "en-GB", fr: "fr-FR", es: "es-ES", it: "it-IT", pt: "pt-PT", nl: "nl-NL" };

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
        const lang = ev.lang || (EN ? "en" : "de");  // Sprache der gewählten Stimme
        u.lang = SPEECH_LOCALES[lang] || lang;
        if (germanVoice && lang === "de") u.voice = germanVoice;
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
    // Vorwärmen (Chat einlesen) läuft im Hintergrund – sonst stünde „Bereit“, obwohl das Modell beschäftigt ist
    const warming = !loadingModel && s === "idle" && S.prewarm && Date.now() - S.prewarm.at < 600000;
    if (warming) s = "thinking";
    orb.setState(s);
    const label = $("state-label");
    label.textContent = loadingModel && S.connected ? L("LADE MODELL", "LOADING MODEL")
      : warming ? L("LESE CHAT EIN", "READING CHAT") : (LABELS[s] || s.toUpperCase());
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
      send({ type: "plan_mode", enabled: S.plan });  // Denken und Auto-Modus kommen vom Server (hello)
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

  function send(obj) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  }

  // Unter dem Orb, solange Orbwise arbeitet: wechselnde Sprüche passend zur Tätigkeit (die technischen
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
        S.name = ev.name || "Orbwise";
        S.wakePhrase = ev.wake_phrase || S.wakePhrase;
        S.serverState = ev.busy ? "thinking" : "idle";  // tatsächlichen Zustand übernehmen (auch nach Neuverbinden)
        S.transcribing = false;  // neue Verbindung = neue Audio-Sitzung, eine alte Transkription meldet sich nie mehr
        if (ev.context) showContext(ev.context);
        applyMode(ev);
        // Bildmodus nur innerhalb desselben Serverlaufs wiederherstellen – nach einem Neustart von Orbwise: leerer Chat
        if (ev.boot && ev.boot !== store.get("boot", "")) { store.set("boot", ev.boot); store.set("view", "chat"); }
        else if (store.get("view", "chat") === "image" && !IV.open) setTimeout(openImageView, 0);
        applySettings(ev, true);
        loadThinkCaps();
        loadChats();  // Chats des aktuellen Modus
        break;
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
        if (!ev.routine) actGroup = startActGroup(ev.text || L("Frage", "Question"));
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

  // ---------------------------------------------------------------- Chat
  const chat = $("chat");
  const assistants = {};

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMarkdown(src) {
    const blocks = [];
    let text = src.replace(/```[\w-]*\n?([\s\S]*?)(```|$)/g, (_, code) => {
      blocks.push(`<div class="code"><button class="copy" type="button" title="${L("Kopieren", "Copy")}">${ico("copy")}</button>`
        + `<pre>${escapeHtml(code.replace(/\n$/, ""))}</pre></div>`);
      return `\u0000${blocks.length - 1}\u0000`;
    });
    // Inline-Code und Links vorab als Platzhalter – darin wird nichts formatiert und kein „|“ trennt Tabellenzellen
    const inl = [];
    const keep = (html) => { inl.push(html); return `\u0002${inl.length - 1}\u0002`; };
    text = escapeHtml(text)
      .replace(/\\\|/g, "\u0001")
      .replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`))
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, t, u) => keep(`<a href="${u}" target="_blank" rel="noopener">${t}</a>`))
      .replace(/(^|[\s(])(https?:\/\/[^\s<)|]+)/g, (_, pre, u) => pre + keep(`<a href="${u}" target="_blank" rel="noopener">${u}</a>`))
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_(?!\s)([^_\n]+?)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    // Tabellen (GitHub-Markdown): Kopfzeile, direkt darunter die Trennzeile |---|:--:|
    const SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
    const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const table = (rows) => {
      const head = cells(rows[0]);
      const align = cells(rows[1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
      const td = (tag, c, i) => `<${tag}${align[i] ? ` style="text-align:${align[i]}"` : ""}>${c}</${tag}>`;
      const fit = (r) => head.map((_, i) => r[i] ?? "");
      const body = rows.slice(2).map((r) => `<tr>${fit(cells(r)).map((c, i) => td("td", c, i)).join("")}</tr>`).join("");
      return `<div class="md-table"><table><thead><tr>${head.map((c, i) => td("th", c, i)).join("")}</tr></thead>`
        + `<tbody>${body}</tbody></table></div>`;
    };
    const html = text.split(/\n{2,}/).map((para) => {
      if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
      // Zeilenweise: Überschriften (## …), Listen (- … / 1. …), Tabellen, Zitate, Trennlinien und normaler Text
      const out = [];
      let list = null, text = [], quote = [];
      const flushText = () => { if (text.length) out.push(`<p>${text.join("<br>")}</p>`); text = []; };
      const flushQuote = () => { if (quote.length) out.push(`<blockquote>${quote.join("<br>")}</blockquote>`); quote = []; };
      // nummerierte Listen behalten ihre Nummer – auch wenn Leerzeilen sie in mehrere Absätze teilen („1. … 2. …“)
      const flushList = () => {
        if (list) out.push(`<${list.tag}${list.start > 1 ? ` start="${list.start}"` : ""}>${list.items.join("")}</${list.tag}>`);
        list = null;
      };
      const flushAll = () => { flushText(); flushList(); flushQuote(); };
      const lines = para.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.includes("|") && i + 1 < lines.length && SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
          let j = i + 2;
          while (j < lines.length && lines[j].includes("|") && lines[j].trim()) j++;
          flushAll();
          out.push(table(lines.slice(i, j)));
          i = j - 1;
          continue;
        }
        const h = /^\s*#{1,4}\s+(.+)$/.exec(l);
        const li = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(l);
        const q = /^\s*&gt;\s?(.*)$/.exec(l);
        if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { flushAll(); out.push("<hr>"); }
        else if (h) { flushAll(); out.push(`<div class="md-h">${h[1]}</div>`); }
        else if (q) { flushText(); flushList(); quote.push(q[1]); }
        else if (li) {
          const tag = /\d/.test(li[1]) ? "ol" : "ul";
          flushText(); flushQuote();
          if (list && list.tag !== tag) flushList();
          list = list || { tag, items: [], start: tag === "ol" ? parseInt(li[1], 10) : 1 };
          list.items.push(`<li>${li[2]}</li>`);
        } else if (l.trim()) { flushList(); flushQuote(); text.push(l); }
      }
      flushAll();
      return out.join("");
    }).join("");
    return html.replace(/\u0002(\d+)\u0002/g, (_, i) => inl[+i]).replace(/\u0001/g, "|")
      .replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  // Mitlaufen beim Generieren: unten bleiben, solange der Nutzer nicht selbst hochscrollt. Jede Änderung im Chat
  // (Text, Gedankenblock, Werkzeug-Zeilen, Bilder) zieht nach – nicht nur die Stellen, die selbst scrollen.
  let follow = true, userScrollAt = 0, pinQueued = false;
  const atBottom = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
  // sofort, nicht „smooth“ (CSS): eine Animation je Token hinkt beim Streamen hinterher und bricht ab
  const toBottom = () => chat.scrollTo({ top: chat.scrollHeight, behavior: "instant" });
  function scrollChat() { follow = true; toBottom(); }
  function pinChat() {
    if (!follow || pinQueued) return;
    pinQueued = true;
    requestAnimationFrame(() => { pinQueued = false; if (follow) toBottom(); });
  }
  for (const ev of ["wheel", "touchmove", "keydown", "pointerdown"]) {
    chat.addEventListener(ev, () => { userScrollAt = performance.now(); }, { passive: true });
  }
  // nur eine Bewegung des Nutzers entscheidet (eigenes Nachziehen oder Scroll-Anker des Browsers zählen nicht)
  chat.addEventListener("scroll", () => { if (performance.now() - userScrollAt < 1000) follow = atBottom(); }, { passive: true });
  new MutationObserver(pinChat).observe(chat, { childList: true, subtree: true, characterData: true,
                                                attributes: true, attributeFilter: ["open", "class"] });
  chat.addEventListener("load", pinChat, true);           // Bilder (Screenshots, Anhänge) ändern die Höhe später
  chat.addEventListener("transitionend", pinChat, true);  // aufklappende Blöcke

  // Kopieren-Knopf an Code-Blöcken (Ereignis-Delegation, auch für später gerenderte Nachrichten)
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".code .copy");
    if (!btn) return;
    const text = btn.parentElement.querySelector("pre").textContent;
    try { await navigator.clipboard.writeText(text); } catch {
      const r = document.createRange(); r.selectNodeContents(btn.parentElement.querySelector("pre"));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); document.execCommand("copy"); sel.removeAllRanges();
    }
    btn.innerHTML = ico("check"); btn.classList.add("done");
    setTimeout(() => { btn.innerHTML = ico("copy"); btn.classList.remove("done"); }, 1200);
  });

  // Ecken eines Panels kurz aufleuchten lassen (neue Nachricht / neue Aktivität)
  function addMsg(cls, who, html) {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.innerHTML = `<div class="who">${who}</div><div class="tools"></div><div class="body">${html}</div>`;
    chat.appendChild(el);
    scrollChat();
    return el;
  }

  function imageBox(names, cls = "") {
    const box = document.createElement("div");
    box.className = "msg-images" + (cls ? " " + cls : "");
    for (const name of names) {
      const img = document.createElement("img");
      img.src = `/api/attachments/${encodeURIComponent(name)}`;
      img.alt = "";
      img.loading = "lazy";
      img.onclick = () => window.open(img.src, "_blank");
      box.appendChild(img);
    }
    return box;
  }
  // Screenshots des Agenten: unter dem Antworttext (nicht in .body – die wird beim Streamen neu gerendert)
  function addShots(el, names) {
    let box = el.querySelector(".msg-shots");
    if (!box) { box = imageBox([], "msg-shots"); el.querySelector(".body").after(box); }
    for (const img of [...imageBox(names).children]) box.appendChild(img);
  }
  function addUser(text, source, images, turn, editable, files) {
    const el = addMsg("user", source === "voice" ? L("Du · Sprache", "You · voice") : L("Du", "You"), escapeHtml(text));
    el._raw = text;
    if (turn) el.dataset.turn = turn;
    userActions(el, editable !== false && !!turn);
    if (images && images.length) el.querySelector(".body").prepend(imageBox(images));
    if (files && files.length) {
      const box = document.createElement("div");
      box.className = "msg-files";
      for (const d of files) {
        box.appendChild(fileChip(d.title, d, () => window.open(`/api/attachments/${encodeURIComponent(d.name)}`, "_blank", "noopener")));
      }
      el.querySelector(".body").prepend(box);
    }
  }
  // Ziel-Modus (/goal): Chip über der Eingabe, solange ein Ziel läuft; Meldungen bei Ende/Grenze/Rückfrage
  function renderGoal() {
    const chip = $("goal-chip");
    chip.classList.toggle("hidden", !S.goal);
    if (!S.goal) return;
    chip.querySelector(".goal-text").textContent = L("Ziel: ", "Goal: ") + S.goal.text;
    chip.querySelector(".goal-steps").textContent = S.goal.steps ? L(` · Schritt ${S.goal.steps}`, ` · step ${S.goal.steps}`) : "";
    chip.title = S.goal.text;
  }
  $("goal-chip").querySelector(".goal-stop").onclick = () => send({ type: "user_message", text: "/goal stop" });
  function goalEvent(ev) {
    const text = {
      started: L(`Ziel-Modus: ${ev.text}`, `Goal mode: ${ev.text}`),
      done: L(`Ziel erreicht (${ev.steps} Schritte).`, `Goal reached (${ev.steps} steps).`),
      question: L("Ziel-Modus wartet auf deine Antwort.", "Goal mode is waiting for your answer."),
      stopped: L("Ziel-Modus beendet.", "Goal mode ended."),
      limit: L("Ziel-Modus angehalten (Grenze) – „/goal weiter“ setzt fort.", "Goal mode paused (limit) – “/goal continue” carries on."),
      active: L(`Aktives Ziel: ${ev.text}`, `Active goal: ${ev.text}`),
    }[ev.state];
    S.goal = ["done", "stopped"].includes(ev.state) ? null : { text: ev.text, steps: ev.steps || 0 };
    renderGoal();
    if (ev.state !== "progress" && text) toast(text);
    if (ev.state === "progress" && S.currentMsg && assistants[S.currentMsg]) {
      const n = document.createElement("div");
      n.className = "goal-nudge";
      n.textContent = L("↻ weiter am Ziel", "↻ continuing with the goal");
      assistants[S.currentMsg].el.querySelector(".body").after(n);
    }
  }
  function addSystem(text) {
    const el = document.createElement("div");
    el.className = "msg system";
    el.textContent = text;
    chat.appendChild(el);
    scrollChat();
  }
  function addError(text) { addMsg("assistant error", "System", escapeHtml(text)); }

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
    const el = addMsg("assistant streaming" + (plan ? " plan" : ""), S.name, "");
    el.dataset.msg = id;
    if (S.turns) el.dataset.turn = S.turns;
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
  const GOAL_MARK = /\s*\[(ZIEL ERREICHT|GOAL REACHED|FRAGE|QUESTION)\]\s*$/i;  // Ziel-Modus-Marker nicht anzeigen
  function renderAssistant(a) {
    // nur Leerraum: leer lassen (der Plankasten erscheint erst mit dem ersten Wort)
    const raw = a.raw.replace(GOAL_MARK, "");
    a.el.querySelector(".body").innerHTML = raw.trim() ? renderMarkdown(raw.replace(/\n{3,}/g, "\n\n")) : "";
  }
  function flushTokens() {
    renderQueued = false;
    if (!dirty.size) return;
    for (const a of dirty) renderAssistant(a);
    dirty.clear();
    pinChat();
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
  // Denkmodus: Gedankengang läuft direkt in der Antwort mit (aufgeklappt) und klappt zu, sobald das Modell etwas
  // ausgibt (Antwort-Token oder Werkzeugaufruf). Denkt es nach einem Werkzeug weiter, geht derselbe Block wieder auf.
  const Thought = {
    byMsg: {}, warned: false,
    block(id) {
      let t = this.byMsg[id];
      if (t) return t;
      const a = assistants[id] || (startAssistant(id), assistants[id]);
      const el = document.createElement("details");
      el.className = "thought-log";
      el.innerHTML = "<summary></summary><pre></pre>";
      a.el.insertBefore(el, a.el.querySelector(".tools") || a.el.querySelector(".body"));  // zuerst gedacht, dann gehandelt
      t = this.byMsg[id] = { el, pre: el.querySelector("pre"), text: "", ms: 0, since: 0, queued: false, own: false, auto: false, msg: a };
      // Hat der Nutzer selbst auf- oder zugeklappt, bleibt es so
      el.addEventListener("toggle", () => { if (t.auto) t.auto = false; else t.own = true; });
      return t;
    },
    setOpen(t, open) {
      if (t.own || t.el.open === open) return;
      t.auto = true;
      t.el.open = open;
    },
    summary(t) {
      const secs = Math.max(1, Math.round(t.ms / 1000));
      t.el.querySelector("summary").textContent = t.since ? L("Denkt nach …", "Thinking …")
        : L(`Gedankengang · ${secs} s`, `Thoughts · ${secs} s`);
    },
    add(id, text) {
      if (!text) return;
      const t = this.block(id);
      if (!t.since) {  // (wieder) am Denken
        // solange gedacht wird, steht der Block ganz unten – nach Text oder Werkzeug sonst oben außer Sicht
        t.msg.el.insertBefore(t.el, t.msg.live && t.msg.live.parentNode === t.msg.el ? t.msg.live : null);
        t.since = performance.now();
        t.el.classList.add("live");
        this.summary(t);
        this.setOpen(t, true);
      }
      t.text += text;
      if (t.queued) return;
      t.queued = true;
      let done = false;
      const go = () => {
        if (done) return;
        done = true;
        t.queued = false;
        t.pre.textContent = t.text.trim();
        t.pre.scrollTop = t.pre.scrollHeight;
        pinChat();
      };
      requestAnimationFrame(go);
      setTimeout(go, 120);
    },
    collapse(id) {
      const t = this.byMsg[id];
      if (!t || !t.since) return;
      t.ms += performance.now() - t.since;
      t.since = 0;
      t.pre.textContent = t.text.trim();
      t.el.classList.remove("live");
      this.summary(t);
      this.setOpen(t, false);
      t.msg.el.insertBefore(t.el, t.msg.el.querySelector(".tools"));  // zugeklappt wieder oben: zuerst gedacht, dann gehandelt
    },
    finish(id) {
      const t = this.byMsg[id];
      this.collapse(id);
      delete this.byMsg[id];
      if (!t && S.think !== "off" && !this.warned) {
        this.warned = true;
        toast(L("Denkmodus an, aber das Modell hat keinen Gedankengang geliefert – ggf. '--reasoning-budget 0' aus dem Startbefehl entfernen.",
              "Thinking mode is on, but the model returned no reasoning – for llama-server remove '--reasoning-budget 0' from the start command."));
      }
    },
  };

  function finishAssistant(id, cancelled) {
    const a = assistants[id];
    if (!a) return;
    if (dirty.has(a)) { dirty.delete(a); renderAssistant(a); pinChat(); }
    a.el.classList.remove("streaming");
    a.live.remove();
    a.el._raw = a.raw.trim();
    assistantActions(a.el);
    if (a.steps.length) ensureActions(a.el).appendChild(statsLine(a, cancelled));
    if (!a.raw.trim()) {
      a.el.querySelector(".body").innerHTML = cancelled ? "<em>(abgebrochen)</em>" : "";
      if (!cancelled && !a.el.querySelector(".tool-chip")) a.el.remove();
    }
    delete assistants[id];
    markLast();
  }

  // ---------------------------------------------------------------- Aktionen an Nachrichten
  // Kopieren, Bearbeiten (eigene Nachricht), Neu generieren (letzte Antwort), 👍/👎, Statistik – erscheinen beim
  // Darüberfahren bzw. an der letzten Antwort; am Handy immer (dezent).
  function ensureActions(el) {
    let bar = el.querySelector(":scope > .msg-actions");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "msg-actions";
      el.appendChild(bar);
    }
    return bar;
  }
  function actBtn(icon, title, onclick, cls = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ma-btn " + cls;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.innerHTML = `<svg class="i"><use href="#i-${icon}"/></svg>`;
    b.onclick = (e) => { e.stopPropagation(); onclick(b); };
    return b;
  }
  async function copyText(text, btn) {
    try { await navigator.clipboard.writeText(text); } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    if (btn) { btn.classList.add("ok"); setTimeout(() => btn.classList.remove("ok"), 1200); }
    toast(L("Kopiert.", "Copied."));
  }
  const isBusy = () => !!S.serverState && S.serverState !== "idle";
  function userActions(el, editable) {
    const bar = ensureActions(el);
    bar.appendChild(actBtn("copy", L("Kopieren", "Copy"), (b) => copyText(el._raw || "", b)));
    if (editable) bar.appendChild(actBtn("edit", L("Bearbeiten und neu senden", "Edit and resend"), () => editUser(el)));
  }
  function assistantActions(el) {
    const bar = ensureActions(el);
    if (bar.querySelector(".ma-copy")) return;
    bar.prepend(actBtn("copy", L("Antwort kopieren (Markdown)", "Copy answer (Markdown)"), (b) => copyText(el._raw || "", b), "ma-copy"));
    if (el.dataset.turn && !el.classList.contains("locked")) {
      bar.insertBefore(actBtn("redo", L("Neu generieren", "Regenerate"), () => rewind(Number(el.dataset.turn)), "ma-regen"),
                       bar.querySelector(".ma-copy").nextSibling);
    }
  }
  // „Letzte Antwort“: dort bleiben die Aktionen sichtbar und nur dort gibt es „Neu generieren“
  function markLast() {
    chat.querySelectorAll(".msg.last").forEach((x) => x.classList.remove("last"));
    const msgs = chat.querySelectorAll(".msg.assistant:not(.streaming):not(.error)");
    if (msgs.length) msgs[msgs.length - 1].classList.add("last");
  }
  async function rewind(turn, text) {
    if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return false; }
    try {
      await api("POST", "/api/chats/active/rewind", text == null ? { turn } : { turn, text });
      return true;
    } catch { return false; }  // Meldung kommt von api()
  }
  function editUser(el) {
    if (el.querySelector(".msg-edit")) return;
    if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
    const turn = Number(el.dataset.turn);
    const body = el.querySelector(".body");
    const form = document.createElement("form");
    form.className = "msg-edit";
    form.innerHTML = `<textarea rows="3"></textarea><div class="me-btns"><span class="me-hint"></span>`
      + `<button type="button" class="btn small ghost me-cancel"></button><button type="submit" class="btn small primary"></button></div>`;
    const ta = form.querySelector("textarea");
    ta.value = el._raw || "";
    form.querySelector(".me-cancel").textContent = L("Abbrechen", "Cancel");
    form.querySelector("[type=submit]").textContent = L("Senden", "Send");
    const later = (S.turns || turn) - turn;
    form.querySelector(".me-hint").textContent = later > 0
      ? L(`Die ${later + 1} Antworten ab hier werden ersetzt.`, `The ${later + 1} answers from here are replaced.`)
      : L("Enter sendet · Esc bricht ab", "Enter sends · Esc cancels");
    const close = () => { form.remove(); body.hidden = false; el.classList.remove("editing"); };
    form.querySelector(".me-cancel").onclick = close;
    ta.onkeydown = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const text = ta.value.trim();
      if (!text) return;
      if (later > 0 && !await ask(L(`Ab dieser Nachricht neu beginnen? Die ${later + 1} Antworten danach werden ersetzt.`,
                                  `Restart from this message? The ${later + 1} answers after it are replaced.`))) return;
      if (await rewind(turn, text)) close();
    };
    body.hidden = true;
    el.classList.add("editing");
    el.insertBefore(form, el.querySelector(".msg-actions"));
    ta.style.height = Math.min(320, Math.max(72, body.scrollHeight + 24)) + "px";
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  function cutChat(turn) {
    if (!turn) return;
    const first = [...chat.children].find((el) => Number(el.dataset.turn) >= turn);
    if (first) {
      let el = first;
      while (el) { const next = el.nextElementSibling; el.remove(); el = next; }
    }
    S.turns = turn - 1;  // die neu gestellte Frage bekommt gleich wieder diese Nummer
    markLast();
  }

  // ---------------------------------------------------------------- Planmodus
  const PLAN_OUTCOME = {
    accepted: L("wird ausgeführt", "being carried out"), revised: L("wird überarbeitet", "being revised"),
    discarded: L("verworfen", "discarded"), replaced: L("ersetzt", "replaced"),
  };
  function showPlan(ev) {
    const el = chat.querySelector(`.msg[data-msg="${ev.id}"]`);
    if (!el || el.querySelector(".plan-bar")) return;
    const bar = document.createElement("div");
    bar.className = "plan-bar";
    bar.innerHTML = `<div class="plan-actions">
        <button class="btn plan-run">${ico("play")}${L("Ausführen", "Run")}</button>
        <button class="btn plan-edit">${ico("edit")}${L("Ändern", "Change")}</button>
        <button class="btn plan-drop">${ico("x")}${L("Verwerfen", "Discard")}</button></div>
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
  // Je Frage (bzw. Routine/Telegram-Lauf) eine Gruppe, darin die Schritte in Reihenfolge – lesbare Namen mit Icon,
  // Status und Dauer rechts, Argumente und Ausgabe zum Aufklappen
  const activity = $("activity");
  const acts = {};
  let actGroup = null;            // Gruppe der aktuellen Frage im Dashboard
  const runGroups = {};           // Routine/Telegram → {el, last}
  const MAX_GROUPS = 20;

  const TOOL_LABELS = {
    web_search: ["Websuche", "Web search"], fetch_url: ["Webseite lesen", "Read web page"],
    code_review: ["Code-Check", "Code check"],
    open_website: ["Webseite öffnen", "Open website"], run_shell: ["Befehl", "Command"], ssh_run: ["Befehl per SSH", "SSH command"],
    read_file: ["Datei lesen", "Read file"], write_file: ["Datei schreiben", "Write file"], edit_file: ["Datei ändern", "Edit file"],
    list_directory: ["Ordner anzeigen", "List folder"], find_files: ["Dateien suchen", "Find files"],
    search_file_contents: ["In Dateien suchen", "Search in files"], search_nas: ["NAS durchsuchen", "Search NAS"],
    save_output: ["Ausgabe speichern", "Save output"], system_update: ["Systemupdate", "System update"],
    system_info: ["Systeminfo", "System info"], remember: ["Merken", "Remember"], recall: ["Erinnern", "Recall"],
    forget: ["Vergessen", "Forget"], set_reminder: ["Erinnerung stellen", "Set reminder"],
    list_reminders: ["Erinnerungen", "Reminders"], look_at_screen: ["Bildschirm ansehen", "Look at screen"],
    look_at_image: ["Bild ansehen", "Look at image"], generate_image: ["Bild erzeugen", "Generate image"],
    todo_write: ["Aufgabenliste", "Task list"], load_tools: ["Werkzeuge laden", "Load tools"],
    daily_briefing: ["Briefing", "Briefing"], weather: ["Wetter", "Weather"], open_app: ["Programm öffnen", "Open app"],
    list_installed_apps: ["Programme auflisten", "List apps"],
    // Coding-Modus (nach OpenCode)
    read: ["Lesen", "Read"], edit: ["Bearbeiten", "Edit"], write: ["Schreiben", "Write"], grep: ["Durchsuchen", "Grep"],
    glob: ["Dateien finden", "Glob"], shell: ["Befehl", "Command"], job_output: ["Job-Ausgabe", "Job output"],
    websearch: ["Websuche", "Web search"], webfetch: ["Webseite lesen", "Fetch page"],
    subagent: ["Unteragent", "Subagent"], question: ["Rückfrage", "Question"], screenshot: ["Screenshot", "Screenshot"],
  };
  const TOOL_PREFIX = [[/^mail_/, ["Mail", "Mail"]], [/^paperless_/, ["Paperless", "Paperless"]], [/^ha_/, ["Smart Home", "Smart home"]],
    [/^calendar_/, ["Kalender", "Calendar"]], [/^obsidian_/, ["Obsidian", "Obsidian"]], [/^trilium_/, ["Trilium", "Trilium"]],
    [/^portainer_/, ["Portainer", "Portainer"]], [/^routine_/, ["Routine", "Routine"]], [/^package_|_package/, ["Pakete", "Packages"]]];
  function toolLabel(name) {
    const hit = TOOL_LABELS[name];
    if (hit) return hit[EN ? 1 : 0];
    for (const [rx, l] of TOOL_PREFIX) {
      if (rx.test(name)) return `${l[EN ? 1 : 0]} · ${name.replace(rx, "").replace(/_/g, " ")}`.replace(/ · $/, "");
    }
    const t = name.replace(/_/g, " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }
  const ACT_ICONS = { shell: "code", ssh: "code", packages: "code", sysadmin: "code", system: "code", power: "power",
    portainer: "code", web: "search", files: "file", apps: "bolt", memory_tools: "brain", calendar_tools: "calendar",
    reminder_tools: "bell", mail: "chat", telegram_tools: "chat", vision: "image", images: "image", paperless: "file" };
  const CODE_ICONS = { read: "file", edit: "file", write: "file", grep: "search", glob: "search", shell: "code",
    job_output: "code", websearch: "search", webfetch: "search", subagent: "brain", question: "chat", screenshot: "image" };
  const toolIcon = (ev) => (ev.group === "code" ? CODE_ICONS[ev.name] : ACT_ICONS[ev.group]) || "bolt";
  // das aussagekräftigste Argument als Kurzinfo hinter dem Namen
  function keyArg(args) {
    for (const k of ["command", "query", "path", "url", "pattern", "description", "text", "name", "topic", "title", "q", "step"]) {
      const v = args && args[k];
      if (typeof v === "string" && v.trim()) {
        const one = v.replace(/\s+/g, " ").trim();
        return one.length > 90 ? one.slice(0, 89) + "…" : one;
      }
    }
    return "";
  }

  // erscheint erst mit dem ersten Schritt (CSS :has) – Fragen ohne Werkzeuge lassen keinen leeren Kopf zurück
  function startActGroup(title, kind = "user") {
    const g = document.createElement("section");
    g.className = "act-group " + kind;
    g.innerHTML = `<div class="act-group-head"><span class="agh-title"></span><span class="agh-time"></span></div><div class="act-steps"></div>`;
    const t = g.querySelector(".agh-title");
    t.textContent = title;
    t.title = title;
    g.querySelector(".agh-time").textContent = new Date().toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    activity.prepend(g);
    const groups = activity.querySelectorAll(".act-group");
    for (let i = MAX_GROUPS; i < groups.length; i++) groups[i].remove();
    return g;
  }

  // Wohin gehört ein Schritt? Routinen/Telegram in ihre eigene Gruppe (ein Lauf = solange Schritte kommen)
  function stepsFor(routine) {
    if (routine) {
      const r = runGroups[routine];
      const fresh = r && r.el.isConnected && Date.now() - r.last < 120000;
      const el = fresh ? r.el : startActGroup(routine === "Telegram" ? "Telegram" : L(`Routine · ${routine}`, `Routine · ${routine}`), "run");
      runGroups[routine] = { el, last: Date.now() };
      return el.querySelector(".act-steps");
    }
    if (!actGroup || !actGroup.isConnected) actGroup = startActGroup(L("Hintergrund", "Background"), "bg");
    return actGroup.querySelector(".act-steps");
  }

  function actStep(kind, icon, name, info, routine, parent) {
    const empty = activity.querySelector(".empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = `act ${kind} running`;
    el.innerHTML = `<button type="button" class="act-row"><span class="act-ico">${ico(icon)}</span>
      <span class="act-main"><span class="act-name"></span><span class="act-info"></span></span>
      <span class="act-state"><span class="act-status"></span><span class="act-mark"></span></span></button>
      <div class="act-bar"><i></i></div><div class="act-details"><div class="act-args"></div><pre class="act-out"></pre></div>`;
    el.querySelector(".act-name").textContent = name;
    el.querySelector(".act-info").textContent = info;
    el.querySelector(".act-row").onclick = () => el.classList.toggle("open");
    el._start = performance.now();
    if (parent) {  // Schritt eines Unteragenten: eingerückt unter seinem Aufruf
      let subs = parent.querySelector(":scope > .act-subs");
      if (!subs) { subs = document.createElement("div"); subs.className = "act-subs"; parent.insertBefore(subs, parent.querySelector(".act-details")); }
      subs.appendChild(el);
    } else stepsFor(routine).prepend(el);  // neueste Aktivität oben – nicht suchen müssen
    if (!phaseTimer) phaseTimer = setInterval(tickPhases, 1000);
    return el;
  }

  function finishStep(el, status, seconds) {
    el.classList.remove("running");
    el.classList.add(status);
    const sec = seconds ?? (performance.now() - el._start) / 1000;
    el.querySelector(".act-status").textContent = status !== "ok" ? STATUS_TEXT[status] || status
      : sec < 0.1 ? `< ${secs(0.1)}` : secs(sec);
    el.querySelector(".act-mark").innerHTML = status === "ok" ? ico("check") : ico("x");
  }

  // Vorlesen des Chats (Prompt-Cache vorwärmen): keine eigene Zeile, nur eine Fußnote über dem Verlauf
  function prewarmNote(ev) {
    let note = $("act-note");
    if (!note) {
      note = document.createElement("div");
      note.id = "act-note";
      note.className = "act-note";
      activity.before(note);
    }
    note.textContent = ev.phase === "done" ? phaseSummary(ev) : phaseText(ev);
    note.classList.toggle("on", ev.phase !== "done");
  }

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

  // Warum der Server viel neu einlesen musste (reread.py) – nur erwähnenswert, wenn es wirklich viel war
  const REREAD = {
    first: () => L("erster Aufruf seit dem Start", "first request since start"),
    model: () => L("anderes Modell", "different model"),
    chat: () => L("anderer Chat", "different chat"),
    compact: () => L("nach dem Zusammenfassen", "after summarising"),
    think: () => L("Denkmodus gewechselt (die Vorlage baut den Prompt anders)", "thinking mode changed (the template builds a different prompt)"),
    tools: (r) => L("Werkzeugliste geändert", "tool list changed") + (r.added && r.added.length ? ` (+${r.added.slice(0, 4).join(", ")}${r.added.length > 4 ? " …" : ""})` : ""),
    system: () => L("Systemteil geändert", "system part changed"),
    cleared: () => L("alte Ergebnisse ausgeblendet", "old results hidden"),
    edited: () => L("frühere Nachricht geändert", "earlier message changed"),
    server: () => L("der Server hatte den Anfang nicht mehr (Neustart, Bild-Modus oder Hybrid-Modell ohne Checkpoint)",
                    "the server no longer had the beginning (restart, image mode or hybrid model without checkpoint)"),
    api: () => L("ein anderes Programm hat das Modell über die API benutzt", "another program used the model via the API"),
  };
  function rereadText(r) {
    if (!r || r.reason === "appended" || !r.new || r.new < 5000 || !REREAD[r.reason]) return "";
    return L(`${num(r.new)} neu eingelesen – Grund: `, `${num(r.new)} re-read – reason: `) + REREAD[r.reason](r);
  }

  function phaseSummary(ev) {
    if (ev.compress) return L(`Chat zusammengefasst · ${num(ev.before)} → ${num(ev.after)} Token · Zusammenfassung ${num(ev.tokens)} Token`,
                              `chat summarised · ${num(ev.before)} → ${num(ev.after)} tokens · summary ${num(ev.tokens)} tokens`);
    if (ev.prewarm) return ev.error ? L("Vorlesen übersprungen", "pre-read skipped")
      : ev.new != null && ev.new < ev.tokens
        ? L(`Chat vorgelesen · ${num(ev.tokens)} Token, davon ${num(ev.tokens - ev.new)} aus dem Cache · ${num(ev.new)} neu in ${secs(ev.seconds)}`,
            `chat pre-read · ${num(ev.tokens)} tokens, ${num(ev.tokens - ev.new)} from the cache · ${num(ev.new)} new in ${secs(ev.seconds)}`)
        : L(`Chat vorgelesen · ${num(ev.tokens)} Token in ${secs(ev.seconds)} – die nächste Frage liest nur noch Neues`,
            `chat pre-read · ${num(ev.tokens)} tokens in ${secs(ev.seconds)} – the next question only reads what is new`)
        + (rereadText(ev.reread) ? ` · ${rereadText(ev.reread)}` : "");
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
    if (rereadText(ev.reread)) parts.push(rereadText(ev.reread));
    return parts.join(" · ") || L("fertig", "done");
  }

  function tickPhases() {
    const open = [...activity.querySelectorAll(".act.running:not(.waiting)"), ...chat.querySelectorAll(".live.on")];
    open.forEach((el) => {
      const s = Math.floor((performance.now() - el._start) / 1000);
      const t = el.querySelector(".act-status, .ls");
      if (t && s >= 1) t.textContent = secs(s);
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
    pinChat();
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
    const worst = steps.filter((s) => rereadText(s.reread)).sort((x, y) => y.reread.new - x.reread.new)[0];
    if (worst) parts.push(rereadText(worst.reread));
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
    if (ev.phase === "prewarm" || ev.prewarm) {  // Chat im Hintergrund einlesen: Aktivität und Orb zeigen es
      prewarmNote(ev);
      S.prewarm = ev.phase === "done" ? null : { tokens: ev.tokens, eta: ev.eta_s, at: Date.now() };
      refresh();
      return;
    }
    let el = acts["m-" + ev.id];
    if (!el) {
      if (ev.phase === "done") return;
      el = actStep("model", "brain", L("Modell", "Model"), "", ev.routine);
      acts["m-" + ev.id] = el;
    }
    const bar = el.querySelector(".act-bar i");
    if (ev.phase === "done") {
      el.querySelector(".act-info").textContent = phaseSummary(ev);
      finishStep(el, ev.error ? "error" : "ok", ev.seconds);
      delete acts["m-" + ev.id];
      return;
    }
    const text = phaseText(ev);
    el.querySelector(".act-info").textContent = text;
    el.dataset.phase = ev.phase;
    setBar(bar, ev);
    if (ev.phase === "compress" && Say.ctx !== "compress") { sayNext("compress"); refresh(); }
  }

  // Kurzname für den Werkzeug-Satelliten am Orb
  const SAT_GROUPS = [
    [/^(read|edit|write|grep|glob)$/, ["Dateien", "Files"]], [/^(shell|job_output)$/, ["Shell", "Shell"]],
    [/^(websearch|webfetch)$/, ["Web", "Web"]], [/^subagent$/, ["Unteragent", "Subagent"]],
    [/^question$/, ["Rückfrage", "Question"]], [/^screenshot$/, ["Screenshot", "Screenshot"]],
    [/^look_at_/, ["Sehen", "Vision"]], [/^(web_search|fetch_url|open_website)$/, ["Web", "Web"]], [/^paperless_/, ["Paperless", "Paperless"]],
    [/^mail_/, ["Mail", "Mail"]], [/^obsidian_/, ["Obsidian", "Obsidian"]], [/^trilium_/, ["Trilium", "Trilium"]],
    [/^ha_/, ["Smart Home", "Smart Home"]], [/^calendar_/, ["Kalender", "Calendar"]], [/^run_shell$/, ["Shell", "Shell"]],
    [/(package|system_update)/, ["Pakete", "Packages"]], [/(file|folder)/, ["Dateien", "Files"]],
    [/^weather/, ["Wetter", "Weather"]], [/^routine_/, ["Routinen", "Routines"]], [/reminder/, ["Erinnerung", "Reminder"]],
    [/(remember|recall|forget|memory)/, ["Gedächtnis", "Memory"]],
  ];
  function satLabel(name) {
    const hit = SAT_GROUPS.find(([rx]) => rx.test(name));
    return hit ? hit[1][EN ? 1 : 0] : name.split("_")[0].toUpperCase();
  }

  // Symbol je Aktionsart (weitere Symbole: ICONS in orb.js); Gedächtnis leuchtet im Kern statt als Satellit
  const ICON_GROUPS = { code: "cli", shell: "cli", packages: "cli", sysadmin: "cli", system: "cli", power: "cli", web: "cloud",
                        mail: "mail", paperless: "paperless", vision: "eye" };

  function toolCall(ev) {
    activityArrived();
    if (ev.sub) {  // Schritt eines Unteragenten – nur in der Aktivität, eingerückt
      const parent = acts[ev.sub];
      const el = actStep("tool", toolIcon(ev), toolLabel(ev.name), keyArg(ev.args), ev.routine, parent);
      el.querySelector(".act-args").textContent = `${ev.name}  ${fmtArgs(ev.name, ev.args)}`;
      acts[ev.id] = el;
      return;
    }
    if (ev.preview) S.previews[ev.id] = ev.preview;
    if (!ev.routine) { sayNext(SAYINGS[ev.group] ? ev.group : "think"); refresh(); }
    if (ev.group === "memory_tools") orb.memoryGlow(ev.id, true, satLabel(ev.name));
    else orb.addSatellite(ev.id, satLabel(ev.name), ICON_GROUPS[ev.group] || null);
    const waiting = ev.risk === "confirm";
    const el = actStep("tool", toolIcon(ev), toolLabel(ev.name), keyArg(ev.args), ev.routine);
    el.querySelector(".act-name").title = ev.name;
    if (S.sandbox && ev.name === "shell") el.querySelector(".act-name").textContent += L(" · Sandbox", " · sandbox");
    else if (ev.jailed) el.querySelector(".act-name").textContent += L(" · isoliert", " · isolated");
    el.querySelector(".act-args").textContent = `${ev.name}  ${fmtArgs(ev.name, ev.args)}`;
    if (ev.preview) {
      const d = document.createElement("pre");
      d.className = "diff act-diff";
      d.innerHTML = renderDiff(ev.preview);
      el.querySelector(".act-details").insertBefore(d, el.querySelector(".act-out"));
    }
    if (waiting) {
      el.classList.add("waiting");
      el.querySelector(".act-status").textContent = STATUS_TEXT.waiting;
    }
    if (ev.risk === "blocked") el.querySelector(".act-status").textContent = STATUS_TEXT.blocked;
    acts[ev.id] = el;

    const a = !ev.routine && S.currentMsg && assistants[S.currentMsg];
    if (a) {  // über der Nachricht nur der aktuelle Aufruf – die früheren stehen in der Aktivität
      const tools = a.el.querySelector(".tools");
      a.toolCount = (a.toolCount || 0) + 1;
      tools.replaceChildren();
      const chip = document.createElement("span");
      chip.className = "tool-chip running";
      chip.id = "chip-" + ev.id;
      chip.textContent = ev.name;
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
    delete S.previews[ev.id];
    if (!ev.sub) {
      orb.removeSatellite(ev.id, ev.status === "error" || ev.status === "blocked");
      orb.memoryGlow(ev.id, false);
    }
    const el = acts[ev.id];
    if (el) {
      el.classList.remove("waiting");
      finishStep(el, ev.status);
      const out = el.querySelector(".act-out");
      if (!out.textContent.trim() || ev.status !== "ok") out.textContent = ev.text;
      delete acts[ev.id];
    }
    const chip = $("chip-" + ev.id);
    if (chip) chip.className = "tool-chip " + ev.status;
  }

  // ---------------------------------------------------------------- Bestätigung
  // Felder, die der Nutzer vor dem Bestätigen noch ändern darf (z. B. mail_send)
  const EDIT_FIELDS = {
    to: [L("AN", "TO"), "input"], cc: [L("CC", "CC"), "input"],
    subject: [L("Betreff", "Subject"), "input"], body: [L("Text", "Text"), "textarea"],
  };
  const APPROVE_HTML = $("confirm-yes").innerHTML;

  // ---------------------------------------------------------------- Coding: Diffs, Rückfragen, Änderungen, Jobs
  function renderDiff(text) {
    return String(text || "").split("\n").map((ln) => {
      const cls = ln.startsWith("+++") || ln.startsWith("---") ? "d-file" : ln.startsWith("@@") ? "d-hunk"
        : ln.startsWith("+") ? "d-add" : ln.startsWith("-") ? "d-del" : "";
      return `<span class="${cls}">${escapeHtml(ln) || " "}</span>`;
    }).join("");
  }

  // Werkzeug question: Karte im Gespräch – Optionen anklicken (bei multiple mehrere) oder frei antworten
  function openQuestion(ev) {
    closeQuestion();
    const wrap = document.createElement("section");
    wrap.className = "confirm-wrap question-wrap";
    wrap.setAttribute("role", "dialog");
    const card = document.createElement("div");
    card.className = "confirm-card question-card";
    const blocks = (ev.questions || []).map((q, qi) => {
      const box = document.createElement("div");
      box.className = "q-block";
      box.innerHTML = `<div class="q-head">${q.header ? `<span class="q-tag"></span>` : ""}<span class="q-text"></span></div>
        <div class="q-options"></div><input class="q-other" type="text">`;
      if (q.header) box.querySelector(".q-tag").textContent = q.header;
      box.querySelector(".q-text").textContent = q.question;
      const other = box.querySelector(".q-other");
      other.placeholder = L("Andere Antwort …", "Other answer …");
      const opts = box.querySelector(".q-options");
      (q.options || []).forEach((o, oi) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "q-opt";
        b.innerHTML = `<span class="q-label"></span>${o.description ? `<span class="q-desc"></span>` : ""}`;
        b.querySelector(".q-label").textContent = o.label;
        if (o.description) b.querySelector(".q-desc").textContent = o.description;
        b.onclick = () => {
          if (!q.multiple) opts.querySelectorAll(".q-opt").forEach((x) => x.classList.remove("on"));
          b.classList.toggle("on");
          if (!q.multiple && ev.questions.length === 1) answerQuestion(collect());
        };
        if (oi === 0 && qi === 0) setTimeout(() => b.focus({ preventScroll: true }), 50);
        opts.appendChild(b);
      });
      other.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); answerQuestion(collect()); } };
      return box;
    });
    const collect = () => blocks.map((box) => {
      const text = box.querySelector(".q-other").value.trim();
      if (text) return text;
      return [...box.querySelectorAll(".q-opt.on .q-label")].map((x) => x.textContent);
    });
    const title = document.createElement("div");
    title.className = "confirm-title";
    title.innerHTML = `<span class="warn-dot"></span><span></span>`;
    title.lastChild.textContent = L("Orbwise hat eine Rückfrage", "Orbwise has a question");
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.innerHTML = `<button type="button" class="btn ghost q-skip"></button><button type="button" class="btn approve q-send"></button>`;
    actions.querySelector(".q-skip").textContent = L("Überspringen", "Skip");
    actions.querySelector(".q-send").textContent = L("Antworten", "Answer");
    actions.querySelector(".q-skip").onclick = () => answerQuestion(null);
    actions.querySelector(".q-send").onclick = () => answerQuestion(collect());
    card.append(title, ...blocks, actions);
    wrap.appendChild(card);
    chat.appendChild(wrap);
    S.question = { id: ev.id, el: wrap };
    const act = acts[ev.id];
    if (act) { act.classList.add("waiting"); act.querySelector(".act-status").textContent = STATUS_TEXT.waiting; }
    scrollChat();
    refresh();
  }

  function answerQuestion(answers) {
    if (!S.question) return;
    const id = S.question.id;
    const empty = !answers || answers.every((a) => !a || !a.length);
    send({ type: "question_answer", id, answers: empty ? null : answers });
    closeQuestion(id);
  }

  function closeQuestion(id) {
    if (!S.question || (id && S.question.id !== id)) return;
    S.question.el.remove();
    S.question = null;
    const act = acts[id];
    if (act) { act.classList.remove("waiting"); act._start = performance.now(); }
    refresh();
  }

  // Coding: geänderte Dateien unter der Antwort – aufklappbar, mit „Rückgängig“ (Schnappschuss, siehe snapshot.py)
  function showChanges(msgEl, turn, files, undone) {
    if (!msgEl || !files || !files.length) return;
    let box = msgEl.querySelector(".msg-changes");
    if (!box) {
      box = document.createElement("div");
      box.className = "msg-changes";
      msgEl.insertBefore(box, msgEl.querySelector(".msg-actions"));
    }
    box.dataset.turn = turn;
    const add = files.reduce((n, f) => n + (f.add || 0), 0);
    const del = files.reduce((n, f) => n + (f.del || 0), 0);
    const n = files.length;
    box.innerHTML = `<div class="mc-head"><button type="button" class="mc-toggle">${ico("file")}<span class="mc-sum"></span>
      <span class="mc-add">+${add}</span><span class="mc-del">−${del}</span></button><button type="button" class="btn small ghost mc-undo"></button></div>
      <ul class="mc-files"></ul>`;
    box.querySelector(".mc-sum").textContent = n === 1 ? L("1 Datei geändert", "1 file changed") : L(`${n} Dateien geändert`, `${n} files changed`);
    const ul = box.querySelector(".mc-files");
    for (const f of files) {
      const li = document.createElement("li");
      const tag = f.status === "added" ? L("neu", "new") : f.status === "deleted" ? L("gelöscht", "deleted") : "";
      li.innerHTML = `<span class="mc-path"></span>${tag ? `<span class="mc-tag"></span>` : ""}<span class="mc-add">${f.add == null ? "" : "+" + f.add}</span><span class="mc-del">${f.del == null ? "" : "−" + f.del}</span>`;
      li.querySelector(".mc-path").textContent = f.path;
      if (tag) li.querySelector(".mc-tag").textContent = tag;
      ul.appendChild(li);
    }
    box.querySelector(".mc-toggle").onclick = () => box.classList.toggle("open");
    box.querySelector(".mc-undo").onclick = async () => {
      const redo = box.classList.contains("undone");
      if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
      if (!redo && !await ask(L(`Die Änderungen dieser Antwort an ${n === 1 ? "1 Datei" : n + " Dateien"} zurücksetzen? Spätere Änderungen an denselben Dateien gehen dabei verloren.`,
                              `Undo this answer's changes to ${n === 1 ? "1 file" : n + " files"}? Later changes to the same files are lost.`))) return;
      try {
        const r = await api("POST", "/api/chats/active/revert", { turn: Number(turn), redo });
        setChangesUndone(box, r.undone);
        toast(redo ? L("Änderungen wiederhergestellt.", "Changes restored.") : L("Änderungen zurückgesetzt.", "Changes undone."));
      } catch { /* Meldung kommt von api() */ }
    };
    setChangesUndone(box, undone);
  }

  function setChangesUndone(box, undone) {
    box.classList.toggle("undone", !!undone);
    const b = box.querySelector(".mc-undo");
    b.innerHTML = undone ? `${ico("redo")}${L("Wiederherstellen", "Restore")}` : `${ico("undo")}${L("Rückgängig", "Undo")}`;
  }

  // Hintergrund-Befehle (shell mit background=true): eigener Schritt mit Live-Ausgabe und „Stoppen“
  function jobEvent(ev) {
    const key = "job-" + ev.id;
    let el = acts[key];
    if (ev.phase === "start") {
      el = actStep("tool job", "code", L("Hintergrund-Job", "Background job"), ev.command || "", null);
      el.querySelector(".act-args").textContent = `${ev.id}  ${ev.cwd || ""}`;
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "btn small ghost act-stop";
      stop.textContent = L("Stoppen", "Stop");
      stop.onclick = (e) => { e.stopPropagation(); api("POST", `/api/jobs/${ev.id}/stop`).catch(() => {}); };
      el.querySelector(".act-state").prepend(stop);
      acts[key] = el;
      return;
    }
    if (!el) return;
    if (ev.phase === "output") {
      const out = el.querySelector(".act-out");
      out.textContent = (out.textContent + ev.text).slice(-20000);
      out.scrollTop = out.scrollHeight;
    } else if (ev.phase === "done") {
      el.querySelector(".act-stop")?.remove();
      const ok = ev.status === "exit" && ev.rc === 0;
      finishStep(el, ok ? "ok" : ev.status === "stopped" ? "denied" : "error",
                 ev.ended && ev.started ? ev.ended - ev.started : undefined);
      if (ev.status === "stopped") el.querySelector(".act-status").textContent = L("gestoppt", "stopped");
      else if (!ok) el.querySelector(".act-status").textContent = `Exit ${ev.rc}`;
      delete acts[key];
    }
  }

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
    $("confirm").classList.toggle("editing", !!editable.length);
    $("confirm-yes").innerHTML = editable.length && ev.name === "mail_send"
      ? `${L("Senden", "Send")} <kbd>${L("Strg", "Ctrl")}+Enter</kbd>` : editable.length ? `${L("Ausführen", "Run")} <kbd>${L("Strg", "Ctrl")}+Enter</kbd>` : APPROVE_HTML;
    $("confirm-summary").textContent = ev.name === "mail_send"
      ? L("Mail prüfen, bei Bedarf ändern und senden:", "Check the e-mail, edit it if needed and send it:")
      : L(`Soll ich ${ev.summary} ausführen?`, `Shall I run ${ev.summary}?`);
    const cmd = $("confirm-cmd");
    const preview = S.previews[ev.id];
    cmd.classList.toggle("diff", !!preview);
    if (preview) cmd.innerHTML = renderDiff(preview);
    else cmd.textContent = ev.name === "run_shell" || ev.name === "shell"
      ? (ev.args.workdir ? `(${ev.args.workdir}) ` : "") + ev.args.command : `${ev.name}(${JSON.stringify(ev.args, null, 2)})`;
    $("confirm-reason").textContent = ev.reason && !editable.length ? L("Grund: ", "Reason: ") + ev.reason : "";
    $("confirm-trust").classList.toggle("hidden", !ev.trustable || !!editable.length);
    $("confirm-voice").textContent = editable.length ? L("Senden nur per Klick – „Nein“ bricht ab.", "Send only by click – “no” cancels.")
      : A.micReady ? L("oder sag „Ja“ bzw. „Nein“", "or say “yes” or “no”") : "";
    $("confirm-voice").classList.remove("listening");
    placeConfirm();
    const act = acts[ev.id];
    if (act) { act.classList.add("waiting"); act.querySelector(".act-status").textContent = STATUS_TEXT.waiting; }
    setTimeout(() => {
      (form.querySelector("input, textarea") || $("confirm-yes")).focus({ preventScroll: true });
      scrollChat();
    }, 50);
    refresh();
    if (!S.tts) onSpeechDrained();
  }

  function editedFields() {
    const out = {};
    for (const f of $("confirm-form").querySelectorAll("[data-key]")) out[f.dataset.key] = f.value;
    return out;
  }

  // Die Karte steht am Ende des Gesprächs – auch nachdem der Chat neu gezeichnet wurde
  function placeConfirm() {
    const card = $("confirm");
    card.classList.remove("hidden");
    if (card.parentElement !== chat || chat.lastElementChild !== card) chat.appendChild(card);
  }

  function parkConfirm() {
    const card = $("confirm");
    card.classList.add("hidden");
    $("confirm-home").appendChild(card);
  }

  function closeConfirm(id) {
    if (S.confirm && (!id || S.confirm.id === id)) {
      S.confirm = null;
      parkConfirm();
      if (S.recording) send({ type: "cancel_listen" });
      refresh();
    }
  }

  function answerConfirm(approved, trust = false) {
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
    send(args ? { type: "confirm", id: S.confirm.id, approved, args }
      : { type: "confirm", id: S.confirm.id, approved, ...(trust ? { trust: true } : {}) });
    if (trust) toast(L("In diesem Chat fragt fremder Inhalt nicht mehr nach – bis neuer dazukommt.",
      "Outside content no longer asks in this chat – until new content arrives."));
    const act = acts[S.confirm.id];
    if (act && approved) { act.classList.remove("waiting"); act._start = performance.now(); act.querySelector(".act-status").textContent = ""; }
    closeConfirm();
  }
  $("confirm-yes").onclick = () => answerConfirm(true);
  $("confirm-trust").onclick = () => answerConfirm(true, true);
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

  // ---------------------------------------------------------------- Bilder und PDFs anhängen (Büroklammer, Strg+V, Ziehen)
  const ATT = { items: [] };  // { name, url, pending, pdf, title, doc }
  const isPdf = (f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name || "");
  function docLine(d) {
    if (!d) return L("wird gelesen …", "reading …");
    if (d.scanned) return L("kein Text – gescannt?", "no text – scanned?");
    return d.pages === 1 ? L("1 Seite", "1 page") : L(`${d.pages} Seiten`, `${d.pages} pages`);
  }
  function fileChip(title, d, onclick) {
    const chip = document.createElement(onclick ? "button" : "div");
    if (onclick) { chip.type = "button"; chip.onclick = onclick; }
    chip.className = "file-chip" + (d && d.scanned ? " warn" : "");
    chip.innerHTML = `<svg class="i"><use href="#i-file"/></svg><span class="fc-text"><b></b><small></small></span>`;
    chip.querySelector("b").textContent = title;
    chip.querySelector("small").textContent = docLine(d) + (d && d.truncated ? L(" · gekürzt", " · shortened") : "");
    chip.title = d && d.scanned ? L("Dieses PDF enthält keinen auslesbaren Text (vermutlich ein Scan) – das Modell sieht nur den Dateinamen.",
                                    "This PDF has no extractable text (probably a scan) – the model only sees the file name.") : title;
    return chip;
  }
  function renderAttachments() {
    const row = $("attach-row");
    row.innerHTML = "";
    row.classList.toggle("hidden", !ATT.items.length);
    for (const it of ATT.items) {
      let chip;
      if (it.pdf) {
        chip = fileChip(it.title, it.pending ? null : it.doc);
        chip.classList.add("attach-file");
        if (it.pending) chip.classList.add("loading");
        chip.insertAdjacentHTML("beforeend", `<button type="button" class="fc-x" aria-label="${L("Entfernen", "Remove")}">✕</button>`);
      } else {
        chip = document.createElement("div");
        chip.className = "attach-chip" + (it.pending ? " loading" : "");
        chip.innerHTML = `<img alt=""><button type="button" aria-label="${L("Entfernen", "Remove")}">✕</button>`;
        chip.querySelector("img").src = it.preview || it.url;
      }
      chip.querySelector("button:last-child").onclick = () => { ATT.items = ATT.items.filter((x) => x !== it); renderAttachments(); };
      row.appendChild(chip);
    }
    if (ATT.items.some((it) => !it.pdf) && ATT.vision === false) {
      const hint = document.createElement("span");
      hint.className = "attach-hint";
      hint.textContent = L("Das Modell sieht keine Bilder – das Vision-Modell schaut es sich an.",
                           "This model can't see images – the vision model will look at it.");
      row.appendChild(hint);
    }
  }
  function attachFiles(files) {
    for (const f of files) {
      const pdf = isPdf(f);
      if (!pdf && !f.type.startsWith("image/")) { toast(L(`${f.name}: nur Bilder und PDFs.`, `${f.name}: images and PDFs only.`)); continue; }
      if (ATT.items.length >= 4) { toast(L("Höchstens 4 Dateien pro Nachricht.", "At most 4 files per message.")); break; }
      if (f.size > (pdf ? 50e6 : 20e6)) {
        toast(pdf ? L("PDF zu groß (höchstens 50 MB).", "PDF too large (50 MB max).") : L("Bild zu groß (höchstens 20 MB).", "Image too large (20 MB max)."));
        continue;
      }
      const it = pdf ? { pending: true, pdf: true, title: f.name } : { pending: true, preview: URL.createObjectURL(f) };
      ATT.items.push(it);
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const r = await api("POST", "/api/attachments", { data: reader.result, name: f.name });
          Object.assign(it, { name: r.name, url: r.url, pending: false, doc: r.doc });
          if (!pdf) ATT.vision = r.vision;
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
    if (ATT.items.some((it) => it.pending)) { toast(L("Datei wird noch hochgeladen …", "File still uploading …")); return; }
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
    toast(S.plan ? L("Planmodus an – Orbwise legt erst einen Plan vor, ausgeführt wird nach deiner Freigabe.",
                     "Plan mode on – Orbwise presents a plan first and carries it out once you approve it.")
                 : L("Planmodus aus – Orbwise legt direkt los.", "Plan mode off – Orbwise gets going right away."));
    refresh();
  };

  // Denken: aufklappbar mit Stufen (Modelle ohne echte Stufen bekommen ein Denk-Budget)
  const thinkMenu = $("think-menu");
  function setThinkMenu(open) {
    thinkMenu.classList.toggle("hidden", !open);
    $("btn-think").setAttribute("aria-expanded", String(open));
  }
  // Was das geladene Modell kann: "levels" (eigene Stufen des Modells), "toggle" (nur an/aus), "none" – null =
  // unbekannt (an/aus). Stufen gibt es nur, wenn das Modell sie selbst vorgibt – keine erfundenen von Orbwise.
  async function loadThinkCaps() {
    try {
      const r = await getJSON("/api/think");
      S.thinkCaps = r.caps; S.thinkLevels = r.levels || []; S.thinkDefault = r.default || null; S.thinkEvidence = r.evidence || [];
    } catch { S.thinkCaps = null; S.thinkLevels = []; S.thinkDefault = null; S.thinkEvidence = []; }
    buildThinkLevels();
    refresh();
  }
  function thinkLevels() { return S.thinkCaps === "levels" ? S.thinkLevels || [] : []; }
  function thinkCurrent() {  // gewählte Stufe – sonst die Voreinstellung des Modells
    const levels = thinkLevels();
    return levels.includes(S.think) ? S.think : levels.includes(S.thinkDefault) ? S.thinkDefault : levels[0];
  }
  function buildThinkLevels() {
    document.querySelectorAll("[data-think].lvl, .think-source").forEach((b) => b.remove());
    const levels = thinkLevels();
    if (levels.length) {  // Herkunft zeigen: die Stufen stehen so in der Chat-Vorlage des Modells
      const src = document.createElement("div");
      src.className = "think-source";
      src.textContent = L(`Stufen aus der Chat-Vorlage des Modells: ${levels.join(", ")}`, `Levels from the model's chat template: ${levels.join(", ")}`);
      src.title = (S.thinkEvidence || []).join("\n") || L("Dokumentierte Stufen des Modells", "The model's documented levels");
      thinkMenu.appendChild(src);
    }
    for (const box of [thinkMenu, $("set-think")]) {
      if (!box) continue;
      let after = box.querySelector('[data-think="on"]');
      for (const k of levels) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "lvl"; b.dataset.think = k;
        if (box === thinkMenu) {
          b.setAttribute("role", "menuitemradio");
          b.innerHTML = `<b>${escapeHtml(k)}</b><small>${k === S.thinkDefault ? L("Stufe des Modells – seine Voreinstellung.", "The model's level – its default.") : L("Stufe des Modells.", "The model's level.")}</small>`;
        } else {
          b.textContent = thinkName(k);
        }
        b.onclick = (e) => { e.stopPropagation(); setThinkMenu(false); setThink(k); };
        after.after(b);
        after = b;
      }
    }
  }
  function renderThink() {
    const none = S.thinkCaps === "none", levels = thinkLevels().length > 0;
    const on = S.think !== "off" && !none;
    const btn = $("btn-think");
    btn.classList.toggle("on", on);
    btn.disabled = none;
    btn.title = none ? L("Dieses Modell kann nicht denken.", "This model cannot think.")
      : L("Denkmodus: Orbwise überlegt vor der Antwort – langsamer, dafür gründlicher.", "Thinking mode: Orbwise reasons before answering – slower but more thorough.");
    $("think-label").textContent = levels && on ? L("Denken · ", "Think · ") + thinkName(thinkCurrent()) : L("Denken", "Think");
    const cur = levels ? thinkCurrent() : "";
    document.querySelectorAll("[data-think]").forEach((b) => {
      const k = b.dataset.think;
      if (k === "on") b.classList.toggle("hidden", levels);
      b.classList.toggle("on", k === "off" ? !on : k === "on" ? on : on && k === cur);
      b.disabled = none && k !== "off";
    });
  }
  function sendThink() {
    const on = S.think !== "off" && S.thinkCaps !== "none";
    send({ type: "think", enabled: on, level: on && thinkLevels().length ? thinkCurrent() : undefined });
  }
  function applySettings(ev, fromHello) {
    if (["off", "read", "files", "auto"].includes(ev.auto_mode)) { S.autoMode = ev.auto_mode; store.set("autoMode", S.autoMode); }
    S.root = !!ev.root;
    if (ev.jail) S.jail = ev.jail;
    if (ev.think === null || ev.think === undefined) {
      if (fromHello) sendThink();  // der Server kennt noch keine Wahl → die gemerkte schicken
    } else if (ev.think === "on") {
      if (S.think === "off") S.think = "on";  // „an“ bei Modellen ohne Stufen – eine gemerkte Stufe bleibt
    } else {
      S.think = thinkLevel(ev.think);
    }
    store.set("think", S.think);
    refresh();
  }
  function setThink(level) {
    if (level === "on" && S.think !== "off") level = S.think;  // die gemerkte Stufe bleibt für Modelle mit Stufen
    S.think = thinkLevel(level);
    store.set("think", S.think);
    sendThink();
    toast(S.think === "off" ? L("Denken aus – schnelle Antworten.", "Thinking off – fast answers.")
      : thinkLevels().length ? L("Denken: ", "Thinking: ") + thinkName(thinkCurrent()) + "."
      : L("Denken an – der Gedankengang läuft in der Antwort mit.", "Thinking on – the reasoning shows in the answer."));
    refresh();
  }
  $("btn-think").onclick = (e) => { e.stopPropagation(); setThinkMenu(thinkMenu.classList.contains("hidden")); };
  document.querySelectorAll("[data-think]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); setThinkMenu(false); setThink(b.dataset.think); };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#think-wrap")) setThinkMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !thinkMenu.classList.contains("hidden")) setThinkMenu(false); });

  // Darstellung: hell, dunkel oder wie das System (das Skript im <head> setzt <html data-theme>)
  function renderTheme() {
    const pick = store.get("theme", "system");
    document.querySelectorAll("[data-theme-pick]").forEach((b) => b.classList.toggle("on", b.dataset.themePick === pick));
  }
  document.querySelectorAll("[data-theme-pick]").forEach((b) => {
    b.onclick = () => { store.set("theme", b.dataset.themePick); window.orbwiseApplyTheme(b.dataset.themePick); renderTheme(); };
  });
  renderTheme();

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
    toast({ off: L("Auto aus – jeder Befehl fragt vorher.", "Auto off – every command asks first."),
            read: L("Auto: nur lesen.", "Auto: read only."),
            files: L("Auto: lesen und Dateien ändern – Bauen und Tests laufen isoliert ohne Netz.",
                     "Auto: read and change files – builds and tests run isolated without network."),
            auto: L("Auto: alles läuft – nur sudo fragt nach dem Passwort, Pushen und Mails senden fragen.",
                    "Auto: everything runs – only sudo asks for the password, pushing and sending e-mail ask.") }[mode]);
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
    if (name === "settings") showSection(section || "general");
    if (name === "models") renderModelsPage();
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
    if (section === "voice") openVoiceMenu();
    if (section === "status") loadStatus();
    if (section === "skills") loadSkills();
  }

  // ---------------------------------------------------------------- Skills: einrichten, an-/abschalten
  // Werte liegen in state.json, Tokens/Passwörter im Schlüsselbund – der Server schickt sie nie zurück.
  const SKILL_ICON = { globe: "i-globe", mail: "i-mail", "file-text": "i-file", home: "i-home", box: "i-box",
    calendar: "i-calendar", notebook: "i-edit", send: "i-send", github: "i-github", terminal: "i-terminal",
    cloud: "i-cloud", repeat: "i-clock", bell: "i-bell", package: "i-download", app: "i-play", eye: "i-image",
    sun: "i-sparkles", image: "i-image", cpu: "i-chip" };
  async function loadSkills() {
    const box = $("set-skills");
    let data;
    try { data = await getJSON("/api/skills"); } catch { box.textContent = L("Skills konnten nicht geladen werden.", "Could not load skills."); return; }
    renderSkills(box, data);
  }
  const hasValue = (f) => "value" in f && f.value !== null && f.value !== "" && f.value !== 0
    && !(typeof f.value === "object" && !Object.keys(f.value).length);
  function skillState(sk) {
    if (sk.kind === "connect" && !sk.configured) return sk.fields.some((f) => f.required && (f.set || hasValue(f)))
      ? ["warn", L("Unvollständig", "Incomplete")] : ["", L("Nicht eingerichtet", "Not set up")];
    return sk.on ? ["ok", L("Aktiv", "Active")] : ["", L("Aus", "Off")];
  }
  function renderSkills(box, data) {
    const where = data.backend === "keyring"
      ? L("Zugangsdaten liegen verschlüsselt im Schlüsselbund deiner Sitzung – nicht in config.yaml.",
          "Credentials are stored encrypted in your session keyring – not in config.yaml.")
      : L(`Kein Schlüsselbund gefunden – Zugangsdaten liegen in ${data.secrets_file}, nur für dich lesbar.`,
          `No keyring found – credentials are stored in ${data.secrets_file}, readable only by you.`);
    box.innerHTML = `<p class="set-hint skills-where"><svg class="i"><use href="#i-key"/></svg><span></span></p>`;
    box.querySelector(".skills-where span").textContent = where;
    if ((data.plaintext || []).length) {
      const warn = document.createElement("p");
      warn.className = "set-hint skills-warn";
      warn.textContent = L(`Noch im Klartext in config.yaml: ${data.plaintext.join(", ")} – beim nächsten Start zieht Orbwise sie um.`,
                           `Still in plain text in config.yaml: ${data.plaintext.join(", ")} – Orbwise moves them on the next start.`);
      box.appendChild(warn);
    }
    for (const [kind, title] of [["connect", L("Verbindungen", "Connections")], ["builtin", L("Eingebaut", "Built in")]]) {
      const h = document.createElement("div");
      h.className = "mm-title";
      h.textContent = title;
      box.appendChild(h);
      const list = document.createElement("div");
      list.className = "skill-list";
      for (const sk of data.skills.filter((x) => x.kind === kind)) list.appendChild(skillRow(sk, data));
      box.appendChild(list);
    }
  }
  function skillRow(sk, data) {
    const row = document.createElement("div");
    const [cls, label] = skillState(sk);
    row.className = "skill-row " + cls;
    row.innerHTML = `<span class="skill-ico"><svg class="i"><use href="#${SKILL_ICON[sk.icon] || "i-sparkles"}"/></svg></span>`
      + `<div class="skill-text"><b></b><small></small></div><span class="skill-state"><i></i><em></em></span>`
      + `<button type="button" class="btn ghost skill-edit"></button><button type="button" class="switch"></button>`;
    row.querySelector("b").textContent = sk.name;
    row.querySelector("small").textContent = sk.desc + (sk.hint ? " " + sk.hint : "");
    row.querySelector("em").textContent = label;
    const edit = row.querySelector(".skill-edit");
    if (sk.fields.length) {
      edit.textContent = sk.fields.some((f) => f.set || hasValue(f)) ? L("Bearbeiten", "Edit") : L("Einrichten", "Set up");
      edit.onclick = () => skillForm(sk);
    } else edit.remove();
    const sw = row.querySelector(".switch");
    const on = sk.on && sk.configured;
    sw.classList.toggle("on", on);
    sw.textContent = on ? L("An", "On") : L("Aus", "Off");
    sw.title = sk.configured ? "" : L("Erst einrichten", "Set up first");
    sw.onclick = async () => {
      if (!sk.configured) { skillForm(sk); return; }
      sw.disabled = true;
      try { await api("POST", `/api/skills/${sk.id}`, { enabled: !sk.on }); } catch { /* Meldung kommt von api() */ }
      loadSkills();
    };
    return row;
  }
  function skillField(f) {
    const wrap = document.createElement("div");
    wrap.className = "skill-field" + (f.advanced ? " adv hidden" : "");
    wrap.innerHTML = `<span class="field-label"></span>`;
    wrap.querySelector(".field-label").textContent = f.label + (f.required ? " *" : "");
    let get;
    if (f.type === "bool") {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "switch";
      const set = (v) => { b.classList.toggle("on", v); b.textContent = v ? L("An", "On") : L("Aus", "Off"); };
      set(f.value === true || f.value === "true");
      b.onclick = () => set(!b.classList.contains("on"));
      wrap.appendChild(b);
      get = () => b.classList.contains("on");
    } else if (f.type === "select") {
      const sel = document.createElement("select");
      sel.className = "ui-dialog-input";
      for (const o of f.options) { const opt = document.createElement("option"); opt.value = o; opt.textContent = o; sel.appendChild(opt); }
      if (f.value) sel.value = f.value;
      wrap.appendChild(sel);
      get = () => sel.value;
    } else if (f.type === "hosts") {
      const table = document.createElement("div");
      table.className = "skill-hosts";
      const rows = [];
      const addRow = (name = "", h = {}) => {
        const r = document.createElement("div");
        r.className = "skill-host";
        r.innerHTML = `<input class="ui-dialog-input" placeholder="nas"><input class="ui-dialog-input" placeholder="192.168.1.10">`
          + `<input class="ui-dialog-input" placeholder="${L("Benutzer", "user")}"><input class="ui-dialog-input" type="number" placeholder="22">`
          + `<button type="button" class="icon-btn" title="${L("Entfernen", "Remove")}"><svg class="i"><use href="#i-x"/></svg></button>`;
        const [n, host, user, port] = r.querySelectorAll("input");
        n.value = name; host.value = h.host || ""; user.value = h.user || ""; port.value = h.port && h.port !== 22 ? h.port : "";
        const item = { n, host, user, port };
        r.querySelector("button").onclick = () => { r.remove(); rows.splice(rows.indexOf(item), 1); };
        rows.push(item);
        table.insertBefore(r, add);
      };
      const add = document.createElement("button");
      add.type = "button";
      add.className = "btn ghost";
      add.textContent = L("+ Kurzname", "+ short name");
      add.onclick = () => addRow();
      table.appendChild(add);
      for (const [name, h] of Object.entries(f.value || {})) addRow(name, h);
      wrap.appendChild(table);
      get = () => Object.fromEntries(rows.filter((r) => r.n.value.trim() && r.host.value.trim()).map((r) => [r.n.value.trim(),
        { host: r.host.value.trim(), user: r.user.value.trim(), port: parseInt(r.port.value, 10) || 22 }]));
    } else {
      const line = document.createElement("div");
      line.className = "skill-input";
      const inp = document.createElement("input");
      inp.className = "ui-dialog-input";
      inp.type = f.type === "password" ? "password" : f.type === "number" ? "number" : "text";
      inp.spellcheck = false;
      inp.autocomplete = f.type === "password" ? "new-password" : "off";
      inp.placeholder = f.type === "password" && f.set ? L("•••• gespeichert – zum Ändern neu eingeben", "•••• saved – type to change") : f.placeholder || "";
      if (f.type !== "password" && f.value !== null && f.value !== undefined && f.value !== 0) inp.value = f.value;
      line.appendChild(inp);
      if (f.type === "password") {
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "btn ghost";
        eye.textContent = L("Zeigen", "Show");
        eye.onclick = () => { const show = inp.type === "password"; inp.type = show ? "text" : "password"; eye.textContent = show ? L("Verbergen", "Hide") : L("Zeigen", "Show"); };
        line.appendChild(eye);
      }
      if (f.type === "path") {
        const browse = document.createElement("button");
        browse.type = "button";
        browse.className = "btn ghost";
        browse.textContent = L("Durchsuchen", "Browse");
        browse.onclick = async () => { const p = await askFolder(inp.value || "~"); if (p) inp.value = p; };
        line.appendChild(browse);
      }
      wrap.appendChild(line);
      get = () => inp.value;
      wrap.setValue = (v) => { inp.value = v; };
    }
    if (f.help) {
      const help = document.createElement("small");
      help.className = "skill-help";
      help.textContent = f.help;
      wrap.appendChild(help);
    }
    if (f.type === "password" && f.source && !["keyring", "file"].includes(f.source)) {
      const src = document.createElement("small");
      src.className = "skill-help";
      src.textContent = { config: L("Steht noch in config.yaml.", "Still in config.yaml."), env: L("Kommt aus einer Umgebungsvariable.", "Comes from an environment variable."),
                          gh: L("Kommt aus der Anmeldung der GitHub-CLI (gh).", "Comes from the GitHub CLI login (gh).") }[f.source] || "";
      wrap.appendChild(src);
    }
    return { wrap, get, field: f };
  }
  function skillForm(sk) {
    const scrim = document.createElement("div");
    scrim.className = "ui-dialog-scrim";
    const box = document.createElement("div");
    box.className = "ui-dialog skill-form";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML = `<div class="skill-form-head"><span class="skill-ico"><svg class="i"><use href="#${SKILL_ICON[sk.icon] || "i-sparkles"}"/></svg></span><div><b></b><small></small></div></div>`
      + `<div class="skill-fields"></div><p class="skill-result hidden"></p>`
      + `<div class="modal-actions"><button type="button" class="btn ghost danger-text skill-remove"></button><span class="spacer"></span>`
      + `<button type="button" class="btn ghost skill-test"></button><button type="button" class="btn ghost ui-cancel"></button><button type="button" class="btn primary ui-ok"></button></div>`;
    box.querySelector(".skill-form-head b").textContent = sk.name;
    box.querySelector(".skill-form-head small").textContent = sk.desc;
    const holder = box.querySelector(".skill-fields");
    const inputs = sk.fields.map(skillField);
    inputs.forEach((x) => holder.appendChild(x.wrap));
    if (inputs.some((x) => x.field.advanced)) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "btn-link skill-more";
      more.textContent = L("Mehr Einstellungen", "More settings");
      more.onclick = () => { holder.querySelectorAll(".adv").forEach((el) => el.classList.toggle("hidden")); more.remove(); };
      holder.appendChild(more);
    }
    const result = box.querySelector(".skill-result");
    const values = () => Object.fromEntries(inputs.map((x) => [x.field.key, x.get()]));
    const close = () => { document.removeEventListener("keydown", keys, true); scrim.remove(); };
    const keys = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); } };
    document.addEventListener("keydown", keys, true);
    const btnTest = box.querySelector(".skill-test"), btnOk = box.querySelector(".ui-ok"), btnRemove = box.querySelector(".skill-remove");
    btnTest.textContent = L("Verbindung testen", "Test connection");
    box.querySelector(".ui-cancel").textContent = L("Abbrechen", "Cancel");
    btnOk.textContent = L("Speichern", "Save");
    btnRemove.textContent = L("Entfernen", "Remove");
    if (!sk.fields.some((f) => f.set || hasValue(f))) btnRemove.classList.add("hidden");
    const show = (ok, text) => { result.className = "skill-result " + (ok === true ? "ok" : ok === false ? "bad" : ""); result.textContent = text; };
    btnTest.onclick = async () => {
      btnTest.disabled = true;
      show(null, L("Teste …", "Testing …"));
      try {
        const r = await api("POST", `/api/skills/${sk.id}/test`, { values: values() });
        show(r.ok, (r.ok ? "✓ " : r.ok === false ? "✕ " : "") + r.message);
        const chat = r.details && r.details.chat_id;
        const chatField = inputs.find((x) => x.field.key === "chat_id");
        if (chat && chatField && chatField.wrap.setValue) chatField.wrap.setValue(chat);
      } catch { show(false, L("Test fehlgeschlagen.", "Test failed.")); }
      btnTest.disabled = false;
    };
    btnOk.onclick = async () => {
      btnOk.disabled = true;
      try {
        await api("POST", `/api/skills/${sk.id}`, { values: values(), enabled: true });
        close();
        toast(L(`✔ ${sk.name} gespeichert.`, `✔ ${sk.name} saved.`));
        loadSkills();
      } catch { btnOk.disabled = false; }
    };
    btnRemove.onclick = async () => {
      if (!await ask(L(`${sk.name}: gespeicherte Daten und Zugangsdaten löschen?`, `${sk.name}: delete the saved data and credentials?`))) return;
      try { await api("DELETE", `/api/skills/${sk.id}`); close(); loadSkills(); } catch { /* Meldung von api() */ }
    };
    box.querySelector(".ui-cancel").onclick = close;
    scrim.onclick = (e) => { if (e.target === scrim) close(); };
    scrim.appendChild(box);
    document.body.appendChild(scrim);
    setTimeout(() => { const first = holder.querySelector("input:not([type=hidden])"); if (first) first.focus(); }, 30);
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
  // Wird das Fenster schmal (Handy-Breite), legt sich ein offenes Panel nicht über den ganzen Chat
  let lastWidth = window.innerWidth;
  window.addEventListener("resize", () => {
    if (lastWidth >= 900 && window.innerWidth < 900 && app.classList.contains("drawer-open")) setDrawer(false);
    lastWidth = window.innerWidth;
  });
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
  };
  $("fx-amount").oninput = (e) => {
    S.fxAmount = e.target.value / 100;
    store.set("fxAmount", S.fxAmount);
    renderFx();
  };
  renderFx();

  // Stimmen-Menü (Einstellungen → Stimme): Pocket TTS installieren, Stimme und ihre Sprache wählen, anhören
  const voiceMenu = $("voice-menu");
  let pocketPoll = null;
  async function openVoiceMenu() {
    const list = $("voice-list");
    let data;
    try { data = await getJSON("/api/voices"); } catch { toast(L("Stimmen nicht ladbar", "Could not load voices")); return; }
    list.innerHTML = `<div class="mm-title vm-pocket-head"><span>${L("Stimme wählen", "Choose voice")}</span>
      <a href="https://kyutai.org/pocket-tts" target="_blank" rel="noopener">${L("Probehören ↗", "Listen ↗")}</a></div>`;
    list.querySelector("a").onclick = (e) => e.stopPropagation();
    const note = (text) => {
      list.insertAdjacentHTML("beforeend", `<div class="mm-hint"></div>`);
      list.lastElementChild.textContent = text;
    };
    voiceMenu.classList.remove("hidden");
    if (!data.available) {
      note(L("Sprachausgabe ist deaktiviert (voice.enabled) – es spricht der Browser.", "Speech output is disabled (voice.enabled) – the browser speaks instead."));
      return;
    }
    const pk = data.pocket || {};
    if (!pk.installed) {
      if (!pk.supported) { note(L("Pocket TTS braucht eine CPU mit AVX2 (x86-64) oder ARM64 – es spricht der Browser.", "Pocket TTS needs a CPU with AVX2 (x86-64) or ARM64 – the browser speaks instead.")); return; }
      const b = document.createElement("button");
      b.className = "model-item add";
      b.innerHTML = `<div>${L("Pocket TTS installieren", "Install Pocket TTS")}</div><div class="mi-note"></div>`;
      b.querySelector(".mi-note").textContent = L(
        "Natürliche Sprachausgabe auf der CPU. Programm wenige MB, beim ersten Start lädt es Modell und Stimme (einige hundert MB). Bis dahin spricht der Browser.",
        "Natural speech output on the CPU. The program is a few MB; on first start it downloads the model and voice (a few hundred MB). Until then the browser speaks.");
      b.onclick = async (e) => {
        e.stopPropagation();
        b.disabled = true;
        b.querySelector(".mi-note").textContent = L("Lade Pocket TTS …", "Downloading Pocket TTS …");
        try {
          await api("POST", "/api/pocket/install");
          toast(L("✔ Pocket TTS installiert – das Modell wird jetzt geladen", "✔ Pocket TTS installed – loading the model now"));
          loadStatus();
          openVoiceMenu();
        } catch { b.disabled = false; b.querySelector(".mi-note").textContent = L("Nochmal versuchen?", "Try again?"); }
      };
      list.appendChild(b);
      return;
    }
    if (pk.state === "starting") {
      note(L("Startet … beim ersten Mal werden Modell und Stimme heruntergeladen.", "Starting … the first time, the model and voice are downloaded."));
    } else if (pk.state === "error" && pk.error) {
      note(L("Fehler: ", "Error: ") + pk.error + L(" – solange spricht der Browser.", " – the browser speaks meanwhile."));
    }
    for (const v of data.voices) list.appendChild(voiceRow(v, data.languages));
    const rm = document.createElement("button");
    rm.className = "model-item add";
    rm.textContent = L("Pocket TTS entfernen", "Remove Pocket TTS");
    rm.onclick = async (e) => {
      e.stopPropagation();
      if (!await ask(L("Pocket TTS mit Modell und Stimmen löschen? Danach spricht der Browser.", "Delete Pocket TTS with its model and voices? The browser speaks afterwards."))) return;
      try {
        const r = await api("DELETE", "/api/pocket");
        toast(L(`✔ Pocket TTS entfernt (${r.freed_mb} MB frei)`, `✔ Pocket TTS removed (${r.freed_mb} MB freed)`));
        loadStatus();
        openVoiceMenu();
      } catch { /* Meldung kommt von api() */ }
    };
    list.appendChild(rm);
    // solange der Server startet, die Anzeige nachziehen (nur solange das Menü sichtbar ist)
    clearTimeout(pocketPoll);
    if (pk.state === "starting") {
      pocketPoll = setTimeout(() => { if (voiceMenu.offsetParent) openVoiceMenu(); }, 3000);
    }
  }

  async function chooseVoice(v, language) {
    try {
      const r = await api("POST", "/api/voices/select", { voice: v.name, language });
      toast(L(`Stimme: ${v.label} · ${langLabel(r.language)}`, `Voice: ${v.label} · ${langLabel(r.language)}`));
    } catch { /* Meldung kommt von api() */ }
  }

  let voiceLanguages = [];
  function langLabel(code) {
    const hit = voiceLanguages.find((l) => l.code === code);
    return hit ? hit.label : code;
  }

  function voiceRow(v, languages) {
    voiceLanguages = languages;
    const active = v.current;
    const b = document.createElement("button");
    b.className = "model-item" + (active ? " active" : "");
    b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
    b.querySelector(".mi-name").textContent = v.label;
    b.querySelector(".mi-tag").textContent = active ? L("Aktiv", "Active") : v.male ? L("männlich", "male") : L("weiblich", "female");
    b.querySelector(".mi-sub").textContent = v.description;
    b.onclick = (e) => { e.stopPropagation(); if (!active) chooseVoice(v, v.language); };
    const lang = document.createElement("select");
    lang.className = "voice-lang";
    lang.title = L("Sprache dieser Stimme – auch die Sprache der Antworten", "Language of this voice – also the language of the answers");
    for (const l of languages) lang.add(new Option(l.label, l.code, false, l.code === v.language));
    lang.onclick = (e) => e.stopPropagation();
    lang.onchange = () => chooseVoice(v, lang.value);
    const play = document.createElement("button");
    play.className = "model-del voice-play";
    play.innerHTML = ico("play");
    play.title = L("Anhören", "Preview");
    play.onclick = (e) => { e.stopPropagation(); previewVoice(v.name); };
    const row = document.createElement("div");
    row.className = "model-row";
    row.append(b, lang, play);
    return row;
  }

  $("pill-voice").onclick = () => openSheet("settings", "voice");

  async function previewVoice(name) {
    await initAudio();
    const slow = setTimeout(() => toast(L("Pocket TTS startet noch …", "Pocket TTS is still starting …")), 1500);
    const r = await fetch(`/api/voices/${encodeURIComponent(name)}/preview`);
    clearTimeout(slow);
    if (!r.ok) {
      let msg = L("Probe nicht möglich", "Preview not possible");
      try { msg = (await r.json()).detail || msg; } catch { /* egal */ }
      toast(msg);
      return;
    }
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
  // Zeile unter der Eingabe: Kontext jetzt, wie viele Token des Fensters im Grafikspeicher bzw. RAM liegen (KV-Cache),
  // und die Token der ganzen Sitzung (dieses Chats). Ohne Angaben bleibt der Hinweistext des Modus.
  function renderTokenLine() {
    const el = document.querySelector(".composer-hint");
    if (!el) return;
    const c = S.ctxLive || S.ctx, u = S.usage, m = lastMem, hintText = (MODE_TEXT[S.mode] || MODE_TEXT.tools).hint;
    const parts = [], tips = [];
    if (c && c.window) {
      const used = c.real || c.tokens || c.used || 0;
      parts.push(L(`Kontext ${kTok(used)} / ${ctxLabel(c.window)}`, `Context ${kTok(used)} / ${ctxLabel(c.window)}`));
      tips.push(L(`Kontext: ${used} von ${c.window} Token des Kontextfensters sind gerade belegt.`,
                  `Context: ${used} of ${c.window} tokens of the context window are in use right now.`));
      // KV-Cache nur, wenn davon etwas im (langsamen) RAM liegt – sonst sagt es nichts
      if (m && m.available && m.kv_vram_share != null && m.kv_vram_share < 0.99) {
        const ram = c.window - Math.round(c.window * m.kv_vram_share);
        parts.push(L(`KV-Cache ${ctxLabel(ram)} im RAM (langsam)`, `KV cache ${ctxLabel(ram)} in RAM (slow)`));
        tips.push(L(`Vom Arbeitsspeicher des Modells für das Kontextfenster (KV-Cache) liegen ${ram} Token im RAM statt im Grafikspeicher – das bremst. Ein kleineres Kontextfenster passt ganz in den VRAM.`,
                    `Of the model's working memory for the context window (KV cache), ${ram} tokens are in RAM instead of VRAM – that slows it down. A smaller context window fits fully in VRAM.`));
      }
    }
    const pc = m && m.prompt_cache;
    if (pc && pc.prompts) {  // llama-server hebt fertig gelesene Chats im RAM auf (--cache-ram)
      const gbTxt = (mib) => fmt(mib / 1024, mib >= 10240 ? 0 : 1);
      parts.push(L(`Prompt-Cache ${pc.prompts} ${pc.prompts === 1 ? "Chat" : "Chats"} · ${gbTxt(pc.mib)}/${gbTxt(pc.limit_mib)} GB im RAM`,
                   `Prompt cache ${pc.prompts} ${pc.prompts === 1 ? "chat" : "chats"} · ${gbTxt(pc.mib)}/${gbTxt(pc.limit_mib)} GB in RAM`));
      tips.push(L(`Prompt-Cache: llama-server hebt ${pc.prompts} fertig gelesene Chats (${pc.tokens} Token) im RAM auf – nach Chatwechsel, Routine oder Telegram muss er sie nicht neu einlesen. Belegt ${Math.round(pc.mib)} von ${Math.round(pc.limit_mib)} MiB (--cache-ram).`,
                  `Prompt cache: llama-server keeps ${pc.prompts} already read chats (${pc.tokens} tokens) in RAM – after a chat switch, routine or Telegram it doesn't have to read them again. Uses ${Math.round(pc.mib)} of ${Math.round(pc.limit_mib)} MiB (--cache-ram).`));
    }
    if (u && (u.read || u.written)) {
      parts.push(L(`Sitzung ${kTok(u.read + u.written)} Token`, `Session ${kTok(u.read + u.written)} tokens`));
      tips.push(L(`Sitzung (dieser Chat): ${u.read} Token gelesen, ${u.written} geschrieben, ${u.cached} aus dem Cache übernommen, ${u.requests} Anfragen ans Modell.`,
                  `Session (this chat): ${u.read} tokens read, ${u.written} written, ${u.cached} reused from the cache, ${u.requests} model requests.`));
    }
    el.textContent = parts.length ? parts.join(" · ") : hintText;
    el.title = parts.length ? [...tips, "", hintText].join("\n") : "";
  }

  function showContext(c, live = false) {
    if (!c || !c.window) return;
    if (live) S.ctxLive = c;
    else {  // neuer Stand vom Server: ab hier zählen die gestreamten Token neu
      S.ctx = c;
      S.ctxLive = null;
      T.streamed = 0;
      if (c.usage && !c.routine) S.usage = c.usage;
      if (!lastMem) loadCtxMemory();
    }
    renderTokenLine();
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
  $("tele-ctx").onclick = async () => {
    if (!S.connected) return;
    if (await ask(L("Chat jetzt zusammenfassen? Der volle Verlauf bleibt sichtbar; das Modell arbeitet danach mit der Zusammenfassung weiter.",
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

  // Mehrere Grafikkarten: in der Kachel je Karte eine Zeile mit Balken (rows = null → normale Kachel)
  function setDual(key, rows) {
    const el = $("tele-" + key);
    if (!el) return;
    let box = el.querySelector(".tele-dual");
    el.classList.toggle("dual", !!rows);
    if (!rows) { if (box) box.remove(); return; }
    if (!box || box.children.length !== rows.length) {
      if (box) box.remove();
      box = document.createElement("div");
      box.className = "tele-dual";
      box.innerHTML = rows.map(() => `<div class="td-row"><span class="td-l"></span><span class="td-bar"><i></i></span><span class="td-v"></span></div>`).join("");
      el.insertBefore(box, el.querySelector(".tele-sub"));
    }
    rows.forEach((r, i) => {
      const row = box.children[i];
      row.querySelector(".td-l").textContent = r.label;
      row.querySelector(".td-v").textContent = r.text;
      row.querySelector(".td-bar i").style.width = `${Math.min(100, Math.max(0, r.pct || 0))}%`;
      row.classList.toggle("warn", r.pct >= 80 && r.pct < 95);
      row.classList.toggle("crit", r.pct >= 95);
    });
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
    const color = cssVar(tile.classList.contains("crit") ? "--danger" : tile.classList.contains("warn") ? "--warn" : "--accent");
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
    S.metrics = m;
    if (openSheetName === "models") updateGpuChip();
    const g = m.gpu;
    const gb = (b) => (b === null || b === undefined ? null : b / 1024 ** 3);
    if (g) {
      // mehrere Karten: zusammengefasst, im Tooltip jede einzeln
      const each = (g.gpus || []).map((c, i) => `GPU ${i} · ${c.name}: ${fmt(c.util, 0)} % · ${fmt(gb(c.vram_used))}/${fmt(gb(c.vram_total))} GB`
        + (c.temp !== null && c.temp !== undefined ? ` · ${fmt(c.temp, 0)} °C` : "")).join("\n");
      const title = each ? `${g.name}\n${each}` : g.name;
      setTile("gpu", g.util, { pct: g.util, digits: 0, sub: g.temp !== null && g.temp !== undefined ? `${fmt(g.temp, 0)} °C` : "", title });
      const vp = g.vram_total ? (100 * g.vram_used) / g.vram_total : null;
      setTile("vram", gb(g.vram_used), { pct: vp, sub: `${L("von", "of")} ${fmt(gb(g.vram_total))} GB`, title });
      setTile("power", g.power, { digits: 0, sub: g.name ? g.name.replace(/^(NVIDIA|AMD)\s+/i, "") : "", title: g.name });
      const cards = g.gpus && g.gpus.length > 1 ? g.gpus : null;
      setDual("gpu", cards && cards.map((c, i) => ({ label: `GPU ${i + 1}`, pct: c.util, text: `${fmt(c.util, 0)} %` })));
      setDual("vram", cards && cards.map((c, i) => {
        const pct = c.vram_total ? (100 * c.vram_used) / c.vram_total : null;
        return { label: `GPU ${i + 1}`, pct, text: `${fmt(gb(c.vram_used), 1)}/${fmt(gb(c.vram_total), 0)}` };
      }));
      pushSpark("gpu", g.util);
      pushSpark("vram", gb(g.vram_used));
      pushSpark("power", g.power);
    } else {
      setTile("gpu", null, { sub: L("keine GPU", "no GPU"), title: L("keine GPU erkannt", "no GPU detected") });
      setDual("gpu", null);
      setDual("vram", null);
      setTile("vram", null);
      setTile("power", null);
    }
    if (m.ram) {
      setTile("ram", gb(m.ram.used), { pct: (100 * m.ram.used) / m.ram.total,
        sub: `${L("von", "of")} ${fmt(gb(m.ram.total))} GB` + (m.cpu !== null && m.cpu !== undefined ? ` · CPU ${fmt(m.cpu, 0)} %` : "") });
      pushSpark("ram", gb(m.ram.used));
    }
    showBraveUsage((m.usage || {}).brave);
  }

  // Brave Search API: Kontingent laut Brave (Stand der letzten Antwort) – bei Guthaben-Tarifen meldet Brave keinen
  // Verbrauch, dann verweist die Kachel aufs Brave-Dashboard
  let braveWarned = false;
  let braveDashboard = null;
  $("tele-brave").addEventListener("click", () => { if (braveDashboard) window.open(braveDashboard, "_blank", "noopener"); });
  function showBraveUsage(b) {
    const el = $("tele-brave");
    el.hidden = !b;
    if (!b) return;
    const day = (t) => new Date(t * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short" });
    const time = b.at ? new Date(b.at * 1000).toLocaleString(LOCALE, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
    const blocked = b.blocked_until ? L(`aufgebraucht – wieder ab ${day(b.blocked_until)}`, `used up – again from ${day(b.blocked_until)}`) : "";
    braveDashboard = b.plan === "credit" ? b.dashboard : null;
    el.classList.toggle("link", !!braveDashboard);
    const fallback = L("Ist das Kontingent aufgebraucht, sucht Orbwise mit search_fallback über die Suchseiten weiter, sonst bis zur Freigabe nicht.",
      "Once the quota is used up, Orbwise searches via result pages if search_fallback is on, otherwise not until it resets.");
    if (b.plan === "quota") {
      const reset = b.reset_at ? L(` · neu ab ${day(b.reset_at)}`, ` · resets ${day(b.reset_at)}`) : "";
      setTile("brave", b.used, { pct: (100 * b.used) / b.limit, digits: 0,
        sub: blocked || L(`von ${num(b.limit)} · noch ${num(b.remaining)}`, `of ${num(b.limit)} · ${num(b.remaining)} left`) + reset,
        title: [L(`Verbraucht laut Brave: ${num(b.used)} von ${num(b.limit)} Anfragen diesen Monat.`,
                  `Used according to Brave: ${num(b.used)} of ${num(b.limit)} requests this month.`),
                time ? L(`Stand der letzten Anfrage: ${time}.`, `As of the last request: ${time}.`) : "", fallback].filter(Boolean).join("\n") });
      if (b.remaining < 0.1 * b.limit && !braveWarned) {
        braveWarned = true;
        toast(L(`Brave-Suche: nur noch ${num(b.remaining)} von ${num(b.limit)} Anfragen diesen Monat.`,
                `Brave search: only ${num(b.remaining)} of ${num(b.limit)} requests left this month.`));
      }
    } else if (b.plan === "credit") {
      setTile("brave", null, { sub: blocked || L("Guthaben-Tarif · Stand im Brave-Dashboard ↗", "Credit plan · balance in the Brave dashboard ↗"),
        title: [L("Brave meldet bei Guthaben-Tarifen keinen Verbrauch – Guthaben und Kosten stehen im Brave-Dashboard (Klick öffnet es).",
                  "Brave reports no usage for credit plans – balance and costs are in the Brave dashboard (click to open)."), fallback].join("\n") });
    } else {
      setTile("brave", null, { sub: blocked || L("noch keine Antwort von Brave", "no answer from Brave yet"),
        title: L("Das Kontingent zeigt Brave mit jeder Antwort – nach der ersten Suche steht es hier.",
                 "Brave reports the quota with every answer – it appears here after the first search.") });
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
    if (streaming && S.ctx && T.streamed) {  // Kontext wächst mit dem, was gerade gestreamt wird (auch Gedanken)
      const base = S.ctx.real || S.ctx.tokens || S.ctx.used || 0;
      showContext({ ...S.ctx, real: base + T.streamed, tokens: null }, true);
    }
  }, 500);
  setTile("tps", null, { sub: L("wartet auf Antwort", "waiting for an answer") });

  // ---------------------------------------------------------------- Kontext im Speicher (VRAM/RAM) + Größe einstellen
  // Zeigt, wie viel der KV-Cache belegt und ob er im Grafikspeicher liegt – und wie groß das Kontextfenster werden
  // darf (llama-server: genau aus seinem Log, Ollama: geschätzt).
  const gbs = (b) => `${(b / 1024 ** 3).toLocaleString(LOCALE, { maximumFractionDigits: b < 1024 ** 3 ? 2 : 1 })} GB`;
  let lastMem = null;
  const ctxLabel = (n) => (n >= 1048576 && n % 1048576 === 0 ? `${n / 1048576}M` : n % 1024 === 0 ? `${n / 1024}k` : kTok(n));
  async function loadCtxMemory() {
    try { lastMem = await getJSON("/api/llm/memory"); } catch { return; }
    showMemory(lastMem);
    if (openSheetName === "models") updateDetailMemory();
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
    renderTokenLine();
    const box = $("tele-mem");
    if (!box) return;  // Kachel „Kontext im Speicher“ gibt es nicht mehr – die Zeile unter der Eingabe zeigt es
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
      m.source === "estimate" ? L("KV-Cache aus der Modell-Architektur geschätzt.", "KV cache estimated from the model architecture.")
        : L("Genau laut llama-server-Log.", "Exact, from the llama-server log."),
      m.flash_attn ? L(`Flash-Attention ${m.flash_attn === "on" ? "aktiv" : "aus"}.`, `Flash attention ${m.flash_attn === "on" ? "on" : "off"}.`) : "",
      L("Ändern: Modelle (Seitenleiste) → Kontextfenster.", "Change it: Models (sidebar) → Context window."),
    ].filter(Boolean).join("\n");
  }
  setInterval(() => { if (S.connected && (app.classList.contains("drawer-open") || openSheetName === "models")) loadCtxMemory(); }, 10000);

  // ---------------------------------------------------------------- Modellseite (wie LM Studio)
  // Links die eigenen Modelle, rechts alles zum gewählten: laden, Speicher, Kontextfenster, KV-Cache, Sampling,
  // Diagnose. Zweiter Reiter: neue Modelle von Hugging Face, aus ~/models oder der Vorauswahl.
  const MP = { tab: "mine", sel: null, data: null, ctxMode: null };
  const FIT_MARKS = { ok: "✔", tight: "~", big: "✘" };
  const fitText = (fit) => ({ ok: L("passt in den Grafikspeicher", "fits in VRAM"),
                              tight: L("teils im RAM (langsamer)", "partly in RAM (slower)"),
                              big: L("zu groß für den Grafikspeicher", "too big for VRAM") })[fit] || "";
  const modelCtx = (p, mode) => {
    const tools = p.active && S.mode !== "coding" ? p.num_ctx : (p.ctx_tools || p.num_ctx);
    if (mode !== "coding") return tools;
    return p.active && S.mode === "coding" ? p.num_ctx : (p.ctx_coding || tools);
  };

  function openModelsPage(tab) {
    if (tab) MP.tab = tab;
    openSheet("models");
  }
  async function renderModelsPage() {
    try { MP.data = await getJSON("/api/models"); } catch { toast(L("Modelle nicht ladbar", "Could not load models")); return; }
    renderModelsHead();
    renderPulls();
    document.querySelectorAll("#mp-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.mtab === MP.tab));
    $("mp-mine").classList.toggle("hidden", MP.tab !== "mine");
    $("mp-add").classList.toggle("hidden", MP.tab !== "add");
    $("mp-api").classList.toggle("hidden", MP.tab !== "api");
    if (MP.tab === "mine") { renderModelList(); renderModelDetail(); }
    else if (MP.tab === "api") renderApiTab();
    else if (!$("mp-add").childElementCount) renderAddTab();
  }
  document.querySelectorAll("#mp-tabs button").forEach((b) => {
    b.onclick = () => { MP.tab = b.dataset.mtab; renderModelsPage(); };
  });
  // nach Wechsel, Download oder Änderung neu zeichnen – Eingaben im Hinzufügen-Reiter bleiben stehen
  function refreshModelViews() {
    if (openSheetName === "models") renderModelsPage();
  }

  // Kopf: Grafikkarte mit Belegung (live aus den Metriken) und llama.cpp-Version
  function renderModelsHead() {
    updateGpuChip();
    renderLlamacppChip();
  }
  function updateGpuChip() {
    const g = (MP.data && MP.data.gpu) || {};
    const live = S.metrics && S.metrics.gpu;
    const box = $("mp-gpu");
    if (!g.vendor || g.vendor === "none") { box.textContent = L("keine GPU erkannt – Modelle laufen auf der CPU", "no GPU detected – models run on the CPU"); }
    else {
      box.innerHTML = `<svg class="i"><use href="#i-chip"/></svg><span class="mg-name"></span><span class="mg-bar"><i></i></span><span class="mg-val"></span>`;
      box.querySelector(".mg-name").textContent = g.name || g.vendor.toUpperCase();
      const used = live && live.vram_used != null ? live.vram_used / 1024 ** 3 : null;
      const total = live && live.vram_total ? live.vram_total / 1024 ** 3 : g.vram_gb;
      box.querySelector(".mg-bar i").style.width = used != null && total ? `${Math.min(100, (100 * used) / total)}%` : "0";
      box.querySelector(".mg-val").textContent = used != null
        ? `${fmt(used, 1)} / ${fmt(total, 0)} GB` : `${fmt(total, 0)} GB VRAM`;
      box.title = L("Grafikspeicher: belegt / gesamt", "Video memory: used / total");
    }
  }
  async function renderLlamacppChip(force) {
    const box = $("mp-runtime");
    let st;
    try { st = await getJSON("/api/llamacpp"); } catch { box.textContent = ""; return; }
    drawRuntime(box, st, false);
    try { drawRuntime(box, await getJSON("/api/llamacpp?check=1" + (force ? "&force=1" : "")), true); } catch { /* offline */ }
  }
  // llama.cpp-Build (aktualisierbar) und ROCm (nur Anzeige – Systempaket mit Treibern, das aktualisierst du selbst)
  function drawRuntime(box, st, checked) {
    box.innerHTML = `<span class="rt-ver"></span><div class="seg rt-seg"></div><button type="button" class="mm-link rt-btn"></button><span class="rt-rocm"></span>`;
    const switchable = st.installed && (st.flavors || []).length > 1;
    box.querySelector(".rt-ver").textContent = st.installed ? `llama.cpp ${st.tag || ""}` + (st.flavor && !switchable ? ` · ${st.flavor}` : "")
      : L("llama.cpp nicht installiert", "llama.cpp not installed");
    const seg = box.querySelector(".rt-seg");
    if (!switchable) seg.remove();
    for (const f of switchable ? st.flavors : []) {  // Schalter ROCm/Vulkan (bzw. CUDA/Vulkan) für alle llama.cpp-Modelle
      const b = document.createElement("button");
      b.type = "button"; b.textContent = FLAVOR_LABEL[f.flavor] || f.flavor;
      b.classList.toggle("on", f.flavor === st.flavor);
      b.disabled = !!st.updating;
      b.title = f.flavor === st.flavor ? L("Aktiver Build", "Active build")
        : f.ready ? L("Umschalten – ein geladenes Modell startet damit neu.", "Switch – a loaded model restarts with it.")
        : L("Umschalten – der Build wird zuerst geladen (einige hundert MB).", "Switch – the build is downloaded first (a few hundred MB).");
      b.onclick = () => switchFlavor(f, st);
      seg.appendChild(b);
    }
    box.title = L("Startet Modelle von Hugging Face und aus ~/models. Neue Modelltypen brauchen oft einen neuen Build.",
                  "Runs models from Hugging Face and ~/models. New model types often need a newer build.");
    const btn = box.querySelector(".rt-btn");
    const upToDate = checked && st.installed && st.latest && !st.update;
    btn.textContent = st.updating ? L("Wird geladen …", "Downloading …")
      : !st.installed ? L("Installieren", "Install")
      : st.update ? L(`Update auf ${st.latest}`, `Update to ${st.latest}`)
      : upToDate ? L("✓ aktuell", "✓ up to date") : L("Aktualisieren", "Update");
    btn.classList.toggle("rt-new", !!st.update);
    btn.disabled = !!st.updating;
    btn.title = upToDate ? L("Neueste Version – klicken, um erneut nachzusehen.", "Latest version – click to check again.") : "";
    btn.onclick = async () => {
      if (upToDate) { btn.disabled = true; await renderLlamacppChip(true); return; }
      btn.disabled = true;
      btn.textContent = L("Wird geladen …", "Downloading …");
      try { await api("POST", "/api/llamacpp/update"); renderPulls(); } catch { btn.disabled = false; }
    };
    const r = st.rocm, rb = box.querySelector(".rt-rocm");
    if (!r || !r.installed) { rb.remove(); return; }
    rb.innerHTML = `<span></span>`;
    rb.firstChild.textContent = `ROCm ${r.installed}`;
    rb.title = L("ROCm ist ein Systempaket mit Treibern – Orbwise aktualisiert es nicht selbst. Nach einem ROCm-Update hier llama.cpp aktualisieren, dann passt der Build wieder.",
                 "ROCm is a system package with drivers – Orbwise does not update it itself. After a ROCm update, update llama.cpp here so the build matches again.");
    if (r.mismatch) {
      const w = document.createElement("span");
      w.className = "rt-warn";
      w.textContent = L(` · Build für ROCm ${r.build}`, ` · build for ROCm ${r.build}`);
      rb.appendChild(w);
    }
    if (r.newer && r.latest) {
      const a = document.createElement("a");
      a.className = "mm-link"; a.href = r.guide; a.target = "_blank"; a.rel = "noopener";
      a.textContent = L(`neu: ${r.latest} – Anleitung`, `new: ${r.latest} – guide`);
      rb.appendChild(a);
    }
  }

  async function switchFlavor(f, st) {
    if (f.flavor === st.flavor) return;
    const name = FLAVOR_LABEL[f.flavor] || f.flavor;
    if (!f.ready && !await ask(L(`Den ${name}-Build von llama.cpp ${st.tag} laden (einige hundert MB) und alle llama.cpp-Modelle darauf umstellen? Ein geladenes Modell startet danach neu.`,
                                  `Download the ${name} build of llama.cpp ${st.tag} (a few hundred MB) and switch all llama.cpp models to it? A loaded model restarts afterwards.`))) return;
    try {
      const r = await api("POST", "/api/llamacpp/flavor", { flavor: f.flavor });
      if (r.job) { renderPulls(); toast(L(`${name}-Build wird geladen …`, `Downloading the ${name} build …`)); }
      else toast(r.restart ? L(`✔ ${name} aktiv – das Modell startet damit neu.`, `✔ ${name} active – the model restarts with it.`)
                           : L(`✔ ${name} aktiv – gilt ab dem nächsten Modellstart.`, `✔ ${name} active – used from the next model start.`));
    } catch { /* Fehler zeigt api() */ }
    renderLlamacppChip();
  }

  // laufende Downloads stehen über beiden Reitern
  async function renderPulls() {
    const box = $("mp-pulls");
    let pulls = (MP.data && MP.data.pulls) || [];
    if (MP.data && MP.data.active === "demo") pulls = [];
    box.innerHTML = "";
    for (const d of pulls) box.appendChild(pullRow(d));
    box.classList.toggle("hidden", !pulls.length);
  }

  // ---- Reiter „Meine Modelle“: Liste
  function renderModelList() {
    const d = MP.data;
    const list = $("mp-list");
    list.innerHTML = "";
    if (!MP.sel || !d.profiles.some((p) => p.name === MP.sel)) MP.sel = (d.profiles.find((p) => p.active) || d.profiles[0] || {}).name;
    for (const p of d.profiles) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "mp-row" + (p.name === MP.sel ? " sel" : "") + (p.active ? " active" : "") + (p.loaded === false && p.active ? " unloaded" : "");
      row.innerHTML = `<i class="mp-dot"></i><span class="mp-main"><span class="mp-name"></span><span class="mp-meta"></span></span><span class="mp-tag"></span>`;
      row.querySelector(".mp-name").textContent = p.label;
      row.querySelector(".mp-meta").textContent = [p.engine, p.quant, p.size_gb ? `${fmt(p.size_gb, 1)} GB` : "",
                                                   p.num_ctx ? ctxLabel(modelCtx(p, "tools")) : ""].filter(Boolean).join(" · ");
      const tag = row.querySelector(".mp-tag");
      if (p.name === d.switching) { tag.textContent = L("Lädt …", "Loading …"); row.classList.add("loading"); }
      else if (p.active) tag.textContent = p.loaded === false ? L("Entladen", "Unloaded") : L("Geladen", "Loaded");
      else if (p.fit) { tag.textContent = FIT_MARKS[p.fit]; tag.className = "mp-tag fit-" + p.fit; tag.title = fitText(p.fit); }
      row.onclick = () => {
        MP.sel = p.name;
        list.querySelectorAll(".mp-row").forEach((r) => r.classList.toggle("sel", r === row));
        $("mp-detail").scrollTop = 0;
        renderModelDetail();
      };
      row.ondblclick = () => activateModel(p);
      list.appendChild(row);
    }
    if (d.active !== "demo") {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "mp-add-btn";
      add.innerHTML = `<svg class="i"><use href="#i-plus"/></svg><span></span>`;
      add.querySelector("span").textContent = L("Modell hinzufügen", "Add model");
      add.onclick = () => { MP.tab = "add"; renderModelsPage(); };
      list.appendChild(add);
    }
  }

  // Aktiv und wirklich geladen (von Hand entladen: Änderungen gelten erst beim nächsten Laden)
  const isLive = (p) => p.active && p.loaded !== false;
  const restartNote = (p) => (isLive(p) ? (p.managed ? L("Zum Ändern startet der Modell-Server neu.", "Changing it restarts the model server.")
                                                     : L("Zum Ändern lädt Ollama das Modell neu.", "Changing it makes Ollama reload the model."))
                                        : L("Gilt ab dem nächsten Laden.", "Applies the next time it is loaded."));

  // Erklärtext: das Wichtigste sichtbar, der Rest hinter „Mehr“
  function hint(el, short, more) {
    el.textContent = short;
    if (!more) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn-link hint-more";
    b.textContent = L("Mehr", "More");
    b.onclick = () => b.replaceWith(document.createTextNode(more));
    el.append(" ", b);
  }

  async function unloadModel() {
    try { await api("POST", "/api/models/unload"); } catch { /* Meldung kommt von api() */ }
    refreshModelViews();
  }

  async function activateModel(p) {
    if ((p.active && p.loaded !== false) || (MP.data && MP.data.switching)) return;
    if (p.fit === "big" && !await ask(L(`${p.label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem laden?`,
                                      `${p.label} is too big for your video memory and will be very slow. Load anyway?`))) return;
    MP.data.switching = p.name;
    if (openSheetName === "models") { renderModelList(); renderModelDetail(); }
    const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/activate`, { method: "POST" });
    if (!r.ok && r.status !== 502) toast(L("Umschalten fehlgeschlagen", "Switching failed"));
    refreshModelViews();
  }

  // ---- Reiter „Meine Modelle“: Details zum gewählten Modell
  function renderModelDetail() {
    const d = MP.data;
    const box = $("mp-detail");
    const p = d.profiles.find((x) => x.name === MP.sel);
    if (!p) { box.innerHTML = ""; return; }
    const demo = d.active === "demo";
    box.innerHTML = `
      <div class="md-head">
        <div class="md-title"><h3></h3><span class="md-badge"></span></div>
        <div class="md-actions"><button type="button" class="btn small md-unload" hidden></button><button type="button" class="btn primary small md-load"></button>
          <button type="button" class="icon-btn md-more" aria-label="${L("Weitere Aktionen", "More actions")}"><svg class="i"><use href="#i-more"/></svg></button></div>
      </div>
      <div class="md-sub"></div>
      <dl class="md-facts"></dl>
      <div class="md-mem hidden"><div class="md-mem-head"><b></b><span></span></div><div class="tm-bar"><i class="vram"></i><i class="ram"></i></div></div>
      <section class="md-sec md-ctx"><div class="md-sec-head"><h4></h4>
        <div class="seg md-mode"><button type="button" data-m="tools">Tools</button><button type="button" data-m="coding">Coding</button></div></div>
        <div class="ctx-now"></div><div class="ctx-opts"></div><p class="set-hint"></p></section>
      <section class="md-sec md-gpus"><div class="md-sec-head"><h4></h4></div><div class="ctx-opts gpu-mode"></div><div class="gpu-rows"></div><p class="set-hint"></p></section>
      <section class="md-sec md-perf"><div class="md-sec-head"><h4></h4></div><div class="batch-stats"></div><div class="tune-slot"></div><p class="set-hint"></p></section>
      <section class="md-sec md-smp"><div class="md-sec-head"><h4></h4></div><div class="smp"></div></section>
      <details class="md-adv"><summary></summary>
        <section class="md-sec md-kv"><div class="md-sec-head"><h4></h4></div><div class="ctx-opts kv-opts"></div><p class="set-hint kv-hint"></p></section>
        <section class="md-sec md-batch"><div class="md-sec-head"><h4></h4></div><div class="ctx-opts batch-opts"></div><p class="set-hint"></p></section>
        <section class="md-sec md-spec"><div class="md-sec-head"><h4></h4></div><div class="ctx-opts spec-opts"></div><div class="spec-stats"></div><p class="set-hint"></p></section>
      </details>
      <section class="md-sec md-diag"><div class="md-sec-head"><h4></h4></div>
        <div class="md-btns"><button type="button" class="btn small ctx-test-btn"></button><button type="button" class="btn small md-log-btn"></button></div>
        <div class="ctx-test"></div><pre class="md-log hidden"></pre></section>`;
    box.querySelector("h3").textContent = p.label;
    const badge = box.querySelector(".md-badge");
    const loaded = p.active && p.loaded !== false;
    badge.textContent = p.name === d.switching ? L("lädt …", "loading …") : loaded ? L("geladen", "loaded")
      : p.active ? L("entladen", "unloaded") : "";
    badge.hidden = !badge.textContent;
    badge.classList.toggle("off", p.active && !loaded && p.name !== d.switching);
    const load = box.querySelector(".md-load");
    load.textContent = p.name === d.switching ? L("Lädt …", "Loading …") : loaded ? L("Geladen", "Loaded") : L("Laden", "Load");
    load.disabled = loaded || !!d.switching;
    load.onclick = () => activateModel(p);
    const unload = box.querySelector(".md-unload");
    unload.textContent = L("Entladen", "Unload");
    unload.title = L("Aus dem Grafikspeicher nehmen – die nächste Frage lädt es wieder", "Free the video memory – the next question loads it again");
    unload.hidden = !loaded || demo || p.name === d.switching;
    unload.disabled = !!d.switching;
    unload.onclick = unloadModel;
    const more = box.querySelector(".md-more");
    const items = [];
    if (p.repo) items.push({ icon: "download", label: L("Auf Hugging Face ansehen", "View on Hugging Face"),
                             run: () => window.open(`https://huggingface.co/${p.repo}`, "_blank", "noopener") });
    const only = p.active && d.profiles.length < 2;
    if (p.deletable) items.push({ icon: "trash", danger: true, disabled: only || !!d.switching,
                                  label: p.keeps_files ? L("Aus der Liste entfernen", "Remove from list") : L("Löschen", "Delete"),
                                  title: only ? L("Das einzige Modell – erst ein anderes hinzufügen", "The only model – add another one first") : "",
                                  run: () => deleteModel(p) });
    more.hidden = !items.length;
    more.onclick = (e) => { e.stopPropagation(); popMenu(more, items); };
    box.querySelector(".md-sub").textContent = p.path || (p.backend === "ollama" ? `Ollama · ${p.model}` : p.model);
    const facts = [
      [L("Engine", "Engine"), p.engine],
      [L("Quantisierung", "Quantization"), p.quant],
      [L("Größe", "Size"), p.size_gb ? `${fmt(p.size_gb, 1)} GB` : null],
      [L("Braucht", "Needs"), p.vram_gb ? `~${fmt(p.vram_gb, 1)} GB VRAM` : null, p.fit],
      [L("Kontext", "Context"), p.num_ctx ? ctxLabel(modelCtx(p, "tools")) + (p.ctx_coding ? ` · Coding ${ctxLabel(p.ctx_coding)}` : "") : null],
      [L("KV-Cache", "KV cache"), p.kv || (p.backend === "ollama" ? L("Ollama-Dienst", "Ollama service") : null)],
      [L("Batch", "Batch"), p.ubatch && p.ubatch !== 512 ? String(p.ubatch) : null],
      [L("Beschleunigung", "Speed-up"), p.spec && p.spec !== "off" ? (p.spec === "mtp" ? "MTP" : L("N-Gramm", "N-gram"))
        : (p.spec_options || []).some((o) => o.mode === "mtp" && o.ok) ? L("MTP möglich", "MTP available") : null],
      [L("Bilder", "Images"), p.vision === undefined ? null : p.vision ? L("ja (Bildmodul)", "yes (vision module)") : L("nein", "no")],
    ];
    const dl = box.querySelector(".md-facts");
    for (const [k, v, fit] of facts) {
      if (!v) continue;
      const wrap = document.createElement("div");
      wrap.innerHTML = "<dt></dt><dd></dd>";
      wrap.querySelector("dt").textContent = k;
      wrap.querySelector("dd").textContent = v;
      if (fit) {
        const s = document.createElement("span");
        s.className = "md-fit fit-" + fit;
        s.textContent = ` ${FIT_MARKS[fit]} ${fitText(fit)}`;
        wrap.querySelector("dd").appendChild(s);
      }
      dl.appendChild(wrap);
    }
    if (!dl.childElementCount) dl.remove();
    if (demo) { box.querySelectorAll(".md-sec, .md-adv").forEach((s) => s.remove()); load.hidden = true; return; }
    updateDetailMemory();
    renderCtxSection(box.querySelector(".md-ctx"), p);
    if (p.managed) renderPerf(box.querySelector(".md-perf"), p);
    else box.querySelector(".md-perf").remove();  // Ollama/fremder Server: Tempo regelt der Dienst selbst
    renderSampling(box.querySelector(".md-smp"), p);
    // Erweitert (eingeklappt): KV-Cache, Batch-Größe von Hand, spekulatives Decoding
    const adv = box.querySelector(".md-adv");
    adv.querySelector("summary").textContent = L("Erweitert", "Advanced");
    adv.open = !!MP.advOpen;
    adv.ontoggle = () => { MP.advOpen = adv.open; };
    renderKvSettings(box.querySelector(".md-kv"), p);
    const gpus = ((MP.data && MP.data.gpu) || {}).gpus || [];
    if (p.managed && p.gpu_split_settable && gpus.length > 1) renderGpus(box.querySelector(".md-gpus"), p, gpus);
    else box.querySelector(".md-gpus").remove();
    if (p.managed) {
      renderBatch(box.querySelector(".md-batch"), p);
      renderSpec(box.querySelector(".md-spec"), p);
    } else {
      box.querySelector(".md-batch").remove();
      box.querySelector(".md-spec").remove();
    }
    renderDiag(box.querySelector(".md-diag"), p);
  }

  // Speicher des geladenen Modells (alle 10 s frisch, ohne den Rest neu zu zeichnen)
  function updateDetailMemory() {
    const box = $("mp-detail").querySelector(".md-mem");
    const p = MP.data && MP.data.profiles.find((x) => x.name === MP.sel);
    if (!box || !p) return;
    const m = p.active && lastMem && lastMem.available && lastMem.profile === p.name ? lastMem : null;
    box.classList.toggle("hidden", !m);
    if (!m) return;
    const share = m.kv_vram_share ?? 1;
    box.querySelector("b").textContent = L(`Im Speicher: Modell ${gbs(m.model_bytes)} + KV-Cache ${gbs(m.kv_bytes)}`,
                                           `In memory: model ${gbs(m.model_bytes)} + KV cache ${gbs(m.kv_bytes)}`);
    box.querySelector(".md-mem-head span").textContent = memText(m) + (m.flash_attn ? ` · Flash-Attention ${m.flash_attn === "on" ? L("an", "on") : L("aus", "off")}` : "");
    box.querySelector(".vram").style.width = `${share * 100}%`;
    box.querySelector(".ram").style.width = `${(1 - share) * 100}%`;
    box.classList.toggle("warn", (m.recommend || {}).verdict === "reduce");
  }

  function renderCtxSection(sec, p) {
    const mode = MP.ctxMode || (S.mode === "coding" ? "coding" : "tools");
    const cur = modelCtx(p, mode);
    sec.querySelector("h4").textContent = L("Kontextfenster", "Context window");
    sec.querySelectorAll(".md-mode button").forEach((b) => {
      b.classList.toggle("on", b.dataset.m === mode);
      b.onclick = () => { MP.ctxMode = b.dataset.m; renderCtxSection(sec, p); };
    });
    const m = p.active && lastMem && lastMem.available && lastMem.profile === p.name ? lastMem : null;
    const opts = (m && m.recommend && m.recommend.options)
      || [4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144].map((c) => ({ ctx: c }));
    const missing = p.active && lastMem && !lastMem.available && lastMem.reason ? lastMem.reason : "";
    sec.querySelector(".ctx-now").textContent = (mode === "coding" && !p.ctx_coding
      ? L(`Coding nutzt das Fenster von Tools: ${num(cur)} Token`, `Coding uses the Tools window: ${num(cur)} tokens`)
      : L(`Aktuell ${num(cur)} Token`, `Currently ${num(cur)} tokens`))
      + (m && recText(m) ? ` · ${recText(m)}` : "")
      + (missing ? L(` · keine Speicherangaben: ${missing}`, ` · no memory data: ${missing}`) : "");
    const box = sec.querySelector(".ctx-opts");
    box.innerHTML = "";
    const busy = !!MP.data.switching;
    const apply = async (ctx) => {
      box.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      const restart = isLive(p) && (mode === "coding") === (S.mode === "coding");
      if (restart) toast(p.managed ? L("Modell-Server startet mit neuem Kontextfenster neu …", "Restarting the model server with the new context window …")
                                   : L("Modell wird mit neuem Kontextfenster geladen …", "Reloading the model with the new context window …"));
      const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/context`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ctx, mode }) });
      if (!r.ok) toast(L("Ändern fehlgeschlagen: ", "Change failed: ") + ((await r.json().catch(() => ({}))).detail || r.status));
      else toast(ctx ? L(`Kontextfenster (${mode === "coding" ? "Coding" : "Tools"}) jetzt ${num(ctx)} Token.`, `Context window (${mode === "coding" ? "Coding" : "Tools"}) now ${num(ctx)} tokens.`)
                     : L("Coding nutzt wieder das Fenster von Tools.", "Coding uses the Tools window again."));
      await loadCtxMemory();
      refreshModelViews();
    };
    if (mode === "coding") {
      const same = document.createElement("button");
      same.type = "button";
      same.className = "ctx-opt" + (p.ctx_coding ? "" : " on");
      same.innerHTML = "<span></span><small></small>";
      same.querySelector("span").textContent = L("wie Tools", "as Tools");
      same.querySelector("small").textContent = ctxLabel(modelCtx(p, "tools"));
      same.disabled = busy;
      same.onclick = () => { if (p.ctx_coding) apply(0); };
      box.appendChild(same);
    }
    for (const o of opts) {
      const b = document.createElement("button");
      b.type = "button";
      const on = o.ctx === cur && (mode !== "coding" || p.ctx_coding);
      b.className = "ctx-opt" + (on ? " on" : "") + (o.fits === false ? " big" : "");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = ctxLabel(o.ctx);
      const lean = o.ctx < 16384 ? L("Sparmodus", "lean mode") : "";
      b.querySelector("small").textContent = [o.kv_bytes ? `KV ${gbs(o.kv_bytes)}` + (o.fits === false ? L(" · zu groß", " · too big") : "") : "", lean]
        .filter(Boolean).join(" · ");
      b.disabled = busy;
      b.onclick = async () => {
        if (on) return;
        if (o.fits === false && !await ask(L(`${ctxLabel(o.ctx)} passt voraussichtlich nicht in den Grafikspeicher – Teile landen im RAM und alles wird deutlich langsamer. Trotzdem?`,
                                          `${ctxLabel(o.ctx)} probably doesn't fit in VRAM – parts move to RAM and everything gets much slower. Continue?`))) return;
        apply(o.ctx);
      };
      box.appendChild(b);
    }
    hint(sec.querySelector(".set-hint"),
      L("Größer = längere Chats bis zum Zusammenfassen, braucht aber mehr Grafikspeicher. ", "Bigger = longer chats before summarising, but needs more VRAM. ")
        + restartNote(p),
      L("Unter 16k arbeitet Orbwise im Sparmodus (weniger Werkzeuge vorab, kürzere Ergebnisse). Coding kann ein eigenes Fenster haben "
        + "(z. B. größer für viel Code) – beim Wechsel zwischen Tools und Coding startet der Modell-Server dann neu.",
        "Below 16k Orbwise runs in lean mode (fewer tools up front, shorter results). Coding can have its own window "
        + "(e.g. larger for lots of code) – switching between Tools and Coding then restarts the model server."));
  }

  // KV-Cache-Stufe: f16 (voll), q8_0 (~halber Speicher), q4_0 (~ein Viertel) – nur bei eigenem llama-server
  const KV_KNOWN = {
    f16: { factor: 2, label: "f16", note: () => L("Standard, volle Qualität", "default, full quality") },
    q8_0: { factor: 34 / 32, label: "q8", note: () => L("~halber Speicher, kaum Verlust", "~half the memory, hardly any loss") },
    q4_0: { factor: 18 / 32, label: "q4", note: () => L("~ein Viertel, leicht ungenauer", "~a quarter, slightly less accurate") },
  };
  function kvLevel(kv) {
    if (KV_KNOWN[kv]) return { kv, ...KV_KNOWN[kv] };
    const bits = Number(((kv.match(/q(\d)/) || [])[1]) || 16);
    return { kv, factor: (bits * 32 + 16) / 8 / 32, label: kv,
             note: () => bits <= 2 ? L("~1/8 Speicher, deutlich ungenauer", "~1/8 of the memory, noticeably less accurate")
                                    : L("~1/6 Speicher, ungenauer", "~1/6 of the memory, less accurate") };
  }
  function renderKvSettings(sec, p) {
    const m = p.active && lastMem && lastMem.available && lastMem.profile === p.name ? lastMem : null;
    const levels = (p.kv_options && p.kv_options.length ? p.kv_options : ["f16", "q8_0", "q4_0"]).map(kvLevel);
    const current = levels.find((x) => x.kv === p.kv) || kvLevel(p.kv || "f16");
    sec.querySelector("h4").textContent = L("KV-Cache", "KV cache");
    const opts = sec.querySelector(".kv-opts");
    for (const lv of levels) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctx-opt" + (p.kv_settable && lv.kv === current.kv ? " on" : "");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = lv.label;
      const size = m && m.kv_bytes ? `KV ${gbs(m.kv_bytes * lv.factor / current.factor)} · ` : "";
      b.querySelector("small").textContent = size + lv.note();
      b.disabled = !p.kv_settable || !!MP.data.switching;
      b.onclick = async () => {
        if (lv.kv === current.kv) return;
        opts.querySelectorAll(".ctx-opt").forEach((x) => { x.disabled = true; });
        if (p.active) toast(L(`Modell-Server startet mit KV-Cache ${lv.label} neu …`, `Restarting the model server with KV cache ${lv.label} …`));
        const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/kv`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kv: lv.kv }) });
        if (!r.ok) toast(L("Ändern fehlgeschlagen: ", "Change failed: ") + ((await r.json().catch(() => ({}))).detail || r.status));
        else toast(L(`KV-Cache jetzt ${lv.label}.`, `KV cache now ${lv.label}.`));
        await loadCtxMemory();
        refreshModelViews();
      };
      opts.appendChild(b);
    }
    const low = levels.some((x) => /^(?:t|i)?q[23](?!\d)/.test(x.kv));
    if (p.kv_settable) {
      hint(sec.querySelector(".kv-hint"),
        L("Kleinerer KV-Cache = mehr Kontext im Grafikspeicher. ", "Smaller KV cache = more context in VRAM. ") + restartNote(p),
        low ? "" : L("2-Bit kann dieser llama-server nicht (das offizielle llama.cpp bietet höchstens 4 Bit).",
                     "2-bit is not supported by this llama-server (official llama.cpp goes down to 4 bits)."));
    } else {
      hint(sec.querySelector(".kv-hint"), L("Bei Ollama gilt die Stufe für den ganzen Dienst.", "With Ollama the level applies to the whole service."),
        L('sudo systemctl edit ollama → Environment="OLLAMA_FLASH_ATTENTION=1" "OLLAMA_KV_CACHE_TYPE=q8_0", dann sudo systemctl restart ollama.',
          'sudo systemctl edit ollama → Environment="OLLAMA_FLASH_ATTENTION=1" "OLLAMA_KV_CACHE_TYPE=q8_0", then sudo systemctl restart ollama.'));
    }
  }

  // Mehrere Grafikkarten: automatisch (llama.cpp verteilt nach freiem Speicher) oder eigene Anteile je Karte
  function renderGpus(sec, p, gpus) {
    sec.querySelector("h4").textContent = L("Grafikkarten", "GPUs");
    const total = gpus.reduce((n, g) => n + (g.vram_gb || 0), 0);
    const own = Array.isArray(p.gpu_split) ? gpus.map((_, i) => p.gpu_split[i] || 0) : null;
    const fair = gpus.map((g) => Math.round((g.vram_gb || 0) * 100 / (total || 1)));
    const shares = (own || fair).slice();
    const mode = sec.querySelector(".gpu-mode"), rows = sec.querySelector(".gpu-rows");
    const send = async (split) => {
      sec.querySelectorAll("button, input").forEach((x) => { x.disabled = true; });
      if (p.active) toast(L("Modell-Server startet mit der neuen Aufteilung neu …", "Restarting the model server with the new split …"));
      try {
        await api("POST", `/api/models/${encodeURIComponent(p.name)}/gpus`, { split });
        toast(split ? L("Eigene Aufteilung gespeichert.", "Custom split saved.")
          : L("Automatische Aufteilung nach freiem Grafikspeicher.", "Automatic split by free VRAM."));
      } catch { /* Meldung kommt von api() */ }
      await loadCtxMemory();
      refreshModelViews();
    };
    const opt = (label, note, on, click) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctx-opt" + (on ? " on" : "");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = label;
      b.querySelector("small").textContent = note;
      b.disabled = !!MP.data.switching;
      b.onclick = click;
      mode.appendChild(b);
    };
    opt(L("Automatisch", "Automatic"), L(`alle Karten · ${total.toFixed(0)} GB zusammen`, `all GPUs · ${total.toFixed(0)} GB together`),
      !own, () => { if (own) send(null); });
    opt(L("Eigene Aufteilung", "Custom split"), L("Anteil je Karte, 0 % = nicht nutzen", "share per GPU, 0 % = don't use"),
      !!own, () => rows.classList.remove("hidden"));
    rows.classList.toggle("hidden", !own);
    gpus.forEach((g, i) => {
      const row = document.createElement("label");
      row.className = "gpu-row";
      row.innerHTML = `<span class="gpu-name"></span><input type="range" min="0" max="100" step="5"><b></b>`;
      row.querySelector(".gpu-name").textContent = `GPU ${g.index} · ${g.name} · ${g.vram_gb} GB`;
      const input = row.querySelector("input"), out = row.querySelector("b");
      input.value = shares[i];
      out.textContent = `${shares[i]} %`;
      input.oninput = () => { shares[i] = Number(input.value); out.textContent = `${shares[i]} %`; };
      rows.appendChild(row);
    });
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn small";
    apply.textContent = L("Übernehmen", "Apply");
    apply.onclick = () => {
      if (!shares.some((x) => x > 0)) { toast(L("Mindestens eine Karte braucht einen Anteil über 0 %.", "At least one GPU needs a share above 0 %.")); return; }
      send(shares);
    };
    rows.appendChild(apply);
    hint(sec.querySelector(".set-hint"),
      L("Das Modell wird auf die Karten verteilt – so passen größere Modelle oder mehr Kontext. ", "The model is split across the GPUs – so bigger models or more context fit. ") + restartNote(p),
      L("Automatisch verteilt llama.cpp die Schichten nach freiem Grafikspeicher. Eigene Anteile helfen, wenn eine Karte "
        + "zusätzlich den Bildschirm treibt (ihr weniger geben) oder eine Karte deutlich langsamer ist. Mit mehr gemeinsamem "
        + "Speicher kannst du oben einen größeren Kontext wählen. Eine einzelne Karte (die andere auf 0 %) ist am schnellsten, "
        + "wenn das Modell allein hineinpasst.",
        "Automatically llama.cpp splits the layers by free VRAM. Custom shares help when one GPU also drives the display "
        + "(give it less) or one GPU is much slower. With more shared memory you can choose a bigger context above. A single "
        + "GPU (the other at 0 %) is fastest when the model fits on it alone."));
  }

  // Batch-Größe (-ub): größer = lange Eingaben (Werkzeuge, Dateien, PDFs) werden schneller eingelesen, kostet VRAM
  function renderBatch(sec, p) {
    sec.querySelector("h4").textContent = L("Einlesen (Batch-Größe)", "Prompt processing (batch size)");
    const big = ((MP.data && MP.data.gpu) || {}).vram_gb >= 16;
    const notes = {
      512: L("Standard", "default"),
      1024: L("schneller, etwas mehr VRAM", "faster, a bit more VRAM") + (big ? L(" · empfohlen", " · recommended") : ""),
      2048: L("am schnellsten bei langen Eingaben, mehr VRAM", "fastest for long inputs, more VRAM"),
    };
    const opts = sec.querySelector(".batch-opts");
    const current = p.ubatch || 512;
    for (const n of [512, 1024, 2048]) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctx-opt" + (p.ubatch_settable && n === current ? " on" : "") + (p.ubatch_settable ? "" : " na");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = String(n);
      b.querySelector("small").textContent = p.ubatch_settable ? notes[n] : L("nur mit llama.cpp", "llama.cpp only");
      b.disabled = !p.ubatch_settable || !!MP.data.switching;
      b.onclick = async () => {
        if (n === current) return;
        opts.querySelectorAll(".ctx-opt").forEach((x) => { x.disabled = true; });
        if (p.active) toast(L(`Modell-Server startet mit Batch-Größe ${n} neu …`, `Restarting the model server with batch size ${n} …`));
        try {
          await api("POST", `/api/models/${encodeURIComponent(p.name)}/ubatch`, { size: n });
          toast(L(`Batch-Größe jetzt ${n} – die Einlese-Geschwindigkeit wird bei der nächsten längeren Antwort neu gemessen.`,
                  `Batch size now ${n} – the prompt speed is measured again with the next longer answer.`));
        } catch { /* Meldung kommt von api() */ }
        await loadCtxMemory();
        refreshModelViews();
      };
      opts.appendChild(b);
    }
    hint(sec.querySelector(".set-hint"),
      L("Größer = lange Eingaben werden schneller eingelesen, kostet etwas Grafikspeicher. ", "Bigger = long inputs are read faster, costs a little VRAM. ") + restartNote(p),
      L("Wie viele Token der Server auf einmal verarbeitet. Spürbar bei langen Eingaben (Werkzeug-Ergebnisse, Dateien, PDFs, erster Start eines Chats), "
        + "kaum bei kurzen Fragen. Der größere Rechenpuffer im Grafikspeicher kann das Kontextfenster verkleinern. Einfacher: oben „Schnellste Einstellung messen“.",
        "How many tokens the server processes at once. Noticeable with long inputs (tool results, files, PDFs, the first message of a chat), "
        + "hardly with short questions. The larger compute buffer can shrink the context window. Easier: “Find the fastest setting” above."));
  }

  // Leistung: Einlese-Tempo, Messen der schnellsten Einstellung (Build × Batch-Größe)
  function renderPerf(sec, p) {
    sec.querySelector("h4").textContent = L("Leistung", "Performance");
    const m = p.active && lastMem && lastMem.available && lastMem.profile === p.name ? lastMem : null;
    sec.querySelector(".batch-stats").textContent = [
      p.prefill_tps ? L(`Einlesen zuletzt ~${num(Math.round(p.prefill_tps))} Token/s`, `Prompt processing last ~${num(Math.round(p.prefill_tps))} tokens/s`)
        : p.active ? L("Noch nicht gemessen – das passiert bei der nächsten längeren Antwort.", "Not measured yet – happens with the next longer answer.") : "",
      p.ubatch ? `Batch ${p.ubatch}` : "",
      m && m.compute_bytes ? L(`Rechenpuffer ${gbs(m.compute_bytes)}`, `compute buffer ${gbs(m.compute_bytes)}`) : "",
    ].filter(Boolean).join(" · ");
    if (p.tunable) renderTuning(sec.querySelector(".tune-slot"), p);
    hint(sec.querySelector(".set-hint"),
      p.tunable ? L("Misst, welcher Build und welche Batch-Größe hier am schnellsten einlesen, und übernimmt das – die Antworten bleiben gleich.",
                    "Measures which build and batch size read prompts fastest here and applies it – the answers stay the same.")
                : L("Tempo des Modell-Servers beim Einlesen.", "Prompt speed of the model server."),
      p.tunable ? L("Verglichen werden der installierte Build und der andere (ROCm ↔ Vulkan bzw. CUDA ↔ Vulkan) mit Batch 512/1024/2048, "
                    + "am Chat-Anfang und bei langem Chat. Dauert 3–6 Minuten, das Modell ist so lange entladen. Am meisten spart weiterhin der Prompt-Cache.",
                    "It compares the installed build with the other one (ROCm ↔ Vulkan or CUDA ↔ Vulkan) at batch 512/1024/2048, "
                    + "at the start of a chat and with a long chat. Takes 3–6 minutes; the model is unloaded meanwhile. The prompt cache still saves the most.") : "");
  }

  // „Schnellste Einstellung messen“: llama-bench über Build (ROCm/Vulkan bzw. CUDA/Vulkan) × Batch-Größe
  const FLAVOR_LABEL = { rocm: "ROCm", vulkan: "Vulkan", cuda: "CUDA", cpu: "CPU" };
  function renderTuning(slot, p) {
    const box = document.createElement("div");
    box.className = "tune-box";
    box.innerHTML = `<div class="md-btns"><button type="button" class="btn small tune-btn"></button></div><div class="tune-res"></div>`;
    slot.appendChild(box);
    const btn = box.querySelector(".tune-btn");
    const running = (MP.data.pulls || []).some((x) => x.tag === "Tuning");
    btn.textContent = running ? L("Misst gerade …", "Measuring …") : p.tuning ? L("Erneut messen", "Measure again") : L("Schnellste Einstellung messen", "Find the fastest setting");
    btn.disabled = running || !!MP.data.switching;
    btn.onclick = async () => {
      if (!await ask(L("Misst mit llama-bench, welcher Build (ROCm/Vulkan bzw. CUDA/Vulkan) und welche Batch-Größe hier am schnellsten einlesen – ohne Einfluss auf die Antworten. "
                     + "Dauert etwa 3–6 Minuten; das Modell wird dafür entladen und danach wieder geladen, Chats warten so lange. Beim ersten Mal wird der zweite Build geladen (einige hundert MB). Starten?",
                     "Uses llama-bench to measure which build (ROCm/Vulkan or CUDA/Vulkan) and which batch size read prompts fastest here – without changing the answers. "
                     + "Takes about 3–6 minutes; the model is unloaded for it and loaded again afterwards, chats wait meanwhile. The first time the second build is downloaded (a few hundred MB). Start?"))) return;
      btn.disabled = true;
      try { await api("POST", `/api/models/${encodeURIComponent(p.name)}/tune`); btn.textContent = L("Misst gerade …", "Measuring …"); }
      catch { btn.disabled = false; }
      refreshModelViews();
    };
    const t = p.tuning;
    const res = box.querySelector(".tune-res");
    if (!t) return;
    const kt = (v) => (v ? num(Math.round(v)) : "–");
    const ok = (t.runs || []).filter((r) => r.pp);
    const isBest = (r) => t.best && r.flavor === t.best.flavor && r.ubatch === t.best.ubatch;
    const isNow = (r) => r.flavor === t.flavor && r.ubatch === t.ubatch;
    const rows = (t.runs || []).map((r) => `<tr class="${isBest(r) ? "best" : ""}"><td>${FLAVOR_LABEL[r.flavor] || r.flavor}</td><td>${r.ubatch}</td>`
      + (r.pp ? `<td>${kt(r.pp_short)}</td><td>${kt(r.pp_deep)}</td><td>${kt(r.tg)}</td>` : `<td colspan="3" class="err"></td>`)
      + `<td>${isNow(r) ? L("aktiv", "active") : ""}</td></tr>`).join("");
    res.innerHTML = `<table class="tune-table"><thead><tr><th>${L("Build", "Build")}</th><th>Batch</th><th>${L("Einlesen", "Prompt")}</th>`
      + `<th>${L(`bei ${ctxLabel(t.depth || 16384)}`, `at ${ctxLabel(t.depth || 16384)}`)}</th><th>${L("Ausgabe", "Output")}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
      + `<p class="set-hint tune-note"></p>`;
    res.querySelectorAll("td.err").forEach((td, i) => { td.textContent = (t.runs.filter((r) => !r.pp)[i] || {}).error || ""; });
    const notes = [];
    if (!ok.length) notes.push(L("Keine Messwerte – Details oben.", "No results – details above."));
    else notes.push(L(`Token/s gemessen am ${new Date(t.at * 1000).toLocaleDateString()}. Batch ${t.ubatch} ist eingestellt`, `Tokens/s measured on ${new Date(t.at * 1000).toLocaleDateString()}. Batch ${t.ubatch} is set`)
                    + (t.applied_ubatch ? L(" (nach der Messung übernommen).", " (applied after measuring).") : "."));
    (t.notes || []).forEach((n) => notes.push(n));
    if (t.quant_hint) notes.push(t.quant_hint);
    res.querySelector(".tune-note").textContent = notes.join(" ");
    if (t.other && t.other.gain_pct >= 10) {
      const name = FLAVOR_LABEL[t.other.flavor] || t.other.flavor;
      const sw = document.createElement("button");
      sw.type = "button";
      sw.className = "btn small primary";
      sw.textContent = L(`${name} übernehmen (+${t.other.gain_pct} % beim Einlesen)`, `Use ${name} (+${t.other.gain_pct} % prompt speed)`);
      sw.onclick = async () => {
        if (!await ask(L(`Auf den ${name}-Build umstellen? Gilt für alle llama.cpp-Modelle; ein geladenes Modell startet neu. Zurück geht es mit „Erneut messen“.`,
                       `Switch to the ${name} build? Applies to all llama.cpp models; a loaded model restarts. Measure again to switch back.`))) return;
        sw.disabled = true;
        try {
          await api("POST", "/api/llamacpp/flavor", { flavor: t.other.flavor });
          toast(L(`llama.cpp nutzt jetzt den ${name}-Build.`, `llama.cpp now uses the ${name} build.`));
        } catch { sw.disabled = false; }
        refreshModelViews();
      };
      box.querySelector(".md-btns").appendChild(sw);
    }
  }

  // Spekulatives Decoding: das Modell schlägt mehrere Token vor, der Server prüft sie auf einmal – schneller antworten
  function renderSpec(sec, p) {
    sec.querySelector("h4").textContent = L("Schneller antworten (spekulatives Decoding)", "Faster answers (speculative decoding)");
    const opts = sec.querySelector(".spec-opts");
    const labels = { off: L("Aus", "Off"), mtp: "MTP", ngram: L("N-Gramm", "N-gram") };
    for (const o of p.spec_options || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctx-opt" + (o.mode === (p.spec || "off") ? " on" : "") + (o.ok ? "" : " na");
      b.innerHTML = "<span></span><small></small>";
      b.querySelector("span").textContent = labels[o.mode] || o.mode;
      b.querySelector("small").textContent = !o.ok ? o.why
        : o.mode === "off" ? L("Standard", "default")
        : o.mode === "mtp" ? (o.layers ? L("im Modell eingebaut · ~1,5–3× schneller", "built into the model · ~1.5–3× faster")
                                       : L("MTP-Datei neben dem Modell · ~1,5–3× schneller", "MTP file next to the model · ~1.5–3× faster"))
        : L("für Code und Wiederholungen · kein Extra-Speicher", "for code and repetition · no extra memory");
      b.title = b.querySelector("small").textContent;
      b.disabled = !o.ok || !!MP.data.switching;
      b.onclick = async () => {
        if (o.mode === (p.spec || "off")) return;
        opts.querySelectorAll(".ctx-opt").forEach((x) => { x.disabled = true; });
        if (p.active) toast(L("Modell-Server startet neu …", "Restarting the model server …"));
        try {
          await api("POST", `/api/models/${encodeURIComponent(p.name)}/spec`, { mode: o.mode });
          toast(o.mode === "off" ? L("Spekulatives Decoding aus.", "Speculative decoding off.")
                                 : L(`${labels[o.mode]} an – die Trefferquote steht hier nach den nächsten Antworten.`,
                                     `${labels[o.mode]} on – the hit rate shows up here after the next answers.`));
        } catch { /* Meldung kommt von api() */ }
        refreshModelViews();
      };
      opts.appendChild(b);
    }
    const stats = sec.querySelector(".spec-stats");
    if (p.active && p.spec && p.spec !== "off") {
      getJSON("/api/models/spec/stats").then((d) => {
        const st = d.stats;
        stats.textContent = st ? L(`Trefferquote zuletzt ${Math.round(st.rate * 100)} % (${num(st.accepted)} von ${num(st.generated)} vorgeschlagenen Token angenommen)`,
                                   `Last hit rate ${Math.round(st.rate * 100)} % (${num(st.accepted)} of ${num(st.generated)} proposed tokens accepted)`)
          : L("Noch keine Trefferquote – sie erscheint nach der nächsten Antwort.", "No hit rate yet – it appears after the next answer.");
        stats.classList.toggle("low", !!st && st.rate < 0.3);
      }).catch(() => {});
    }
    hint(sec.querySelector(".set-hint"),
      L("Schlägt mehrere Token auf einmal vor – gleiche Antwort, nur schneller. ", "Proposes several tokens at once – same answer, just faster. ") + restartNote(p),
      L("MTP nutzt eigene Vorhersage-Schichten des Modells (kostet etwas Grafikspeicher), N-Gramm wiederholt Stellen aus dem Kontext. "
        + "Bei hoher Temperatur oder freiem Text bringt es weniger; unter 30 % Trefferquote lieber aus.",
        "MTP uses the model's own prediction layers (costs a little VRAM), N-gram repeats passages from the context. "
        + "It helps less at high temperature or with free text; below a 30 % hit rate better turn it off."));
  }

  // Sampling je Modell und Modus (mit/ohne Denken) – gilt ab der nächsten Antwort
  const SAMPLING_FIELDS = [
    ["temperature", L("Temperatur", "Temperature"), 0.05], ["top_p", "top_p", 0.05], ["top_k", "top_k", 1],
    ["min_p", "min_p", 0.01], ["presence_penalty", "presence", 0.1], ["repeat_penalty", "repeat", 0.05],
  ];
  async function renderSampling(sec, p) {
    sec.querySelector("h4").textContent = "Sampling";
    let data;
    try { data = await getJSON(`/api/models/${encodeURIComponent(p.name)}/sampling`); } catch { return; }
    const wrap = sec.querySelector(".smp");
    wrap.innerHTML = `<div class="smp-cols"></div><div class="smp-btns"></div><p class="set-hint"></p>`;
    const cols = wrap.querySelector(".smp-cols");
    const inputs = { think: {}, fast: {} };
    for (const [mode, title] of [["think", L("Mit Denken", "Thinking")], ["fast", L("Ohne Denken", "Non-thinking")]]) {
      const col = document.createElement("div");
      col.className = "smp-col";
      col.innerHTML = `<b></b>`;
      col.querySelector("b").textContent = title;
      for (const [key, label, step] of SAMPLING_FIELDS) {
        const row = document.createElement("label");
        row.className = "smp-row";
        row.innerHTML = `<span></span><input type="number">`;
        row.querySelector("span").textContent = label;
        const inp = row.querySelector("input");
        const [lo, hi] = data.limits[key] || [];
        Object.assign(inp, { min: lo, max: hi, step });
        inp.placeholder = key === "temperature" && data.temperature != null ? String(data.temperature) : L("Standard", "default");
        if (data[mode][key] != null) inp.value = data[mode][key];
        inputs[mode][key] = inp;
        col.appendChild(row);
      }
      cols.appendChild(col);
    }
    const btns = wrap.querySelector(".smp-btns");
    const fill = (sets) => {
      for (const mode of ["think", "fast"]) {
        for (const [key] of SAMPLING_FIELDS) inputs[mode][key].value = sets && sets[mode][key] != null ? sets[mode][key] : "";
      }
    };
    for (const [id, preset] of Object.entries(data.presets || {})) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mm-link";
      b.textContent = preset.label.toUpperCase();
      b.dataset.preset = id;
      b.onclick = () => fill(preset);
      btns.appendChild(b);
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "mm-link";
    reset.textContent = L("Zurücksetzen", "Reset");
    reset.onclick = () => fill(null);
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn small smp-save";
    save.textContent = L("Speichern", "Save");
    save.onclick = async () => {
      const body = { think: {}, fast: {} };
      for (const mode of ["think", "fast"]) {
        for (const [key] of SAMPLING_FIELDS) {
          const v = inputs[mode][key].value.trim();
          if (v !== "") body[mode][key] = Number(v);
        }
      }
      try {
        await api("POST", `/api/models/${encodeURIComponent(p.name)}/sampling`, body);
        toast(L("Sampling gespeichert – gilt ab der nächsten Antwort.", "Sampling saved – applies from the next answer."));
      } catch { /* Meldung kommt von api() */ }
    };
    btns.append(reset, save);
    wrap.querySelector(".set-hint").textContent = L(
      "Welcher Satz gilt, entscheidet der Denken-Knopf im Chat. Leere Felder: Standard des Modells (Temperatur: die des Profils). Kein Neustart nötig.",
      "The think button in the chat decides which set applies. Empty fields: the model's default (temperature: the profile's). No restart needed.");
  }

  // Diagnose: Kontext-Test mit dem geladenen Modell und die Ausgabe des letzten Server-Starts
  function renderDiag(sec, p) {
    sec.querySelector("h4").textContent = L("Diagnose", "Diagnostics");
    const testBtn = sec.querySelector(".ctx-test-btn");
    testBtn.textContent = L("Kontext testen", "Test context");
    testBtn.title = p.active ? L("Prüft mit diesem Modell: Fenstergröße, Speicher, Token-Schätzung, Prompt-Cache und ein volles Fenster (kann einige Minuten dauern).",
                                 "Checks with this model: window size, memory, token estimate, prompt cache and a full window (may take a few minutes).")
      : L("Nur mit dem geladenen Modell möglich.", "Only possible with the loaded model.");
    testBtn.disabled = !p.active || !!MP.data.switching;
    testBtn.onclick = () => runContextTest(sec.querySelector(".ctx-test"), testBtn);
    const logBtn = sec.querySelector(".md-log-btn");
    const pre = sec.querySelector(".md-log");
    logBtn.textContent = L("Server-Log", "Server log");
    logBtn.title = L("Ausgabe des letzten Modell-Server-Starts (llama-server) – zeigt, warum ein Modell nicht lädt.",
                     "Output of the last model server start (llama-server) – shows why a model doesn't load.");
    logBtn.hidden = !p.managed;
    logBtn.onclick = async () => {
      if (!pre.classList.contains("hidden")) { pre.classList.add("hidden"); return; }
      let data;
      try { data = await getJSON("/api/models/log?lines=400"); } catch { return; }
      pre.textContent = data.lines.length ? data.lines.join("\n") : L(`Noch kein Log (${data.file}).`, `No log yet (${data.file}).`);
      pre.classList.remove("hidden");
      pre.scrollTop = pre.scrollHeight;
    };
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

  // Altes Hugging-Face-Modell über Ollama (stürzt bei neuen Modelltypen oft ab) → mit llama.cpp neu laden
  async function deleteModel(p) {
    const size = p.size_gb ? L(` Gibt ~${p.size_gb} GB frei.`, ` Frees ~${p.size_gb} GB.`) : "";
    const text = p.keeps_files ? L(`${p.label} aus der Liste entfernen? Die Datei in ~/models bleibt.`,
                                   `Remove ${p.label} from the list? The file in ~/models stays.`)
      : L(`${p.label} löschen? Die Modelldateien werden entfernt.`, `Delete ${p.label}? The model files are removed.`) + size;
    const next = p.active && (MP.data.profiles.find((x) => x.name !== p.name) || {}).label;
    const active = p.active ? L(`\n\nEs ist gerade ausgewählt: Orbwise entlädt es vorher und wählt ein anderes Modell (z. B. ${next}), das erst bei der nächsten Frage geladen wird.`,
                                `\n\nIt is selected right now: Orbwise unloads it first and selects another model (e.g. ${next}), loaded with the next question.`) : "";
    if (!await ask(text + active)) return;
    try {
      const r = await api("DELETE", `/api/models/${encodeURIComponent(p.name)}`);
      const now = p.active && (MP.data.profiles.find((x) => x.name === r.active) || {}).label;
      toast(L(`✔ ${p.label} gelöscht`, `✔ ${p.label} deleted`) + (now ? L(` – ausgewählt: ${now} (entladen)`, ` – selected: ${now} (unloaded)`) : ""));
      MP.sel = null;
      refreshModelViews();
    } catch { /* Meldung kommt von api() */ }
  }

  // ---- Reiter „Hinzufügen“: Hugging Face (Suche, Link, Pfad), Dateien in ~/models, Vorauswahl
  async function renderAddTab() {
    const box = $("mp-add");
    box.innerHTML = `
      <form class="mp-search" autocomplete="off"><svg class="i"><use href="#i-search"/></svg><input type="search" spellcheck="false">
        <button type="submit" class="btn primary small"></button></form>
      <p class="set-hint mp-search-hint"></p>
      <div class="mm-hf mp-hf"></div>
      <div class="mp-add-cols"><section class="mp-col mp-disk"></section><section class="mp-col mp-presets"></section></div>`;
    const form = box.querySelector("form");
    const q = form.querySelector("input");
    q.placeholder = L("Hugging Face durchsuchen, Link oder Pfad zu einer .gguf einfügen (z. B. qwen3.6 27b)",
                      "Search Hugging Face, paste a link or a path to a .gguf (e.g. qwen3.6 27b)");
    form.querySelector("button").textContent = L("Suchen", "Search");
    const hf = box.querySelector(".mp-hf");
    form.onsubmit = (e) => {
      e.preventDefault();
      const text = q.value.trim();
      if (/^[~/].*\.gguf$/i.test(text)) { addLocal(text); return; }  // Pfad zu einer eigenen GGUF-Datei
      hfSearch(text, hf);
    };
    localModels(box.querySelector(".mp-disk"));
    renderPresets(box.querySelector(".mp-presets"));
  }

  async function renderPresets(box) {
    let data;
    try { data = await getJSON("/api/models/presets"); } catch { box.textContent = L("Vorauswahl nicht ladbar", "Could not load presets"); return; }
    const g = data.gpu;
    const gpuText = g.vendor === "none" ? L("keine GPU erkannt", "no GPU detected") : `${g.name || g.vendor} · ${g.vram_gb} GB`;
    $("mp-add").querySelector(".mp-search-hint").textContent = gpuText + " · "
      + L("✔ passt · ~ teils im RAM (langsamer) · ✘ zu groß. Orbwise lädt die Datei nach ~/models und startet sie mit llama.cpp.",
          "✔ fits · ~ partly in RAM (slower) · ✘ too big. Orbwise downloads the file to ~/models and runs it with llama.cpp.");
    box.innerHTML = `<h4></h4><div class="mm-hf"></div>`;
    box.querySelector("h4").textContent = L("Vorauswahl", "Presets");
    const list = box.querySelector(".mm-hf");
    for (const p of data.presets) {
      const pulling = data.pulling.includes(p.tag);
      const mark = document.createElement("span");
      mark.className = "fit-" + p.fit;
      mark.textContent = FIT_MARKS[p.fit] + " ";
      const b = hfItem([mark, document.createTextNode(p.label)],
                       pulling ? L("Lädt …", "Loading …") : p.installed ? L("Installiert", "Installed") : p.recommended ? L("Empfohlen", "Recommended") : "",
                       `${p.tag} · ~${p.download_gb} GB`, p.note);
      b.disabled = pulling;
      b.onclick = () => startPull(p.tag, p.label, p.fit);
      list.appendChild(b);
    }
  }

  async function startPull(tag, label, fit) {
    if (fit === "big" && !await ask(L(`${label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem laden?`,
                                    `${label} is too big for your video memory and will be very slow. Download anyway?`))) return;
    try {
      await api("POST", "/api/models/pull", { tag });
      toast(L(`Lade ${tag} …`, `Downloading ${tag} …`));
      refreshModelViews();  // Fortschritt erscheint oben
    } catch { /* Meldung kommt von api() */ }
  }

  function hfItem(head, tag, sub, note) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "model-item";
    b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div>
      <div class="mi-sub"></div>` + (note ? `<div class="mi-note"></div>` : "");
    b.querySelector(".mi-name").append(...head);
    b.querySelector(".mi-tag").textContent = tag;
    b.querySelector(".mi-sub").textContent = sub;
    if (note) b.querySelector(".mi-note").textContent = note;
    return b;
  }

  async function hfSearch(text, box) {
    text = text.trim();
    if (!text) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="mm-hint">${L("Suche auf Hugging Face …", "Searching Hugging Face …")}</div>`;
    let data;
    try { data = await getJSON("/api/models/hf/search?q=" + encodeURIComponent(text)); }
    catch { box.innerHTML = `<div class="mm-hint">${L("Hugging Face nicht erreichbar.", "Hugging Face not reachable.")}</div>`; return; }
    box.innerHTML = "";
    if (!data.results.length) {
      box.innerHTML = `<div class="mm-hint">${L("Nichts gefunden – kürzeren Begriff versuchen (z. B. nur „qwen3.6“).",
                                                "Nothing found – try a shorter term (e.g. just “qwen3.6”).")}</div>`;
      return;
    }
    if (data.results.length === 1 && data.results[0].tag) {  // Link auf eine bestimmte Datei
      const r = data.results[0];
      return hfQuants(r.repo, box, r.tag);
    }
    if (data.results.length === 1) return hfQuants(data.results[0].repo, box);
    for (const r of data.results) {
      const b = hfItem([document.createTextNode(r.repo)], r.downloads ? L(`${r.downloads.toLocaleString(LOCALE)} Downloads`, `${r.downloads.toLocaleString(LOCALE)} downloads`) : "",
                       L("GGUF · Quantisierung wählen →", "GGUF · choose quantization →"));
      b.onclick = () => hfQuants(r.repo, box);
      box.appendChild(b);
    }
  }

  async function hfQuants(repo, box, only) {
    box.innerHTML = `<div class="mm-hint">${L("Lade Dateiliste …", "Loading file list …")}</div>`;
    let data;
    try { data = await api("GET", "/api/models/hf/quants?repo=" + encodeURIComponent(repo)); }
    catch { box.innerHTML = ""; return; }
    box.innerHTML = "";
    const title = document.createElement("div");
    title.className = "mp-hf-title";
    title.textContent = repo;
    box.appendChild(title);
    const items = only ? data.quants.filter((x) => x.tag.toLowerCase() === only.toLowerCase()) : data.quants;
    if (!items.length) {
      box.insertAdjacentHTML("beforeend", `<div class="mm-hint">${L("Keine GGUF-Datei in diesem Repo.", "No GGUF file in this repo.")}</div>`);
      return;
    }
    for (const x of items) {
      const mark = document.createElement("span");
      mark.className = "fit-" + x.fit;
      mark.textContent = FIT_MARKS[x.fit] + " ";
      const pulling = data.pulling.includes(x.tag);
      const b = hfItem([mark, document.createTextNode(x.quant)],
                       pulling ? L("Lädt …", "Loading …") : x.installed ? L("Heruntergeladen", "Downloaded")
                         : x.recommended ? L("Empfohlen", "Recommended") : "",
                       `~${x.download_gb} GB · ${L("braucht", "needs")} ~${x.vram_gb} GB VRAM`
                         + (x.parts > 1 ? ` · ${x.parts} ${L("Teile", "parts")}` : "")
                         + (x.mmproj ? L(" · + Bildmodul", " · + vision") : ""));
      b.disabled = pulling;
      b.onclick = () => startPull(x.tag, `${repo} ${x.quant}`, x.fit);
      box.appendChild(b);
    }
  }

  // GGUF-Dateien in ~/models, die noch kein Modell sind: ein Klick legt es an (läuft mit llama.cpp)
  async function localModels(box) {
    let data;
    try { data = await getJSON("/api/models/local"); } catch { return; }
    const free = data.items.filter((x) => !x.profile);
    box.innerHTML = `<h4></h4><div class="mm-hf"></div>`;
    box.querySelector("h4").textContent = L("Auf der Festplatte", "On disk") + ` · ${data.dir}`;
    const list = box.querySelector(".mm-hf");
    if (!free.length) {
      const hint = document.createElement("div");
      hint.className = "mm-hint";
      hint.textContent = data.items.length ? L("Alle GGUF-Dateien dort sind schon Modelle.", "All GGUF files there are models already.")
        : L("Hier abgelegte GGUF-Dateien erscheinen automatisch – oder einen Pfad oben einfügen.",
            "GGUF files placed there show up here – or paste a path above.");
      list.appendChild(hint);
      return;
    }
    for (const x of free) {
      const mark = document.createElement("span");
      mark.className = "fit-" + x.fit;
      mark.textContent = FIT_MARKS[x.fit] + " ";
      const b = hfItem([mark, document.createTextNode(x.label)], data.pulling.includes(x.label) ? L("Lädt …", "Loading …") : "",
                       `${x.rel} · ~${x.size_gb} GB · ${L("braucht", "needs")} ~${x.vram_gb} GB VRAM`
                         + (x.parts > 1 ? ` · ${x.parts} ${L("Teile", "parts")}` : "")
                         + (x.mmproj ? L(" · + Bildmodul", " · + vision") : ""));
      b.onclick = () => addLocal(x.path, x.label, x.fit);
      list.appendChild(b);
    }
  }

  async function addLocal(path, label, fit) {
    if (fit === "big" && !await ask(L(`${label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem hinzufügen?`,
                                    `${label} is too big for your video memory and will be very slow. Add anyway?`))) return;
    try {
      const r = await api("POST", "/api/models/local", { path });
      toast(L(`Lege ${r.tag} an …`, `Adding ${r.tag} …`));
      refreshModelViews();
    } catch { /* Meldung kommt von api() */ }
  }

  function pullPct(d) {
    return d.total ? Math.floor((100 * (d.completed || 0)) / d.total) : null;
  }

  // Laufender Download: Fortschritt live (model_pull-Events) und Abbrechen
  function pullRow(d) {
    const row = document.createElement("div");
    row.className = "model-pull";
    row.dataset.tag = d.tag;
    row.innerHTML = `<div class="mp-head"><span class="mp-text"></span>
      <button class="mp-cancel">${L("Abbrechen", "Cancel")}</button></div><div class="mp-bar"><i></i></div>`;
    row.querySelector(".mp-cancel").onclick = async (e) => {
      e.target.disabled = true;
      try { await api("DELETE", `/api/models/pull/${encodeURIComponent(d.tag)}`); } catch { /* Meldung kommt von api() */ }
    };
    updatePullRow(row, d);
    return row;
  }

  function updatePullRow(row, d) {
    const pct = pullPct(d);
    row.querySelector(".mp-text").textContent = (d.tag === "Tuning" ? L("Messen", "Measuring") : d.tag)
      + (pct !== null ? ` · ${pct} %` : d.status ? ` · ${d.status}` : " …");
    row.querySelector(".mp-bar i").style.width = (pct || 0) + "%";
  }

  function modelPullEvent(ev) {
    const page = openSheetName === "models";
    let row = [...document.querySelectorAll("#mp-pulls .model-pull")].find((r) => r.dataset.tag === ev.tag);
    if (row && (ev.done || ev.error || ev.cancelled)) { row.remove(); $("mp-pulls").classList.toggle("hidden", !$("mp-pulls").childElementCount); }
    else if (row) updatePullRow(row, ev);
    else if (page && !(ev.done || ev.error || ev.cancelled)) {  // Download, der nach dem Öffnen der Seite begann
      row = pullRow(ev);
      $("mp-pulls").appendChild(row);
      $("mp-pulls").classList.remove("hidden");
    }
    if (ev.cancelled) { toast(L(`Download von ${ev.tag} abgebrochen`, `Download of ${ev.tag} cancelled`)); return; }
    if (ev.error) { toast(L(`✘ ${ev.tag}: `, `✘ ${ev.tag}: `) + ev.error); return; }
    if (ev.done && ev.kind === "llamacpp") {
      const v = ev.version || "";
      const fl = ev.flavor && ev.kind === "llamacpp" ? ` (${FLAVOR_LABEL[ev.flavor] || ev.flavor})` : "";
      toast(ev.changed === false ? L(`llama.cpp ${v} ist aktuell.`, `llama.cpp ${v} is up to date.`)
        : ev.restarted ? L(`✔ llama.cpp ${v}${fl} bereit – das Modell startet damit neu.`, `✔ llama.cpp ${v}${fl} ready – the model restarts with it.`)
        : L(`✔ llama.cpp ${v}${fl} bereit – gilt ab dem nächsten Modellstart.`, `✔ llama.cpp ${v}${fl} ready – used from the next model start.`));
      refreshModelViews();
      return;
    }
    if (ev.done && ev.kind === "tuning") {
      const other = FLAVOR_LABEL[ev.other_flavor] || ev.other_flavor;
      toast((ev.applied_ubatch ? L(`✔ Gemessen – Batch-Größe jetzt ${ev.applied_ubatch}.`, `✔ Measured – batch size now ${ev.applied_ubatch}.`)
                               : L("✔ Gemessen – die bisherige Batch-Größe war schon die schnellste.", "✔ Measured – the current batch size was already the fastest."))
            + (ev.gain_pct >= 10 ? L(` ${other} liest ${ev.gain_pct} % schneller ein – Umstellen auf der Modellseite.`,
                                     ` ${other} reads prompts ${ev.gain_pct} % faster – switch on the model page.`) : ""));
      refreshModelViews();
      return;
    }
    if (ev.done) {
      toast(L(`✔ ${ev.tag} geladen – jetzt unter Modelle auswählbar.`, `✔ ${ev.tag} downloaded – now selectable under Models.`));
      addSystem(L(`Modell ${ev.tag} ist bereit (Modelle in der Seitenleiste oder oben).`, `Model ${ev.tag} is ready (Models in the sidebar or at the top).`));
      if (ev.profile) MP.sel = ev.profile;
      if (page) { MP.tab = "mine"; refreshModelViews(); }
      return;
    }
    if (row) return;  // Seite offen: Fortschritt steht dort
    const pct = pullPct(ev) !== null ? ` ${pullPct(ev)} %` : "";
    toast(L(`Lade ${ev.tag}: `, `Downloading ${ev.tag}: `) + (ev.status || "") + pct
      + L(" · abbrechen unter Modelle", " · cancel under Models"));
  }

  // ---- Reiter „API-Server“: OpenAI-kompatible Schnittstelle für andere Programme (wie bei LM Studio)
  async function renderApiTab() {
    let st;
    try { st = await getJSON("/api/apiserver"); } catch { return; }
    const box = $("mp-api");
    const model = MP.data && MP.data.profiles.find((p) => p.active);
    const modelId = model ? model.name : "orbwise";
    box.innerHTML = `
      <div class="api-card">
        <div class="set-row"><div><b></b><small></small></div><button type="button" class="switch api-enable"></button></div>
        <div class="api-field"><span class="api-label"></span><code class="api-url"></code><span class="api-btns"></span></div>
        <div class="api-field"><span class="api-label"></span><code class="api-key"></code><span class="api-btns"></span></div>
        <div class="set-row"><div><b></b><small></small></div><button type="button" class="switch api-switch"></button></div>
      </div>
      <div class="api-status"><i></i><span></span></div>
      <h4 class="api-h"></h4>
      <div class="api-howto"></div>
      <p class="set-hint api-notes"></p>`;
    const rows = box.querySelectorAll(".api-card .set-row");
    rows[0].querySelector("b").textContent = L("API-Server", "API server");
    rows[0].querySelector("small").textContent = L("OpenAI-kompatibel – für VS Code (Continue, Cline), Open WebUI oder eigene Skripte.",
                                                   "OpenAI compatible – for VS Code (Continue, Cline), Open WebUI or your own scripts.");
    rows[1].querySelector("b").textContent = L("Programme dürfen das Modell wechseln", "Programs may switch the model");
    rows[1].querySelector("small").textContent = L("Nennt ein Programm ein anderes deiner Modelle, lädt Orbwise es – auch für deinen Chat. Aus: es antwortet immer das geladene Modell.",
                                                   "If a program names another of your models, Orbwise loads it – for your chat too. Off: the loaded model always answers.");
    const toggle = (btn, on) => { btn.classList.toggle("on", on); btn.textContent = on ? L("An", "On") : L("Aus", "Off"); };
    const save = async (body) => {
      try { await api("POST", "/api/apiserver", body); } catch { return; }
      renderApiTab();
    };
    toggle(box.querySelector(".api-enable"), st.enabled);
    box.querySelector(".api-enable").onclick = () => save({ enabled: !st.enabled });
    toggle(box.querySelector(".api-switch"), st.allow_switch);
    box.querySelector(".api-switch").onclick = () => save({ allow_switch: !st.allow_switch });
    const fields = box.querySelectorAll(".api-field");
    const small = (label, run) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mm-link";
      b.textContent = label;
      b.onclick = run;
      return b;
    };
    fields[0].querySelector(".api-label").textContent = L("Adresse", "Base URL");
    fields[0].querySelector("code").textContent = st.url;
    fields[0].querySelector(".api-btns").append(small(L("Kopieren", "Copy"), () => copyText(st.url)));
    fields[1].querySelector(".api-label").textContent = L("API-Schlüssel", "API key");
    const keyEl = fields[1].querySelector("code");
    const masked = st.key ? st.key.slice(0, 6) + "•".repeat(18) : L("wird beim Einschalten erzeugt", "created when turned on");
    keyEl.textContent = masked;
    if (st.key) {
      const show = small(L("Anzeigen", "Show"), () => {
        const open = keyEl.textContent === st.key;
        keyEl.textContent = open ? masked : st.key;
        show.textContent = open ? L("Anzeigen", "Show") : L("Verbergen", "Hide");
      });
      fields[1].querySelector(".api-btns").append(show, small(L("Kopieren", "Copy"), () => copyText(st.key)),
        small(L("NEU", "NEW"), async () => {
          if (await ask(L("Neuen Schlüssel erzeugen? Programme mit dem alten Schlüssel kommen dann nicht mehr durch.",
                        "Create a new key? Programs using the old key will no longer get through."))) save({ new_key: true });
        }));
    }
    showApiStatus(st);
    box.querySelector(".api-h").textContent = L("So verbindest du ein Programm", "How to connect a program");
    const key = st.key || "ow-…";
    const how = box.querySelector(".api-howto");
    const item = (title, text, code) => {
      const d = document.createElement("div");
      d.className = "api-how";
      d.innerHTML = "<b></b><span></span>" + (code ? "<pre></pre>" : "");
      d.querySelector("b").textContent = title;
      d.querySelector("span").textContent = text;
      if (code) d.querySelector("pre").textContent = code;
      how.appendChild(d);
    };
    item("Continue (VS Code)", L("In config.yaml unter models:", "In config.yaml under models:"),
         `- name: Orbwise\n  provider: openai\n  model: ${modelId}\n  apiBase: ${st.url}\n  apiKey: ${key}`);
    item("Open WebUI", L("Einstellungen → Verbindungen → OpenAI-API: Adresse und Schlüssel von oben. Läuft Open WebUI in Docker, mit --network=host starten (Orbwise ist nur unter 127.0.0.1 erreichbar).",
                         "Settings → Connections → OpenAI API: the URL and key from above. If Open WebUI runs in Docker, start it with --network=host (Orbwise only listens on 127.0.0.1)."));
    item(L("Terminal", "Terminal"), L("Zum Ausprobieren:", "To try it out:"),
         `curl ${st.url}/chat/completions \\\n  -H "Authorization: Bearer ${key}" -H "Content-Type: application/json" \\\n  -d '{"model": "${modelId}", "messages": [{"role": "user", "content": "Hallo"}]}'`);
    box.querySelector(".api-notes").textContent = L(
      "Nur auf diesem Rechner erreichbar. Programme bekommen das Modell ohne Orbwise-Systemprompt, Gedächtnis und Werkzeuge – sie können also nichts am PC tun. "
      + "Orbwise und das Programm teilen sich das Modell: Nach einer API-Anfrage liest Orbwise deinen Chat beim nächsten Mal neu ein (dauert einen Moment). "
      + "Bei Ollama-Modellen kann Ollama das Modell mit anderer Kontextgröße neu laden.",
      "Only reachable on this computer. Programs get the model without the Orbwise system prompt, memory and tools – so they can't do anything on the PC. "
      + "Orbwise and the program share the model: after an API request Orbwise re-reads your chat next time (takes a moment). "
      + "With Ollama models, Ollama may reload the model with a different context size.");
  }
  function showApiStatus(st) {
    const el = $("mp-api").querySelector(".api-status");
    if (!el) return;
    const s = st.stats || {};
    const on = st.enabled && st.available;
    el.className = "api-status" + (s.running ? " busy" : on ? " on" : "");
    const parts = [!st.available ? L("im Demo-Modus nicht verfügbar", "not available in demo mode")
      : !st.enabled ? L("aus", "off") : s.running ? L(`beantwortet gerade ${s.running} Anfrage(n)`, `answering ${s.running} request(s)`) : L("bereit", "ready")];
    if (s.requests) parts.push(L(`${s.requests} Anfrage(n) seit dem Start`, `${s.requests} request(s) since start`));
    if (s.last) parts.push(L("zuletzt ", "last ") + chatWhen(s.last) + (s.last_model ? ` · ${s.last_model}` : "")
      + (s.last_client ? ` · ${s.last_client.split(/[ /]/)[0]}` : ""));
    if (s.last_error) parts.push(L("Fehler: ", "Error: ") + s.last_error);
    el.querySelector("span").textContent = parts.join(" · ");
  }

  // Schnellwahl an der Modell-Pille oben: laden mit einem Klick, alles Weitere auf der Modellseite
  $("pill-llm").onclick = async (e) => {
    e.stopPropagation();
    const anchor = $("pill-llm");
    let data;
    try { data = await getJSON("/api/models"); } catch { openModelsPage("mine"); return; }
    const items = data.profiles.map((p) => ({
      icon: p.active ? "check" : "chip", label: p.label, checked: p.active,
      sub: p.name === data.switching ? L("lädt …", "loading …")
        : [p.engine, p.quant, p.size_gb ? `${fmt(p.size_gb, 1)} GB` : "", p.fit && !p.active ? `${FIT_MARKS[p.fit]}` : ""].filter(Boolean).join(" · "),
      disabled: !!data.switching && !p.active,
      run: () => { if (!p.active) { MP.data = data; activateModel(p); } },
    }));
    const cur = data.profiles.find((p) => p.active);
    if (cur && cur.loaded === true && !data.switching)
      items.push({ sep: true }, { icon: "power", label: L("Modell entladen", "Unload model"),
                                  sub: L("Grafikspeicher freigeben – die nächste Frage lädt es wieder", "Free the video memory – the next question loads it again"),
                                  run: unloadModel });
    else if (cur && cur.loaded === false && !data.switching)
      items.push({ sep: true }, { icon: "power", label: L("Modell laden", "Load model"), run: () => { MP.data = data; activateModel(cur); } });
    items.push({ sep: true }, { icon: "settings", label: L("Modelle verwalten …", "Manage models …"), run: () => openModelsPage("mine") });
    popMenu(anchor, items);
  };

  let toastTimer;
  // ---------------------------------------------------------------- Eigene Dialoge (statt Browser-Fenster)
  // ask(text) → true/false, askText(text, wert) → Text oder null, askFolder(pfad) → Ordner oder null
  function dialog({ text, input = null, multiline = false, ok, danger = false, body = null }) {
    return new Promise((resolve) => {
      const scrim = document.createElement("div");
      scrim.className = "ui-dialog-scrim";
      const box = document.createElement("div");
      box.className = "ui-dialog";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      const msg = document.createElement("p");
      msg.className = "ui-dialog-text";
      msg.textContent = text;
      box.appendChild(msg);
      let field = null;
      if (input !== null) {
        field = document.createElement(multiline ? "textarea" : "input");
        field.className = "ui-dialog-input";
        if (!multiline) field.type = "text";
        field.value = input;
        field.rows = 3;
        field.spellcheck = multiline;
        box.appendChild(field);
      }
      if (body) box.appendChild(body.el);
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      actions.innerHTML = `<button type="button" class="btn ghost ui-cancel"></button><button type="button" class="btn ${danger ? "danger" : "primary"} ui-ok"></button>`;
      actions.querySelector(".ui-cancel").textContent = L("Abbrechen", "Cancel");
      actions.querySelector(".ui-ok").textContent = ok || "OK";
      box.appendChild(actions);
      scrim.appendChild(box);
      document.body.appendChild(scrim);
      const done = (value) => {
        document.removeEventListener("keydown", keys, true);
        scrim.remove();
        resolve(value);
      };
      const value = () => (body ? body.value() : field ? field.value : true);
      const keys = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(input !== null || body ? null : false); }
        else if (e.key === "Enter" && !(multiline && !(e.ctrlKey || e.metaKey)) && e.target.tagName !== "BUTTON") {
          e.preventDefault(); e.stopPropagation(); done(value());
        } else if (e.code === "Space" && !(e.target.closest && e.target.closest(".ui-dialog"))) e.stopPropagation();
      };
      document.addEventListener("keydown", keys, true);
      actions.querySelector(".ui-cancel").onclick = () => done(input !== null || body ? null : false);
      actions.querySelector(".ui-ok").onclick = () => done(value());
      scrim.onclick = (e) => { if (e.target === scrim) done(input !== null || body ? null : false); };
      if (body) body.done = done;
      setTimeout(() => {
        if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
        else (body && body.focus ? body.focus() : actions.querySelector(".ui-ok").focus());
      }, 30);
    });
  }
  function ask(text, opts = {}) {
    const danger = opts.danger ?? /löschen|entfernen|verwerfen|beenden|überschreiben|delete|remove|discard|overwrite/i.test(text.split("\n")[0]);
    return dialog({ text, ok: opts.ok, danger });
  }
  function askText(text, value = "", opts = {}) {
    return dialog({ text, input: value, multiline: !!opts.multiline, ok: opts.ok });
  }
  // Projektordner: Pfad eintippen oder durch die Ordner klicken (der Server listet nur Ordnernamen)
  function askFolder(start) {
    const el = document.createElement("div");
    el.className = "ui-folder";
    el.innerHTML = `<input class="ui-dialog-input" type="text" spellcheck="false"><ul class="ui-dialog-list"></ul>`;
    const field = el.querySelector("input"), list = el.querySelector("ul");
    let timer = null;
    const show = async (path) => {
      let r;
      try { r = await getJSON("/api/fs/dirs?path=" + encodeURIComponent(path)); } catch { return; }
      if (r.path && document.activeElement !== field) field.value = r.path;
      list.innerHTML = "";
      const item = (name, target, cls = "") => {
        const li = document.createElement("li");
        li.innerHTML = `<button type="button" class="${cls}">${ico("folder")}<span></span></button>`;
        li.querySelector("span").textContent = name;
        li.querySelector("button").onclick = () => { field.value = target; show(target); };
        list.appendChild(li);
      };
      if (r.parent) item("..", r.parent, "up");
      for (const d of r.dirs) item(d, (r.path.endsWith("/") ? r.path : r.path + "/") + d, d.startsWith(".") ? "hidden-dir" : "");
      if (!r.dirs.length && !r.parent) list.innerHTML = `<li class="empty">${L("Kein Ordner", "No folder")}</li>`;
      if (r.error) list.insertAdjacentHTML("afterbegin", `<li class="empty"></li>`), (list.firstChild.textContent = r.error);
    };
    field.oninput = () => { clearTimeout(timer); timer = setTimeout(() => show(field.value), 250); };
    field.value = start || "~/";
    show(field.value);
    const body = { el, value: () => field.value.trim(), focus: () => field.focus() };
    return dialog({ text: L("Projektordner für diesen Chat – Pfad eingeben oder Ordner anklicken (leer = keiner):",
                            "Project folder for this chat – type a path or click through the folders (empty = none):"),
                    body, ok: L("Diesen Ordner nehmen", "Use this folder") });
  }

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
        <button class="ghost" data-a="run" title="${L("jetzt ausführen", "run now")}">${ico("play")}</button>
        <button class="ghost" data-a="edit" title="${L("bearbeiten", "edit")}">${ico("edit")}</button>
        <button class="ghost" data-a="del" title="${L("löschen", "delete")}">${ico("trash")}</button>`;
      li.querySelector(".n").textContent = r.name;
      // „morgen 08:00“ → „morgen“, wenn die Uhrzeit ohnehin im Zeitplan steht
      const nextShort = r.next.endsWith(" " + r.time) ? r.next.slice(0, -r.time.length - 1) : r.next;
      const next = r.enabled && r.next !== "–" ? ` · ${nextShort}` : r.enabled ? "" : ` · ${L("pausiert", "paused")}`;
      li.querySelector(".rt-meta").textContent = r.schedule + next;
      li.querySelector(".rt-meta").title = r.enabled && r.next !== "–" ? `${L("nächste Ausführung", "next run")}: ${r.next}` : "";
      li.querySelector(".rt-main").title = r.task + (r.last_summary ? "\n\n" + L("Zuletzt: ", "Last: ") + r.last_summary : "");
      li.querySelector(".rt-main").onclick = async () => {
        if (!r.chat_id) { toast(L("Noch kein Ergebnis – „Jetzt ausführen“ startet die Routine.", "No result yet – “run now” starts the routine.")); return; }
        if (!await confirmLeaveSandbox()) return;
        api("POST", `/api/chats/${r.chat_id}/activate`).catch(() => {});
      };
      li.querySelector("input").onchange = (e) => api("PUT", `/api/routines/${r.id}`, { enabled: e.target.checked })
        .then(loadRoutines).catch(() => toast(L("Speichern fehlgeschlagen", "Saving failed")));
      li.querySelectorAll("button").forEach((b) => b.onclick = async () => {
        if (b.dataset.a === "edit") return openRoutineForm(r);
        if (b.dataset.a === "del") {
          if (!await ask(L(`Routine „${r.name}“ löschen? Ihr Chat bleibt im Verlauf.`, `Delete routine “${r.name}”? Its chat stays in the history.`))) return;
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
    el.classList.remove("ok", "warn", "bad", "off");
    el.classList.add(cls);
    el.querySelector("em").textContent = text;
  }

  async function loadStatus() {
    const asked = Date.now();
    try {
      const st = await getJSON("/api/status");
      S.status = st;
      greet();
      const l = st.llm;
      const name = l.label && l.label !== l.model ? `${l.label} · ${l.model}` : l.model;
      // Nur eine Antwort, die nach dem letzten „Modell aktiv“ angefragt wurde, darf „lädt“ setzen – sonst bliebe
      // „Lade Modell“ nach dem Start stehen, bis man neu lädt
      if (l.switching && asked > S.modelDoneAt) { S.modelSwitching = S.modelSwitching || l.switching; refresh(); }
      else if (!l.switching && S.modelSwitching && asked > S.modelDoneAt) { S.modelSwitching = null; refresh(); }
      setPill("pill-llm", l.switching ? "warn" : l.unloaded ? "off" : !l.online ? "bad" : l.model_available ? "ok" : "warn",
        l.switching ? L("lädt …", "loading …") : l.unloaded ? `${l.label || l.model} · ${L("entladen", "unloaded")}` : !l.online ? `${l.label || l.model} offline` : l.model_available ? name : l.model + L(" fehlt", " missing"));
      const v = st.voice;
      const vCls = v.stt && v.tts ? "ok" : v.stt || v.tts ? "warn" : "bad";
      setPill("pill-voice", vCls, [v.stt ? "STT" : null, v.tts ? "TTS" : "TTS(Browser)", v.wake ? "Wake-Word" : null].filter(Boolean).join(" · "));
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
    let group = "";
    for (const c of list) {
      const g = chatGroup(c);
      if (g !== group) {
        group = g;
        const h = document.createElement("li");
        h.className = "chat-group";
        h.textContent = g;
        ul.appendChild(h);
      }
      const li = document.createElement("li");
      li.className = "chat-item" + (c.active ? " active" : "") + (c.starred ? " pinned" : "");
      li.title = c.preview || c.title;
      li.innerHTML = `<div class="ci-main"><div class="t"></div></div><span class="ci-when"></span>`
        + `<button class="ci-more" type="button" aria-label="${L("Aktionen", "Actions")}"><svg class="i"><use href="#i-more"/></svg></button>`;
      li.querySelector(".t").textContent = c.title;
      li.querySelector(".ci-when").textContent = chatShortWhen(c.updated);
      li.onclick = async () => {
        if (c.active || li.querySelector("input") || !await confirmLeaveSandbox()) return;
        api("POST", `/api/chats/${c.id}/activate`).catch(() => {});
      };
      li.querySelector(".ci-more").onclick = (e) => { e.stopPropagation(); chatMenu(e.currentTarget, c, li); };
      ul.appendChild(li);
    }
  }

  // Gruppen wie in LM Studio/ChatGPT: Angeheftet · Heute · Gestern · Letzte 7 Tage · Letzte 30 Tage · Älter
  function chatGroup(c) {
    if (c.starred) return L("Angeheftet", "Pinned");
    const day = (ts) => { const d = new Date(ts); d.setHours(0, 0, 0, 0); return d.getTime(); };
    const diff = Math.round((day(Date.now()) - day((c.updated || 0) * 1000)) / 86400000);
    if (diff <= 0) return L("Heute", "Today");
    if (diff === 1) return L("Gestern", "Yesterday");
    if (diff < 7) return L("Letzte 7 Tage", "Previous 7 days");
    if (diff < 30) return L("Letzte 30 Tage", "Previous 30 days");
    return L("Älter", "Older");
  }
  function chatShortWhen(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    if (d.toDateString() === new Date().toDateString()) return d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" });
  }

  // Kleines Menü an einem Knopf (Chatliste, Kopfzeile)
  let openMenu = null;
  function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
  document.addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  // Kleines Menü an einem Knopf. Einträge: {icon, label, run, sub?, checked?, disabled?, danger?, title?} oder {sep: true}
  function popMenu(anchor, items) {
    closeMenu();
    const m = document.createElement("div");
    m.className = "pop-menu";
    m.setAttribute("role", "menu");
    for (const it of items) {
      if (it.sep) { m.appendChild(Object.assign(document.createElement("div"), { className: "pm-sep" })); continue; }
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pm-item" + (it.danger ? " danger" : "") + (it.checked ? " on" : "");
      b.setAttribute("role", "menuitem");
      b.innerHTML = `<svg class="i"><use href="#i-${it.icon}"/></svg><span class="pm-text"><span></span></span>`;
      b.querySelector(".pm-text span").textContent = it.label;
      if (it.sub) b.querySelector(".pm-text").appendChild(Object.assign(document.createElement("small"), { textContent: it.sub }));
      b.disabled = !!it.disabled;
      if (it.title) b.title = it.title;
      b.onclick = (e) => { e.stopPropagation(); closeMenu(); it.run(); };
      m.appendChild(b);
    }
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
    m.style.top = (r.bottom + h + 8 > window.innerHeight ? r.top - h - 4 : r.bottom + 4) + "px";
    openMenu = m;
    (m.querySelector("button:not(:disabled)") || m).focus();
  }
  function chatMenu(anchor, c, li) {
    popMenu(anchor, [
      { icon: "edit", label: L("Umbenennen", "Rename"), run: () => renameInline(li.querySelector(".t"), c) },
      { icon: "pin", label: c.starred ? L("Lösen", "Unpin") : L("Anheften", "Pin"),
        run: () => api("POST", `/api/chats/${c.id}/star`, { starred: !c.starred }).then(loadChats).catch(() => {}) },
      { icon: "download", label: L("Als Markdown exportieren", "Export as Markdown"), run: () => exportChat(c.id) },
      { icon: "trash", label: L("Löschen", "Delete"), danger: true, run: () => deleteChat(c) },
    ]);
  }
  function renameInline(target, c) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "rename-input";
    input.value = c.title;
    input.maxLength = 120;
    const old = target.textContent;
    target.textContent = "";
    target.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      input.remove();
      target.textContent = save && title ? title : old;
      if (save && title && title !== c.title) {
        api("PATCH", `/api/chats/${c.id}`, { title }).then(() => {
          loadChats();
          if (c.active || c.id === S.chatId) $("chat-title").textContent = title;
        }).catch(() => {});
      }
    };
    input.onclick = (e) => e.stopPropagation();
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    };
    input.onblur = () => finish(true);
  }
  function exportChat(id) {
    const a = document.createElement("a");
    a.href = `/api/chats/${encodeURIComponent(id)}/export`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  async function deleteChat(c) {
    if (c.active && !await confirmLeaveSandbox(true)) return;
    const box = c.sandbox ? L("\n\nDie Arbeitskopie der Sandbox wird mitgelöscht – nicht übernommene Änderungen gehen verloren.",
      "\n\nThe sandbox working copy is deleted too – changes not applied are lost.") : "";
    const extra = box + (c.legacy ? L("\n\nHinweis: Dieser Chat stammt von vor der Chat-Historie – ältere Tagebuch-Einträge "
      + "daraus lassen sich nicht zuordnen und bleiben im Gedächtnis.",
      "\n\nNote: this chat predates the chat history – older journal entries from it cannot be attributed and stay in memory.") : "");
    if (await ask(L(`„${c.title}“ löschen?\n\nDer Chat wird auch aus Orbwise' Gedächtnis entfernt (Tagebuch, Suche, `
        + `Tageszusammenfassung). Gelernte Fakten bleiben.`, `Delete “${c.title}”?\n\nThe chat is also removed from `
        + `Orbwise' memory (journal, search, daily summary). Learned facts are kept.`) + extra)) {
      api("DELETE", `/api/chats/${c.id}`).then(() => { toast(L("Chat gelöscht.", "Chat deleted.")); loadChats(); }).catch(() => {});
    }
  }
  // Kopfzeile: Titel anklicken benennt um, ⋯ bietet Exportieren/Löschen für den offenen Chat
  async function activeChat() {
    try { return (await getJSON("/api/chats")).find((x) => x.active) || null; } catch { return null; }
  }
  $("chat-title").onclick = async () => {
    const c = await activeChat();
    if (c) renameInline($("chat-title"), c);
  };
  $("chat-more").onclick = async (e) => {
    e.stopPropagation();
    const anchor = e.currentTarget;
    const c = await activeChat();
    if (!c) return;
    popMenu(anchor, [
      { icon: "edit", label: L("Umbenennen", "Rename"), run: () => renameInline($("chat-title"), c) },
      { icon: "download", label: L("Als Markdown exportieren", "Export as Markdown"), run: () => exportChat(c.id) },
      { icon: "trash", label: L("Löschen", "Delete"), danger: true, run: () => deleteChat(c) },
    ]);
  };
  $("chat-search").addEventListener("input", () => {
    clearTimeout(chatSearchTimer);
    chatSearchTimer = setTimeout(loadChats, 250);
  });
  $("chat-new").onclick = () => newChat();

  // ---------------------------------------------------------------- Bild-Modus (Qwen-Image-2.1)
  // Eigene Ansicht statt Chat: Prompt, Format, Galerie. Läuft ohne Sprachmodell; der Server gibt den Grafikspeicher
  // ans Sprachmodell zurück, sobald es wieder gebraucht wird (oder beim Verlassen der Ansicht).
  const IV = { open: false, status: null, refs: [], busy: false };
  const RES_LABEL = { 0.5: L("Schnell · 0,5 MP", "Fast · 0.5 MP"), 1: L("Standard · 1 MP", "Standard · 1 MP"),
                      1.5: L("Fein · 1,5 MP", "Fine · 1.5 MP"), 2: L("Groß · 2 MP", "Large · 2 MP") };
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
    $("iv-upload-label").querySelector("span").textContent = L("Vorlagen hinzufügen …", "Add references …");
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
        sel.add(new Option(L("Wie Vorlage (Bild 1)", "Like reference (image 1)"), "ref"));
        sel.add(new Option(L("Eigene Größe …", "Custom size …"), "custom"));
        sel.value = store.get("iv.size", st.size || "1:1");
        if (!sel.value) sel.value = "1:1";
        const res = $("iv-res");
        for (const r of st.resolutions || [1]) res.add(new Option(RES_LABEL[r] || `${r} MP`, String(r)));
        res.value = String(store.get("iv.mp", st.megapixels || 1));
        if (!res.value) res.value = "1";
        const [w, h] = store.get("iv.custom", [1280, 704]);
        $("iv-w").value = w; $("iv-h").value = h;
        $("iv-steps").value = store.get("iv.steps", st.steps || 20);
      }
      updateDims();
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
    card.querySelector(".iv-cap").textContent = (img.edit ? (img.refs > 1 ? `✎×${img.refs} ` : "✎ ") : "") + (img.prompt || "");
    card.querySelector(".iv-meta").textContent = `${img.width}×${img.height} · ${img.steps} ${L("Schritte", "steps")} · Seed ${img.seed}`
      + (img.seconds ? ` · ${secs(img.seconds)}` : "");
    const actions = card.querySelector(".iv-actions");
    const add = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn small"; b.innerHTML = label.startsWith("<svg") ? label : escapeHtml(label);
      b.title = title; b.onclick = fn;
      actions.appendChild(b);
    };
    add(L("Variante", "Variation"), L("Gleicher Prompt, neuer Seed", "Same prompt, new seed"), () => {
      $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = "";
      generateImage();
    });
    add(L("Gleich", "Reuse"), L("Prompt und Einstellungen übernehmen (Seed bleibt)", "Reuse prompt and settings (same seed)"), () => {
      $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = img.seed;
      $("iv-steps").value = img.steps;
      $("iv-size").value = "custom"; $("iv-w").value = img.width; $("iv-h").value = img.height; updateDims();
      $("iv-prompt").focus();
    });
    if (IV.status && IV.status.can_edit) add(L("Als Vorlage", "Use as reference"), L("Als Vorlage hinzufügen (bis zu 3 – z. B. Personen aus mehreren Bildern zusammenbringen)", "Add as a reference (up to 3 – e.g. bring people from several images together)"), () => {
      addRef({ file: img.file, url }); $("iv-prompt").focus();
    });
    if (IV.status && IV.status.telegram) add(L("Handy", "Phone"), L("Per Telegram aufs Handy", "Send to the phone via Telegram"), () =>
      api("POST", `/api/image/telegram/${encodeURIComponent(img.file)}`).then(() => toast(L("Aufs Handy geschickt.", "Sent to the phone."))).catch(() => {}));
    add(ico("trash"), L("Löschen", "Delete"), async () => {
      if (!await ask(L("Bild löschen?", "Delete image?"), { danger: true })) return;
      api("DELETE", `/api/image/file/${encodeURIComponent(img.file)}`).then(() => card.remove()).catch(() => {});
    });
    return card;
  }
  function addRef(ref) {
    const max = (IV.status && IV.status.max_refs) || 3;
    if (IV.refs.length >= max) { toast(L(`Höchstens ${max} Vorlagen.`, `At most ${max} references.`)); return; }
    if (ref.file && IV.refs.some((r) => r.file === ref.file)) return;
    IV.refs.push(ref);
    renderRefs();
  }
  function renderRefs() {
    const box = $("iv-refs");
    const list = $("iv-ref-list");
    box.classList.toggle("hidden", !IV.refs.length);
    list.innerHTML = "";
    IV.refs.forEach((r, i) => {
      const item = document.createElement("div");
      item.className = "iv-ref-item";
      item.innerHTML = `<img alt=""><b></b><button type="button" aria-label="${L("Entfernen", "Remove")}">✕</button>`;
      item.querySelector("img").src = r.url || r.data;
      item.querySelector("b").textContent = L(`Bild ${i + 1}`, `Image ${i + 1}`);
      item.querySelector("button").onclick = () => { IV.refs.splice(i, 1); renderRefs(); };
      list.appendChild(item);
    });
    $("iv-ref-hint").textContent = IV.refs.length > 1
      ? L("Im Text auf die Bilder verweisen – z. B. „die Frau aus Bild 1 und der Mann aus Bild 2 zusammen auf einer Parkbank, Abendlicht“.",
          "Refer to the images in the text – e.g. “the woman from image 1 and the man from image 2 together on a park bench, evening light”.")
      : L("Beschreib die Änderung – z. B. „mach den Himmel rot“. Für mehrere Personen weitere Bilder hinzufügen (bis zu 3).",
          "Describe the change – e.g. “make the sky red”. Add more images for several people (up to 3).");
    updateDims();
  }
  // Ergebnisgröße anzeigen (wie der Server rechnet: Seitenverhältnis × Megapixel, auf 32 gerundet)
  const RATIO = { "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 3 / 2, "2:3": 2 / 3 };
  const r32 = (v) => Math.max(256, Math.min(2048, Math.round(v / 32) * 32));
  function updateDims() {
    const size = $("iv-size").value;
    $("iv-custom").classList.toggle("hidden", size !== "custom");
    $("iv-res").classList.toggle("hidden", size === "custom");
    let text = "";
    if (size === "custom") text = "";
    else if (size === "ref") text = IV.refs.length ? L("Seitenverhältnis wie Bild 1", "aspect of image 1") : L("erst eine Vorlage hinzufügen", "add a reference first");
    else if (RATIO[size]) {
      const w = Math.sqrt(Number($("iv-res").value || 1) * 1048576 * RATIO[size]);
      text = `${r32(w)} × ${r32(w / RATIO[size])}`;
    }
    $("iv-dims").textContent = text;
  }
  $("iv-size").onchange = updateDims;
  $("iv-res").onchange = updateDims;
  $("iv-upload").onchange = (e) => {
    const files = [...(e.target.files || [])];
    e.target.value = "";
    for (const f of files) {
      if (f.size > 20e6) { toast(L("Bild zu groß (höchstens 20 MB).", "Image too large (20 MB max).")); continue; }
      const reader = new FileReader();
      reader.onload = () => { addRef({ data: reader.result }); $("iv-prompt").focus(); };
      reader.readAsDataURL(f);
    }
  };
  async function generateImage() {
    const prompt = $("iv-prompt").value.trim();
    if (!prompt) { $("iv-prompt").focus(); return; }
    let size = $("iv-size").value;
    if (size === "ref" && !IV.refs.length) size = "1:1";
    store.set("iv.size", $("iv-size").value);
    store.set("iv.mp", Number($("iv-res").value) || 1);
    store.set("iv.steps", Number($("iv-steps").value) || 20);
    if (size === "custom") {
      const w = r32(Number($("iv-w").value) || 1024), h = r32(Number($("iv-h").value) || 1024);
      store.set("iv.custom", [w, h]);
      size = `${w}x${h}`;
    }
    const seed = $("iv-seed").value === "" ? -1 : Number($("iv-seed").value);
    const body = { prompt, negative: $("iv-negative").value, size, megapixels: Number($("iv-res").value) || 1,
                   steps: Number($("iv-steps").value) || 0, count: Number($("iv-count").value) || 1, seed,
                   refs: IV.refs.map((r) => (r.file ? { file: r.file } : { data: r.data })) };
    setImageBusy(true);
    imageProgress({ phase: "queued" });
    try { await api("POST", "/api/image/generate", body); } catch { setImageBusy(false); }
  }
  $("iv-form").addEventListener("submit", (e) => { e.preventDefault(); generateImage(); });
  $("iv-prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); generateImage(); }
  });
  $("iv-cancel").onclick = () => api("POST", "/api/image/cancel").catch(() => {});

  // ---------------------------------------------------------------- Modi: Tools (Assistent) / Coding
  // Getrennte Chat-Verläufe; Coding lädt nur Dateien, Shell, Web und Gedächtnis und hat keine Sprache.
  const MODE_TEXT = {
    get tools() {
      return { placeholder: L(`Frag ${S.name} oder sag „${S.wakePhrase}“ …`, `Ask ${S.name} or say “${S.wakePhrase}” …`),
               hint: L(`Leertaste halten zum Sprechen · Esc bricht ab · Änderungen am System fragt ${S.name} vorher`,
                       `Hold the space bar to talk · Esc cancels · ${S.name} asks before changing your system`) };
    },
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
  function markModeTabs() {
    const current = IV.open ? "image" : S.mode;
    document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
      b.classList.toggle("on", b.dataset.mode === current);
      b.setAttribute("aria-selected", String(b.dataset.mode === current));
    });
  }
  document.querySelectorAll(".mode-switch [data-mode]").forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.mode === "image") { openImageView(); return; }
      if (IV.open) closeImageView();
      if (b.dataset.mode === S.mode || !await confirmLeaveSandbox()) return;
      api("POST", "/api/mode", { mode: b.dataset.mode })
        .then(() => toast(b.dataset.mode === "coding"
          ? L("Coding-Modus – nur Dateien, Shell, Web und Gedächtnis, ohne Sprache. Eigene Chats.", "Coding mode – only files, shell, web and memory, no voice. Separate chats.")
          : L("Tools-Modus – alle Werkzeuge und Sprache.", "Tools mode – all tools and voice.")))
        .catch(() => {});  // Fehler zeigt api() schon an
    };
  });
  // ---------------------------------------------------------------- Projekt, GitHub, Sandbox (Coding)
  const projectMenu = $("project-menu"), sandboxMenu = $("sandbox-menu");
  $("project-chip").onclick = (e) => {
    e.stopPropagation();
    sandboxMenu.classList.add("hidden");
    projectMenu.classList.toggle("hidden");
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#project-wrap")) projectMenu.classList.add("hidden");
    if (!e.target.closest("#sandbox-wrap")) sandboxMenu.classList.add("hidden");
  });
  projectMenu.querySelector('[data-project="folder"]').onclick = async () => {
    projectMenu.classList.add("hidden");
    const path = await askFolder(S.project || "");
    if (path === null || !S.chatId || !await confirmLeaveSandbox(true)) return;
    api("POST", `/api/chats/${S.chatId}/project`, { path: path.trim() })
      .then((r) => toast(r.project ? L(`Projektordner: ${r.project}`, `Project folder: ${r.project}`) : L("Kein Projektordner.", "No project folder.")))
      .catch(() => {});  // Fehler zeigt api() schon an
  };
  projectMenu.querySelector('[data-project="github"]').onclick = () => { projectMenu.classList.add("hidden"); openGithub(); };

  // GitHub-Repo und Branch im Projekt-Chip (bei Sandbox: die der Arbeitskopie)
  let projectInfoSeq = 0;
  async function loadProjectInfo() {
    if (S.mode !== "coding" || !S.project) return;
    const seq = ++projectInfoSeq;
    let p;
    try { p = await getJSON("/api/project"); } catch { return; }
    if (seq !== projectInfoSeq || !p.path) return;
    const label = $("project-label"), badge = $("project-branch"), chip = $("project-chip");
    const folder = S.project.replace(/\/+$/, "").split("/").pop();
    label.textContent = p.repo || (p.branch ? folder : S.project.replace(/^\/home\/[^/]+/, "~"));
    chip.querySelector(".i use").setAttribute("href", p.repo ? "#i-github" : "#i-folder");
    badge.classList.toggle("hidden", !p.branch);
    badge.querySelector("span").textContent = p.branch || "";
    chip.title = [S.project, p.repo ? `GitHub: ${p.repo}` : "", p.branch ? `Branch: ${p.branch}` : "",
                  p.sandbox ? L("Arbeitskopie in der Sandbox", "Working copy in the sandbox") : ""].filter(Boolean).join("\n");
  }

  let ghTimer = null;
  async function openGithub() {
    openSheet("github");
    const status = $("gh-status"), list = $("gh-repos"), search = $("gh-search");
    list.innerHTML = "";
    status.textContent = L("Verbinde mit GitHub …", "Connecting to GitHub …");
    let st;
    try { st = await getJSON("/api/github/status"); } catch { st = { connected: false }; }
    search.classList.toggle("hidden", !st.connected);
    if (!st.connected) {
      status.innerHTML = "";
      status.append(st.error || L("GitHub ist nicht verbunden. Melde dich mit der GitHub-CLI an (", "GitHub is not connected. Sign in with the GitHub CLI ("));
      if (!st.error) {
        const code = document.createElement("code");
        code.textContent = "gh auth login";
        status.append(code, L(") oder trag ein Token unter github.token in der Config ein.", ") or put a token under github.token in the config."));
      }
      return;
    }
    const src = { gh: "GitHub-CLI", env: L("Umgebungsvariable", "environment variable"), config: "Config" }[st.source] || st.source;
    status.textContent = L(`Angemeldet als @${st.login} (${src}) · geklont wird nach ${st.clone_dir}`,
                           `Signed in as @${st.login} (${src}) · clones go to ${st.clone_dir}`);
    const load = async () => {
      list.innerHTML = `<li class="empty">${L("Lade Repos …", "Loading repos …")}</li>`;
      try {
        const r = await getJSON("/api/github/repos?q=" + encodeURIComponent(search.value.trim()));
        list.innerHTML = r.repos.length ? "" : `<li class="empty">${L("Keine Repos gefunden.", "No repos found.")}</li>`;
        for (const repo of r.repos) {
          const li = document.createElement("li");
          li.innerHTML = `<button type="button" class="gh-repo">${ico("branch")}<span class="gh-main"><b></b><small></small></span><span class="gh-meta"></span></button>`;
          li.querySelector("b").textContent = repo.full_name;
          li.querySelector("small").textContent = repo.description || "";
          li.querySelector(".gh-meta").textContent = (repo.private ? L("privat", "private") : L("öffentlich", "public"))
            + (repo.pushed_at ? " · " + new Date(repo.pushed_at).toLocaleDateString(LOCALE) : "");
          li.querySelector("button").onclick = () => openRepo(repo, li);
          list.appendChild(li);
        }
      } catch { list.innerHTML = `<li class="empty">${L("Repos nicht ladbar.", "Could not load repos.")}</li>`; }
    };
    search.oninput = () => { clearTimeout(ghTimer); ghTimer = setTimeout(load, 350); };
    search.value = "";
    load();
    setTimeout(() => search.focus(), 50);
  }
  // Klick auf ein Repo: Zeile aufklappen mit Branch-Auswahl und „Klonen“ (nur eine Zeile offen)
  async function openRepo(repo, li) {
    const list = li.parentElement;
    const open = li.querySelector(".gh-clone");
    list.querySelectorAll(".gh-clone").forEach((el) => el.remove());
    list.querySelectorAll("li.open").forEach((el) => el.classList.remove("open"));
    if (open) return;
    li.classList.add("open");
    const box = document.createElement("div");
    box.className = "gh-clone";
    box.innerHTML = `<label>${L("Branch", "Branch")}<select disabled><option></option></select></label>
      <button type="button" class="btn small primary">${L("Klonen", "Clone")}</button>`;
    li.appendChild(box);
    const select = box.querySelector("select");
    select.options[0].value = repo.default_branch;
    select.options[0].textContent = L("lade …", "loading …");
    box.querySelector("button").onclick = async () => { if (await confirmLeaveSandbox(true)) cloneRepo(repo.full_name, select.value, li); };
    let names = [repo.default_branch];
    try { names = (await getJSON("/api/github/branches?repo=" + encodeURIComponent(repo.full_name))).branches || names; } catch {}
    if (!box.isConnected) return;
    select.innerHTML = "";
    for (const n of names.length ? names : [repo.default_branch]) {
      const o = document.createElement("option");
      o.value = o.textContent = n;
      if (n === repo.default_branch) o.textContent += L(" (Standard)", " (default)");
      select.appendChild(o);
    }
    select.value = repo.default_branch;
    select.disabled = false;
  }
  async function cloneRepo(name, branch, li) {
    li.classList.add("busy");
    li.querySelector(".gh-meta").textContent = L("klone …", "cloning …");
    try {
      const r = await api("POST", "/api/github/clone", { repo: name, branch });
      closeSheet();
      toast(L(`Geklont – Branch ${branch} · Projektordner: ${r.project}`, `Cloned – branch ${branch} · project folder: ${r.project}`));
    } catch { li.classList.remove("busy"); li.querySelector(".gh-meta").textContent = L("fehlgeschlagen", "failed"); }
  }

  // Sandbox: Container + Arbeitskopie (Befehle laufen dort, das Projekt bleibt bis „Übernehmen“ unberührt)
  function renderSandboxChip() {
    const chip = $("sandbox-chip");
    const on = !!S.sandbox;
    chip.classList.toggle("on", on);
    chip.classList.toggle("unset", !on);
    $("sandbox-label").textContent = !on ? L("Sandbox", "Sandbox")
      : S.sandbox.network === false ? L("Sandbox · offline", "Sandbox · offline") : L("Sandbox an", "Sandbox on");
    chip.title = on ? L(`Sandbox: Befehle laufen im Container (${S.sandbox.engine}), Branch ${S.sandbox.branch || "–"}`,
                        `Sandbox: commands run in the container (${S.sandbox.engine}), branch ${S.sandbox.branch || "–"}`)
      : L("Sandbox: Befehle laufen in einem Container auf einer Arbeitskopie", "Sandbox: commands run in a container on a working copy");
  }
  // 🛡 Code-Check: Änderungen bzw. ganzes Projekt in der Sandbox prüfen (Server: /review, siehe agent_review.py)
  const reviewMenu = $("review-menu");
  $("review-chip").onclick = (e) => {
    e.stopPropagation();
    if (!S.project) { toast(L("Erst einen Projektordner wählen.", "Choose a project folder first.")); return; }
    reviewMenu.classList.toggle("hidden");
  };
  reviewMenu.querySelectorAll("[data-review]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      reviewMenu.classList.add("hidden");
      if (["thinking", "executing", "confirm"].includes(S.serverState)) { toast(L("Erst die laufende Antwort abwarten.", "Wait for the running answer first.")); return; }
      send({ type: "user_message", text: b.dataset.review ? "/review all" : "/review" });
    };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#review-wrap")) reviewMenu.classList.add("hidden"); });
  $("sandbox-chip").onclick = async (e) => {
    e.stopPropagation();
    projectMenu.classList.add("hidden");
    if (!S.project) { toast(L("Erst einen Projektordner wählen.", "Choose a project folder first.")); return; }
    if (!S.sandbox) {
      if (!await ask(L("Sandbox einschalten? Orbwise arbeitet dann in einer Kopie des Projekts in einem Container und darf dort frei bauen, testen und installieren. Dein Projektordner bleibt unberührt, bis du die Änderungen übernimmst.",
                     "Turn on the sandbox? Orbwise then works on a copy of the project in a container and may build, test and install freely there. Your project folder stays untouched until you apply the changes."),
                     { ok: L("Einschalten", "Turn on") })) return;
      $("sandbox-label").textContent = L("Sandbox …", "Sandbox …");
      try {
        await api("POST", "/api/sandbox", { enabled: true });
        toast(L("Sandbox an – der Container startet beim ersten Befehl.", "Sandbox on – the container starts with the first command."));
      } catch { renderSandboxChip(); }
      return;
    }
    if (!sandboxMenu.classList.contains("hidden")) { sandboxMenu.classList.add("hidden"); return; }
    sandboxMenu.innerHTML = `<div class="sb-status">${L("Lade …", "Loading …")}</div>`;
    sandboxMenu.classList.remove("hidden");
    let st;
    try { st = await getJSON("/api/sandbox"); } catch { sandboxMenu.classList.add("hidden"); return; }
    const container = { running: L("läuft", "running"), exited: L("angehalten", "stopped"), missing: L("startet beim ersten Befehl", "starts with the first command") }[st.container] || st.container || "";
    sandboxMenu.innerHTML = `<div class="sb-status"><b></b><small></small></div>
      <button type="button" data-sb="network"><b></b><small></small></button>
      <button type="button" data-sb="containers"><b></b><small></small></button>
      <button type="button" data-sb="changes"><b></b><small></small></button>
      <button type="button" data-sb="apply"><b>${L("In Projekt übernehmen", "Apply to project")}</b><small>${L("Geänderte Dateien in deinen Projektordner kopieren.", "Copy changed files into your project folder.")}</small></button>
      ${st.github ? `<button type="button" data-sb="push"><b>${L("Zu GitHub pushen", "Push to GitHub")}</b><small></small></button>` : ""}
      <button type="button" data-sb="off" class="danger"><b>${L("Sandbox beenden", "End sandbox")}</b><small>${L("Container und Arbeitskopie löschen – nicht Übernommenes geht verloren.", "Delete container and working copy – anything not applied is lost.")}</small></button>`;
    sandboxMenu.querySelector(".sb-status b").textContent = `${st.engine || "?"} · ${st.image}`;
    sandboxMenu.querySelector(".sb-status small").textContent = L(`Container ${container} · Branch ${st.branch || "–"}`, `Container ${container} · branch ${st.branch || "–"}`);
    const net = sandboxMenu.querySelector('[data-sb="network"]');
    net.querySelector("b").textContent = st.network ? L("Netz: an", "Network: on") : L("Netz: aus", "Network: off");
    net.querySelector("small").textContent = st.network ? L("Zum Abschalten klicken (Container startet neu).", "Click to switch off (container restarts).")
      : L("Kein Internet im Container – zum Einschalten klicken.", "No internet in the container – click to switch on.");
    if (st.warning) net.querySelector("small").textContent += " ⚠ " + st.warning;
    const cont = sandboxMenu.querySelector('[data-sb="containers"]');
    const test = st.containers_test;
    cont.querySelector("b").textContent = st.containers ? L("Docker im Container: an", "Docker in the container: on")
      : L("Docker im Container: aus", "Docker in the container: off");
    cont.querySelector("small").textContent = !st.containers_allowed && !st.containers
      ? st.containers_blocked
      : st.containers ? (test && test.ok === false ? "⚠ " + test.message : (test && test.message) || L("Docker-Projekte bauen und testen.", "Build and test Docker projects."))
      : L("Erlaubt docker build/run in der Sandbox (rootless).", "Allows docker build/run in the sandbox (rootless).");
    cont.disabled = !st.containers_allowed && !st.containers;
    const ch = sandboxMenu.querySelector('[data-sb="changes"]');
    ch.querySelector("b").textContent = L(`Änderungen ansehen (${st.changes})`, `View changes (${st.changes})`);
    ch.querySelector("small").textContent = L("Was sich in der Arbeitskopie geändert hat, mit Diff.", "What changed in the working copy, with diffs.");
    const push = sandboxMenu.querySelector('[data-sb="push"]');
    if (push) push.querySelector("small").textContent = L(`Branch ${st.branch} nach github.com/${st.github}`, `Branch ${st.branch} to github.com/${st.github}`);
    net.onclick = () => { sandboxMenu.classList.add("hidden"); api("POST", "/api/sandbox", { network: !st.network }).catch(() => {}); };
    cont.onclick = async () => {
      sandboxMenu.classList.add("hidden");
      if (!st.containers && !await ask(L("Docker im Container erlauben? Der Agent kann dann Images bauen und Container starten – rootless als eigener Benutzer im Container, ohne Root-Rechte und ohne Zugriff auf deinen Rechner. Dafür bekommt die Sandbox FUSE und User-Namespaces, das vergrößert die Angriffsfläche zum Kernel etwas. Der Container startet neu (die Arbeitskopie bleibt), danach ein kurzer Test.",
                                        "Allow Docker in the container? The agent can then build images and start containers – rootless as its own user inside the container, without root and without access to your machine. The sandbox gets FUSE and user namespaces for that, which slightly enlarges the attack surface towards the kernel. The container restarts (the working copy stays), then a short test runs."))) return;
      toast(st.containers ? L("Docker im Container wird abgeschaltet …", "Switching Docker in the container off …") : L("Docker im Container wird eingerichtet und getestet …", "Setting up and testing Docker in the container …"));
      try {
        const r = await api("POST", "/api/sandbox", { containers: !st.containers });
        const t = r.containers_test;
        if (r.containers) toast(t && t.ok ? L("Docker im Container funktioniert.", "Docker in the container works.") : "⚠ " + ((t && t.message) || "?"));
      } catch {}
    };
    ch.onclick = () => { sandboxMenu.classList.add("hidden"); openSandboxChanges(); };
    sandboxMenu.querySelector('[data-sb="apply"]').onclick = () => { sandboxMenu.classList.add("hidden"); applySandbox(); };
    if (push) push.onclick = () => { sandboxMenu.classList.add("hidden"); pushGithub(); };
    sandboxMenu.querySelector('[data-sb="off"]').onclick = async () => {
      sandboxMenu.classList.add("hidden");
      if (!await ask(L("Sandbox beenden? Container und Arbeitskopie werden gelöscht – nicht übernommene Änderungen gehen verloren.",
                     "End the sandbox? Container and working copy are deleted – changes not applied are lost."))) return;
      api("POST", "/api/sandbox", { enabled: false }).then(() => toast(L("Sandbox beendet.", "Sandbox ended."))).catch(() => {});
    };
  };
  async function openSandboxChanges() {
    openSheet("sbchanges");
    const list = $("sbc-files"), pre = $("sbc-diff");
    list.innerHTML = "";
    pre.innerHTML = "";
    $("sbc-push").classList.toggle("hidden", true);
    let r;
    try { r = await getJSON("/api/sandbox/changes"); } catch { return; }
    const n = r.files.length;
    $("sbc-info").textContent = n ? (n === 1 ? L("1 Datei geändert.", "1 file changed.") : L(`${n} Dateien geändert – klicke eine Datei für den Diff.`, `${n} files changed – click a file for its diff.`))
      : L("Noch keine Änderungen in der Arbeitskopie.", "No changes in the working copy yet.");
    $("sbc-apply").disabled = !r.files.length;
    getJSON("/api/sandbox").then((st) => $("sbc-push").classList.toggle("hidden", !st.github)).catch(() => {});
    const show = async (f, li) => {
      list.querySelectorAll("li").forEach((x) => x.classList.toggle("on", x === li));
      try { pre.innerHTML = renderDiff((await getJSON("/api/sandbox/changes?path=" + encodeURIComponent(f.path))).diff); } catch { pre.textContent = ""; }
    };
    r.files.forEach((f, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="mc-path"></span><span class="mc-add">${f.add == null ? "" : "+" + f.add}</span><span class="mc-del">${f.del == null ? "" : "−" + f.del}</span>`;
      li.querySelector(".mc-path").textContent = f.path;
      li.onclick = () => show(f, li);
      list.appendChild(li);
      if (i === 0) show(f, li);
    });
  }
  $("sbc-apply").onclick = () => applySandbox(true);
  $("sbc-push").onclick = () => pushGithub();
  async function applySandbox(fromSheet) {
    if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
    try {
      let r = await api("POST", "/api/sandbox/apply", {});
      if (r.conflicts.length && await ask(L(`Diese Dateien hast du im Projekt inzwischen selbst geändert:\n${r.conflicts.join("\n")}\n\nTrotzdem mit der Version aus der Sandbox überschreiben?`,
                                          `You changed these files in the project yourself in the meantime:\n${r.conflicts.join("\n")}\n\nOverwrite them with the sandbox version anyway?`))) {
        const forced = await api("POST", "/api/sandbox/apply", { force: true });
        r = { applied: [...r.applied, ...forced.applied], conflicts: forced.conflicts };
      }
      const n = r.applied.length;
      toast(n ? (n === 1 ? L("1 Datei ins Projekt übernommen.", "1 file applied to the project.") : L(`${n} Dateien ins Projekt übernommen.`, `${n} files applied to the project.`))
        : r.conflicts.length ? L("Nichts übernommen.", "Nothing applied.") : L("Keine Änderungen zu übernehmen.", "No changes to apply."));
      if (fromSheet) closeSheet();
      loadProjectInfo();
    } catch { /* Meldung kommt von api() */ }
  }
  async function pushGithub() {
    if (!await ask(L("Den aktuellen Branch zu GitHub pushen?", "Push the current branch to GitHub?"))) return;
    try {
      const r = await api("POST", "/api/github/push");
      toastLink(L(`Branch ${r.branch} ist auf GitHub.`, `Branch ${r.branch} is on GitHub.`), r.compare, L("Pull Request erstellen", "Create pull request"));
      loadProjectInfo();
    } catch { /* Meldung kommt von api() */ }
  }
  function toastLink(text, url, label) {
    toast(text);
    if (!url) return;
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = " " + label + " ↗";
    $("toast").appendChild(a);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 15000);
  }

  async function newChat() {
    if (!await confirmLeaveSandbox()) return;
    api("POST", "/api/chats").catch(() => {});
  }

  // Vor dem Verlassen eines Chats mit Sandbox: nicht übernommene Änderungen anbieten. Ohne Übernehmen bleiben sie in
  // diesem Chat (Chat wieder öffnen → Sandbox-Menü) – außer discards: Projektwechsel/Löschen werfen die Kopie weg.
  async function confirmLeaveSandbox(discards = false) {
    if (!S.sandbox) return true;
    let st;
    try { st = await getJSON("/api/sandbox"); } catch { return true; }
    if (!st.enabled || !st.changes) return true;
    const el = document.createElement("div");
    el.className = "leave-choice";
    el.innerHTML = `<button type="button" class="btn ghost"></button>`;
    const body = { el, value: () => "apply" };
    const skip = el.querySelector("button");
    skip.textContent = discards ? L("Verwerfen", "Discard") : L("Weiter ohne Übernehmen", "Continue without applying");
    skip.onclick = () => body.done("skip");
    const n = st.changes;
    const head = n === 1 ? L("In der Sandbox ist 1 geänderte Datei, die noch nicht im Projekt ist.", "The sandbox has 1 changed file that is not in the project yet.")
      : L(`In der Sandbox sind ${n} geänderte Dateien, die noch nicht im Projekt sind.`, `The sandbox has ${n} changed files that are not in the project yet.`);
    const tail = discards ? L("Die Arbeitskopie wird dabei gelöscht.", "The working copy will be deleted.")
      : L("Ohne Übernehmen bleiben sie in diesem Chat erhalten (später wieder öffnen → Sandbox-Menü).",
          "Without applying they stay in this chat (open it again later → sandbox menu).");
    const choice = await dialog({ text: `${head}\n\n${tail}`, body, ok: L("Übernehmen und weiter", "Apply and continue") });
    if (choice === "skip") return true;
    if (choice !== "apply") return false;
    try {
      const r = await api("POST", "/api/sandbox/apply", {});
      if (r.conflicts.length) {
        toast(L(`Nicht übernommen – im Projekt inzwischen selbst geändert: ${r.conflicts.join(", ")}. Unter „Änderungen ansehen“ klären.`,
                `Not applied – changed in the project meanwhile: ${r.conflicts.join(", ")}. Resolve under “View changes”.`));
        return false;
      }
      toast(r.applied.length === 1 ? L("1 Datei ins Projekt übernommen.", "1 file applied to the project.")
        : L(`${r.applied.length} Dateien ins Projekt übernommen.`, `${r.applied.length} files applied to the project.`));
      return true;
    } catch { return false; }
  }

  function openChatView(ev) {
    for (const id of Object.keys(assistants)) delete assistants[id];
    parkConfirm();  // nicht mit dem alten Chat wegwerfen
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
      S.turns = h.turns || 0;
      S.usage = h.usage || null;
      renderTokenLine();
      h.messages.forEach((m, i) => {
        if (dividers[i]) { chat.appendChild(compactDivider(dividers[i].summary, dividers[i].ts)); }
        if (m.goal_nudge) { addSystem(L("↻ weiter am Ziel", "↻ continuing with the goal")); return; }
        if (m.role === "user") { addUser(m.content, "", m.images, m.turn, m.editable, m.files); return; }
        const el = addMsg("assistant" + (m.editable === false ? " locked" : ""), S.name, renderMarkdown(m.content));
        el._raw = m.content;
        if (m.turn) el.dataset.turn = m.turn;
        assistantActions(el);
        if (m.images && m.images.length) addShots(el, m.images);
        if (m.changes) showChanges(el, m.turn, m.changes.files, m.changes.undone);
      });
      markLast();
      if (S.confirm) placeConfirm();  // offene Freigabe bleibt unter dem Verlauf
      if (S.question) chat.appendChild(S.question.el);
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
        li.innerHTML = `<span class="rem-when"></span><span class="rem-text"></span><button class="ghost small" title="${L("Löschen", "Delete")}">${ico("trash")}</button>`;
        li.querySelector(".rem-when").textContent = (r.kind === "timer" ? L("Timer · ", "Timer · ") : "") + reminderWhen(r.due);
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
    $("reminder-kind").textContent = ev.late ? L("Verpasste Erinnerung", "Missed reminder")
      : ev.kind === "timer" ? L("Timer abgelaufen", "Timer finished") : L("Erinnerung", "Reminder");
    $("reminder-text").textContent = ev.text;
    $("reminder-banner").classList.remove("hidden");
    chime();
    addSystem(ev.spoken);
    if (!$("tab-memory").classList.contains("hidden")) loadReminders();
  }
  $("reminder-ok").onclick = () => $("reminder-banner").classList.add("hidden");

  async function loadMemory() {
    try {
      const [facts, days, scope] = await Promise.all([getJSON("/api/memory/facts"), getJSON("/api/memory/days"),
        getJSON("/api/memory/scope")]);
      // Coding-Projekte haben je ein eigenes Gedächtnis – getrennt von dem der normalen Chats
      const project = scope.project ? scope.project.replace(/\/+$/, "").split("/").pop() : "";
      $("memory-scope").innerHTML = scope.mode === "coding"
        ? L("Gedächtnis des Projekts ", "Memory of project ") + `<b>${escapeHtml(project || L("ohne Ordner", "without folder"))}</b>`
          + L(" – getrennt von den normalen Chats.", " – separate from the regular chats.")
        : L("Gedächtnis der normalen Chats – Coding-Projekte haben je ein eigenes.",
          "Memory of the regular chats – coding projects each have their own.");
      $("facts").innerHTML = facts.length
        ? facts.map((f) => `<li>${escapeHtml(f.fact)}<small>${f.day}</small></li>`).join("")
        : `<li class="empty">${L("Noch keine Fakten gespeichert.", "No facts stored yet.")}</li>`;
      $("days").innerHTML = days.length
        ? days.map((d) => `<li><button data-day="${d.day}">${formatDay(d.day)}<span>${d.summary ? L("Zusammenfassung", "Summary") : L("Protokoll", "Log")}</span></button></li>`).join("")
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
    if (d.summary) html += `<h3>${L("Zusammenfassung", "Summary")}</h3><div class="body">${renderMarkdown(d.summary.replace(/^# .*\n/, ""))}</div>`;
    if (d.journal) html += `<h3>${L("Protokoll", "Log")}</h3><pre>${escapeHtml(d.journal.replace(/^# .*\n/, ""))}</pre>`;
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
