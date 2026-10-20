import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from glevels import motion


FIXTURES = Path(__file__).parent / 'fixtures-motion'


def token_set():
    return {'durations': {'--dur-base': 140, '--dur-sheet': 500},
            'easings': {'--ease-out': 'cubic-bezier(.2,0,0,1)'}}


def animation(**changes):
    result = dict(selector='#box', properties=['transform'], duration=140, delay=0,
                  easing='cubic-bezier(.2,0,0,1)', iterations=1, narrative=False, sheet=False)
    result.update(changes)
    return result


def checks(findings):
    return {f['check'] for f in findings}


def test_tokens_and_spring_visual_duration(tmp_path):
    file = tmp_path / 'tokens.css'
    file.write_text(':root{--dur-base:140ms;--spring-smooth-dur:495ms;--ease-out:cubic-bezier(.2,0,0,1);'
                    '--spring-smooth:linear(0, .5 20%, .98 60%, 1);--dur-alias:var(--dur-base);}'
                    '@supports not(animation-timing-function:linear(0,1)){:root{--spring-smooth:var(--ease-out);}}')
    tokens = motion.parse_tokens(file)
    assert tokens['durations']['--dur-alias'] == 140
    visual = motion.visual_duration(495, tokens['easings']['--spring-smooth'])
    assert 295 <= visual <= 298
    a = animation(duration=495, easing=tokens['easings']['--spring-smooth'], visual_ms=visual)
    assert 'ui-duration' not in checks(motion.animation_findings(a, tokens))


@pytest.mark.parametrize('easing, point, expected', [
    ('linear', .25, .25), ('linear(0, 1)', .25, .25),
    ('linear(0, .5, 1)', .25, .25), ('linear(0, .5 20% 60%, 1)', .4, .5),
    ('steps(4, end)', .3, .25), ('steps(4, jump-none)', .3, 1/3),
    ('steps(4, jump-both)', 0, .2), ('step-start', 0, 1),
    ('ease-in-out', .5, .5), ('cubic-bezier(.42,0,.58,1)', .5, .5),
])
def test_easing(easing, point, expected):
    assert motion.easing_function(easing)(point) == pytest.approx(expected, abs=.0001)


def test_jump_and_narrow_blocking():
    times = [0, 16.67, 33.34, 50]
    metrics = motion.curve_metrics(times, [[0], [0], [1], [1]])
    findings = motion.animation_findings(animation(duration=800, metrics=metrics), token_set())
    assert next(f for f in findings if f['check'] == 'jump')['severity'] == 'block'
    assert any(f['severity'] == 'block' for f in motion.animation_findings(animation(duration=2000), token_set()))
    assert not any(f['severity'] == 'block' for f in motion.animation_findings(animation(duration=500), token_set()))
    assert not motion.animation_findings(animation(), token_set())


def test_sheet_token_gets_sheet_ceiling():
    a = animation(duration=500, visual_ms=430)
    assert 'ui-duration' not in checks(motion.animation_findings(a, token_set()))
    assert 'ui-duration' in checks(motion.animation_findings(animation(duration=480, visual_ms=430), token_set()))


def test_entrance_duration_is_a_lead():
    a = animation(duration=1400, visual_ms=1250, properties=['opacity'])
    blocking = [f for f in motion.animation_findings(a, token_set()) if f['check'] == 'ui-duration']
    assert blocking[0]['severity'] == 'block'
    lead = [f for f in motion.animation_findings(a, token_set(), entrance=True) if f['check'] == 'ui-duration']
    assert lead[0]['severity'] == 'review'


def test_ambient_indicator_and_reduced_motion():
    assert checks(motion.animation_findings(animation(iterations='infinite'), token_set())) == {'looping'}
    assert not motion.animation_findings(animation(iterations='infinite', indicator=True), token_set())
    metrics = motion.curve_metrics([0, 16.67, 33.34], [[0], [.5], [1]])
    assert checks(motion.animation_findings(animation(metrics=metrics), token_set(), True)) == {'reduced-motion'}
    assert not motion.animation_findings(animation(properties=['opacity'], metrics=metrics), token_set(), True)


def test_rhythm():
    events = [{'selector': '#log', 'time': 100 * i} for i in range(8)]
    assert checks(motion.event_findings(events)) == {'clockwork-rhythm'}
    natural = [{'selector': '#log', 'time': t} for t in [0, 30, 60, 90, 120, 150, 180, 4290]]
    assert not motion.event_findings(natural)
    assert 'too-fast-to-read' in checks(motion.event_findings([{'selector': '#log', 'time': i*20} for i in range(8)]))
    interrupted = [{'selector': '#log', 'time': t} for t in [0, 100, 200, 300, 400, 500, 930, 1500]]
    assert 'clockwork-rhythm' in checks(motion.event_findings(interrupted))
    assert not motion.event_findings(events[:7])
    metrics = motion.rhythm_metrics(natural)[0]
    assert metrics['cv'] > 1
    assert metrics['events'] == 8


def test_rhythm_counts_only_visible_reveals():
    events = [dict(selector='#log', time=i * 100, reveal=True) for i in range(8)]
    hidden = [dict(e, occluded=True) for e in events]
    offscreen = [dict(e, onscreen=False) for e in events]
    assert not motion.rhythm_metrics(hidden + offscreen)
    assert motion.rhythm_metrics(events + hidden + offscreen)[0]['events'] == 8


def test_images_instant_and_unscaled_strips(tmp_path):
    np, Image = motion.dependencies()
    files = []
    for i in range(4):
        pixels = np.zeros((40, 80), dtype=np.uint8)
        if i >= 2:
            pixels[10:30, 20:60] = 200
        file = tmp_path / f'{i}.png'
        Image.fromarray(pixels).save(file)
        files.append(str(file))
    energies, boxes = motion.frame_energy(files)
    times = [i * motion.FRAME_MS for i in range(4)]
    leads = motion.episode_findings(energies, times, boxes)
    assert checks(leads) == {'instant'}
    step = {'frames': [{'file': f, 'time': t} for f, t in zip(files, times)], 'start': 0, 'end': times[-1]}
    strips = motion.frame_strips(leads[0], step, tmp_path, 0)
    with Image.open(strips[0]) as im:
        assert im.size == (160, 20)


def test_cliff_and_sine_energy():
    times = [i * motion.FRAME_MS for i in range(63)]
    boxes = [[0, 0, 20, 20]] * len(times)
    cliff = [0] + [1] * 60 + [0, 0]
    assert checks(motion.episode_findings(cliff, times, boxes)) == {'no-ramp'}
    sine = [0] + [math.sin(math.pi * (i + .5) / 60) for i in range(60)] + [0, 0]
    assert not motion.episode_findings(sine, times, boxes)
    delayed = [t + (80 if i >= 30 else 0) for i, t in enumerate(times)]
    assert not motion.episode_findings(sine, delayed, boxes)


def test_cliff_with_coalesced_capture_frame():
    times = [i * motion.FRAME_MS for i in range(63)]
    boxes = [[0, 0, 20, 20]] * len(times)
    cliff = [0] + [1] * 60 + [0, 0]
    cliff[30] = 4  # One coalesced capture must not hide the sustained speed at the ends.
    assert checks(motion.episode_findings(cliff, times, boxes)) == {'no-ramp'}
    cliff[1] = cliff[60] = .1
    assert checks(motion.episode_findings(cliff, times, boxes)) == {'no-ramp'}
    sine = [0] + [math.sin(math.pi * (i + .5) / 60) for i in range(60)] + [0, 0]
    sine[30] = 4
    assert not motion.episode_findings(sine, times, boxes)


def test_initial_browser_clear_is_not_a_reveal(tmp_path):
    np, Image = motion.dependencies()
    frames = []
    for i, pixels in enumerate([np.full((40, 80), 255, dtype=np.uint8), np.full((40, 80), 17, dtype=np.uint8)]):
        file = tmp_path / f'{i}.png'
        Image.fromarray(pixels).save(file)
        frames.append({'file': str(file), 'time': i * motion.FRAME_MS})
    run = {'reduced': False, 'steps': [dict(name='load', frames=frames, animations=[], events=[], raf=[], loaf=[],
                                         smoothScroll=[], start=0, end=100, video='unused.webm')]}
    assert not motion.analyse_run(run, token_set(), tmp_path)


def test_impossible_input(tmp_path):
    p = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--out', str(tmp_path), '/missing-motion.html'],
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert 'impossible:' in p.stdout


def test_unwritable_output(monkeypatch, tmp_path, capsys):
    def denied(*args, **kwargs):
        raise PermissionError('output denied')
    monkeypatch.setattr(motion.Path, 'mkdir', denied)
    monkeypatch.setattr(motion.Path, 'write_text', denied)
    monkeypatch.setattr(sys, 'argv', ['motion.py', '--out', str(tmp_path), '/missing-motion.html'])
    assert motion.main() == 2
    assert 'impossible: output denied' in capsys.readouterr().out


@pytest.fixture(scope='module')
def browser_available():
    if not shutil.which('node') or not shutil.which('ffmpeg') or not motion.TOKENS.is_file():
        pytest.skip('Node, ffmpeg or g-design tokens unavailable')
    with open(motion.LOCK, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if motion.free_mb() < motion.MIN_FREE_MB:
            pytest.skip('not enough memory to launch Chromium')
        try:
            p = subprocess.run(['node', '-e', 'const r=require("module").createRequire("/home/gabriel/g-design/package.json");'
                                'r("playwright").chromium.launch().then(async b=>{await b.close()}).catch(e=>{console.error(String(e));process.exit(2)})'],
                               capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            pytest.skip('Playwright cannot launch within 45 seconds')
    if p.returncode:
        pytest.skip('Playwright cannot launch: ' + p.stderr[-250:])


@pytest.mark.parametrize('fixture, duration, required, forbidden', [
    ('log-instant', 1200, {'instant'}, {'clockwork-rhythm'}),
    ('log-clockwork', 1200, {'clockwork-rhythm'}, set()),
    ('log-natural', 5300, set(), {'instant', 'clockwork-rhythm', 'too-fast-to-read', 'no-ramp', 'narrative-fast'}),
    ('left-linear', 1200, {'animated-layout', 'movement-easing', 'no-ramp'}, set()),
    ('snappy', 1000, {'snappy'}, set()),
    ('hover-saturation', 750, set(), {'instant', 'jump', 'no-ramp', 'narrative-fast', 'reduced-motion'}),
    ('canvas-cliff', 1800, {'no-ramp'}, set()),
    ('canvas-eased', 1800, set(), {'instant', 'no-ramp'}),
])
def test_browser_fixtures(browser_available, tmp_path, fixture, duration, required, forbidden):
    p = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--out', str(tmp_path),
                        '--viewport', '640x480', '--duration', str(duration), '--script', str(FIXTURES / 'observe.mjs'),
                        str(FIXTURES / f'{fixture}.html')], capture_output=True, text=True, timeout=150)
    assert p.returncode in (0, 1), p.stdout + p.stderr
    result = json.loads((tmp_path / 'findings.json').read_text())
    found = checks(result['findings'])
    assert required <= found, p.stdout
    assert not forbidden.intersection(found), p.stdout
    if not required:
        assert not any(f['severity'] == 'block' for f in result['findings']), p.stdout
    for f in result['findings']:
        assert Path(f['video']).is_file()
        assert f['strips'] and all(Path(s).is_file() for s in f['strips'])


def test_browser_teleport_and_ambient(browser_available, tmp_path):
    file = tmp_path / 'faults.html'
    file.write_text('''<!doctype html><style>
body{background:#111}div{width:40px;height:40px;background:#eee}
#jump{animation:jump 800ms steps(1,end) 200ms both}
#early{animation:jump 800ms steps(1,start) 200ms both}
#ambient{animation:fade 1000ms infinite alternate}
@property --travel{syntax:"<number>";inherits:false;initial-value:0}
#derived{animation:custom 800ms linear 200ms both;transform:translateX(clamp(0px,calc((var(--travel) - .5)*100000px),300px))}
@keyframes custom{to{--travel:1}}
@keyframes jump{from{transform:translateX(0)}to{transform:translateX(300px)}}
@keyframes fade{from{opacity:.5}to{opacity:1}}
</style><div id="jump"></div><div id="early"></div><div id="ambient"></div><div id="derived"></div>''')
    p = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--out', str(tmp_path / 'result'),
                        '--viewport', '640x480', '--duration', '1100', '--script', str(FIXTURES / 'observe.mjs'), str(file)],
                       capture_output=True, text=True, timeout=150)
    assert p.returncode == 1, p.stdout + p.stderr
    data = json.loads((tmp_path / 'result/findings.json').read_text())
    jumps = {f['selector'] for f in data['findings'] if f['check'] == 'jump' and f['severity'] == 'block'}
    assert {'#jump', '#early', '#derived'} <= jumps, p.stdout
    assert any(f['check'] == 'looping' and f['severity'] == 'review' for f in data['findings']), p.stdout
    assert any(f['check'] == 'reduced-motion' and f['selector'] == '#derived' for f in data['findings']), p.stdout


@pytest.mark.parametrize('duration, properties, expected', [
    (140, ['transform'], False), (300, ['opacity'], False), (300, ['backgroundColor'], False),
    (200, ['translate'], True), (300, ['width'], True),
])
def test_jump_only_long_movement(duration, properties, expected):
    metrics = motion.curve_metrics([0, 16.67, 33.34, 50], [[0], [0], [1], [1]])
    found = motion.animation_findings(animation(duration=duration, properties=properties, metrics=metrics), token_set())
    assert ('jump' in checks(found)) == expected


@pytest.mark.parametrize('duration, frames, expected', [(1, 1, False), (40, 1, False), (40, 2, True), (51, 1, True)])
def test_reduced_motion_instant_fallback(duration, frames, expected):
    metrics = motion.curve_metrics([0, 16.67, 33.34], [[0], [.5], [1]])
    metrics['movement_frames'] = frames
    found = motion.animation_findings(animation(duration=duration, metrics=metrics), token_set(), True)
    assert ('reduced-motion' in checks(found)) == expected


def test_reduced_motion_unmeasured_long_movement():
    assert 'reduced-motion' in checks(motion.animation_findings(animation(duration=200), token_set(), True))
    assert not motion.animation_findings(animation(duration=1), token_set(), True)


@pytest.mark.parametrize('fraction, expected', [(.02, False), (.03, False), (.031, True), (.2, True)])
def test_hover_fraction(fraction, expected):
    found = motion.animation_findings(animation(hover=True, hover_fraction=fraction), token_set())
    assert ('hover-movement' in checks(found)) == expected


def test_content_and_duplicate_context():
    base = dict(selector='#log', time=500, instant=True, count=8, rect=[0, 0, 200, 200], narrative=True)
    found = motion.content_findings({'events': [base]})
    assert found[0]['severity'] == 'block'
    assert motion.content_findings({'events': [{**base, 'narrative': False}]})[0]['severity'] == 'review'
    assert not motion.content_findings({'events': [{**base, 'feedback': True}]})
    assert not motion.content_findings({'events': [{**base, 'instant': False}]})
    assert not motion.content_findings({'events': [{**base, 'onscreen': False}]})
    sequence = [{**base, 'time': i*100, 'count': 1, 'reveal': True} for i in range(8)]
    assert not motion.content_findings({'events': sequence})
    assert not motion.content_findings({'events': [{**e, 'count': 2} for e in sequence]})
    assert motion.content_findings({'events': sequence + [{**base, 'time': 1000, 'reveal': True}]})
    skipped = [{**base, 'time': i*100, 'count': 1, 'reveal': True} for i in range(3)] + [{**base, 'time': 400, 'count': 40, 'reveal': True}]
    assert [f['metrics']['content_units'] for f in motion.content_findings({'events': skipped})] == [40]  # A cut-short sequence still blocks on the bulk reveal, not its lines.
    reduced = motion.reduced_content_findings({'events': [base]})
    assert [(f['check'], f['severity']) for f in reduced] == [('reduced-instant', 'review')]
    findings = [{**found[0], 'step': step, 'video': 'v', 'strips': []} for step in ['hover-1', 'click-1', 'hover-1']]
    collapsed = motion.collapse_findings(findings)
    assert len(collapsed) == 1
    assert collapsed[0]['steps'] == ['hover-1', 'click-1']
    assert collapsed[0]['occurrences'] == 3


def test_duplicates_across_motion_preferences():
    normal = dict(check='unmeasured-animation', selector='#toast', severity='review',
                  detail='removed', step='load', steps=['load', 'click'], reduced=False,
                  video='normal.webm', strips=['normal.png'], metrics={}, occurrences=2)
    reduced = dict(normal, step='click', steps=['click'], reduced=True,
                   video='reduced.webm', strips=['reduced.png'], occurrences=3)
    found = motion.collapse_findings([normal, reduced])
    assert len(found) == 1
    assert found[0]['steps'] == ['load', 'click']
    assert found[0]['occurrences'] == 5
    assert found[0]['evidence'][0]['reduced'] is True


def test_browser_feedback_loops_seed_and_assets(browser_available, tmp_path):
    root = tmp_path / 'site'
    (root / 'pages').mkdir(parents=True)
    (root / 'assets').mkdir()
    (root / 'assets/style.css').write_text('''
body{background:#111;color:#eee}button{transition:transform 140ms cubic-bezier(.2,0,0,1)}
#small:hover{transform:scale(1.02);background:red}#large:hover{transform:translateX(20%)}
#instant{transition:none}#instant:hover{transform:scale(1.08)}#content{margin-top:60px}i{display:inline-block;width:20px;height:20px;background:#eee;animation:loop 700ms linear infinite}
@keyframes loop{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){button,i{transition-duration:1ms;animation:none}}
''')
    (root / 'assets/init.js').write_text('window.firstRandom = Math.random(); window.assetLoaded = true;')
    page = root / 'pages/page.html'
    page.write_text('''<!doctype html><link rel="stylesheet" href="../assets/style.css"><script src="../assets/init.js"></script>
<button id="small">Small</button><button id="large">Large</button><button id="instant">Instant</button><button id="replace" onclick="content.textContent='Changed content'">Replace</button>
<div id="content">Original content</div><i id="ambient"></i><span aria-busy="true"><i id="busy"></i></span>
<span role="status"><i id="status"></i></span><i id="loading" class="loading"></i><i id="spinning" class="spin"></i><span id="loader-bar"><i id="named"></i></span><span class="skeleton"><i id="shimmer"></i></span><span role="progressbar"><i id="progress"></i></span>
''')
    script = tmp_path / 'interact.mjs'
    script.write_text('''export default async (page, mark) => {
 if (!await page.evaluate(() => window.assetLoaded && window.firstRandom === 0.6270739405881613)) throw new Error('assets or seed did not precede page scripts');
 for (const id of ['small','large','instant']) {await mark('hover-'+id);await page.locator('#'+id).hover();await page.waitForTimeout(200);}
 await mark('click-replace');await page.locator('#replace').click();await page.waitForTimeout(200);
};''')
    out = tmp_path / 'result'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--seed', '1', '--duration', '200',
                           '--viewport', '640x480', '--script', str(script), '--out', str(out), str(page)],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((out / 'findings.json').read_text())
    findings = data['findings']
    assert {f['selector'] for f in findings if f['check'] == 'looping'} == {'#ambient'}
    assert {f['selector'] for f in findings if f['check'] == 'hover-movement'} == {'#large', '#instant'}
    assert 'jump' not in checks(findings)
    assert 'reduced-motion' not in checks(findings)
    assert {f['selector'] for f in findings if f['check'] == 'instant'} == {'#content'}
    assert len(findings) == len({(f['check'], f['selector']) for f in findings})


def test_browser_gsite_resolve(browser_available, tmp_path):
    from glevels.spacing import serve
    tree = tmp_path / 'gsite'
    page = tree / 'pages/example/content.html'
    page.parent.mkdir(parents=True)
    page.write_text('<p id="compiled">Compiled page</p><script src="/asset.js"></script>')
    (tree / 'asset.js').write_text('window.proxied = true;')
    (tree / 'compile.py').write_text('''from pathlib import Path
OUTPUT_DIR = None
def load_shell(): return 'shell'
def compile_page(page, shell):
    out = OUTPUT_DIR / 'compiled.html'
    out.write_text('<!doctype html>' + (page/'content.html').read_text())
    return out, {'slug':'example'}, None
''')
    script = tmp_path / 'verify.mjs'
    script.write_text('''export default async page => {
 if (!await page.evaluate(() => window.proxied && !!document.querySelector('#compiled'))) throw new Error('compile or proxy failed');
};''')
    server, base = serve({'/': page}, tree)
    try:
        proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '150', '--seed', '1',
                               '--out', str(tmp_path / 'result'), '--script', str(script), str(page)],
                              env=dict(os.environ, GSPACING_BASE=base), capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        server.shutdown()
        server.server_close()
    assert not (tree / 'output').exists()


def test_loop_ignores_timing_easing_and_wobble():
    metrics = motion.curve_metrics([0, 16.67, 33.34, 50], [[0], [2], [-1], [1]])
    a = animation(iterations='infinite', duration=2000, easing='linear', metrics=metrics)
    assert checks(motion.animation_findings(a, token_set())) == {'looping'}
    assert not motion.animation_findings({**a, 'indicator': True}, token_set())
    assert not motion.animation_findings(animation(properties=['transform', 'opacity'], movementObserved=False, metrics=metrics), token_set(), True)


def test_browser_long_observation(browser_available, tmp_path):
    page = tmp_path / 'late.html'
    page.write_text('''<!doctype html><p>Waiting for content</p><script>
setTimeout(() => { const p = document.createElement('p'); p.id='late'; p.textContent='Arrived after thirty seconds'; document.body.append(p); }, 30500);
</script>''')
    script = tmp_path / 'wait.mjs'
    script.write_text('export default async page => { await page.waitForTimeout(31000); };')
    out = tmp_path / 'result'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--seed', '1', '--duration', '100',
                           '--viewport', '320x240', '--script', str(script), '--out', str(out), str(page)],
                          capture_output=True, text=True, timeout=150)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((out / 'findings.json').read_text())
    assert any(f['check'] == 'instant' and f['selector'] == '#late' for f in data['findings']), proc.stdout
    for run in data['runs']:
        step = run['steps'][0]
        assert step['end'] - step['start'] >= 31000
        assert all(Path(f['file']).is_file() for f in step['frames'])


def test_browser_body_click_reveals_narrative(browser_available, tmp_path):
    page = tmp_path / 'skip.html'
    page.write_text('''<!doctype html><div id="log" data-motion="narrative">Waiting</div><script>
document.addEventListener('click', () => { log.innerHTML = '<p>Loaded</p>'.repeat(8); }, {once:true});
</script>''')
    script = tmp_path / 'skip.mjs'
    script.write_text('export default async page => { await page.evaluate(() => document.body.click()); };')
    out = tmp_path / 'result'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '200', '--seed', '1',
                           '--out', str(out), '--script', str(script), str(page)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    data = json.loads((out / 'findings.json').read_text())
    assert any(f['check'] == 'instant' and f['severity'] == 'block' and f['selector'] == '#log' for f in data['findings'])


def test_occluded_content_is_not_instant():
    event = dict(selector='#log', time=20, instant=True, count=40, rect=[0, 0, 200, 100], narrative=True)
    assert motion.content_findings({'events': [event]})[0]['severity'] == 'block'
    assert not motion.content_findings({'events': [dict(event, occluded=True)]})  # Cleared under a loading screen.


def test_large_regions_and_first_paint():
    step = dict(name='click', frames=[{'time': 0}], events=[], narrativeRegions=[{'selector': '#scene', 'rect': [0, 0, 100, 100]}])
    large = motion.finding('instant', 'whole visible change in one captured frame', box=[10, 10, 60, 60], start=100, end=117)
    assert motion.region_findings([dict(large)], step, 10000)[0]['severity'] == 'block'
    assert not motion.region_findings([dict(large, box=[0, 0, 10, 10])], step, 10000)
    assert not motion.region_findings([dict(large, start=0)], {**step, 'name': 'load'}, 10000)
    assert not motion.region_findings([dict(large)], {**step, 'name': 'load', 'loadEnd': 90}, 10000)
    assert not motion.region_findings([dict(large)], {**step, 'name': 'load', 'loadEnd': 50, 'firstPaint': 90}, 10000)
    assert motion.region_findings([dict(large)], {**step, 'name': 'load', 'loadEnd': 50}, 10000)
    assert not motion.region_findings([dict(large)], {**step, 'events': [{'time': 110}]}, 10000)


def test_browser_content_cleared_under_overlay(browser_available, tmp_path):
    page = tmp_path / 'covered.html'
    page.write_text('''<!doctype html><style>#cover{position:fixed;inset:0;background:#000;z-index:9}</style>
<div id="log" data-motion="narrative"><p>Line one</p><p>Line two</p><p>Line three</p></div><div id="cover"></div><script>
setTimeout(() => { for (const p of log.children) p.textContent = ''; }, 400);
setTimeout(() => cover.remove(), 900);
</script>''')
    script = tmp_path / 'wait.mjs'
    script.write_text('export default async page => { await page.waitForTimeout(1200); };')
    out = tmp_path / 'result'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '200', '--seed', '1',
                           '--out', str(out), '--script', str(script), str(page)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((out / 'findings.json').read_text())
    assert not any(f['check'] == 'instant' and f['selector'] == '#log' for f in data['findings']), proc.stdout


def test_browser_attribute_fade_removal(browser_available, tmp_path):
    page = tmp_path / 'fade.html'
    page.write_text('''<!doctype html><style>
body{background:#111;color:#eee}#toast{transition:opacity 300ms ease}
#toast[data-leaving]{opacity:0}
</style><div id="toast"><b>Saved</b><p>All changes saved</p></div><script>
setTimeout(() => toast.setAttribute('data-leaving', ''), 400);
setTimeout(() => toast.remove(), 900);
</script>''')
    out = tmp_path / 'result'
    proc = subprocess.run([sys.executable, str(motion.HERE / 'motion.py'), '--duration', '1200',
                           '--viewport', '640x480', '--script', str(FIXTURES / 'observe.mjs'),
                           '--out', str(out), str(page)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads((out / 'findings.json').read_text())
    assert 'instant' not in checks(data['findings']), proc.stdout
