"""Capture the visible text and controls of a repo's running page for text sensors."""
import fcntl
import json
import os
import subprocess
from pathlib import Path


def capture(url, out):
    command = ["node", str(Path(__file__).with_name("walk.mjs")), "--mode", "smoke", "--out", str(out), url]
    try:
        with open(os.environ.get("GSPACING_LOCK", "/tmp/glevels-spacing.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                     env=dict(os.environ, GLEVELS_WALK_LOCKED="1"))
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"could not render {url}: {error}") from error
    if process.returncode:
        raise ValueError(f"could not render {url}: {(process.stderr or process.stdout).strip()[-800:]}")
    walk = json.loads((out / "walk.json").read_text())
    states = walk.get("states", [])
    if not isinstance(states, list) or not states or any(not isinstance(state, dict) or state.get("error") or
                                                       not isinstance(state.get("text"), str) for state in states):
        raise ValueError(f"{url}: no rendered page evidence")
    for state in states:
        text = out / state["text"]
        if not text.read_text().strip():
            raise ValueError(f"{url}: rendered page has no visible text")
    return out


def requested(target):
    return os.environ.get("GLEVELS_RENDERED") == "1" and target.startswith(("http://", "https://"))
