/* Browser half of the correct.ui.spacing sensor. spacing.py is the entry point:
   it resolves the targets, serves g-site pages fresh, guards memory and formats
   the report. This file opens one Chromium, renders every target at every
   viewport, measures, and writes screenshots and findings.json to --out.

   Usage: node spacing.mjs --out DIR [--viewports phone=390x844,desktop=1440x900]
          [--landscape] [--states loaded,loading,error,long-text] [--steps N]
          [--step-key ArrowRight] [--storage-state FILE] [--header 'Name: value']
          [--wait-for SELECTOR] [--tiles 10] URL [URL ...]

   Defaults: phone-s 320x568, phone 390x844, tablet 768x1024, desktop 1440x900.
   Stress states run at 390px; only sideways scrolling and text overlap block
   there. Loaded screens also check fixed/sticky occlusion at both page ends.

   Each finding has a severity. "block" is a clear fault: text overlapping text,
   text touching or spilling out of its box, a box escaping the box it sits in,
   text cut off by a clipping box, the page scrolling sideways. "review" is a
   lead for the reviewer, who judges it against the screenshots: off-grid
   edges, uneven or off-scale gaps, near-misses in alignment, cramped controls,
   boxes touching, big empty bands, same-kind siblings at different sizes or
   with their titles in different places, layout shift, small tap targets.

   Grid and gap numbers come from g-design's rhythm-measure.mjs, the same code
   rhythm-audit.mjs runs, so the two never disagree. */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import fs from "node:fs";
import path from "node:path";

const GDESIGN = process.env.GDESIGN || "/home/gabriel/g-design";
const require = createRequire(path.join(GDESIGN, "package.json"));
const { chromium } = require("playwright");
const { measureRhythm } = await import(
  pathToFileURL(path.join(GDESIGN, "packages/g-ui/scripts/rhythm-measure.mjs")).href);

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i === -1 ? fallback : args[i + 1];
};
const flagged = new Set();
for (let i = 0; i < args.length; i++) {
  if (["--out", "--viewports", "--tiles", "--states", "--steps", "--step-key", "--storage-state", "--header", "--wait-for"].includes(args[i])) {
    flagged.add(i); flagged.add(++i);
  }
}
const urls = args.filter((a, i) => !flagged.has(i) && !a.startsWith("--"));
const outDir = flag("out", null);
if (!outDir || !urls.length) {
  console.error("usage: node spacing.mjs --out DIR [--viewports name=WIDTHxHEIGHT] [--states loaded,loading,error,long-text] URL [URL ...]");
  process.exit(2);
}
const viewports = String(flag("viewports", "phone-s=320x568,phone=390x844,tablet=768x1024,desktop=1440x900")).split(",").map((v) => {
  const [name, size] = v.split("=");
  const [width, height] = size.split("x").map(Number);
  return { name, width, height };
});
const maxTiles = Number(flag("tiles", 10));
if (args.includes("--landscape") && !viewports.some((v) => v.name === "phone-land")) viewports.push({ name: "phone-land", width: 844, height: 390 });
const states = [...new Set(flag("states", "loaded,loading,error,long-text").split(","))];
const steps = Number(flag("steps", 0));
const options = args.includes("--options-stdin") ? JSON.parse(fs.readFileSync(0, "utf8")) : {
  storageState: flag("storage-state", undefined) ? JSON.parse(fs.readFileSync(flag("storage-state"), "utf8")) : undefined,
  waitFor: flag("wait-for", undefined), headers: {},
};
for (let i = 0; i < args.length; i++) {
  if (args[i] !== "--header") continue;
  const header = args[++i], at = header.indexOf(":");
  if (at < 1) throw new Error("--header must use Name: value");
  options.headers[header.slice(0, at).trim()] = header.slice(at + 1).trim();
}
fs.mkdirSync(outDir, { recursive: true });

/* Runs inside the page. Must not close over anything. */
function analyse(o) {
  const CAP = o.cap;
  const out = [];
  const vw = o.vw || innerWidth;   // the screen asked for: a phone zooms out to hide overflow
  const vh = innerHeight;
  const counts = {};
  const doc = (r) => r && [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)];
  const add = (check, severity, el, detail, r) => {
    counts[check] = (counts[check] || 0) + 1;
    if (counts[check] > CAP) return;
    out.push({ check, severity, selector: el ? describe(el) : "", detail, rect: doc(r) });
  };
  const describe = (el) => {
    const cls = [...(el.classList || [])].filter((c) => c.length < 32).slice(0, 2).join(".");
    const s = el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (cls ? "." + cls : "");
    const t = (el.textContent || el.getAttribute("aria-label") || el.getAttribute("alt") || "")
      .trim().replace(/\s+/g, " ").slice(0, 40);
    return t ? `${s} "${t}"` : s;
  };
  const styles = new Map();
  const cs = (el) => { let s = styles.get(el); if (!s) { s = getComputedStyle(el); styles.set(el, s); } return s; };
  const rects = new Map();
  const rectOf = (el) => { let r = rects.get(el); if (!r) { r = el.getBoundingClientRect(); rects.set(el, r); } return r; };
  const px = (v) => parseFloat(v) || 0;
  const alpha = (c) => {
    if (!c || c === "transparent") return 0;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return 1;
    const parts = m[1].split(/[ ,/]+/).filter(Boolean);
    return parts.length > 3 ? parseFloat(parts[3]) : 1;
  };
  const MEDIA = new Set(["IMG", "svg", "CANVAS", "VIDEO", "IFRAME", "PICTURE"]);
  const CONTROL = new Set(["INPUT", "SELECT", "TEXTAREA", "BUTTON"]);
  const skipped = (el) => !!el.closest("[data-audit-skip], .sr-only, .visually-hidden, script, style, noscript, template");
  const visible = (el) => {
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const r = rectOf(el);
    return r.width >= 2 && r.height >= 2;
  };
  /* A fixed box that fills the screen is the page (a deck, a full-screen app), not a bar over it. */
  const screenLike = (el) => {
    const r = rectOf(el);
    return r.width >= 0.8 * innerWidth && r.height >= 0.8 * innerHeight;
  };
  const fixedMemo = new Map();
  const isFixed = (el) => {
    if (!el || el === document.body) return false;
    if (fixedMemo.has(el)) return fixedMemo.get(el);
    const p = cs(el).position;
    const v = (p === "fixed" && !screenLike(el)) || p === "sticky" || isFixed(el.parentElement);
    fixedMemo.set(el, v);
    return v;
  };
  const placed = (el) => {   // absolutely placed by its author, not by the flow
    const s = cs(el);
    return s.position === "absolute" || s.position === "fixed" || (s.transform && s.transform !== "none");
  };
  const placedMemo = new Map();
  const placedUp = (el) => {   // it or anything above it placed by hand: maps, stamps, overlays
    if (!el || el === document.body || el === document.documentElement) return false;
    if (placedMemo.has(el)) return placedMemo.get(el);
    const v = placed(el) || placedUp(el.parentElement);
    placedMemo.set(el, v);
    return v;
  };
  /* The background a box is drawn on, for telling a card from a bare div. */
  const bgMemo = new Map();
  const bgOf = (el) => {
    if (!el) return "rgb(255, 255, 255)";
    if (bgMemo.has(el)) return bgMemo.get(el);
    const s = cs(el);
    const v = alpha(s.backgroundColor) > 0.5 ? s.backgroundColor : bgOf(el.parentElement);
    bgMemo.set(el, v);
    return v;
  };
  const boxMemo = new Map();
  const isBox = (el) => {   // has an edge a reader can see
    if (boxMemo.has(el)) return boxMemo.get(el);
    let v = false;
    if (el.tagName === "svg" || MEDIA.has(el.tagName) || CONTROL.has(el.tagName)) v = true;
    else if (el instanceof HTMLElement) {
      const s = cs(el);
      if (s.display !== "inline" && s.display !== "contents") {
        const sides = ["Top", "Right", "Bottom", "Left"].filter((k) =>
          px(s[`border${k}Width`]) >= 1 && alpha(s[`border${k}Color`]) > 0.15 && s[`border${k}Style`] !== "none").length;
        const bg = alpha(s.backgroundColor) > 0.05 && s.backgroundColor !== bgOf(el.parentElement);
        const img = s.backgroundImage && s.backgroundImage !== "none";
        const shadow = s.boxShadow && s.boxShadow !== "none";
        v = sides >= 3 || bg || img || shadow;
      }
    }
    boxMemo.set(el, v);
    return v;
  };
  const boxAbove = (el, self = false) => {   // nearest ancestor (or itself) with a visible edge
    for (let p = self ? el : el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      if (isBox(p) && p.tagName !== "svg" && !MEDIA.has(p.tagName)) return p;
    }
    return null;
  };
  const clipAbove = (el, axis) => {   // nearest ancestor that clips or scrolls on an axis
    for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
      const s = cs(p);
      const ov = axis === "x" ? s.overflowX : s.overflowY;
      if (ov !== "visible") return { el: p, mode: ov };
    }
    return null;
  };
  const inside = (a, b, tol = 0.5) => a.left >= b.left - tol && a.right <= b.right + tol && a.top >= b.top - tol && a.bottom <= b.bottom + tol;
  const area = (r) => Math.max(0, r.width) * Math.max(0, r.height);
  const inter = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? w * h : 0;
  };
  /* The ink of a line of text, roughly: the content box less the font's own
     internal leading, so tight line-heights do not read as overlap. */
  const ink = (r) => {
    const cut = r.height * 0.18;
    return { left: r.left, right: r.right, top: r.top + cut, bottom: r.bottom - cut, width: r.width, height: r.height - 2 * cut };
  };

  /* What the ancestors leave visible of an element: the intersection of every
     box above it that clips or scrolls. Text outside it is not on screen. */
  const clipMemo = new Map();
  const clipOf = (el) => {
    if (!el || el === document.documentElement) return { left: -1e9, top: -1e9, right: 1e9, bottom: 1e9 };
    if (clipMemo.has(el)) return clipMemo.get(el);
    const up = clipOf(el.parentElement);
    const s = cs(el);
    let v = up;
    if (el !== document.body && (s.overflowX !== "visible" || s.overflowY !== "visible")) {
      const r = rectOf(el);
      v = {
        left: s.overflowX !== "visible" ? Math.max(up.left, r.left) : up.left,
        right: s.overflowX !== "visible" ? Math.min(up.right, r.right) : up.right,
        top: s.overflowY !== "visible" ? Math.max(up.top, r.top) : up.top,
        bottom: s.overflowY !== "visible" ? Math.min(up.bottom, r.bottom) : up.bottom,
      };
    }
    clipMemo.set(el, v);
    return v;
  };
  const clamp = (r, c) => {
    const left = Math.max(r.left, c.left), right = Math.min(r.right, c.right);
    const top = Math.max(r.top, c.top), bottom = Math.min(r.bottom, c.bottom);
    return right - left < 1 || bottom - top < 1 ? null : { left, right, top, bottom, width: right - left, height: bottom - top };
  };

  /* ---------- collect ---------- */
  const elements = [];
  for (const el of document.querySelectorAll("body *")) {
    if (skipped(el)) continue;
    if (el.closest("svg") && el.tagName !== "svg") continue;
    if (!visible(el)) continue;
    elements.push(el);
  }
  const boxes = elements.filter(isBox);
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let n = walker.nextNode(); n && texts.length < 6000; n = walker.nextNode()) {
    if (!/\S/.test(n.data)) continue;
    const el = n.parentElement;
    if (!el || skipped(el) || !visible(el)) continue;
    if (el.closest("svg") && !el.closest("text")) continue;
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      texts.push({ el, node: n, r });
    }
  }

  if (o.edge) {
    const overlays = elements.filter((el) => cs(el).position === "sticky" || cs(el).position === "fixed" && !screenLike(el));
    const screen = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
    const covered = new Set();
    const painted = (el, x, y) => {
      const s = cs(el);
      return alpha(s.backgroundColor) > 0.05 || s.backgroundImage !== "none" || MEDIA.has(el.tagName)
        || texts.some((t) => el === t.el && x >= t.r.left && x <= t.r.right && y >= t.r.top && y <= t.r.bottom);
    };
    const cover = (A, B, r) => {
      const overlap = clamp(r, rectOf(B));
      const seen = overlap && clamp(overlap, screen);
      if (!seen || seen.width * seen.height < 4) return false;
      const points = [0.2, 0.5, 0.8].map((f) => [seen.left + seen.width * f, seen.top + seen.height / 2]);
      return points.some(([x, y]) => {
        for (const hit of document.elementsFromPoint(x, y)) {
          if (hit === A || A.contains(hit)) return false;
          if ((hit === B || B.contains(hit)) && painted(hit, x, y)) return true;
        }
        return false;
      });
    };
    const inFlow = (el) => {
      for (let p = el; p && p !== document.body; p = p.parentElement) {
        if (cs(p).position === "absolute" || cs(p).position === "fixed" && !screenLike(p)) return false;
      }
      return !isFixed(el);
    };
    const candidates = texts.filter((t) => inFlow(t.el))
      .map((t) => ({ el: t.el, r: clamp(ink(t.r), clipOf(t.el)) }))
      .concat(elements.filter((el) => inFlow(el) && el.matches("button, input, select, textarea, [role=button], [role=tab]"))
        .map((el) => ({ el, r: clamp(rectOf(el), clipOf(el)) })));
    for (const B of overlays) {
      const br = rectOf(B);
      if (o.scrollable && (o.edge === "top" && br.top > 0 || o.edge === "bottom" && br.bottom < innerHeight)) continue;
      for (const { el, r } of candidates) {
        if (!r || B.contains(el) || el.contains(B)) continue;
        const key = describe(el) + "|" + describe(B);
        if (covered.has(key) || !cover(el, B, r)) continue;
        covered.add(key);
        add("fixed-sticky-occlusion", "block", el, `covered by ${describe(B)} at the ${o.edge} of the page`, r);
      }
    }
    for (let i = 0; i < overlays.length; i++) {
      for (const B of overlays.slice(i + 1)) {
        const A = overlays[i];
        if (A.contains(B) || B.contains(A)) continue;
        if (cover(A, B, rectOf(A)) || cover(B, A, rectOf(B))) {
          add("fixed-elements-overlap", "review", A, `overlaps ${describe(B)} at the ${o.edge} of the page`, rectOf(A));
        }
      }
    }
    return { findings: out, counts };
  }

  for (const canvas of elements.filter((el) => el.tagName === "CANVAS")) {
    const clipped = clamp(rectOf(canvas), clipOf(canvas));
    const seen = clipped && clamp(clipped, { left: 0, top: 0, right: vw, bottom: vh });
    if (seen && area(seen) > 0.6 * vw * vh) {
      add("canvas-page", "review", canvas, "canvas page: geometry only sees the HTML over the canvas; judge the canvas from the screenshots", rectOf(canvas));
      break;
    }
  }
  if (o.state === "error") {
    const raw = /Traceback|at Object\.|TypeError|\[object Object\]|\bundefined\b|\bNaN\b|Internal Server Error|(?:^|\n)\s*at\s+\S+.*:\d+(?::\d+)?/;
    const seen = new Set();
    for (const t of texts) {
      if (seen.has(t.el) || !clamp(t.r, clipOf(t.el)) || !raw.test(t.node.data)) continue;
      seen.add(t.el);
      add("raw-failure-text", "review", t.el, `raw failure text: ${t.node.data.trim().replace(/\s+/g, " ").slice(0, 120)}`, t.r);
    }
  }

  /* ---------- page fits the screen ---------- */
  const sw = document.documentElement.scrollWidth;
  if (sw > vw + 1) {
    add("page-scrolls-sideways", "block", document.body, `the page is ${sw}px wide in a ${vw}px viewport, so it scrolls sideways`, rectOf(document.body));
    let n = 0;
    for (const el of elements) {
      const r = rectOf(el);
      if (r.right <= vw + 1 || isFixed(el)) continue;
      const c = clipAbove(el, "x");
      if (c && c.el !== document.body) continue;
      const p = el.parentElement;
      if (p && p !== document.body && rectOf(p).right > vw + 1) continue;   // report the outermost only
      add("page-scrolls-sideways", "block", el, `right edge at ${Math.round(r.right)}px, past the ${vw}px screen`, r);
      if (++n >= 5) break;
    }
  }

  /* ---------- text against text ---------- */
  const sorted = texts.filter((t) => !isFixed(t.el))
    .map((t) => ({ ...t, seen: clamp(t.r, clipOf(t.el)) })).filter((t) => t.seen)
    .map((t) => ({ ...t, i: ink(t.seen) })).sort((a, z) => a.i.top - z.i.top);
  const pairSeen = new Set();
  for (let a = 0; a < sorted.length; a++) {
    const A = sorted[a];
    for (let z = a + 1; z < sorted.length && sorted[z].i.top < A.i.bottom; z++) {
      const Z = sorted[z];
      if (Z.node === A.node) continue;
      const ov = inter(A.i, Z.i);
      if (ov <= 24 || ov < 0.3 * Math.min(area(A.i), area(Z.i))) continue;
      const key = describe(A.el) + "|" + describe(Z.el);
      if (pairSeen.has(key)) continue;
      pairSeen.add(key);
      add("text-overlaps-text", placedUp(A.el) || placedUp(Z.el) ? "review" : "block", A.el, `overlaps ${describe(Z.el)} by ${Math.round(ov)}px²`, A.r);
    }
  }

  /* ---------- text inside its box ---------- */
  const perBox = new Map();
  for (const t of texts) {
    const B = boxAbove(t.el, true);
    if (!B || CONTROL.has(B.tagName) && B.tagName !== "BUTTON") continue;
    const s = cs(B);
    const br = rectOf(B);
    const inner = {
      left: br.left + px(s.borderLeftWidth), right: br.right - px(s.borderRightWidth),
      top: br.top + px(s.borderTopWidth), bottom: br.bottom - px(s.borderBottomWidth),
    };
    let content = t.r;
    for (let p = t.el; p && p !== B && content; p = p.parentElement) {
      const s = cs(p), r = rectOf(p);
      content = clamp(content, {
        left: s.overflowX === "visible" ? -1e9 : r.left,
        right: s.overflowX === "visible" ? 1e9 : r.right,
        top: s.overflowY === "visible" ? -1e9 : r.top,
        bottom: s.overflowY === "visible" ? 1e9 : r.bottom,
      });
    }
    if (!content) continue;
    const i = ink(content);
    const seen = clamp(t.r, clipOf(t.el));
    const v = seen && ink(seen);
    const g = perBox.get(B) || { left: Infinity, right: Infinity, top: Infinity, bottom: Infinity,
                                 vleft: Infinity, vright: Infinity, vtop: Infinity, vbottom: Infinity, r: br, el: B };
    g.left = Math.min(g.left, i.left - inner.left);
    g.right = Math.min(g.right, inner.right - i.right);
    g.top = Math.min(g.top, i.top - inner.top);
    g.bottom = Math.min(g.bottom, inner.bottom - i.bottom);
    if (v) {   // what is on screen, for touching and cramped
      g.vleft = Math.min(g.vleft, v.left - inner.left);
      g.vright = Math.min(g.vright, inner.right - v.right);
      g.vtop = Math.min(g.vtop, v.top - inner.top);
      g.vbottom = Math.min(g.vbottom, inner.bottom - v.bottom);
    }
    perBox.set(B, g);
  }
  for (const [B, g] of perBox) {
    const s = cs(B);
    const ellipsis = s.textOverflow === "ellipsis";
    const clipsX = s.overflowX !== "visible";
    const clipsY = s.overflowY !== "visible";
    for (const side of ["left", "right", "top", "bottom"]) {
      let v = g[side];
      if (!isFinite(v)) continue;
      if (v >= -1) v = g["v" + side];   // nothing spills: judge the visible text
      if (!isFinite(v)) continue;
      const horiz = side === "left" || side === "right";
      if (side === "right" && ellipsis) continue;
      const mode = horiz ? s.overflowX : s.overflowY;
      if (v < -1 && (mode === "auto" || mode === "scroll")) {
        add("scrolls-inside-box", "review", B, `${Math.round(-v)}px of text sits past the ${side} edge, reached only by scrolling the box`, g.r);
      } else if (v < -1) {
        if (horiz ? clipsX : clipsY) {
          if (!(horiz && ellipsis) && !(side === "bottom" && s.webkitLineClamp !== "none" && s.webkitLineClamp)) {
            add("text-cut-off", "block", B, `text runs ${Math.round(-v)}px past the ${side} edge and is cut off`, g.r);
          }
        } else {
          add("text-spills-out", "block", B, `text spills ${Math.round(-v)}px out of the ${side} edge`, g.r);
        }
      } else if (v < 1) {
        add("text-touches-edge", horiz ? "block" : "review", B, `text touches the ${side} edge (${v.toFixed(1)}px)`, g.r);
      } else if (horiz && v < 6) {
        add("text-cramped", "review", B, `text is ${v.toFixed(1)}px from the ${side} edge`, g.r);
      } else if (!horiz && v < 2) {
        add("text-cramped", "review", B, `text is ${v.toFixed(1)}px from the ${side} edge`, g.r);
      }
    }
  }

  /* ---------- boxes inside boxes: escaping, lopsided, squeezed ---------- */
  for (const X of boxes) {
    const B = boxAbove(X);
    if (!B) continue;
    const xr = rectOf(X);
    const br = rectOf(B);
    const s = cs(B);
    const gaps = { left: xr.left - br.left, right: br.right - xr.right, top: xr.top - br.top, bottom: br.bottom - xr.bottom };
    const worst = Math.min(gaps.left, gaps.right, gaps.top, gaps.bottom);
    if (worst < -4) {
      const side = Object.keys(gaps).find((k) => gaps[k] === worst);
      const horiz = side === "left" || side === "right";
      const c = clipAbove(X, horiz ? "x" : "y");
      const clipped = c && (c.el === B || B.contains(c.el));
      if (clipped) continue;   // a scroller or a mask, cut on purpose
      add("box-escapes-box", placed(X) || isFixed(X) ? "review" : "block", X,
        `pokes ${Math.round(-worst)}px out of the ${side} of ${describe(B)}`, xr);
      continue;
    }
    if (placed(X)) continue;
    const lop = (a, z) => a < 1 && z > 12;
    if (lop(gaps.left, gaps.right) || lop(gaps.right, gaps.left)) {
      add("lopsided-inset", "review", X, `${Math.round(gaps.left)}px from the left of ${describe(B)}, ${Math.round(gaps.right)}px from the right`, xr);
    }
    const control = B.tagName === "BUTTON" || B.tagName === "A" || B.getAttribute("role") === "button";
    const glyph = X.tagName === "svg" || X.tagName === "IMG" || (["I", "SPAN"].includes(X.tagName) && !X.textContent.trim());
    if (control && glyph && Math.min(gaps.left, gaps.right) < 4 && br.width < 64) {
      add("control-cramped", "review", B, `icon has ${Math.round(gaps.left)}px and ${Math.round(gaps.right)}px either side`, br);
    }
  }

  /* ---------- things that are not nested: touching and colliding ---------- */
  const flowBoxes = boxes.filter((b) => !isFixed(b));
  const sep = (a, z) => ({
    dx: Math.max(a.left - z.right, z.left - a.right),
    dy: Math.max(a.top - z.bottom, z.top - a.bottom),
  });
  const textSample = sorted.length > 3000 ? sorted.filter((_, k) => k % 2 === 0) : sorted;
  const touchSeen = new Set();
  for (const X of flowBoxes) {
    const xr = clamp(rectOf(X), clipOf(X));   // a card scrolled under a bar is clipped, not covered
    if (!xr) continue;
    for (const T of textSample) {
      if (X.contains(T.el) || T.el.contains(X)) continue;
      const { dx, dy } = sep(xr, T.i);
      if (dx < -0.5 && dy < -0.5 && placed(X) && !X.textContent.trim()) {
        const layer = boxAbove(X);   // a fill or backdrop drawn under the text it sits with
        if (!layer || layer.contains(T.el)) continue;
      }
      if (dx > 6 || dy > 6) continue;
      const key = describe(X) + "|" + describe(T.el);
      if (touchSeen.has(key)) continue;
      if (dx < -0.5 && dy < -0.5) {
        const ov = inter(xr, T.i);
        if (ov < 0.3 * area(T.i)) continue;
        touchSeen.add(key);
        const media = X.tagName === "svg" || MEDIA.has(X.tagName);
        const sev = media || placedUp(X) || placedUp(T.el) ? "review" : "block";
        add("text-over-box", sev, T.el, `sits over ${describe(X)}`, T.r);
      } else {
        const gap = Math.max(dx, dy);
        const along = dx > dy ? "beside" : "above or below";
        if (gap < 1) {
          touchSeen.add(key);
          add("text-touches-box", placedUp(X) || placedUp(T.el) ? "review" : "block", T.el, `touches ${describe(X)} (${gap.toFixed(1)}px, ${along})`, T.r);
        } else if (gap < 4) {
          touchSeen.add(key);
          add("text-near-box", "review", T.el, `${gap.toFixed(1)}px from ${describe(X)} (${along})`, T.r);
        }
      }
    }
  }
  /* An icon-only control squeezed narrower than it is tall: a flex row shrank it. */
  for (const B of elements.filter((el) => el.matches("button, a, [role=button]") && !el.textContent.trim())) {
    const r = rectOf(B);
    if (r.height >= 20 && r.height <= 64 && r.width < 0.8 * r.height && B.querySelector("svg, img, i, span")) {
      add("icon-button-squeezed", "block", B, `icon button is ${Math.round(r.width)}px wide and ${Math.round(r.height)}px tall: give it room either side of the icon`, r);
    }
  }

  /* A cut-out image (a mascot, a product shot on transparency) touches text with its
     painted pixels, not its box: measure from the text's ink to the nearest opaque cell. */
  for (const X of elements.filter((el) => el.tagName === "IMG" && el.complete && el.naturalWidth > 0)) {
    const xr = rectOf(X);
    if (xr.width < 24 || xr.height < 24) continue;
    const near = textSample.filter((T) => {
      if (X.contains(T.el) || !clamp(T.i, clipOf(T.el))) return false;
      const { dx, dy } = sep(xr, T.i);
      return dx < 12 && dy < 12;
    });
    if (!near.length) continue;
    const cell = Math.max(1, Math.ceil(Math.max(xr.width, xr.height) / 240));
    const cols = Math.ceil(xr.width / cell), rows = Math.ceil(xr.height / cell);
    let data;
    try {
      const c = document.createElement("canvas");
      c.width = cols; c.height = rows;
      const g = c.getContext("2d", { willReadFrequently: true });
      const fit = cs(X).objectFit, nw = X.naturalWidth, nh = X.naturalHeight;
      let w = cols, h = rows;
      if (fit === "contain" || fit === "scale-down") { const k = Math.min(cols / nw, rows / nh); w = nw * k; h = nh * k; }
      else if (fit === "cover") { const k = Math.max(cols / nw, rows / nh); w = nw * k; h = nh * k; }
      else if (fit === "none") { w = nw / cell; h = nh / cell; }
      g.drawImage(X, (cols - w) / 2, (rows - h) / 2, w, h);
      data = g.getImageData(0, 0, cols, rows).data;
    } catch { continue; }   // a cross-origin image taints the canvas: the box checks still apply
    let clear = 0;
    for (let k = 3; k < data.length; k += 4) if (data[k] < 16) clear++;
    if (clear < 0.05 * cols * rows) continue;   // an opaque photo: its box is its ink
    for (const T of near) {
      const key = describe(X) + "|ink|" + describe(T.el);
      if (touchSeen.has(key)) continue;
      let gap = Infinity;
      for (let row = 0; row < rows && gap > -1; row++) {
        const top = xr.top + row * cell, bottom = top + cell;
        const dy = Math.max(top - T.i.bottom, T.i.top - bottom);
        if (dy >= gap) continue;
        for (let col = 0; col < cols; col++) {
          if (data[(row * cols + col) * 4 + 3] < 64) continue;
          const left = xr.left + col * cell;
          const d = Math.max(left - T.i.right, T.i.left - (left + cell), dy);
          if (d < gap) gap = d;
        }
      }
      // Air scales with the type: 8px beside body text reads as touching beside an 80px title.
      const size = px(cs(T.el).fontSize);
      const touching = Math.max(4, 0.25 * size), close = Math.max(8, 0.5 * size);
      if (gap >= close) continue;
      touchSeen.add(key);
      if (gap < 0) add("text-over-image", "review", T.el, `sits on the painted pixels of ${describe(X)}`, T.r);
      else if (gap < touching) add("text-touches-image", placedUp(T.el) && !placedUp(X) ? "review" : "block", T.el,
        `${gap.toFixed(0)}px from the painted edge of ${describe(X)}, under a quarter of its ${size}px type`, T.r);
      else add("text-near-image", "review", T.el, `${gap.toFixed(0)}px from the painted edge of ${describe(X)}; give ${Math.ceil(close)}px beside ${size}px type`, T.r);
    }
  }

  for (let a = 0; a < flowBoxes.length; a++) {
    const A = flowBoxes[a];
    const ar = rectOf(A);
    for (let z = a + 1; z < flowBoxes.length; z++) {
      const Z = flowBoxes[z];
      if (A.contains(Z) || Z.contains(A)) continue;
      const zr = rectOf(Z);
      const { dx, dy } = sep(ar, zr);
      if (dx > 1 || dy > 1) continue;
      if (dx < -0.5 && dy < -0.5) {
        const ov = inter(ar, zr);
        if (ov < 0.1 * Math.min(area(ar), area(zr))) continue;
        add("boxes-collide", placedUp(A) || placedUp(Z) ? "review" : "block", A, `overlaps ${describe(Z)} by ${Math.round(ov)}px²`, ar);
      } else if (A.parentElement !== Z.parentElement || cs(A.parentElement).gap !== "0px") {
        add("boxes-touch", "review", A, `touches ${describe(Z)}`, ar);
      }
    }
  }

  /* ---------- siblings: gaps, alignment, sizes, titles ---------- */
  const sig = (el) => el.tagName + (el.classList[0] ? "." + el.classList[0] : "");   // kind, not variant
  const kidsOf = (el) => [...el.children].filter((c) => !skipped(c) && visible(c) && !placed(c) && !isFixed(c));
  const HEAD = "h1, h2, h3, h4, h5, h6, [role=heading], [class*=title], [class*=heading]";
  for (const P of elements.concat([document.body])) {
    const kids = kidsOf(P);
    if (kids.length < 2) continue;
    const rs = kids.map(rectOf);
    // stacked or in a row?
    const stacked = rs.every((r, k) => k === 0 || r.top >= rs[k - 1].bottom - 1);
    const row = rs.every((r, k) => k === 0 || r.left >= rs[k - 1].right - 1);
    const sameKind = kids.every((k) => sig(k) === sig(kids[0]));
    if (sameKind && kids.length >= 3 && (stacked || row)) {
      const gaps = rs.slice(1).map((r, k) => stacked ? r.top - rs[k].bottom : r.left - rs[k].right);
      const spread = Math.max(...gaps) - Math.min(...gaps);
      if (spread > 2) {
        add("uneven-gaps", "review", P, `gaps between its ${kids.length} ${sig(kids[0]).toLowerCase()} children run ${gaps.map((g) => Math.round(g)).join(", ")}`, rectOf(P));
      }
    }
    if (stacked && kids.length >= 2) {
      const lefts = rs.map((r) => r.left);
      for (let k = 0; k < lefts.length; k++) {
        for (let j = k + 1; j < lefts.length; j++) {
          const d = Math.abs(lefts[k] - lefts[j]);
          if (d > 0.5 && d <= 6) {
            add("near-miss-alignment", "review", kids[j], `left edge ${d.toFixed(1)}px off ${describe(kids[k])}`, rs[j]);
            j = lefts.length; k = lefts.length;
          }
        }
      }
    }
    if (sameKind && kids.length >= 2) {
      const small = rs.every((r) => r.height < 64);
      const icons = kids.every((k) => k.tagName === "svg" || k.tagName === "IMG");
      if (small) {
        const hs = rs.map((r) => r.height);
        const d = Math.max(...hs) - Math.min(...hs);
        const dw = icons ? Math.max(...rs.map((r) => r.width)) - Math.min(...rs.map((r) => r.width)) : 0;
        if ((d >= 1 && d <= 8) || (dw >= 1 && dw <= 8)) {
          add("siblings-differ-in-size", "review", P, `same-kind children at ${rs.map((r) => `${Math.round(r.width)}x${Math.round(r.height)}`).join(", ")}`, rectOf(P));
        }
      }
      // shared titles in the same place across same-size sibling screens or cards
      const big = rs.every((r) => r.height >= 120 && r.width >= 160);
      const sameSize = rs.every((r) => Math.abs(r.width - rs[0].width) <= 0.05 * rs[0].width && Math.abs(r.height - rs[0].height) <= 0.05 * rs[0].height);
      if (big) {
        const heads = kids.map((k) => k.querySelector(HEAD)).map((h) => (h && visible(h) ? h : null));
        if (heads.every(Boolean)) {
          const hr = heads.map(rectOf);
          const rel = hr.map((h, k) => ({ top: h.top - rs[k].top, bottom: rs[k].bottom - h.bottom, mid: (h.top + h.bottom) / 2 - rs[k].top,
                                          left: h.left - rs[k].left, right: rs[k].right - h.right }));
          const spread = (key) => Math.max(...rel.map((x) => x[key])) - Math.min(...rel.map((x) => x[key]));
          const dy = Math.min(spread("top"), spread("bottom"), spread("mid"));
          const dx = Math.min(spread("left"), spread("right"));
          const d = Math.max(dx, dy);
          if (d > 8) {
            add("titles-move-between-siblings", sameSize && d >= 48 ? "block" : "review", P,
              `the title sits up to ${Math.round(d)}px apart across its ${kids.length} sibling ${sig(kids[0]).toLowerCase()} panels`, rectOf(P));
          }
        }
      }
    }
  }

  /* ---------- tables that share columns should line them up ---------- */
  const cols = new Map();
  for (const th of document.querySelectorAll("table th")) {
    if (!visible(th) || skipped(th)) continue;
    const key = th.textContent.trim().toLowerCase();
    if (!key) continue;
    const t = th.closest("table");
    const list = cols.get(key) || [];
    if (!list.some((x) => x.t === t)) list.push({ t, th, x: rectOf(th).left - rectOf(t).left });
    cols.set(key, list);
  }
  for (const [key, list] of cols) {
    if (list.length < 2) continue;
    const xs = list.map((c) => c.x);
    const d = Math.max(...xs) - Math.min(...xs);
    if (d > 2) add("columns-move-between-tables", "review", list[0].th,
      `the "${key}" column starts at ${xs.map((x) => Math.round(x)).join(", ")}px across ${list.length} tables`, rectOf(list[0].th));
  }

  /* ---------- empty bands ---------- */
  const spans = texts.map((t) => [t.r.top + scrollY, t.r.bottom + scrollY])
    .concat(boxes.filter((b) => CONTROL.has(b.tagName) || (b instanceof HTMLElement && cs(b).backgroundImage !== "none"))
      .map((b) => [rectOf(b).top + scrollY, rectOf(b).bottom + scrollY]))
    // media still loading counts too: a lazy image at opacity 0 is a filled slot
    .concat([...document.querySelectorAll("img, picture, video, canvas, iframe, svg")]
      .filter((m) => (!m.checkVisibility || m.checkVisibility()) && m.getBoundingClientRect().height >= 2)
      .map((m) => { const r = m.getBoundingClientRect(); return [r.top + scrollY, r.bottom + scrollY]; }))
    // and a blurred placeholder painted by a pseudo-element
    .concat(elements.filter((el) => el instanceof HTMLElement && rectOf(el).height >= 48
        && ["::before", "::after"].some((ps) => getComputedStyle(el, ps).backgroundImage !== "none"))
      .map((el) => [rectOf(el).top + scrollY, rectOf(el).bottom + scrollY]))
    .sort((a, z) => a[0] - z[0]);
  let reach = spans.length ? spans[0][1] : 0;
  const bandMin = Math.max(240, 0.5 * vh);
  for (const [t, b] of spans) {
    if (t - reach > bandMin) {
      add("empty-band", "review", document.body, `${Math.round(t - reach)}px with nothing in it, from y=${Math.round(reach)} to y=${Math.round(t)}`,
        { left: 0, top: reach - scrollY, width: vw, height: t - reach });
    }
    reach = Math.max(reach, b);
  }

  /* ---------- tap targets on touch screens ---------- */
  if (o.touch) {
    for (const el of elements) {
      if (!el.matches("a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=tab], summary")) continue;
      const r = rectOf(el);
      if (r.width >= 24 && r.height >= 24) continue;
      const s = cs(el);
      if (s.display === "inline" && el.parentElement && el.parentElement.textContent.trim().length > el.textContent.trim().length + 8) continue;
      add("small-tap-target", "review", el, `${Math.round(r.width)}x${Math.round(r.height)}, under 24x24`, r);
    }
  }

  /* ---------- landmarks, for comparing sibling screens ---------- */
  const landmarks = {};
  const landmarkSamples = {};
  for (const q of ["header", "nav", "h1", "main", "footer", "[role=banner]", "[role=navigation]"]) {
    const el = [...document.querySelectorAll(q)].find((e) => visible(e) && !skipped(e));
    if (el) { landmarks[q] = doc(rectOf(el)); landmarkSamples[q] = describe(el); }
  }
  const title = elements.find((el) => el.matches("h1, h2, h3, h4, h5, h6, [role=heading]"));
  if (title) { landmarks["screen-title"] = doc(rectOf(title)); landmarkSamples["screen-title"] = describe(title); }
  const selector = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    if (!el.parentElement || el === document.body) return "body";
    const peers = [...el.parentElement.children].filter((e) => e.tagName === el.tagName);
    return selector(el.parentElement) + ">" + el.tagName.toLowerCase() + `:nth-of-type(${peers.indexOf(el) + 1})`;
  };
  const anchors = elements.filter((el) => !isFixed(el) && el.matches("h1, h2, h3, p, button, label, [role=heading]"))
    .slice(0, 300).map((el) => ({ selector: selector(el), sample: el.textContent.trim().replace(/\s+/g, " ").slice(0, 40), rect: doc(rectOf(el)) }));
  const layout = o.layout ? [document.body, ...elements].slice(0, 6000).map((el) => {
    if (el === document.body) return { selector: "body", sample: describe(el), rect: doc(rectOf(el)) };
    const parts = [];
    for (let p = el, depth = 0; p && p !== document.body && depth < 10; p = p.parentElement, depth++) {
      if (p.id) { parts.unshift("#" + CSS.escape(p.id)); break; }
      const classes = [...p.classList].filter((c) => !c.startsWith("impeccable-")).slice(0, 2);
      let part = p.tagName.toLowerCase() + classes.map((c) => "." + CSS.escape(c)).join("");
      if (p.parentElement.querySelectorAll(":scope > " + part).length > 1) {
        const peers = [...p.parentElement.children].filter((e) => e.tagName === p.tagName);
        part += `:nth-of-type(${peers.indexOf(p) + 1})`;
      }
      parts.unshift(part);
      if (document.querySelectorAll(parts.join(" > ")).length === 1) break;
    }
    return { selector: parts.join(" > "), sample: describe(el), rect: doc(rectOf(el)) };
  }) : undefined;
  return {
    findings: out, counts, landmarks, landmarkSamples, anchors, layout,
    measured: { elements: elements.length, boxes: boxes.length, textRects: texts.length },
    cls: Math.round((window.__glevelsCls || 0) * 1000) / 1000,
    pageHeight: document.documentElement.scrollHeight,
  };
}

async function longText(page) {
  await page.addStyleTag({ content: `
    * { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; }
    p { margin-bottom: 2em !important; }
  ` });
  await page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const text = n.data.trim();
      if (!text || text.length > 40 || !n.parentElement.closest("button, a, h1, h2, h3, h4, h5, h6, th, label, [class*=badge], [role=tab], [role=heading]")) continue;
      if (n.parentElement.closest("script, style, noscript, template, svg")) continue;
      nodes.push(n);
    }
    for (const n of nodes) {
      const target = Math.ceil(n.data.trim().length * 0.4);
      let extra = "";
      const words = [" more", " words", " here"];
      for (let k = 0; extra.length < target; k++) extra += words[k % words.length];
      n.data += extra;
    }
  });
}

async function measure(page, entry, vp) {
  const touch = vp.width < 720;
  const reach = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let y = vp.height; y < Math.min(reach, maxTiles * vp.height); y += vp.height) {
    await page.evaluate((top) => window.scrollTo(0, top), y);
    await page.waitForTimeout(150);
  }
  await page.waitForFunction(() => [...document.images].every((i) => i.complete), null, { timeout: 4000 }).catch(() => {});
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(300);
  entry.rhythm = await measureRhythm(page);
  entry.rhythm.rows = entry.rhythm.rows.slice(0, 8);
  Object.assign(entry, await page.evaluate(analyse, { cap: 25, touch, vw: vp.width, state: entry.state,
    layout: entry.render === "loaded" && (["phone", "desktop"].includes(vp.name) || [390, 1440].includes(vp.width)) }));
  const maxScroll = await page.evaluate(() => Math.max(0, document.documentElement.scrollHeight - innerHeight));
  for (const [edge, y] of maxScroll > 0 ? [["top", 0], ["bottom", maxScroll]] : [["top/bottom", 0]]) {
    await page.evaluate((top) => window.scrollTo(0, top), y);
    await page.waitForTimeout(150);
    const found = await page.evaluate(analyse, { cap: 25, vw: vp.width, edge, scrollable: maxScroll > 0 });
    entry.findings.push(...found.findings);
    for (const [check, n] of Object.entries(found.counts)) entry.counts[check] = (entry.counts[check] || 0) + n;
  }
  if (entry.state !== "loaded") {
    for (const f of entry.findings) {
      if (!["page-scrolls-sideways", "text-overlaps-text"].includes(f.check)) f.severity = "review";
    }
  }
  const slug = `${results.length}-${vp.name}-${entry.render.replaceAll(" ", "-")}`;
  const last = Math.max(0, entry.pageHeight - vp.height);
  const positions = new Set(maxTiles > 1 ? [0, last] : [0]);
  for (const f of entry.findings) {
    if (f.severity === "block" && f.rect && positions.size < maxTiles) {
      positions.add(Math.max(0, Math.min(last, Math.floor(f.rect[1] / vp.height) * vp.height)));
    }
  }
  for (let y = 0; y < entry.pageHeight && positions.size < maxTiles; y += vp.height) positions.add(Math.min(y, last));
  entry.screenshots = [];
  entry.tiles = [];
  for (const [k, y] of [...positions].sort((a, b) => a - b).entries()) {
    await page.evaluate((top) => window.scrollTo(0, top), y);
    await page.waitForTimeout(100);
    const file = path.join(outDir, `${slug}-${k}.png`);
    await page.screenshot({ path: file, animations: "disabled" });
    entry.screenshots.push(file);
    entry.tiles.push({ path: file, y });
  }
  if (entry.pageHeight > maxTiles * vp.height) entry.notShown = entry.pageHeight - maxTiles * vp.height;
  await page.evaluate(() => window.scrollTo(0, 0));
}

const browser = await chromium.launch();
const results = [];
try {
  for (const url of urls) {
    const phone = viewports.find((vp) => vp.width === 390) || { name: "phone", width: 390, height: 844 };
    const jobs = states.includes("loaded") ? viewports.map((vp) => ({ vp, state: "loaded" })) : [];
    jobs.push(...states.filter((state) => state !== "loaded").map((state) => ({ vp: phone, state })));
    let noRequestsNoted = false;
    for (const { vp, state } of jobs) {
      const reference = results.find((r) => r.url === url && r.vp === phone.name && r.render === "loaded");
      if (["loading", "error"].includes(state) && reference && !reference.error && !reference.requests) {
        if (!noRequestsNoted) {
          (reference.notes ||= []).push("loading/error skipped: the page made no fetch/XHR requests");
          noRequestsNoted = true;
        }
        continue;
      }
      const touch = vp.width < 720;
      const context = await browser.newContext({
        viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 1,
        reducedMotion: "reduce", isMobile: false, hasTouch: touch, serviceWorkers: "block",
        extraHTTPHeaders: options.headers, storageState: options.storageState,
      });
      await context.addInitScript(() => {
        window.__glevelsCls = 0;
        try {
          new PerformanceObserver((list) => {
            for (const e of list.getEntries()) if (!e.hadRecentInput) window.__glevelsCls += e.value;
          }).observe({ type: "layout-shift", buffered: true });
        } catch {}
      });
      const page = await context.newPage();
      let requests = 0;
      const errors = [];
      page.on("request", (request) => { if (["fetch", "xhr"].includes(request.resourceType())) requests++; });
      page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
      page.on("console", (m) => m.type() === "error" && errors.push(m.text().slice(0, 200)));
      if (["loading", "error"].includes(state)) {
        await page.route("**/*", async (route) => {
          if (!["fetch", "xhr"].includes(route.request().resourceType())) return route.continue();
          if (state === "error") return route.fulfill({ status: 500, json: { error: "Internal Server Error" } });
          // Leaving the route unresolved holds it until context.close(), after the screenshots.
        });
      }
      let entry = { url, viewport: `${vp.name} ${vp.width}x${vp.height}`, vp: vp.name, state, render: state === "long-text" ? "long text" : state, consoleErrors: errors };
      try {
        const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
        entry.status = resp ? resp.status() : null;
        if (state !== "loading") await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(800);
        if (options.waitFor) await page.waitForSelector(options.waitFor, { state: "visible", timeout: 10000 });
        if (state === "long-text") await longText(page);
        if (["loading", "error"].includes(state) && !requests) {
          if (!noRequestsNoted) {
            const r = reference || results.find((r) => r.url === url);
            if (r) (r.notes ||= []).push("loading/error skipped: the page made no fetch/XHR requests");
            else {
              entry.notes = ["loading/error skipped: the page made no fetch/XHR requests"];
              entry.skipped = true;
              results.push(entry);
            }
            noRequestsNoted = true;
          }
          continue;
        }
        const loadCls = await page.evaluate(() => window.__glevelsCls || 0);
        await measure(page, entry, vp);
        entry.cls = Math.round(loadCls * 1000) / 1000;
        entry.requests = requests;
        results.push(entry);
        if (state === "loaded") {
          for (let k = 1; k <= steps; k++) {
            entry = { url, viewport: `${vp.name} ${vp.width}x${vp.height}`, vp: vp.name, state, render: `step ${k}`, step: k, status: entry.status, consoleErrors: errors };
            await page.keyboard.press(flag("step-key", "ArrowRight"));
            await page.waitForTimeout(600);
            if (options.waitFor) await page.waitForSelector(options.waitFor, { state: "visible", timeout: 10000 });
            await measure(page, entry, vp);
            entry.cls = 0;
            entry.requests = requests;
            results.push(entry);
          }
        }
      } catch (e) {
        entry.error = String(e).slice(0, 300);
        if (!results.includes(entry)) results.push(entry);
      } finally {
        await context.close();
      }
    }
  }
} finally {
  await browser.close();
}
fs.writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(results, null, 2));
console.log(path.join(outDir, "findings.json"));
