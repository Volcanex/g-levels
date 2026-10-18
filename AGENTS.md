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

A command sensor passes on exit 0. A command containing `{file}` runs once per
tracked file matching the charter's `paths`. A review sensor prints its rubric and
waits for `glevels record`. `caught` is logged when a sensor fails, so the counts
show catches as well as final results.

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
