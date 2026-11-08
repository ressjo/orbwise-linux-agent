/* Orbwise – Anhänge: Bilder und PDFs (Büroklammer, Einfügen, Ziehen) */

import { initMic, stopSpeech, updateMicStreaming } from "./audio.js";
import { escapeHtml } from "./chat.js";
import { $, L, LOCALE, S, api, getJSON, store, thinkLevel, thinkName } from "./core.js";
import { toast } from "./dialogs.js";
import { refresh } from "./state.js";
import { send } from "./ws.js";

let ATT, isPdf, composer, thinkMenu, autoMenu, sudoUntil, sudoTimer;

function docLine(d) {
  if (!d) return L("wird gelesen …", "reading …");
  if (d.scanned) return L("kein Text – gescannt?", "no text – scanned?");
  return d.pages === 1 ? L("1 Seite", "1 page") : L(`${d.pages} Seiten`, `${d.pages} pages`);
}
export function fileChip(title, d, onclick) {
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
function setThinkMenu(open) {
  thinkMenu.classList.toggle("hidden", !open);
  $("btn-think").setAttribute("aria-expanded", String(open));
}
// Was das geladene Modell kann: "levels" (eigene Stufen des Modells), "toggle" (nur an/aus), "none" – null =
// unbekannt (an/aus). Stufen gibt es nur, wenn das Modell sie selbst vorgibt – keine erfundenen von Orbwise.
export async function loadThinkCaps() {
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
export function renderThink() {
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
export function applySettings(ev, fromHello) {
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

// Darstellung: hell, dunkel oder wie das System (das Skript im <head> setzt <html data-theme>)
function renderTheme() {
  const pick = store.get("theme", "system");
  document.querySelectorAll("[data-theme-pick]").forEach((b) => b.classList.toggle("on", b.dataset.themePick === pick));
}
// Orb im Chat: Größe (aus/klein/mittel/groß) und Platz (links, rechts, oben rechts) – je Gerät gemerkt wie die Darstellung
const ORB_SIZES = ["off", "s", "m", "l"], ORB_POS = ["bl", "br", "tr"];
function renderOrb() {
  const size = ORB_SIZES.includes(store.get("orb_size", "m")) ? store.get("orb_size", "m") : "m";
  const pos = ORB_POS.includes(store.get("orb_pos", "br")) ? store.get("orb_pos", "br") : "br";
  document.documentElement.dataset.orbSize = size;
  document.documentElement.dataset.orbPos = pos;
  document.querySelectorAll("[data-orb-size]").forEach((b) => b.classList.toggle("on", b.dataset.orbSize === size));
  document.querySelectorAll("[data-orb-pos]").forEach((b) => {
    b.classList.toggle("on", b.dataset.orbPos === pos);
    b.disabled = size === "off";
  });
}
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
export function sudoCached(ev) {
  const was = sudoUntil;
  sudoUntil = ev.until || null;
  if (sudoUntil && !sudoTimer) sudoTimer = setInterval(showSudo, 30000);
  if (sudoUntil && !was) toast(L("Root-Passwort gemerkt – Einstellungen → Status zum Vergessen.",
                                 "Root password remembered – Settings → Status to forget it."));
  showSudo();
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  ATT = { items: [] };
  isPdf = (f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name || "");
  $("btn-attach").onclick = () => $("attach-input").click();
  $("attach-input").onchange = (e) => { attachFiles([...e.target.files]); e.target.value = ""; };
  $("input").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); attachFiles(files); }
  });
  composer = $("form");
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
  thinkMenu = $("think-menu");
  $("btn-think").onclick = (e) => { e.stopPropagation(); setThinkMenu(thinkMenu.classList.contains("hidden")); };
  document.querySelectorAll("[data-think]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); setThinkMenu(false); setThink(b.dataset.think); };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#think-wrap")) setThinkMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !thinkMenu.classList.contains("hidden")) setThinkMenu(false); });
  document.querySelectorAll("[data-theme-pick]").forEach((b) => {
    b.onclick = () => { store.set("theme", b.dataset.themePick); window.orbwiseApplyTheme(b.dataset.themePick); renderTheme(); };
  });
  renderTheme();
  document.querySelectorAll("button[data-orb-size]").forEach((b) => {
    b.onclick = () => { store.set("orb_size", b.dataset.orbSize); renderOrb(); };
  });
  document.querySelectorAll("button[data-orb-pos]").forEach((b) => {
    b.onclick = () => { store.set("orb_pos", b.dataset.orbPos); renderOrb(); };
  });
  renderOrb();

  // Auto-Modus: aufklappbarer Knopf neben „Denken“ (und Auswahl in den Einstellungen)
  autoMenu = $("auto-menu");
  $("btn-auto").onclick = (e) => { e.stopPropagation(); setAutoMenu(autoMenu.classList.contains("hidden")); };
  document.querySelectorAll("[data-auto]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); setAutoMenu(false); setAutoMode(b.dataset.auto); };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#auto-wrap")) setAutoMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !autoMenu.classList.contains("hidden")) setAutoMenu(false); });

  // Gemerktes sudo-Passwort: Anzeige in den Einstellungen (Status) mit „Vergessen“
  sudoUntil = null;
  sudoTimer = null;
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
}
