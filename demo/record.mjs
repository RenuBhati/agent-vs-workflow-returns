// Records the demo page scene by scene as timestamped frames.
// Output: rec/frames/*.jpg, rec/frames.json, rec/beats.json (scene and wait timings, trace numbers).
import fs from "node:fs";
import puppeteer from "puppeteer-core";

const OUT = "rec";
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(`${OUT}/frames`, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true, timeout: 180000, protocolTimeout: 1800000,
  defaultViewport: { width: 1280, height: 800, deviceScaleFactor: 1 },
  args: ["--force-color-profile=srgb", "--hide-scrollbars"],
});
const page = await browser.newPage();
await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
page.on("pageerror", (e) => console.log("PAGE ERROR", e.message));

const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;
const frames = [], beats = [], waits = [];
let n = 0;
const cdp = await page.createCDPSession();
cdp.on("Page.screencastFrame", async (f) => {
  const file = `${OUT}/frames/${String(n++).padStart(6, "0")}.jpg`;
  fs.writeFileSync(file, Buffer.from(f.data, "base64"));
  frames.push({ t: now(), file });
  await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
});

await page.goto("http://localhost:8000/", { waitUntil: "networkidle0" });

// ---------- helpers drawn into the page, so they are part of the recording
await page.addStyleTag({ content: `
  #card { position: fixed; inset: 0; background: #f6f5f2; z-index: 50; display: flex; align-items: center;
          justify-content: center; font: 17px/1.5 system-ui, sans-serif; color: #1d1d1b; }
  #card .inner { max-width: 820px; padding: 0 40px; }
  #card h1 { font-size: 38px; margin: 0 0 14px; } #card p { color: #555; margin: 6px 0; }
  #card table { border-collapse: collapse; margin-top: 18px; font-size: 18px; width: 100%; }
  #card td, #card th { padding: 8px 14px; border-bottom: 1px solid #ddd; text-align: left; }
  #card th { color: #666; font-weight: 500; } #card td.n { text-align: right; font-variant-numeric: tabular-nums; }
  #banner { position: fixed; left: 50%; top: 76px; transform: translateX(-50%); z-index: 40; background: #1d1d1b;
            color: #fff; padding: 8px 16px; border-radius: 999px; font: 14px system-ui, sans-serif; }
  .ring { position: fixed; z-index: 60; width: 34px; height: 34px; margin: -17px 0 0 -17px; border-radius: 50%;
          border: 3px solid #2f5d50; pointer-events: none; animation: ring .6s ease-out forwards; }
  @keyframes ring { from { transform: scale(.4); opacity: 1 } to { transform: scale(1.6); opacity: 0 } }` });

const card = (html) => page.evaluate((h) => {
  document.getElementById("card")?.remove();
  if (!h) return;
  const d = document.createElement("div"); d.id = "card"; d.innerHTML = `<div class="inner">${h}</div>`;
  document.body.append(d);
}, html);
const banner = (text) => page.evaluate((t) => {
  let b = document.getElementById("banner");
  if (!t) { b?.remove(); return; }
  if (!b) { b = document.createElement("div"); b.id = "banner"; document.body.append(b); }
  b.textContent = t;
}, text);
const pause = (s) => new Promise((r) => setTimeout(r, s * 1000));

async function click(sel) {
  const box = await (await page.$(sel)).boundingBox();
  await page.evaluate((x, y) => {
    const r = document.createElement("div"); r.className = "ring"; r.style.left = x + "px"; r.style.top = y + "px";
    document.body.append(r); setTimeout(() => r.remove(), 700);
  }, box.x + box.width / 2, box.y + box.height / 2);
  await pause(0.35);
  await page.click(sel);
}

// Waits for the assistant; the time is marked as a wait and shown with a running timer.
async function waitForReply(label) {
  const start = now();
  const timer = setInterval(() => {
    const s = Math.round(now() - start);
    banner(`${label} · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} real time (sped up)`).catch(() => {});
  }, 1000);
  await page.waitForFunction(() => !document.querySelector(".thinking"), { timeout: 1500000, polling: 250 });
  clearInterval(timer);
  const end = now();
  await banner("");
  if (end - start > 4) waits.push({ start: start + 0.5, end, real: end - start });
  return end - start;
}

async function say(text, label) {
  await page.type("#text", text, { delay: 35 });
  await pause(0.3);
  await click("#sendbtn");
  return waitForReply(label);
}

async function newConversation(assistant, persona = "amira") {
  await page.select("#assistant", assistant);
  await page.select("#persona", persona);
  await click("#reset");
  await page.waitForFunction(() => document.querySelector(".empty")?.textContent.startsWith("Shop reset"));
  await pause(0.4);
  await click("#start");
  await page.waitForSelector("#text:not([disabled])");
}

async function beat(name, fn) {
  const start = now();
  const extra = (await fn()) || {};
  beats.push({ name, start, end: now(), ...extra });
  console.log(`beat ${name}: ${(now() - start).toFixed(1)} s`, JSON.stringify(extra));
}

await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1 });
await pause(1.5);

// ---------- the scenes
await beat("intro", async () => {
  await card(`<h1>Agent vs workflow</h1>
    <p>Clothing returns and exchanges, built twice on the same model, tools and database rules.</p>
    <p>A <b>fixed workflow</b>: the model reads each message, plain code decides the next step.</p>
    <p>A <b>tool-using agent</b>: the model decides which tools to call and what to ask.</p>
    <p style="margin-top:18px;color:#888">Model: qwen3.5:4b, running locally on an 8 GB MacBook Air.</p>`);
  await pause(3);
});

await beat("workflow_ask", async () => {
  await card("");
  await newConversation("workflow");
  await pause(0.6);
  await say("Hi, I'd like to exchange my blue Oxford for a medium please.", "Workflow working");
  await pause(0.8);
});

await beat("workflow_confirm", async () => {
  await click("#confirm");
  await waitForReply("Workflow working");
  await pause(1);
  return { totals: await page.$eval("#totals", (e) => e.textContent) };
});

await beat("agent_ask", async () => {
  await newConversation("agent");
  await pause(0.6);
  const s = await say("Hi, I'd like to exchange my blue Oxford for a medium please.", "Agent working");
  await pause(1.2);
  return { seconds: s };
});

await beat("agent_confirm", async () => {
  await click("#confirm");
  const s = await waitForReply("Agent working");
  await pause(1.5);
  return { seconds: s, totals: await page.$eval("#totals", (e) => e.textContent) };
});

await beat("return_setup", async () => {
  await newConversation("workflow");
  await pause(0.5);
  await say("I'd like to return my white Oxford.", "Workflow working");
  await pause(0.8);
});

await beat("yes_not_confirm", async () => {
  await say("Yes, go ahead.", "Workflow working");
  await pause(1.5);
  return { reply: await page.$$eval(".msg.assistant", (m) => m.at(-1).textContent) };
});

await beat("results", async () => {
  await card(`<h1>Held-out results</h1>
    <p>17 scenarios × 3 wordings = 51 conversations per assistant, run once after freezing both.</p>
    <table>
      <tr><th></th><th class="n">Workflow</th><th class="n">Agent</th></tr>
      <tr><td>Right final state</td><td class="n">45</td><td class="n">44</td></tr>
      <tr><td>Clean (no other problem)</td><td class="n">45</td><td class="n">31</td></tr>
      <tr><td>Claimed success with nothing done</td><td class="n">0</td><td class="n">1</td></tr>
      <tr><td>Seconds per conversation</td><td class="n">8.7</td><td class="n">126.6</td></tr>
      <tr><td>Prompt tokens per conversation</td><td class="n">525</td><td class="n">17,005</td></tr>
    </table>
    <p style="margin-top:18px;color:#888">Results, decisions and every failure: github.com/RenuBhati/agent-vs-workflow-returns</p>`);
  await pause(3);
});

await cdp.send("Page.stopScreencast");
await pause(0.5);
fs.writeFileSync(`${OUT}/frames.json`, JSON.stringify({ frames, end: now() }, null, 1));
fs.writeFileSync(`${OUT}/beats.json`, JSON.stringify({ beats, waits }, null, 1));
console.log(`${frames.length} frames, ${now().toFixed(0)} s recorded, ${waits.length} waits`);
await browser.close();
