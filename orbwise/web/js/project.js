/* Orbwise – Projekt, GitHub und Sandbox (Coding) */

import { addMsg, addShots, addSystem, addUser, assistantActions, assistants, chat, compactDivider, isBusy, markLast, renderMarkdown } from "./chat.js";
import { parkConfirm, placeConfirm, renderDiff, showChanges } from "./coding.js";
import { $, L, LOCALE, S, api, getJSON, ico } from "./core.js";
import { ask, askFolder, dialog, toast, toastLink } from "./dialogs.js";
import { loadChats } from "./history.js";
import { closeSheet, openSheet } from "./nav.js";
import { renderBoot } from "./startup.js";
import { renderTokenLine } from "./telemetry.js";
import { send } from "./ws.js";

let projectMenu, sandboxMenu, projectInfoSeq, ghTimer, reviewMenu;

export async function loadProjectInfo() {
  if (S.mode !== "coding" || !S.project) return;
  const seq = ++projectInfoSeq;
  let p;
  try { p = await getJSON("/api/project"); } catch { return; }
  if (seq !== projectInfoSeq || !p.path) return;
  S.projectInfo = p;
  const label = $("project-label"), badge = $("project-branch"), chip = $("project-chip");
  const folder = S.project.replace(/\/+$/, "").split("/").pop();
  label.textContent = p.repo || (p.branch ? folder : S.project.replace(/^\/home\/[^/]+/, "~"));
  chip.querySelector(".i use").setAttribute("href", p.repo ? "#i-github" : "#i-folder");
  badge.classList.toggle("hidden", !p.branch);
  badge.querySelector("span").textContent = p.branch || "";
  chip.title = [S.project, p.repo ? `GitHub: ${p.repo}` : "", p.branch ? `Branch: ${p.branch}` : "",
                p.sandbox ? L("Arbeitskopie in der Sandbox", "Working copy in the sandbox") : ""].filter(Boolean).join("\n");
}
async function openGithub(mode = "clone") {
  openSheet("github");
  const status = $("gh-status"), list = $("gh-repos"), search = $("gh-search");
  const connect = mode === "connect";
  $("sheet-github").querySelector("h2").textContent = connect ? L("Mit GitHub verbinden", "Connect to GitHub") : L("Von GitHub klonen", "Clone from GitHub");
  list.innerHTML = "";
  status.textContent = L("Verbinde mit GitHub …", "Connecting to GitHub …");
  let st;
  try { st = await getJSON("/api/github/status"); } catch { st = { connected: false }; }
  search.classList.toggle("hidden", !st.connected);
  if (!st.connected) {
    status.innerHTML = "";
    status.append(st.error || L("GitHub ist nicht verbunden. Melde dich mit der GitHub-CLI an (", "GitHub is not connected. Sign in with the GitHub CLI ("));
    if (!st.error) {
      const code = document.createElement("code");
      code.textContent = "gh auth login";
      status.append(code, L(") oder trag ein Token unter github.token in der Config ein.", ") or put a token under github.token in the config."));
    }
    return;
  }
  const src = { gh: "GitHub-CLI", env: L("Umgebungsvariable", "environment variable"), config: "Config" }[st.source] || st.source;
  status.textContent = connect
    ? L(`Angemeldet als @${st.login} (${src}) · wähle das Repo, in das das Projekt hochgeladen wird – am besten ein frisch angelegtes, leeres.`,
        `Signed in as @${st.login} (${src}) · pick the repo to upload the project to – ideally a freshly created, empty one.`)
    : L(`Angemeldet als @${st.login} (${src}) · geklont wird nach ${st.clone_dir}`,
        `Signed in as @${st.login} (${src}) · clones go to ${st.clone_dir}`);
  const load = async () => {
    list.innerHTML = `<li class="empty">${L("Lade Repos …", "Loading repos …")}</li>`;
    try {
      const r = await getJSON("/api/github/repos?q=" + encodeURIComponent(search.value.trim()));
      list.innerHTML = r.repos.length ? "" : `<li class="empty">${L("Keine Repos gefunden.", "No repos found.")}</li>`;
      for (const repo of r.repos) {
        const li = document.createElement("li");
        li.innerHTML = `<button type="button" class="gh-repo">${ico("branch")}<span class="gh-main"><b></b><small></small></span><span class="gh-meta"></span></button>`;
        li.querySelector("b").textContent = repo.full_name;
        li.querySelector("small").textContent = repo.description || "";
        li.querySelector(".gh-meta").textContent = (repo.private ? L("privat", "private") : L("öffentlich", "public"))
          + (repo.pushed_at ? " · " + new Date(repo.pushed_at).toLocaleDateString(LOCALE) : "");
        li.querySelector("button").onclick = () => connect ? connectRepo(repo, li) : openRepo(repo, li);
        list.appendChild(li);
      }
    } catch { list.innerHTML = `<li class="empty">${L("Repos nicht ladbar.", "Could not load repos.")}</li>`; }
  };
  search.oninput = () => { clearTimeout(ghTimer); ghTimer = setTimeout(load, 350); };
  search.value = "";
  load();
  setTimeout(() => search.focus(), 50);
}
// Klick auf ein Repo: Zeile aufklappen mit Branch-Auswahl und „Klonen“ (nur eine Zeile offen)
async function openRepo(repo, li) {
  const list = li.parentElement;
  const open = li.querySelector(".gh-clone");
  list.querySelectorAll(".gh-clone").forEach((el) => el.remove());
  list.querySelectorAll("li.open").forEach((el) => el.classList.remove("open"));
  if (open) return;
  li.classList.add("open");
  const box = document.createElement("div");
  box.className = "gh-clone";
  box.innerHTML = `<label>${L("Branch", "Branch")}<select disabled><option></option></select></label>
      <button type="button" class="btn small primary">${L("Klonen", "Clone")}</button>`;
  li.appendChild(box);
  const select = box.querySelector("select");
  select.options[0].value = repo.default_branch;
  select.options[0].textContent = L("lade …", "loading …");
  box.querySelector("button").onclick = async () => { if (await confirmLeaveSandbox(true)) cloneRepo(repo.full_name, select.value, li); };
  let names = [repo.default_branch];
  try { names = (await getJSON("/api/github/branches?repo=" + encodeURIComponent(repo.full_name))).branches || names; } catch {}
  if (!box.isConnected) return;
  select.innerHTML = "";
  for (const n of names.length ? names : [repo.default_branch]) {
    const o = document.createElement("option");
    o.value = o.textContent = n;
    if (n === repo.default_branch) o.textContent += L(" (Standard)", " (default)");
    select.appendChild(o);
  }
  select.value = repo.default_branch;
  select.disabled = false;
}
// Projekt ohne GitHub-Repo: mit dem gewählten verbinden (origin), notfalls git init + erster Commit, dann pushen
async function connectRepo(repo, li) {
  const p = S.projectInfo || {};
  const steps = p.sandbox ? L(`Hochgeladen wird der Branch der Sandbox-Arbeitskopie (${p.branch || "–"}).`, `The sandbox working copy's branch (${p.branch || "–"}) is uploaded.`)
    : p.has_commits ? L(`Hochgeladen wird der Branch ${p.branch || "–"} mit allen Commits – nicht Committetes bleibt lokal.`, `Branch ${p.branch || "–"} is uploaded with all commits – uncommitted work stays local.`)
    : L("Das Projekt hat noch keinen Commit: Alle Dateien (ohne .gitignore und ohne Zugangsdaten wie .env oder Schlüssel) werden als „Erster Commit“ festgehalten.",
        "The project has no commit yet: all files (except .gitignore'd ones and credentials like .env or keys) are recorded as \"First commit\".");
  if (!await ask(L(`Projekt mit github.com/${repo.full_name} verbinden und hochladen?\n\n${steps}`, `Connect the project to github.com/${repo.full_name} and upload it?\n\n${steps}`),
                 { ok: L("Verbinden und hochladen", "Connect and upload") })) return;
  li.classList.add("busy");
  li.querySelector(".gh-meta").textContent = L("lade hoch …", "uploading …");
  try {
    const r = await api("POST", "/api/github/connect", { repo: repo.full_name, message: L("Erster Commit", "First commit") });
    closeSheet();
    let text = L(`Verbunden – Branch ${r.branch} ist auf GitHub.`, `Connected – branch ${r.branch} is on GitHub.`);
    if (r.skipped && r.skipped.length) text += " " + L(`Nicht hochgeladen (Zugangsdaten): ${r.skipped.join(", ")}`, `Not uploaded (credentials): ${r.skipped.join(", ")}`);
    toastLink(text, r.url, L("Ansehen", "View"));
    loadProjectInfo();
  } catch { li.classList.remove("busy"); li.querySelector(".gh-meta").textContent = L("fehlgeschlagen", "failed"); }
}
async function cloneRepo(name, branch, li) {
  li.classList.add("busy");
  li.querySelector(".gh-meta").textContent = L("klone …", "cloning …");
  try {
    const r = await api("POST", "/api/github/clone", { repo: name, branch });
    closeSheet();
    toast(L(`Geklont – Branch ${branch} · Projektordner: ${r.project}`, `Cloned – branch ${branch} · project folder: ${r.project}`));
  } catch { li.classList.remove("busy"); li.querySelector(".gh-meta").textContent = L("fehlgeschlagen", "failed"); }
}

// Sandbox: Container + Arbeitskopie (Befehle laufen dort, das Projekt bleibt bis „Übernehmen“ unberührt)
export function renderSandboxChip() {
  const chip = $("sandbox-chip");
  const on = !!S.sandbox;
  chip.classList.toggle("on", on);
  chip.classList.toggle("unset", !on);
  $("sandbox-label").textContent = !on ? L("Sandbox", "Sandbox")
    : S.sandbox.network === false ? L("Sandbox · offline", "Sandbox · offline") : L("Sandbox an", "Sandbox on");
  chip.title = on ? L(`Sandbox: Befehle laufen im Container (${S.sandbox.engine}), Branch ${S.sandbox.branch || "–"}`,
                      `Sandbox: commands run in the container (${S.sandbox.engine}), branch ${S.sandbox.branch || "–"}`)
    : L("Sandbox: Befehle laufen in einem Container auf einer Arbeitskopie", "Sandbox: commands run in a container on a working copy");
}
async function openSandboxChanges() {
  openSheet("sbchanges");
  const list = $("sbc-files"), pre = $("sbc-diff");
  list.innerHTML = "";
  pre.innerHTML = "";
  $("sbc-push").classList.toggle("hidden", true);
  let r;
  try { r = await getJSON("/api/sandbox/changes"); } catch { return; }
  const n = r.files.length;
  $("sbc-info").textContent = n ? (n === 1 ? L("1 Datei geändert.", "1 file changed.") : L(`${n} Dateien geändert – klicke eine Datei für den Diff.`, `${n} files changed – click a file for its diff.`))
    : L("Noch keine Änderungen in der Arbeitskopie.", "No changes in the working copy yet.");
  $("sbc-apply").disabled = !r.files.length;
  getJSON("/api/sandbox").then((st) => $("sbc-push").classList.toggle("hidden", !st.github)).catch(() => {});
  const show = async (f, li) => {
    list.querySelectorAll("li").forEach((x) => x.classList.toggle("on", x === li));
    try { pre.innerHTML = renderDiff((await getJSON("/api/sandbox/changes?path=" + encodeURIComponent(f.path))).diff); } catch { pre.textContent = ""; }
  };
  r.files.forEach((f, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="mc-path"></span><span class="mc-add">${f.add == null ? "" : "+" + f.add}</span><span class="mc-del">${f.del == null ? "" : "−" + f.del}</span>`;
    li.querySelector(".mc-path").textContent = f.path;
    li.onclick = () => show(f, li);
    list.appendChild(li);
    if (i === 0) show(f, li);
  });
}
async function applySandbox(fromSheet) {
  if (isBusy()) { toast(L("Orbwise arbeitet gerade – erst abwarten oder STOP.", "Orbwise is busy – wait or press STOP.")); return; }
  try {
    let r = await api("POST", "/api/sandbox/apply", {});
    if (r.conflicts.length && await ask(L(`Diese Dateien hast du im Projekt inzwischen selbst geändert:\n${r.conflicts.join("\n")}\n\nTrotzdem mit der Version aus der Sandbox überschreiben?`,
                                        `You changed these files in the project yourself in the meantime:\n${r.conflicts.join("\n")}\n\nOverwrite them with the sandbox version anyway?`))) {
      const forced = await api("POST", "/api/sandbox/apply", { force: true });
      r = { applied: [...r.applied, ...forced.applied], conflicts: forced.conflicts };
    }
    const n = r.applied.length;
    toast(n ? (n === 1 ? L("1 Datei ins Projekt übernommen.", "1 file applied to the project.") : L(`${n} Dateien ins Projekt übernommen.`, `${n} files applied to the project.`))
      : r.conflicts.length ? L("Nichts übernommen.", "Nothing applied.") : L("Keine Änderungen zu übernehmen.", "No changes to apply."));
    if (fromSheet) closeSheet();
    loadProjectInfo();
  } catch { /* Meldung kommt von api() */ }
}
async function pushGithub() {
  if (!await ask(L("Den aktuellen Branch zu GitHub pushen?", "Push the current branch to GitHub?"))) return;
  try {
    const r = await api("POST", "/api/github/push");
    toastLink(L(`Branch ${r.branch} ist auf GitHub.`, `Branch ${r.branch} is on GitHub.`), r.compare, L("Pull Request erstellen", "Create pull request"));
    loadProjectInfo();
  } catch { /* Meldung kommt von api() */ }
}

export async function newChat() {
  if (!await confirmLeaveSandbox()) return;
  api("POST", "/api/chats").catch(() => {});
}

// Vor dem Verlassen eines Chats mit Sandbox: nicht übernommene Änderungen anbieten. Ohne Übernehmen bleiben sie in
// diesem Chat (Chat wieder öffnen → Sandbox-Menü) – außer discards: Projektwechsel/Löschen werfen die Kopie weg.
export async function confirmLeaveSandbox(discards = false) {
  if (!S.sandbox) return true;
  let st;
  try { st = await getJSON("/api/sandbox"); } catch { return true; }
  if (!st.enabled || !st.changes) return true;
  const el = document.createElement("div");
  el.className = "leave-choice";
  el.innerHTML = `<button type="button" class="btn ghost"></button>`;
  const body = { el, value: () => "apply" };
  const skip = el.querySelector("button");
  skip.textContent = discards ? L("Verwerfen", "Discard") : L("Weiter ohne Übernehmen", "Continue without applying");
  skip.onclick = () => body.done("skip");
  const n = st.changes;
  const head = n === 1 ? L("In der Sandbox ist 1 geänderte Datei, die noch nicht im Projekt ist.", "The sandbox has 1 changed file that is not in the project yet.")
    : L(`In der Sandbox sind ${n} geänderte Dateien, die noch nicht im Projekt sind.`, `The sandbox has ${n} changed files that are not in the project yet.`);
  const tail = discards ? L("Die Arbeitskopie wird dabei gelöscht.", "The working copy will be deleted.")
    : L("Ohne Übernehmen bleiben sie in diesem Chat erhalten (später wieder öffnen → Sandbox-Menü).",
        "Without applying they stay in this chat (open it again later → sandbox menu).");
  const choice = await dialog({ text: `${head}\n\n${tail}`, body, ok: L("Übernehmen und weiter", "Apply and continue") });
  if (choice === "skip") return true;
  if (choice !== "apply") return false;
  try {
    const r = await api("POST", "/api/sandbox/apply", {});
    if (r.conflicts.length) {
      toast(L(`Nicht übernommen – im Projekt inzwischen selbst geändert: ${r.conflicts.join(", ")}. Unter „Änderungen ansehen“ klären.`,
              `Not applied – changed in the project meanwhile: ${r.conflicts.join(", ")}. Resolve under “View changes”.`));
      return false;
    }
    toast(r.applied.length === 1 ? L("1 Datei ins Projekt übernommen.", "1 file applied to the project.")
      : L(`${r.applied.length} Dateien ins Projekt übernommen.`, `${r.applied.length} files applied to the project.`));
    return true;
  } catch { return false; }
}

export function openChatView(ev) {
  for (const id of Object.keys(assistants)) delete assistants[id];
  parkConfirm();  // nicht mit dem alten Chat wegwerfen
  $("chat").innerHTML = "";
  $("chat-title").textContent = ev.private ? L("Privater Chat", "Private chat") : ev.title || "";
  loadHistory();
  loadChats();
}

export async function loadHistory() {
  try {
    const h = await getJSON("/api/history");
    S.historyLoaded = true;
    setTimeout(renderBoot, 0);
    $("chat-title").textContent = S.private ? L("Privater Chat", "Private chat") : h.chat && h.chat.title ? h.chat.title : "";
    if (h.summary) addSystem(L("Frühere Gesprächsteile sind im Gedächtnis zusammengefasst.", "Earlier parts of this chat are summarised in memory."));
    const dividers = {};
    for (const ep of h.epochs || []) dividers[ep.at] = ep;
    S.turns = h.turns || 0;
    S.usage = h.usage || null;
    renderTokenLine();
    h.messages.forEach((m, i) => {
      if (dividers[i]) { chat.appendChild(compactDivider(dividers[i].summary, dividers[i].ts)); }
      if (m.goal_nudge) { addSystem(L("↻ weiter am Ziel", "↻ continuing with the goal")); return; }
      if (m.verify_nudge) { addSystem(L("↻ Prüfung rot – Orbwise korrigiert", "↻ checks failed – Orbwise fixes it")); return; }
      if (m.role === "user") { addUser(m.content, "", m.images, m.turn, m.editable, m.files); return; }
      const el = addMsg("assistant" + (m.editable === false ? " locked" : ""), S.name, renderMarkdown(m.content));
      el._raw = m.content;
      if (m.turn) el.dataset.turn = m.turn;
      assistantActions(el);
      if (m.images && m.images.length) addShots(el, m.images);
      if (m.changes) showChanges(el, m.turn, m.changes.files, m.changes.undone);
    });
    markLast();
    if (S.confirm) placeConfirm();  // offene Freigabe bleibt unter dem Verlauf
    if (S.question) chat.appendChild(S.question.el);
  } catch { /* egal */ }
}

// Beim Start (von app.js in der ursprünglichen Reihenfolge aufgerufen)
export function init() {
  projectMenu = $("project-menu");
  sandboxMenu = $("sandbox-menu");
  $("project-chip").onclick = (e) => {
    e.stopPropagation();
    sandboxMenu.classList.add("hidden");
    projectMenu.classList.toggle("hidden");
    const p = S.projectInfo, small = projectMenu.querySelector('[data-project="push"] small');
    small.textContent = !S.project ? L("Erst einen Projektordner wählen.", "Choose a project folder first.")
      : p && p.repo ? L(`Branch ${p.branch || "–"} nach github.com/${p.repo}`, `Branch ${p.branch || "–"} to github.com/${p.repo}`)
      : L("Noch kein GitHub-Repo – mit einem deiner (leeren) Repos verbinden und hochladen.",
          "No GitHub repo yet – connect to one of your (empty) repos and upload.");
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#project-wrap")) projectMenu.classList.add("hidden");
    if (!e.target.closest("#sandbox-wrap")) sandboxMenu.classList.add("hidden");
  });
  projectMenu.querySelector('[data-project="folder"]').onclick = async () => {
    projectMenu.classList.add("hidden");
    const path = await askFolder(S.project || "");
    if (path === null || !S.chatId || !await confirmLeaveSandbox(true)) return;
    api("POST", `/api/chats/${S.chatId}/project`, { path: path.trim() })
      .then((r) => toast(r.project ? L(`Projektordner: ${r.project}`, `Project folder: ${r.project}`) : L("Kein Projektordner.", "No project folder.")))
      .catch(() => {});  // Fehler zeigt api() schon an
  };
  projectMenu.querySelector('[data-project="github"]').onclick = () => { projectMenu.classList.add("hidden"); openGithub(); };
  // Zu GitHub pushen: mit origin der normale Push, sonst erst mit einem (leeren) Repo verbinden
  projectMenu.querySelector('[data-project="push"]').onclick = () => {
    projectMenu.classList.add("hidden");
    if (!S.project) { toast(L("Erst einen Projektordner wählen.", "Choose a project folder first.")); return; }
    if (S.projectInfo && S.projectInfo.repo) pushGithub();
    else openGithub("connect");
  };

  // GitHub-Repo und Branch im Projekt-Chip (bei Sandbox: die der Arbeitskopie)
  projectInfoSeq = 0;

  ghTimer = null;
  // 🛡 Code-Check: Änderungen bzw. ganzes Projekt in der Sandbox prüfen (Server: /review, siehe agent_review.py)
  reviewMenu = $("review-menu");
  $("review-chip").onclick = (e) => {
    e.stopPropagation();
    if (!S.project) { toast(L("Erst einen Projektordner wählen.", "Choose a project folder first.")); return; }
    reviewMenu.classList.toggle("hidden");
  };
  reviewMenu.querySelectorAll("[data-review]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      reviewMenu.classList.add("hidden");
      if (["thinking", "executing", "confirm"].includes(S.serverState)) { toast(L("Erst die laufende Antwort abwarten.", "Wait for the running answer first.")); return; }
      send({ type: "user_message", text: b.dataset.review ? "/review all" : "/review" });
    };
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#review-wrap")) reviewMenu.classList.add("hidden"); });
  $("sandbox-chip").onclick = async (e) => {
    e.stopPropagation();
    projectMenu.classList.add("hidden");
    if (!S.project) { toast(L("Erst einen Projektordner wählen.", "Choose a project folder first.")); return; }
    if (!S.sandbox) {
      if (!await ask(L("Sandbox einschalten? Orbwise arbeitet dann in einer Kopie des Projekts in einem Container und darf dort frei bauen, testen und installieren. Dein Projektordner bleibt unberührt, bis du die Änderungen übernimmst.",
                     "Turn on the sandbox? Orbwise then works on a copy of the project in a container and may build, test and install freely there. Your project folder stays untouched until you apply the changes."),
                     { ok: L("Einschalten", "Turn on") })) return;
      $("sandbox-label").textContent = L("Sandbox …", "Sandbox …");
      try {
        await api("POST", "/api/sandbox", { enabled: true });
        toast(L("Sandbox an – der Container startet beim ersten Befehl.", "Sandbox on – the container starts with the first command."));
      } catch { renderSandboxChip(); }
      return;
    }
    if (!sandboxMenu.classList.contains("hidden")) { sandboxMenu.classList.add("hidden"); return; }
    sandboxMenu.innerHTML = `<div class="sb-status">${L("Lade …", "Loading …")}</div>`;
    sandboxMenu.classList.remove("hidden");
    let st;
    try { st = await getJSON("/api/sandbox"); } catch { sandboxMenu.classList.add("hidden"); return; }
    const container = { running: L("läuft", "running"), exited: L("angehalten", "stopped"), missing: L("startet beim ersten Befehl", "starts with the first command") }[st.container] || st.container || "";
    sandboxMenu.innerHTML = `<div class="sb-status"><b></b><small></small></div>
      <button type="button" data-sb="network"><b></b><small></small></button>
      <button type="button" data-sb="containers"><b></b><small></small></button>
      <button type="button" data-sb="changes"><b></b><small></small></button>
      <button type="button" data-sb="apply"><b>${L("In Projekt übernehmen", "Apply to project")}</b><small>${L("Geänderte Dateien in deinen Projektordner kopieren.", "Copy changed files into your project folder.")}</small></button>
      ${st.github ? `<button type="button" data-sb="push"><b>${L("Zu GitHub pushen", "Push to GitHub")}</b><small></small></button>` : ""}
      <button type="button" data-sb="off" class="danger"><b>${L("Sandbox beenden", "End sandbox")}</b><small>${L("Container und Arbeitskopie löschen – nicht Übernommenes geht verloren.", "Delete container and working copy – anything not applied is lost.")}</small></button>`;
    sandboxMenu.querySelector(".sb-status b").textContent = `${st.engine || "?"} · ${st.image}`;
    sandboxMenu.querySelector(".sb-status small").textContent = L(`Container ${container} · Branch ${st.branch || "–"}`, `Container ${container} · branch ${st.branch || "–"}`);
    const net = sandboxMenu.querySelector('[data-sb="network"]');
    net.querySelector("b").textContent = st.network ? L("Netz: an", "Network: on") : L("Netz: aus", "Network: off");
    net.querySelector("small").textContent = st.network ? L("Zum Abschalten klicken (Container startet neu).", "Click to switch off (container restarts).")
      : L("Kein Internet im Container – zum Einschalten klicken.", "No internet in the container – click to switch on.");
    if (st.warning) net.querySelector("small").textContent += " ⚠ " + st.warning;
    const cont = sandboxMenu.querySelector('[data-sb="containers"]');
    const test = st.containers_test;
    cont.querySelector("b").textContent = st.containers ? L("Docker im Container: an", "Docker in the container: on")
      : L("Docker im Container: aus", "Docker in the container: off");
    cont.querySelector("small").textContent = !st.containers_allowed && !st.containers
      ? st.containers_blocked
      : st.containers ? (test && test.ok === false ? "⚠ " + test.message : (test && test.message) || L("Docker-Projekte bauen und testen.", "Build and test Docker projects."))
      : L("Erlaubt docker build/run in der Sandbox (rootless).", "Allows docker build/run in the sandbox (rootless).");
    cont.disabled = !st.containers_allowed && !st.containers;
    const ch = sandboxMenu.querySelector('[data-sb="changes"]');
    ch.querySelector("b").textContent = L(`Änderungen ansehen (${st.changes})`, `View changes (${st.changes})`);
    ch.querySelector("small").textContent = L("Was sich in der Arbeitskopie geändert hat, mit Diff.", "What changed in the working copy, with diffs.");
    const push = sandboxMenu.querySelector('[data-sb="push"]');
    if (push) push.querySelector("small").textContent = L(`Branch ${st.branch} nach github.com/${st.github}`, `Branch ${st.branch} to github.com/${st.github}`);
    net.onclick = () => { sandboxMenu.classList.add("hidden"); api("POST", "/api/sandbox", { network: !st.network }).catch(() => {}); };
    cont.onclick = async () => {
      sandboxMenu.classList.add("hidden");
      if (!st.containers && !await ask(L("Docker im Container erlauben? Der Agent kann dann Images bauen und Container starten – rootless als eigener Benutzer im Container, ohne Root-Rechte und ohne Zugriff auf deinen Rechner. Dafür bekommt die Sandbox FUSE und User-Namespaces, das vergrößert die Angriffsfläche zum Kernel etwas. Der Container startet neu (die Arbeitskopie bleibt), danach ein kurzer Test.",
                                        "Allow Docker in the container? The agent can then build images and start containers – rootless as its own user inside the container, without root and without access to your machine. The sandbox gets FUSE and user namespaces for that, which slightly enlarges the attack surface towards the kernel. The container restarts (the working copy stays), then a short test runs."))) return;
      toast(st.containers ? L("Docker im Container wird abgeschaltet …", "Switching Docker in the container off …") : L("Docker im Container wird eingerichtet und getestet …", "Setting up and testing Docker in the container …"));
      try {
        const r = await api("POST", "/api/sandbox", { containers: !st.containers });
        const t = r.containers_test;
        if (r.containers) toast(t && t.ok ? L("Docker im Container funktioniert.", "Docker in the container works.") : "⚠ " + ((t && t.message) || "?"));
      } catch {}
    };
    ch.onclick = () => { sandboxMenu.classList.add("hidden"); openSandboxChanges(); };
    sandboxMenu.querySelector('[data-sb="apply"]').onclick = () => { sandboxMenu.classList.add("hidden"); applySandbox(); };
    if (push) push.onclick = () => { sandboxMenu.classList.add("hidden"); pushGithub(); };
    sandboxMenu.querySelector('[data-sb="off"]').onclick = async () => {
      sandboxMenu.classList.add("hidden");
      if (!await ask(L("Sandbox beenden? Container und Arbeitskopie werden gelöscht – nicht übernommene Änderungen gehen verloren.",
                     "End the sandbox? Container and working copy are deleted – changes not applied are lost."))) return;
      api("POST", "/api/sandbox", { enabled: false }).then(() => toast(L("Sandbox beendet.", "Sandbox ended."))).catch(() => {});
    };
  };
  $("sbc-apply").onclick = () => applySandbox(true);
  $("sbc-push").onclick = () => pushGithub();
}
