/* Orbwise – Audio: Wiedergabe der Sprachausgabe, Pegel für den Orb */

import { EN, L, S, orb } from "./core.js";
import { toast } from "./dialogs.js";
import { startListen } from "./mic.js";
import { refresh } from "./state.js";
import { fxRate } from "./voice.js";
import { send } from "./ws.js";

export let A;
let germanVoice, SPEECH_LOCALES;

export async function initAudio() {
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

export async function initMic() {
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

export function updateMicStreaming() {
  S.streamMic = A.micReady && (S.wake || S.recording);
}

function b64ToBuffer(b64) {
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function pickVoice() {
  if (!window.speechSynthesis) return;
  const voices = speechSynthesis.getVoices();
  germanVoice = voices.find((v) => /de[-_]DE/i.test(v.lang) && /male|mann|stefan|markus/i.test(v.name))
    || voices.find((v) => /^de/i.test(v.lang)) || null;
}

export function enqueueSpeech(ev) {
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

export function stopSpeech(notifyServer) {
  A.gen++;
  A.queue.length = 0;
  try { if (A.current) A.current.stop(); } catch { /* schon gestoppt */ }
  A.current = null;
  if (window.speechSynthesis) speechSynthesis.cancel();
  S.playing = false;
  if (notifyServer) send({ type: "speech_interrupt" });
  refresh();
}

export function onSpeechDrained() {
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  A = { ctx: null, outAnalyser: null, micAnalyser: null, micNode: null, micReady: false,
              queue: [], gen: 0, current: null, micLevel: 0, spec: null, synthLevel: 0 };

  germanVoice = null;
  if (window.speechSynthesis) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }

  SPEECH_LOCALES = { de: "de-DE", en: "en-GB", fr: "fr-FR", es: "es-ES", it: "it-IT", pt: "pt-PT", nl: "nl-NL" };
  requestAnimationFrame(levelLoop);
}
