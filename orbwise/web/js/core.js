/* Orbwise – Grundlagen: DOM-Helfer, Sprache, gespeicherte Einstellungen, Symbole, gemeinsamer Zustand S, REST-Aufrufe */

import { toast } from "./dialogs.js";
import { signedOut } from "./planner.js";

export let $, orb, EN, L, LOCALE, LABELS, STATUS_TEXT, store, ico, cssVar, AUTO_LABEL, S;

// Denkstufe: off | low | medium | high (früher gespeichert als true/false)
// Denken: "off", "on" (Modelle ohne Stufen) oder eine Stufe, die das Modell selbst vorgibt (z. B. low/medium/high)
export function thinkLevel(v) { return v === true ? "on" : typeof v === "string" && /^[a-z][a-z-]{1,15}$/.test(v) ? v : "off"; }
// Stufen heißen so, wie das Modell sie nennt (z. B. "low", "xhigh") – keine eigenen Namen oder Stufen
export function thinkName(k) { return k; }
export async function getJSON(url) {
  const r = await fetch(url);
  if (r.status === 401) signedOut();
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

export async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {},
                               body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) signedOut();
  if (!r.ok) {
    let msg = `${L("Fehler", "Error")} ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* egal */ }
    toast(msg);
    throw new Error(msg);
  }
  return r.json();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  $ = (id) => document.getElementById(id);
  orb = new window.Orb($("orb"), $("orb-overlay"));
  // Sprache: der Server liefert index.html bereits mit lang="de" bzw. lang="en" aus
  EN = document.documentElement.lang === "en";
  L = (de, en) => (EN ? en : de);
  LOCALE = EN ? "en-GB" : "de-DE";

  LABELS = {
    offline: "Offline", idle: L("Bereit", "Ready"), listening: L("Höre zu", "Listening"),
    thinking: L("Denke nach", "Thinking"), speaking: L("Spreche", "Speaking"), executing: L("Führe aus", "Running"),
    confirm: L("Warte auf Freigabe", "Awaiting approval"), error: L("Fehler", "Error"),
  };
  STATUS_TEXT = EN
    ? { running: "running", waiting: "waiting", ok: "done", denied: "denied", blocked: "blocked", error: "error",
        planned: "planned" }
    : { running: "läuft", waiting: "wartet", ok: "fertig", denied: "abgelehnt", blocked: "blockiert", error: "fehler",
        planned: "geplant" };

  store = {
    get(k, d) { try { const v = localStorage.getItem("orbwise." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("orbwise." + k, JSON.stringify(v)); } catch { /* egal */ } },
  };

  // SVG-Symbol aus dem Sprite in index.html; Farbe/Wert einer CSS-Variablen (für Canvas)
  ico = (name) => `<svg class="i"><use href="#i-${name}"/></svg>`;
  cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  // Etikett am Knopf: immer mit „Auto“ davor – „Lesen“ allein läse sich wie eine Aktion
  AUTO_LABEL = { off: L("Auto: aus", "Auto: off"), read: L("Auto: Lesen", "Auto: read"), files: L("Auto: Dateien", "Auto: files"),
                 auto: L("Auto: alles", "Auto: all") };
  S = {
    name: "Orbwise", wakePhrase: "Wake-Word",  // aus „hello“: assistant_name und der Satz des Wake-Word-Modells
    previews: {},  // Diff-Vorschau je Werkzeug-Aufruf (für die Freigabe-Karte)
    commands: {},  // der Befehl, den ein Werkzeug ausführen wird (für die Freigabe-Karte)
    question: null,  // offene Rückfrage (Werkzeug question)
    ws: null, connected: false, retry: 0,
    serverState: "idle", substate: "",
    tts: store.get("tts", true), wake: store.get("wake", false), think: thinkLevel(store.get("think", "off")), autoMode: store.get("autoMode", store.get("auto", true) === false ? "off" : "read"), plan: store.get("plan", false),
    fxOn: store.get("fx", true), fxAmount: store.get("fxAmount", 0.6),
    recording: false, transcribing: false, streamMic: false,
    playing: false, confirm: null, confirmListenSent: false,
    historyLoaded: false, status: null, errorUntil: 0, modelDoneAt: 0, startupModel: null,
  };
}
