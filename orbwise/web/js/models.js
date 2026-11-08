/* Orbwise – Modellseite (wie LM Studio): Modelle laden, einstellen, hinzufügen */

import { num } from "./activity.js";
import { addSystem, copyText } from "./chat.js";
import { $, L, LOCALE, S, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { chatWhen, popMenu } from "./history.js";
import { IM, IV, activateImageModel, loadImageModels, openImageView, renderImageModels } from "./images.js";
import { openSheet, openSheetName } from "./nav.js";
import { ctxLabel, fmt, gbs, lastMem, loadCtxMemory, memText, recText } from "./telemetry.js";

export let MP, FIT_MARKS;
let fitText, modelCtx, isLive, restartNote, KV_KNOWN, FLAVOR_LABEL, SAMPLING_FIELDS, HF, kCtx;

function openModelsPage(tab) {
  if (tab) MP.tab = tab;
  openSheet("models");
}
export async function renderModelsPage() {
  const imaging = IV.open;  // im Bild-Modus: nur die Bildmodelle
  $("mp-title").textContent = imaging ? L("Bildmodelle", "Image models") : L("Modelle", "Models");
  $("sheet-models").classList.toggle("mp-imaging", imaging);
  $("mp-image").classList.toggle("hidden", !imaging);
  if (imaging) {
    ["mp-mine", "mp-add", "mp-api", "mp-pulls"].forEach((id) => $(id).classList.add("hidden"));
    await loadImageModels();
    renderImageModels();
    return;
  }
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
// nach Wechsel, Download oder Änderung neu zeichnen – Eingaben im Hinzufügen-Reiter bleiben stehen
export async function refreshModelViews() {
  if (openSheetName === "models") await renderModelsPage();
}

// Kopf: Grafikkarte mit Belegung (live aus den Metriken) und llama.cpp-Version
function renderModelsHead() {
  updateGpuChip();
  renderLlamacppChip();
}
export function updateGpuChip() {
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

// ---- Eigenes Backend (Experten): ein Server, den Orbwise nicht startet – Sprachmodell (OpenAI-kompatibel) oder Bild
export function backendForm(image) {
  const box = document.createElement("details");
  box.className = "ext-backend";
  box.innerHTML = `<summary></summary><p class="set-hint"></p>
      <div class="ext-grid">
        <label class="ext-kind hidden"><span></span><select name="kind"></select></label>
        <label><span></span><input name="label" type="text" spellcheck="false"></label>
        <label class="ext-wide"><span></span><input name="base_url" type="url" spellcheck="false"></label>
        <label><span></span><input name="api_key" type="password" autocomplete="off" spellcheck="false"></label>
        <label><span></span><input name="model" type="text" list="" spellcheck="false"><datalist></datalist></label>
        <label class="ext-ctx"><span></span><input name="context" type="number" min="1024" step="1024"></label>
      </div>
      <div class="ext-actions"><button type="button" class="btn small ext-probe"></button>
        <button type="button" class="btn primary small ext-save"></button><span class="ext-state"></span></div>`;
  const f = (n) => box.querySelector(`[name="${n}"]`);
  const label = (n, de, en) => { f(n).closest("label").querySelector("span").textContent = L(de, en); };
  box.querySelector("summary").textContent = L("Eigenes Backend (für Experten)", "Own backend (experts)");
  box.querySelector(".set-hint").textContent = image
    ? L("Einen Bildserver nutzen, der schon läuft – Orbwise startet und lädt dort nichts, rechnet nicht selbst und gibt keinen Grafikspeicher frei. Größe, Schritte und Seed werden mitgeschickt, soweit die Schnittstelle sie kennt.",
        "Use an image server that is already running – Orbwise starts and loads nothing there, does not compute itself and frees no video memory. Size, steps and seed are sent as far as the interface knows them.")
    : L("Einen beliebigen OpenAI-kompatiblen Server nutzen (z. B. Strata, vLLM, LM Studio, llama-server auf einem anderen Rechner). Orbwise startet dort nichts – Kontextfenster, KV-Cache und Grafikkarten legt der Server fest. Das Kontextfenster hier sagt Orbwise nur, wie viel hineinpasst (leer: der Server meldet es oder der Standard gilt).",
        "Use any OpenAI-compatible server (e.g. Strata, vLLM, LM Studio, llama-server on another machine). Orbwise starts nothing there – context window, KV cache and GPUs are up to the server. The context window here only tells Orbwise how much fits (empty: the server reports it or the default applies).");
  if (image) {
    box.querySelector(".ext-kind").classList.remove("hidden");
    label("kind", "Schnittstelle", "Interface");
    for (const [v, t] of [["openai", "OpenAI Images API (/v1/images/generations)"], ["a1111", "Automatic1111 / Forge (/sdapi/v1/txt2img)"],
                           ["sdcpp", "stable-diffusion.cpp sd-server (/sdcpp/v1)"]]) f("kind").add(new Option(t, v));
    box.querySelector(".ext-ctx").classList.add("hidden");
  }
  label("label", "Name", "Name");
  label("base_url", "Adresse", "Address");
  label("api_key", "API-Schlüssel (optional)", "API key (optional)");
  label("model", image ? "Modell (optional)" : "Modell", image ? "Model (optional)" : "Model");
  label("context", "Kontextfenster (optional, Token)", "Context window (optional, tokens)");
  const hintUrl = () => {
    f("base_url").placeholder = image ? { openai: "http://localhost:8000/v1", a1111: "http://localhost:7860", sdcpp: "http://localhost:1234" }[f("kind").value]
      : "http://localhost:8000/v1";
  };
  hintUrl();
  f("label").placeholder = image ? L("z. B. Forge im Keller", "e.g. Forge in the basement") : "z. B. Strata";
  f("model").placeholder = image ? L("leer: Standard des Servers", "empty: the server's default") : L("„Verbindung prüfen“ füllt die Auswahl", "“Test connection” fills the list");
  const listId = "ext-models-" + (image ? "img" : "llm");
  box.querySelector("datalist").id = listId;
  f("model").setAttribute("list", listId);
  const state = box.querySelector(".ext-state");
  const values = () => Object.fromEntries(["kind", "label", "base_url", "api_key", "model", "context"].map((n) => [n, f(n).value.trim()]));
  if (image) f("kind").onchange = hintUrl;
  box.querySelector(".ext-probe").textContent = L("Verbindung prüfen", "Test connection");
  box.querySelector(".ext-save").textContent = L("Hinzufügen und nutzen", "Add and use");
  box.querySelector(".ext-probe").onclick = async () => {
    state.textContent = L("prüfe …", "testing …"); state.className = "ext-state";
    let r;
    try { r = await api("POST", image ? "/api/image/backend/probe" : "/api/models/external/probe", values()); } catch { state.textContent = ""; return; }
    if (!image && r.base_url && r.base_url !== f("base_url").value.trim()) f("base_url").value = r.base_url;  // /v1 ergänzt
    const dl = box.querySelector("datalist");
    dl.innerHTML = "";
    for (const m of r.models || []) dl.appendChild(new Option(m, m));
    if (r.ok && !f("model").value && (r.models || []).length === 1) f("model").value = r.models[0];
    state.className = "ext-state " + (r.ok ? "ok" : "bad");
    state.textContent = r.ok ? L(`✔ erreichbar${r.models.length ? ` · ${r.models.length} Modell(e)` : ""}`, `✔ reachable${r.models.length ? ` · ${r.models.length} model(s)` : ""}`)
      : "✘ " + r.error;
  };
  box.querySelector(".ext-save").onclick = async () => {
    const v = values();
    if (!v.label || !v.base_url || (!image && !v.model)) {
      toast(image ? L("Bitte Name und Adresse angeben.", "Please give a name and an address.")
        : L("Bitte Name, Adresse und Modell angeben.", "Please give a name, an address and a model.")); return;
    }
    try {
      if (image) {
        await api("POST", "/api/image/backend", v);
        toast(L(`Bild-Backend „${v.label}“ ist aktiv.`, `Image backend “${v.label}” is active.`));
        f("api_key").value = "";
        if (IV.open) openImageView(); else loadImageModels();
      } else {
        const r = await api("POST", "/api/models/external", v);
        f("api_key").value = "";
        toast(L(`„${v.label}“ angelegt – wird geladen …`, `“${v.label}” added – loading …`));
        MP.tab = "mine"; MP.sel = r.name;  // gleich in „Meine Modelle“ zeigen
        await refreshModelViews();
        const p = (MP.data && MP.data.profiles || []).find((x) => x.name === r.name) || { name: r.name, label: v.label };
        activateModel(p);
      }
    } catch { /* Meldung kommt von api() */ }
  };
  return box;
}
async function renderAddTab() {
  const box = $("mp-add");
  box.innerHTML = `
    <form class="mp-search" autocomplete="off"><svg class="i"><use href="#i-search"/></svg><input type="search" spellcheck="false">
        <button type="submit" class="btn primary small"></button></form>
      <div class="hf-tools"><select class="hf-sort"></select><label class="hf-fit"><input type="checkbox"> <span></span></label></div>
      <p class="set-hint mp-search-hint"></p>
      <div class="mm-hf mp-hf"></div>
      <div class="mp-add-cols"><section class="mp-col mp-disk"></section><section class="mp-col mp-presets"></section></div>`;
  const form = box.querySelector("form");
  const q = form.querySelector("input");
  q.placeholder = L("Hugging Face durchsuchen, Link oder Pfad zu einer .gguf einfügen (z. B. qwen3.6 27b)",
                    "Search Hugging Face, paste a link or a path to a .gguf (e.g. qwen3.6 27b)");
  q.value = HF.q;
  form.querySelector("button").textContent = L("Suchen", "Search");
  const sort = box.querySelector(".hf-sort");
  for (const [v, de, en] of [["downloads", "Meiste Downloads", "Most downloads"], ["trending", "Gerade beliebt", "Trending"],
                             ["likes", "Meiste Likes", "Most likes"], ["updated", "Zuletzt aktualisiert", "Recently updated"]])
    sort.add(new Option(L(de, en), v));
  sort.value = HF.sort;
  const fitBox = box.querySelector(".hf-fit input");
  fitBox.checked = HF.fitOnly;
  box.querySelector(".hf-fit span").textContent = L("nur was in meinen Speicher passt", "only what fits my memory");
  const hf = box.querySelector(".mp-hf");
  const run = (now) => {
    clearTimeout(HF.timer);
    const text = q.value.trim();
    HF.q = text;
    if (/^[~/].*\.gguf$/i.test(text)) { if (now) addLocal(text); return; }  // Pfad zu einer eigenen GGUF-Datei
    HF.timer = setTimeout(() => (text.length >= 2 ? hfSearch(text, hf) : hfSuggest(hf)), now ? 0 : 400);
  };
  form.onsubmit = (e) => { e.preventDefault(); run(true); };
  q.addEventListener("input", () => run(false));  // Ergebnisse schon beim Tippen
  sort.onchange = () => { HF.sort = sort.value; run(true); };
  fitBox.onchange = () => { HF.fitOnly = fitBox.checked; run(true); };
  run(true);
  localModels(box.querySelector(".mp-disk"));
  renderPresets(box.querySelector(".mp-presets"));
  box.appendChild(backendForm(false));
}
// Kurzinfos eines Modells als kleine Marken: 27B · qwen3 · 128k · Bild · Werkzeuge · Lizenz · gesperrt
function hfBadges(r) {
  const wrap = document.createElement("div");
  wrap.className = "hf-badges";
  const add = (text, cls, title) => {
    const b = document.createElement("span");
    b.className = "hf-badge" + (cls ? " " + cls : "");
    b.textContent = text;
    if (title) b.title = title;
    wrap.appendChild(b);
  };
  if (r.params_b) add(`${r.params_b >= 10 ? Math.round(r.params_b) : r.params_b}B`, "", L("Parameter", "Parameters"));
  if (r.arch) add(r.arch, r.supported === false ? "warn" : "", r.supported === false
    ? L("Diese Architektur kennt dein llama.cpp noch nicht – erst aktualisieren", "Your llama.cpp does not know this architecture yet – update it first")
    : L("Architektur", "Architecture"));
  if (r.context) add(kCtx(r.context), "", L("Maximaler Kontext laut Modell", "Maximum context according to the model"));
  if (r.vision) add(L("Bild", "Vision"), "", L("Versteht Bilder (Bildmodul wird mitgeladen)", "Understands images (vision module is downloaded too)"));
  if (r.tools) add(L("Werkzeuge", "Tools"), "", L("Die Chat-Vorlage kennt Werkzeug-Aufrufe", "The chat template supports tool calls"));
  if (r.license) add(r.license, "muted", L("Lizenz", "License"));
  if (r.gated) add(L("🔒 Zugang nötig", "🔒 Gated"), "warn", L("Lizenz auf Hugging Face akzeptieren und ein Token eintragen", "Accept the license on Hugging Face and enter a token"));
  return wrap;
}
function hfResult(r) {
  const mark = document.createElement("span");
  mark.className = "fit-" + (r.fit || "tight");
  mark.textContent = r.fit ? FIT_MARKS[r.fit] + " " : "";
  const [owner, name] = r.repo.split("/");
  const stats = [r.downloads ? L(`${r.downloads.toLocaleString(LOCALE)} Downloads`, `${r.downloads.toLocaleString(LOCALE)} downloads`) : "",
                 r.likes ? `♥ ${r.likes.toLocaleString(LOCALE)}` : "", r.updated ? L(`aktualisiert ${r.updated}`, `updated ${r.updated}`) : ""]
    .filter(Boolean).join(" · ");
  const b = hfItem([mark, document.createTextNode(name || r.repo)], r.est_gb ? `~${r.est_gb} GB (Q4)` : "",
                   `${owner}${stats ? " · " + stats : ""}`);
  b.appendChild(hfBadges(r));
  b.onclick = () => hfQuants(r.repo, $("mp-add").querySelector(".mp-hf"));
  return b;
}
function hfList(box, title, results, extra) {
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "mp-hf-title hf-head";
  head.textContent = title;
  if (extra) head.appendChild(extra);
  box.appendChild(head);
  const shown = HF.fitOnly ? results.filter((r) => r.fit !== "big") : results;
  for (const r of shown) box.appendChild(hfResult(r));
  if (!shown.length) box.insertAdjacentHTML("beforeend", `<div class="mm-hint">${results.length
    ? L("Nichts davon passt in deinen Speicher – Filter ausschalten, um alles zu sehen.", "None of these fits your memory – turn the filter off to see all.")
    : L("Nichts gefunden – kürzeren Begriff versuchen (z. B. nur „qwen3.6“).", "Nothing found – try a shorter term (e.g. just “qwen3.6”).")}</div>`);
}

async function hfSuggest(box, refresh = false) {
  const seq = ++HF.seq;
  box.innerHTML = `<div class="mm-hint">${L("Lade Vorschläge von Hugging Face …", "Loading suggestions from Hugging Face …")}</div>`;
  let data;
  try { data = await getJSON("/api/models/hf/suggest" + (refresh ? "?refresh=1" : "")); }
  catch { if (seq === HF.seq) box.innerHTML = `<div class="mm-hint">${L("Hugging Face nicht erreichbar – unten die feste Vorauswahl.", "Hugging Face not reachable – see the presets below.")}</div>`; return; }
  if (seq !== HF.seq) return;  // inzwischen getippt
  const again = document.createElement("button");
  again.type = "button"; again.className = "btn-link"; again.textContent = L("neu laden", "reload");
  again.onclick = () => hfSuggest(box, true);
  hfList(box, L("Gerade beliebt auf Hugging Face – passend für deinen Speicher", "Trending on Hugging Face – fitting your memory"),
         data.results, again);
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

export function hfItem(head, tag, sub, note) {
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
  if (!text) return hfSuggest(box);
  const seq = ++HF.seq;
  box.innerHTML = `<div class="mm-hint">${L("Suche auf Hugging Face …", "Searching Hugging Face …")}</div>`;
  let data;
  try { data = await getJSON(`/api/models/hf/search?q=${encodeURIComponent(text)}&sort=${HF.sort}`); }
  catch { if (seq === HF.seq) box.innerHTML = `<div class="mm-hint">${L("Hugging Face nicht erreichbar.", "Hugging Face not reachable.")}</div>`; return; }
  if (seq !== HF.seq) return;  // inzwischen weitergetippt
  if (data.results.length === 1 && data.results[0].tag) {  // Link auf eine bestimmte Datei
    const r = data.results[0];
    return hfQuants(r.repo, box, r.tag);
  }
  if (data.results.length === 1 && !data.results[0].downloads && !data.results[0].arch) return hfQuants(data.results[0].repo, box);  // Link aufs Repo
  hfList(box, L(`Ergebnisse für „${text}“`, `Results for “${text}”`), data.results);
}

async function hfQuants(repo, box, only) {
  ++HF.seq;
  box.innerHTML = `<div class="mm-hint">${L("Lade Dateiliste …", "Loading file list …")}</div>`;
  let data;
  try { data = await api("GET", "/api/models/hf/quants?repo=" + encodeURIComponent(repo)); }
  catch { box.innerHTML = ""; return; }
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "hf-detail";
  head.innerHTML = `<div class="hf-detail-top"><button type="button" class="btn-link hf-back"></button>
      <a class="hf-link" target="_blank" rel="noopener"></a></div><div class="mp-hf-title"></div><div class="hf-warn hidden"></div>`;
  head.querySelector(".hf-back").textContent = "← " + L("zurück", "back");
  head.querySelector(".hf-back").onclick = () => (HF.q.length >= 2 ? hfSearch(HF.q, box) : hfSuggest(box));
  const link = head.querySelector(".hf-link");
  link.href = `https://huggingface.co/${repo}`;
  link.textContent = L("Auf Hugging Face ansehen ↗", "View on Hugging Face ↗");
  head.querySelector(".mp-hf-title").textContent = repo;
  const info = data.info || {};
  if (info.repo) head.appendChild(hfBadges(info));
  const warn = head.querySelector(".hf-warn");
  const warnings = [];
  if (info.gated) warnings.push(info.hf_token
    ? L("Zugangsbeschränkt: Lizenz auf der Hugging-Face-Seite akzeptieren, dann klappt der Download mit deinem Token.",
        "Gated: accept the license on the Hugging Face page, then the download works with your token.")
    : L("Zugangsbeschränkt: Lizenz auf der Hugging-Face-Seite akzeptieren und unter Einstellungen → Allgemein → Hugging Face ein Token eintragen.",
        "Gated: accept the license on the Hugging Face page and enter a token under Settings → General → Hugging Face."));
  if (info.supported === false) warnings.push(L(`Dein llama.cpp kennt die Architektur „${info.arch}“ noch nicht – oben unter llama.cpp aktualisieren, sonst startet das Modell nicht.`,
                                               `Your llama.cpp does not know the “${info.arch}” architecture yet – update llama.cpp above, otherwise the model will not start.`));
  if (warnings.length) { warn.textContent = warnings.join(" "); warn.classList.remove("hidden"); }
  box.appendChild(head);
  const items = only ? data.quants.filter((x) => x.tag.toLowerCase() === only.toLowerCase()) : data.quants;
  if (!items.length) {
    box.insertAdjacentHTML("beforeend", `<div class="mm-hint">${L("Keine GGUF-Datei in diesem Repo.", "No GGUF file in this repo.")}</div>`);
    return;
  }
  const vram = data.gpu && data.gpu.vram_gb;
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
                       + (x.mmproj ? L(" · + Bildmodul", " · + vision") : ""),
                     x.note || "");
    if (vram) {  // Balken: wie viel vom Grafikspeicher die Variante belegt – und was an Kontext daneben passt
      const bar = document.createElement("div");
      bar.className = "hf-vram";
      bar.innerHTML = `<div class="hf-vram-bar"><i></i></div><span></span>`;
      const i = bar.querySelector("i");
      i.style.width = Math.min(100, x.vram_pct || 0) + "%";
      i.className = "fit-" + x.fit;
      bar.querySelector("span").textContent = x.fit === "ok"
        ? L(`${x.vram_pct} % von ${vram} GB · ≈ ${kCtx(x.ctx)} Kontext`, `${x.vram_pct} % of ${vram} GB · ≈ ${kCtx(x.ctx)} context`)
        : x.fit === "tight" ? L("teils im Arbeitsspeicher – langsamer", "partly in system RAM – slower")
        : L("zu groß für den Grafikspeicher", "too big for the video memory");
      if (x.fit === "ok" && !x.ctx) bar.querySelector("span").textContent = L(`${x.vram_pct} % von ${vram} GB · kaum Platz für Kontext`, `${x.vram_pct} % of ${vram} GB · little room for context`);
      b.appendChild(bar);
    }
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

export function modelPullEvent(ev) {
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
export function showApiStatus(st) {
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Links die eigenen Modelle, rechts alles zum gewählten: laden, Speicher, Kontextfenster, KV-Cache, Sampling,
  // Diagnose. Zweiter Reiter: neue Modelle von Hugging Face, aus ~/models oder der Vorauswahl.
  MP = { tab: "mine", sel: null, data: null, ctxMode: null };
  FIT_MARKS = { ok: "✔", tight: "~", big: "✘" };
  fitText = (fit) => ({ ok: L("passt in den Grafikspeicher", "fits in VRAM"),
                              tight: L("teils im RAM (langsamer)", "partly in RAM (slower)"),
                              big: L("zu groß für den Grafikspeicher", "too big for VRAM") })[fit] || "";
  modelCtx = (p, mode) => {
    const tools = p.active && S.mode !== "coding" ? p.num_ctx : (p.ctx_tools || p.num_ctx);
    if (mode !== "coding") return tools;
    return p.active && S.mode === "coding" ? p.num_ctx : (p.ctx_coding || tools);
  };
  document.querySelectorAll("#mp-tabs button").forEach((b) => {
    b.onclick = () => { MP.tab = b.dataset.mtab; renderModelsPage(); };
  });

  // Aktiv und wirklich geladen (von Hand entladen: Änderungen gelten erst beim nächsten Laden)
  isLive = (p) => p.active && p.loaded !== false;
  restartNote = (p) => (isLive(p) ? (p.managed ? L("Zum Ändern startet der Modell-Server neu.", "Changing it restarts the model server.")
                                                     : L("Zum Ändern lädt Ollama das Modell neu.", "Changing it makes Ollama reload the model."))
                                        : L("Gilt ab dem nächsten Laden.", "Applies the next time it is loaded."));

  // KV-Cache-Stufe: f16 (voll), q8_0 (~halber Speicher), q4_0 (~ein Viertel) – nur bei eigenem llama-server
  KV_KNOWN = {
    f16: { factor: 2, label: "f16", note: () => L("Standard, volle Qualität", "default, full quality") },
    q8_0: { factor: 34 / 32, label: "q8", note: () => L("~halber Speicher, kaum Verlust", "~half the memory, hardly any loss") },
    q4_0: { factor: 18 / 32, label: "q4", note: () => L("~ein Viertel, leicht ungenauer", "~a quarter, slightly less accurate") },
  };

  // „Schnellste Einstellung messen“: llama-bench über Build (ROCm/Vulkan bzw. CUDA/Vulkan) × Batch-Größe
  FLAVOR_LABEL = { rocm: "ROCm", vulkan: "Vulkan", cuda: "CUDA", cpu: "CPU" };

  // Sampling je Modell und Modus (mit/ohne Denken) – gilt ab der nächsten Antwort
  SAMPLING_FIELDS = [
    ["temperature", L("Temperatur", "Temperature"), 0.05], ["top_p", "top_p", 0.05], ["top_k", "top_k", 1],
    ["min_p", "min_p", 0.01], ["presence_penalty", "presence", 0.1], ["repeat_penalty", "repeat", 0.05],
  ];

  // ---- Reiter „Hinzufügen“: Hugging Face (Vorschläge, Live-Suche, Link, Pfad), Dateien in ~/models, Vorauswahl
  HF = { q: "", sort: "downloads", fitOnly: false, timer: 0, seq: 0 };

  kCtx = (n) => (n >= 1024 ? `${Math.round(n / 1024)}k` : String(n || ""));

  // Schnellwahl an der Modell-Pille oben: laden mit einem Klick, alles Weitere auf der Modellseite
  $("pill-llm").onclick = async (e) => {
    e.stopPropagation();
    const anchor = $("pill-llm");
    if (IV.open) {  // Bild-Modus: nur die Bildmodelle
      await loadImageModels();
      const d = IM.data;
      const items = (d ? d.models.filter((m) => m.installed) : []).map((m) => ({
        icon: m.active ? "check" : "image", label: m.label, checked: m.active,
        sub: [`${m.disk_gb} GB`, L(`${m.steps} Schritte`, `${m.steps} steps`), m.edit ? L("kann bearbeiten", "can edit") : ""].filter(Boolean).join(" · "),
        run: () => { if (!m.active) activateImageModel(m.id); },
      }));
      if (items.length) items.push({ sep: true });
      items.push({ icon: "settings", label: L("Bildmodelle verwalten …", "Manage image models …"), run: () => openSheet("models") });
      popMenu(anchor, items);
      return;
    }
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
}
