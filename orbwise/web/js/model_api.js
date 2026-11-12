/* Orbwise – Modellseite, Reiter „API-Server“: OpenAI-kompatible Schnittstelle für andere Programme */

import { copyText } from "./chat.js";
import { $, L, api, getJSON } from "./core.js";
import { ask } from "./dialogs.js";
import { chatWhen } from "./history.js";
import { MP } from "./models.js";

// ---- Reiter „API-Server“: OpenAI-kompatible Schnittstelle für andere Programme (wie bei LM Studio)
export async function renderApiTab() {
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
    + "Orbwise und das Programm teilen sich das Modell: Nach einer API-Anfrage liest Orbwise deinen Chat beim nächsten Mal neu ein (dauert einen Moment).",
    "Only reachable on this computer. Programs get the model without the Orbwise system prompt, memory and tools – so they can't do anything on the PC. "
    + "Orbwise and the program share the model: after an API request Orbwise re-reads your chat next time (takes a moment).");
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
