import json
import subprocess

import pytest

from glevels import depthorder


@pytest.mark.parametrize('manifest', [None, {}, {'script': 'missing.mjs'}, {'script': '../elsewhere.mjs'}])
def test_missing_or_invalid_sensor_is_impossible(tmp_path, capsys, manifest):
    if manifest is not None:
        (tmp_path / '.glevels').mkdir()
        (tmp_path / '.glevels/depth-order.json').write_text(json.dumps(manifest))
    assert depthorder.check(tmp_path) == 2
    assert 'impossible:' in capsys.readouterr().err


@pytest.mark.parametrize('result', [0, 1, 2])
def test_manifest_runs_the_sensors_default_suite(tmp_path, monkeypatch, result):
    (tmp_path / '.glevels').mkdir()
    (tmp_path / '.glevels/depth-order.json').write_text('{"script":"tools/depth.mjs"}')
    script = tmp_path / 'tools/depth.mjs'
    script.parent.mkdir()
    script.touch()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, result)

    monkeypatch.setattr(depthorder.subprocess, 'run', run)
    assert depthorder.check(tmp_path) == result
    assert calls == [(['node', str(script)], {'cwd': tmp_path})]
