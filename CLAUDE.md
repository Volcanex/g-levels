# g-levels

**Owner:** Gabriel Penman **Host:** h **CLI:** `glevels` (or `python3 -m glevels`) **No service, no deps**

The registry for the making ladder: gates (L2), charters (L3), the results they
produce. The theory is in Volcanex/g-research
(`pieces/making-ladder.md`). The framework itself is the g-levels skill
(`gp-core/skills/g-levels/SKILL.md`). The explainer is gabrielpenman.com/charter.

## Layout

- `gates/<id>.toml`: one gate per file. Shared by every charter that pins it.
- `charters/<id>.toml`: scope (`repos`, per-gate `paths`), principles, the gates it
  pins by version, which block, and exceptions. `g-system` covers every repo.
  A narrower charter `extends` it and wins per gate.
- `glevels/`: registry loading and scope resolution (`registry.py`), the CLI (`cli.py`).
- `glevels/export.py`: a read-only JSON snapshot of the registry, results, backlog and level
  summary, for gcommand's /levels page; `project_view` exposes project charter and gate states.
  Grading still goes through `glevels grade`.
- `glevels/spacing.py`, `onsystem.py` (with `systems.json`, the house systems) and `assets.py`:
  the look sensors, each a Python CLI driving a `.mjs` Playwright half. They share spacing's
  page resolution, browser lock and memory guard. `vendor.py` pins and installs the maintained
  tools they marry (Impeccable, stylelint-declaration-strict-value) under `~/.cache/glevels/node`.
- `glevels/motion.py`, `frames.py` and `meshcheck.py`: the moving-picture sensors. `motion.py`
  (with `motion.mjs`) records a page's animations and reveals; `frames.py` (with `frames.mjs`)
  reads or captures consecutive frames, or runs a repo's `.glevels/frames.json` recipe;
  `meshcheck.py` checks exported geometry. `adapters/` holds per-project mesh exporters.
- `glevels/contentpiece.py`: the sensor for `correct.content.recipe-recorded`, checking each content-lab piece
  (`~/content-lab/pieces/<id>/`) against the lab's CONTRACT.md. The `content` charter scopes that lab by `dirs` and holds
  a `[[proposed_exceptions]]` block, which lint and scope resolution ignore until Gabriel approves it into `[[exceptions]]`.
- `glevels/idcast.py`: the shader-source sensor for `correct.engine.interpolated-id`, an
  int()/uint() cast of an unrounded vertex attribute in TSL node code.
- `var/results.jsonl`: the results log. Gitignored, because notes can carry private
  incident detail. Set `GLEVELS_LOG` to write elsewhere.

## Using it

    glevels check <path>          run every-run gates in scope (--all adds milestone and on-demand)
    glevels record <gate> <result> <path> --note "..."   report a review gate
    glevels status <path>         latest result per gate, silent ones flagged
    glevels counts                counts per charter, gate, repo
    glevels notes <gate>          the notes left at one gate
    glevels brief --charter C     gate guidance to paste into a subagent brief (--gate G for single gates)
    glevels brief --charter C --review PATH   the independent reviewer's brief for that work
    glevels sample                a few recent verdicts for Gabriel to grade (glevels grade <id> agree|disagree)
    glevels map                   the gate taxonomy and its links
    glevels lint                  validate the registry (run before committing here)

A command sensor passes on exit 0. By default, a command containing `{file}` runs once per
tracked file matching the charter's `paths`. A review sensor prints its rubric and
waits for `glevels record`. A review sensor may also carry a `run` command, its
mechanical half: `check` runs it and logs a catch before printing the rubric.
`cannot_run_exit` names the exit code that means the sensor could not run, logged
as `impossible` rather than a catch (ganti uses 2 for absolutes, so it is opt-in). `caught` is logged when a sensor fails, so the counts
show catches as well as final results.

### Page and screen inputs

A repo may name its running pages in `.glevels/pages.json`:

```json
{
  "serve": "python3 -m http.server {port}",
  "ready": "http://127.0.0.1:{port}/",
  "timeout": 20,
  "viewports": "phone=390x844,desktop=1440x900",
  "env": {"GSPACING_LOCK": "/tmp/g-studio-browser.lock"},
  "pages": [
    {"name": "Home", "url": "http://127.0.0.1:{port}/index.html"},
    {"name": "Settings", "url": "http://127.0.0.1:{port}/settings.html"}
  ],
  "siblings": ["Home", "Settings"],
  "capabilities": ["Account"]
}
```

`serve`, `ready` and a nonempty list of uniquely named `pages` are required.
`{port}` is a free loopback port and `{repo}` is the repository root. The serve command
runs in that root once per check. Readiness needs HTTP 2xx within `timeout` seconds
(default 20, maximum 300). Invalid inputs, a failed command or a readiness timeout
report impossible and fail the check for page gates. The entire server process group is stopped on
normal exit, errors, Ctrl-C, SIGTERM and SIGHUP. SIGKILL cannot run cleanup handlers.

Source paths still scope gates. For each gate in scope, a `{file}` command runs once per
named page URL, quoted for the shell. This includes every `{file}` gate, so do not add
this manifest to a repo whose charter pins a source-file sensor such as the copy gates.
The runtime, frames, parity, option-tree and screens sensors also get the server. Every
other gate never starts it, so a broken manifest cannot change its result. Without this
manifest the original per-file behavior is unchanged. `viewports` is optional and may be overridden per page.
Spacing and on-system read it through `GLEVELS_VIEWPORTS`. Other sensors keep their
existing viewports. No threshold or stress state is changed.

Every page gate receives `GLEVELS_URL` (all URLs, or the current page for a `{file}` run),
`GLEVELS_READY_URL`, `GLEVELS_REPO` (assets finds the repo's assets.toml with it), and
`GLEVELS_PAGES` (the expanded page list as JSON). Optional `env`
entries are strings with the same substitutions. `GLEVELS_RENDERED=1` makes claims and
parity capture visible text and controls with the existing browser walker before judging
URLs. Without it their existing static-input behavior stays unchanged. Optional `siblings`
names pages of the same kind for `GLEVELS_SIBLINGS`. Optional `capabilities` supplies
`GLEVELS_CAPABILITIES`. Listing unrelated screens as siblings gives misleading comparisons.

`.glevels/screens` is a shell command file run from the repo root by the first-run and
app-craft gates. It must save ordered screenshots and a manifest with each role's steps,
taps and elapsed time to home and a first useful result. Capture supported themes, empty,
loading and error states for app-craft. Exit 0 means the mechanical walk completed, 1
means a fault and 2 means it could not run. Screenshots still need independent review.
The harness owns its browser lock. Without this file, `app/test/screens.py` runs exactly
as before for Wing, with its existing environment and output folder.

`.glevels/frames.json` keeps the frame sensor's capture and mesh format. In a capture URL,
`{url}` expands to `GLEVELS_READY_URL` from pages.json. The recipe still needs a real
interaction script and consecutive frames. Use the shared `GSPACING_LOCK` for all page
sensor browser launches, including motion and frames.

## Gate conventions

These hold for every gate, in every charter.

- **Structure is strict, judgement is fuzzy.** Links, versions, pins, scopes, skills and
  exceptions have right answers, and `glevels lint` enforces them. What counts as met
  stays a judgement. Don't tune thresholds or automate pass criteria yet: tuning a
  fuzzy sensor hard teaches agents to satisfy the sensor.
- **Honesty is part of every sensor.** Each check ends with the same question: did the
  work meet the principle, or only the sensor? Recording `met` or `attempted` needs
  `--honest`, one line on how you know. Editing the check, threshold or test, not the
  work, is a catch.
- **Fail loud.** A hook that breaks still allows, but records `impossible` with the error.
- **A skill is a gate's guidance half.** Name it in `skill`. `glevels map` shows the whole
  taxonomy: kind, sensor type, trigger, skill and the charters that pin each gate.
- **Ids are `kind.domain.name`** (`correct.secrets.argv`, `design.product.feature-ideas`), and the file is `gates/<id>.toml`. Lint enforces both.
- **Gate kinds.** `correcting` gates have a right answer. `design` gates generate options
  and stop at saturation, checked by a probe run from a different strong model family.
- **Sensor types.** `command` (exit 0 passes), `review` (a rubric the agent answers, then
  `glevels record`), `hook` (enforced at tool time, outside `glevels check`).

## Who runs gates

Gates are for agents, not for chat with Gabriel. The orchestrator picks a charter (or
a set of gates, say for a night run), pastes `glevels brief` into each worker's brief,
and the worker runs command gates before reporting. Review gates are judged by a
separate agent from `glevels brief --review`, never the worker that did the work.
Gabriel sees impossible results and the counts, and grades a sample of verdicts
(`glevels sample`). The taste column in `glevels counts` is his agreement rate, the
calibration a fuzzy sensor gets without being tuned.

Charters mostly live at project level. `g-system` stays lean: compliance and drift.
Reusable charters such as `delivery` name no repos and are extended by project
charters.

## Writing a gate

Fields are checked by `glevels lint`: id, version, kind (`correcting` or `design`),
trigger (`every-run`, `milestone`, `on-demand`), optional skill, principle, guidance, sensor, pass,
remediation, origin, written_against. A design gate also names its generators,
judge, saturation rule, probe and budget. Test a new gate against the original failure, a
related one and clean work. Bump `version` when the sensor or pass criteria change,
then update the pins in charters.

## Non-obvious things

- **The repo is public (AGPL).** Origins are one-line public summaries. Private
  incident detail stays in memories, failures.md or the results log.
- **Gates never scope themselves.** Paths live in the charter's gate entry, so one
  gate serves several charters.
- **Several gates started as memories** (correct.secrets.argv, correct.hosts.work-server-quiet,
  correct.hosts.service-file-owner). A memory is read sometimes. A gate is checked and counted.
- **Design gates need strong generators.** Cheap fan-out saturates on the model's
  range, not on the space being explored. There is no design-gate runner yet.
- Placeholders are substituted by plain replace, not `str.format`, so shell `${VAR}`
  works in sensor commands.
- **`GLEVELS_ROOT` points the CLI at another registry** with this layout. Baseline's
  b-levels (gates for its client research sweeps) runs this CLI that way; the
  pointer charter `charters/baseline-sense.toml` says where it lives. A charter's
  `dirs` (absolute or `~/` globs, one path segment per glob part) scopes directories
  outside git, so `~/research/*` makes each sweep directory its own "repo".
