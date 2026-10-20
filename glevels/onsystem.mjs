/* Browser half of correct.ui.on-system. onsystem.py resolves and serves targets,
   holds the host browser lock, and supplies the maintained optional tools. */
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire("/home/gabriel/g-design/package.json");
const { chromium } = require("playwright");
let postcss;
try { postcss = require("postcss"); } catch {}
const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const outDir = config.out;
const viewports = config.viewports.split(",").map(v => {
  const [name, size] = v.split("=");
  const [width, height] = size.split("x").map(Number);
  if (!width || !height) throw Error(`invalid viewport: ${v}`);
  return { name, width, height };
});
const properties = ["color", "background-color", "border-color", "fill", "stroke", "font-size", "font-family",
  "font-weight", "border-radius", "box-shadow", "gap", "padding", "margin"];
const basename = url => url.split(/[?#]/)[0].split("/").pop();

async function readCss(source, request, seen = new Set(), skip = []) {
  if (seen.has(source)) return "";
  seen.add(source);
  let css;
  if (/^https?:/.test(source)) {
    const response = await request.get(source, { timeout: 30000 });
    if (!response.ok()) throw Error(`stylesheet returned HTTP ${response.status()}: ${source}`);
    css = await response.text();
  } else css = fs.readFileSync(source, "utf8");
  const imports = [];
  for (const match of css.matchAll(/@import\s+(?:url\(\s*)?["']?([^\s"');]+)/g)) imports.push(match[1]);
  for (const file of imports) {
    const next = /^https?:/.test(source) ? new URL(file, source).href : path.resolve(path.dirname(source), file);
    if (!skip.includes(basename(next))) css += "\n" + await readCss(next, request, seen, skip);
  }
  return css;
}

async function palette(page, css, profile) {
  return page.evaluate(({ css, profile }) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = `position:fixed;left:-100000px;width:${innerWidth}px;height:${innerHeight}px;visibility:hidden;pointer-events:none;border:0`;
    document.body.append(frame);
    const doc = frame.contentDocument;
    const style = doc.createElement("style");
    // Fonts are measured on the work, never downloaded for the token probe.
    style.textContent = css.replace(/@font-face\s*\{[^}]*\}/g, "").replace(/@import[^;]+;/g, "")
      + "\n*,*::before,*::after{transition:none!important;animation:none!important}";
    doc.head.append(style);
    const parsed = new frame.contentWindow.CSSStyleSheet();
    parsed.replaceSync(css.replace(/@import[^;]+;/g, ""));
    const declarations = [];
    const walk = rules => {
      for (const rule of rules) {
        if (rule.style && rule.type !== CSSRule.FONT_FACE_RULE)
          for (const prop of rule.style) declarations.push([prop, rule.style.getPropertyValue(prop)]);
        if (rule.cssRules) walk(rule.cssRules);
      }
    };
    walk(parsed.cssRules);
    const probe = doc.createElement("span");
    doc.body.append(probe);
    const sets = { colours: new Set(), sizes: new Set(), families: new Set(profile.families),
      weights: new Set(["400"]), radii: new Set(["0px"]), shadows: new Set(["none"]) };
    const tokens = [...new Set(declarations.filter(([p]) => p.startsWith("--")).map(([p]) => p))];
    const pixel = doc.createElement("canvas").getContext("2d", { willReadFrequently: true });
    const rgb = value => {
      const nums = value.match(/[\d.]+/g);
      if (nums && /^rgba?\(/.test(value)) return nums.slice(0, 3).map(Number).join(",");
      if (!CSS.supports("color", value)) return null;
      pixel.clearRect(0, 0, 1, 1);
      pixel.fillStyle = value.replace(/\s*\/\s*[\d.]+%?\s*\)$/, ")");
      pixel.fillRect(0, 0, 1, 1);
      return [...pixel.getImageData(0, 0, 1, 1).data].slice(0, 3).join(",");
    };
    for (const theme of ["", "light", "dark"]) {
      doc.documentElement.setAttribute("data-theme", theme);
      for (const [prop, value] of declarations) {
        const raw = prop.startsWith("--");
        let key, target;
        if ((raw && CSS.supports("color", value)) || /^(color|background-color|border.*-color|fill|stroke)$/.test(prop)) {
          key = "colours"; target = "color";
        } else if (prop === "font-size" || (raw && /font-size|^--fs-/.test(prop))) { key = "sizes"; target = "font-size"; }
        else if (prop === "font-family" || (raw && /font-(ui|mono|display|family)|^--ff-/.test(prop))) { key = "families"; target = "font-family"; }
        else if (prop === "font-weight" || (raw && /font-weight|^--fw-/.test(prop))) { key = "weights"; target = "font-weight"; }
        else if (/^border-.*radius$/.test(prop) || (raw && /radius|^--r-/.test(prop))) { key = "radii"; target = "border-radius"; }
        else if (prop === "box-shadow" || (raw && /shadow/.test(prop))) { key = "shadows"; target = "box-shadow"; }
        else continue;
        probe.style.cssText = `${target}:${value}`;
        if (!probe.style.getPropertyValue(target)) continue;
        const computed = frame.contentWindow.getComputedStyle(probe).getPropertyValue(target);
        if (key === "colours") {
          const c = rgb(computed);
          if (c) sets.colours.add(c);
        } else if (key === "families") {
          const family = computed.split(",")[0].trim().replace(/["']/g, "");
          if (!(profile.fallbacks || []).includes(family)) sets.families.add(family);
        } else sets[key].add(computed);
      }
    }
    // Include literals inside gradients, shadows and shorthand declarations too.
    for (const [, value] of declarations) {
      for (const literal of value.match(/#[\da-f]{3,8}\b|(?:rgb|hsl|oklch|oklab|lab|lch|color)\([^()]*\)|\b[a-z]+\b/gi) || []) {
        if (!CSS.supports("color", literal) || /^(inherit|initial|unset|currentcolor)$/i.test(literal)) continue;
        probe.style.cssText = `color:${literal}`;
        const c = rgb(frame.contentWindow.getComputedStyle(probe).color);
        if (c) sets.colours.add(c);
      }
    }
    frame.remove();
    return { ...Object.fromEntries(Object.entries(sets).map(([key, set]) => [key, [...set]])), tokens };
  }, { css, profile });
}

function analyse({ allowed, profile, missing, sheets }) {
  const findings = [], counts = {}, notes = [];
  const styles = new Map();
  const cs = e => { if (!styles.has(e)) styles.set(e, getComputedStyle(e)); return styles.get(e); };
  const sample = e => (e.innerText || e.value || e.getAttribute("aria-label") || e.getAttribute("placeholder") || "").trim().replace(/\s+/g, " ").slice(0, 70);
  const selector = e => e.tagName.toLowerCase() + (e.id ? `#${CSS.escape(e.id)}` : [...e.classList].slice(0, 3).map(c => `.${CSS.escape(c)}`).join(""));
  const rect = e => { const r = e.getBoundingClientRect(); return [Math.round(r.x + scrollX), Math.round(r.y + scrollY), Math.round(r.width), Math.round(r.height)]; };
  const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 &&
    e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && !e.closest("[inert]");
  const elements = [document.body, ...document.body.querySelectorAll("*")].filter(visible);
  const add = (check, severity, e, detail, limit = 5) => {
    counts[check] = (counts[check] || 0) + 1;
    if (counts[check] <= limit) findings.push({ check, severity, selector: selector(e), sample: sample(e), detail, rect: rect(e) });
  };
  if (missing) add("missing-system", "block", document.body, "loads no house design system; the remaining checks use gdesign");
  const rules = [];
  const walk = (list, house) => {
    for (const rule of list) {
      if (rule.type === CSSRule.MEDIA_RULE && !matchMedia(rule.conditionText).matches) continue;
      if (rule.type === CSSRule.SUPPORTS_RULE && !CSS.supports(rule.conditionText)) continue;
      if (rule.type === CSSRule.IMPORT_RULE) {
        try { walk(rule.styleSheet.cssRules, house || profile.sheetNames.includes(rule.href.split(/[?#]/)[0].split("/").pop())); } catch {}
      }
      else {
        if (rule.selectorText && rule.style) rules.push({ selector: rule.selectorText, house,
          visual: [...rule.style].some(p => /^(background|border|padding|font|box-shadow)/.test(p)) });
        if (rule.cssRules) walk(rule.cssRules, house);
      }
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules, sheets.some(s => s.house && s.href === sheet.href)); }
    catch { notes.push(`could not read selectors in ${sheet.href}; component reuse needs review`); }
  }
  const matches = (e, rule) => { try { return e.matches(rule.selector); } catch { return false; } };
  const houseRules = rules.filter(r => r.house && r.visual && !/^(\*|:root|html|body)(\s*,|$)/.test(r.selector));
  const consumerRules = rules.filter(r => !r.house && r.visual);
  const text = elements.filter(e => [...e.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim()) ||
    e.matches('textarea,select,input:not([type]),input[type=text],input[type=search],input[type=email],input[type=url],input[type=tel],input[type=number],input[type=password],input[type=button],input[type=submit],input[type=reset]'));
  const loaded = [...document.fonts].filter(f => f.status === "loaded").map(f => f.family.replace(/["']/g, "").toLowerCase());
  const canvas = document.createElement("canvas").getContext("2d");
  const fontAvailable = family => {
    if (/^(serif|sans-serif|monospace|system-ui|ui-serif|ui-sans-serif|ui-monospace|ui-rounded|cursive|fantasy|math)$/i.test(family)) return true;
    if (loaded.includes(family.toLowerCase())) return true;
    const words = "mmmmmmmmmmlliWWWW0123456789";
    return ["monospace", "serif", "sans-serif"].some(base => {
      canvas.font = `72px ${base}`; const width = canvas.measureText(words).width;
      canvas.font = `72px "${family}", ${base}`;
      return Math.abs(width - canvas.measureText(words).width) > 0.01;
    });
  };
  const availability = new Map();
  const fonts = new Set();
  for (const e of text) {
    if (e.closest("svg,math") || e.matches("i[class*=ph], [class*=icon]") && !e.textContent.trim()) continue;
    const style = cs(e);
    const families = style.fontFamily.split(",").map(f => f.trim().replace(/["']/g, ""));
    const first = families.find(f => {
      if (!availability.has(f)) availability.set(f, fontAvailable(f));
      return availability.get(f);
    }) || families.at(-1);
    const mono = e.closest("pre,code,kbd,samp") && profile.mono?.length;
    const allowedFamily = allowed.families.some(f => f.toLowerCase() === first.toLowerCase());
    if (allowed.families.length && !allowedFamily && !(mono && /mono|courier/i.test(first))) {
      const key = `${style.fontFamily}|${first}`;
      if (!fonts.has(key)) {
        add("foreign-font", "block", e, `text uses ${first}; computed stack ${style.fontFamily}; house families: ${allowed.families.join(", ")}`);
        fonts.add(key);
      }
    }
    const sized = px => allowed.sizes.some(v => Math.abs(parseFloat(v) - px) < 0.2);
    // inline code set in em against an allowed size is the system's own relative mono sizing
    const relative = e.matches("code,kbd,samp") && e.parentElement && sized(parseFloat(cs(e.parentElement).fontSize));
    if (!relative && !sized(parseFloat(style.fontSize)))
      add("font-size", "review", e, `${style.fontSize} is outside the system's font sizes`);
    if (!allowed.weights.includes(style.fontWeight) || profile.maxWeight && +style.fontWeight > profile.maxWeight)
      add("font-weight", "review", e, `weight ${style.fontWeight} is outside the system's UI weights`);
    const own = [...e.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(" ").trim();
    if (/Traceback(?: \(most recent call last\))?|\[object Object\]|Internal Server Error|\bat\s+[\w.$<>]+\s*\([^\n]+:\d+(?::\d+)?\)|\b(?:[A-Z][\w]*(?:Error|Exception)|Exception)\b/.test(own))
      add("raw-failure", "block", e, `raw failure text visible to the reader: ${own.slice(0, 110)}`);
  }
  for (const e of elements.filter(e => e.matches("td,th,label,button,dt,dd,[role=cell],[role=gridcell],[role=status],.label"))) {
    if (/^(undefined|NaN|null)$/.test((e.innerText || "").trim())) add("raw-failure", "block", e, `raw failure text visible to the reader: ${e.innerText.trim()}`);
  }
  const palette = allowed.colours.map(c => c.split(",").map(Number));
  let coloured = 0, off = 0;
  const pixel = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const colours = new Map();
  const colour = value => {
    if (colours.has(value)) return colours.get(value);
    const nums = value.match(/[\d.]+/g)?.map(Number);
    let rgb = null;
    if (/^rgba?\(/.test(value) && nums) rgb = nums.length < 4 || nums[3] > 0 ? nums.slice(0, 3) : null;
    else if (CSS.supports("color", value) && value !== "transparent") {
      pixel.clearRect(0, 0, 1, 1);
      pixel.fillStyle = value;
      pixel.fillRect(0, 0, 1, 1);
      if (pixel.getImageData(0, 0, 1, 1).data[3] > 0) {
        pixel.fillStyle = value.replace(/\s*\/\s*[\d.]+%?\s*\)$/, ")");
        pixel.fillRect(0, 0, 1, 1);
        rgb = [...pixel.getImageData(0, 0, 1, 1).data].slice(0, 3);
      }
    }
    colours.set(value, rgb);
    return rgb;
  };
  for (const e of elements) {
    const s = cs(e);
    const values = [["text", text.includes(e) ? s.color : "transparent"], ["background", s.backgroundColor],
      ...["Top", "Right", "Bottom", "Left"].filter(side => parseFloat(s[`border${side}Width`]) > 0).map(side => ["border", s[`border${side}Color`]])];
    const used = values.map(([kind, value]) => [kind, value, colour(value)]).filter(([, , c]) => c);
    if (used.length) coloured++;
    const wrong = used.filter(([, , c]) => !palette.some(p => p.every((v, i) => Math.abs(v - c[i]) <= 3)));
    if (wrong.length) { off++; add("off-palette", "review", e, wrong.map(([k, v]) => `${k} ${v}`).join(", ") + " outside the system palette"); }
    if (s.borderRadius !== "0px" && !allowed.radii.includes(s.borderRadius)) add("border-radius", "review", e, `${s.borderRadius} is outside the system's radii`);
    if (s.boxShadow !== "none" && !allowed.shadows.includes(s.boxShadow)) add("box-shadow", "review", e, `${s.boxShadow} is outside the system's shadows`);
    if (/gradient\(/.test(s.backgroundImage) && (s.backgroundClip === "text" || s.webkitBackgroundClip === "text"))
      add("gradient-text", "block", e, "gradient text is an unambiguous AI tell");
    let kind = e.matches("button,[role=button]") ? "button" : e.matches("[role=tab]") ? "tab" : e.matches("input,select,textarea,table,dialog") ? e.tagName.toLowerCase() : null;
    if (!kind && e.matches("a") && (parseFloat(s.paddingLeft) > 0 && (colour(s.backgroundColor) || parseFloat(s.borderTopWidth) > 0))) kind = "button";
    if (!kind && parseFloat(s.paddingTop) > 0 && (parseFloat(s.borderTopWidth) > 0 || s.boxShadow !== "none") && e.querySelector("h1,h2,h3,h4,[role=heading],.card-title")) kind = "card";
    if (kind && !houseRules.some(r => matches(e, r)) &&
        (consumerRules.some(r => matches(e, r)) || /background|border|padding|font|box-shadow/.test(e.getAttribute("style") || "")))
      add(`local-${kind}`, "review", e, `styled locally instead of using the house ${kind}`);
  }
  if (off) notes.push(`off-palette: ${off} of ${coloured} coloured elements (${Math.round(off / coloured * 100)}%)`);
  const firstTwo = text.filter(e => rect(e)[1] < innerHeight * 2 && !e.closest("nav,footer,[role=navigation],[role=contentinfo]"));
  const seen = new Map();
  for (const e of firstTwo) {
    if ([...e.children].some(c => text.includes(c))) continue;
    const t = (e.innerText || "").trim().replace(/\s+/g, " ");
    if (t.split(/\s+/).length < 4) continue;
    if (seen.has(t)) add("mirrored", "review", e, `mirrored text also appears in ${selector(seen.get(t))}`);
    else seen.set(t, e);
  }
  const app = elements.some(e => e.matches("button,input,select,textarea,table,[role=application]"));
  if (app) for (const e of text) {
    if (e.closest(".prose,article,main.prose,pre,code") || [...e.children].some(c => text.includes(c))) continue;
    if ((e.innerText || "").trim().split(/\s+/).length > 80) add("wall-of-text", "review", e, "wall of text, could be a table, list or details");
  }
  const first = elements.filter(e => rect(e)[1] < innerHeight);
  if (!first.some(e => e.matches("h1,[role=heading]") || e.matches("h2,h3,[class*=title],[class*=heading]") && parseFloat(cs(e).fontSize) >= 24))
    add("missing-heading", "review", document.body, "the first screen has no h1 or title-sized heading");
  const filled = new Map();
  const rootStyle = getComputedStyle(document.documentElement);
  const accents = ["--accent-fill", "--contrast-fill", "--c-accent"].map(t => colour(rootStyle.getPropertyValue(t).trim())).filter(Boolean);
  for (const e of first.filter(e => e.matches("button,a,[role=button]"))) {
    const bg = colour(cs(e).backgroundColor), fg = colour(cs(e).color);
    if (!bg || !fg || Math.max(...bg.map((v, i) => Math.abs(v - fg[i]))) < 70) continue;
    if (e.matches(".btn-secondary,.btn-tertiary,[disabled]") || /secondary|tertiary|ghost/.test(e.className)) continue;
    if (!/primary|btn-go/.test(e.className) && !accents.some(c => c.every((v, i) => Math.abs(v - bg[i]) <= 3))) continue;
    const key = bg.join(",");
    const group = filled.get(key) || []; group.push(e); filled.set(key, group);
  }
  for (const group of filled.values()) if (group.length > 1)
    add("competing-primary", "review", group[0], `${group.length} filled primary-looking buttons share the same accent background in the first screen`);
  for (const e of elements.filter(e => e.matches("[role=log],[role=alert],.errors,.error-list,.logs,[class*=error-list],[class*=log-list]"))) {
    const collapse = e.closest("details") || e.querySelector("summary,[aria-expanded],[aria-controls]") ||
      e.id && document.querySelector(`[aria-controls="${CSS.escape(e.id)}"]`);
    if (rect(e)[3] > innerHeight && !collapse) add("errors-fill-screen", "review", e, "errors take out the page; the list is taller than one screen with no collapse");
  }
  return { findings, counts, notes, pageHeight: document.documentElement.scrollHeight };
}

async function lint(sources, allowed, page, entry, prefix) {
  if (!postcss) {
    for (const [i, source] of sources.entries()) fs.writeFileSync(path.join(outDir, `${prefix}-consumer-${i}.css`), source.css);
    entry.notes.push("source lint did not run: g-design's Stylelint CSS parser is not installed");
    return;
  }
  const definitions = new Set(allowed.tokens);
  const parsed = [];
  for (let i = 0; i < sources.length; i++) {
    const source = sources[i];
    const file = path.join(outDir, `${prefix}-consumer-${i}.css`);
    fs.writeFileSync(file, source.css);
    let root;
    try { root = postcss.parse(source.css, { from: file }); }
    catch (error) { entry.notes.push(`source lint did not run for ${file}: ${String(error).slice(0, 200)}`); continue; }
    root.walkDecls(d => { if (d.prop.startsWith("--")) definitions.add(d.prop); });
    parsed.push({ root, file, source });
  }
  const add = async (check, selector, detail, file, line, limit = 10) => {
    entry.counts[check] = (entry.counts[check] || 0) + 1;
    if (entry.counts[check] > limit) return;
    const location = await page.evaluate(selector => {
      let el;
      try { el = document.querySelector(selector); } catch {}
      if (!el) el = document.body;
      const r = el.getBoundingClientRect();
      return { selector: el.tagName.toLowerCase() + (el.id ? `#${CSS.escape(el.id)}` : [...el.classList].slice(0, 2).map(c => `.${CSS.escape(c)}`).join("")),
        sample: (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "").trim().replace(/\s+/g, " ").slice(0, 60),
        rect: [Math.round(r.x + scrollX), Math.round(r.y + scrollY), Math.round(r.width), Math.round(r.height)] };
    }, selector);
    entry.findings.push({ check, severity: "review", ...location, detail: `${file}:${line}: ${detail}`, file, line });
  };
  for (const { root, file } of parsed) {
    const unknown = [];
    root.walkDecls(d => {
      for (const match of d.value.matchAll(/var\(\s*(--[\w-]+)/g)) if (!definitions.has(match[1])) unknown.push([d, match[1]]);
    });
    for (const [d, name] of unknown) await add("invented-token", d.parent.selector || "body", `invented token ${name}, defined by neither system nor consumer`, file, d.source.start.line, 5);
  }
  if (!config.plugin) { entry.notes.push(`source lint did not run: ${config.lintWhy}`); return; }
  try {
    const stylelint = require("stylelint");
    const counts = {};
    for (const { file, source, root } of parsed) {
      const result = await stylelint.lint({ code: source.css, codeFilename: file,
        config: { plugins: [require.resolve(config.plugin)], rules: { "scale-unlimited/declaration-strict-value": [properties,
          { ignoreVariables: true, ignoreFunctions: false, ignoreValues: ["inherit", "currentColor", "transparent", "none", "0"],
            disableFix: true, message: "raw ${property}: ${value}" }] } } });
      for (const warning of result.results[0].warnings) {
        if (warning.rule !== "scale-unlimited/declaration-strict-value") throw Error(warning.text);
        const prop = warning.text.match(/^raw ([\w-]+):/)?.[1] || "unknown";
        counts[prop] = (counts[prop] || 0) + 1;
        let selector = "body";
        root.walkDecls(d => { if (d.source.start.line === warning.line && d.source.start.column === warning.column) selector = d.parent.selector || "body"; });
        await add("raw-css-value", selector, `${prop}: raw value where the system has a token`, file, warning.line);
      }
    }
    if (Object.keys(counts).length) entry.notes.push("raw CSS values by property: " + Object.entries(counts).map(([p, n]) => `${p} ${n}`).join(", "));
  } catch (error) { entry.notes.push(`source lint did not run: ${String(error).slice(0, 220)}`); }
}

async function screenshots(page, vp, folder, slug, findings = []) {
  fs.mkdirSync(folder, { recursive: true });
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  const last = Math.max(0, height - vp.height);
  const positions = new Set([0, last]);
  for (const finding of findings) {
    if (finding.severity === "block" && finding.rect && positions.size < 10)
      positions.add(Math.max(0, Math.min(last, Math.floor(finding.rect[1] / vp.height) * vp.height)));
  }
  for (let y = 0; y < height && positions.size < 10; y += vp.height) positions.add(Math.min(y, last));
  const shots = [], tiles = [];
  for (const [i, y] of [...positions].sort((a, b) => a - b).entries()) {
    await page.evaluate(y => scrollTo({ top: y, behavior: "instant" }), y);
    await page.waitForTimeout(100);
    const file = path.join(folder, `${slug}-${i}.png`);
    await page.screenshot({ path: file, animations: "disabled" });
    shots.push(file);
    tiles.push({ path: file, y });
  }
  await page.evaluate(() => scrollTo({ top: 0, behavior: "instant" }));
  return { screenshots: shots, tiles, ...(height > 10 * vp.height ? { notShown: height - 10 * vp.height } : {}) };
}

const browser = await chromium.launch();
const results = [], cssCache = new Map(), referenceCache = new Map();
let impeccableNoted = false;
try {
  for (const url of config.urls) for (const vp of viewports) {
    const entry = { url, viewport: `${vp.name} ${vp.width}x${vp.height}`, vp: vp.name, findings: [], counts: {}, notes: [], screenshots: [] };
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 1,
      reducedMotion: "reduce", isMobile: false, hasTouch: vp.width < 720, serviceWorkers: "block" });
    const page = await context.newPage();
    try {
      const response = await page.goto(url, { waitUntil: "load", timeout: 30000 });
      entry.status = response?.status();
      await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
      await page.evaluate(() => document.fonts.ready);
      const height = await page.evaluate(() => document.documentElement.scrollHeight);
      for (let y = vp.height; y < Math.min(height, 10 * vp.height); y += vp.height) {
        await page.evaluate(y => scrollTo(0, y), y);
        await page.waitForTimeout(150);
      }
      await page.evaluate(() => scrollTo(0, 0));
      await page.waitForTimeout(300);
      const detected = await page.evaluate(profiles => {
        const sheets = [...document.styleSheets].map(s => s.href).filter(Boolean);
        const root = getComputedStyle(document.documentElement);
        return Object.keys(profiles).filter(name => name !== "custom" &&
          (sheets.some(h => profiles[name].recogniseSheets.includes(h.split(/[?#]/)[0].split("/").pop())) ||
            profiles[name].tokens.every(t => root.getPropertyValue(t).trim())));
      }, config.profiles);
      entry.system = config.system === "auto" ? detected[0] || "gdesign" : config.system;
      const profile = config.profiles[entry.system];
      if (!cssCache.has(entry.system)) {
        let css = "";
        for (const source of profile.stylesheets) css += await readCss(source, context.request) + "\n";
        cssCache.set(entry.system, css);
      }
      const allowed = await palette(page, cssCache.get(entry.system), profile);
      if (!allowed.families.length) entry.notes.push("the system stylesheet defines no font family; font membership needs review");
      const sheetList = await page.evaluate(() => [...document.styleSheets].map(s => ({ href: s.href, css: s.href ? null : s.ownerNode?.textContent || "" })));
      const sheets = [];
      for (const sheet of sheetList) {
        sheet.house = Boolean(sheet.href && profile.sheetNames.includes(basename(sheet.href)));
        if (sheet.href) {
          try { sheet.css = await readCss(sheet.href, context.request, new Set(), sheet.house ? [] : profile.sheetNames); }
          catch (error) { entry.notes.push(`consumer stylesheet could not be read: ${sheet.href}: ${String(error).slice(0, 150)}`); }
        }
        sheets.push(sheet);
      }
      const measured = await page.evaluate(analyse, { allowed, profile, missing: config.system === "auto" && !detected.length, sheets });
      measured.notes.unshift(...entry.notes);
      Object.assign(entry, measured);
      await lint(sheets.filter(s => !s.house && s.css), allowed, page, entry, `${results.length}-${vp.name}`);
      const external = config.impeccable.find(e => e.url === url && e.size === `${vp.width}x${vp.height}`);
      if (external?.why && !impeccableNoted) {
        entry.notes.push(`Impeccable did not run: ${external.why}`);
        impeccableNoted = true;
      }
      entry.impeccable = [];
      for (const finding of external?.findings || []) {
        const location = await page.evaluate(({ f, sheets }) => {
          const snippet = String(f.snippet || "");
          let element;
          try { element = document.querySelector(f.selector || snippet.match(/^([^{}]+)\s*\{/)?.[1] || "__missing__"); } catch {}
          const visible = e => e.getBoundingClientRect().height > 0 && e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
          if (element && !visible(element)) element = null;
          if (f.antipattern === "bounce-easing") {
            const bounces = e => {
              if (!visible(e)) return false;
              const s = getComputedStyle(e);
              return [...`${s.animationTimingFunction},${s.transitionTimingFunction}`.matchAll(/cubic-bezier\(([^)]+)\)/g)].some(m => {
                const n = m[1].split(",").map(Number); return n[1] < 0 || n[1] > 1 || n[3] < 0 || n[3] > 1;
              });
            };
            if (!element || !bounces(element)) element = [...document.querySelectorAll("body *")].find(bounces);
          }
          const sourceOnly = !element;
          if (!element) element = [...document.querySelectorAll("body *")].find(e => e.textContent.trim() && snippet.includes(e.textContent.trim()) && e.getBoundingClientRect().height > 0);
          element ||= document.body;
          const property = {
            "side-tab": /^border-(?:left|right|top|bottom)-(?:width|style|color)$/,
            "bounce-easing": /^(?:animation|transition)-(?:timing-function|name)$/,
            "gradient-text": /^(?:background-image|(?:-webkit-)?background-clip)$/,
            "dark-glow": /^(?:box-shadow|text-shadow)$/,
            "radial-spotlight-glow": /^background-image$/,
            "ai-color-palette": /^(?:color|background-color|background-image)$/,
          }[f.antipattern];
          let house = false, consumer = false;
          if (property) {
            const walk = (rules, owned) => {
              for (const rule of rules) {
                if (rule.type === CSSRule.MEDIA_RULE && !matchMedia(rule.conditionText).matches) continue;
                if (rule.type === CSSRule.SUPPORTS_RULE && !CSS.supports(rule.conditionText)) continue;
                if (rule.selectorText && rule.style) {
                  let match = false;
                  try { match = element.matches(rule.selectorText); } catch {}
                  if (match && [...rule.style].some(p => property.test(p))) {
                    if (owned) house = true; else consumer = true;
                  }
                }
                if (rule.cssRules) walk(rule.cssRules, owned);
              }
            };
            for (const sheet of sheets.filter(s => s.css)) {
              const parsed = new CSSStyleSheet();
              parsed.replaceSync(sheet.css.replace(/@import[^;]+;/g, ""));
              walk(parsed.cssRules, sheet.house);
            }
            if (element.style && [...element.style].some(p => property.test(p))) consumer = true;
          }
          const r = element.getBoundingClientRect();
          return { selector: element.tagName.toLowerCase() + (element.id ? `#${CSS.escape(element.id)}` : [...element.classList].slice(0, 2).map(c => `.${CSS.escape(c)}`).join("")),
            sample: (element.innerText || element.value || element.getAttribute("aria-label") || element.getAttribute("placeholder") || element.getAttribute("alt") || element.closest("[title]")?.getAttribute("title") || "").trim().replace(/\s+/g, " ").slice(0, 60),
            houseStyle: house && !consumer, sourceOnly,
            decorativeMotion: f.antipattern === "bounce-easing" && element.matches('img[alt=""],img[aria-hidden=true],svg[aria-hidden=true]') && !element.closest("button,a,[role=button]"),
            rect: [Math.round(r.x + scrollX), Math.round(r.y + scrollY), Math.round(r.width), Math.round(r.height)] };
        }, { f: finding, sheets });
        const context = location.houseStyle ? "; this visual rule comes from the house stylesheet" : location.decorativeMotion ? "; decorative illustration motion needs judgement" : location.sourceOnly ? "; source-only lead, no matching rendered fault located" : "";
        entry.impeccable.push({ ...finding, ...location, check: `impeccable:${finding.antipattern}`, detail: `${finding.name}: ${finding.snippet || ""}${context}` });
      }
      Object.assign(entry, await screenshots(page, vp, outDir, `${results.length}-${vp.name}`, entry.findings));
      if (config.refs) {
        const key = `${entry.system}-${vp.name}`;
        if (!referenceCache.has(key)) {
          const refs = [];
          for (const [i, ref] of (config.refs[entry.system] || []).entries()) {
            const r = { url: ref, viewport: entry.viewport };
            try {
              await page.goto(ref, { waitUntil: "load", timeout: 30000 });
              await page.evaluate(() => document.fonts.ready);
              Object.assign(r, await screenshots(page, vp, path.join(outDir, "refs"), `${key}-${i}`));
            } catch (error) { r.error = String(error).slice(0, 250); }
            refs.push(r);
          }
          referenceCache.set(key, refs);
        }
        entry.references = referenceCache.get(key);
      }
    } catch (error) { entry.error = String(error).slice(0, 400); }
    finally { await context.close(); }
    results.push(entry);
    fs.writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(results, null, 2));
  }
} finally { await browser.close(); }
