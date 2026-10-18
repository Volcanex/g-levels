# g-levels

**Owner:** Gabriel Penman **Host:** h **CLI:** `glevels` (or `python3 -m glevels`) **No service, no deps**

The registry for the making ladder: gates (L2), charters (L3), the results they
produce, and the research behind them. The framework itself is the g-levels skill
(`gp-core/skills/g-levels/SKILL.md`). The explainer is gabrielpenman.com/charter.

## Layout

- `gates/<id>.toml`: one gate per file. Shared by every charter that pins it.
- `charters/<id>.toml`: scope (`repos`, per-gate `paths`), principles, the gates it
  pins by version, which block, and exceptions. `g-system` covers every repo.
  A narrower charter `extends` it and wins per gate.
- `glevels/`: registry loading and scope resolution (`registry.py`), the CLI (`cli.py`).
- `research/`: theory pieces, with sources.
- `var/results.jsonl`: the results log. Gitignored, because notes can carry private
  incident detail. Set `GLEVELS_LOG` to write elsewhere.

## Using it

    glevels check <path>          run every-run gates in scope (--all adds milestone and on-demand)
    glevels record <gate> <result> <path> --note "..."   report a review gate
    glevels status <path>         latest result per gate, silent ones flagged
    glevels counts                counts per charter, gate, repo
    glevels notes <gate>          the notes left at one gate
    glevels lint                  validate the registry (run before committing here)

A command sensor passes on exit 0. A command containing `{file}` runs once per
tracked file matching the charter's `paths`. A review sensor prints its rubric and
waits for `glevels record`. `caught` is logged when a sensor fails, so the counts
show catches as well as final results.

## Writing a gate

Fields are checked by `glevels lint`: id, version, kind (`correcting` or `design`),
trigger (`every-run`, `milestone`, `on-demand`), principle, guidance, sensor, pass,
remediation, origin, written_against. A design gate also names its generators,
judge, saturation rule and budget. Test a new gate against the original failure, a
related one and clean work. Bump `version` when the sensor or pass criteria change,
then update the pins in charters.

## Non-obvious things

- **The repo is public (AGPL).** Origins are one-line public summaries. Private
  incident detail stays in memories, failures.md or the results log.
- **Gates never scope themselves.** Paths live in the charter's gate entry, so one
  gate serves several charters.
- **Several gates started as memories** (secrets-out-of-argv, work-host-quiet,
  service-owner-files). A memory is read sometimes. A gate is checked and counted.
- **Design gates need strong generators.** Cheap fan-out saturates on the model's
  range, not on the space being explored. There is no design-gate runner yet.
- Placeholders are substituted by plain replace, not `str.format`, so shell `${VAR}`
  works in sensor commands.
