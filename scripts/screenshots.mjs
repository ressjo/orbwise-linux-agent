// README-Screenshots im Demo-Modus neu erzeugen (kein echtes Modell nötig).
//
//   ORBWISE_FAKE_LLM=1 orbwise serve   (mit einer Config: language: en, voice.enabled: false)
//   node scripts/screenshots.mjs [http://localhost:8765] [chat|confirm|sandbox …]
//
// Braucht Playwright (npm i -g playwright). CHROMIUM=/pfad/zu/chrome setzt den Browser.
// „sandbox“ braucht Podman (das Sandbox-Image bringt Chromium für das Screenshot-Werkzeug mit); das Demo-Projekt
// (scripts/demo/roastery) wird dafür als Git-Repository in einen temporären Ordner kopiert.
import { execSync } from "node:child_process";
import { cpSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
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

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1440, height: 860 } });

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

// 1) Gespräch: kompakter Orb, Werkzeug-Symbol am Orb, Aktivität mit Telemetrie
if (want("chat")) {
  await ask("Good evening! What can you help me with?");
  await page.waitForTimeout(3000);
  await ask("Search the web for Linux 6.18 release notes");
  await page.waitForTimeout(800); // Werkzeug-Symbol (Cloud) + Strahl am Orb sichtbar
  await page.screenshot({ path: "docs/screenshot-chat.png" });
  await page.waitForTimeout(6000);
}

// 2) Rückfrage vor einer Änderung am System
if (want("confirm")) {
  await page.click("#chat-new"); // neuer Chat
  await page.waitForTimeout(800);
  await ask("Please update my system");
  await page.locator("#confirm").waitFor({ state: "visible" });
  await page.waitForTimeout(900);
  await page.screenshot({ path: "docs/screenshot-confirm.png" });
  await page.click("#confirm-no");
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

await browser.close();
