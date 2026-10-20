/* Browser capture for frames.py. Install the clock before navigation, pause it
   after loading, then run every timer and rAF due in each frame interval. */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import fs from "node:fs";
import path from "node:path";

async function main() {
  const args = process.argv.slice(2);
  const options = {};
  const urls = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      if (!["--out", "--path", "--frames", "--fps", "--viewport"].includes(args[i]) || !args[i + 1])
        throw new Error(`invalid option: ${args[i]}`);
      options[args[i].slice(2)] = args[++i];
    } else urls.push(args[i]);
  }
  const count = Number(options.frames || 60);
  const fps = Number(options.fps || 30);
  const [width, height] = (options.viewport || "1280x720").split("x").map(Number);
  if (!options.out || !options.path || urls.length !== 1 || !Number.isInteger(count) || count < 1 ||
      !Number.isFinite(fps) || fps <= 0 || !Number.isInteger(width) || width < 1 || !Number.isInteger(height) || height < 1)
    throw new Error("usage: frames.mjs --out DIR --path SCRIPT.mjs [--frames N --fps F --viewport WxH] URL");
  const require = createRequire(path.join(process.env.GDESIGN || "/home/gabriel/g-design", "package.json"));
  const { chromium } = require("playwright");
  const version = require("playwright/package.json").version;
  const { default: move } = await import(pathToFileURL(path.resolve(options.path)).href);
  if (typeof move !== "function") throw new Error("path script must default-export async (page, i, n)");
  const directory = path.join(options.out, "frames");
  fs.mkdirSync(directory, { recursive: true });
  if (fs.readdirSync(directory).length) throw new Error(`capture directory is not empty: ${directory}`);
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const start = Date.UTC(2026, 0, 1);
    await page.clock.install({ time: start });
    const response = await page.goto(urls[0], { waitUntil: "networkidle" });
    if (!response || !response.ok()) throw new Error(`navigation failed: HTTP ${response?.status()}`);
    await page.evaluate(() => document.fonts.ready);
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1000);
    const frames = [];
    const modes = new Set();
    for (let i = 0; i < count; i++) {
      await move(page, i, count);
      const stepped = await page.evaluate(async (frame) => {
        if (typeof window.__glevelsFrame !== "function") return false;
        await window.__glevelsFrame(frame);
        return true;
      }, i);
      if (!stepped) {
        const elapsed = Math.round((i + 1) * 1000 / fps) - Math.round(i * 1000 / fps);
        await page.clock.runFor(elapsed);
      }
      modes.add(stepped ? "engine" : "clock");
      const name = `${String(i).padStart(8, "0")}.png`;
      await page.screenshot({ path: path.join(directory, name), fullPage: false, scale: "css" });
      frames.push(name);
    }
    fs.writeFileSync(path.join(directory, "frames.json"), JSON.stringify({
      sequences: [{ name: "capture", camera: "moving", frames }],
      capture: { url: urls[0], fps, viewport: { width, height }, playwright: version, steppers: [...modes] },
    }, null, 2) + "\n");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(`impossible: ${error.message}`);
  process.exitCode = 2;
});
