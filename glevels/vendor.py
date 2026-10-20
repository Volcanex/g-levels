"""Maintained tools the look sensors marry instead of rewriting, pinned and installed on first use.

    from glevels import vendor
    findings, why = vendor.impeccable(url, "390x844", scope="layout")

Packages go to GLEVELS_NODE (default ~/.cache/glevels/node), never into a project. Impeccable
(Apache-2.0, pbakaus/impeccable) runs with telemetry off and the house browser from g-design's
Playwright. stylelint-declaration-strict-value (MIT) rides on g-design's stylelint.
Every helper returns (result, None) or (None, reason): a missing tool is a note for the
reviewer, never a crash, and never silently a pass.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

NODE_DIR = Path(os.environ.get("GLEVELS_NODE", Path.home() / ".cache" / "glevels" / "node"))
GDESIGN = Path(os.environ.get("GDESIGN", "/home/gabriel/g-design"))
PINS = {"impeccable": "4.7.0", "stylelint-declaration-strict-value": "1.12.1"}
QUIET = {"IMPECCABLE_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1"}


def ensure(name):
    """Path to node_modules/<name> at its pinned version, installing it once. (path, None) or (None, why)."""
    mod = NODE_DIR / "node_modules" / name
    pkg = mod / "package.json"
    if pkg.is_file() and json.loads(pkg.read_text()).get("version") == PINS[name]:
        return mod, None
    if shutil.which("npm") is None:
        return None, "npm is not installed"
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    if not (NODE_DIR / "package.json").exists():
        (NODE_DIR / "package.json").write_text('{"private": true}\n')
    p = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--ignore-scripts", f"{name}@{PINS[name]}"],
                       cwd=NODE_DIR, capture_output=True, text=True, timeout=300, env=dict(os.environ, **QUIET))
    if p.returncode or not pkg.is_file():
        return None, f"could not install {name}@{PINS[name]}: {(p.stderr or p.stdout).strip()[-300:]}"
    return mod, None


def chromium():
    """The Chromium g-design's Playwright drives, so every sensor sees the same browser."""
    p = subprocess.run(["node", "-e", "console.log(require('playwright').chromium.executablePath())"],
                       cwd=GDESIGN, capture_output=True, text=True)
    path = p.stdout.strip()
    return path if path and Path(path).exists() else None


def _launcher(browser):
    """Chromium refuses to start as root without --no-sandbox, and Impeccable takes no browser
    flags, so as root it gets a wrapper that adds the flag."""
    if os.geteuid() != 0:
        return browser
    wrap = NODE_DIR / "chromium-no-sandbox"
    body = f'#!/bin/sh\nexec "{browser}" --no-sandbox "$@"\n'
    if not wrap.is_file() or wrap.read_text() != body:
        NODE_DIR.mkdir(parents=True, exist_ok=True)
        wrap.write_text(body)
        wrap.chmod(0o755)
    return str(wrap)


def impeccable(target, viewport="1280x800", scope=None, timeout=180):
    """Impeccable's detector on a URL or file. ([finding, ...], None) or (None, why).
    Each finding has antipattern, name, category, severity, snippet, advisory."""
    mod, why = ensure("impeccable")
    if not mod:
        return None, why
    env = dict(os.environ, **QUIET)
    browser = chromium()
    if browser:
        env["IMPECCABLE_BROWSER"] = _launcher(browser)
    cmd = ["node", str(mod / "cli" / "bin" / "cli.js"), "detect", "--json", "--no-config", "--viewport", viewport]
    if scope:
        cmd += ["--scope", scope]
    cmd.append(str(target))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=NODE_DIR)
    except subprocess.TimeoutExpired:
        return None, f"impeccable timed out after {timeout}s"
    if p.returncode not in (0, 2):   # 1 means a target could not be scanned
        return None, f"impeccable could not scan {target}: {(p.stderr or p.stdout).strip()[-300:]}"
    try:
        return json.loads(p.stdout or "[]"), None
    except json.JSONDecodeError:
        return None, f"impeccable printed no JSON: {p.stdout.strip()[-200:]}"
