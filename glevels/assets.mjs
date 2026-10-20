/* Browser half of correct.assets.real-sourced; assets.py resolves and reports. */
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const gdesign = process.env.GDESIGN || "/home/gabriel/g-design";
const require = createRequire(path.join(gdesign, "package.json"));
const { chromium } = require("playwright");
const housePaths = new Set();
for (const dir of ["packages/g-icons/icons", "packages/g-brand"]) {
  const folder = path.join(gdesign, dir);
  if (!fs.existsSync(folder)) continue;
  for (const file of fs.readdirSync(folder).filter(f => f.endsWith(".svg"))) {
    for (const match of fs.readFileSync(path.join(folder, file), "utf8").matchAll(/<path\b[^>]*\bd=["']([^"']+)["']/g)) housePaths.add(match[1].replace(/\s+/g, ""));
  }
}
const args = process.argv.slice(2);
const outIndex = args.indexOf("--out");
const out = args[outIndex + 1];
const urls = args.filter((_, i) => i !== outIndex && i !== outIndex + 1);
const hash = bytes => createHash("sha256").update(bytes).digest("hex").slice(0, 12);
const assetDir = path.join(out, "assets");
fs.mkdirSync(assetDir, { recursive: true });
let serial = 0;

function collect(knownPaths) {
  const assets = [], findings = [];
  const layers = value => {
    const parts = [];
    let start = 0, depth = 0, quote = "";
    for (let i = 0; i < value.length; i++) {
      const c = value[i];
      if (quote) {
        if (c === "\\") i++;
        else if (c === quote) quote = "";
      } else if (c === '"' || c === "'") quote = c;
      else if (c === "(") depth++;
      else if (c === ")") depth--;
      else if (c === "," && depth === 0) { parts.push(value.slice(start, i).trim()); start = i + 1; }
    }
    parts.push(value.slice(start).trim());
    return parts;
  };
  const selector = el => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      parts.unshift(`${p.localName}:nth-of-type(${[...p.parentElement.children].filter(x => x.localName === p.localName).indexOf(p) + 1})`);
    }
    return parts.join(" > ") || "body";
  };
  const description = el => {
    const r = el.getBoundingClientRect();
    const sample = (el.getAttribute("alt") || el.getAttribute("aria-label") || el.closest("figure")?.querySelector("figcaption")?.textContent || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 50) || el.localName;
    return { selector: selector(el), sample,
      rect: [r.x + scrollX, r.y + scrollY, r.width, r.height].map(Math.round) };
  };
  const add = (el, type, url, extra = {}) => assets.push({ ...description(el), type, url, ...extra });
  const lead = (el, check, detail) => findings.push({ ...description(el), severity: "review", check, detail });
  for (const el of document.querySelectorAll("body *")) {
    if (el.closest("script, style, template, noscript") || (el.closest("svg") && el.localName !== "svg")) continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && (!el.checkVisibility || el.checkVisibility({ opacityProperty: true, visibilityProperty: true }));
    if (el.localName === "img") {
      const url = el.currentSrc || el.src;
      add(el, "image", url || `missing:img:${selector(el)}`, { alt: el.alt, natural: [el.naturalWidth, el.naturalHeight], densityAdjusted: !!(el.srcset || el.closest("picture")?.querySelector("source[srcset]")), broken: el.complete && !el.naturalWidth, visible, objectFit: s.objectFit });
    }
    if (el.matches("video, audio")) {
      if (el.currentSrc || el.src) add(el, "media", el.currentSrc || el.src, { broken: !!el.error, visible });
      for (const child of el.querySelectorAll("source[src]")) add(el, "media", child.src, { broken: !!el.error, visible });
      if (el.poster) add(el, "image", el.poster, { visible, poster: true });
    }
    for (const pseudo of [null, "::before", "::after"]) {
      const st = pseudo ? getComputedStyle(el, pseudo) : s;
      if (pseudo && st.content === "none") continue;
      const sizes = layers(st.backgroundSize);
      for (const [layerIndex, layer] of layers(st.backgroundImage).entries()) {
        for (const m of layer.matchAll(/url\((?:"([^"]*)"|'([^']*)'|([^)]*))\)/g)) {
          add(el, "image", new URL(m[1] ?? m[2] ?? m[3], document.baseURI).href, { visible, background: true, backgroundSize: sizes[layerIndex % sizes.length], pseudo, drawnSize: pseudo ? [parseFloat(st.width), parseFloat(st.height)] : null });
        }
      }
    }
    if (!visible) continue;
    const glyph = [s, getComputedStyle(el, "::before"), getComputedStyle(el, "::after")].find(st =>
      /phosphor/i.test(st.fontFamily) && (st === s ? el.textContent.trim() : !["none", "normal", '""'].includes(st.content)));
    if (glyph && !el.children.length) add(el, "font-glyph", "inline:font:", { markup: glyph.fontFamily + (glyph === s ? el.textContent : glyph.content), visible, house: true });
    if (el.localName === "svg" && Math.max(r.width, r.height) >= 24) {
      const paths = [...el.querySelectorAll("path")];
      const house = paths.length > 0 && !el.querySelector("circle, ellipse, rect, line, polyline, polygon, image, use, text") && paths.every(p => knownPaths.includes((p.getAttribute("d") || "").replace(/\s+/g, "")));
      add(el, "svg", "inline:svg:", { markup: el.outerHTML, visible, house });
    }
    if (el.localName === "canvas") {
      let content;
      try { content = el.toDataURL(); } catch { content = el.outerHTML; }
      add(el, "canvas", "inline:canvas:", { markup: content, visible });
    }
    if (el instanceof HTMLElement && !el.children.length && !el.textContent.trim() && !el.matches("img, video, audio, canvas, input, hr") && s.backgroundImage === "none" && Math.min(r.width, r.height) >= 24) {
      const colour = s.backgroundColor.match(/[\d.]+/g)?.map(Number) || [];
      const rgb = colour.slice(0, 3).map(v => v / 255);
      const hi = Math.max(...rgb), lo = Math.min(...rgb), light = (hi + lo) / 2;
      const saturation = hi === lo ? 0 : (hi - lo) / (1 - Math.abs(2 * light - 1));
      const radius = s.borderTopLeftRadius.endsWith("%") ? parseFloat(s.borderTopLeftRadius) / 100 : parseFloat(s.borderTopLeftRadius) / Math.min(r.width, r.height);
      if ((colour.length === 3 || colour[3] > 0.5) && saturation < 0.12 && (radius >= 0.4 || Math.min(r.width, r.height) >= 64)) {
        lead(el, "empty-slot", radius >= 0.4 ? "grey circle with no image or initials" : "empty image-shaped slot with a flat grey background");
      }
    }
    const directText = [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join("");
    const context = el.closest("button, h1, h2, h3, h4, h5, h6, nav, li, [role=button], [class*=card]");
    const marker = el.localName === "li" ? getComputedStyle(el, "::marker").content : "";
    const pseudoText = ["::before", "::after"].map(p => getComputedStyle(el, p).content).join("");
    if (context && /\p{Extended_Pictographic}/u.test(directText + marker + pseudoText)) lead(el, "emoji-icon", "emoji used as an icon");
  }
  return { assets, findings, pageHeight: document.documentElement.scrollHeight };
}

async function primitiveSvg(page, markup) {
  return page.evaluate(text => {
    const root = new DOMParser().parseFromString(text, "image/svg+xml");
    if (root.querySelector("parsererror, image, linearGradient, radialGradient, pattern, filter, text, use, foreignObject")) return 0;
    const shapes = [...root.querySelectorAll("circle, ellipse, rect, line, polyline, polygon, path")];
    if (!shapes.length || shapes.length > 12) return 0;
    if (shapes.some(s => s.localName === "path" && /[cCqQaAsStT]/.test(s.getAttribute("d") || ""))) return 0;
    if (shapes.some(s => ["polyline", "polygon"].includes(s.localName) && (s.getAttribute("points") || "").split(/[\s,]+/).length > 16)) return 0;
    return shapes.length;
  }, markup);
}

const browser = await chromium.launch();
const results = [];
try {
  for (const [targetIndex, url] of urls.entries()) {
    for (const [name, width, height] of [["desktop", 1440, 900], ["phone", 390, 844]]) {
      const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, reducedMotion: "reduce" });
      const page = await context.newPage();
      const entry = { target_index: targetIndex, url, viewport: `${name} ${width}x${height}`, assets: [], findings: [] };
      const requests = new Map(), pending = [];
      const isAsset = request => ["image", "media", "font"].includes(request.resourceType()) || /\.(?:glb|gltf|ktx2|hdr|exr|splat|ply|spz)(?:[?#]|$)/i.test(request.url());
      const noteRequest = request => {
        if (!isAsset(request)) return null;
        if (!requests.has(request.url())) {
          const previous = request.redirectedFrom();
          requests.set(request.url(), previous && requests.get(previous.url()) || { url: request.url(), type: request.resourceType(), selector: "request", sample: path.basename(new URL(request.url()).pathname).slice(0, 50) });
        }
        return requests.get(request.url());
      };
      page.on("request", noteRequest);
      page.on("requestfailed", request => {
        const record = noteRequest(request);
        if (record) record.failed = request.failure()?.errorText || "load failed";
      });
      page.on("response", response => {
        const record = noteRequest(response.request());
        if (!record) return;
        record.status = response.status();
        if (record.status >= 400) record.failed = `HTTP ${record.status}`;
        record.mime = (response.headers()["content-type"] || "").split(";")[0];
        const image = /^image\/(?:png|jpeg|webp|gif|avif|svg\+xml)$/.test(record.mime) || /\.(?:png|jpe?g|webp|gif|avif|svg)(?:[?#]|$)/i.test(record.url);
        if (record.status < 200 || record.status >= 300 || (!image && !/^(?:data|blob):/.test(record.url))) return;
        pending.push((async () => {
          try {
            const body = await response.body();
            if (/^(?:data|blob):/.test(record.url)) record.key = `${record.url.split(":")[0]}:${record.mime || record.type}:${hash(body)}`;
            if (!image) return;
            const ext = record.mime === "image/svg+xml" ? ".svg" : ({ "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif" }[record.mime] || path.extname(new URL(record.url).pathname));
            record.original = path.join(assetDir, `${hash(body)}-original${ext}`);
            fs.writeFileSync(record.original, body);
          } catch (e) { record.readError = String(e).slice(0, 160); }
        })());
      });
      try {
        const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
        if (response?.status() >= 400) throw new Error(`HTTP ${response.status()}`);
        await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
        await page.evaluate(() => Promise.race([document.fonts.ready, new Promise(resolve => setTimeout(resolve, 6000))]));
        const reach = await page.evaluate(() => document.documentElement.scrollHeight);
        for (let y = 0; y < reach; y += height) {
          await page.evaluate(top => scrollTo(0, top), y);
          await page.waitForTimeout(100);
        }
        await page.waitForFunction(() => [...document.images].every(i => i.complete), null, { timeout: 5000 }).catch(() => {});
        await page.evaluate(() => scrollTo(0, 0));
        const collected = await page.evaluate(collect, [...housePaths]);
        entry.pageHeight = collected.pageHeight;
        entry.findings.push(...collected.findings);
        await Promise.all(pending);
        for (const asset of collected.assets) {
          const record = requests.get(asset.url);
          if (record) Object.assign(asset, { original: record.original, mime: record.mime, failed: record.failed });
          if (asset.markup) {
            asset.key = asset.url + hash(asset.markup);
            asset.url = asset.key;
          } else if (asset.url.startsWith("data:") || asset.url.startsWith("blob:")) {
            const raw = asset.original ? null : await page.evaluate(async u => Array.from(new Uint8Array(await (await fetch(u)).arrayBuffer())), asset.url).catch(() => []);
            const bytes = asset.original ? fs.readFileSync(asset.original) : Buffer.from(raw);
            const mime = asset.url.startsWith("data:") ? asset.url.slice(5).split(/[;,]/)[0] : (asset.mime || asset.type);
            asset.mime ||= mime;
            asset.key = `${asset.url.startsWith("data:") ? "data" : "blob"}:${mime}:${hash(bytes)}`;
            if (!asset.original && /^image\/(?:png|jpeg|webp|gif|avif|svg\+xml)$/.test(mime)) {
              const ext = { "image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg" }[mime] || mime.split("/")[1];
              asset.original = path.join(assetDir, `${hash(bytes)}-original.${ext}`);
              fs.writeFileSync(asset.original, bytes);
            }
            asset.url = asset.key;
          } else asset.key = asset.url;
          if (asset.type === "image" && (!asset.natural || asset.background || asset.poster || asset.densityAdjusted) && !asset.failed) {
            const imageUrl = record?.url || asset.url;
            const natural = await page.evaluate(async u => {
              const i = new Image(); i.src = u;
              try { await i.decode(); return [i.naturalWidth, i.naturalHeight]; } catch { return null; }
            }, imageUrl);
            if (natural) asset.natural = natural;
            else asset.broken = true;
          }
          const add = (check, severity, detail) => entry.findings.push({ selector: asset.selector + (asset.pseudo || ""), sample: asset.sample, rect: asset.rect, check, severity, detail });
          if (asset.broken || asset.failed) add("broken-media", "block", `broken image or media: ${asset.url} (${asset.failed || "naturalWidth 0"})`);
          if (record?.readError) add("metadata-unavailable", "review", `original could not be read for provenance: ${record.readError}`);
          const svg = asset.type === "svg" || asset.mime === "image/svg+xml" || /\.svg(?:[?#]|$)/i.test(asset.url);
          if (asset.natural?.[0] && !svg && asset.visible && (!asset.pseudo || asset.drawnSize?.every(v => v > 0))) {
            let [shownW, shownH] = asset.drawnSize || asset.rect.slice(2);
            const [nw, nh] = asset.natural;
            if (asset.background && !["contain", "cover"].includes(asset.backgroundSize)) {
              const dims = asset.backgroundSize.split(/\s+/);
              const size = (value, parent) => {
                const n = value?.endsWith("%") ? parseFloat(value) * parent / 100 : parseFloat(value);
                return Number.isFinite(n) ? n : null;
              };
              const bw = size(dims[0], shownW), bh = size(dims[1], shownH);
              shownW = bw ?? (bh !== null ? bh * nw / nh : nw);
              shownH = bh ?? (bw !== null ? bw * nh / nw : nh);
            } else if (asset.background && asset.backgroundSize === "cover") {
              const scale = Math.max(shownW / nw, shownH / nh);
              shownW = nw * scale; shownH = nh * scale;
            } else if (asset.objectFit === "cover" && !asset.background) {
              const scale = Math.max(shownW / nw, shownH / nh);
              shownW = nw * scale; shownH = nh * scale;
            } else if (asset.objectFit === "none" && !asset.background) {
              shownW = nw; shownH = nh;
            } else if (asset.objectFit === "contain" || asset.objectFit === "scale-down" || (asset.background && asset.backgroundSize === "contain")) {
              const scale = Math.min(shownW / nw, shownH / nh, asset.objectFit === "scale-down" ? 1 : Infinity);
              shownW = nw * scale; shownH = nh * scale;
            }
            if (shownW > nw + 1 || shownH > nh + 1) add("upscaled", "block", `raster upscaled: natural ${nw}x${nh}, shown ${Math.round(shownW)}x${Math.round(shownH)} at DPR 1`);
            else if (shownW * 2 > nw + 1 || shownH * 2 > nh + 1) add("retina-resolution", "review", `raster has less than 2x resolution for a retina screen: natural ${nw}x${nh}, shown ${Math.round(shownW)}x${Math.round(shownH)}`);
          }
          if (asset.visible && Math.max(...(asset.drawnSize || asset.rect.slice(2))) >= 48) {
            let markup = asset.markup;
            if (svg && !markup && asset.original) markup = fs.readFileSync(asset.original, "utf8");
            if (svg && markup) {
              const count = await primitiveSvg(page, markup);
              if (count) add("primitive-svg", "review", `${count} primitive shapes: drawn from shapes, not crafted`);
            }
            const slug = asset.sample.replace(/[^a-z0-9]+/gi, "-").slice(0, 40) || asset.type;
            asset.crop = path.join(assetDir, `${++serial}-${slug}.png`);
            try {
              await page.locator(asset.selector).screenshot({ path: asset.crop, animations: "disabled", scale: "css", timeout: 5000 });
            } catch (e) {
              delete asset.crop;
              add("crop-unavailable", "review", "asset crop could not be saved: " + String(e).slice(0, 100));
            }
          }
          delete asset.markup;
          entry.assets.push(asset);
        }
        for (const record of new Set(requests.values())) {
          if (collected.assets.some(a => a.key === (record.key || record.url) || a.original && a.original === record.original)) continue;
          entry.assets.push({ ...record, url: record.key || record.url, key: record.key || record.url });
          if (record.failed) entry.findings.push({ severity: "block", check: "broken-media", selector: record.selector, sample: record.sample, detail: `media failed to load: ${record.url} (${record.failed})` });
        }
        entry.screenshot = path.join(out, `${targetIndex}-${name}.png`);
        await page.screenshot({ path: entry.screenshot, fullPage: true, animations: "disabled" });
      } catch (e) { entry.error = String(e).slice(0, 400); }
      results.push(entry);
      await context.close();
    }
  }
} finally { await browser.close(); }
fs.writeFileSync(path.join(out, "findings.json"), JSON.stringify(results, null, 2));
