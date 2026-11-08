/* Orbwise – Modellseite (wie LM Studio): Seite und Liste „Meine Modelle“, Kopf mit GPU und llama.cpp, laden/entladen, Schnellwahl an der Modell-Pille */

import { $, L, S, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { popMenu } from "./history.js";
import { IM, IV, activateImageModel, loadImageModels, renderImageModels } from "./images.js";
import { pullRow, renderAddTab } from "./model_add.js";
import { renderApiTab } from "./model_api.js";
import { renderModelDetail } from "./model_detail.js";
import { FLAVOR_LABEL } from "./model_tuning.js";
import { openSheet, openSheetName } from "./nav.js";
import { ctxLabel, fmt } from "./telemetry.js";

export let MP, FIT_MARKS, fitText, modelCtx, isLive, restartNote, kCtx;

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
export function hint(el, short, more) {
  el.textContent = short;
  if (!more) return;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "btn-link hint-more";
  b.textContent = L("Mehr", "More");
  b.onclick = () => b.replaceWith(document.createTextNode(more));
  el.append(" ", b);
}
export async function unloadModel() {
  try { await api("POST", "/api/models/unload"); } catch { /* Meldung kommt von api() */ }
  refreshModelViews();
}
export async function activateModel(p) {
  if ((p.active && p.loaded !== false) || (MP.data && MP.data.switching)) return;
  if (p.fit === "big" && !await ask(L(`${p.label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem laden?`,
                                    `${p.label} is too big for your video memory and will be very slow. Load anyway?`))) return;
  MP.data.switching = p.name;
  if (openSheetName === "models") { renderModelList(); renderModelDetail(); }
  const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/activate`, { method: "POST" });
  if (!r.ok && r.status !== 502) toast(L("Umschalten fehlgeschlagen", "Switching failed"));
  refreshModelViews();
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
