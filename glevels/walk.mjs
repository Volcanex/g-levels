/* Shared browser engine for runtime-clean and option-tree checks.
   node walk.mjs --mode smoke|tree --out DIR [--journey FILE] [--safe]
                 [--max-states 40] [--max-actions 200] [--settle 1500] [--budget-s 480]
                 [--viewport 1280x800] URL [URL ...]
   Exit 0 writes walk.json and evidence; exit 2 prints impossible: reason.
   Findings are evidence, not a verdict: callers decide which ones block. */
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function timed(promise, ms, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    })]);
  } finally { clearTimeout(timer); }
}
const local = (url) => ["localhost", "127.0.0.1"].includes(new URL(url).hostname);
const origin = (url) => { try { return new URL(url).origin; } catch { return ""; } };
const risky = /delete|remove|destroy|cancel subscription|unsubscribe|pay|purchase|buy|checkout|send|invite|sign ?out|log ?out|logout|reset|archive|revoke|disconnect/i;

function fault(error, errors) {
  const { kind, sameOrigin, status, resourceType } = error;
  if (["action", "journey", "dead", "errortext", "blank", "freeze"].includes(kind)) return true;
  if (!sameOrigin) return false;
  if (["pageerror", "requestfailed"].includes(kind)) return true;
  if (kind === "http") return status >= 500 || status === 404 && ["script", "stylesheet", "fetch", "xhr", "document"].includes(resourceType);
  if (kind !== "console" || error.level !== "error") return false;
  return !(/Failed to load resource/.test(error.message) && errors.some((e) => e.kind === "http" &&
    e.url === error.url && e.status >= 400 && e.status < 500 && ["image", "font", "other"].includes(e.resourceType)));
}

function options() {
  const o = { mode: "smoke", out: null, journey: null, maxStates: 40, maxActions: 200,
    settle: 1500, budgetS: 480, safe: false, viewport: { width: 1280, height: 800 }, urls: [] };
  const names = { mode: "mode", out: "out", journey: "journey", "max-states": "maxStates",
    "max-actions": "maxActions", settle: "settle", viewport: "viewport", "budget-s": "budgetS" };
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--safe") { o.safe = true; continue; }
    if (!arg.startsWith("--")) { o.urls.push(arg); continue; }
    const key = names[arg.slice(2)];
    if (!key || !args[i + 1]) throw new Error(`unknown or incomplete option ${arg}`);
    o[key] = args[++i];
  }
  if (!o.out || !o.urls.length || !["smoke", "tree"].includes(o.mode))
    throw new Error("usage: node walk.mjs --mode smoke|tree --out DIR [options] URL [URL ...]");
  for (const key of ["maxStates", "maxActions", "settle", "budgetS"]) {
    o[key] = Number(o[key]);
    if (!Number.isInteger(o[key]) || o[key] < (key === "settle" ? 0 : 1))
      throw new Error(`invalid ${key}`);
  }
  if (typeof o.viewport === "string") {
    if (!/^\d+x\d+$/.test(o.viewport)) throw new Error("viewport must be WIDTHxHEIGHT");
    const [width, height] = o.viewport.split("x").map(Number);
    if (!width || !height) throw new Error("viewport must have positive dimensions");
    o.viewport = { width, height };
  }
  for (const url of o.urls) if (!/^https?:$/.test(new URL(url).protocol))
    throw new Error(`not an HTTP URL: ${url}`);
  o.out = path.resolve(o.out);
  return o;
}

function init() {
  window.__walk = { mutations: 0, longtasks: [], paints: 0 };
  new MutationObserver((rs) => { window.__walk.mutations += rs.length; })
    .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (e.duration > 1000) window.__walk.longtasks.push(e.duration);
    }).observe({ type: "longtask", buffered: true });
  } catch {}
  for (const name of ["CanvasRenderingContext2D", "WebGLRenderingContext", "WebGL2RenderingContext"]) {
    const proto = window[name]?.prototype;
    for (const method of ["fillRect", "clearRect", "drawImage", "putImageData", "stroke", "fill", "drawArrays", "drawElements", "clear"]) {
      if (!proto?.[method]) continue;
      const original = proto[method];
      proto[method] = function (...args) { window.__walk.paints++; return original.apply(this, args); };
    }
  }
}

function inspect() {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    return [...el.getClientRects()].some((r) => r.width > 0 && r.height > 0);
  };
  const selector = (el) => {
    if (el.id && document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) return `#${CSS.escape(el.id)}`;
    const testid = el.getAttribute("data-testid");
    if (testid) {
      const s = `[data-testid=${JSON.stringify(testid)}]`;
      if (document.querySelectorAll(s).length === 1) return s;
    }
    const parts = [];
    for (let p = el; p && p.nodeType === 1; p = p.parentElement) {
      const tag = p.tagName.toLowerCase();
      const siblings = p.parentElement ? [...p.parentElement.children].filter((c) => c.tagName === p.tagName) : [p];
      parts.unshift(tag + (siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(p) + 1})` : ""));
    }
    return parts.join(" > ");
  };
  const label = (el) => clean((el.getAttribute("aria-labelledby") || "").split(/\s+/)
    .map((id) => document.getElementById(id)?.textContent || "").join(" ") || el.getAttribute("aria-label") ||
    [...(el.labels || [])].map((l) => l.innerText).join(" ") || el.getAttribute("alt") ||
    el.innerText || el.getAttribute("title") || (["submit", "button", "reset"].includes(el.type) ? el.value : "") ||
    el.getAttribute("placeholder") || el.name).slice(0, 80);
  const base = 'a[href],button,input,select,textarea,summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],[role="switch"],[role="radio"],[role="option"],[onclick]';
  const controls = [...document.querySelectorAll(`${base},[tabindex="0"]`)]
    .filter((el) => el.matches(base) || getComputedStyle(el).cursor === "pointer")
    .map((el, i) => {
      const tag = el.tagName.toLowerCase(), type = el.type || "";
      const r = el.getBoundingClientRect(), seen = visible(el);
      const role = el.getAttribute("role") || ({ a: "link", button: "button", summary: "button", select: "combobox",
        textarea: "textbox", input: ["checkbox", "radio"].includes(type) ? type :
          ["submit", "button", "reset", "image"].includes(type) ? "button" : "textbox" })[tag] || "button";
      return { ref: `c${i + 1}`, role, tag, type, name: label(el), href: el.href || null,
        disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true", visible: seen,
        inViewport: seen && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth,
        chrome: !!el.closest("nav, header, footer, aside, [role=navigation], [role=banner], [role=menu], dialog:not([open])"),
        checked: "checked" in el ? el.checked : el.hasAttribute("aria-checked") ? el.getAttribute("aria-checked") === "true" : null,
        options: tag === "select" ? [...el.options].map((p) => ({ label: p.label, value: p.value, selected: p.selected, disabled: p.disabled })) : [],
        selector: selector(el), form: el.form ? selector(el.form) : null };
    });
  const forms = [...document.forms].map((f) => ({ action: f.action, method: f.method, selector: selector(f),
    fields: [...f.elements].filter((el) => el.matches("input,select,textarea")).map((el) => ({
      name: el.name, type: el.type, label: label(el), required: el.required, value: el.value,
      checked: !!el.checked, selector: selector(el), disabled: !!el.disabled })) }));
  const texts = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || el.closest("script,style,noscript,template") || !clean(n.data)) continue;
    if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
    const r = document.createRange(); r.selectNode(n);
    if (![...r.getClientRects()].some((b) => b.width > 0 && b.height > 0)) continue;
    texts.push({ text: clean(n.data), selector: selector(el), code: !!el.closest("pre,code") });
  }
  const patterns = [/unexpected error/i, /something went wrong/i, /internal server error/i,
    /traceback \(most recent call last\)/i, /application error/i, /\[object object\]/i,
    /502 bad gateway/i, /503 service/i, /504 gateway/i];
  const errors = texts.filter((t) => patterns.some((re) => re.test(t.text)) ||
    (!t.code && /\b(undefined|NaN)\b/i.test(t.text))).map((t) => ({ kind: "errortext",
      message: `${t.selector}: ${t.text.slice(0, 180)}`, url: location.href, sameOrigin: true }));
  const media = [...document.querySelectorAll("canvas,img,svg")].filter(visible).some((el) => {
    if (el.tagName === "IMG") return el.complete && el.naturalWidth > 0;
    if (el.tagName.toLowerCase() === "svg") return !!el.querySelector("path,rect,circle,ellipse,line,polyline,polygon,text,image,use");
    try {
      const c = el.getContext("2d");
      return c ? c.getImageData(0, 0, el.width, el.height).data.some((v) => v !== 0) : window.__walk.paints > 0;
    } catch { return true; }
  });
  if (!texts.length && !media) errors.push({ kind: "blank", message: "blank: no visible text or non-empty canvas/img/svg",
    url: location.href, sameOrigin: true });
  return { title: document.title, controls, forms, errors,
    text: document.body?.innerText || "", headings: [...document.querySelectorAll("h1,h2")].filter(visible).map((el) => clean(el.innerText)),
    height: Math.max(innerHeight, document.documentElement.scrollHeight), mutations: window.__walk.mutations,
    longtasks: [...window.__walk.longtasks], paints: window.__walk.paints,
    canvas: [...document.querySelectorAll("canvas")].some(visible) };
}

async function session(browser, o, entry) {
  // A signed-in walk reads a Playwright storage state from a 0600 file, never from argv.
  const context = await browser.newContext({ viewport: o.viewport, deviceScaleFactor: 1, serviceWorkers: "block",
    ...(process.env.GLEVELS_STORAGE_STATE ? { storageState: process.env.GLEVELS_STORAGE_STATE } : {}) });
  context.setDefaultTimeout(5000);
  await context.addInitScript(init);
  const s = { context, page: null, entry, errors: [], requests: 0, dialogs: 0, blocked: [] };
  if (o.mode === "tree") {
    const origins = new Set(o.urls.map(origin));
    await context.route("**/*", (route, request) => {
      if (request.isNavigationRequest() && !origins.has(origin(request.url()))) {
        let main = true;
        try { main = request.frame() === request.frame().page().mainFrame(); } catch {}
        if (main) { s.blocked.push(request.url()); return route.abort("aborted"); }
      }
      return route.continue();
    });
  }
  const add = (kind, message, url, extra = {}) => s.errors.push({ kind, message, url,
    sameOrigin: origin(url || entry) === origin(/^https?:/.test(s.page?.url() || "") ? s.page.url() : s.entry), ...extra });
  context.on("console", (m) => {
    if (["error", "warning"].includes(m.type())) add("console", m.text(), m.location().url || m.page()?.url() || entry,
      { level: m.type(), location: m.location() });
  });
  context.on("weberror", (event) => {
    const e = event.error();
    const source = event.location().url || event.page()?.url() || entry;
    add("pageerror", String(e.stack || e).split("\n")[0], source);
  });
  context.on("request", () => { s.requests++; });
  context.on("response", (r) => {
    if (r.status() >= 400) add("http", `HTTP ${r.status()} ${r.url()}`, r.url(),
      { status: r.status(), resourceType: r.request().resourceType() });
  });
  context.on("requestfailed", (r) => {
    const message = r.failure()?.errorText || "request failed";
    if (r.isNavigationRequest() && /aborted|cancelled|canceled|ERR_ABORTED/i.test(message)) return;
    add("requestfailed", message, r.url(), { resourceType: r.resourceType() });
  });
  context.on("dialog", (d) => {
    s.dialogs++; add("dialog", d.message(), d.page()?.url() || entry);
    d.dismiss().catch(() => {});
  });
  s.page = await context.newPage();
  return s;
}

async function snapshot(s, o, id, depth, via, start) {
  const page = s.page;
  // Flutter web draws to a canvas and builds its accessibility tree only after this placeholder
  // is activated; without it every control is invisible to the walk.
  const flutter = await timed(page.evaluate(() => {
    const p = document.querySelector("flt-semantics-placeholder");
    if (!p) return false;
    p.click(); return true;
  }), 5000, "page did not answer").catch(() => false);
  if (flutter) await sleep(800);
  let fps = null, data, frozen = false;
  let beforePaints = null;
  try {
    fps = await timed(page.evaluate(() => new Promise((r) => {
      const paints = window.__walk?.paints || 0;
      let n = 0; const t = performance.now();
      function f() { n++; if (performance.now() - t < 1000) requestAnimationFrame(f); else r({ n, paints }); }
      requestAnimationFrame(f);
    })), 5000, "freeze: main thread did not answer in 5s");
    beforePaints = fps.paints; fps = fps.n;
  } catch (e) {
    // A heavy load on a slow machine (software WebGL, a busy host) can hold the main thread for
    // 20 s and then run fine. Give it 15 s more: still silent is a freeze, answering is a slow load.
    const late = await timed(page.evaluate(() => true), 15000, "no answer").catch(() => false);
    if (late) s.errors.push({ kind: "slow", message: "main thread did not answer for 5-20 s, then recovered", url: page.url(), sameOrigin: true });
    else {
      frozen = true;
      s.errors.push({ kind: "freeze", message: "freeze: main thread did not answer in 20s", url: page.url(), sameOrigin: true });
    }
  }
  try { data = await timed(page.evaluate(inspect), 20000, "page inspection timed out after 20s"); }
  catch (e) {
    if (!frozen) s.errors.push({ kind: "freeze", message: `freeze: ${e.message}`, url: page.url(), sameOrigin: true });
    data = { title: "", controls: [], forms: [], errors: [], text: "", headings: [], height: o.viewport.height,
      mutations: 0, longtasks: [], canvas: false };
  }
  const sig = createHash("sha256").update(JSON.stringify([new URL(page.url() === "about:blank" ? s.entry : page.url()).pathname +
    new URL(page.url() === "about:blank" ? s.entry : page.url()).search,
    data.controls.filter((c) => c.visible).map((c) => c.name).sort(), data.headings])).digest("hex").slice(0, 20);
  const screenshot = `shots/${id}.png`, text = `text/${id}.txt`;
  let shot = screenshot;
  try {
    await page.screenshot({ path: path.join(o.out, screenshot), fullPage: true, timeout: 5000,
      clip: { x: 0, y: 0, width: o.viewport.width, height: Math.min(data.height, o.viewport.height * 3) } });
  } catch { shot = null; }
  fs.writeFileSync(path.join(o.out, text), data.text);
  return { id, url: page.url(), title: data.title, depth, via, sig, screenshot: shot, text,
    controls: data.controls, forms: data.forms, errors: [...s.errors.slice(start), ...data.errors], fps,
    longtasks: data.longtasks, canvas: data.canvas, animatingCanvas: data.canvas && beforePaints !== null && data.paints > beforePaints,
    mutations: data.mutations };
}

function dummy(field) {
  return ({ email: "reviewer@example.test", url: "https://example.test", tel: "01234567890", number: "1",
    date: "2026-01-01", time: "12:00", "datetime-local": "2026-01-01T12:00", month: "2026-01", week: "2026-W01",
    password: "Review-example-123" })[field.type] || "Review example";
}

async function perform(s, action) {
  const page = s.page;
  if (["goto", "link"].includes(action.kind)) await page.goto(new URL(action.url, /^(https?|file):/.test(page.url()) ? page.url() : undefined).href, { waitUntil: "commit", timeout: 15000 });
  else if (action.kind === "click") await page.locator(action.selector).click({ timeout: 5000 });
  else if (action.kind === "fill") await page.locator(action.selector).fill(String(action.value));
  else if (action.kind === "select") await page.locator(action.selector).selectOption(action.value);
  else if (action.kind === "press") await page.keyboard.press(action.value);
  else if (action.kind === "wait") await sleep(action.value);
  else if (action.kind === "expect") {
    const seen = await page.locator(`text=${JSON.stringify(action.value)}`).filter({ visible: true }).first().isVisible();
    if (!seen) throw new Error(`expected visible text ${JSON.stringify(action.value)}`);
  } else if (action.kind === "submit") {
    for (const f of action.fields) {
      if (f.disabled || f.value || !["text", "search", "email", "url", "tel", "number", "date", "time",
        "datetime-local", "month", "week", "password", "textarea"].includes(f.type)) continue;
      await page.locator(f.selector).fill(dummy(f), { timeout: 5000 });
    }
    await page.locator(action.selector).evaluate((f) => f.requestSubmit());
  }
}

function candidates(state, o, origins) {
  const actions = [], skipped = [];
  const safe = o.safe || !local(state.url);
  const skip = (c, reason) => skipped.push({ state: state.id, ref: c.ref, name: c.name, reason });
  for (const c of state.controls) {
    if (!c.visible || c.disabled) { skip(c, c.disabled ? "disabled" : "not visible"); continue; }
    if (safe && risky.test(`${c.name} ${c.href || ""}`)) { skip(c, "safe: potentially destructive control"); continue; }
    if (c.href) {
      if (!/^https?:/i.test(c.href)) { skip(c, "non-HTTP link (mailto/tel/javascript)"); continue; }
      if (!origins.has(origin(c.href))) { skip(c, "outside entry origins"); continue; }
      actions.push({ kind: "link", ref: c.ref, name: c.name, selector: c.selector, url: c.href });
    } else if (c.form && ["submit", "image"].includes(c.type)) {
      if (safe) skip(c, "safe: form submission");
    } else if (c.tag === "select") {
      for (const p of c.options.filter((p) => !p.selected && !p.disabled).slice(0, 5))
        actions.push({ kind: "select", ref: c.ref, name: `${c.name}: ${p.label}`, selector: c.selector, value: p.value });
    } else if (["input", "textarea"].includes(c.tag) && !["checkbox", "radio", "button", "reset"].includes(c.type)) {
      skip(c, "text fields are filled only during form submission");
    } else actions.push({ kind: "click", ref: c.ref, name: c.name, selector: c.selector });
  }
  for (const f of state.forms) {
    const c = { ref: null, name: `form ${f.selector}` };
    if (safe || !local(state.url)) { skip(c, "safe: form submission"); continue; }
    if (!origins.has(origin(f.action))) { skip(c, "form outside entry origins"); continue; }
    actions.push({ kind: "submit", ref: null, name: c.name, selector: f.selector, fields: f.fields });
  }
  // The page itself before its navigation chrome, and both before links that leave the page,
  // so a budget is spent on what the entry offers rather than on the site menu.
  const here = new URL(state.url).pathname;
  const rank = (a) => {
    const c = state.controls.find((x) => x.ref === a.ref);
    if (a.kind === "link" && new URL(a.url).pathname !== here) return 2;
    return c?.chrome ? 1 : 0;
  };
  actions.sort((x, y) => rank(x) - rank(y));
  return { actions, skipped };
}

function journeyAction(step) {
  const kind = ["goto", "click", "fill", "select", "press", "wait", "expect"].find((k) => k in step);
  if (!kind) {
    if (typeof step.name === "string") return { kind: "wait", ref: null, name: step.name, selector: null, value: 0 };
    throw new Error("journey step needs goto/click/fill/select/press/wait/expect/name");
  }
  return { kind, ref: null, name: step.name || String(step[kind]),
    selector: ["click", "fill", "select"].includes(kind) ? step[kind] : null,
    url: kind === "goto" ? step.goto : undefined,
    value: ["fill", "select"].includes(kind) ? step.value : step[kind] };
}

async function walk(o) {
  const GDESIGN = process.env.GDESIGN || "/home/gabriel/g-design";
  const require = createRequire(path.join(GDESIGN, "package.json"));
  const { chromium } = require("playwright");
  const journey = o.journey ? JSON.parse(fs.readFileSync(o.journey, "utf8")) : [];
  if (!Array.isArray(journey)) throw new Error("journey must be a JSON list of steps");
  const steps = journey.map(journeyAction);
  const report = { mode: o.mode, entry: o.urls, viewport: o.viewport, started: new Date().toISOString(),
    states: [], edges: [], skipped: [], budget: { maxStates: o.maxStates, maxActions: o.maxActions, seconds: o.budgetS, states: 0, actions: 0, hit: false } };
  const deadline = Date.now() + o.budgetS * 1000;
  fs.mkdirSync(path.join(o.out, "shots"), { recursive: true });
  fs.mkdirSync(path.join(o.out, "text"), { recursive: true });
  const browser = await chromium.launch();
  const seen = new Map(), paths = new Map(), queue = [];
  let serial = 0;
  const room = () => {
    if (report.states.length >= o.maxStates || report.edges.length >= o.maxActions || Date.now() > deadline) {
      report.budget.hit = true; return false;
    }
    return true;
  };
  async function step(s, from, action, route, asserted = false) {
    const id = `e${report.edges.length + 1}`, start = s.errors.length;
    const requests = s.requests, dialogs = s.dialogs, blocked = s.blocked.length, t = Date.now(), url = s.page.url();
    if (from) await timed(s.page.evaluate(() => { window.__walk.mutations = 0; }), 5000, "page did not answer").catch(() => {});
    let failure = null, downloaded = false, unclickable = null;
    try { await perform(s, action); }
    catch (e) {
      const message = e.message.split("\n")[0];
      if (/Download is starting/.test(message)) downloaded = true;
      else if (!asserted && ["click", "submit"].includes(action.kind) && /Timeout \d+ms exceeded/.test(message)) unclickable = message;
      else if (s.blocked.length === blocked) {
        failure = e;
        s.errors.push({ kind: asserted ? "journey" : "action", message: `${action.name}: ${e.message.split("\n")[0]}`,
          url: s.page.url(), sameOrigin: true });
      }
    }
    await sleep(o.settle);
    const state = await snapshot(s, o, `s${++serial}`, from ? from.depth + 1 : 0, id, start);
    const errors = state.errors.filter((e) => !["errortext", "blank"].includes(e.kind) ||
      !from?.errors.some((old) => old.kind === e.kind && old.message === e.message));
    let outcome = failure || errors.some((e) => fault(e, errors)) ? "error" :
      s.page.url() !== url ? "navigate" : s.dialogs > dialogs ? "dialog" :
      state.mutations > 0 || (from && state.sig !== from.sig) ? "dom" : s.requests > requests ? "request" : "none";
    if (outcome !== "error" && downloaded) outcome = "download";
    if (outcome !== "error" && unclickable) outcome = "unclickable";
    if (s.blocked.length > blocked) {
      outcome = "skipped";
      report.skipped.push({ state: from?.id || state.id, ref: action.ref, name: action.name, reason: "outside entry origins" });
    }
    if (asserted && action.kind === "click" && outcome === "none") {
      errors.push({ kind: "dead", message: `${action.name}: dead control (no navigation, DOM change, request or dialog)`,
        url: state.url, sameOrigin: true });
      outcome = "error";
    }
    const key = `${origin(state.url)} ${state.sig}`;
    let target = o.mode === "tree" ? seen.get(key) : null;
    if (!target) {
      target = state; report.states.push(state); seen.set(key, state);
      paths.set(state.id, { entry: s.entry, actions: route }); queue.push(state);
    } else {
      for (const e of state.errors) if (!target.errors.some((old) => JSON.stringify(old) === JSON.stringify(e))) target.errors.push(e);
    }
    const edge = { id, from: from?.id || null, to: target.id, action, outcome, errors, ms: Date.now() - t };
    if (unclickable) edge.note = unclickable;
    report.edges.push(edge);
    return target;
  }
  try {
    let smoke = null, current = null;
    for (const url of o.urls) {
      if (!room()) break;
      const s = o.mode === "smoke" && smoke ? smoke : await session(browser, o, url);
      s.entry = url;
      const action = { kind: "goto", ref: null, name: url, selector: null, url };
      current = await step(s, null, action, []);
      if (o.mode === "smoke") smoke = s;
      else await s.context.close();
    }
    if (smoke) {
      for (const action of steps) {
        if (!room()) break;
        current = await step(smoke, current, action, [], true);
      }
      await smoke.context.close();
    }
    const origins = new Set(o.urls.map(origin));
    while (o.mode === "tree" && queue.length && room()) {
      const state = queue.shift(), route = paths.get(state.id);
      const next = candidates(state, o, origins);
      report.skipped.push(...next.skipped);
      for (const action of next.actions) {
        if (!room()) break;
        const s = await session(browser, o, route.entry);
        try {
          await perform(s, { kind: "goto", url: route.entry }); await sleep(o.settle);
          for (const a of route.actions) { await perform(s, a); await sleep(o.settle); }
          await step(s, state, action, [...route.actions, action]);
        } catch (e) {
          report.edges.push({ id: `e${report.edges.length + 1}`, from: state.id, to: null, action, outcome: "error",
            errors: [{ kind: "action", message: `restore ${state.url}: ${e.message.split("\n")[0]}`, url: state.url, sameOrigin: true }], ms: 0 });
        } finally { await s.context.close(); }
      }
    }
  } finally { await browser.close(); }
  report.budget.states = report.states.length; report.budget.actions = report.edges.length;
  fs.writeFileSync(path.join(o.out, "walk.json"), JSON.stringify(report, null, 2));
  console.log(path.join(o.out, "walk.json"));
}

try {
  const o = options();
  if (!process.env.GLEVELS_WALK_LOCKED) {
    const child = spawn("flock", [process.env.GSPACING_LOCK || "/tmp/glevels-spacing.lock", process.execPath, ...process.argv.slice(1)],
      { stdio: "inherit", env: { ...process.env, GLEVELS_WALK_LOCKED: "1" } });
    const code = await new Promise((resolve, reject) => { child.on("error", reject); child.on("exit", (code) => resolve(code ?? 2)); });
    process.exitCode = code;
  } else {
    const mb = Number(fs.readFileSync("/proc/meminfo", "utf8").match(/^MemAvailable:\s+(\d+)/m)?.[1] || 0) / 1024;
    const min = Number(process.env.GSPACING_MIN_FREE_MB || 600);
    if (mb < min) throw new Error(`only ${Math.floor(mb)} MB of memory free, a browser needs ${min}. Try again when the host is quieter`);
    await walk(o);
  }
} catch (e) {
  console.error(`impossible: ${e.message}`);
  process.exitCode = 2;
}
