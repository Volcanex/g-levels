import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from glevels import cli, pages


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    for name in ("a.js", "b.css"):
        (tmp_path / "src" / name).write_text(name)
    subprocess.run(["git", "-C", str(tmp_path), "add", "src"], check=True)
    (tmp_path / ".glevels").mkdir()
    (tmp_path / "server.py").write_text('''import http.server, os, pathlib, signal, subprocess, sys
pathlib.Path("server.pid").write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
pathlib.Path("child.pid").write_text(str(child.pid))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.path.encode())
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
''')
    (tmp_path / "sensor.py").write_text('''import json, os, pathlib, sys, urllib.request
target = sys.argv[1]
body = urllib.request.urlopen(target).read().decode() if target.startswith("http") else pathlib.Path(target).read_text()
with open("runs.jsonl", "a") as f:
    f.write(json.dumps({"target": target, "body": body, "url": os.environ.get("GLEVELS_URL"), "viewports": os.environ.get("GLEVELS_VIEWPORTS")}) + "\\n")
''')
    monkeypatch.setattr(cli, "LOG", tmp_path / "results.jsonl")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def manifest(repo, **changes):
    doc = {"serve": f"{sys.executable} server.py {{port}}", "ready": "http://127.0.0.1:{port}/ready",
           "timeout": 3, "pages": [{"name": "One", "url": "http://127.0.0.1:{port}/one"},
                                    {"name": "Two", "url": "http://127.0.0.1:{port}/two"}],
           "viewports": "phone=390x844,desktop=1440x900"}
    doc.update(changes)
    (repo / ".glevels/pages.json").write_text(json.dumps(doc))


def check(repo, monkeypatch, commands=None, rel="src", sensor=None):
    commands = commands or [f'{sys.executable} {{repo}}/sensor.py {{file}}']
    gates = {f"correct.test.page-{i}": {"id": f"correct.test.page-{i}", "version": 1, "remediation": "Read the failure",
             "sensor": {"type": "command", "run": command, **(sensor or {})}} for i, command in enumerate(commands)}
    entries = [{"id": gid, "paths": ["src/**"], "charter": "test", "waiver": None} for gid in gates]
    monkeypatch.setattr(cli.registry, "load_gates", lambda: gates)
    monkeypatch.setattr(cli.registry, "load_charters", lambda: {})
    monkeypatch.setattr(cli.registry, "in_scope", lambda repo, rel, charters:
                        ("test", [e for e in entries if cli.registry.covers(rel, e["paths"])]))
    return cli.cmd_check(argparse.Namespace(path=str(repo / rel), all=False, gate=None))


def stopped(repo):
    for name in ("server.pid", "child.pid"):
        pid = int((repo / name).read_text())
        state = Path(f"/proc/{pid}/stat")
        def running():
            try:
                return state.read_text().split()[2] not in ("Z", "X")
            except (FileNotFoundError, ProcessLookupError):
                return False
        deadline = time.monotonic() + 1
        while running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not running()


def test_check_runs_once_per_page_and_stops_whole_server_group(repo, monkeypatch):
    manifest(repo)
    assert check(repo, monkeypatch) == 0
    runs = [json.loads(line) for line in (repo / "runs.jsonl").read_text().splitlines()]
    assert [row["body"] for row in runs] == ["/one", "/two"]
    assert all(row["url"] == row["target"] for row in runs)
    assert all(row["viewports"] == "phone=390x844,desktop=1440x900" for row in runs)
    stopped(repo)


def test_multiple_gates_share_one_server_and_unmatched_sources_stay_out(repo, monkeypatch):
    manifest(repo)
    command = f'{sys.executable} {{repo}}/sensor.py {{file}}'
    assert check(repo, monkeypatch, [command, command]) == 0
    runs = [json.loads(line) for line in (repo / "runs.jsonl").read_text().splitlines()]
    assert len(runs) == 4
    assert runs[0]["target"] == runs[2]["target"]
    (repo / "runs.jsonl").unlink()
    check(repo, monkeypatch, rel="elsewhere")
    assert not (repo / "runs.jsonl").exists()
    stopped(repo)


def test_repo_without_pages_keeps_per_file_behavior(repo, monkeypatch):
    assert check(repo, monkeypatch) == 0
    runs = [json.loads(line) for line in (repo / "runs.jsonl").read_text().splitlines()]
    assert [row["body"] for row in runs] == ["a.js", "b.css"]
    assert not (repo / "server.pid").exists()


def test_file_input_sensor_keeps_tracked_files_beside_pages(repo, monkeypatch):
    manifest(repo)
    assert check(repo, monkeypatch, sensor={"input": "files"}) == 0
    runs = [json.loads(line) for line in (repo / "runs.jsonl").read_text().splitlines()]
    assert [row["body"] for row in runs] == ["a.js", "b.css"]
    assert not (repo / "server.pid").exists()


def test_in_scope_page_gate_does_not_need_tracked_source_files(repo, monkeypatch):
    manifest(repo)
    subprocess.run(["git", "-C", str(repo), "rm", "--cached", "-q", "src/a.js", "src/b.css"], check=True)
    assert check(repo, monkeypatch) == 0
    assert len((repo / "runs.jsonl").read_text().splitlines()) == 2
    stopped(repo)


@pytest.mark.parametrize("changes, reason", [
    ({"ready": "http://127.0.0.1:1/unready", "timeout": 0.4}, "not ready"),
    ({"serve": "echo fixture startup failed >&2\nexit 9"}, "fixture startup failed"),
    ({"pages": []}, "nonempty list"),
])
def test_unready_failed_or_invalid_server_is_impossible(repo, monkeypatch, capsys, changes, reason):
    manifest(repo, **changes)
    assert check(repo, monkeypatch) == 1
    assert "impossible" in capsys.readouterr().out
    result = json.loads((repo / "results.jsonl").read_text())
    assert result["result"] == "impossible"
    assert reason in result["note"]
    assert not (repo / "runs.jsonl").exists()
    if (repo / "server.pid").exists():
        stopped(repo)


@pytest.mark.parametrize("error", [RuntimeError("sensor failed"), KeyboardInterrupt()])
def test_sensor_exception_stops_server(repo, monkeypatch, error):
    manifest(repo)
    with pytest.raises(type(error)):
        with pages.PageInputs(repo) as inputs:
            assert inputs.start() is None
            raise error
    stopped(repo)


def test_sigterm_stops_server_and_restores_signal_handler(repo):
    manifest(repo)
    old = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        with pages.PageInputs(repo) as inputs:
            assert inputs.start() is None
            os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == old
    stopped(repo)


def test_server_dying_between_gates_is_impossible(repo):
    manifest(repo)
    with pages.PageInputs(repo) as inputs:
        assert inputs.start() is None
        os.killpg(inputs.process.pid, signal.SIGKILL)
        inputs.process.wait()
        assert "exited" in inputs.start()
    stopped(repo)


def test_gate_without_pages_starts_no_server_and_survives_a_broken_manifest(repo, monkeypatch):
    manifest(repo, serve="touch server.started\nexit 9")
    command = f'{sys.executable} -c "import os; assert not os.environ.get(\'GLEVELS_URL\')"'
    assert check(repo, monkeypatch, [command]) == 0
    assert not (repo / "server.started").exists()
    assert json.loads((repo / "results.jsonl").read_text())["result"] == "met"


def test_page_sensor_without_file_gets_all_page_urls(repo, monkeypatch):
    manifest(repo)
    command = f'{sys.executable} -c "import os; open(\'urls\', \'w\').write(os.environ[\'GLEVELS_URL\'])" # glevels/runtime.py'
    assert check(repo, monkeypatch, [command]) == 0
    assert [url.rsplit("/", 1)[1] for url in (repo / "urls").read_text().split()] == ["one", "two"]
    stopped(repo)


def test_sighup_stops_server(repo):
    manifest(repo)
    with pytest.raises(KeyboardInterrupt):
        with pages.PageInputs(repo) as inputs:
            assert inputs.start() is None
            os.kill(os.getpid(), signal.SIGHUP)
    stopped(repo)


def test_page_urls_are_shell_quoted(repo, monkeypatch):
    manifest(repo, pages=[{"name": "Query", "url": "http://127.0.0.1:{port}/?a=1&b=two#frag"}])
    assert check(repo, monkeypatch) == 0
    row = json.loads((repo / "runs.jsonl").read_text())
    assert row["body"] == "/?a=1&b=two"


def test_screen_command_and_wing_fallback(repo):
    script = Path(cli.__file__).with_name("screens.py")
    def run():
        return subprocess.run([sys.executable, str(script), str(repo)], capture_output=True, text=True)
    assert run().returncode == 2
    (repo / "app/test").mkdir(parents=True)
    (repo / "app/test/screens.py").write_text('print("Wing walk")')
    assert run().stdout.strip() == "Wing walk"
    (repo / ".glevels/screens").write_text('echo "Repo walk"\nexit 1\n')
    result = run()
    assert result.stdout.strip() == "Repo walk"
    assert result.returncode == 1
