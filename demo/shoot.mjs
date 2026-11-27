import fs from "node:fs";
import puppeteer from "puppeteer-core";
const SRC = process.argv[2] || "slides.html", DEST = process.argv[3] || "slides";
fs.mkdirSync(DEST, { recursive: true });
const b = await puppeteer.launch({ executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true, timeout: 180000, defaultViewport: { width: 1280, height: 800 } });
const p = await b.newPage();
await p.goto("file://" + process.cwd() + "/" + SRC, { waitUntil: "networkidle0" });
for (const s of await p.$$("section.slide")) {
  const id = await s.evaluate((e) => e.id);
  const over = await s.evaluate((e) => [...e.querySelectorAll("*")].some((c) => {
    const r = c.getBoundingClientRect(), o = e.getBoundingClientRect();
    return r.bottom > o.bottom - 20 || r.right > o.right; }));
  await s.screenshot({ path: `${DEST}/${id}.jpg`, type: "jpeg", quality: 92 });
  console.log(id, over ? "OVERFLOW" : "ok");
}
await b.close();
