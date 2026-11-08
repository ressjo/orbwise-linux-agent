/* Orbwise – Skills: einrichten, an-/abschalten */

import { $, L, api, getJSON, store } from "./core.js";
import { ask, askFolder, toast } from "./dialogs.js";
import { app, closeSheet, openSheet, showSection } from "./nav.js";

let SKILL_ICON, hasValue, actUnseen, lastWidth;

export async function loadSkills() {
  const box = $("set-skills");
  let data;
  try { data = await getJSON("/api/skills"); } catch { box.textContent = L("Skills konnten nicht geladen werden.", "Could not load skills."); return; }
  renderSkills(box, data);
}
function skillState(sk) {
  if (sk.kind === "connect" && !sk.configured) return sk.fields.some((f) => f.required && (f.set || hasValue(f)))
    ? ["warn", L("Unvollständig", "Incomplete")] : ["", L("Nicht eingerichtet", "Not set up")];
  return sk.on ? ["ok", L("Aktiv", "Active")] : ["", L("Aus", "Off")];
}
function renderSkills(box, data) {
  const where = data.backend === "keyring"
    ? L("Zugangsdaten liegen verschlüsselt im Schlüsselbund deiner Sitzung – nicht in config.yaml.",
        "Credentials are stored encrypted in your session keyring – not in config.yaml.")
    : L(`Kein Schlüsselbund gefunden – Zugangsdaten liegen in ${data.secrets_file}, nur für dich lesbar.`,
        `No keyring found – credentials are stored in ${data.secrets_file}, readable only by you.`);
  box.innerHTML = `<p class="set-hint skills-where"><svg class="i"><use href="#i-key"/></svg><span></span></p>`;
  box.querySelector(".skills-where span").textContent = where;
  if ((data.plaintext || []).length) {
    const warn = document.createElement("p");
    warn.className = "set-hint skills-warn";
    warn.textContent = L(`Noch im Klartext in config.yaml: ${data.plaintext.join(", ")} – beim nächsten Start zieht Orbwise sie um.`,
                         `Still in plain text in config.yaml: ${data.plaintext.join(", ")} – Orbwise moves them on the next start.`);
    box.appendChild(warn);
  }
  for (const [kind, title] of [["connect", L("Verbindungen", "Connections")], ["builtin", L("Eingebaut", "Built in")]]) {
    const h = document.createElement("div");
    h.className = "mm-title";
    h.textContent = title;
    box.appendChild(h);
    const list = document.createElement("div");
    list.className = "skill-list";
    for (const sk of data.skills.filter((x) => x.kind === kind)) list.appendChild(skillRow(sk, data));
    box.appendChild(list);
  }
}
function skillRow(sk, data) {
  const row = document.createElement("div");
  const [cls, label] = skillState(sk);
  row.className = "skill-row " + cls;
  row.innerHTML = `<span class="skill-ico"><svg class="i"><use href="#${SKILL_ICON[sk.icon] || "i-sparkles"}"/></svg></span>`
    + `<div class="skill-text"><b></b><small></small></div><span class="skill-state"><i></i><em></em></span>`
    + `<button type="button" class="btn ghost skill-edit"></button><button type="button" class="switch"></button>`;
  row.querySelector("b").textContent = sk.name;
  row.querySelector("small").textContent = sk.desc + (sk.hint ? " " + sk.hint : "");
  row.querySelector("em").textContent = label;
  const edit = row.querySelector(".skill-edit");
  if (sk.fields.length) {
    edit.textContent = sk.fields.some((f) => f.set || hasValue(f)) ? L("Bearbeiten", "Edit") : L("Einrichten", "Set up");
    edit.onclick = () => skillForm(sk);
  } else edit.remove();
  const sw = row.querySelector(".switch");
  const on = sk.on && sk.configured;
  sw.classList.toggle("on", on);
  sw.textContent = on ? L("An", "On") : L("Aus", "Off");
  sw.title = sk.configured ? "" : L("Erst einrichten", "Set up first");
  sw.onclick = async () => {
    if (!sk.configured) { skillForm(sk); return; }
    sw.disabled = true;
    try { await api("POST", `/api/skills/${sk.id}`, { enabled: !sk.on }); } catch { /* Meldung kommt von api() */ }
    loadSkills();
  };
  return row;
}
function skillField(f) {
  const wrap = document.createElement("div");
  wrap.className = "skill-field" + (f.advanced ? " adv hidden" : "");
  wrap.innerHTML = `<span class="field-label"></span>`;
  wrap.querySelector(".field-label").textContent = f.label + (f.required ? " *" : "");
  let get;
  if (f.type === "bool") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "switch";
    const set = (v) => { b.classList.toggle("on", v); b.textContent = v ? L("An", "On") : L("Aus", "Off"); };
    set(f.value === true || f.value === "true");
    b.onclick = () => set(!b.classList.contains("on"));
    wrap.appendChild(b);
    get = () => b.classList.contains("on");
  } else if (f.type === "select") {
    const sel = document.createElement("select");
    sel.className = "ui-dialog-input";
    for (const o of f.options) { const opt = document.createElement("option"); opt.value = o; opt.textContent = o; sel.appendChild(opt); }
    if (f.value) sel.value = f.value;
    wrap.appendChild(sel);
    get = () => sel.value;
  } else if (f.type === "hosts") {
    const table = document.createElement("div");
    table.className = "skill-hosts";
    const rows = [];
    const addRow = (name = "", h = {}) => {
      const r = document.createElement("div");
      r.className = "skill-host";
      r.innerHTML = `<input class="ui-dialog-input" placeholder="nas"><input class="ui-dialog-input" placeholder="192.168.1.10">`
        + `<input class="ui-dialog-input" placeholder="${L("Benutzer", "user")}"><input class="ui-dialog-input" type="number" placeholder="22">`
        + `<button type="button" class="icon-btn" title="${L("Entfernen", "Remove")}"><svg class="i"><use href="#i-x"/></svg></button>`;
      const [n, host, user, port] = r.querySelectorAll("input");
      n.value = name; host.value = h.host || ""; user.value = h.user || ""; port.value = h.port && h.port !== 22 ? h.port : "";
      const item = { n, host, user, port };
      r.querySelector("button").onclick = () => { r.remove(); rows.splice(rows.indexOf(item), 1); };
      rows.push(item);
      table.insertBefore(r, add);
    };
    const add = document.createElement("button");
    add.type = "button";
    add.className = "btn ghost";
    add.textContent = L("+ Kurzname", "+ short name");
    add.onclick = () => addRow();
    table.appendChild(add);
    for (const [name, h] of Object.entries(f.value || {})) addRow(name, h);
    wrap.appendChild(table);
    get = () => Object.fromEntries(rows.filter((r) => r.n.value.trim() && r.host.value.trim()).map((r) => [r.n.value.trim(),
      { host: r.host.value.trim(), user: r.user.value.trim(), port: parseInt(r.port.value, 10) || 22 }]));
  } else {
    const line = document.createElement("div");
    line.className = "skill-input";
    const inp = document.createElement("input");
    inp.className = "ui-dialog-input";
    inp.type = f.type === "password" ? "password" : f.type === "number" ? "number" : "text";
    inp.spellcheck = false;
    inp.autocomplete = f.type === "password" ? "new-password" : "off";
    inp.placeholder = f.type === "password" && f.set ? L("•••• gespeichert – zum Ändern neu eingeben", "•••• saved – type to change") : f.placeholder || "";
    if (f.type !== "password" && f.value !== null && f.value !== undefined && f.value !== 0) inp.value = f.value;
    line.appendChild(inp);
    if (f.type === "password") {
      const eye = document.createElement("button");
      eye.type = "button";
      eye.className = "btn ghost";
      eye.textContent = L("Zeigen", "Show");
      eye.onclick = () => { const show = inp.type === "password"; inp.type = show ? "text" : "password"; eye.textContent = show ? L("Verbergen", "Hide") : L("Zeigen", "Show"); };
      line.appendChild(eye);
    }
    if (f.type === "path") {
      const browse = document.createElement("button");
      browse.type = "button";
      browse.className = "btn ghost";
      browse.textContent = L("Durchsuchen", "Browse");
      browse.onclick = async () => { const p = await askFolder(inp.value || "~"); if (p) inp.value = p; };
      line.appendChild(browse);
    }
    wrap.appendChild(line);
    get = () => inp.value;
    wrap.setValue = (v) => { inp.value = v; };
  }
  if (f.help) {
    const help = document.createElement("small");
    help.className = "skill-help";
    help.textContent = f.help;
    wrap.appendChild(help);
  }
  if (f.type === "password" && f.source && !["keyring", "file"].includes(f.source)) {
    const src = document.createElement("small");
    src.className = "skill-help";
    src.textContent = { config: L("Steht noch in config.yaml.", "Still in config.yaml."), env: L("Kommt aus einer Umgebungsvariable.", "Comes from an environment variable."),
                        gh: L("Kommt aus der Anmeldung der GitHub-CLI (gh).", "Comes from the GitHub CLI login (gh).") }[f.source] || "";
    wrap.appendChild(src);
  }
  return { wrap, get, field: f };
}
function skillForm(sk) {
  const scrim = document.createElement("div");
  scrim.className = "ui-dialog-scrim";
  const box = document.createElement("div");
  box.className = "ui-dialog skill-form";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.innerHTML = `<div class="skill-form-head"><span class="skill-ico"><svg class="i"><use href="#${SKILL_ICON[sk.icon] || "i-sparkles"}"/></svg></span><div><b></b><small></small></div></div>`
    + `<div class="skill-fields"></div><p class="skill-result hidden"></p>`
    + `<div class="modal-actions"><button type="button" class="btn ghost danger-text skill-remove"></button><span class="spacer"></span>`
    + `<button type="button" class="btn ghost skill-test"></button><button type="button" class="btn ghost ui-cancel"></button><button type="button" class="btn primary ui-ok"></button></div>`;
  box.querySelector(".skill-form-head b").textContent = sk.name;
  box.querySelector(".skill-form-head small").textContent = sk.desc;
  const holder = box.querySelector(".skill-fields");
  const inputs = sk.fields.map(skillField);
  inputs.forEach((x) => holder.appendChild(x.wrap));
  if (inputs.some((x) => x.field.advanced)) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "btn-link skill-more";
    more.textContent = L("Mehr Einstellungen", "More settings");
    more.onclick = () => { holder.querySelectorAll(".adv").forEach((el) => el.classList.toggle("hidden")); more.remove(); };
    holder.appendChild(more);
  }
  const result = box.querySelector(".skill-result");
  const values = () => Object.fromEntries(inputs.map((x) => [x.field.key, x.get()]));
  const close = () => { document.removeEventListener("keydown", keys, true); scrim.remove(); };
  const keys = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); } };
  document.addEventListener("keydown", keys, true);
  const btnTest = box.querySelector(".skill-test"), btnOk = box.querySelector(".ui-ok"), btnRemove = box.querySelector(".skill-remove");
  btnTest.textContent = L("Verbindung testen", "Test connection");
  box.querySelector(".ui-cancel").textContent = L("Abbrechen", "Cancel");
  btnOk.textContent = L("Speichern", "Save");
  btnRemove.textContent = L("Entfernen", "Remove");
  if (!sk.fields.some((f) => f.set || hasValue(f))) btnRemove.classList.add("hidden");
  const show = (ok, text) => { result.className = "skill-result " + (ok === true ? "ok" : ok === false ? "bad" : ""); result.textContent = text; };
  btnTest.onclick = async () => {
    btnTest.disabled = true;
    show(null, L("Teste …", "Testing …"));
    try {
      const r = await api("POST", `/api/skills/${sk.id}/test`, { values: values() });
      show(r.ok, (r.ok ? "✓ " : r.ok === false ? "✕ " : "") + r.message);
      const chat = r.details && r.details.chat_id;
      const chatField = inputs.find((x) => x.field.key === "chat_id");
      if (chat && chatField && chatField.wrap.setValue) chatField.wrap.setValue(chat);
    } catch { show(false, L("Test fehlgeschlagen.", "Test failed.")); }
    btnTest.disabled = false;
  };
  btnOk.onclick = async () => {
    btnOk.disabled = true;
    try {
      await api("POST", `/api/skills/${sk.id}`, { values: values(), enabled: true });
      close();
      toast(L(`✔ ${sk.name} gespeichert.`, `✔ ${sk.name} saved.`));
      loadSkills();
    } catch { btnOk.disabled = false; }
  };
  btnRemove.onclick = async () => {
    if (!await ask(L(`${sk.name}: gespeicherte Daten und Zugangsdaten löschen?`, `${sk.name}: delete the saved data and credentials?`))) return;
    try { await api("DELETE", `/api/skills/${sk.id}`); close(); loadSkills(); } catch { /* Meldung von api() */ }
  };
  box.querySelector(".ui-cancel").onclick = close;
  scrim.onclick = (e) => { if (e.target === scrim) close(); };
  scrim.appendChild(box);
  document.body.appendChild(scrim);
  setTimeout(() => { const first = holder.querySelector("input:not([type=hidden])"); if (first) first.focus(); }, 30);
}
export function setDrawer(open, byUser) {
  app.classList.toggle("drawer-open", open);
  if (byUser) store.set("drawer", open ? "open" : "closed");
  if (open) { actUnseen = 0; $("act-badge").classList.add("hidden"); }
  $("btn-activity").classList.toggle("on", open);
}
export function activityArrived() {
  if (app.classList.contains("drawer-open")) return;
  if (store.get("drawer", "") !== "closed" && window.innerWidth > 1200) { setDrawer(true); return; }
  actUnseen += 1;
  $("act-badge").textContent = actUnseen > 9 ? "9+" : String(actUnseen);
  $("act-badge").classList.remove("hidden");
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  // Werte liegen in state.json, Tokens/Passwörter im Schlüsselbund – der Server schickt sie nie zurück.
  SKILL_ICON = { globe: "i-globe", mail: "i-mail", "file-text": "i-file", home: "i-home", box: "i-box",
    calendar: "i-calendar", notebook: "i-edit", send: "i-send", github: "i-github", terminal: "i-terminal",
    cloud: "i-cloud", repeat: "i-clock", bell: "i-bell", package: "i-download", app: "i-play", eye: "i-image",
    sun: "i-sparkles", image: "i-image", cpu: "i-chip" };
  hasValue = (f) => "value" in f && f.value !== null && f.value !== "" && f.value !== 0
    && !(typeof f.value === "object" && !Object.keys(f.value).length);
  document.querySelectorAll(".nav-item[data-sheet]").forEach((n) => { n.onclick = () => openSheet(n.dataset.sheet); });
  document.querySelectorAll(".set-tab").forEach((t) => { t.onclick = () => showSection(t.dataset.section); });
  document.querySelectorAll("[data-close-sheet]").forEach((b) => { b.onclick = closeSheet; });
  $("scrim").onclick = () => { closeSheet(); app.classList.remove("side-open"); };
  document.querySelectorAll("[data-mirror]").forEach((sw) => { sw.onclick = () => $(sw.dataset.mirror).click(); });

  // Seitenleiste ein-/ausklappen (Desktop) bzw. als Menü öffnen (schmal)
  app.classList.toggle("side-collapsed", !!store.get("sideCollapsed", false));
  $("btn-sidebar").onclick = () => {
    const c = !app.classList.contains("side-collapsed");
    app.classList.toggle("side-collapsed", c);
    store.set("sideCollapsed", c);
  };
  $("btn-menu").onclick = () => { app.classList.add("side-open"); $("scrim").classList.remove("hidden"); };

  // Aktivität: öffnet sich beim ersten Werkzeug von selbst – außer man hat sie bewusst geschlossen
  actUnseen = 0;
  setDrawer(store.get("drawer", "") === "open" && window.innerWidth > 1200);
  $("btn-activity").onclick = () => setDrawer(!app.classList.contains("drawer-open"), true);
  // Wird das Fenster schmal (Handy-Breite), legt sich ein offenes Panel nicht über den ganzen Chat
  lastWidth = window.innerWidth;
  window.addEventListener("resize", () => {
    if (lastWidth >= 900 && window.innerWidth < 900 && app.classList.contains("drawer-open")) setDrawer(false);
    lastWidth = window.innerWidth;
  });
  $("drawer-close").onclick = () => setDrawer(false, true);
}
