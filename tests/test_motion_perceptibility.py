import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from glevels import motion
from test_motion import browser_available


def trace(position, hz=60, seconds=3):
    return [dict(selector='#pointer', rect=[20, 20, 12, 18],
                 samples=[[i * 1000 / hz, *position(i / hz)] for i in range(int(seconds * hz) + 1)])]


@pytest.mark.parametrize('amplitude,frequency', [(.3, 9), (.4, 10.6), (.25, 5)])
def test_motion_catches_resting_tremor_with_wander(amplitude, frequency):
    pointers = trace(lambda t: (100 + amplitude * math.sin(t * math.tau * frequency) + 1.4 * math.sin(t * 2), 30))
    findings = motion.pointer_findings(pointers)
    assert findings[0]['check'] == 'pointer-jitter'
    assert findings[0]['severity'] == 'block'


@pytest.mark.parametrize('position', [
    lambda t: (100, 30),
    lambda t: (100 + .001 * math.sin(t * math.tau * 9), 30),
    lambda t: (100 + 2 * math.sin(t), 30),
    lambda t: (100 + 200 * (t / 3) ** 2 * (3 - 2 * t / 3), 30),
    lambda t: (100 + 30 * t + .3 * math.sin(t * math.tau * 9), 30),
])
def test_motion_allows_still_slow_wander_and_deliberate_moves(position):
    assert not motion.pointer_findings(trace(position))


def watched_step(tmp_path, fractions):
    import numpy as np
    from PIL import Image
    pixels = np.zeros((100, 100, 3), dtype=np.uint8)
    frames = []
    for i, fraction in enumerate([0, *fractions]):
        pixels[:int(fraction * 100)] ^= 255
        file = tmp_path / f'{i}.png'
        Image.fromarray(pixels).save(file)
        frames.append(dict(file=str(file), time=i * 1000,
                           regions=[dict(selector='#scene', rect=[0, 0, 100, 100], sustained=True)]))
    return dict(watchedFrames=frames)


def test_motion_catches_long_holds_but_allows_perceptible_narrative(tmp_path):
    still = watched_step(tmp_path, [0] * 10)
    assert motion.narrative_findings(still)[0]['severity'] == 'block'
    assert motion.narrative_findings(watched_step(tmp_path, [0] * 7 + [.15] * 3))
    assert not motion.narrative_findings(watched_step(tmp_path, [.15] * 10))
    assert not motion.narrative_findings(watched_step(tmp_path, [.15] * 7 + [0] * 3))
    assert motion.narrative_findings(watched_step(tmp_path, [0, .15, 0, 0, .15, 0, 0, .15, 0, .15]))


def test_motion_does_not_call_missing_visibility_a_pass(tmp_path):
    step = watched_step(tmp_path, [0] * 10)
    step['watchedFrames'][5]['regions'] = []
    assert motion.narrative_findings(step)[0]['check'] == 'unmeasured-narrative'


def test_motion_completed_reveal_is_a_review_lead(tmp_path):
    step = watched_step(tmp_path, [0] * 10)
    for frame in step['watchedFrames']:
        frame['regions'][0]['sustained'] = False
    assert motion.narrative_findings(step)[0]['severity'] == 'review'


def test_motion_sparse_pointer_trace_is_unmeasured():
    findings = motion.pointer_findings(trace(lambda t: (100, 30), hz=10))
    assert findings[0]['check'] == 'unmeasured-pointer'
    assert findings[0]['severity'] == 'review'


def test_motion_sparse_tremor_is_a_review_lead():
    pointers = trace(lambda t: (100 + .35 * math.sin(t * math.tau * 5), 30), hz=15)
    findings = motion.pointer_findings(pointers)
    assert [(f['check'], f['severity']) for f in findings] == [('unmeasured-pointer', 'review')]


@pytest.mark.parametrize('stop', [False, True])
def test_motion_accumulates_slow_gradient_changes(tmp_path, stop):
    np, Image = motion.dependencies()
    frames = []
    for i in range(41):
        drift = min(i, 8) if stop else i
        pixels = np.repeat((np.arange(100)[None, :] + 40 + drift).astype('uint8'), 100, axis=0)
        file = tmp_path / f'gradient-{i}.png'
        Image.fromarray(pixels).save(file)
        frames.append(dict(file=str(file), time=i * 250,
                           regions=[dict(selector='#field', rect=[0, 0, 100, 100], sustained=True)]))
    step = dict(watchedFrames=frames)
    findings = motion.narrative_findings(step)
    assert bool(findings) == stop
    if stop:
        assert findings[0]['check'] == 'imperceptible-narrative'
    assert any(m['changed_fraction_per_second'] > .5 for m in step['perceptibility'][0]['intervals'])


def test_motion_pointer_measurements_are_json_serializable():
    pointers = trace(lambda t: (100 + .3 * math.sin(t * math.tau * 9), 30))
    assert motion.pointer_findings(pointers)
    assert json.loads(json.dumps(pointers))[0]['measurement']['resolved_windows'] > 0


def test_motion_pointer_evidence_follows_the_resting_position():
    pointers = trace(lambda t: (100 + 200 * min(t, 1) + .3 * math.sin(t * math.tau * 9), 30))
    finding = motion.pointer_findings(pointers)[0]
    assert finding['check'] == 'pointer-jitter'
    assert 299 <= finding['rect'][0] <= 301


def test_motion_samples_within_each_second_do_not_alias_periodic_motion(tmp_path):
    step = watched_step(tmp_path, [.15] * 40)
    for i, frame in enumerate(step['watchedFrames']):
        frame['time'] = i * 250
    assert not motion.narrative_findings(step)
    assert step['perceptibility'][0]['observed_ms'] == 10000


def test_motion_foreground_cannot_supply_a_still_layers_activity(tmp_path):
    step = watched_step(tmp_path, [.5] * 10)
    for frame in step['watchedFrames']:
        frame['regions'][0]['foreground'] = [[0, 0, 100, 50]]
    assert motion.narrative_findings(step)[0]['check'] == 'imperceptible-narrative'


def test_motion_keeps_the_last_partial_second(tmp_path):
    step = watched_step(tmp_path, [0] * 40)
    for i, frame in enumerate(step['watchedFrames']):
        frame['time'] = i * 251
    assert motion.narrative_findings(step)[0]['check'] == 'imperceptible-narrative'
    assert step['perceptibility'][0]['observed_ms'] == 10040


@pytest.mark.parametrize('fixture,expected', [('resting-tremor', 1), ('perceptible-move', 0)])
def test_browser_motion_perceptibility(browser_available, tmp_path, fixture, expected):
    fixtures = Path(__file__).parent / 'fixtures-motion'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '150',
                           '--viewport', '640x480', '--seed', '7', '--script', str(fixtures / 'idle.mjs'),
                           '--out', str(tmp_path), str(fixtures / f'{fixture}.html')],
                          capture_output=True, text=True, timeout=150)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    data = json.loads((tmp_path / 'findings.json').read_text())
    checks = {f['check'] for f in data['findings'] if f['severity'] == 'block'}
    assert checks == ({'imperceptible-narrative', 'pointer-jitter'} if expected else set()), proc.stdout
    step = next(s for s in data['runs'][0]['steps'] if s['name'] == 'perceptibility')
    assert step['perceptibility'][0]['observed_ms'] >= 9900
    assert len(step['pointers'][0]['samples']) > 200


@pytest.mark.parametrize('fixture,expected', [
    ('canvas-raf-low', 1), ('canvas-raf-high', 0), ('slow-gradient', 0), ('descendant-loop', 1),
])
def test_browser_watched_loop_sources(browser_available, tmp_path, fixture, expected):
    fixtures = Path(__file__).parent / 'fixtures-motion'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '150',
                           '--viewport', '640x480', '--script', str(fixtures / 'idle.mjs'),
                           '--out', str(tmp_path), str(fixtures / f'{fixture}.html')],
                          capture_output=True, text=True, timeout=150)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    data = json.loads((tmp_path / 'findings.json').read_text())
    blocks = {f['check'] for f in data['findings'] if f['severity'] == 'block'}
    assert blocks == ({'imperceptible-narrative'} if expected else set()), proc.stdout
    step = next(s for s in data['runs'][0]['steps'] if s['name'] == 'perceptibility')
    assert all(r['sustained'] for f in step['watchedFrames'] for r in f['regions'])
    if fixture.startswith('canvas-raf'):
        fractions = [m['changed_fraction_per_second'] for m in step['perceptibility'][0]['intervals']]
        target = .005 if fixture.endswith('low') else .02
        assert sum(fractions) / len(fractions) == pytest.approx(target, abs=.003)


def test_browser_offscreen_narrative_is_a_lead_without_script(browser_available, tmp_path):
    page = Path(__file__).parent / 'fixtures-motion/offscreen-narrative.html'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '150',
                           '--viewport', '640x480', '--out', str(tmp_path), str(page)],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((tmp_path / 'findings.json').read_text())
    assert any(f['check'] == 'unmeasured-narrative' and f['severity'] == 'review' for f in data['findings'])


@pytest.mark.parametrize('source', ['default', 'flag', 'env'])
def test_browser_pointer_selectors_are_explicit(browser_available, tmp_path, source):
    page = tmp_path / 'pointers.html'
    page.write_text('''<!doctype html><style>i{display:block;width:12px;height:18px;background:#111}</style>
<i class="nav-caret"></i><i class="cursor-pointer"></i><i class="presence-ptr"></i><i class="lp-hand"></i>
<i id="marked" data-motion="pointer"></i><i id="chosen"></i>''')
    args = ['--pointer', '#chosen'] if source == 'flag' else []
    env = dict(os.environ, GLEVELS_POINTER_SELECTORS='["#chosen"]' if source == 'env' else '')
    fixtures = Path(__file__).parent / 'fixtures-motion'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '150',
                           '--script', str(fixtures / 'idle.mjs'), '--out', str(tmp_path), *args, str(page)],
                          env=env, capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((tmp_path / 'findings.json').read_text())
    selectors = {p['selector'] for s in data['runs'][0]['steps'] for p in s['pointers']}
    assert selectors == ({'#marked'} if source == 'default' else {'#marked', '#chosen'})
