import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest


@pytest.mark.parametrize("adapter, expected", [("apple metal-3", 0), ("google swiftshader", 2)])
def test_pair_url_stays_in_file_and_cdp_body(monkeypatch, capsys, adapter, expected):
    path = Path(__file__).resolve().parents[1] / "scripts/mac-runner"
    loader = importlib.machinery.SourceFileLoader("mac_runner", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    runner = importlib.util.module_from_spec(spec)
    loader.exec_module(runner)
    secret = "dev_SECRET_NEVER_IN_ARGV"
    pair_url = "https://runner.gabrielpenman.com/pair#device=mac-m3&token=" + secret
    states = iter([dict(now=1, devices=[]), dict(now=1000), dict(devices=[
        dict(name="mac-m3", online=True, seen=1001, info=dict(ua="Chrome test", gpu=dict(adapter=adapter)))])])
    monkeypatch.setattr(runner, "state", lambda: next(states))
    lifecycle, commands, messages = [], [], []
    monkeypatch.setattr(runner, "remote", lambda action, name: lifecycle.append(action) or ("/tmp/grunner-m3", "stored" if action == "paired" else "pair-needed"))

    def run(argv, **kwargs):
        assert secret not in repr(argv)
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, pair_url if "--pair" in argv else "", "")

    class Tunnel:
        stderr = io.BytesIO()
        def poll(self): return None
        def terminate(self): lifecycle.append("tunnel-stop")
        def wait(self, **kwargs): return 0

    def popen(argv, **kwargs):
        assert secret not in repr(argv)
        assert "-S" in argv and "none" in argv
        return Tunnel()

    class Connection:
        def send(self, body): messages.append(json.loads(body))
        def recv(self): return '{"id":1,"result":{}}'
        def close(self): pass

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner, "urlopen", lambda *a, **k: io.StringIO(json.dumps([
        dict(type="page", webSocketDebuggerUrl="ws://127.0.0.1:9333/devtools/page/test")
    ])))
    monkeypatch.setitem(sys.modules, "websocket", types.SimpleNamespace(create_connection=lambda *a, **k: Connection()))
    assert runner.main(["start"]) == expected
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert messages[0]["params"]["url"] == pair_url
    staged = [(argv, kw) for argv, kw in commands if "cat >" in argv[-1]]
    assert len(staged) == 1 and staged[0][1]["input"] == pair_url
    assert "umask 077" in staged[0][0][-1] and "chmod 600" in staged[0][0][-1]
    assert any("rm -f /tmp/grunner-m3/pair-url" == argv[-1] for argv, _ in commands)
    assert ("stop" in lifecycle) == (expected == 2)
    assert lifecycle[-1] == "tunnel-stop"


def test_busy_runner_refuses_before_touching_the_mac(monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / "scripts/mac-runner"
    loader = importlib.machinery.SourceFileLoader("mac_runner_busy", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    runner = importlib.util.module_from_spec(spec)
    loader.exec_module(runner)
    monkeypatch.setattr(runner, "state", lambda: dict(now=1, devices=[dict(name="mac-m3", online=True, job="j1")]))
    touched = []
    monkeypatch.setattr(runner, "remote", lambda action, name: touched.append(action) or ("/tmp/grunner-m3", ""))
    assert runner.main(["start", "--device", "mac-m3"]) == runner.BUSY
    assert touched == []
    assert "busy" in capsys.readouterr().out
