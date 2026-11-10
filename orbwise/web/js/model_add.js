/* Orbwise – Modellseite, Reiter „Hinzufügen“: eigenes Backend, Hugging Face, ~/models, Vorauswahl, Downloads */

import { addSystem } from "./chat.js";
import { $, L, LOCALE, api, getJSON } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { IV, loadImageModels, openImageView } from "./images.js";
import { FLAVOR_LABEL } from "./model_tuning.js";
import { FIT_MARKS, MP, activateModel, fitText, kCtx, refreshModelViews } from "./models.js";
import { openSheetName } from "./nav.js";

let HF;

// „Passt“-Marke als farbige Pille: ✔ passt · ~ teils RAM · ✘ zu groß
function fitPill(fit) {
  const x = document.createElement("span");
  if (!fit) return x;
  x.className = "fit-pill fit-" + fit;
  x.textContent = `${FIT_MARKS[fit]} ${({ ok: L("passt", "fits"), tight: L("teils RAM", "partly RAM"), big: L("zu groß", "too big") })[fit]}`;
  x.title = fitText(fit);
  return x;
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
export async function renderAddTab() {
  const box = $("mp-add");
  box.innerHTML = `
    <form class="mp-search" autocomplete="off"><svg class="i"><use href="#i-search"/></svg><input type="search" spellcheck="false">
        <button type="submit" class="btn primary small"></button></form>
      <div class="hf-tools"><select class="hf-sort"></select><label class="hf-fit"><input type="checkbox"> <span></span></label><span class="fit-legend"></span></div>
      <p class="set-hint mp-search-hint"></p><p class="set-hint mp-disk-note"></p>
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
  box.querySelector(".fit-legend").append(fitPill("ok"), fitPill("tight"), fitPill("big"));
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
  const mark = fitPill(r.fit);
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
  const hidden = results.length - shown.length;
  if (hidden && shown.length) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "btn-link mp-more-big";
    more.textContent = L(`${hidden} weitere passen nicht in deinen Speicher – zeigen`, `${hidden} more don't fit your memory – show`);
    more.onclick = () => { HF.fitOnly = false; const cb = $("mp-add").querySelector(".hf-fit input"); if (cb) cb.checked = false; hfList(box, title, results, extra); };
    box.appendChild(more);
  }
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
    + L("Orbwise lädt die Datei nach ~/models und startet sie mit llama.cpp.", "Orbwise downloads the file to ~/models and runs it with llama.cpp.");
  box.innerHTML = `<h4></h4><div class="mm-hf mp-cards"></div>`;
  box.querySelector("h4").textContent = L("Vorauswahl", "Presets");
  const list = box.querySelector(".mm-hf");
  const big = data.presets.filter((p) => p.fit === "big" && !p.installed);
  const shown = HF.allPresets ? data.presets : data.presets.filter((p) => !big.includes(p));
  for (const p of shown) {
    const pulling = data.pulling.includes(p.tag);
    const mark = fitPill(p.fit);
    const b = hfItem([mark, document.createTextNode(p.label)],
                     pulling ? L("Lädt …", "Loading …") : p.installed ? L("Installiert", "Installed") : p.recommended ? L("Empfohlen", "Recommended") : "",
                     `${p.tag} · ~${p.download_gb} GB`, p.note);
    b.disabled = pulling;
    b.onclick = () => startPull(p.tag, p.label, p.fit);
    list.appendChild(b);
  }
  if (big.length) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "btn-link mp-more-big";
    more.textContent = HF.allPresets ? L("Zu große ausblenden", "Hide too big ones")
      : L(`${big.length} weitere passen nicht in deinen Speicher – zeigen`, `${big.length} more don't fit your memory – show`);
    more.onclick = () => { HF.allPresets = !HF.allPresets; renderPresets(box); };
    box.appendChild(more);
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
  // Name zuerst, die „Passt“-Pille dahinter
  b.querySelector(".mi-name").append(...head.filter((x) => !(x.classList && x.classList.contains("fit-pill"))),
                                     ...head.filter((x) => x.classList && x.classList.contains("fit-pill")));
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
    const mark = fitPill(x.fit);
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
  const cols = box.closest(".mp-add-cols");
  if (!free.length) {  // nichts Neues dort: Spalte weg, ein Satz unter der Suche genügt
    box.remove();
    if (cols) cols.classList.add("single");
    const note = $("mp-add").querySelector(".mp-disk-note");
    if (note) note.textContent = L(`Eigene GGUF-Dateien in ${data.dir} erscheinen hier automatisch – oder einen Pfad oben einfügen.`,
                                   `Your own GGUF files in ${data.dir} show up here automatically – or paste a path above.`);
    return;
  }
  box.innerHTML = `<h4></h4><div class="mm-hf"></div>`;
  box.querySelector("h4").textContent = L("Auf der Festplatte", "On disk") + ` · ${data.dir}`;
  const list = box.querySelector(".mm-hf");
  for (const x of free) {
    const mark = fitPill(x.fit);
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
export function pullRow(d) {
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
  row.querySelector(".mp-text").textContent = (d.tag === "Tuning" ? L("Messen", "Measuring") : d.tag === "Quality" ? L("Qualität messen", "Measuring quality") : d.tag)
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

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {

  // ---- Reiter „Hinzufügen“: Hugging Face (Vorschläge, Live-Suche, Link, Pfad), Dateien in ~/models, Vorauswahl
  HF = { q: "", sort: "downloads", fitOnly: true, allPresets: false, timer: 0, seq: 0 };
}
