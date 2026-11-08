/* Orbwise – Aktivität: Werkzeug-Schritte und Modell-Schritte im Seitenpanel */

import { assistants, chat, pinChat } from "./chat.js";
import { renderDiff } from "./coding.js";
import { $, EN, L, LOCALE, S, STATUS_TEXT, api, ico, orb } from "./core.js";
import { activityArrived, setDrawer } from "./skills.js";
import { refresh } from "./state.js";
import { kTok } from "./telemetry.js";
import { SAYINGS, Say, sayNext } from "./ws.js";

export let acts, num, secs, oldResults;
let activity, actGroup, runGroups, MAX_GROUPS, TOOL_LABELS, TOOL_PREFIX, ACT_ICONS, CODE_ICONS, toolIcon, kChars, phaseTimer, GROUP_NAMES, groupName, groupNames, toolsChanged, REREAD, SAT_GROUPS, ICON_GROUPS, reviewText;

function toolLabel(name) {
  const hit = TOOL_LABELS[name];
  if (hit) return hit[EN ? 1 : 0];
  for (const [rx, l] of TOOL_PREFIX) {
    if (rx.test(name)) return `${l[EN ? 1 : 0]} · ${name.replace(rx, "").replace(/_/g, " ")}`.replace(/ · $/, "");
  }
  const t = name.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
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

// Neue Frage: ihre Schritte kommen in eine eigene Gruppe
export function newQuestionGroup(title) { actGroup = startActGroup(title); }

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

export function actStep(kind, icon, name, info, routine, parent) {
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

export function finishStep(el, status, seconds) {
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
export function showTodos(items) {
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
export function liveLine(text) {
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
    live.querySelector(".lt").textContent = a.reviewStep ? reviewText(a) : ev.calls && ev.calls.length
      ? L(`führt ${ev.calls.join(", ")} aus`, `running ${ev.calls.join(", ")}`) : L("gleich weiter", "continuing");
    return;
  }
  if (live._phase !== ev.id) { live._phase = ev.id; live._start = performance.now(); live.querySelector(".ls").textContent = ""; }
  const text = a.reviewStep ? reviewText(a, phaseText(ev)) : phaseText(ev);
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
export function statsLine(a, cancelled) {
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

export function modelPhase(ev) {
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
function satLabel(name) {
  const hit = SAT_GROUPS.find(([rx]) => rx.test(name));
  return hit ? hit[1][EN ? 1 : 0] : name.split("_")[0].toUpperCase();
}

export function toolCall(ev) {
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
  if (ev.name === "code_review") reviewStep(el, ev);

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

// Code-Check: Schritt in der Live-Zeile der Antwort („Code-Check: Sicherheit“) und „Stoppen“ in der Aktivität
function reviewStep(el, ev) {
  el.dataset.review = "1";
  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "btn small ghost act-stop";
  stop.textContent = L("Stoppen", "Stop");
  stop.title = L("Code-Check hier beenden – Bericht mit dem Stand bis dahin, ohne automatisches Beheben.",
                 "End the code check here – report with the state so far, no automatic fixing.");
  stop.onclick = (e) => {
    e.stopPropagation();
    stop.disabled = true;
    api("POST", "/api/review/stop").catch(() => {});
  };
  el.querySelector(".act-state").prepend(stop);
  const a = S.currentMsg && assistants[S.currentMsg];
  if (!a) return;
  a.reviewStep = String((ev.args && ev.args.step) || "").replace(/^(Durchgang|Pass): /, "");
  const live = a.live;
  live._start = performance.now();
  live.querySelector(".ls").textContent = "";
  live.querySelector(".lt").textContent = reviewText(a);
}

export function toolOutput(ev) {
  const el = acts[ev.id];
  if (!el) return;
  const out = el.querySelector(".act-out");
  out.textContent = (out.textContent + ev.text).slice(-20000);
  out.scrollTop = out.scrollHeight;
}

export function toolResult(ev) {
  delete S.previews[ev.id];
  if (!ev.sub) {
    orb.removeSatellite(ev.id, ev.status === "error" || ev.status === "blocked");
    orb.memoryGlow(ev.id, false);
  }
  const el = acts[ev.id];
  if (el) {
    el.classList.remove("waiting");
    el.querySelector(".act-stop")?.remove();
    finishStep(el, ev.status);
    if (el.dataset.review && ev.status === "denied") el.querySelector(".act-status").textContent = L("gestoppt", "stopped");
    const out = el.querySelector(".act-out");
    if (!out.textContent.trim() || ev.status !== "ok") out.textContent = ev.text;
    delete acts[ev.id];
  }
  const chip = $("chip-" + ev.id);
  if (chip) chip.className = "tool-chip " + ev.status;
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Je Frage (bzw. Routine/Telegram-Lauf) eine Gruppe, darin die Schritte in Reihenfolge – lesbare Namen mit Icon,
  // Status und Dauer rechts, Argumente und Ausgabe zum Aufklappen
  activity = $("activity");
  acts = {};
  actGroup = null;
  runGroups = {};
  MAX_GROUPS = 20;

  TOOL_LABELS = {
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
  TOOL_PREFIX = [[/^mail_/, ["Mail", "Mail"]], [/^paperless_/, ["Paperless", "Paperless"]], [/^ha_/, ["Smart Home", "Smart home"]],
    [/^calendar_/, ["Kalender", "Calendar"]], [/^obsidian_/, ["Obsidian", "Obsidian"]], [/^trilium_/, ["Trilium", "Trilium"]],
    [/^portainer_/, ["Portainer", "Portainer"]], [/^routine_/, ["Routine", "Routine"]], [/^package_|_package/, ["Pakete", "Packages"]]];
  ACT_ICONS = { shell: "code", ssh: "code", packages: "code", sysadmin: "code", system: "code", power: "power",
    portainer: "code", web: "search", files: "file", apps: "bolt", memory_tools: "brain", calendar_tools: "calendar",
    reminder_tools: "bell", mail: "chat", telegram_tools: "chat", vision: "image", images: "image", paperless: "file" };
  CODE_ICONS = { read: "file", edit: "file", write: "file", grep: "search", glob: "search", shell: "code",
    job_output: "code", websearch: "search", webfetch: "search", subagent: "brain", question: "chat", screenshot: "image" };
  toolIcon = (ev) => (ev.group === "code" ? CODE_ICONS[ev.name] : ACT_ICONS[ev.group]) || "bolt";

  // ---------------------------------------------------------------- Modell-Schritte in der Aktivität
  // Zwischen den Werkzeugen arbeitet das Modell unsichtbar (Prompt einlesen, denken, Aufruf schreiben …) –
  // je Schritt eine Zeile mit Phase und mitlaufender Zeit, danach eine Zusammenfassung, wohin die Zeit ging.
  num = (n) => Number(n || 0).toLocaleString(LOCALE);
  secs = (s) => `${Number(s).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} s`;
  kChars = (n) => (n >= 1000 ? `${(n / 1000).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} k` : String(n || 0));
  phaseTimer = null;

  GROUP_NAMES = {
    paperless: "Paperless", mail: "Mail", homeassistant: "Smart Home", calendar_tools: L("Kalender", "Calendar"),
    sysadmin: "System", packages: L("Pakete", "Packages"), routine_tools: L("Routinen", "Routines"),
    trilium: "Trilium", obsidian: "Obsidian", vision: L("Bildschirm", "Screen"), telegram_tools: "Telegram",
    weather: L("Wetter", "Weather"), power: L("Ausschalten", "Power"), apps: L("Programme", "Apps"),
    briefing: L("Tagesüberblick", "Briefing"), reminder_tools: L("Erinnerungen", "Reminders"),
    files: L("Dateien", "Files"), web: "Web", system: L("Systeminfo", "System info"),
    todo_tools: L("Aufgabenliste", "Task list"), memory_tools: L("Gedächtnis", "Memory"),
    portainer: "Docker", ssh: "SSH", image_tools: L("Bilder", "Images"),
  };
  oldResults = (n) => n === 1 ? L("1 altes Werkzeug-Ergebnis", "1 old tool result")
    : L(`${num(n)} alte Werkzeug-Ergebnisse`, `${num(n)} old tool results`);
  // „paperless:write“ = der ändernde Teil einer Gruppe (kleines Kontextfenster)
  groupName = (g) => g.endsWith(":write") ? `${GROUP_NAMES[g.slice(0, -6)] || g.slice(0, -6)} ${L("(ändern)", "(changes)")}`
    : GROUP_NAMES[g] || g;
  groupNames = (groups) => (groups || []).map(groupName).join(", ");
  toolsChanged = (added, dropped) => (added && added.length ? L("Werkzeuge dazugeladen: ", "tools added: ") + groupNames(added) : "")
    + (dropped && dropped.length ? (added && added.length ? " · " : "") + L("weggelassen: ", "dropped: ") + groupNames(dropped) : "");

  // Warum der Server viel neu einlesen musste (reread.py) – nur erwähnenswert, wenn es wirklich viel war
  REREAD = {
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

  // Kurzname für den Werkzeug-Satelliten am Orb
  SAT_GROUPS = [
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

  // Symbol je Aktionsart (weitere Symbole: ICONS in orb.js); Gedächtnis leuchtet im Kern statt als Satellit
  ICON_GROUPS = { code: "cli", shell: "cli", packages: "cli", sysadmin: "cli", system: "cli", power: "cli", web: "cloud",
                        mail: "mail", paperless: "paperless", vision: "eye" };
  reviewText = (a, more) => L("Code-Check: ", "Code check: ") + a.reviewStep + (more ? " · " + more : "");
}
