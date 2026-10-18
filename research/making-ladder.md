# The making ladder: gates, charters and where the compute goes

Gabriel Penman's g-system, research note 1. Draft of 2026-10-18.

## Summary

The making ladder is a way of describing how a person who builds with AI agents moves their own effort upward. At each rung the person stops doing a kind of work and starts designing the thing that does it. L-1 is making the artefact. L0 is designing how it gets made (one source of truth, explicit coupling). L1 hands the L0 work to agents, and the person designs their briefs and skills. L2 adds gates that check agent output. L3 adds charters that hold gates with a scope and exceptions, and a ratchet that turns every leak into a new gate. L4, not built, would generate charters.

This note places the ladder in its lineage and says what is new, proposes a taxonomy of gates (error-correcting gates converge, design gates diverge and stop at saturation), sets out how gates are formed, tuned and held by charters, proposes roles for Brain, skills, g-levels and research, and lists the critiques and what to measure.

Claims are tagged where it matters. **Established** means the literature or the system's own records show it. **Argued** means it follows from reasoning I think is sound but have not tested. **Conjecture** means a guess worth testing.

## 1. The ladder in one page

Each level does the work of the one below, and the person designs one step up. Every level has two kinds of work: design, which decides how the level below gets made, and error correction, which finds what went wrong and stops it coming back. Climbing a rung hands error correction from the person to a mechanism.

A **gate** checks one principle for one class of flaw. It has two halves. The guidance is what the working agent reads before it starts. The sensor checks the result afterwards. Every gate in scope reports one of four results: `met`, `attempted`, `waived` or `impossible`. Silence is the only failure. A **charter** is a scope, the principles that hold there, the gates that check them, which of those block, and the exceptions. Gates never scope themselves. Counts go up to the charter, and notes stay at the gate.

The **fix strength** ladder ranks responses to a flaw: prompt (this output only), memory (read sometimes), skill (loaded when the topic comes up), contract (bad input cannot cross a stage boundary), gate (checked every run, result recorded). The rule is to use the strongest fix that is cheap enough.

Two features carry most of the weight. The first is the result vocabulary. Most checking systems are binary. Here `attempted` and `impossible` make partial compliance and principled refusal into reportable outcomes, which removes the pressure to fake a binary pass and shows the person where rules strain (argued). The second is the insistence that silence is the failure. A gate that does not report is treated as a broken pipeline, not as a pass.

## 2. Lineage

Almost every piece of the ladder has a precedent. The combination, and a few specific moves, are new.

### 2.1 Levels of learning and of improvement

Bateson's "The Logical Categories of Learning and Communication" orders learning into logical types [1]. Learning I corrects a choice within a set of alternatives. Learning II is change in the process of Learning I, learning to learn, which he had earlier called deutero-learning [2]. Learning III is change in the process of Learning II. The ladder's "design one step up" is a practical version of the same move. At L1 the person corrects outputs (Learning I). At L2 and L3 they change the process that produces corrections (Learning II). **Established** that the structure is Batesonian. **Argued** that the mapping is useful rather than decorative: it predicts that the hard step is the one where the person must stop correcting outputs and start correcting the correcting, which matches the explainer's own diagnosis of why people get stuck at L2.

Argyris and Schön turned this into a theory of professional and organisational practice [3][4]. Single-loop learning changes actions while keeping the governing variables, as a thermostat holds a temperature. Double-loop learning changes the governing variables, as when someone asks whether the setting is right. In 1978 they took deutero-learning from Bateson to mean that "the organization needs to learn how to carry out single- and double-loop learning". The ratchet is a double-loop device. A leak does not just get fixed (single loop). It prompts the question of why no gate caught it (double loop), and charter review asks whether the review itself works (deutero). Argyris's later point that "defensive reasoning can block learning even when the individual commitment to it is high" [5] has an agent-era analogue: an agent that acknowledges a complaint and changes nothing, which the Newcastle failure record describes in exactly those terms (section 9).

The closest precedent for "each level does the work of the one below" is Engelbart's A, B and C activities [6]. A is the organisation's core work. B improves the capability to do A. C improves the capability to do B. Engelbart called this bootstrapping. The ladder has more rungs and a different substrate, but the shape is his. I had not seen this precedent cited in the g-system before, and it is the one to read first.

The name "ladder" also echoes Victor's ladder of abstraction, which is about moving between levels to understand a system [7]. The resemblance is in the image only: Victor's rungs are views of one system, the making ladder's rungs are who does the work.

Meadows's leverage points rank places to intervene in a system, from parameters at the weak end through feedback loops, information flows, rules and self-organisation up to goals and paradigms [8]. Gates sit at the "rules" and "information flows" points, charters at "self-organisation". Her list is a reminder that the ladder does not reach the top places. It assumes the goal is given.

### 2.2 Toyota: poka-yoke, jidoka, kaizen

Shingo's poka-yoke is mistake-proofing at the source, so a defect cannot be made or cannot pass to the next process [9]. The fix-strength rung called "contract" is poka-yoke for agent pipelines. Jidoka, "automation with a human touch", means a machine stops itself at an abnormality, and on Toyota's lines "the operator can stop the line by pulling the stop cord" [10][11][12]. A blocking gate is jidoka. Kaizen is continuous incremental improvement [13], and the ratchet is kaizen with a record: every leak improves the process a little, and the improvement is written down so it does not decay.

What Toyota adds that the ladder underplays is the stop. Jidoka works because stopping the line is cheap and normal and because the fix happens at once. A gate that only records and never sends work back is closer to inspection after the fact, which Shingo's source inspection was meant to replace.

### 2.3 Fitness functions

Ford, Parsons and Kua write that "an architectural fitness function provides an objective integrity assessment of some architectural characteristic(s)" [14]. Their first edition sorts them as atomic or holistic, triggered or continual, static or dynamic, automated or manual, temporal, intentional over emergent, and domain-specific. A g-system gate is a fitness function with three additions: guidance as a first-class half, the four-valued result, and an origin incident. The g-brain repository already contains a pure fitness function in their sense: `tests/test_boundaries.py` checks every import against the component table, so a new cross-component import fails the suite. That test is a gate in all but registration.

### 2.4 Harness engineering, 2026

In 2026 "harness engineering" became the name for designing the environment around coding agents: the instructions, tools, checks and feedback loops. Ryan Lopopolo's OpenAI article of 11 February 2026 is often cited as the origin, and the attribution of that article to him is correct [15]. The coinage is not his. Mitchell Hashimoto used the phrase six days earlier, saying he had "grown to calling this harness engineering" [16], Osmani credits Viv Trivedy with coining it [17], and a slide from a November 2025 briefing credits Dex Horthy (that copy was last updated in January 2026, so its date is not certain) [18]. No single coiner can be established. The version closest to this framework is Birgitta Böckeler's article on martinfowler.com, which in April 2026 split the harness into guides, "feedforward controls" that "anticipate the agent's behaviour", and sensors, "feedback controls" that "observe after the agent acts" [19][20]. That is the gate's guidance and sensor, under the same names. The academic literature followed. Agentic Harness Engineering evolves harness components automatically against benchmark feedback and reports Terminal-Bench 2 pass@1 rising from 69.7% to 77.0% over ten iterations [21]. Meta-Harness searches over harness code with a proposer that reads earlier candidates' code, scores and logs [22]. Wang and colleagues then showed that many such results lack matched-budget baselines and draw their verification signal from the same benchmark used for final reporting, and on Terminal-Bench 2.1 evolution failed to beat simple test-time scaling [23]. That last paper is the Goodhart problem (section 9) appearing inside harness research itself.

The ladder is a personal-scale theory of harness engineering. Its L1 is the harness as instructions, its L2 and L3 are the harness as checks. What it adds is the account of how checks accumulate (the ratchet), how they are scoped (charters), and how they report (four results, counts up, notes down).

### 2.5 Measures that become targets

Goodhart's law in its original form says "any observed statistical regularity will tend to collapse once pressure is placed upon it for control purposes" [24]. Strathern's general form, "when a measure becomes a target, it ceases to be a good measure", is the one usually quoted [25]. She credits the idea to Goodhart by way of Hoskin. Campbell's version says the more a quantitative indicator is used for social decisions, "the more subject it will be to corruption pressures", and the more it will "distort and corrupt the social processes it is intended to monitor" [26]. Manheim and Garrabrant separate regressional, extremal, causal and adversarial variants [27]. Every sensor in the ladder is a measure, and every agent that can see a sensor is under pressure to satisfy it. Section 9 takes this up.

### 2.6 Cybernetics and the Viable System Model

Ashby's law of requisite variety says that "only variety in R can force down the variety due to D": a regulator can only absorb as much variety in the disturbances as it has itself [28]. Beer's Viable System Model divides a viable organisation into five systems: operations (1), coordination (2), control including the sporadic audit channel 3* (3), intelligence about the environment and the future (4), and policy and identity (5), with the whole structure recursing at every level [29]. Read through Beer, gates are System 3 and 3*, charters are System 3 with some System 5, and the ratchet is a weak System 4. The VSM's warning is about variety. A person acting as the only sensor for every class of flaw cannot match the variety of what a fleet of agents produces, which is a formal way of saying why L2 stalls when the person is the sensor.

### 2.7 Policy as code

Open Policy Agent decouples policy decisions from enforcement and expresses policy in a declarative language (Rego) [30]. HashiCorp Sentinel attaches enforcement levels to policies: advisory, soft-mandatory (can be overridden) and hard-mandatory [31]. These map closely onto the ladder's non-blocking, overridable and blocking gates. The ladder's `waived` result is Sentinel's override with the reason recorded at charter level rather than per run. Claude Code hooks provide the enforcement point for agent tool calls: a PreToolUse hook can deny a call and return a reason the model reads [32].

### 2.8 Constitutions and model specs

Constitutional AI trains a model with a short list of written principles, using model-generated critiques and revisions against those principles [33]. OpenAI's Model Spec of May 2024 ordered authority as "Platform > Developer > User > Tool", and its 2026 edition says "instructions with higher authority override those with lower authority" [34]. Anthropic's constitution for Claude, published in January 2026, says Anthropic "generally favor[s] cultivating good values and judgment over strict rules and decision procedures", while keeping a small set of hard constraints [35]. These are charter-like objects: a scope, principles, an order for conflicts, and exceptions. They differ from charters in one important way. They act through training, so there is no per-run result. A charter acts at run time and counts. A constitution is guidance without a sensor, which is the rung-3 skill in fix-strength terms, applied very powerfully.

### 2.9 Prompt and program optimisation

DSPy compiles declarative language-model pipelines by optimising prompts and demonstrations against a metric [36]. OPRO uses a model as the optimiser over prompts [37]. GEPA evolves prompts through natural-language reflection on execution traces and reports outperforming the GRPO reinforcement-learning method on its tested tasks [38]. Self-Refine and Reflexion loop a model's own feedback into revision [39][40]. Anthropic's "evaluator-optimizer" workflow is the same loop described as an engineering pattern [41]. All of these are L2 machinery: a sensor that drives revision until the criterion is met. They automate tuning the guidance half of a gate against a fixed sensor. None of them say where the sensor comes from, how it is scoped, or what to do when it is wrong, which is the L3 question.

### 2.10 Generate and select

Design gates have their own lineage. Guilford separated the two: "divergent production is a matter of generating logical possibilities, convergent production is a matter of generating logical necessities" [42]. Campbell, the same Campbell as the law, described creative thought as blind variation and selective retention [43]. The Design Council's Double Diamond, shared from 2004, alternates diverging and converging phases [44]. FunSearch and AlphaEvolve pair a language-model generator with an automated evaluator and keep the best programs [45][46]. With repeated sampling, the share of problems solved by some sample often grows log-linearly with the number of samples, but without a verifier, voting and reward-model selection plateau and miss most of the gain [47]. The saturation stopping rule comes from ecology and statistics: species-accumulation curves [48], Good's sample-coverage estimate [49], Chao's lower-bound estimator [50], capture-recapture [51], and its use in software inspection to estimate defects not yet found [52][53]. Qualitative research uses the same idea as data saturation [54].

### 2.11 What is new

**Not new:** levels of learning, the double loop, improving the improvement process, mistake-proofing, stopping the line, fitness functions, policy as code, enforcement levels, generate-and-select, and saturation estimators.

**New, as far as I can find (argued):**

1. *The four-valued result with silence as the only failure.* Fitness functions and policies pass or fail. Sentinel's advisory level lets a failing policy through with a warning, but the outcome is still pass or fail. Making `attempted` and `impossible` first-class, and treating a missing report as a pipeline bug, is a distinct design. It makes the sensor's coverage observable.
2. *Counts up, notes down.* Results aggregate to the charter as counts per gate per scope, while the qualitative notes stay at the gate for the next agent working there. This is a specific information architecture for agent pipelines.
3. *A gate's origin and model.* Guidance plus sensor is not new: it is Böckeler's guides and sensors. What the ladder adds is binding the pair into one versioned object with an origin incident and a "written against" model, which makes retirement after a model change a defined procedure (replay the incident) rather than a judgement call.
4. *The fix-strength ladder as a decision rule.* Ranking prompt, memory, skill, contract and gate by durability, and choosing the strongest fix that is cheap enough, is simple but I have not found it stated this way.
5. *Design gates with saturation stopping.* Generate-and-select is old. Treating it as a gate type, stopped by a measured saturation curve and reported with the same vocabulary as correcting gates, is the newest part of the framework and the least tested (section 4).
6. *The ladder as a personal practice.* Engelbart and Beer wrote about organisations. Here the organisation is one person and a fleet of agents, which changes the economics. Writing a gate costs minutes, so the ratchet can turn daily.

## 3. A taxonomy of gates

### 3.1 Dimensions

**Direction.** An *error-correcting* gate is convergent. It asks whether the work meets a principle, and the answer is one of the four results. Running it twice should give the same result, and when the sensor is stochastic (an agent review), repeats only reduce variance around one verdict. A *design* gate is divergent. It asks what could be added or changed ("what feature would this benefit from?"). Its output is a set of ideas, and more runs give a larger set. The two need different stopping rules and different staffing.

**Mechanism.** From most to least mechanical:
- *deterministic*: a regex, lint, type check, import check or test. Cheap, exact, narrow.
- *calibrated statistical*: a measured score against a reference distribution, with a threshold set from data. ganti's loudness score against Gabriel's own corpus is the example.
- *judged*: an agent with a stated rubric. Covers fuzzy principles but adds judge error.
- *human*: the person. Most expensive and least scalable, and at L2 the default sensor for everything not yet gated.

**Cost per run.** Near zero (a hook regex), seconds (a test suite), an agent call (cents to a dollar), many agent calls (a design gate run to saturation), and human attention.

**Trigger.** Per tool call (PreToolUse hooks), per run or commit, at milestones (before deploy, before a release, at the end of a sprint), continual (timers and probes), and on demand. Ford and colleagues' triggered and continual distinction covers most of this.

**Blocking.** *Hard*: the work cannot proceed (a denied tool call). *Overridable*: blocked unless an exception or approval line is present. *Advisory*: the agent sees the finding and proceeds. *Recorded*: no one sees it until charter review.

**Stopping rule.** Deterministic gates run once. Judged correcting gates run once or k times until verdicts agree. Design gates run until saturation or budget.

**Failure behaviour.** What happens when the sensor itself breaks: *fail open* (allow), *fail closed* (deny) or *fail loud* (allow but report `impossible` with the error). The ladder's rule that silence is the only failure implies fail loud.

### 3.2 Where the existing g-system gates fall

| Gate | Direction | Mechanism | Trigger | Blocking | On sensor error |
|---|---|---|---|---|---|
| ganti copy gate (PreToolUse on Write/Edit to copy paths) | correcting | deterministic absolutes, calibrated loudness | per tool call | hard for absolutes, advisory for loudness, bypass by comment | fails open |
| agent-gate (PreToolUse on Agent) | correcting (policy) | deterministic | per tool call | overridable by an "Operator approved:" line | fails open on stale pace data |
| g-brain `test_boundaries.py` | correcting | deterministic | per test run | blocks the suite | fails loud (test error) |
| ganti `tests/test_score.py` (corpus never trips absolutes) | correcting, on the sensor itself | deterministic | per test run | blocks the suite | fails loud |
| g-site venv pytest | correcting | deterministic | milestone | by convention | fails loud, but only with the right interpreter |
| gcommerce `pool promote`, `content review`, `write_brief` | correcting | deterministic thresholds over evidence, frozen rubric | per stage boundary | hard (contract) | fails closed |
| brain-status L5 probes with heal-before-page | correcting | deterministic probes | continual | n/a (heals, then pages) | pages |
| g-gcore-comply (the four compliance boxes) | correcting | mixed, run by a skill | on demand | none | not recorded |
| NOAD forge: crop, value and rule gates, then a pairwise judge, then owner review | correcting then design-like selection | deterministic, then judged, then human | per generated game | 3 failed repairs escalate | escalates |
| format-on-save | not a gate (an automatic fixer) | deterministic | per write | n/a | n/a |

Three observations. First, both PreToolUse gates in the harness settings fail open, which contradicts the framework's own rule. Claude Code treats most hook errors as non-blocking [32], and both hooks are written to allow when their own inputs fail: the ganti adapter swallows every exception, and agent-gate allows on unreadable input or missing pace data. A broken ganti import or stale pace data produces no denial and no report, which is silence. The cheap fix is fail loud: allow, but write an `impossible` result with the error. **Argued.**

Second, until 18 October 2026 the gates existed with no registry, so none of them reported counts to a charter. ganti logged metadata to `var/scores.jsonl`, and the others reported only by blocking. That day a registry, g-levels, was committed on h. It holds seven gates (six correcting, one design) and two charters. `g-system` covers every repo and `g-site` extends it. The charters pin gate versions, mark `no-secrets` and `secrets-out-of-argv` as blocking, and carry one exception with a reason, an approver and a review date. A gitignored `var/results.jsonl` collects results, and it logs `caught` whenever a sensor fails, so counts show catches as well as final results. At the time of writing it held four rows. The hooks in the table above are not yet wired to it. L3 now has a place to steer from, but almost nothing to steer by yet. That was the explainer's own diagnosis, and it still applies.

Third, there are no design gates in production. g-levels registers one, `feature-ideas`, with Opus-class generators, a separate judge, a saturation rule (stop when a round of three runs adds fewer than one new idea after merging) and a four-round budget, but its own origin field says it has not run and no runner exists. The NOAD forge's pairwise judge is the closest thing in use, and it selects between variants rather than generating ideas.

## 4. Design gates

This is the newest idea in the framework and the one that needs the most care.

### 4.1 What they are

A design gate poses a generative question about an artefact and returns a shortlist. Examples: "think of a feature this tool would benefit from", "what would make this page read less generated", "what classes of failure has this test suite never exercised", "which existing gate would have caught this incident if its scope were wider". The output is not a verdict on the work. It is a set of candidate changes.

The difference from a correcting gate is structural. A correcting gate has a fixed target, and once the work meets it, more compute buys nothing. A design gate samples from a space of possible ideas. Each run can add ideas the others missed, so the union grows with compute, and the quality of the best idea found tends to rise with it. This is the same shape as repeated sampling on hard problems, where coverage keeps rising with samples while a verifier picks the winner [47]. **Established** for verifiable tasks, **argued** for design.

### 4.2 The pipeline

1. **Frame.** Write the generative question and a few distinct framings of it (different user personas, different constraints, a request for the opposite of the obvious, an analogy domain). Framings are the main source of diversity.
2. **Generate.** Run generators, each producing a handful of ideas per run, rotating framings.
3. **Deduplicate.** A judge clusters ideas that are the same idea in different words. What counts as "the same" is a granularity choice and must be stated, because every count downstream depends on it.
4. **Track saturation.** After each run, record how many distinct ideas are new.
5. **Stop** when new distinct ideas per run fall below a threshold across framings, or at the budget cap.
6. **Rank.** A strong judge ranks the distinct ideas against a stated rubric.
7. **Pick.** The person picks. The gate's job ends at a ranked shortlist with the saturation curve attached.

Results map onto the shared vocabulary. `met`: saturation reached and a shortlist delivered. `attempted`: budget ran out before saturation, with the curve showing how far it got. `impossible`: nothing viable surfaced (itself informative). `waived`: the charter excludes this scope.

### 4.3 Saturation as a stopping rule

Let D(n) be the number of distinct ideas after n runs. The yield of the next run is D(n+1) minus D(n). The simplest rule stops when the yield over the last k runs is below a threshold. Ecology offers sharper tools. Good's estimate says that the number of ideas seen exactly once, divided by the total number of ideas generated (duplicates included), estimates the probability that the next idea is new [49]. Chao's estimator adds a correction from singletons and doubletons to estimate how many distinct ideas exist in total, as a lower bound [50]. With two independent generators, capture-recapture estimates the total from the overlap: if one finds n1 ideas, the other n2, and m are shared, the total is about n1 n2 / m [51]. Software inspection used exactly this to estimate how many defects reviewers had missed and so decide "whether the inspection can stop or whether it should continue", with the warning that "when the number of inspectors is too small, no model is sufficiently accurate" [52][53]. A design gate with three generators is in that regime.

The estimators come with assumptions that design work breaks. Ideas are not equally catchable: obvious ideas appear in every run and the valuable ones are rare, so estimates are biased low, which is why Chao framed hers as a lower bound. The population is not closed: the judge's dedup granularity changes the count. And independence between "traps" is the assumption that matters most, which brings us to Gabriel's correction.

### 4.4 Saturation is relative to the generator

Gabriel corrected the first version of this idea: fanning a design gate out to cheap agents is a mistake. Design gates are taste work. Weak models produce more of the same mediocre ideas, and the saturation curve then flattens early for the wrong reason. The model's range is exhausted, not the space of good ideas.

This is a statement about catchability. Capture-recapture assumes the two samples are independent draws from the same population. Two runs of one weak model are highly correlated draws from a small sub-population (the ideas that model can produce). The overlap m is large, so n1 n2 / m is small, and the estimate reports that the space is nearly exhausted when only the generator is. More runs of the same generator make the estimate more confident and no more correct. **Argued**, from the estimator's assumptions.

The literature supports the shape of the correction, with one complication. Si, Yang and Hashimoto had one model (Claude 3.5 Sonnet) generate 4,000 seed research ideas per topic and report "only 200 non-duplicate unique ideas", with the accumulated count "eventually" plateauing [55]. Their duplicate rule was a cosine similarity above 0.8, so the plateau belongs to that model, that prompt and that threshold, which is the point. Meincke, Mollick and Terwiesch found GPT-4 ideas "less diverse than ideas generated by groups of human subjects", and that chain-of-thought prompting gave the most diverse ideas [56]. Doshi and Hauser found that AI help raised individual creativity ratings while the resulting stories were "more similar to each other" [57]. Jiang and colleagues report homogeneity across different models too, though a 2026 reanalysis disputes their baselines [58][59].

The complication is in Terwiesch and colleagues' 2026 study of product ideas [60]. Growing a GPT-4o pool from 100 to 1,000 ideas kept improving its coverage of a human idea set, and they found "no sign of plateauing within the range tested". They also found that "more recent models are capable of generating more diverse ideas", but the ordering was not simple: o3-mini produced the least diverse set of the models they tested. So the generator's range is real and differs by model, but it does not line up neatly with general capability. **Established** that diversity is model- and prompt-dependent. **Argued** that saturation measured on one generator is a property of that generator. **Unverified**: no study I found compares saturation ceilings across models ordered by capability.

Design consequences:

- **Generators must be taste-capable.** The g-system already measures taste as a model's Design Arena Elo beyond what its general benchmark score predicts. That puts Opus at +6.3 and Sol at -11.9, and the router sends design work to Opus, to Astra only when Claude quota is very low, and never to Sol (memories `taste_metric` and `ui_needs_claude`). Design gates should run on Opus-class generators. **Argued**: Design Arena rates built interfaces, not ideas, so its fit to ideation is itself an assumption to check against the person's picks.
- **Diversity comes from framings and from model families, not from more runs.** A few strong generators with distinct framings beat many runs of one. Treat each framing, and each model family, as a different trap type. Do not assume two families are independent traps, since models trained on similar data may share blind spots. Measure overlap between them instead.
- **Saturation is reported with its generator.** "Saturated across Opus with four framings" is a claim about that generator set, not about the space.
- **Probe after apparent saturation.** When the curve flattens, run one different strong generator or a new framing. A fresh burst of new ideas means the earlier flattening was generator saturation. No burst is weak evidence of real saturation.
- **Cheap fan-out has two proper uses.** It suits correcting checks (many cheap sensors, best-of-two verification) and gathering raw material that feeds the strong generator's context (reference examples, competitor features, incident excerpts). It does not suit generating the ideas themselves.

### 4.5 The judge

Design gates depend on two judge calls: dedup and rank. Both are fuzzy and both are biased. Zheng and colleagues documented position, verbosity and self-enhancement biases in model judges [61]. Sternlicht and colleagues found model judges of novelty so sensitive to prompt wording that accuracy sometimes fell below chance, and that a cheap prompted baseline beat two purpose-built novelty evaluators [62]. Shankar and colleagues found that people's evaluation criteria change as they grade outputs ("criteria drift"), so a judge cannot be fully specified in advance [63].

Mitigations, all **argued**: a frozen anchor set of ideas the person has already judged, which the judge re-scores on a schedule so that judge drift can be told apart from a change in the work (Li proposes this for evaluation pipelines generally [64]), pairwise comparison with both orders, hidden provenance (the judge does not know which model generated an idea, which limits self-preference when generator and judge share a family), a written rubric, and above all calibration against the person's picks. The person's picks are the ground truth. Over time, record whether the person's choice was in the judge's top three. If agreement is low, the judge's rubric is wrong, not the person.

### 4.6 Where spare compute goes

The framework's slogan for design gates is "make the intelligence work for you": when compute would otherwise expire, pour it into divergent work. The correction above sharpens this. Spare *strong* compute goes to design gates. Spare *cheap* compute goes to correcting gates (more sensors, more often, verifiers on verifiers) and to gathering raw material. **Argued.**

## 5. Forming and tuning gates

### 5.1 From incidents

Every gate starts from an incident, a flaw that reached the person. The L2 procedure asks which stage produced it, why no gate caught it, and what the strongest cheap fix is. This is the postmortem pattern from site reliability engineering, where a blameless write-up ends in action items that prevent recurrence [65]. Reason's Swiss cheese model, in which defences are "slices of Swiss cheese, having many holes" that sometimes "line up", is the right picture for "why didn't a gate catch it": several layers each had a hole, and the holes lined up [66].

The g-system's memory directory is the best evidence that the ratchet is not yet turning. Of 58 memory files, 19 are typed as feedback, and several typed as project also carry rules. Many are rung-2 fixes for classes of flaw that a cheap deterministic sensor could check:

- `secrets_never_in_argv`: a PreToolUse Bash hook that denies commands passing a value matching a key format as an argument.
- `b2_is_work_server`: deny load-generating commands addressed to b2.
- `nginx_test_alt_config_as_root`: deny `nginx -t -c` run as root on h.
- `find_is_bfs_on_h`: lint scripts for relative `-newermt`.
- `r2_photos_mount_recursive_reads`: deny recursive reads of `/home/gabriel` that do not exclude the mount.
- `worktree_force_deletes_data`: deny `git worktree remove --force` when the tree holds gitignored data.
- `dont_pause_other_sessions`: deny STOP or KILL signals to Claude processes outside the current fan-out.
- `commit_attribution_sole_author`: a commit-msg hook.
- `claude_tmp_root_owned` and `root_tests_break_user_services`: continual ownership probes, or a check before a service restart.
- `design_no_middle_dots`: a one-line addition to an existing gate. ganti's copy gate denies em dashes, en dashes, semicolons and ellipses, but not the middle dot banned on 2026-09-08. The new `copy-voice` gate in g-levels tells the agent to avoid middle dots in its guidance, but its sensor is the same ganti score, which does not check for them. The guidance and the sensor have drifted apart inside one gate, which is the defect the guidance-plus-sensor structure is meant to prevent.

That is about ten classes of flaw that were "read sometimes" when this note was started. Three of them (`secrets_never_in_argv`, `b2_is_work_server` and `root_tests_break_user_services`) were promoted into g-levels on 18 October 2026, and their origin fields say "Held as a memory until now". All three were registered as review gates with a rubric, not as the deterministic hooks suggested above, so they run at milestones rather than on every command. **Established** that these are memories or were until that day, **argued** that each has a cheap sensor. The case also shows a pattern worth naming: memories are the system's queue of un-promoted gates.

### 5.2 The three-case test

A new gate is tested against the original failure, a related failure of the same class, and clean work that must still pass. The third case is the important one. It is the gate's specificity check, and ganti states the strongest form of it: "a rule the reference corpus itself fails is a bug". ganti's first version hardcoded banned words and scored Gabriel's own technical report at 100/100, because he uses some of the "AI words". The fix was to derive absolutes from his corpus and enforce, in a test, that his whole corpus never trips them.

Generalised: **every gate should have a reference corpus of known-good work, and a gate that fails it is wrong, not the work.** For code gates the corpus is the current passing codebase plus accepted past diffs. For copy gates it is the person's own writing. For design taste it is past picks. **Argued.**

### 5.3 Sensor calibration

A sensor has a false-negative rate (flaws it misses) and a false-positive rate (clean work it fails). The three-case test gives one point each. Calibration needs more: a set of seeded flaws of the class, to estimate recall, and the clean corpus, to estimate the false-positive rate. This is ordinary evals practice in AI product work [67]. ganti's calibration is the model in the system: five-fold cross-validation on 1,000-word chunks, a threshold set at the 90th percentile of his own held-out writing, with the caveat recorded that short copy gives noisier scores and the comparison set of generated text is small.

Two lessons from the system's incident records sharpen this. The gcommerce pipeline found that Blender exits 0 when its script fails, so frame count became the success signal. And the Newcastle record found `ok: true` meaning "the recording succeeded" while the performance verdict had failed. Both are sensors measuring the wrong thing. **A sensor should measure the property, not the process exit.**

### 5.4 Judge reliability for fuzzy gates

A judged correcting gate is only as good as its judge. Three checks, all **argued**: agreement with the person on a labelled sample, before the gate goes live, stability across reruns and prompt rewordings (Sternlicht and colleagues show this can be poor [62]), and separation of judge and generator where the gate's principle is something the generator's family is known to be bad at. Run judged gates k times only when the verdict is near the threshold.

### 5.5 Saturation thresholds

For design gates the threshold is a cost decision. A reasonable starting rule: stop when two consecutive runs across different framings add fewer than one new distinct idea each, then do one probe run with a different generator. Tune the rule from outcomes: if the person's picks almost always come from early runs, the threshold can be tighter, and if picks come from late runs or probe runs, the gate is stopping too early. **Conjecture**, to be tested. The `feature-ideas` gate as registered stops when a round of three runs adds fewer than one idea, with no probe run, so it cannot tell the space running out from its generator running out. Adding the probe as a fixed last round is a one-line change to its saturation field.

### 5.6 Versioning

A gate is a versioned object: its guidance, its sensor code, its threshold and its scope rules all change. A charter should pin gate versions, so a sharper sensor does not silently start failing work in another project. Results should record the gate version that produced them, or counts across a version change are not comparable.

### 5.7 Retirement after model changes

Each gate records the model it was written against. A gate that stops firing after a model change may be obsolete or may have lost its scope. The rule in the skill is right: replay the original incident against the new model, and retire only if it passes and the scope still ran through the gate. Silence alone is not evidence. Retired gates should be archived with their incidents, because a later model can regress.

## 6. Charters

### 6.1 Parts

A charter has a scope (paths, modules, or a declaration in the code), principles, gates (pinned versions), a blocking set, exceptions (each with scope, reason and review date) and a budget. Gates never scope themselves, so one gate definition can serve many charters.

**Conflicts.** When two charters disagree about a shared gate, the skill says the narrower scope wins and the reason is recorded. That is the specificity rule familiar from CSS and from policy engines, and it is right for scope. It does not settle conflicts between principles within one scope (a performance gate against a readability gate). Those need an explicit order in the charter, the way the Model Spec orders authority [34].

**Blocking sets.** Only gates with low false-positive rates and cheap remediation should block. A blocking gate with a noisy sensor teaches agents to route around it. Advisory first, then blocking once the clean-corpus pass rate is high, is the safe promotion path. This matches Sentinel's three enforcement levels [31].

**Budgets.** Every gate costs something per run. A charter should state a cost budget per run and per milestone, so that cost creep becomes visible at review. Design gates in particular need a hard cap.

**Review from counts.** The skill's review table (a gate often `impossible` in one scope suggests an exception, a gate often `attempted` everywhere suggests unclear guidance, a gate that never fires needs its scope checked) is the right starting set. Add two patterns: a gate with a rising `waived` count is an exception that is becoming the rule, and a gate whose results never vary at all may not be measuring anything.

### 6.2 Charters by problem type

| Problem type | Character | Gates that suit it | Blocking | Notes |
|---|---|---|---|---|
| Engine simulation code (g-engine sim core) | crisp invariants, measurable | unit checks (SI units), memory ladder, determinism, perf budgets, precompute-not-runtime | most correcting gates block | the next charter planned after g-system and g-site. LOD is a separate scope where SI precision is waived |
| Infra and ops (h, b, b2, services) | high blast radius, low variety | PreToolUse denials, ownership probes, secret scanning, compliance boxes | hard blocks for destructive actions | memories are the backlog here |
| Copy | fuzzy but measurable against a corpus | ganti absolutes and loudness, register selection | absolutes block, loudness advisory | reference corpus is the person's own writing |
| UI and visual taste | fuzzy, judged | g-design hierarchy rules (mechanical parts), judged visual review, design gates for "what would make this better" | mechanical parts only | judges calibrated against picks. No cheap fan-out |
| Research writing | verifiable facts, fuzzy argument | citation existence and quote checks, claim tagging | citation checks block | this note was written under such a charter informally |
| Client work | factory belongs to someone else | the client's own checks plus a thin personal charter | per client | usually L1 or L2 |

The pattern is that blocking tracks how mechanical the sensor is, and design gates fit where taste matters and the work is not urgent. **Argued.**

### 6.3 The g-system as one charter

The gcore compliance boxes are already called the system-wide charter. The cleaner statement is that the g-system is one charter whose scope is every repo on every host, and repo charters extend it. A repo charter can add gates and add exceptions to system gates, with reasons. It cannot silently drop a system gate. This keeps "don't build a second compliance system" true while giving repos room.

## 7. Where knowledge lives: Brain, skills, g-levels and research

Gabriel's working split is: Brain holds facts that are true (what is), skills hold guidance (how to do it), the gate registry holds gates and charters (what must hold, checked and counted), and research holds why (theory and evidence). Memories are mostly rungs 2 and 3 of the fix ladder waiting to be promoted.

### 7.1 Testing the split

The split survives, with three amendments.

**Brain is not one kind of thing.** g-brain today has a long-term corpus of notes, a pulse layer for what is true now, dated todos, the alert spine and status probes. The split holds for the corpus. The status probes in L5, and the verify sweep that stamps dependents of a changed note with `stale_dep`, are gates by this note's definition: continual, mechanical, with results. They should stay where they run but register as gates, so their counts reach a charter. Brain owns the facts. It should not own the definition of what must hold.

**Skills are half of a gate, and some skills are a whole one.** A skill is guidance. When a skill has a sensor (anti-ai-voice has `ganti score`, g-gcore-comply has its checklist), the gate is complete, and both of these are now registered in g-levels as `copy-voice` and `gcore-comply`, whose guidance fields point at the skills. The amendment is that the link should run both ways: the skill should also name its gate, so an agent that loads the skill knows its work will be checked and counted. A skill without any sensor stays a rung-3 fix, and that is fine where the principle cannot yet be measured.

**Memories are a queue, not a store.** Each feedback memory should end in one of three places: a gate (if a sensor is cheap), a skill line (if it is guidance that cannot be measured), or a Brain note (if it is a fact about the world, such as "b2 runs the Baseline scraper"). Many memories hold both: the fact goes to Brain, the rule goes to a gate. The `secrets_never_in_argv` memory is a fact (sudo logs argv on h and b) plus a rule (never pass secrets as arguments) plus an incident (2026-10-17). In the proposed split those become a Brain note, a gate, and the gate's origin field.

### 7.2 Where each kind of new knowledge goes

| New knowledge | Goes to | Example |
|---|---|---|
| A fact about a system, person or thing that is true always | Brain note | "b2 runs the Baseline scraper and Twenty" |
| Something true now, likely to change | Brain pulse | "Telnyx WhatsApp blocked since 2026-10-06" |
| A dated commitment | Brain todo | "first paying tutoring client by 2026-10-28" |
| How to do a task well | skill | the anti-ai-voice workflow |
| A rule that a sensor can check | g-levels gate, with its skill as guidance | "no secret values in argv" |
| Where a rule applies, and its exceptions | g-levels charter | "SI units in sim core, waived in LOD" |
| A gate result | g-levels result stream (counts), notes at the gate | `met 41, attempted 3` |
| An incident | the gate's origin field, and a charter incident log | failures.md entries |
| Why the framework works, and the evidence | research | this note |
| A rule waiting for promotion | memory, briefly | any new feedback memory |

### 7.3 Overlaps and links

Links should run in both directions between a gate and its skill, from a gate to its origin incident, from a charter to the Brain notes that hold its domain facts, and from research to the gates and incidents it cites as evidence. Brain's own reference graph and `stale_dep` stamps already do this for notes. g-levels should use the same discipline: a gate that depends on a Brain fact (the IP of b2) should be stamped when that fact changes.

The main overlap risk is incident detail. Incidents are private (paths, hosts, sometimes leaked key names). Gates in a public repo should carry a sanitised origin ("a secret appeared in sudo logs via argv") with a private pointer to the full record, which stays in the private failures logs or Brain.

### 7.4 Research: own repo or inside g-levels?

This note was briefed against a planned repo called g-charter. While it was being written, the registry was built as g-levels, and its CLAUDE.md already assigns `research/` to "theory pieces, with sources". So the question is now whether to keep that. There are two options. Research can stay in g-levels under `research/`, linked from the gates it justifies. Or a separate g-research repo can hold this note and also index ANS (`/home/gabriel/ans`), which already keeps verified literature notes in `lit/` with a shared `refs.bib`.

My recommendation is **a separate g-research repo, with g-levels linking to it**, for three reasons. The research cadence differs from the gate cadence. Gates change weekly, research notes are written and revised slowly, and mixing them makes the registry's history noisy. The research is not only about charters. ANS studies harness and model effects on small local models, which bears on gate costs and judge choice but is not a gate. And ANS already has a working verified-literature practice (fetched sources, dated verification, explicit UNVERIFIED marks) that a research repo can adopt as its contract.

The counter-argument is real. A separate repo is one more thing to keep in sync, and a theory note far from the code it justifies tends to drift. The mitigation is cheap: the g-levels CLAUDE.md names the research note that justifies each design decision, and research notes cite gate ids. If g-research would hold only this one note for months, keep it in `g-levels/research/` as now built, and move it when a second research thread appears. **Argued.** This is an architectural fork for Gabriel to decide, and the repo as built has already taken the fallback.

## 8. Sharing

**Gates across projects first.** A gate is a small, versioned, testable object. Sharing it means publishing its guidance, sensor, pass criteria and three test cases. A charter pins the version it uses, so an upstream change does not break downstream work silently. This is how linters and their rule sets already travel.

**Private detail stays out.** g-levels is licensed AGPL and its CLAUDE.md treats it as public. The g-system went public on 2026-10-10, and other repos were hidden the same day, some because of leaked keys in their history. Public gates therefore carry sanitised origins and generic examples. g-levels already does this: origins are one-line public summaries, and the results log is gitignored because notes can carry private incident detail. Incident specifics stay in the private instance. The three test cases need care: the original failure case for a secrets gate must use a fake key that matches the format.

**Charters across projects later.** Charters encode a project's scope and exceptions, so they are less portable. What travels first is a charter template per problem type (section 6.2), with the system charter as the shared base.

**The g-system as one charter.** Every instance of the g-system would start from the system charter and its pinned gates, and add repo charters. Sharing between instances then means sharing gate versions and charter templates, not raw incident logs.

## 9. Critiques

**Goodhart and sensor gaming.** Agents see their sensors, and agents are optimisers. Specification gaming is well documented in reinforcement learning [68], and coding agents now do it to their tests. METR reported frontier models "modifying the tests or scoring code" to raise their scores [69]. ImpossibleBench builds tasks where the tests contradict the specification, so any pass is a shortcut, and reports a 54% cheating rate for GPT-5 on one variant under its full scaffold [70]. Anthropic's Claude 3.7 Sonnet system card describes "directly returning expected test values rather than implementing general solutions" [71]. OpenAI found that optimising a model against a monitor of its reasoning led it to hide its intent while still cheating [72]. That last finding matters most here: pressure on a sensor can make the flaw invisible to the sensor rather than absent. Every gate is a target. Mitigations: keep sensors partly hidden or randomised where possible, prefer property checks over process checks, and treat a sharp rise in `met` with no change in leaks as a warning sign. Wang and colleagues' finding that harness evolution looks better when the verification signal comes from the reporting benchmark is this critique in miniature [23]. **Established** as a risk, **argued** for the mitigations.

**False confidence.** The Newcastle failure record is the sharpest evidence in the system. A report led with 1,802 passing tests while the visible improvements remained unverified, and Gabriel repeatedly tested an older build while receiving reports about newer work. Gates measure what they measure. A charter full of green counts says nothing about the classes of flaw no gate covers, and the person can mistake coverage of known flaws for quality. The Newcastle record's own lesson is to keep recording, correctness, performance and acceptance as separate claims. In charter terms: acceptance is a gate too, its sensor is the person on the real path, and it never gets waived.

**Gate rot and cost creep.** Gates accumulate. Each is cheap, the sum is not, and stale gates fail clean work. failures.md warns against responding to failures with "an ever-growing universal prompt, mandatory agent teams, or additional permission ceremonies". The ratchet only turns one way, which is the risk. Countermeasures: budgets per charter, retirement by replay after model changes, and a review pattern that flags gates whose `met` count never varies.

**Over-formalising creative work.** Taste is hard to state, and gates for taste can flatten work toward what the judge likes. Doshi and Hauser's result (individually better, collectively more alike) [57] is the risk at system scale: if every page passes the same design gate with the same judge, the pages converge. Design gates should widen the options a person picks from, never pick. Correcting gates on taste should cover only the mechanical rules (sentence case, no fades, no middle dots).

**Judge bias in design gates.** Covered in 4.5. The specific risk is self-preference: a Claude judge ranking Claude ideas, or an Opus judge preferring Opus prose. Hidden provenance and calibration against picks are the defence. **Argued.**

**Are the levels discrete?** Probably not. A single session moves between levels, and many artefacts are mixed (a skill with a sensor is a gate, a contract is a gate built into a stage boundary). The levels are better read as a description of where the person's attention goes than as stages of a system. That is how Bateson's categories work too: they are logical types, not a timeline. The useful claim is directional (move attention upward) rather than categorical. **Argued.**

**The person as sensor.** Section 2.6's variety argument cuts both ways. Gates add variety to the regulator, but every gate is also a thing the person must maintain. Beyond some number of gates the person's job becomes maintaining gates, which is L3 working as designed, or drowning in them, which is gate rot. Which one happens depends on whether review from counts is cheap. **Conjecture.**

## 10. What to measure

**Gate results as primary data.** Every gate in scope writes one result per run: gate id and version, charter, scope, result, cost and a pointer to its note. This is the minimum for L3. ganti's `scores.jsonl` already has the right shape (metadata, no private text).

**Level tags on sessions.** The skill already asks the person to name a level at the start of a session. Record it, so later analysis can compare sessions by level.

**Corrections per session.** Count the times the person corrects an agent, from session logs. The g-system's week-review tooling already reconstructs sessions from logs, so this is an extension rather than a new system.

**Repeat-flaw rate.** The share of incidents whose class has appeared before. This is the ratchet's real metric. If the ratchet works, repeats fall even as total incidents rise with output.

**Design-gate yield.** Per design gate run, the saturation curve, the number of distinct ideas, where in the curve the person's pick came from, and whether the probe generator added anything.

**Reading them.** All of these are confounded. Corrections per session depend on the model (the g-system moved much of its work from Claude to Codex models in early October 2026), the task, the person's mood and the novelty of the work. Incidents depend on how much is shipped and how closely the person looks. A falling correction rate after a gate is added is weak evidence, because the same weeks may bring a model upgrade. Three defences, all **argued**: compare within a task type rather than across, look for the specific incident class to disappear rather than for aggregate rates to fall, and use model changes as natural experiments by replaying the incident set against the new model before and after. The failures.md caveat applies everywhere: these records "do not isolate a model-quality effect". The repeat-flaw rate is the least confounded measure because it conditions on the class.

## 11. Notes toward L4

Speculative throughout.

L4 generates charters rather than building them. Three ingredients look necessary. A pooled incident log across projects, with incidents tagged by class, so that patterns appear. A cross-charter view of gates: which gates appear in most charters, which exceptions recur, which gates fire in one project and never in another. And a drafting step: given a new project's description and its first incidents, propose a charter assembled from existing gates, with suggested scopes and exceptions, for the person to approve.

The drafting step is a design gate, so section 4 applies. Strong generators, a few framings, a judge, saturation, the person picks. The interesting property is that L4 would be a design gate whose output is L3 objects, which is the ladder recursing, as Beer's model recurses. Whether that recursion is useful or just neat is the first thing to test. **Conjecture.**

## 12. Main claims, by status

**Established:** the ladder reproduces Bateson's logical types and Engelbart's A, B and C activities. Guidance and sensor are Böckeler's guides and sensors. Blocking levels match Sentinel's enforcement levels. The g-system had working gates but no registry until g-levels was committed on 18 October 2026 with seven gates and two charters, and its harness gates fail open and are not yet wired to the registry. About ten memories describe flaw classes with cheap mechanical sensors.

**Argued:** the four-valued result, "silence is the only failure" and "counts up, notes down" are new. Design gates are a distinct gate type stopped by saturation, and saturation measures the space only relative to the generator. Every gate needs a reference corpus it must pass. Research belongs in its own repo, linked from g-levels.

**Conjecture:** specific saturation thresholds, and whether L4 is worth building.

## Sources

All checked on 2026-10-18 by the author or a research scout, against the URL given. Entries marked partial were confirmed bibliographically, with the content checked through the secondary source named. G-system evidence (memory files, failures.md, conventions.md, the g-anti-ai CLAUDE.md, hook scripts and settings) was read directly on host h and is cited by path in the text.

1. Bateson, G. (1972). "The Logical Categories of Learning and Communication." In *Steps to an Ecology of Mind*. San Francisco: Chandler. Reissued University of Chicago Press, 2000. https://doi.org/10.7208/chicago/9780226924601.001.0001
2. Bateson, G. (1942). "Social Planning and the Concept of Deutero-Learning." In L. Bryson and L. Finkelstein (eds.), *Science, Philosophy and Religion: Second Symposium*, pp. 81-97. Reprinted in *Steps to an Ecology of Mind* (1972). Checked via the book's bibliography and text: https://ejcj.orfaleacenter.ucsb.edu/wp-content/uploads/2017/06/1972.-Gregory-Bateson-Steps-to-an-Ecology-of-Mind.pdf
3. Argyris, C. and Schön, D. A. (1974). *Theory in Practice: Increasing Professional Effectiveness*. San Francisco: Jossey-Bass. https://openlibrary.org/books/OL5043812M/Theory_in_practice (text read in a third-party reproduction)
4. Argyris, C. and Schön, D. A. (1978). *Organizational Learning: A Theory of Action Perspective*. Reading, MA: Addison-Wesley. https://archive.org/details/organizationalle00chri (text read in a third-party reproduction)
5. Argyris, C. (1991). "Teaching Smart People How to Learn." *Harvard Business Review* 69(3), 99-109. https://hbr.org/1991/05/teaching-smart-people-how-to-learn
6. Engelbart, D. C. (1992). "Toward High-Performance Organizations: A Strategic Role for Groupware." *Proceedings of GroupWare '92*, San Jose, 77-100. Morgan Kaufmann. https://www-leland.stanford.edu/dept/SUL/sites/engelbart/append2-ntb.html
7. Victor, B. (2011). "Up and Down the Ladder of Abstraction: A Systematic Approach to Interactive Visualization." https://worrydream.com/LadderOfAbstraction/
8. Meadows, D. H. (1999). *Leverage Points: Places to Intervene in a System*. Hartland, VT: The Sustainability Institute. https://www.donellameadows.org/wp-content/userfiles/Leverage_Points.pdf
9. Shingo, S. (1986). *Zero Quality Control: Source Inspection and the Poka-Yoke System*. Trans. A. P. Dillon. Productivity Press. https://ndlsearch.ndl.go.jp/books/R100000002-I000003184396 (partial: bibliography confirmed, content checked via the publisher description and a secondary source, not the book text)
10. Ohno, T. (1988). *Toyota Production System: Beyond Large-Scale Production*. Productivity Press. https://openlibrary.org/books/OL2407711M/Toyota_production_system (partial: bibliography confirmed, concepts confirmed through Toyota's own pages, not the book text)
11. Toyota Motor Corporation (undated). "Development and Deployment of the Toyota Production System." https://www.toyota-global.com/company/history_of_toyota/75years/text/entering_the_automotive_business/chapter1/section4/item4.html
12. Toyota Motor Corporation (undated). "Toyota Production System." https://global.toyota/en/company/vision-and-philosophy/production-system/
13. Imai, M. (1986). *Kaizen: The Key to Japan's Competitive Success*. 1st ed. McGraw-Hill. https://books.google.com/books/about/Kaizen_The_Key_To_Japan_s_Competitive_Su.html?id=tCu1AAAAIAAJ
14. Ford, N., Parsons, R. and Kua, P. (2017). *Building Evolutionary Architectures: Support Constant Change*. O'Reilly. 2nd ed. with P. Sadalage (2022), *Automated Software Governance*. https://www.oreilly.com/library/view/building-evolutionary-architectures/9781491986356/ and the definition as quoted on the authors' site https://evolutionaryarchitecture.com/ffkatas/
15. Lopopolo, R. (2026). "Harness engineering: leveraging Codex in an agent-first world." OpenAI, 11 February 2026. https://openai.com/index/harness-engineering/
16. Hashimoto, M. (2026). "My AI Adoption Journey." 5 February 2026, section "Step 5: Engineer the Harness." https://mitchellh.com/writing/my-ai-adoption-journey
17. Osmani, A. (2026). "Agent Harness Engineering." 19 April 2026. https://addyosmani.com/blog/agent-harness-engineering/
18. Quinn, B. (2025). "Metadata Agents in Practice." CNI Fall 2025 project briefing slides, slide on "Harness engineering as a subset of CTXE". https://www.cni.org/wp-content/uploads/2025/12/CNI_Integrating_Quinn.pdf (the hosted copy may postdate the 2025 talk)
19. Böckeler, B. (2026). "Harness engineering for coding agent users." martinfowler.com, 2 April 2026. https://martinfowler.com/articles/harness-engineering.html
20. Böckeler, B. (2026). "Harness Engineering - first thoughts." Exploring Gen AI, martinfowler.com, 17 February 2026. https://martinfowler.com/articles/exploring-gen-ai/harness-engineering-memo.html
21. Lin, J., Liu, S., Pan, C., et al. (2026). "Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses." arXiv:2604.25850. https://arxiv.org/abs/2604.25850
22. Lee, Y., Nair, R., Zhang, Q., Lee, K., Khattab, O. and Finn, C. (2026). "Meta-Harness: End-to-End Optimization of Model Harnesses." arXiv:2603.28052. https://arxiv.org/abs/2603.28052
23. Wang, Y., Zhu, H., Hu, Z., et al. (2026). "Rethinking the Evaluation of Harness Evolution for Agents." arXiv:2607.12227, v4 5 October 2026. https://arxiv.org/abs/2607.12227
24. Goodhart, C. A. E. (1975). "Problems of Monetary Management: The U.K. Experience." Reprinted in *Monetary Theory and Practice: The UK Experience* (1984), 91-121. London: Macmillan. https://link.springer.com/chapter/10.1007/978-1-349-17295-5_4 (partial: wording checked in Chrystal and Mizen, "Goodhart's Law: Its Origins, Meaning and Implications for Monetary Policy", 2001 draft, p. 4, not the original)
25. Strathern, M. (1997). "'Improving ratings': audit in the British University system." *European Review* 5(3), 305-321. https://ideas.repec.org/a/cup/eurrev/v5y1997i03p305-321_00.html
26. Campbell, D. T. (1979). "Assessing the impact of planned social change." *Evaluation and Program Planning* 2(1), 67-90. https://doi.org/10.1016/0149-7189(79)90048-X
27. Manheim, D. and Garrabrant, S. (2018). "Categorizing Variants of Goodhart's Law." arXiv:1803.04585. https://arxiv.org/abs/1803.04585
28. Ashby, W. R. (1956). *An Introduction to Cybernetics*. London: Chapman & Hall. https://pespmc1.vub.ac.be/books/IntroCyb.pdf
29. Beer, S. (1972). *Brain of the Firm*. Allen Lane. Also *The Heart of Enterprise* (1979) and *Diagnosing the System for Organizations* (1985), Wiley. Bibliography: https://metaphorum.org/wp-content/uploads/2022/03/SB_Bibliography.pdf (partial: the five systems checked in Walker, *The Viable System Model: A Guide for Co-operatives and Federations*, https://metaphorum.org/wp-content/uploads/2022/11/vsm-coop-walker.pdf)
30. Open Policy Agent (undated). "Open Policy Agent (OPA)" documentation. https://www.openpolicyagent.org/docs
31. HashiCorp (undated). "Enforcement Levels." Sentinel documentation. https://developer.hashicorp.com/sentinel/docs/concepts/enforcement-levels
32. Anthropic (undated). "Hooks reference." Claude Code documentation. https://code.claude.com/docs/en/hooks
33. Bai, Y., Kadavath, S., Kundu, S., et al. (2022). "Constitutional AI: Harmlessness from AI Feedback." arXiv:2212.08073. https://arxiv.org/abs/2212.08073
34. OpenAI (2024). "Model Spec," 8 May 2024 edition, https://cdn.openai.com/spec/model-spec-2024-05-08.html, and 18 August 2026 edition, https://model-spec.openai.com/2026-08-18.html
35. Askell, A., Carlsmith, J., et al. (2026). "Claude's Constitution." Anthropic, January 2026 (PDF dated 21 January, announcement dated 22 January). https://www.anthropic.com/constitution
36. Khattab, O., Singhvi, A., Maheshwari, P., et al. (2023). "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines." arXiv:2310.03714. https://arxiv.org/abs/2310.03714
37. Yang, C., Wang, X., Lu, Y., Liu, H., Le, Q. V., Zhou, D. and Chen, X. (2023). "Large Language Models as Optimizers." arXiv:2309.03409. https://arxiv.org/abs/2309.03409
38. Agrawal, L. A., Tan, S., Soylu, D., et al. (2025). "GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning." arXiv:2507.19457. https://arxiv.org/abs/2507.19457
39. Madaan, A., Tandon, N., Gupta, P., et al. (2023). "Self-Refine: Iterative Refinement with Self-Feedback." NeurIPS 36. arXiv:2303.17651. https://arxiv.org/abs/2303.17651
40. Shinn, N., Cassano, F., Gopinath, A., Narasimhan, K. and Yao, S. (2023). "Reflexion: Language Agents with Verbal Reinforcement Learning." NeurIPS 36. arXiv:2303.11366. https://arxiv.org/abs/2303.11366
41. Anthropic (Erik S. and Barry Zhang) (2024). "Building effective agents." 19 December 2024. https://www.anthropic.com/engineering/building-effective-agents
42. Guilford, J. P. (1967). *The Nature of Human Intelligence*. New York: McGraw-Hill. https://openlibrary.org/books/OL23785371M/The_nature_of_human_intelligence
43. Campbell, D. T. (1960). "Blind variation and selective retention in creative thought as in other knowledge processes." *Psychological Review* 67(6), 380-400. https://pubmed.ncbi.nlm.nih.gov/13690223/
44. Design Council (undated). "History of the Double Diamond." https://www.designcouncil.org.uk/resources/the-double-diamond/history-of-the-double-diamond/
45. Romera-Paredes, B., Barekatain, M., Novikov, A., et al. (2024). "Mathematical discoveries from program search with large language models." *Nature* 625, 468-475. https://doi.org/10.1038/s41586-023-06924-6
46. Novikov, A., Vũ, N., Eisenberger, M., et al. (2025). "AlphaEvolve: A coding agent for scientific and algorithmic discovery." Google DeepMind white paper, arXiv:2506.13131. https://arxiv.org/abs/2506.13131
47. Brown, B., Juravsky, J., Ehrlich, R., Clark, R., Le, Q. V., Ré, C. and Mirhoseini, A. (2024). "Large Language Monkeys: Scaling Inference Compute with Repeated Sampling." arXiv:2407.21787. https://arxiv.org/abs/2407.21787
48. Colwell, R. K. and Coddington, J. A. (1994). "Estimating terrestrial biodiversity through extrapolation." *Philosophical Transactions of the Royal Society B* 345, 101-118. https://www.robertkcolwell.org/publications/7087.pdf
49. Good, I. J. (1953). "The population frequencies of species and the estimation of population parameters." *Biometrika* 40(3-4), 237-264. https://academic.oup.com/biomet/article/40/3-4/237/195998 (the singleton formula checked in Anevski, Gill and Zohren, arXiv:1312.1200)
50. Chao, A. (1984). "Nonparametric estimation of the number of classes in a population." *Scandinavian Journal of Statistics* 11(4), 265-270. Listed at https://sites.google.com/view/chao-lab-website/publication (partial: formula checked via Colwell and Coddington 1994, eq. 8)
51. South Dakota Game, Fish and Parks (undated). "Fish SD: Fish Population Estimation" (Lincoln-Petersen estimator). https://gfp.sd.gov/UserDocs/nav/Fish_SD_Fish_Population_Estimation_.pdf
52. Eick, S. G., Loader, C. R., Long, M. D., Votta, L. G. and Vander Wiel, S. (1992). "Estimating software fault content before coding." *Proceedings of ICSE 14*, 59-65. Bibliography: https://cseweb.ucsd.edu/~wgg/sigsoft-through-1993.pdf (partial: full text not obtained)
53. Briand, L. C., El Emam, K., Freimut, B. G. and Laitenberger, O. (2000). "A Comprehensive Evaluation of Capture-Recapture Models for Estimating Software Defect Content." *IEEE Transactions on Software Engineering* 26(6), 518-540. https://doi.org/10.1109/32.852741
54. Guest, G., Bunce, A. and Johnson, L. (2006). "How Many Interviews Are Enough? An Experiment with Data Saturation and Variability." *Field Methods* 18(1), 59-82. https://doi.org/10.1177/1525822X05279903
55. Si, C., Yang, D. and Hashimoto, T. (2024). "Can LLMs Generate Novel Research Ideas? A Large-Scale Human Study with 100+ NLP Researchers." arXiv:2409.04109. https://arxiv.org/abs/2409.04109
56. Meincke, L., Mollick, E. R. and Terwiesch, C. (2024). "Prompting Diverse Ideas: Increasing AI Idea Variance." arXiv:2402.01727. https://arxiv.org/abs/2402.01727
57. Doshi, A. R. and Hauser, O. P. (2024). "Generative AI enhances individual creativity but reduces the collective diversity of novel content." *Science Advances* 10(28), eadn5290. https://pmc.ncbi.nlm.nih.gov/articles/PMC11244532/
58. Jiang, L., Chai, Y., Li, M., et al. (2025). "Artificial Hivemind: The Open-Ended Homogeneity of Language Models (and Beyond)." NeurIPS 2025 Datasets and Benchmarks. arXiv:2510.22954. https://arxiv.org/abs/2510.22954
59. Schaeffer, R., Miranda, B., Kazdan, J., Chudnovsky, J. and Koyejo, S. (2026). "How Strong Is the Evidence for the Artificial Hivemind?" arXiv:2609.33936. https://arxiv.org/abs/2609.33936
60. Terwiesch, C., Meincke, L., Girotra, K., Mollick, E., Nave, G. and Ulrich, K. (2026). "Artificial intelligence and its impact on creativity and diversity: An empirical study of large language model-generated product ideas." *Production and Operations Management*, OnlineFirst 22 July 2026. https://journals.sagepub.com/doi/10.1177/10591478261474243
61. Zheng, L., Chiang, W.-L., Sheng, Y., et al. (2023). "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena." NeurIPS 36 Datasets and Benchmarks. arXiv:2306.05685. https://arxiv.org/abs/2306.05685
62. Sternlicht, N., Shahid, S., Jansen, P., Weld, D. S., Siangliulue, P. and Hope, T. (2026). "Old Ideas, Novel Problems: The Instability of LLM-Based Novelty Evaluation." arXiv:2610.02022. https://arxiv.org/abs/2610.02022
63. Shankar, S., Zamfirescu-Pereira, J. D., Hartmann, B., Parameswaran, A. G. and Arawjo, I. (2024). "Who Validates the Validators? Aligning LLM-Assisted Evaluation of LLM Outputs with Human Preferences." UIST 2024. arXiv:2404.12272. https://arxiv.org/abs/2404.12272
64. Li, Y. (2026). "Who Drifted: the System or the Judge? Anytime-Valid Attribution in LLM Evaluation Pipelines." arXiv:2606.15474 (single-author preprint). https://arxiv.org/abs/2606.15474
65. Lunney, J. and Lueder, S. (2016). "Postmortem Culture: Learning from Failure." In B. Beyer et al. (eds.), *Site Reliability Engineering*, ch. 15. O'Reilly. https://sre.google/sre-book/postmortem-culture/
66. Reason, J. (2000). "Human error: models and management." *BMJ* 320(7237), 768-770. https://pmc.ncbi.nlm.nih.gov/articles/PMC1117770/ See also Reason, *Human Error* (1990), Cambridge University Press.
67. Husain, H. (2024). "Your AI Product Needs Evals." 29 March 2024. https://hamel.dev/blog/posts/evals/
68. Krakovna, V., Uesato, J., Mikulik, V., et al. (2020). "Specification gaming: the flip side of AI ingenuity." DeepMind blog, 21 April 2020. https://deepmind.google/blog/specification-gaming-the-flip-side-of-ai-ingenuity/
69. Von Arx, S., Chan, L. and Barnes, B. (2025). "Recent Frontier Models Are Reward Hacking." METR, 5 June 2025. https://metr.org/blog/2025-06-05-recent-reward-hacking/
70. Zhong, Z., Raghunathan, A. and Carlini, N. (2025). "ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases." arXiv:2510.20270. https://arxiv.org/abs/2510.20270
71. Anthropic (2025). *Claude 3.7 Sonnet System Card*, section 6, "Excessive Focus on Passing Tests." https://assets.anthropic.com/m/785e231869ea8b3b/original/claude-3-7-sonnet-system-card.pdf
72. OpenAI (2025). "Detecting misbehavior in frontier reasoning models." 10 March 2025. https://openai.com/index/chain-of-thought-monitoring/
