"""Run a repo's depth-order sensor, named by .glevels/depth-order.json.

The manifest's script is relative to the repo. Its default run must check all
scenes, run regressions and compare baked bytes with committed assets.
"""
import json
from pathlib import Path
import subprocess
import sys


def check(repo):
    repo = Path(repo).resolve()
    try:
        manifest = json.loads((repo / '.glevels/depth-order.json').read_text())
        name = manifest.get('script')
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            raise ValueError('depth-order manifest needs a repo-relative script')
        script = (repo / name).resolve()
        if not script.is_relative_to(repo) or not script.is_file():
            raise ValueError('depth-order sensor script missing or outside repo')
        return subprocess.run(['node', str(script)], cwd=repo).returncode
    except (OSError, ValueError, AttributeError) as error:
        print(f'impossible: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(check(sys.argv[1]))
