/* Orbwise – Telemetrie und Kontext im Speicher */

import { num, oldResults } from "./activity.js";
import { assistants } from "./chat.js";
import { $, L, LOCALE, S, cssVar, getJSON, orb } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { updateDetailMemory } from "./model_detail.js";
import { updateGpuChip } from "./models.js";
import { MODE_TEXT } from "./modes.js";
import { app, openSheetName } from "./nav.js";
import { send } from "./ws.js";

export let T, gbs, lastMem, ctxLabel;
let SPARK_MAX, braveWarned, braveDashboard;

// Kontext-Budget: genutzte (geschätzte) Prompt-Token im Verhältnis zum Budget des Modells
export function kTok(n) {
  return n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : String(n);
}
// Zeile unter der Eingabe: Kontext jetzt, wie viele Token des Fensters im Grafikspeicher bzw. RAM liegen (KV-Cache),
// und die Token der ganzen Sitzung (dieses Chats). Ohne Angaben bleibt der Hinweistext des Modus.
export function renderTokenLine() {
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

export function showContext(c, live = false) {
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

export function fmt(v, digits = 1) {
  return v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString(LOCALE, {
    minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function setTile(key, value, { sub = "", pct = null, digits = 1, title = "" } = {}) {
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

export function pushSpark(key, value) {
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

export function showMetrics(m) {
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
export async function loadCtxMemory() {
  try { lastMem = await getJSON("/api/llm/memory"); } catch { return; }
  showMemory(lastMem);
  if (openSheetName === "models") updateDetailMemory();
}
export function memText(m) {
  const share = m.kv_vram_share ?? 1;
  const where = share >= 0.99 ? L("komplett im Grafikspeicher", "fully in VRAM")
    : L(`${Math.round(share * 100)} % im Grafikspeicher, ${gbs(m.kv_ram)} im RAM (langsam)`,
        `${Math.round(share * 100)} % in VRAM, ${gbs(m.kv_ram)} in RAM (slow)`);
  const model = m.model_ram_offload > 64 * 1024 ** 2
    ? L(` · Modell ${Math.round((m.model_vram_share ?? 1) * 100)} % im VRAM`, ` · model ${Math.round((m.model_vram_share ?? 1) * 100)} % in VRAM`) : "";
  return where + model;
}
export function recText(m) {
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  T = { tokenTimes: [], exactTps: null, lastExact: 0, spark: {} };
  SPARK_MAX = { gpu: 100, power: null, vram: null, ram: null, tps: null, ctx: 100 };
  // Klick auf die Kontext-Kachel: jetzt zusammenfassen (wie „/compact“ in der Eingabe)
  $("tele-ctx").onclick = async () => {
    if (!S.connected) return;
    if (await ask(L("Chat jetzt zusammenfassen? Der volle Verlauf bleibt sichtbar; das Modell arbeitet danach mit der Zusammenfassung weiter.",
                         "Summarise the chat now? The full history stays visible; the model then continues with the summary."))) {
      send({ type: "compact" });
    }
  };

  // Brave Search API: Kontingent laut Brave (Stand der letzten Antwort) – bei Guthaben-Tarifen meldet Brave keinen
  // Verbrauch, dann verweist die Kachel aufs Brave-Dashboard
  braveWarned = false;
  braveDashboard = null;
  $("tele-brave").addEventListener("click", () => { if (braveDashboard) window.open(braveDashboard, "_blank", "noopener"); });

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
  gbs = (b) => `${(b / 1024 ** 3).toLocaleString(LOCALE, { maximumFractionDigits: b < 1024 ** 3 ? 2 : 1 })} GB`;
  lastMem = null;
  ctxLabel = (n) => (n >= 1048576 && n % 1048576 === 0 ? `${n / 1048576}M` : n % 1024 === 0 ? `${n / 1024}k` : kTok(n));
  setInterval(() => { if (S.connected && (app.classList.contains("drawer-open") || openSheetName === "models")) loadCtxMemory(); }, 10000);
}
