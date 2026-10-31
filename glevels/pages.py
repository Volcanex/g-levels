"""Repo-local page inputs and the server lifetime for one check."""
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


# Sensors without {file} that still judge the running pages, through the environment or {repo}.
PAGE_SENSORS = ("glevels/runtime.py", "glevels/frames.py", "glevels/parity.py", "glevels/optiontree.py", "glevels/screens.py")


def uses_pages(command):
    return "{file}" in command or any(sensor in command for sensor in PAGE_SENSORS)


class PageInputs:
    def __init__(self, repo):
        self.repo = repo
        self.path = repo / ".glevels/pages.json"
        self.present = self.path.exists()
        self.pages = []
        self.env = dict(os.environ)
        self.process = None
        self.log = None
        self.error = None
        self.started = False
        self.handlers = {}

    def __enter__(self):
        if self.present:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.handlers[sig] = signal.signal(sig, self.interrupted)
        return self

    @staticmethod
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    def __exit__(self, *exc):
        try:
            self.stop()
        finally:
            for sig, handler in self.handlers.items():
                signal.signal(sig, handler)

    def stop(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # The shell may have exited while its children are still alive.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def start(self):
        if self.started and self.process and self.process.poll() is not None:
            self.error = f"page server exited {self.process.returncode} during the check"
            self.stop()
        if not self.present or self.started:
            return self.error
        self.started = True
        try:
            doc = json.loads(self.path.read_text())
            if not isinstance(doc, dict):
                raise ValueError("pages.json must contain an object")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            def expand(value):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("page commands, names and URLs must be nonempty strings")
                return value.replace("{port}", str(port)).replace("{repo}", str(self.repo))

            def url(value):
                value = expand(value)
                parsed = urlsplit(value)
                if parsed.scheme not in ("http", "https") or not parsed.netloc or any(c.isspace() for c in value):
                    raise ValueError(f"invalid page URL: {value}")
                return value

            def viewports(value):
                if value is None:
                    return None
                value = expand(value)
                for item in value.split(","):
                    name, size = item.split("=")
                    width, height = map(int, size.split("x"))
                    if not name or min(width, height) < 1:
                        raise ValueError("viewports need name=WIDTHxHEIGHT")
                return value

            serve, ready = expand(doc["serve"]), url(doc["ready"])
            timeout = doc.get("timeout", 20)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
                raise ValueError("timeout must be between 0 and 300 seconds")
            pages = doc["pages"]
            if not isinstance(pages, list) or not pages:
                raise ValueError("pages must be a nonempty list")
            default_viewports = viewports(doc.get("viewports"))
            names = set()
            for page in pages:
                if not isinstance(page, dict):
                    raise ValueError("each page must be an object")
                name = expand(page["name"])
                if name in names:
                    raise ValueError(f"duplicate page name: {name}")
                names.add(name)
                self.pages.append({"name": name, "url": url(page["url"]),
                                   "viewports": viewports(page.get("viewports")) or default_viewports})
            env = doc.get("env", {})
            if not isinstance(env, dict) or any(not isinstance(k, str) or not k or "=" in k for k in env):
                raise ValueError("env must map environment names to strings")
            self.env.update({key: expand(value) for key, value in env.items()})
            self.env.update(GLEVELS_URL=" ".join(page["url"] for page in self.pages),
                            GLEVELS_READY_URL=ready, GLEVELS_REPO=str(self.repo), GLEVELS_PAGES=json.dumps(self.pages), GLEVELS_RENDERED="1")
            siblings = doc.get("siblings", [])
            if not isinstance(siblings, list) or any(not isinstance(s, str) or s not in names for s in siblings):
                raise ValueError("siblings must list names from pages")
            if siblings:
                if len(set(siblings)) < 2:
                    raise ValueError("siblings needs at least two different pages")
                by_name = {p["name"]: p["url"] for p in self.pages}
                self.env["GLEVELS_SIBLINGS"] = " ".join(by_name[name] for name in siblings)
            capabilities = doc.get("capabilities", [])
            if not isinstance(capabilities, list) or any(not isinstance(c, str) or not c.strip() for c in capabilities):
                raise ValueError("capabilities must be a list of nonempty strings")
            if capabilities:
                self.env["GLEVELS_CAPABILITIES"] = ",".join(capabilities)
            self.log = tempfile.TemporaryFile()
            self.process = subprocess.Popen(serve, shell=True, cwd=self.repo, env=self.env,
                                            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            reason = "no response"
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ValueError(f"page server exited {self.process.returncode}")
                try:
                    with urllib.request.urlopen(ready, timeout=min(1, max(0.01, deadline - time.monotonic()))) as response:
                        if 200 <= response.status < 300 and self.process.poll() is None:
                            return None
                except (OSError, urllib.error.URLError) as error:
                    reason = str(error)
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            raise ValueError(f"page server not ready at {ready} after {timeout}s: {reason}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            detail = ""
            if self.log:
                self.log.seek(0, os.SEEK_END)
                self.log.seek(max(0, self.log.tell() - 2000))
                detail = self.log.read().decode(errors="replace").strip()
            self.error = f"{self.path}: {error}" + (f"\n{detail}" if detail else "")
            self.stop()
            return self.error

    def environment(self, page=None):
        env = dict(self.env)
        if page:
            env["GLEVELS_URL"] = page["url"]
            env["GLEVELS_PAGE_NAME"] = page["name"]
            if page["viewports"]:
                env["GLEVELS_VIEWPORTS"] = page["viewports"]
        return env
