// README-Screenshots im Demo-Modus neu erzeugen (kein echtes Modell nötig).
//
//   ORBWISE_FAKE_LLM=1 orbwise serve   (mit einer Config: language: en, voice.enabled: false)
//   node scripts/screenshots.mjs [http://localhost:8765] [start|chat|confirm|coding|sandbox|demo …]
//
// Braucht Playwright (npm i -g playwright). CHROMIUM=/pfad/zu/chrome setzt den Browser.
// „coding“ und „demo“ brauchen ein Chromium im PATH des Servers (Screenshot-Werkzeug), „demo“ dazu ffmpeg (GIF).
// „sandbox“ braucht Podman (das Sandbox-Image bringt Chromium für das Screenshot-Werkzeug mit); das Demo-Projekt
// (scripts/demo/roastery) wird dafür als Git-Repository in einen temporären Ordner kopiert.
import { execSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

let pw;
try {
  pw = await import("playwright");
} catch {  // global installiert (npm i -g) – ESM ignoriert NODE_PATH
  const root = execSync("npm root -g").toString().trim();
  pw = await import(pathToFileURL(join(root, "playwright", "index.mjs")).href);
}
const { chromium } = pw;

const args = process.argv.slice(2);
const base = args.find((a) => a.startsWith("http")) || "http://localhost:8765";
const only = args.filter((a) => !a.startsWith("http"));
const want = (name) => !only.length || only.includes(name);

// Zugangs-Token wie die orbwise-CLI: $ORBWISE_WEB_TOKEN_FILE bzw. ~/.config/orbwise/web-token
const tokenFile = process.env.ORBWISE_WEB_TOKEN_FILE || join(homedir(), ".config", "orbwise", "web-token");
const token = existsSync(tokenFile) ? readFileSync(tokenFile, "utf8").trim() : "";
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const view = { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2,
               extraHTTPHeaders: token ? { "X-Orbwise-Token": token } : {} };
let context = await browser.newContext(view);
let page = await context.newPage();

async function ask(text) {
  await page.fill("#input", text);
  await page.press("#input", "Enter");
}

async function api(method, path, data) {
  const res = await page.request.fetch(base + path, { method, data, headers: { Origin: base } });
  if (!res.ok()) throw new Error(`${method} ${path}: ${res.status()} ${await res.text()}`);
  return res.json();
}

async function open() {
  await page.goto(base);
  const boot = page.locator("#boot-btn");
  if (await boot.isVisible().catch(() => false)) await boot.click();
  await page.waitForTimeout(4500); // Startsequenz
}

await open();

// Tools-Modus mit leerem Chat (ein früherer Lauf kann den Coding-Modus hinterlassen haben)
async function toolsChat() {
  await api("POST", "/api/mode", { mode: "tools" });
  await open();
  await page.click("#chat-new");
  await page.waitForTimeout(900);
}

async function demoProject() {
  const project = join(mkdtempSync(join(tmpdir(), "orbwise-demo-")), "roastery");
  cpSync(join(dirname(fileURLToPath(import.meta.url)), "demo", "roastery"), project, { recursive: true });
  const git = "git -c user.name=Demo -c user.email=demo@example.com";
  execSync(`git init -q -b main && ${git} add -A && ${git} commit -qm "Landing page" && ` +
           "git remote add origin https://github.com/orbwise-demo/roastery.git", { cwd: project });
  return project;
}

// Wartet auf target und bestätigt unterwegs jede Rückfrage (in der Standardstufe „Nur lesen“ fragt die Änderung an
// der Datei mit einer Diff-Karte) – pause: so lange bleibt die Karte vorher sichtbar (für den Film)
async function approveUntil(target, pause = 300) {
  const until = Date.now() + 120000;
  while (Date.now() < until) {
    if (await target.isVisible().catch(() => false)) return;
    if (await page.locator("#confirm").isVisible().catch(() => false)) {
      await page.waitForTimeout(pause);
      await page.click("#confirm-yes");
    }
    await page.waitForTimeout(250);
  }
  throw new Error("Zeitlimit: das erwartete Ergebnis ist nicht erschienen");
}

// Coding-Chat mit dem Demo-Projekt
async function codingChat() {
  await api("POST", "/api/mode", { mode: "coding" });
  const chat = await api("POST", "/api/chats");  // immer ein frischer Chat – sonst steht das Ergebnis eines früheren Laufs da
  await api("PATCH", `/api/chats/${chat.id}`, { title: "Landing page headline" });
  await api("POST", `/api/chats/${chat.id}/project`, { path: await demoProject() });
  await open();
}

// 0) Startseite: Orb und Spruch
if (want("start")) {
  await toolsChat();
  await page.screenshot({ path: "docs/screenshot-start.png" });
}

// 1) Gespräch mit laufender Websuche, Aktivität mit Telemetrie
if (want("chat")) {
  await toolsChat();
  await ask("Good evening! What can you help me with?");
  await page.waitForTimeout(3000);
  await page.click("#btn-activity");
  await ask("Search the web for Linux 6.18 release notes");
  await page.waitForTimeout(700); // die Suche läuft noch
  await page.screenshot({ path: "docs/screenshot-chat.png" });
  await page.waitForTimeout(6000);
  await page.click("#btn-activity");
}

// 2) Rückfrage vor einer Änderung am System
if (want("confirm")) {
  await toolsChat();
  await ask("Please update my system");
  await page.locator("#confirm").waitFor({ state: "visible" });
  await page.waitForTimeout(900);
  await page.screenshot({ path: "docs/screenshot-confirm.png" });
  await page.click("#confirm-no");
  await page.waitForTimeout(1500);
}

// 3a) Coding ohne Sandbox: Agent ändert die Demo-Seite (Diff-Karte bestätigt), sieht sich das Ergebnis an,
//     „1 Datei geändert“ mit Rückgängig
if (want("coding")) {
  await codingChat();
  await ask("Make the landing page headline more inviting and check how it looks");
  await approveUntil(page.locator(".msg-shots img").first());
  await page.waitForTimeout(2500);
  await page.screenshot({ path: "docs/screenshot-coding.png" });
  await api("POST", "/api/mode", { mode: "tools" });
}

// 3) Coding in der Sandbox: Agent ändert die Demo-Seite und schaut sich das Ergebnis per Screenshot an
if (want("sandbox")) {
  const project = join(mkdtempSync(join(tmpdir(), "orbwise-demo-")), "roastery");
  cpSync(join(dirname(fileURLToPath(import.meta.url)), "demo", "roastery"), project, { recursive: true });
  const git = "git -c user.name=Demo -c user.email=demo@example.com";
  execSync(`git init -q -b main && ${git} add -A && ${git} commit -qm "Landing page" && ` +
           "git remote add origin https://github.com/orbwise-demo/roastery.git", { cwd: project });
  const mode = await api("POST", "/api/mode", { mode: "coding" });
  await api("PATCH", `/api/chats/${mode.chat_id}`, { title: "Landing page headline" });
  await api("POST", `/api/chats/${mode.chat_id}/project`, { path: project });
  await api("POST", "/api/sandbox", { enabled: true });
  await open();
  await ask("Make the landing page headline more inviting and check how it looks");
  await page.locator(".msg-shots img").first().waitFor({ state: "visible", timeout: 120000 });
  await page.waitForTimeout(2500); // Antwort zu Ende, Bild geladen
  await page.screenshot({ path: "docs/screenshot-sandbox.png" });
}

// 4) Kurzer Film fürs README: dieselbe Coding-Aufgabe von der Bitte bis zum Ergebnis, als GIF
if (want("demo")) {
  await codingChat();
  await context.close();
  const dir = mkdtempSync(join(tmpdir(), "orbwise-film-"));
  context = await browser.newContext({ ...view, deviceScaleFactor: 1, recordVideo: { dir, size: view.viewport } });
  page = await context.newPage();
  await open();
  await page.locator("#input").pressSequentially("Make the landing page headline more inviting and check how it looks",
                                                 { delay: 35 });
  await page.waitForTimeout(500);
  await page.press("#input", "Enter");
  await approveUntil(page.locator(".msg-shots img").first(), 2200);  // die Diff-Karte kurz stehen lassen
  await page.waitForTimeout(4000);
  const video = await page.video().path();
  await context.close();
  // die Startsequenz (~5 s) wegschneiden; Palette je Film, damit die ruhigen Flächen nicht rauschen
  execSync(`ffmpeg -y -loglevel error -ss 5.5 -i "${video}" -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];` +
           `[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none" docs/demo.gif`);
  rmSync(dir, { recursive: true, force: true });
}

await browser.close();
