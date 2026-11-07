/* Orbwise – Bild-Modus: Bilder erzeugen, Galerie, Bildmodelle */

import { secs } from "./activity.js";
import { escapeHtml } from "./chat.js";
import { $, L, LOCALE, api, getJSON, ico, store } from "./core.js";
import { ask, toast } from "./dialogs.js";
import { FIT_MARKS, backendForm, hfItem } from "./models.js";
import { markModeTabs } from "./modes.js";
import { app, openSheet, openSheetName } from "./nav.js";
import { loadStatus, setPill } from "./planner.js";

export let IV, IM, IMS;
let RES_LABEL, SIZE_LABEL, RATIO, r32;

function ivText() {
  $("iv-prompt").placeholder = L("Beschreib das Bild – z. B. „ein Fuchs im Schnee, Abendlicht, Fotografie“ (Englisch klappt meist am besten)",
                                 "Describe the image – e.g. “a fox in the snow, evening light, photograph”");
  $("iv-neg-label").textContent = L("Was nicht aufs Bild soll", "What should not be in the image");
  $("iv-steps-label").textContent = L("Schritte", "Steps");
  $("iv-count-label").textContent = L("Anzahl", "Count");
  $("iv-seed").placeholder = L("zufällig", "random");
  $("iv-upload-label").querySelector("span").textContent = L("Vorlagen hinzufügen …", "Add references …");
  $("iv-cancel").textContent = L("Abbrechen", "Cancel");
  $("iv-go").textContent = L("Erzeugen", "Generate");
}
export async function openImageView() {
  if (!IV.open) api("POST", "/api/image/enter").catch(() => {});  // Sprachmodell raus – Bildmodell lädt beim ersten Bild
  IV.open = true;
  store.set("view", "image");
  app.classList.add("imaging");
  markModeTabs();
  ivText();
  try { IV.status = await getJSON("/api/image/status"); } catch { IV.status = null; }
  const st = IV.status || {};
  $("iv-form").classList.toggle("hidden", !st.available);
  $("iv-setup").classList.toggle("hidden", !!st.available);
  if (!st.available) {  // noch kein Bildmodell: Hinweis mit Knopf zur Verwaltung
    $("iv-setup").innerHTML = `<b></b><span></span><div><button type="button" class="btn primary"></button></div>`;
    $("iv-setup").querySelector("b").textContent = L("Noch kein Bildmodell installiert", "No image model installed yet");
    $("iv-setup").querySelector("span").textContent = L(
      "Wähle eins aus – z. B. Z-Image Turbo (schnell, ~6 GB) oder Qwen-Image (kann Bilder bearbeiten). Orbwise lädt es von Hugging Face; danach geht es hier los.",
      "Pick one – e.g. Z-Image Turbo (fast, ~6 GB) or Qwen-Image (can edit pictures). Orbwise downloads it from Hugging Face; then you can start here.");
    const go = $("iv-setup").querySelector("button");
    go.textContent = L("Bildmodell auswählen …", "Choose an image model …");
    go.onclick = () => openSheet("models");
  }
  await loadImageModels();
  if (st.available) {
    const sel = $("iv-size");
    if (!sel.options.length) {
      for (const k of st.sizes || ["1:1"]) sel.add(new Option(SIZE_LABEL[k] || k, k));
      sel.add(new Option(L("Wie Vorlage (Bild 1)", "Like reference (image 1)"), "ref"));
      sel.add(new Option(L("Eigene Größe …", "Custom size …"), "custom"));
      sel.value = store.get("iv.size", st.size || "1:1");
      if (!sel.value) sel.value = "1:1";
      const res = $("iv-res");
      for (const r of st.resolutions || [1]) res.add(new Option(RES_LABEL[r] || `${r} MP`, String(r)));
      res.value = String(store.get("iv.mp", st.megapixels || 1));
      if (!res.value) res.value = "1";
      const [w, h] = store.get("iv.custom", [1280, 704]);
      $("iv-w").value = w; $("iv-h").value = h;
    }
    $("iv-steps").value = store.get("iv.steps." + st.model, st.steps || 20);  // Schritte je Bildmodell
    updateDims();
    $("iv-upload-label").classList.toggle("hidden", !st.can_edit);
    setImageBusy(!!st.busy);
  }
  loadGallery();
  setTimeout(() => $("iv-prompt").focus(), 30);
}
export function closeImageView() {
  IV.open = false;
  store.set("view", "chat");
  app.classList.remove("imaging");
  markModeTabs();
  api("POST", "/api/image/release").catch(() => {});  // Sprachmodell zurück in den Grafikspeicher
  $("pill-llm").title = L("Sprachmodell wechseln", "Switch language model");
  $("pill-llm").querySelector("use").setAttribute("href", "#i-chip");
  loadStatus();  // Pille oben zeigt wieder das Sprachmodell
}
function setImageBusy(busy) {
  IV.busy = busy;
  $("iv-go").disabled = busy;
  $("iv-cancel").classList.toggle("hidden", !busy);
  $("iv-progress").classList.toggle("hidden", !busy);
}
export function imageProgress(ev) {
  setImageBusy(true);
  const p = $("iv-progress");
  const text = $("iv-progress-text");
  p.classList.toggle("indeterminate", !ev.steps);
  if (ev.phase === "unload_llm") text.textContent = L("nimmt das Sprachmodell aus dem Grafikspeicher …", "moving the language model out of VRAM …");
  else if (ev.phase === "loading") text.textContent = ev.text || L("lädt das Bildmodell (beim ersten Bild etwas länger) …", "loading the image model (takes longer the first time) …");
  else if (ev.phase === "queued") text.textContent = L("wartet …", "waiting …");
  else if (ev.steps) {
    $("iv-bar").style.width = `${Math.round(100 * ev.step / ev.steps)}%`;
    text.textContent = L(`Schritt ${ev.step} von ${ev.steps}`, `step ${ev.step} of ${ev.steps}`)
      + (ev.eta_s >= 1 ? ` · ≈ ${secs(ev.eta_s)}` : "");
  } else text.textContent = L("rechnet …", "generating …");
}
export function imageDone(ev) {
  setImageBusy(false);
  $("iv-bar").style.width = "0";
  loadGallery((ev.images || []).map((i) => i.file));
}
export function imageError(ev) {
  setImageBusy(false);
  toast(L("Bild: ", "Image: ") + (ev.text || L("fehlgeschlagen", "failed")));
}
export async function loadImageModels() {
  try { IM.data = await getJSON("/api/image/models"); } catch { IM.data = null; }
  renderImagePill();
  if (openSheetName === "models" && IV.open) renderImageModels();
}
// Im Bild-Modus zeigt die Modell-Pille oben das aktive Bildmodell (statt des Sprachmodells)
export function renderImagePill() {
  if (!IV.open) return;
  const active = IM.data && IM.data.models.find((m) => m.active);
  setPill("pill-llm", active ? "ok" : "off", active ? active.label : L("kein Bildmodell", "no image model"));
  $("pill-llm").title = L("Bildmodell wechseln", "Switch image model");
  $("pill-llm").querySelector("use").setAttribute("href", "#i-image");
}
export function renderImageModels() {
  const d = IM.data;
  const panel = $("mp-image");
  if (!d) { panel.innerHTML = `<div class="mm-hint">${L("Bildmodelle nicht ladbar.", "Could not load image models.")}</div>`; return; }
  if (!panel.querySelector(".ivm-search")) {  // Gerüst einmal – Eingaben bleiben beim Neuzeichnen stehen
    panel.innerHTML = `<form class="mp-search ivm-search" autocomplete="off"><svg class="i"><use href="#i-search"/></svg>
          <input type="search" spellcheck="false"><button type="submit" class="btn primary small"></button></form>
        <div class="hf-tools"><select class="hf-sort"></select></div>
        <p class="set-hint ivm-intro"></p><div class="ivm-runtime"></div><div class="ivm-installing hidden"></div><div class="ivm-body"></div>`;
    const form = panel.querySelector("form"), q = form.querySelector("input"), sort = panel.querySelector(".hf-sort");
    q.placeholder = L("Alle Text-zu-Bild-Modelle auf Hugging Face durchsuchen (z. B. flux, juggernaut xl, dreamshaper)",
                      "Search all text-to-image models on Hugging Face (e.g. flux, juggernaut xl, dreamshaper)");
    form.querySelector("button").textContent = L("Suchen", "Search");
    for (const [v, de, en] of [["downloads", "Meiste Downloads", "Most downloads"], ["trending", "Gerade beliebt", "Trending"],
                               ["likes", "Meiste Likes", "Most likes"], ["updated", "Zuletzt aktualisiert", "Recently updated"]])
      sort.add(new Option(L(de, en), v));
    q.value = IMS.q; sort.value = IMS.sort;
    const run = (now) => {
      clearTimeout(IMS.timer);
      IMS.q = q.value.trim(); IMS.repo = "";
      IMS.timer = setTimeout(renderImageBody, now ? 0 : 400);
    };
    form.onsubmit = (e) => { e.preventDefault(); run(true); };
    q.addEventListener("input", () => run(false));
    sort.onchange = () => { IMS.sort = sort.value; run(true); };
  }
  const installed = d.models.filter((m) => m.installed);
  panel.querySelector(".ivm-intro").textContent = (d.vram_gb ? L(`Grafikkarte: ${d.vram_gb} GB · `, `GPU: ${d.vram_gb} GB · `) : "")
    + L("Immer nur ein Modell ist geladen; wechseln geht auch oben am Modell-Menü. ", "Only one model is loaded at a time; you can also switch at the model menu at the top. ")
    + L(`Dateien in ${d.dir}.`, `Files in ${d.dir}.`) + (installed.length ? "" : " " + L("Noch kein Bildmodell installiert.", "No image model installed yet."));
  renderRuntime();
  renderInstalling();
  renderImageBody();
}
// sd-server (rechnet die eigenen Bildmodelle): Version und Aktualisieren – neuere Modelle brauchen oft einen neueren
function renderRuntime() {
  const box = $("mp-image").querySelector(".ivm-runtime");
  const rt = (IM.data && IM.data.runtime) || {};
  if (!box || !rt.installed) { if (box) box.innerHTML = ""; return; }
  box.innerHTML = `<span></span><button type="button" class="btn small"></button>`;
  box.querySelector("span").textContent = "sd-server " + (rt.version || L("(Version unbekannt)", "(version unknown)"));
  const b = box.querySelector("button");
  b.textContent = rt.updating ? L("aktualisiert …", "updating …") : L("Aktualisieren", "Update");
  b.disabled = !!rt.updating;
  b.title = L("Neuesten Build von stable-diffusion.cpp laden – neuere Bildmodelle (z. B. FLUX.2 klein) brauchen ihn oft",
              "Download the newest stable-diffusion.cpp build – newer image models (e.g. FLUX.2 klein) often need it");
  b.onclick = async () => {
    b.disabled = true; b.textContent = L("aktualisiert …", "updating …");
    try { const r = await api("POST", "/api/image/runtime/update"); toast(r.text || L("sd-server aktualisiert.", "sd-server updated.")); }
    catch { /* Meldung kommt von api() */ }
    loadImageModels();
  };
}
function renderInstalling() {
  const box = $("mp-image").querySelector(".ivm-installing");
  if (!box || !IM.data) return;
  const id = IM.data.installing;
  box.classList.toggle("hidden", !id);
  if (!id) { box.innerHTML = ""; return; }
  const p = IM.progress[id] || { text: IM.data.text, pct: IM.data.pct };
  if (!box.querySelector(".ivm-progress")) {
    box.innerHTML = `<b></b><div class="ivm-progress"><div class="iv-bar"><i></i></div><span></span>
        <button type="button" class="btn small"></button></div>`;
    box.querySelector("button").textContent = L("Abbrechen", "Cancel");
    box.querySelector("button").onclick = () => api("DELETE", "/api/image/models/install").catch(() => {});
  }
  const known = IM.data.models.find((m) => m.id === id);
  box.querySelector("b").textContent = L("Lädt: ", "Downloading: ") + (known ? known.label : (IM.label || id));
  box.querySelector("i").style.width = (p.pct || 0) + "%";
  box.querySelector("span").textContent = p.text || L("startet …", "starting …");
}
async function renderImageBody() {
  const panel = $("mp-image");
  const body = panel.querySelector(".ivm-body");
  if (!body || !IM.data) return;
  const seq = ++IMS.seq;
  if (IMS.repo) return imageRepoFiles(IMS.repo, body, seq);
  if (IMS.q.length >= 2) {  // Suche über alle Text-zu-Bild-Modelle
    body.innerHTML = `<div class="mm-hint">${L("Suche auf Hugging Face …", "Searching Hugging Face …")}</div>`;
    let data;
    try { data = await getJSON(`/api/image/hf/search?q=${encodeURIComponent(IMS.q)}&sort=${IMS.sort}`); }
    catch { if (seq === IMS.seq) body.innerHTML = `<div class="mm-hint">${L("Hugging Face nicht erreichbar.", "Hugging Face not reachable.")}</div>`; return; }
    if (seq !== IMS.seq) return;
    body.innerHTML = "";
    section(body, L(`Ergebnisse für „${IMS.q}“`, `Results for “${IMS.q}”`));
    if (!data.results.length) body.insertAdjacentHTML("beforeend", `<div class="mm-hint">${L("Nichts gefunden – kürzeren Begriff versuchen.", "Nothing found – try a shorter term.")}</div>`);
    for (const r of data.results) body.appendChild(imageHfItem(r));
    return;
  }
  body.innerHTML = "";
  const d = IM.data;
  const installed = d.models.filter((m) => m.installed), offered = d.models.filter((m) => !m.installed);
  if (installed.length) { section(body, L("Installiert", "Installed")); for (const m of installed) body.appendChild(imageModelCard(m, d)); }
  section(body, L("Empfohlen", "Recommended"));
  for (const m of offered) body.appendChild(imageModelCard(m, d));
  body.appendChild(backendForm(true));
  section(body, L("Gerade beliebt auf Hugging Face", "Trending on Hugging Face"));
  const list = document.createElement("div");
  list.className = "mm-hf";
  list.innerHTML = `<div class="mm-hint">${L("Lade …", "Loading …")}</div>`;
  body.appendChild(list);
  let data;
  try { data = await getJSON("/api/image/hf/search"); }
  catch { if (seq === IMS.seq) list.innerHTML = `<div class="mm-hint">${L("Hugging Face nicht erreichbar.", "Hugging Face not reachable.")}</div>`; return; }
  if (seq !== IMS.seq) return;
  list.innerHTML = "";
  for (const r of data.results) list.appendChild(imageHfItem(r));
  if (!data.results.length) list.innerHTML = `<div class="mm-hint">${L("Gerade nichts Passendes.", "Nothing suitable right now.")}</div>`;
}
function section(box, title) {
  const h = document.createElement("div");
  h.className = "mp-hf-title ivm-section";
  h.textContent = title;
  box.appendChild(h);
  return h;
}
function imageHfItem(r) {
  const [owner, name] = r.repo.split("/");
  const stats = [r.downloads ? L(`${r.downloads.toLocaleString(LOCALE)} Downloads`, `${r.downloads.toLocaleString(LOCALE)} downloads`) : "",
                 r.likes ? `♥ ${r.likes.toLocaleString(LOCALE)}` : "", r.updated ? L(`aktualisiert ${r.updated}`, `updated ${r.updated}`) : ""]
    .filter(Boolean).join(" · ");
  const b = hfItem([document.createTextNode(name || r.repo)], r.supported ? r.family_label : L("nicht unterstützt", "not supported"),
                   `${owner}${stats ? " · " + stats : ""}`, r.supported ? "" : r.reason);
  const badges = document.createElement("div");
  badges.className = "hf-badges";
  const add = (text, cls) => { const t = document.createElement("span"); t.className = "hf-badge" + (cls ? " " + cls : ""); t.textContent = text; badges.appendChild(t); };
  if (r.gguf) add("GGUF");
  if (r.license) add(r.license, "muted");
  if (r.gated) add(L("🔒 Zugang nötig", "🔒 Gated"), "warn");
  if (badges.childElementCount) b.appendChild(badges);
  b.classList.toggle("unsupported", !r.supported);
  b.onclick = () => { if (!r.supported) { toast(r.reason); return; } IMS.repo = r.repo; renderImageBody(); };
  return b;
}
async function imageRepoFiles(repo, body, seq) {
  body.innerHTML = `<div class="mm-hint">${L("Lade Dateiliste …", "Loading file list …")}</div>`;
  let data;
  try { data = await api("GET", "/api/image/hf/files?repo=" + encodeURIComponent(repo)); }
  catch { if (seq === IMS.seq) { IMS.repo = ""; renderImageBody(); } return; }
  if (seq !== IMS.seq) return;
  body.innerHTML = "";
  const head = document.createElement("div");
  head.className = "hf-detail";
  head.innerHTML = `<div class="hf-detail-top"><button type="button" class="btn-link hf-back"></button>
      <a class="hf-link" target="_blank" rel="noopener"></a></div><div class="mp-hf-title"></div><div class="hf-warn"></div>`;
  head.querySelector(".hf-back").textContent = "← " + L("zurück", "back");
  head.querySelector(".hf-back").onclick = () => { IMS.repo = ""; renderImageBody(); };
  head.querySelector(".hf-link").href = `https://huggingface.co/${repo}`;
  head.querySelector(".hf-link").textContent = L("Auf Hugging Face ansehen ↗", "View on Hugging Face ↗");
  head.querySelector(".mp-hf-title").textContent = repo;
  const notes = [];
  if (data.family) notes.push(L(`Läuft als ${data.family_label}-Modell: Text-Encoder und VAE lädt Orbwise passend dazu (falls noch nicht da), ${data.steps} Schritte.`,
                                `Runs as a ${data.family_label} model: Orbwise adds the matching text encoder and VAE (if not there yet), ${data.steps} steps.`));
  else notes.push(data.reason);
  if (data.gated) notes.push(L("Zugangsbeschränkt: Lizenz auf der Modellseite akzeptieren und unter Einstellungen → Allgemein → Hugging Face ein Token eintragen.",
                               "Gated: accept the license on the model page and enter a token under Settings → General → Hugging Face."));
  const warn = head.querySelector(".hf-warn");
  warn.textContent = notes.join(" ");
  warn.classList.toggle("info", !!data.family && !data.gated);
  body.appendChild(head);
  if (!data.variants.length) {
    body.insertAdjacentHTML("beforeend", `<div class="mm-hint">${data.family
      ? L("Keine ladbare Datei – sd.cpp braucht eine ganze Modelldatei (GGUF oder .safetensors), keine Diffusers-Aufteilung.",
          "No loadable file – sd.cpp needs a whole model file (GGUF or .safetensors), not a diffusers split.") : ""}</div>`);
    return;
  }
  const vram = IM.data && IM.data.vram_gb;
  for (const v of data.variants) {
    const mark = document.createElement("span");
    mark.className = "fit-" + v.fit;
    mark.textContent = FIT_MARKS[v.fit] + " ";
    const busy = IM.data && IM.data.installing;
    const b = hfItem([mark, document.createTextNode(v.quant || v.name)],
                     busy === v.id ? L("Lädt …", "Loading …") : v.installed ? L("Installiert", "Installed") : v.recommended ? L("Empfohlen", "Recommended") : "",
                     `${v.name} · ~${v.size_gb} GB · ${L("braucht", "needs")} ~${v.need_gb} GB VRAM` + (vram ? ` (${L("du hast", "you have")} ${vram} GB)` : ""));
    b.disabled = !!busy || v.installed;
    b.onclick = async () => {
      if (v.fit === "big" && !await ask(L(`${v.name} ist für deine Grafikkarte zu groß und wird sehr langsam. Trotzdem laden?`,
                                          `${v.name} is too big for your GPU and will be very slow. Download anyway?`))) return;
      try {
        const r = await api("POST", "/api/image/hf/install", { repo, file: v.file });
        IM.data.installing = r.model; IM.label = `${repo.split("/")[1]} ${v.quant}`.trim(); IM.progress[r.model] = {};
        renderInstalling(); b.disabled = true;
        toast(L("Lädt – Fortschritt oben.", "Downloading – progress at the top."));
      } catch {}
    };
    body.appendChild(b);
  }
}
function imageModelCard(m, d) {
  const card = document.createElement("div");
  card.className = "ivm-card" + (m.active ? " active" : "");
  card.innerHTML = `<div class="ivm-name"></div><div class="ivm-note"></div><div class="ivm-meta"></div><div class="ivm-btns"></div>`;
  const name = card.querySelector(".ivm-name");
  name.textContent = m.label;
  const tag = (text, on) => { const t = document.createElement("span"); t.className = "ivm-tag" + (on ? " on" : ""); t.textContent = text; name.appendChild(t); };
  if (m.active) tag(L("aktiv", "active"), true);
  else if (m.installed) tag(L("installiert", "installed"));
  if (m.edit) tag(L("kann bearbeiten", "can edit"));
  if (m.new) tag(L("neu", "new"));
  card.querySelector(".ivm-note").textContent = m.note;
  if (m.misfiled) {  // früher falsch eingeordnet (z. B. klein als FLUX.1) – startet mit falschen Bauteilen
    const w = document.createElement("div");
    w.className = "hf-warn";
    w.textContent = m.misfiled + " " + L("Bitte löschen.", "Please delete it.");
    card.appendChild(w);
  }
  const meta = card.querySelector(".ivm-meta");
  const fit = document.createElement("span");
  fit.className = "fit-" + m.fit;
  fit.textContent = FIT_MARKS[m.fit] + " ";
  meta.append(fit, document.createTextNode([
    m.installed ? L(`${m.disk_gb} GB auf der Platte`, `${m.disk_gb} GB on disk`) : L(`~${m.size_gb} GB Download`, `~${m.size_gb} GB download`),
    m.min_vram_gb ? L(`ab ${m.min_vram_gb} GB VRAM`, `from ${m.min_vram_gb} GB VRAM`) : "", L(`${m.steps} Schritte`, `${m.steps} steps`), m.license].filter(Boolean).join(" · ")));
  const btns = card.querySelector(".ivm-btns");
  const btn = (label, cls, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = "btn small " + cls; b.textContent = label; b.onclick = fn; btns.appendChild(b); return b; };
  const busy = d.installing;
  if (busy === m.id) {
    btn(L("Lädt …", "Loading …"), "", () => {}).disabled = true;
  } else if (!m.installed) {
    const b = btn(L("Laden", "Download"), "primary", async () => {
      if (m.fit === "big" && !await ask(L(`${m.label} ist für deine Grafikkarte zu groß und wird sehr langsam. Trotzdem laden?`,
                                          `${m.label} is too big for your GPU and will be very slow. Download anyway?`))) return;
      try { await api("POST", `/api/image/models/${m.id}/install`); IM.data.installing = m.id; IM.progress[m.id] = {}; renderImageModels(); } catch {}
    });
    b.disabled = !!busy;
  } else {
    if (!m.active) btn(L("Aktivieren", "Activate"), "", () => activateImageModel(m.id));
    btn(L("Löschen", "Delete"), "danger", async () => {
      if (!await ask(L(`${m.label} löschen? ${m.disk_gb} GB werden frei (Dateien, die ein anderes Bildmodell nutzt, bleiben).`,
                       `Delete ${m.label}? Frees ${m.disk_gb} GB (files another image model uses stay).`), { danger: true })) return;
      try {
        const r = await api("DELETE", `/api/image/models/${m.id}`);
        toast(L(`${m.label} gelöscht – ${r.freed_gb} GB frei.`, `${m.label} deleted – ${r.freed_gb} GB freed.`));
        if (IV.open) openImageView(); else loadImageModels();
      } catch {}
    });
  }
  return card;
}
export async function activateImageModel(id) {
  if (IV.busy) { toast(L("Erst das laufende Bild abwarten.", "Wait for the running image first.")); return; }
  try { IV.status = await api("POST", `/api/image/models/${id}/activate`); } catch { return; }
  toast(L(`Bildmodell: ${IV.status.model_label}`, `Image model: ${IV.status.model_label}`));
  if (IV.open) openImageView(); else loadImageModels();
}
export function imageInstallEvent(ev) {
  if (!ev.done) {
    IM.progress[ev.model] = { text: ev.text, pct: ev.pct };
    if (IM.data) IM.data.installing = ev.model;
    if (openSheetName === "models" && IV.open) renderInstalling();  // nur der Fortschrittsblock, nicht die Liste
    return;
  }
  if (IM.data) IM.data.installing = "";
  delete IM.progress[ev.model];
  const label = ((IM.data && IM.data.models.find((m) => m.id === ev.model)) || {}).label || IM.label || ev.model;
  if (ev.ok) toast(L(`${label} ist geladen und aktiv.`, `${label} is downloaded and active.`));
  else if (ev.cancelled) toast(L(`Laden von ${label} abgebrochen – erneut laden macht weiter.`, `Download of ${label} cancelled – downloading again resumes.`));
  else toast(L(`${label}: `, `${label}: `) + (ev.text || L("fehlgeschlagen", "failed")));
  if (IV.open) openImageView(); else loadImageModels();
}

async function loadGallery(fresh = []) {
  let data;
  try { data = await getJSON("/api/image/list"); } catch { return; }
  const box = $("iv-gallery");
  box.innerHTML = "";
  if (!data.images.length) {
    box.innerHTML = `<div class="iv-empty">${L("Noch keine Bilder.", "No images yet.")}</div>`;
    return;
  }
  for (const img of data.images) box.appendChild(imageCard(img, fresh.includes(img.file)));
}
function imageCard(img, fresh) {
  const card = document.createElement("div");
  card.className = "iv-card" + (fresh ? " new" : "");
  const url = `/api/image/file/${encodeURIComponent(img.file)}`;
  card.innerHTML = `<img loading="lazy" alt=""><div class="iv-cap"></div><div class="iv-meta"></div><div class="iv-actions"></div>`;
  card.querySelector("img").src = url;
  card.querySelector("img").alt = img.prompt || "";
  card.querySelector("img").onclick = () => window.open(url, "_blank");
  card.querySelector(".iv-cap").textContent = (img.edit ? (img.refs > 1 ? `✎×${img.refs} ` : "✎ ") : "") + (img.prompt || "");
  card.querySelector(".iv-meta").textContent = (img.model ? img.model + " · " : "") + `${img.width}×${img.height} · ${img.steps} ${L("Schritte", "steps")} · Seed ${img.seed}`
    + (img.seconds ? ` · ${secs(img.seconds)}` : "");
  const actions = card.querySelector(".iv-actions");
  const add = (label, title, fn) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn small"; b.innerHTML = label.startsWith("<svg") ? label : escapeHtml(label);
    b.title = title; b.onclick = fn;
    actions.appendChild(b);
  };
  add(L("Variante", "Variation"), L("Gleicher Prompt, neuer Seed", "Same prompt, new seed"), () => {
    $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = "";
    generateImage();
  });
  add(L("Gleich", "Reuse"), L("Prompt und Einstellungen übernehmen (Seed bleibt)", "Reuse prompt and settings (same seed)"), () => {
    $("iv-prompt").value = img.prompt || ""; $("iv-negative").value = img.negative || ""; $("iv-seed").value = img.seed;
    $("iv-steps").value = img.steps;
    $("iv-size").value = "custom"; $("iv-w").value = img.width; $("iv-h").value = img.height; updateDims();
    $("iv-prompt").focus();
  });
  if (IV.status && IV.status.can_edit) add(L("Als Vorlage", "Use as reference"), L("Als Vorlage hinzufügen (bis zu 3 – z. B. Personen aus mehreren Bildern zusammenbringen)", "Add as a reference (up to 3 – e.g. bring people from several images together)"), () => {
    addRef({ file: img.file, url }); $("iv-prompt").focus();
  });
  if (IV.status && IV.status.telegram) add(L("Handy", "Phone"), L("Per Telegram aufs Handy", "Send to the phone via Telegram"), () =>
    api("POST", `/api/image/telegram/${encodeURIComponent(img.file)}`).then(() => toast(L("Aufs Handy geschickt.", "Sent to the phone."))).catch(() => {}));
  add(ico("trash"), L("Löschen", "Delete"), async () => {
    if (!await ask(L("Bild löschen?", "Delete image?"), { danger: true })) return;
    api("DELETE", `/api/image/file/${encodeURIComponent(img.file)}`).then(() => card.remove()).catch(() => {});
  });
  return card;
}
function addRef(ref) {
  const max = (IV.status && IV.status.max_refs) || 3;
  if (IV.refs.length >= max) { toast(L(`Höchstens ${max} Vorlagen.`, `At most ${max} references.`)); return; }
  if (ref.file && IV.refs.some((r) => r.file === ref.file)) return;
  IV.refs.push(ref);
  renderRefs();
}
function renderRefs() {
  const box = $("iv-refs");
  const list = $("iv-ref-list");
  box.classList.toggle("hidden", !IV.refs.length);
  list.innerHTML = "";
  IV.refs.forEach((r, i) => {
    const item = document.createElement("div");
    item.className = "iv-ref-item";
    item.innerHTML = `<img alt=""><b></b><button type="button" aria-label="${L("Entfernen", "Remove")}">✕</button>`;
    item.querySelector("img").src = r.url || r.data;
    item.querySelector("b").textContent = L(`Bild ${i + 1}`, `Image ${i + 1}`);
    item.querySelector("button").onclick = () => { IV.refs.splice(i, 1); renderRefs(); };
    list.appendChild(item);
  });
  $("iv-ref-hint").textContent = IV.refs.length > 1
    ? L("Im Text auf die Bilder verweisen – z. B. „die Frau aus Bild 1 und der Mann aus Bild 2 zusammen auf einer Parkbank, Abendlicht“.",
        "Refer to the images in the text – e.g. “the woman from image 1 and the man from image 2 together on a park bench, evening light”.")
    : L("Beschreib die Änderung – z. B. „mach den Himmel rot“. Für mehrere Personen weitere Bilder hinzufügen (bis zu 3).",
        "Describe the change – e.g. “make the sky red”. Add more images for several people (up to 3).");
  updateDims();
}
function updateDims() {
  const size = $("iv-size").value;
  $("iv-custom").classList.toggle("hidden", size !== "custom");
  $("iv-res").classList.toggle("hidden", size === "custom");
  let text = "";
  if (size === "custom") text = "";
  else if (size === "ref") text = IV.refs.length ? L("Seitenverhältnis wie Bild 1", "aspect of image 1") : L("erst eine Vorlage hinzufügen", "add a reference first");
  else if (RATIO[size]) {
    const w = Math.sqrt(Number($("iv-res").value || 1) * 1048576 * RATIO[size]);
    text = `${r32(w)} × ${r32(w / RATIO[size])}`;
  }
  $("iv-dims").textContent = text;
}
async function generateImage() {
  const prompt = $("iv-prompt").value.trim();
  if (!prompt) { $("iv-prompt").focus(); return; }
  let size = $("iv-size").value;
  if (size === "ref" && !IV.refs.length) size = "1:1";
  store.set("iv.size", $("iv-size").value);
  store.set("iv.mp", Number($("iv-res").value) || 1);
  store.set("iv.steps." + ((IV.status && IV.status.model) || ""), Number($("iv-steps").value) || 20);
  if (size === "custom") {
    const w = r32(Number($("iv-w").value) || 1024), h = r32(Number($("iv-h").value) || 1024);
    store.set("iv.custom", [w, h]);
    size = `${w}x${h}`;
  }
  const seed = $("iv-seed").value === "" ? -1 : Number($("iv-seed").value);
  const body = { prompt, negative: $("iv-negative").value, size, megapixels: Number($("iv-res").value) || 1,
                 steps: Number($("iv-steps").value) || 0, count: Number($("iv-count").value) || 1, seed,
                 refs: IV.refs.map((r) => (r.file ? { file: r.file } : { data: r.data })) };
  setImageBusy(true);
  imageProgress({ phase: "queued" });
  try { await api("POST", "/api/image/generate", body); } catch { setImageBusy(false); }
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Eigene Ansicht statt Chat: Prompt, Format, Galerie. Läuft ohne Sprachmodell; der Server gibt den Grafikspeicher
  // ans Sprachmodell zurück, sobald es wieder gebraucht wird (oder beim Verlassen der Ansicht).
  IV = { open: false, status: null, refs: [], busy: false };
  RES_LABEL = { 0.5: L("Schnell · 0,5 MP", "Fast · 0.5 MP"), 1: L("Standard · 1 MP", "Standard · 1 MP"),
                      1.5: L("Fein · 1,5 MP", "Fine · 1.5 MP"), 2: L("Groß · 2 MP", "Large · 2 MP") };
  SIZE_LABEL = { "1:1": L("Quadrat 1:1", "Square 1:1"), "4:3": L("Quer 4:3", "Landscape 4:3"),
                       "3:4": L("Hoch 3:4", "Portrait 3:4"), "16:9": L("Breit 16:9", "Wide 16:9"),
                       "9:16": L("Handy 9:16", "Phone 9:16"), "3:2": L("Foto 3:2", "Photo 3:2"), "2:3": L("Foto hoch 2:3", "Photo portrait 2:3") };
  // ---- Bildmodelle: Auswahl oben am Modell-Menü (#pill-llm), Verwaltung auf der Modell-Seite (Bild-Variante)
  IM = { data: null, progress: {} };
  // Bildmodell-Seite: Suche über alle Text-zu-Bild-Modelle auf Hugging Face, darunter installiert / empfohlen / beliebt
  IMS = { q: "", sort: "downloads", timer: 0, seq: 0, repo: "" };
  // Ergebnisgröße anzeigen (wie der Server rechnet: Seitenverhältnis × Megapixel, auf 32 gerundet)
  RATIO = { "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 3 / 2, "2:3": 2 / 3 };
  r32 = (v) => Math.max(256, Math.min(2048, Math.round(v / 32) * 32));
  $("iv-size").onchange = updateDims;
  $("iv-res").onchange = updateDims;
  $("iv-upload").onchange = (e) => {
    const files = [...(e.target.files || [])];
    e.target.value = "";
    for (const f of files) {
      if (f.size > 20e6) { toast(L("Bild zu groß (höchstens 20 MB).", "Image too large (20 MB max).")); continue; }
      const reader = new FileReader();
      reader.onload = () => { addRef({ data: reader.result }); $("iv-prompt").focus(); };
      reader.readAsDataURL(f);
    }
  };
  $("iv-form").addEventListener("submit", (e) => { e.preventDefault(); generateImage(); });
  $("iv-prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); generateImage(); }
  });
  $("iv-cancel").onclick = () => api("POST", "/api/image/cancel").catch(() => {});
}
