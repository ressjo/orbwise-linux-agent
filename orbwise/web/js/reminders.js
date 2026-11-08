/* Orbwise – Erinnerungen und Begrüßung im leeren Chat */

import { A } from "./audio.js";
import { addSystem, escapeHtml, renderMarkdown } from "./chat.js";
import { $, L, LOCALE, S, api, getJSON, ico } from "./core.js";
import { ask, toast } from "./dialogs.js";

function reminderWhen(iso) {
  const d = new Date(iso);
  const today = new Date();
  const tomorrow = new Date(Date.now() + 86400000);
  const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
  if (d.toDateString() === tomorrow.toDateString()) return `${L("morgen", "tomorrow")} ${time}`;
  return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" }) + " " + time;
}

export async function loadReminders() {
  const list = $("reminders");
  try {
    const items = await getJSON("/api/reminders");
    list.innerHTML = items.length ? "" : `<li class="empty">${L("Keine anstehenden Erinnerungen.", "No upcoming reminders.")}</li>`;
    for (const r of items) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="rem-when"></span><span class="rem-text"></span><button class="ghost small" title="${L("Löschen", "Delete")}">${ico("trash")}</button>`;
      li.querySelector(".rem-when").textContent = (r.kind === "timer" ? L("Timer · ", "Timer · ") : "") + reminderWhen(r.due);
      li.querySelector(".rem-text").textContent = r.text;
      li.querySelector("button").onclick = async () => {
        await fetch(`/api/reminders/${encodeURIComponent(r.id)}`, { method: "DELETE" });
        loadReminders();
      };
      list.appendChild(li);
    }
  } catch {
    list.innerHTML = `<li class="empty">${L("Erinnerungen nicht ladbar.", "Could not load reminders.")}</li>`;
  }
}

function chime() {
  if (!A.ctx) return;
  const t0 = A.ctx.currentTime;
  [880, 1318.5, 1760].forEach((freq, i) => {
    const osc = A.ctx.createOscillator();
    const gain = A.ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    const t = t0 + i * 0.18;
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.25, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.9);
    osc.connect(gain).connect(A.ctx.destination);
    osc.start(t);
    osc.stop(t + 1);
  });
}

export function showReminder(ev) {
  $("reminder-kind").textContent = ev.late ? L("Verpasste Erinnerung", "Missed reminder")
    : ev.kind === "timer" ? L("Timer abgelaufen", "Timer finished") : L("Erinnerung", "Reminder");
  $("reminder-text").textContent = ev.text;
  $("reminder-banner").classList.remove("hidden");
  chime();
  addSystem(ev.spoken);
  if (!$("tab-memory").classList.contains("hidden")) loadReminders();
}

export async function loadMemory() {
  try {
    const [facts, days, scope] = await Promise.all([getJSON("/api/memory/facts"), getJSON("/api/memory/days"),
      getJSON("/api/memory/scope")]);
    // Coding-Projekte haben je ein eigenes Gedächtnis – getrennt von dem der normalen Chats
    const project = scope.project ? scope.project.replace(/\/+$/, "").split("/").pop() : "";
    $("memory-scope").innerHTML = scope.mode === "coding"
      ? L("Gedächtnis des Projekts ", "Memory of project ") + `<b>${escapeHtml(project || L("ohne Ordner", "without folder"))}</b>`
        + L(" – getrennt von den normalen Chats.", " – separate from the regular chats.")
      : L("Gedächtnis der normalen Chats – Coding-Projekte haben je ein eigenes.",
        "Memory of the regular chats – coding projects each have their own.");
    const del = (title) => `<button type="button" class="mem-del" title="${title}" aria-label="${title}">${ico("trash")}</button>`;
    $("facts").innerHTML = facts.length
      ? facts.map((f, i) => `<li data-i="${i}"><span class="mem-text">${escapeHtml(f.fact)}<small>${f.day}</small></span>${del(L("Fakt löschen", "Delete fact"))}</li>`).join("")
      : `<li class="empty">${L("Noch keine Fakten gespeichert.", "No facts stored yet.")}</li>`;
    $("facts").querySelectorAll("li[data-i] .mem-del").forEach((b) => {
      const f = facts[Number(b.closest("li").dataset.i)];
      b.onclick = async () => {
        if (!await ask(L(`Diesen Fakt vergessen?\n\n„${f.fact}“`, `Forget this fact?\n\n“${f.fact}”`), { ok: L("Löschen", "Delete"), danger: true })) return;
        try { await api("POST", "/api/memory/facts/delete", { fact: f.fact }); toast(L("Fakt gelöscht.", "Fact deleted.")); loadMemory(); } catch {}
      };
    });
    $("days").innerHTML = days.length
      ? days.map((d) => `<li><button class="mem-day" data-day="${d.day}">${formatDay(d.day)}<span>${d.summary ? L("Zusammenfassung", "Summary") : L("Protokoll", "Log")}</span></button>${del(L("Tag löschen", "Delete day"))}</li>`).join("")
      : `<li class="empty">${L("Noch keine Einträge.", "No entries yet.")}</li>`;
    $("days").querySelectorAll(".mem-day").forEach((b) => (b.onclick = () => openDay(b.dataset.day)));
    $("days").querySelectorAll(".mem-del").forEach((b) => {
      const day = b.closest("li").querySelector(".mem-day").dataset.day;
      b.onclick = async () => {
        if (!await ask(L(`Tagebuch vom ${formatDay(day)} löschen?\n\nProtokoll und Zusammenfassung dieses Tages werden vergessen. Gelernte Fakten und die Chats in der Seitenleiste bleiben.`,
                         `Delete the journal of ${formatDay(day)}?\n\nThat day's log and summary are forgotten. Learned facts and the chats in the sidebar stay.`),
                       { ok: L("Löschen", "Delete"), danger: true })) return;
        try { await api("DELETE", "/api/memory/day/" + day); toast(L("Tag gelöscht.", "Day deleted.")); loadMemory(); } catch {}
      };
    });
  } catch (err) {
    toast(L("Gedächtnis nicht ladbar: ", "Could not load memory: ") + err.message);
  }
}

function formatDay(day) {
  const d = new Date(day + "T12:00:00");
  return d.toLocaleDateString(LOCALE, { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
}

async function openDay(day) {
  const d = await getJSON("/api/memory/day/" + day);
  $("day-title").textContent = formatDay(day).toUpperCase();
  let html = "";
  if (d.summary) html += `<h3>${L("Zusammenfassung", "Summary")}</h3><div class="body">${renderMarkdown(d.summary.replace(/^# .*\n/, ""))}</div>`;
  if (d.journal) html += `<h3>${L("Protokoll", "Log")}</h3><pre>${escapeHtml(d.journal.replace(/^# .*\n/, ""))}</pre>`;
  $("day-body").innerHTML = html || L("Keine Einträge.", "No entries.");
  $("day-modal").classList.remove("hidden");
}

// ---------------------------------------------------------------- Begrüßung (leerer Chat)
export function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? L("Gute Nacht", "Good night") : h < 11 ? L("Guten Morgen", "Good morning")
    : h < 18 ? L("Guten Tag", "Good afternoon") : L("Guten Abend", "Good evening");
  const user = S.status && S.status.user ? ", " + S.status.user : "";
  $("greeting").textContent = S.mode === "coding" ? L("Woran arbeiten wir?", "What are we working on?")
    : `${part}${user} – ${L("wie kann ich helfen?", "how can I help?")}`;
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  $("reminder-ok").onclick = () => $("reminder-banner").classList.add("hidden");
  $("day-close").onclick = () => $("day-modal").classList.add("hidden");
  setInterval(greet, 60000);
  greet();
  document.querySelectorAll(".suggestion").forEach((b) => {
    b.onclick = () => { $("input").value = b.dataset.text; $("form").requestSubmit(); };
  });
}
