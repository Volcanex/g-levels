/* Browser half of correct.motion.rhythm. Run through motion.py for locking,
   serving, memory checks, analysis and the judge pack. Uses the real clock;
   animation seeking happens only after every interaction has been recorded. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const require = createRequire(path.join(process.env.GDESIGN || '/home/gabriel/g-design', 'package.json'));
const { chromium } = require('playwright');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const FRAME_MS = 1000 / 60; // Seek at display cadence, independently of the renderer.
const MAX_SAMPLE_MS = 60000; // Bound pathological declarations; flag incomplete measurements.

function instrument({ selectors, seed }) {
  if (seed !== null && seed !== undefined) {
    let value = seed >>> 0;
    Math.random = () => {
      value = (value + 0x6D2B79F5) >>> 0;
      let n = Math.imul(value ^ value >>> 15, 1 | value);
      n ^= n + Math.imul(n ^ n >>> 7, 61 | n);
      return ((n ^ n >>> 14) >>> 0) / 4294967296;
    };
  }
  const now = () => performance.timeOrigin + performance.now();
  const describe = (el) => {
    if (!el) return '(page)';
    if (el.id) return '#' + CSS.escape(el.id);
    if (!el.parentElement) return el.tagName.toLowerCase();
    const siblings = [...el.parentElement.children].filter(n => n.tagName === el.tagName);
    return describe(el.parentElement) + ' > ' + el.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(el) + 1) + ')';
  };
  const narrativeRoot = el => el.closest(selectors.join(','));
  const narrative = el => !!narrativeRoot(el);
  const state = { animations: [], events: [], raf: [], loaf: [], observed: new Map(), measured: new Map(), describe, now };
  window.__motion = state;
  state.interactions = [];
  state.hovers = [];
  const restRects = new WeakMap();
  const hoverVector = el => {
    const cs = getComputedStyle(el), m = new DOMMatrix(cs.transform === 'none' ? undefined : cs.transform);
    const translate = cs.translate === 'none' ? [] : cs.translate.split(/\s+/);
    const scale = cs.scale === 'none' ? [1, 1] : cs.scale.split(/\s+/).map(Number);
    const px = (value, size) => (parseFloat(value) || 0) * (value?.endsWith('%') ? size / 100 : 1);
    return [m.e + px(translate[0], el.offsetWidth), m.f + px(translate[1], el.offsetHeight),
      Math.hypot(m.a, m.b) * scale[0], Math.hypot(m.c, m.d) * (scale[1] ?? scale[0])];
  };
  let hover = null;
  for (const type of ['pointerover', 'pointerdown', 'click']) document.addEventListener(type, event => {
    const control = event.target.closest('button, a, input, summary, [role="button"]');
    const el = control || event.target;
    const r = el.getBoundingClientRect();
    state.interactions.push({ time: now(), selector: describe(el), rect: [r.x, r.y, r.width, r.height], type,
      feedbackBox: !!control || (!el.children.length && !narrative(el)) });
    if (type !== 'pointerover') { hover = null; return; }
    const old = restRects.get(el) || snapshots.get(el);
    if (old) {
      hover = { target: el, base: old.rect, vector: old.vector || hoverVector(el), selector: describe(el), rect: old.rect, time: now(), end: now(), fraction: 0 };
      state.hovers.push(hover);
    }
  }, true);
  const collect = () => {
    for (const animation of document.getAnimations({ subtree: true })) {
      const effect = animation.effect;
      const target = effect?.target;
      if (!(target instanceof Element)) continue;
      const time = now();
      if (state.observed.has(animation)) {
        const entry = state.observed.get(animation);
        entry.lastSeen = time;
        const r = target.getBoundingClientRect(), old = entry.rect;
        const x = Math.min(old[0], r.x), y = Math.min(old[1], r.y);
        entry.rect = [x, y, Math.max(old[0] + old[2], r.right) - x, Math.max(old[1] + old[3], r.bottom) - y];
        continue;
      }
      const timing = effect.getTiming();
      const computed = effect.getComputedTiming();
      const keyframes = effect.getKeyframes();
      const properties = [...new Set(keyframes.flatMap(k => Object.keys(k)))].filter(k => !['offset', 'computedOffset', 'easing', 'composite'].includes(k));
      const r = target.getBoundingClientRect();
      const entry = {
        id: state.animations.length, animation, target, selector: describe(target), properties,
        duration: Number(timing.duration === 'auto' ? computed.duration : timing.duration) || 0,
        delay: timing.delay, easing: timing.easing === 'linear' && keyframes[0]?.easing !== 'linear' ? keyframes[0].easing : timing.easing,
        keyframeEasings: [...new Set(keyframes.map(k => k.easing))],
        iterations: timing.iterations === Infinity ? 'infinite' : timing.iterations,
        direction: timing.direction, playbackRate: animation.playbackRate,
        narrative: narrative(target), rect: [r.x, r.y, r.width, r.height],
        indicator: !!target.closest('progress, [aria-busy="true"], [role="progressbar"], [role="status"], [data-motion="indicator"]') ||
          [target, ...function* () { for (let n = target.parentElement; n; n = n.parentElement) yield n; }()].some(n => /spin|load|progress|skeleton/i.test((n.getAttribute('class') || '') + ' ' + n.id)),
        hover: target.matches(':hover') && state.interactions.at(-1)?.type === 'pointerover',
        sheet: !!target.closest('dialog, [role="dialog"], [data-motion="sheet"], [class*="sheet"]') || r.width * r.height > innerWidth * innerHeight * .25,
        firstSeen: time, lastSeen: time,
      };
      state.observed.set(animation, entry);
      state.animations.push(entry);
    }
  };
  state.collect = collect;
  let initialized = false;
  const roots = new Set();
  const snapshots = new WeakMap();
  const pending = new Set();
  // Content under an overlay (a loading screen) changes in the DOM but not on screen. Hit testing
  // runs with pointer-events forced on, since a fading overlay often sets pointer-events: none.
  const forceHits = document.createElement('style');
  forceHits.textContent = '*{pointer-events:auto!important}';
  const occluded = (el, [rx, ry, rw, rh]) => {
    if (rx + rw <= 0 || ry + rh <= 0 || rx >= innerWidth || ry >= innerHeight) return true;  // Off screen is unseen too.
    const x = Math.min(innerWidth - 1, Math.max(0, rx + Math.min(rw / 2, 8)));
    const y = Math.min(innerHeight - 1, Math.max(0, ry + rh / 2));
    const top = document.elementFromPoint(x, y);
    if (!top || el.contains(top) || top.contains(el)) return false;
    let opacity = 1, painted = false;  // Only an opaque, painted cover hides content; a fading one does not.
    for (let n = top; n && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      opacity *= Number(cs.opacity);
      if (n.contains(el)) continue;
      const alpha = cs.backgroundColor.startsWith('rgba') ? Number(cs.backgroundColor.match(/[\d.]+(?=\s*\)$)/)?.[0] ?? 1) : 1;
      painted ||= alpha >= .9 || cs.backgroundImage !== 'none' || n.matches('img, video, canvas, svg, iframe');
    }
    return painted && opacity >= .9;
  };
  const scan = () => {
    if (!document.body || document.readyState === 'loading' || (initialized && !roots.size && !pending.size)) return;
    const candidates = new Set(pending);
    if (!initialized) roots.add(document.body);
    for (const root of roots) {
      candidates.add(root);
      for (const el of root.querySelectorAll('*')) candidates.add(el);
    }
    roots.clear();
    const grouped = new Map();
    const animations = document.getAnimations({ subtree: true });
    if (initialized) document.documentElement.append(forceHits);
    try {
    for (const el of candidates) {
      if (['SCRIPT', 'STYLE', 'LINK', 'SVG', 'PATH'].includes(el.tagName) || el.closest('svg, [aria-hidden="true"]')) continue;
      const r = el.getBoundingClientRect();
      const text = [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('').trim();
      const media = el.matches('img, video, canvas, iframe');
      const region = !el.children.length && r.width * r.height >= innerWidth * innerHeight * .02;
      if (!text && !media && !region && !snapshots.has(el)) continue;
      let visible = el.isConnected && r.width > 0 && r.height > 0;
      let opacity = 1;
      for (let node = el; visible && node; node = node.parentElement) {
        const cs = getComputedStyle(node);
        opacity *= Number(cs.opacity);
        visible = cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity) > .01;
      }
      const content = visible ? text || (media ? el.currentSrc || el.getAttribute('src') || el.tagName : region ? '(region)' : '') : '';
      const animated = animations.some(a => a.playState === 'running' && a.effect?.target?.contains(el) &&
        Number(a.effect.getTiming().duration) > 1000 / 60);
      if ((text || media || region) && animated) pending.add(el);
      else pending.delete(el);
      const old = snapshots.get(el);
      const current = { content, opacity, rect: [r.x, r.y, r.width, r.height], vector: hoverVector(el), narrative: narrative(el) };
      snapshots.set(el, current);
      if (!initialized || content === (old?.content || '')) continue;
      const rect = content ? current.rect : old.rect;
      const root = narrativeRoot(el);
      const selector = root ? describe(root) : describe(el);
      const key = selector;
      const interaction = state.interactions.at(-1);
      const feedback = interaction?.feedbackBox && now() - interaction.time < 1000 && (() => {
        const [x, y, w, h] = interaction.rect;
        return rect[0] >= x - 8 && rect[1] >= y - 8 && rect[0] + rect[2] <= x + w + 8 && rect[1] + rect[3] <= y + h + 8;
      })();
      const entry = grouped.get(key) || { selector, time: now(), narrative: !!root || old?.narrative, count: 0,
        rect, instant: false, reveal: false, feedback: true, onscreen: false, occluded: true, units: new Set() };
      let unit = el;
      while (unit.parentElement && unit !== root && ['inline', 'contents'].includes(getComputedStyle(unit).display)) unit = unit.parentElement;
      entry.units.add(unit);
      const [x, y, w, h] = entry.rect;
      const left = Math.min(x, rect[0]), top = Math.min(y, rect[1]);
      entry.rect = [left, top, Math.max(x + w, rect[0] + rect[2]) - left, Math.max(y + h, rect[1] + rect[3]) - top];
      entry.instant ||= !animated && (content ? opacity : old.opacity) >= .98;
      entry.reveal ||= !!content && !(old?.content);
      entry.feedback &&= !!feedback;
      entry.occluded &&= el.isConnected && occluded(el, rect);
      entry.onscreen ||= rect[0] < innerWidth && rect[1] < innerHeight && rect[0] + rect[2] > 0 && rect[1] + rect[3] > 0;
      grouped.set(key, entry);
      if (!el.isConnected) snapshots.delete(el);
    }
    } finally { forceHits.remove(); }
    state.events.push(...[...grouped.values()].map(({ units, ...event }) => ({ ...event, count: units.size })));
    initialized = true;
  };
  function tick() {
    state.raf.push(now());
    collect();
    scan();
    for (const el of document.querySelectorAll('button, input, summary, [role="button"], [data-hover], a')) {
      if (el.matches(':hover')) continue;
      const r = el.getBoundingClientRect();
      restRects.set(el, { rect: [r.x, r.y, r.width, r.height], vector: hoverVector(el) });
    }
    if (hover && hover.target.matches(':hover')) {
      const v = hoverVector(hover.target), b = hover.vector, [, , w, h] = hover.base;
      hover.fraction = Math.max(hover.fraction, Math.abs(v[0] - b[0]) / Math.max(1, w),
        Math.abs(v[1] - b[1]) / Math.max(1, h), Math.abs(v[2] / (b[2] || 1) - 1), Math.abs(v[3] / (b[3] || 1) - 1));
      hover.end = now();
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  new MutationObserver(records => {
    for (const m of records) {
      const touched = [...m.addedNodes, ...m.removedNodes];
      if (touched.length && touched.every(n => n === forceHits)) continue;  // The scan's own hit-test style.
      const el = m.target instanceof Element ? m.target : m.target.parentElement;
      if (!el || el.closest('script, style, svg, [aria-hidden="true"]')) continue;
      if (m.attributeName === 'style' && !/display|visibility|opacity|width|height/.test((m.oldValue || '') + el.getAttribute('style'))) continue;
      roots.add(el);
      for (const n of m.removedNodes) if (n instanceof Element) roots.add(n);
    }
  }).observe(document, {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeOldValue: true,
  });
  state.loafSupported = PerformanceObserver.supportedEntryTypes.includes('long-animation-frame');
  if (state.loafSupported) {
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) state.loaf.push({
        time: performance.timeOrigin + e.startTime, duration: e.duration, blockingDuration: e.blockingDuration,
        scripts: e.scripts.map(s => ({ sourceURL: s.sourceURL, sourceFunctionName: s.sourceFunctionName,
          sourceCharPosition: s.sourceCharPosition, invoker: s.invoker, duration: s.duration })),
      });
    }).observe({ type: 'long-animation-frame', buffered: true });
  }
}

async function sample(page) {
  return page.evaluate(({ frame, cap }) => {
    const state = window.__motion;
    if (!state) throw new Error('page instrumentation was lost');
    const saved = [...state.observed.keys()].map(a => ({ a, time: a.currentTime, playing: a.playState, timing: a.effect.getTiming() }));
    for (const { a } of saved) {
      a.pause();
      a.effect.updateTiming({ fill: 'none' });
      a.currentTime = -1e12;
    }
    const numeric = value => (String(value).match(/[-+]?(?:\d*\.)?\d+(?:e[-+]?\d+)?/gi) || []).map(Number);
    let motionStates, movementValues;
    function vector(entry) {
      const cs = getComputedStyle(entry.target, entry.animation.effect.pseudoElement || null);
      const matrix = new DOMMatrix(cs.transform === 'none' ? undefined : cs.transform);
      movementValues = [...matrix.toFloat64Array(),
        ...(cs.translate === 'none' ? [0, 0] : [...numeric(cs.translate), 0].slice(0, 2)),
        ...(cs.scale === 'none' ? [1, 1] : [...numeric(cs.scale), numeric(cs.scale)[0]].slice(0, 2)),
        cs.rotate === 'none' ? 0 : numeric(cs.rotate).at(-1),
        ...entry.properties.filter(p => /^(left|right|top|bottom|width|height|margin.*|padding.*|inset.*|offsetDistance)$/.test(p)).flatMap(p => numeric(cs[p])),
        entry.target.scrollTop, entry.target.scrollLeft];
      motionStates.add(movementValues.join('|'));
      const values = [];
      const properties = new Set([...entry.properties, 'transform', 'translate', 'scale', 'rotate', 'opacity', 'filter', 'color', 'backgroundColor', 'scrollTop', 'scrollLeft']);
      for (const prop of properties) {
        const value = cs[prop] || cs.getPropertyValue(prop);
        if (prop === 'transform') values.push(...new DOMMatrix(value === 'none' ? undefined : value).toFloat64Array());
        else if (prop === 'translate') values.push(...(value === 'none' ? [0, 0] : [...numeric(value), 0].slice(0, 2)));
        else if (prop === 'scale') values.push(...(value === 'none' ? [1, 1] : [...numeric(value), numeric(value)[0]].slice(0, 2)));
        else if (prop === 'rotate') values.push(value === 'none' ? 0 : numeric(value).at(-1));
        else if (prop === 'filter') {
          const functions = ['blur', 'brightness', 'contrast', 'grayscale', 'hue-rotate', 'invert', 'opacity', 'saturate', 'sepia'];
          for (const fn of functions) {
            const m = String(value).match(new RegExp(fn + '\\(([^)]*)\\)'));
            values.push(m ? numeric(m[1])[0] : ['saturate', 'brightness', 'contrast', 'opacity'].includes(fn) ? 1 : 0);
          }
        } else if (/color$/i.test(prop)) {
          const channels = numeric(String(value).replace(/color\(\s*[\w-]+\s+/, 'color('));
          const alpha = channels.length > 3 ? channels.pop() : 1;
          values.push(...channels.map(c => c * alpha), alpha);
        } else if (prop === 'scrollTop' || prop === 'scrollLeft') values.push(entry.target[prop]);
        else values.push(...numeric(value));
      }
      return values;
    }
    try {
      return state.animations.map(entry => {
        const { animation, target, ...result } = entry;
        if (state.measured.has(entry.id)) return { ...result, ...state.measured.get(entry.id) };
        const original = saved.find(s => s.a === animation);
        const timing = original.timing;
        const keyframes = animation.effect.getKeyframes();
        motionStates = new Set();
        try {
          if (!target.isConnected) throw new Error('target removed before seeking; declared timing retained, curve unavailable');
          if (entry.duration > cap) throw new Error(`duration exceeds ${cap}ms sampling budget; declared timing retained`);
          const iterations = entry.iterations === 'infinite' ? Infinity : entry.iterations;
          const local = Math.max(0, Math.min(iterations - .000001, Math.floor(Math.max(0, (original.time - entry.delay) / (entry.duration || 1)))));
          const iteration = Math.floor(local);
          const samples = [], sampleTimes = [], movementSamples = [];
          const times = [];
          for (let i = 0; i < Math.ceil(entry.duration / frame); i++) times.push(i * frame);
          times.push(entry.duration);
          animation.effect.updateTiming({ fill: 'both' });
          animation.currentTime = entry.delay + iteration * entry.duration;
          animation.effect.updateTiming({ easing: 'linear' });
          if (entry.properties.length) animation.effect.setKeyframes(keyframes.map(k => ({ ...k, easing: 'linear' })));
          const baseline = vector(entry);
          const baseMovement = [...movementValues];
          const baseRect = target.getBoundingClientRect();
          let hoverFraction = 0;
          if (entry.properties.length) animation.effect.setKeyframes(keyframes);
          animation.effect.updateTiming({ easing: timing.easing });
          const initial = vector(entry);
          if (baseline.some((v, i) => Math.abs(v - initial[i]) > .00001)) {
            samples.push(baseline);
            movementSamples.push(baseMovement);
            sampleTimes.push(0);
          }
          for (const t of times) {
            animation.currentTime = entry.delay + iteration * entry.duration + t;
            sampleTimes.push(t);
            samples.push(vector(entry));
            movementSamples.push([...movementValues]);
            const r = target.getBoundingClientRect();
            hoverFraction = Math.max(hoverFraction, Math.abs(r.x - baseRect.x) / Math.max(1, baseRect.width),
              Math.abs(r.y - baseRect.y) / Math.max(1, baseRect.height),
              Math.abs(r.width / Math.max(1, baseRect.width) - 1), Math.abs(r.height / Math.max(1, baseRect.height) - 1));
          }
          if (samples.some(v => v.length !== samples[0].length)) throw new Error('computed property vector changes shape; curve unavailable');
          result.samples = samples;
          result.movementSamples = movementSamples;
          result.hover_fraction = hoverFraction;
          result.sampleTimes = sampleTimes;
          result.movementObserved = motionStates.size > 1;
        } catch (e) { result.sampleError = String(e); }
        finally {
          if (entry.properties.length) animation.effect.setKeyframes(keyframes);
          animation.effect.updateTiming({ ...timing, fill: 'none' });
          animation.currentTime = -1e12;
        }
        state.measured.set(entry.id, { samples: result.samples, movementSamples: result.movementSamples, hover_fraction: result.hover_fraction, sampleTimes: result.sampleTimes, sampleError: result.sampleError, movementObserved: result.movementObserved });
        return result;
      });
    } finally {
      for (const { a, time, playing, timing } of saved) {
        a.effect.updateTiming(timing);
        if (time === null) a.cancel();
        else {
          a.currentTime = time;
          if (playing === 'running') a.play();
        }
      }
    }
  }, { frame: FRAME_MS, cap: MAX_SAMPLE_MS });
}

async function automatic(page, mark) {
  const controls = await page.locator('button, input:not([type="hidden"]), summary, [role="button"], a[href]').evaluateAll(elements => elements.filter(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.disabled) return false;
    if (el.tagName === 'A') {
      const url = new URL(el.href, location.href);
      if (url.origin !== location.origin || url.pathname !== location.pathname || url.search !== location.search) return false;
    }
    return true;
  }).slice(0, 3).map(el => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
      parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + ([...node.parentElement.children].filter(c => c.tagName === node.tagName).indexOf(node) + 1) + ')');
    }
    return parts.join(' > ');
  }));
  for (let i = 0; i < controls.length; i++) {
    const el = page.locator(controls[i]).first();
    await mark(`hover-${i + 1}`);
    await el.hover();
    await page.waitForTimeout(config.duration);
    await mark(`click-${i + 1}`);
    await el.click();
    await page.waitForTimeout(config.duration);
  }
  await mark('scroll');
  await page.evaluate(() => window.scrollTo(0, Math.min(document.documentElement.scrollHeight - innerHeight, innerHeight * .7)));
  await page.waitForTimeout(config.duration);
}

async function capture(browser, reduced) {
  const context = await browser.newContext({ viewport: config.viewport, deviceScaleFactor: 1, reducedMotion: reduced ? 'reduce' : 'no-preference' });
  await context.addInitScript(instrument, { selectors: ['[data-motion="narrative"]', ...config.narrative], seed: config.seed });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  page.on('dialog', dialog => dialog.dismiss());
  const result = { reduced, steps: [], errors: [], capture: page.screencast ? 'playwright-screencast-disk' : 'cdp-screencast-disk' };
  page.on('pageerror', error => result.errors.push(String(error)));
  let step = null, cdp = null;
  const append = (data, time) => {
    if (!step) return;
    const file = path.join(step.directory, `frame-${String(step.captured++).padStart(6, '0')}.jpg`);
    fs.writeFileSync(file, data);
    step.frames.push({ file, time });
  };
  async function finish() {
    if (!step) return;
    step.end = Date.now();
    if (page.screencast) await page.screencast.stop();
    else {
      await cdp.send('Page.stopScreencast');
      step.frames.sort((a, b) => a.time - b.time);
      const list = path.join(step.directory, 'frames.txt');
      const lines = step.frames.flatMap((f, i) => [`file '${path.basename(f.file)}'`, `duration ${Math.max(.001, ((step.frames[i + 1]?.time || Date.now()) - f.time) / 1000)}`]);
      fs.writeFileSync(list, lines.join('\n') + '\n');
      const encoded = spawnSync('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', list, '-c:v', 'libvpx-vp9', '-threads', '1', '-pix_fmt', 'yuv420p', step.video], { encoding: 'utf8' });
      if (encoded.status) throw new Error(`ffmpeg: ${encoded.stderr}`);
    }
    const telemetry = await page.evaluate(selectors => {
      const s = window.__motion;
      if (!s) return { raf: [], events: [], loaf: [], loafSupported: false, smoothScroll: [] };
      s.collect();
      return { raf: s.raf, events: s.events, interactions: s.interactions,
        hovers: s.hovers.map(({ target, ...h }) => h), loaf: s.loaf, loafSupported: s.loafSupported,
        narrativeRegions: [...document.querySelectorAll(selectors.join(','))].map(el => {
          const r = el.getBoundingClientRect();
          return { selector: s.describe(el), rect: [r.x, r.y, r.width, r.height] };
        }),
        firstPaint: performance.timeOrigin + (performance.getEntriesByName('first-paint')[0]?.startTime || 0),
        smoothScroll: [...document.querySelectorAll('*')].filter(el => getComputedStyle(el).scrollBehavior === 'smooth' &&
          (el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth)).map(s.describe) };
    }, ['[data-motion="narrative"]', ...config.narrative]);
    Object.assign(step, telemetry, { raf: telemetry.raf.filter(t => t >= step.start && t <= step.end),
      events: telemetry.events.filter(e => e.time > Math.max(step.start, telemetry.firstPaint || 0) && e.time <= step.end),
      hovers: telemetry.hovers.filter(h => h.time <= step.end && h.end >= step.start),
      loaf: telemetry.loaf.filter(e => e.time >= step.start && e.time <= step.end) });
    if (!step.frames.length) throw new Error(`no captured frames in step ${step.name}`);
    const animations = await sample(page);
    step.animations = animations.filter(a => a.firstSeen <= step.end && a.lastSeen >= step.start);
    result.steps.push(step);
    step = null;
  }
  async function mark(name) {
    if (typeof name !== 'string' || !name.trim()) throw new Error('mark needs a non-empty step name');
    await finish();
    const slug = `${reduced ? 'reduced' : 'normal'}-${String(result.steps.length).padStart(2, '0')}-${name.replace(/[^a-z0-9_-]/gi, '-').slice(0, 60)}`;
    const directory = path.join(config.out, slug);
    fs.mkdirSync(directory, { recursive: true });
    step = { name, directory, video: path.join(directory, 'video.webm'), start: Date.now(), captured: 0, frames: [], animations: [] };
    if (page.screencast) await page.screencast.start({ path: step.video, size: config.viewport, quality: 100,
      onFrame: ({ data, timestamp }) => append(data, timestamp) });
    else {
      if (!cdp) {
        cdp = await context.newCDPSession(page);
        cdp.on('Page.screencastFrame', async event => {
          append(Buffer.from(event.data, 'base64'), event.metadata.timestamp * 1000);
          await cdp.send('Page.screencastFrameAck', { sessionId: event.sessionId }).catch(() => {});
        });
      }
      await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 100, maxWidth: config.viewport.width, maxHeight: config.viewport.height, everyNthFrame: 1 });
    }
  }
  try {
    await mark('load');
    const response = await page.goto(config.target, { waitUntil: 'load', timeout: 30000 });
    if (response && response.status() >= 400) throw new Error(`HTTP ${response.status()}`);
    await page.evaluate(selectors => {
      for (const s of selectors) document.querySelector(s);
    }, config.narrative);
    const paint = await page.evaluate(() => performance.timeOrigin + (performance.getEntriesByName('first-paint')[0]?.startTime || 0));
    step.eventStart = paint;
    step.loadEnd = await page.evaluate(() => performance.timeOrigin + (performance.getEntriesByType('navigation')[0]?.loadEventEnd || 0));
    if (step.frames.length) {
      const firstPaint = step.frames.reduce((best, f, i) => Math.abs(f.time - paint) < Math.abs(step.frames[best].time - paint) ? i : best, 0);
      step.frames = step.frames.slice(firstPaint);
    }
    await page.waitForTimeout(config.duration);
    if (config.script) {
      const script = (await import(pathToFileURL(config.script).href)).default;
      if (typeof script !== 'function') throw new Error('interaction script must default export async (page, mark)');
      await script(page, mark);
      await page.waitForTimeout(config.duration);
    } else await automatic(page, mark);
    await finish();
    return result;
  } finally {
    if (step && page.screencast) await page.screencast.stop().catch(() => {});
    else if (step && cdp) await cdp.send('Page.stopScreencast').catch(() => {});
    await context.close();
  }
}

let browser;
try {
  browser = await chromium.launch();
  const runs = [];
  for (const reduced of [false, true]) runs.push(await capture(browser, reduced));
  fs.writeFileSync(path.join(config.out, 'capture.json'), JSON.stringify({ runs }, null, 2));
} catch (error) {
  console.error(`impossible: ${error.stack || error}`);
  process.exitCode = 2;
} finally {
  if (browser) await browser.close();
}
