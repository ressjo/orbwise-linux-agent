/* Orbwise – Modellseite: Details zum gewählten Modell – Speicher, Kontextfenster, KV-Cache, mehrere GPUs, Batch-Größe */

import { num } from "./activity.js";
import { $, L, S, api, store } from "./core.js";
import { ask, askText, toast } from "./dialogs.js";
import { popMenu } from "./history.js";
import { deleteModel, renderDiag, renderPerf, renderQuality, renderSampling, renderSpec } from "./model_tuning.js";
import { FIT_MARKS, MP, activateModel, choose, fitText, hint, isLive, lock, modelCtx, refreshModelViews, restartNote, unloadModel } from "./models.js";
import { ctxLabel, fmt, gbs, lastMem, loadCtxMemory, memText, recText } from "./telemetry.js";

let KV_KNOWN;

// ---- Reiter „Meine Modelle“: Details zum gewählten Modell
export function renderModelDetail() {
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
      <div class="seg md-tabs" role="tablist"></div>
      <div class="md-pane" data-pane="overview">
        <section class="md-sec md-ctx"><div class="md-sec-head"><h4></h4>
          <div class="seg md-mode"><button type="button" data-m="tools">Tools</button><button type="button" data-m="coding">Coding</button></div></div>
          <div class="ctx-now"></div><div class="ctx-pick"></div><p class="set-hint"></p></section>
      </div>
      <div class="md-pane" data-pane="perf">
        <section class="md-sec md-moe"><div class="md-sec-head"><h4></h4></div><div class="moe-now"></div><div class="moe-opts"></div><p class="set-hint"></p></section>
        <section class="md-sec md-gpus"><div class="md-sec-head"><h4></h4></div><div class="gpu-mode"></div><div class="gpu-rows"></div><p class="set-hint"></p></section>
        <section class="md-sec md-perf"><div class="md-sec-head"><h4></h4></div><div class="batch-stats"></div><div class="tune-slot"></div><p class="set-hint"></p></section>
        <section class="md-sec md-batch"><div class="md-sec-head"><h4></h4></div><div class="batch-opts"></div><p class="set-hint"></p></section>
        <section class="md-sec md-spec"><div class="md-sec-head"><h4></h4></div><div class="spec-opts"></div><div class="spec-stats"></div><p class="set-hint"></p></section>
      </div>
      <div class="md-pane" data-pane="quality">
        <section class="md-sec md-quality"><div class="md-sec-head"><h4></h4></div><div class="quality-slot"></div><p class="set-hint"></p></section>
        <section class="md-sec md-kv"><div class="md-sec-head"><h4></h4></div><div class="kv-opts"></div><p class="set-hint kv-hint"></p></section>
        <section class="md-sec md-smp"><div class="md-sec-head"><h4></h4></div><div class="smp"></div></section>
      </div>
      <div class="md-pane" data-pane="diag">
        <section class="md-sec md-diag"><div class="md-sec-head"><h4></h4></div>
          <div class="md-btns"><button type="button" class="btn small ctx-test-btn"></button><button type="button" class="btn small md-log-btn"></button></div>
          <div class="ctx-test"></div><pre class="md-log hidden"></pre></section>
      </div>`;
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
  if (demo) { box.querySelectorAll(".md-pane, .md-tabs").forEach((s) => s.remove()); load.hidden = true; return; }
  updateDetailMemory();
  renderCtxSection(box.querySelector(".md-ctx"), p);
  if (p.managed && p.moe && p.moe.supported) renderMoe(box.querySelector(".md-moe"), p);
  else box.querySelector(".md-moe").remove();  // nur MoE-Modelle mit einem llama-server, der --n-cpu-moe kennt
  if (p.managed) renderPerf(box.querySelector(".md-perf"), p);
  else box.querySelector(".md-perf").remove();  // Ollama/fremder Server: Tempo regelt der Dienst selbst
  if (p.tunable) renderQuality(box.querySelector(".md-quality"), p);
  else box.querySelector(".md-quality").remove();  // nur mit Orbwises llama.cpp (llama-perplexity)
  renderSampling(box.querySelector(".md-smp"), p);
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
  renderDetailTabs(box);
}
// Reiter der Detailansicht: Übersicht · Leistung · Qualität · Diagnose – nur die mit Inhalt, der letzte bleibt gemerkt
const PANES = [["overview", "Übersicht", "Overview"], ["perf", "Leistung", "Performance"],
               ["quality", "Qualität", "Quality"], ["diag", "Diagnose", "Diagnostics"]];
function renderDetailTabs(box) {
  const tabs = box.querySelector(".md-tabs");
  const panes = [...box.querySelectorAll(".md-pane")];
  const filled = PANES.filter(([key]) => panes.some((x) => x.dataset.pane === key && x.querySelector(".md-sec")));
  let cur = store.get("model_tab", "overview");
  if (!filled.some(([key]) => key === cur)) cur = "overview";
  const show = (key) => {
    tabs.querySelectorAll("button").forEach((b) => { b.classList.toggle("on", b.dataset.pane === key); b.setAttribute("aria-selected", String(b.dataset.pane === key)); });
    panes.forEach((x) => x.classList.toggle("hidden", x.dataset.pane !== key));
  };
  for (const [key, de, en] of filled) {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.pane = key;
    b.setAttribute("role", "tab");
    b.textContent = L(de, en);
    b.onclick = () => { store.set("model_tab", key); show(key); $("mp-detail").scrollTop = 0; };
    tabs.appendChild(b);
  }
  tabs.classList.toggle("hidden", filled.length < 2);
  show(cur);
}
// Speicher des geladenen Modells (alle 10 s frisch, ohne den Rest neu zu zeichnen)
export function updateDetailMemory() {
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
  const box = sec.querySelector(".ctx-pick");
  box.innerHTML = "";
  const busy = !!MP.data.switching;
  const apply = async (ctx) => {
    lock(box);
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
  // Schieberegler über die Stufen (4k … 256k); übernommen wird kurz nach dem Loslassen bzw. der letzten Pfeiltaste
  const list = opts.slice();
  if (cur && !list.some((o) => o.ctx === cur)) list.push({ ctx: cur });
  list.sort((a, b) => a.ctx - b.ctx);
  const same = mode === "coding" && !p.ctx_coding;
  if (mode === "coding") {
    const lbl = document.createElement("label");
    lbl.className = "ctx-same";
    lbl.innerHTML = `<input type="checkbox"> <span></span>`;
    lbl.querySelector("span").textContent = L(`wie Tools (${ctxLabel(modelCtx(p, "tools"))})`, `same as Tools (${ctxLabel(modelCtx(p, "tools"))})`);
    const cb = lbl.querySelector("input");
    cb.checked = same;
    cb.disabled = busy;
    cb.onchange = () => {
      if (cb.checked) { if (p.ctx_coding) apply(0); else range.disabled = true; }
      else range.disabled = busy;
    };
    box.appendChild(lbl);
  }
  const wrap = document.createElement("div");
  wrap.className = "ctx-slider";
  wrap.innerHTML = `<input type="range" min="0" step="1"><div class="ctx-scale"></div><div class="ctx-readout"></div>`;
  box.appendChild(wrap);
  const range = wrap.querySelector("input"), readout = wrap.querySelector(".ctx-readout");
  range.max = String(list.length - 1);
  range.setAttribute("aria-label", L("Kontextfenster", "Context window"));
  let committed = Math.max(0, list.findIndex((o) => o.ctx === cur));
  range.value = String(committed);
  range.disabled = busy || same;
  const scale = wrap.querySelector(".ctx-scale");
  list.forEach((o, i) => {
    const t = document.createElement("span");
    t.textContent = ctxLabel(o.ctx);
    t.className = (o.fits === false ? "big" : "") + (o.ctx < 16384 ? " lean" : "");
    t.onclick = () => { if (!range.disabled) { range.value = String(i); show(); commit(); } };
    scale.appendChild(t);
  });
  const show = () => {
    const o = list[Number(range.value)];
    const moved = Number(range.value) !== committed;
    const parts = [moved ? `→ ${num(o.ctx)} Token` : ""].filter(Boolean);
    if (o.kv_bytes) parts.push(`KV ${gbs(o.kv_bytes)}`);
    if (o.ctx < 16384) parts.push(L("Sparmodus", "lean mode"));
    if (o.fits === false) parts.push(L("passt voraussichtlich nicht in den Grafikspeicher", "probably doesn't fit in VRAM"));
    readout.textContent = parts.join(" · ");
    readout.hidden = !parts.length;
    readout.classList.toggle("warn", o.fits === false);
    range.setAttribute("aria-valuetext", ctxLabel(o.ctx));
    const pct = list.length > 1 ? (100 * Number(range.value)) / (list.length - 1) : 0;
    range.style.setProperty("--pct", pct + "%");
  };
  let timer = null;
  const commit = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const i = Number(range.value);
      if (i === committed) return;
      const o = list[i];
      if (o.fits === false && !await ask(L(`${ctxLabel(o.ctx)} passt voraussichtlich nicht in den Grafikspeicher – Teile landen im RAM und alles wird deutlich langsamer. Trotzdem?`,
                                        `${ctxLabel(o.ctx)} probably doesn't fit in VRAM – parts move to RAM and everything gets much slower. Continue?`))) {
        range.value = String(committed); show(); return;
      }
      committed = i;
      apply(o.ctx);
    }, 600);
  };
  range.oninput = show;
  range.onchange = commit;
  show();
  hint(sec.querySelector(".set-hint"),
    L("Größer = längere Chats bis zum Zusammenfassen, braucht aber mehr Grafikspeicher. ", "Bigger = longer chats before summarising, but needs more VRAM. ")
      + restartNote(p),
    L("Unter 16k arbeitet Orbwise im Sparmodus (weniger Werkzeuge vorab, kürzere Ergebnisse). Coding kann ein eigenes Fenster haben "
      + "(z. B. größer für viel Code) – beim Wechsel zwischen Tools und Coding startet der Modell-Server dann neu.",
      "Below 16k Orbwise runs in lean mode (fewer tools up front, shorter results). Coding can have its own window "
      + "(e.g. larger for lots of code) – switching between Tools and Coding then restarts the model server."));
}
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
  choose(opts, levels.map((lv) => ({
    label: lv.label, on: p.kv_settable && lv.kv === current.kv, disabled: !p.kv_settable || !!MP.data.switching,
    note: (m && m.kv_bytes ? `KV ${gbs(m.kv_bytes * lv.factor / current.factor)} · ` : "") + lv.note(),
    click: async () => {
      if (lv.kv === current.kv) return;
      lock(opts);
      if (p.active) toast(L(`Modell-Server startet mit KV-Cache ${lv.label} neu …`, `Restarting the model server with KV cache ${lv.label} …`));
      const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/kv`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kv: lv.kv }) });
      if (!r.ok) toast(L("Ändern fehlgeschlagen: ", "Change failed: ") + ((await r.json().catch(() => ({}))).detail || r.status));
      else toast(L(`KV-Cache jetzt ${lv.label}.`, `KV cache now ${lv.label}.`));
      await loadCtxMemory();
      refreshModelViews();
    },
  })));
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
    lock(sec);
    if (p.active) toast(L("Modell-Server startet mit der neuen Aufteilung neu …", "Restarting the model server with the new split …"));
    try {
      await api("POST", `/api/models/${encodeURIComponent(p.name)}/gpus`, { split });
      toast(split ? L("Eigene Aufteilung gespeichert.", "Custom split saved.")
        : L("Automatische Aufteilung nach freiem Grafikspeicher.", "Automatic split by free VRAM."));
    } catch { /* Meldung kommt von api() */ }
    await loadCtxMemory();
    refreshModelViews();
  };
  choose(mode, [
    { label: L("Automatisch", "Automatic"), on: !own, disabled: !!MP.data.switching,
      note: L(`alle Karten · ${total.toFixed(0)} GB zusammen`, `all GPUs · ${total.toFixed(0)} GB together`), click: () => { if (own) send(null); } },
    { label: L("Eigene Aufteilung", "Custom split"), on: !!own, disabled: !!MP.data.switching,
      note: L("Anteil je Karte, 0 % = nicht nutzen", "share per GPU, 0 % = don't use"), click: () => rows.classList.remove("hidden") },
  ]);
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
  choose(opts, [512, 1024, 2048].map((n) => ({
    label: String(n), on: p.ubatch_settable && n === current, na: !p.ubatch_settable,
    disabled: !p.ubatch_settable || !!MP.data.switching,
    note: p.ubatch_settable ? notes[n] : L("nur mit llama.cpp", "llama.cpp only"),
    click: async () => {
      if (n === current) return;
      lock(opts);
      if (p.active) toast(L(`Modell-Server startet mit Batch-Größe ${n} neu …`, `Restarting the model server with batch size ${n} …`));
      try {
        await api("POST", `/api/models/${encodeURIComponent(p.name)}/ubatch`, { size: n });
        toast(L(`Batch-Größe jetzt ${n} – die Einlese-Geschwindigkeit wird bei der nächsten längeren Antwort neu gemessen.`,
                `Batch size now ${n} – the prompt speed is measured again with the next longer answer.`));
      } catch { /* Meldung kommt von api() */ }
      await loadCtxMemory();
      refreshModelViews();
    },
  })));
  hint(sec.querySelector(".set-hint"),
    L("Größer = lange Eingaben werden schneller eingelesen, kostet etwas Grafikspeicher. ", "Bigger = long inputs are read faster, costs a little VRAM. ") + restartNote(p),
    L("Wie viele Token der Server auf einmal verarbeitet. Spürbar bei langen Eingaben (Werkzeug-Ergebnisse, Dateien, PDFs, erster Start eines Chats), "
      + "kaum bei kurzen Fragen. Der größere Rechenpuffer im Grafikspeicher kann das Kontextfenster verkleinern. Einfacher: oben „Schnellste Einstellung messen“.",
      "How many tokens the server processes at once. Noticeable with long inputs (tool results, files, PDFs, the first message of a chat), "
      + "hardly with short questions. The larger compute buffer can shrink the context window. Easier: “Find the fastest setting” above."));
}

// MoE-Modelle: Experten-Gewichte der ersten N Schichten im RAM (--n-cpu-moe) – der Rest bleibt auf der GPU
function renderMoe(sec, p) {
  const m = p.moe;
  sec.querySelector("h4").textContent = L("Experten im RAM (MoE)", "Experts in RAM (MoE)");
  sec.querySelector(".moe-now").textContent = m.n
    ? L(`${m.n} von ${m.layers} Schichten · ~${fmt(m.ram_gb, 1)} GB der Experten im RAM`,
        `${m.n} of ${m.layers} layers · ~${fmt(m.ram_gb, 1)} GB of experts in RAM`)
    : L(`Alle ${m.experts} Experten auf der Grafikkarte`, `All ${m.experts} experts on the GPU`);
  const opts = sec.querySelector(".moe-opts");
  const mode = m.auto ? "auto" : m.n ? "manual" : "off";
  const autoNote = m.auto_n == null ? L("passend zum Grafikspeicher", "to fit the video memory")
    : m.auto_n ? L(`jetzt ${m.auto_n} Schichten`, `now ${m.auto_n} layers`) : L("passt ganz – nichts auslagern", "fits – nothing moved");
  const send = async (body, msg) => {
    lock(opts);
    if (p.active) toast(L("Modell-Server startet neu …", "Restarting the model server …"));
    try {
      const r = await api("POST", `/api/models/${encodeURIComponent(p.name)}/moe`, body);
      toast(msg(r));
    } catch { /* Meldung kommt von api() */ }
    await loadCtxMemory();
    refreshModelViews();
  };
  const choices = [
    ["auto", L("Automatisch", "Automatic"), autoNote],
    ["off", L("Aus", "Off"), L("alles auf der GPU – schnellstes Generieren", "all on the GPU – fastest generation")],
    ["manual", L("Eigene Zahl", "Custom"), L("Schichten selbst wählen", "choose the layers yourself")],
  ];
  choose(opts, choices.map(([key, label, note]) => ({
    label, note, on: key === mode, disabled: !!MP.data.switching,
    click: async () => {
      if (key === "auto") {
        await send({ mode: "auto" }, (r) => r.n ? L(`Experten von ${r.n} Schichten im RAM – der Rest passt in den Grafikspeicher.`, `Experts of ${r.n} layers in RAM – the rest fits in video memory.`)
                                                : L("Das Modell passt ganz in den Grafikspeicher – nichts ausgelagert.", "The model fits in video memory – nothing moved."));
      } else if (key === "off") {
        if (mode !== "off") await send({ mode: "off" }, () => L("Alle Experten wieder auf der Grafikkarte.", "All experts back on the GPU."));
      } else {
        const v = await askText(L(`Wie viele Schichten (0–${m.layers}) sollen ihre Experten in den RAM legen?`, `How many layers (0–${m.layers}) should keep their experts in RAM?`),
                                String(m.n || m.auto_n || Math.ceil(m.layers / 4)), { ok: L("Übernehmen", "Apply") });
        if (v === null) return;
        const n = parseInt(String(v), 10);
        if (!(n >= 0 && n <= m.layers)) { toast(L(`Bitte eine Zahl von 0 bis ${m.layers}.`, `Please a number from 0 to ${m.layers}.`)); return; }
        await send({ mode: "manual", n }, (r) => L(`Experten von ${r.n} Schichten im RAM.`, `Experts of ${r.n} layers in RAM.`));
      }
    },
  })));
  hint(sec.querySelector(".set-hint"),
    L("Bei MoE-Modellen rechnet je Token nur ein kleiner Teil der Experten. Liegen sie im RAM, bleibt das Modell schnell und es passt mehr Kontext oder ein genaueres Quant. ", "MoE models only use a few experts per token. Keeping them in RAM keeps the model fast and leaves room for more context or a more precise quant. ") + restartNote(p),
    L(`Attention und KV-Cache bleiben auf der Grafikkarte; nur die Experten-Gewichte der ersten Schichten (insgesamt ~${fmt(m.all_gb, 1)} GB) wandern in den Arbeitsspeicher. Das Einlesen bleibt fast so schnell, das Generieren wird etwas langsamer – je mehr Schichten, desto mehr. „Automatisch“ lagert nur so viel aus, wie für das eingestellte Kontextfenster nötig ist, und rechnet neu, wenn du Kontext oder KV-Cache änderst.`,
      `Attention and KV cache stay on the GPU; only the expert weights of the first layers (~${fmt(m.all_gb, 1)} GB in total) move to system memory. Prompt processing stays almost as fast, generation gets somewhat slower – the more layers, the more. “Automatic” moves only as much as the context window needs and recalculates when you change the context or KV cache.`));
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {

  // KV-Cache-Stufe: f16 (voll), q8_0 (~halber Speicher), q4_0 (~ein Viertel) – nur bei eigenem llama-server
  KV_KNOWN = {
    f16: { factor: 2, label: "f16", note: () => L("Standard, volle Qualität", "default, full quality") },
    q8_0: { factor: 34 / 32, label: "q8", note: () => L("~halber Speicher, kaum Verlust", "~half the memory, hardly any loss") },
    q4_0: { factor: 18 / 32, label: "q4", note: () => L("~ein Viertel, leicht ungenauer", "~a quarter, slightly less accurate") },
  };
}
