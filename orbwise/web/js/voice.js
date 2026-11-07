/* Orbwise – Stimme und Stimmeffekt */

import { A, initAudio, stopSpeech } from "./audio.js";
import { $, EN, L, S, api, getJSON, ico, store } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { openSheet } from "./nav.js";
import { loadStatus } from "./planner.js";
import { refresh } from "./state.js";

export let voiceMenu;
let pocketPoll, VM, voiceLanguages;

export function fxRate() {
  return S.fxOn ? +(1 - 0.08 * S.fxAmount).toFixed(3) : 1;
}

function renderFx() {
  $("fx-toggle").textContent = S.fxOn ? L("An", "On") : L("Aus", "Off");
  $("fx-toggle").classList.toggle("on", S.fxOn);
  $("fx-amount").value = Math.round(S.fxAmount * 100);
  $("fx-value").textContent = Math.round(S.fxAmount * 100) + " %";
  if (A.fx) A.fx.set(S.fxOn, S.fxAmount);
}
export async function openVoiceMenu() {
  const list = $("voice-list");
  let data;
  try { data = await getJSON("/api/voices"); } catch { toast(L("Stimmen nicht ladbar", "Could not load voices")); return; }
  VM.data = data;
  voiceLanguages = data.languages || [];
  list.innerHTML = "";
  const note = (box, text, cls = "") => {
    const d = document.createElement("div");
    d.className = "mm-hint " + cls;
    d.textContent = text;
    box.appendChild(d);
    return d;
  };
  voiceMenu.classList.remove("hidden");
  if (!data.available) {
    note(list, L("Sprachausgabe ist deaktiviert (voice.enabled) – es spricht der Browser.", "Speech output is disabled (voice.enabled) – the browser speaks instead."));
    return;
  }
  // ---- Piper (empfohlen)
  const pp = data.piper || {};
  const head = document.createElement("div");
  head.className = "mm-title vm-pocket-head";
  head.innerHTML = `<span></span><a href="https://rhasspy.github.io/piper-samples/" target="_blank" rel="noopener"></a>`;
  head.querySelector("span").textContent = L("Piper – empfohlen", "Piper – recommended");
  head.querySelector("a").textContent = L("Probehören ↗", "Listen ↗");
  head.querySelector("a").onclick = (e) => e.stopPropagation();
  list.appendChild(head);
  note(list, L("Schnell auf der CPU (kein Grafikspeicher), liest Zahlen, Datum und Uhrzeit richtig. Eine Stimme spricht genau ihre Sprache; Laden richtet Piper beim ersten Mal ein (~60 MB) und lädt die Stimme (20–110 MB).",
               "Fast on the CPU (no video memory), reads numbers, dates and times correctly. A voice speaks exactly its language; downloading sets up Piper the first time (~60 MB) and fetches the voice (20–110 MB)."));
  if (pp.state === "error" && pp.error && data.engine === "piper") note(list, L("Fehler: ", "Error: ") + pp.error, "bad");
  const prog = document.createElement("div");
  prog.className = "vm-progress hidden";
  prog.innerHTML = `<div class="iv-bar"><i></i></div><span></span><button type="button" class="btn small"></button>`;
  prog.querySelector("button").textContent = L("Abbrechen", "Cancel");
  prog.querySelector("button").onclick = (e) => { e.stopPropagation(); api("DELETE", "/api/piper/download").catch(() => {}); };
  list.appendChild(prog);
  VM.progress = prog;
  if (pp.downloading) piperProgress({ voice: pp.downloading, text: L("lädt …", "downloading …") });
  const mine = (pp.voices || []).filter((v) => v.language === data.language || v.language === (EN ? "en" : "de"));
  const others = (pp.voices || []).filter((v) => !mine.includes(v));
  for (const v of mine) list.appendChild(piperRow(v, pp));
  if (others.length) {
    const more = document.createElement("details");
    more.className = "vm-more";
    more.innerHTML = `<summary></summary>`;
    more.querySelector("summary").textContent = L("Weitere Sprachen", "More languages");
    more.onclick = (e) => e.stopPropagation();
    for (const v of others) more.appendChild(piperRow(v, pp));
    list.appendChild(more);
  }
  if (pp.installed) {
    const rm = document.createElement("button");
    rm.className = "model-item add";
    rm.textContent = L("Piper mit allen Stimmen entfernen", "Remove Piper with all voices");
    rm.onclick = async (e) => {
      e.stopPropagation();
      if (data.engine === "piper") { toast(L("Piper ist gerade aktiv – erst eine Pocket-Stimme wählen.", "Piper is active – choose a Pocket voice first.")); return; }
      if (!await ask(L("Piper mit allen Stimmen löschen?", "Delete Piper with all voices?"))) return;
      try { const r = await api("DELETE", "/api/piper"); toast(L(`✔ Piper entfernt (${r.freed_mb} MB frei)`, `✔ Piper removed (${r.freed_mb} MB freed)`)); openVoiceMenu(); } catch {}
    };
    list.appendChild(rm);
  }
  // ---- Pocket TTS (aufklappbar)
  const pk = data.pocket || {};
  const box = document.createElement("details");
  box.className = "vm-more vm-pocket";
  box.open = data.engine === "pocket";
  box.innerHTML = `<summary></summary>`;
  box.querySelector("summary").textContent = L("Pocket TTS – natürlicher Klang, liest Zahlen schlechter", "Pocket TTS – natural sound, weaker with numbers");
  box.onclick = (e) => e.stopPropagation();
  list.appendChild(box);
  if (!pk.installed) {
    if (!pk.supported) { note(box, L("Pocket TTS braucht eine CPU mit AVX2 (x86-64) oder ARM64.", "Pocket TTS needs a CPU with AVX2 (x86-64) or ARM64.")); return; }
    const b = document.createElement("button");
    b.className = "model-item add";
    b.innerHTML = `<div>${L("Pocket TTS installieren", "Install Pocket TTS")}</div><div class="mi-note"></div>`;
    b.querySelector(".mi-note").textContent = L("Programm wenige MB, beim ersten Start lädt es Modell und Stimme (einige hundert MB).",
                                               "The program is a few MB; on first start it downloads the model and voice (a few hundred MB).");
    b.onclick = async (e) => {
      e.stopPropagation();
      b.disabled = true;
      b.querySelector(".mi-note").textContent = L("Lade Pocket TTS …", "Downloading Pocket TTS …");
      try { await api("POST", "/api/pocket/install"); toast(L("✔ Pocket TTS installiert", "✔ Pocket TTS installed")); loadStatus(); openVoiceMenu(); }
      catch { b.disabled = false; b.querySelector(".mi-note").textContent = L("Nochmal versuchen?", "Try again?"); }
    };
    box.appendChild(b);
    return;
  }
  if (pk.state === "starting") note(box, L("Startet … beim ersten Mal werden Modell und Stimme heruntergeladen.", "Starting … the first time, the model and voice are downloaded."));
  else if (pk.state === "error" && pk.error) note(box, L("Fehler: ", "Error: ") + pk.error);
  for (const v of data.voices) box.appendChild(voiceRow(v, data.languages));
  const rm = document.createElement("button");
  rm.className = "model-item add";
  rm.textContent = L("Pocket TTS entfernen", "Remove Pocket TTS");
  rm.onclick = async (e) => {
    e.stopPropagation();
    if (!await ask(L("Pocket TTS mit Modell und Stimmen löschen?", "Delete Pocket TTS with its model and voices?"))) return;
    try {
      const r = await api("DELETE", "/api/pocket");
      toast(L(`✔ Pocket TTS entfernt (${r.freed_mb} MB frei)`, `✔ Pocket TTS removed (${r.freed_mb} MB freed)`));
      loadStatus();
      openVoiceMenu();
    } catch { /* Meldung kommt von api() */ }
  };
  box.appendChild(rm);
  clearTimeout(pocketPoll);
  if (pk.state === "starting") pocketPoll = setTimeout(() => { if (voiceMenu.offsetParent) openVoiceMenu(); }, 3000);
}
function piperRow(v, pp) {
  const b = document.createElement("button");
  b.className = "model-item" + (v.current ? " active" : "");
  b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
  b.querySelector(".mi-name").textContent = `${v.label} · ${langLabel(v.language)}`;
  const busy = pp.downloading === v.id;
  b.querySelector(".mi-tag").textContent = v.current ? L("Aktiv", "Active") : busy ? L("lädt …", "loading …")
    : v.downloaded ? (v.male ? L("männlich", "male") : L("weiblich", "female")) : L(`Laden · ~${v.size_mb} MB`, `Download · ~${v.size_mb} MB`);
  b.querySelector(".mi-sub").textContent = [v.note, v.quality, v.recommended ? L("empfohlen", "recommended") : ""].filter(Boolean).join(" · ");
  b.onclick = async (e) => {
    e.stopPropagation();
    if (v.current) return;
    if (v.downloaded) return chooseVoice({ name: v.name, label: v.label }, null);
    try { await api("POST", `/api/piper/voices/${encodeURIComponent(v.id)}`); piperProgress({ voice: v.id, text: L("startet …", "starting …") }); } catch {}
  };
  const row = document.createElement("div");
  row.className = "model-row";
  row.append(b);
  if (v.downloaded) {
    const play = document.createElement("button");
    play.className = "model-del voice-play";
    play.innerHTML = ico("play");
    play.title = L("Anhören", "Preview");
    play.onclick = (e) => { e.stopPropagation(); previewVoice(v.name); };
    row.append(play);
    if (!v.current) {
      const del = document.createElement("button");
      del.className = "model-del";
      del.innerHTML = ico("trash");
      del.title = L("Stimme löschen", "Delete voice");
      del.onclick = async (e) => {
        e.stopPropagation();
        try { const r = await api("DELETE", `/api/piper/voices/${encodeURIComponent(v.id)}`); toast(L(`Stimme gelöscht (${r.freed_mb} MB frei)`, `Voice deleted (${r.freed_mb} MB freed)`)); openVoiceMenu(); } catch {}
      };
      row.append(del);
    }
  }
  return row;
}
export function piperProgress(ev) {
  const p = VM.progress;
  if (ev.done) {
    if (ev.ok) toast(L("Stimme geladen und aktiv.", "Voice downloaded and active."));
    else toast(L("Stimme nicht geladen – ", "Voice not downloaded – ") + L("Details im Protokoll.", "see the log."));
    if (voiceMenu.offsetParent) openVoiceMenu();
    return;
  }
  if (!p || !p.isConnected) return;
  p.classList.remove("hidden");
  p.querySelector("i").style.width = (ev.pct || 0) + "%";
  p.querySelector("span").textContent = ev.text || "";
}

async function chooseVoice(v, language) {
  try {
    const r = await api("POST", "/api/voices/select", { voice: v.name, language });
    toast(L(`Stimme: ${v.label} · ${langLabel(r.language)}`, `Voice: ${v.label} · ${langLabel(r.language)}`));
  } catch { /* Meldung kommt von api() */ }
}
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
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
  voiceMenu = $("voice-menu");
  pocketPoll = null;
  VM = { data: null, progress: null };

  voiceLanguages = [];

  $("pill-voice").onclick = () => openSheet("settings", "voice");
}
