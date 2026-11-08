/* Orbwise – Eigene Dialoge (statt Browser-Fenster): Fragen, Hinweise */

import { $, L, getJSON, ico } from "./core.js";

let toastTimer;

// ---------------------------------------------------------------- Eigene Dialoge (statt Browser-Fenster)
// ask(text) → true/false, askText(text, wert) → Text oder null, askFolder(pfad) → Ordner oder null
export function dialog({ text, input = null, multiline = false, ok, danger = false, body = null }) {
  return new Promise((resolve) => {
    const scrim = document.createElement("div");
    scrim.className = "ui-dialog-scrim";
    const box = document.createElement("div");
    box.className = "ui-dialog";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    const msg = document.createElement("p");
    msg.className = "ui-dialog-text";
    msg.textContent = text;
    box.appendChild(msg);
    let field = null;
    if (input !== null) {
      field = document.createElement(multiline ? "textarea" : "input");
      field.className = "ui-dialog-input";
      if (!multiline) field.type = "text";
      field.value = input;
      field.rows = 3;
      field.spellcheck = multiline;
      box.appendChild(field);
    }
    if (body) box.appendChild(body.el);
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.innerHTML = `<button type="button" class="btn ghost ui-cancel"></button><button type="button" class="btn ${danger ? "danger" : "primary"} ui-ok"></button>`;
    actions.querySelector(".ui-cancel").textContent = L("Abbrechen", "Cancel");
    actions.querySelector(".ui-ok").textContent = ok || "OK";
    box.appendChild(actions);
    scrim.appendChild(box);
    document.body.appendChild(scrim);
    const done = (value) => {
      document.removeEventListener("keydown", keys, true);
      scrim.remove();
      resolve(value);
    };
    const value = () => (body ? body.value() : field ? field.value : true);
    const keys = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(input !== null || body ? null : false); }
      else if (e.key === "Enter" && !(multiline && !(e.ctrlKey || e.metaKey)) && e.target.tagName !== "BUTTON") {
        e.preventDefault(); e.stopPropagation(); done(value());
      } else if (e.code === "Space" && !(e.target.closest && e.target.closest(".ui-dialog"))) e.stopPropagation();
    };
    document.addEventListener("keydown", keys, true);
    actions.querySelector(".ui-cancel").onclick = () => done(input !== null || body ? null : false);
    actions.querySelector(".ui-ok").onclick = () => done(value());
    scrim.onclick = (e) => { if (e.target === scrim) done(input !== null || body ? null : false); };
    if (body) body.done = done;
    setTimeout(() => {
      if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
      else (body && body.focus ? body.focus() : actions.querySelector(".ui-ok").focus());
    }, 30);
  });
}
export function ask(text, opts = {}) {
  const danger = opts.danger ?? /löschen|entfernen|verwerfen|beenden|überschreiben|delete|remove|discard|overwrite/i.test(text.split("\n")[0]);
  return dialog({ text, ok: opts.ok, danger });
}
export function askText(text, value = "", opts = {}) {
  return dialog({ text, input: value, multiline: !!opts.multiline, ok: opts.ok });
}
// Projektordner: Pfad eintippen oder durch die Ordner klicken (der Server listet nur Ordnernamen)
export function askFolder(start) {
  const el = document.createElement("div");
  el.className = "ui-folder";
  el.innerHTML = `<input class="ui-dialog-input" type="text" spellcheck="false"><ul class="ui-dialog-list"></ul>`;
  const field = el.querySelector("input"), list = el.querySelector("ul");
  let timer = null;
  const show = async (path) => {
    let r;
    try { r = await getJSON("/api/fs/dirs?path=" + encodeURIComponent(path)); } catch { return; }
    if (r.path && document.activeElement !== field) field.value = r.path;
    list.innerHTML = "";
    const item = (name, target, cls = "") => {
      const li = document.createElement("li");
      li.innerHTML = `<button type="button" class="${cls}">${ico("folder")}<span></span></button>`;
      li.querySelector("span").textContent = name;
      li.querySelector("button").onclick = () => { field.value = target; show(target); };
      list.appendChild(li);
    };
    if (r.parent) item("..", r.parent, "up");
    for (const d of r.dirs) item(d, (r.path.endsWith("/") ? r.path : r.path + "/") + d, d.startsWith(".") ? "hidden-dir" : "");
    if (!r.dirs.length && !r.parent) list.innerHTML = `<li class="empty">${L("Kein Ordner", "No folder")}</li>`;
    if (r.error) list.insertAdjacentHTML("afterbegin", `<li class="empty"></li>`), (list.firstChild.textContent = r.error);
  };
  field.oninput = () => { clearTimeout(timer); timer = setTimeout(() => show(field.value), 250); };
  field.value = start || "~/";
  show(field.value);
  const body = { el, value: () => field.value.trim(), focus: () => field.focus() };
  return dialog({ text: L("Projektordner für diesen Chat – Pfad eingeben oder Ordner anklicken (leer = keiner):",
                          "Project folder for this chat – type a path or click through the folders (empty = none):"),
                  body, ok: L("Diesen Ordner nehmen", "Use this folder") });
}

export function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
}
export function toastLink(text, url, label) {
  toast(text);
  if (!url) return;
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = " " + label + " ↗";
  $("toast").appendChild(a);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 15000);
}
