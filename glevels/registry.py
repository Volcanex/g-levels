import datetime
import os
import re
import fnmatch
import subprocess
import tomllib
from pathlib import Path

# GLEVELS_ROOT points the CLI at another registry with the same layout (gates/, charters/), such as
# Baseline's b-levels for Sense sweeps.
ROOT = Path(os.environ.get("GLEVELS_ROOT") or Path(__file__).resolve().parent.parent)
RESULTS = ("met", "attempted", "waived", "impossible")
GATE_FIELDS = ("id", "version", "kind", "principle", "guidance", "sensor", "pass", "remediation", "origin", "written_against")
KINDS = ("correcting", "design")
TRIGGERS = ("every-run", "milestone", "on-demand")
SENSORS = {"command": "run", "review": "rubric", "hook": "hook"}
DESIGN_FIELDS = {"generators", "judge", "saturation", "probe", "budget"}
SKILLS = Path(os.environ.get("GLEVELS_SKILLS", Path.home() / ".agents" / "skills"))
# The universal half of every sensor. Structure is strict, judgement stays fuzzy, and this
# question is the one judgement every gate shares.
HONESTY = ("Did the work meet the principle, or only the sensor? If you changed the check, the "
           "threshold, the test or what it reads instead of the work, that is a catch, not a pass.")


PREFIX = {"correcting": "correct", "design": "design"}
ID = re.compile(r"^(correct|design)\.[a-z0-9-]+\.[a-z0-9-]+$")


def load_gates(root=ROOT):
    gates = {}
    for p in sorted((root / "gates").glob("*.toml")):
        g = tomllib.loads(p.read_text())
        g["_file"] = p.stem
        gates[g["id"]] = g
    return gates


def load_charters(root=ROOT):
    return {c["id"]: c for c in (tomllib.loads(p.read_text()) for p in sorted((root / "charters").glob("*.toml")))}


def repo_of(path, charters=None):
    """The git top of path. Outside git, the directory a charter's dirs glob names (so ~/research/svea/x
    is the svea sweep under dirs = ["~/research/*"]), else the path itself."""
    path = Path(path).resolve()
    try:
        top = subprocess.run(["git", "-C", str(path if path.is_dir() else path.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(top)
    except subprocess.CalledProcessError:
        root = dir_root(path, charters or {})
        return root or (path if path.is_dir() else path.parent)


def _dir_globs(charter):
    return [Path(os.path.expanduser(d)) for d in charter.get("dirs", [])]


def dir_root(path, charters):
    """The ancestor of path (or path) that some charter's dirs glob matches, one segment per glob part."""
    parts = Path(path).parts
    for c in charters.values():
        for g in _dir_globs(c):
            n = len(g.parts)
            if len(parts) >= n and fnmatch.fnmatch(str(Path(*parts[:n])), str(g)):
                return Path(*parts[:n])
    return None


def main_name(repo):
    """A linked worktree's charter is its main repo's: wing-wt-aero is wing."""
    try:
        common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return repo.name
    common = Path(common)
    return common.parent.name if common.name == ".git" else repo.name


def chain(charter_id, charters):
    """The charter and everything it extends, broadest first."""
    out, seen = [], set()

    def walk(cid):
        if cid in seen:
            return
        seen.add(cid)
        for parent in charters[cid].get("extends", []):
            walk(parent)
        out.append(charters[cid])

    walk(charter_id)
    return out


def charter_for(repo_name, charters, repo=None):
    """The narrowest charter naming this repo, else one whose dirs name this directory, else one that
    covers every repo."""
    named = [c for c in charters.values() if repo_name in c.get("repos", [])]
    if repo is not None:
        named += [c for c in charters.values() if any(len(g.parts) == len(Path(repo).parts)
                  and fnmatch.fnmatch(str(repo), str(g)) for g in _dir_globs(c))]
    if named:
        return max(named, key=lambda c: len(chain(c["id"], charters)))["id"]
    wild = [c for c in charters.values() if "*" in c.get("repos", [])]
    return wild[0]["id"] if wild else None


def covers(rel, globs):
    """True if rel matches a glob, or is a directory some glob reaches into."""
    if not rel:
        return True
    parts = rel.split("/")
    for g in globs:
        if fnmatch.fnmatch(rel, g):
            return True
        gp = g.split("/")
        if "**" in gp[:len(parts)] or (len(gp) > len(parts) and all(fnmatch.fnmatch(a, b) for a, b in zip(parts, gp))):
            return True
    return False


def in_scope(repo, rel, charters):
    """Gate entries that apply at repo/rel, with the narrowest charter winning per gate."""
    cid = charter_for(main_name(repo), charters, repo)
    if cid is None:
        return None, []
    entries = {}
    exceptions = []
    for c in chain(cid, charters):
        for e in c.get("gates", []):
            entries[e["id"]] = dict(e, charter=c["id"])
        exceptions += [dict(x, charter=c["id"]) for x in c.get("exceptions", [])]
    scoped = []
    for e in entries.values():
        if not covers(rel, e.get("paths", ["**"])):
            continue
        waiver = next((x for x in exceptions if x["gate"] == e["id"]
                       and x.get("repo", repo.name) in (repo.name, "*")
                       and covers(rel, x.get("paths", ["**"]))), None)
        scoped.append(dict(e, waiver=waiver))
    return cid, scoped


def lint(gates, charters, today=None):
    today = today or datetime.date.today().isoformat()
    problems, warnings = [], []
    for gid, g in gates.items():
        if not ID.match(gid):
            problems.append(f"gate {gid}: id must be kind.domain.name, kind correct or design")
        elif gid.split(".")[0] != PREFIX.get(g.get("kind")):
            problems.append(f"gate {gid}: id prefix does not match kind {g.get('kind')}")
        if g.get("_file", gid) != gid:
            problems.append(f"gate {gid}: file must be named {gid}.toml")
        missing = [f for f in GATE_FIELDS if f not in g]
        if missing:
            problems.append(f"gate {gid}: missing {', '.join(missing)}")
        if g.get("kind") not in KINDS:
            problems.append(f"gate {gid}: kind must be one of {KINDS}")
        if g.get("trigger", "every-run") not in TRIGGERS:
            problems.append(f"gate {gid}: trigger must be one of {TRIGGERS}")
        if g.get("kind") == "design" and not DESIGN_FIELDS <= set(g):
            problems.append(f"gate {gid}: a design gate names its {', '.join(sorted(DESIGN_FIELDS))}")
        s = g.get("sensor", {})
        if s.get("type") not in SENSORS:
            problems.append(f"gate {gid}: sensor type must be one of {tuple(SENSORS)}")
        elif SENSORS[s["type"]] not in s:
            problems.append(f"gate {gid}: {s['type']} sensor needs {SENSORS[s['type']]}")
        if "skill" in g and SKILLS.is_dir() and not (SKILLS / g["skill"] / "SKILL.md").exists():
            problems.append(f"gate {gid}: skill {g['skill']} not found in {SKILLS}")
    pinned = {e["id"] for c in charters.values() for e in c.get("gates", [])}
    for gid in gates:
        if gid not in pinned and gates[gid].get("kind") != "design":
            warnings.append(f"gate {gid}: no charter pins it, so it never fires")
    for cid, c in charters.items():
        for d in c.get("dirs", []):
            if not isinstance(d, str) or not d.startswith(("/", "~/")):
                problems.append(f"charter {cid}: dirs entry {d!r} must be an absolute or ~/ path glob")
        for parent in c.get("extends", []):
            if parent not in charters:
                problems.append(f"charter {cid}: extends unknown {parent}")
        for e in c.get("gates", []):
            g = gates.get(e["id"])
            if g is None:
                problems.append(f"charter {cid}: unknown gate {e['id']}")
            elif e.get("version") != g.get("version"):
                problems.append(f"charter {cid}: pins {e['id']} v{e.get('version')}, registry has v{g.get('version')}")
        for x in c.get("exceptions", []):
            missing = [f for f in ("gate", "reason", "review", "approved_by") if f not in x]
            if missing:
                problems.append(f"charter {cid}: exception for {x.get('gate')} missing {', '.join(missing)}")
            elif x["gate"] not in {e["id"] for k in chain(cid, charters) for e in k.get("gates", [])}:
                problems.append(f"charter {cid}: exception for {x['gate']}, which no charter in its chain pins")
            elif str(x["review"]) < today:
                warnings.append(f"charter {cid}: exception for {x['gate']} was due for review {x['review']}")
    return problems, warnings
