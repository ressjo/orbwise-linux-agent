/* Orbwise – Modellseite: Leistung messen, spekulatives Decoding, Sampling, Diagnose, Löschen */

import { num } from "./activity.js";
import { L, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { MP, hint, refreshModelViews, restartNote } from "./models.js";
import { ctxLabel, fmt, gbs, lastMem } from "./telemetry.js";

export let FLAVOR_LABEL;
let SAMPLING_FIELDS;

// Leistung: Einlese-Tempo, Messen der schnellsten Einstellung (Build × Batch-Größe)
export function renderPerf(sec, p) {
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
// Qualität messen: wie nah sind KV-Cache-Stufen und andere Quants am genauesten vorhandenen Stand (llama-perplexity)
export function renderQuality(sec, p) {
  sec.querySelector("h4").textContent = L("Qualität messen", "Measure quality");
  const slot = sec.querySelector(".quality-slot");
  const box = document.createElement("div");
  box.className = "tune-box";
  box.innerHTML = `<div class="md-btns"><button type="button" class="btn small q-btn"></button></div><div class="q-res"></div>`;
  slot.appendChild(box);
  const btn = box.querySelector(".q-btn");
  const running = (MP.data.pulls || []).some((x) => x.tag === "Quality");
  btn.textContent = running ? L("Misst gerade …", "Measuring …") : p.quality ? L("Erneut messen", "Measure again") : L("Qualität messen", "Measure quality");
  btn.disabled = running || !!MP.data.switching;
  btn.onclick = async () => {
    if (!await ask(L("Vergleicht dieses Modell mit KV-Cache f16/q8/q4 und andere Quants desselben Modells im selben Ordner mit der genauesten vorhandenen Fassung – auf einem festen deutschen und englischen Text (beim ersten Mal von Wikipedia geladen). "
                   + "Dauert meist ein paar Minuten je Variante; das Modell wird dafür entladen und danach wieder geladen, Chats warten so lange. Kurzzeitig braucht die Messung einige GB Platz im Cache-Ordner. Starten?",
                   "Compares this model with KV cache f16/q8/q4 and other quants of the same model in the same folder against the most precise version available – on a fixed German and English text (downloaded from Wikipedia the first time). "
                   + "Usually takes a few minutes per variant; the model is unloaded for it and loaded again afterwards, chats wait meanwhile. The measurement briefly needs a few GB in the cache folder. Start?"))) return;
    btn.disabled = true;
    try { await api("POST", `/api/models/${encodeURIComponent(p.name)}/quality`); btn.textContent = L("Misst gerade …", "Measuring …"); }
    catch { btn.disabled = false; }
    refreshModelViews();
  };
  const q = p.quality;
  const res = box.querySelector(".q-res");
  if (q) {
    const pct = (v) => (v == null ? "–" : `${fmt(v, 1)} %`);
    const dec = (v, d = 3) => (v == null ? "–" : fmt(v, d));
    const rows = (q.variants || []).map((v) => `<tr class="${v.current ? "best" : ""}"><td></td>`
      + (v.error ? `<td colspan="3" class="err"></td>` : `<td>${pct(v.same_top)}</td><td>${dec(v.kld, 4)}</td><td>${dec(v.ppl, 2)}</td>`)
      + `<td>${v.size_gb ? `${fmt(v.size_gb, 1)} GB` : ""}</td><td>${v.current ? L("aktiv", "active") : ""}</td></tr>`).join("");
    res.innerHTML = `<table class="tune-table"><thead><tr><th>${L("Variante", "Variant")}</th><th>${L("gleiches Top-Token", "same top token")}</th>`
      + `<th>KL</th><th>${L("Perplexität", "Perplexity")}</th><th>${L("Datei", "File")}</th><th></th></tr></thead><tbody>${rows}</tbody></table><p class="set-hint q-note"></p>`;
    res.querySelectorAll("tbody tr").forEach((tr, i) => {
      const v = q.variants[i];
      tr.firstElementChild.textContent = v.label;
      const err = tr.querySelector("td.err");
      if (err) err.textContent = v.error;
    });
    const notes = [L(`Verglichen mit ${q.reference} auf ${num(q.tokens)} Token, gemessen am ${new Date(q.at * 1000).toLocaleDateString()}.`,
                     `Compared with ${q.reference} on ${num(q.tokens)} tokens, measured on ${new Date(q.at * 1000).toLocaleDateString()}.`)];
    const kv8 = (q.variants || []).find((v) => v.kind === "kv" && v.kv === "q8_0" && v.same_top != null);
    if (kv8) notes.push(kv8.same_top >= 98.5 ? L(`KV-Cache q8 wählt in ${fmt(kv8.same_top, 1)} % dasselbe Token – kostet praktisch nichts und spart rund die Hälfte des Cache-Speichers.`,
                                                `KV cache q8 picks the same token ${fmt(kv8.same_top, 1)} % of the time – costs practically nothing and saves about half of the cache memory.`)
                                             : L(`KV-Cache q8 weicht hier spürbar ab (${fmt(kv8.same_top, 1)} % gleich) – f16 lassen, wenn der Platz reicht.`,
                                                `KV cache q8 differs noticeably here (${fmt(kv8.same_top, 1)} % same) – keep f16 if there is room.`));
    (q.notes || []).forEach((n) => notes.push(n));
    res.querySelector(".q-note").textContent = notes.join(" ");
  }
  hint(sec.querySelector(".set-hint"),
    L("Zeigt, wie nah KV-Cache-Stufen und andere Quants am Original sind – gemessen statt geraten.", "Shows how close KV cache levels and other quants are to the original – measured, not guessed."),
    L("„Gleiches Top-Token“: wie oft die Variante dasselbe wahrscheinlichste nächste Wort wählt wie die Referenz (über 98 % ist kaum zu merken). KL: wie stark die Wahrscheinlichkeiten im Mittel abweichen (unter 0,01 sehr klein). "
      + "Perplexität: wie gut der Text vorhergesagt wird (kleiner ist besser). Das misst die Nähe zum Original, nicht wie gut das Modell Aufgaben löst. Weitere Quants zum Vergleich: im Reiter „Hinzufügen“ in denselben Ordner laden.",
      "“Same top token”: how often the variant picks the same most likely next word as the reference (above 98 % is hard to notice). KL: how much the probabilities differ on average (below 0.01 is very small). "
      + "Perplexity: how well the text is predicted (lower is better). This measures closeness to the original, not how well the model solves tasks. More quants to compare: download them into the same folder in the “Add” tab."));
}

// Spekulatives Decoding: das Modell schlägt mehrere Token vor, der Server prüft sie auf einmal – schneller antworten
export function renderSpec(sec, p) {
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
export async function renderSampling(sec, p) {
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
export function renderDiag(sec, p) {
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
export async function deleteModel(p) {
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {

  // „Schnellste Einstellung messen“: llama-bench über Build (ROCm/Vulkan bzw. CUDA/Vulkan) × Batch-Größe
  FLAVOR_LABEL = { rocm: "ROCm", vulkan: "Vulkan", cuda: "CUDA", cpu: "CPU" };

  // Sampling je Modell und Modus (mit/ohne Denken) – gilt ab der nächsten Antwort
  SAMPLING_FIELDS = [
    ["temperature", L("Temperatur", "Temperature"), 0.05], ["top_p", "top_p", 0.05], ["top_k", "top_k", 1],
    ["min_p", "min_p", 0.01], ["presence_penalty", "presence", 0.1], ["repeat_penalty", "repeat", 0.05],
  ];
}
