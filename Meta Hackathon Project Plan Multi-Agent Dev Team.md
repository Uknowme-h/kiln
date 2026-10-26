# Meta Hackathon Project Plan: Multi-Agent Dev Team

Oct 26, 2026 · @Nishant Jasiwal

## Summary

The plan is to build a team of AI agents that turns a typed idea into a tested, running app on Meta's models, and to submit it to the Indie Developer track by Nov 4, 9:00 PM ET (Nov 5, 7:45 AM in Kathmandu).

The pitch: you describe a product, a requirements agent writes the spec, a PM agent plans the work, coding agents build it in parallel, and a test loop decides when it is done. The working name is still open (Foreman and Greenlight were the favourites).

Success by Nov 4 means three things:

1. One prompt produces a small working web app with passing tests, unattended, live on camera.
2. The repo is public, all new work, with a clear record of what was built during the hackathon.
3. The 3-minute demo video and a README with an evaluation table are submitted well before the deadline.

## Rules and constraints

The rule that shapes the whole plan is "new work only": everything demoed must be built during the hackathon, and unclear original work means disqualification. These points come from the participant guide.

- **Deadline:** submissions are due Wed Nov 4, 9:00 PM ET, which is Thu Nov 5, 7:45 AM in Kathmandu. Late submissions are not considered.
- **New work only:** the demo may show only features and code built during the event. Judges must be able to see what was created in that window.
- **Existing products:** only the Startups track is explicitly allowed to build on existing codebases. Treat a fork of another product as a risk until organizers confirm.
- **Public repo:** the repository must be public at submission, with a working demo video link.
- **Team size:** up to 4 people, and solo is allowed. This plan assumes solo.
- **Meta models required:** Stage 1 of judging is pass/fail on theme fit and real use of Meta Model API and/or Meta open-weight models.
- **Banned projects:** anything using code, data or assets you lack rights to. The guide's anti-project list (medical advice bots, basic RAG apps, simple chatbots and similar) does not cover this idea.
- **Credits:** claim your own $150 Meta Model API credits by Nov 4, 9:00 PM ET. A credit card is needed to make API calls.

## Scoring strategy

Each judging criterion maps to a build decision made now, so the work earns points instead of being tuned at the end. Stage 2 scores four equally weighted criteria from 0 to 10, and the top 5 per track reach live final judging.

| Criterion | What judges ask | Decision it drives |
| --- | --- | --- |
| Stage 1 (pass/fail) | Does it fit a theme and really use Meta models? | Frame it under Push the Frontier (software). Every agent calls Meta models through one gateway. |
| Impact | Real-world potential, fit with a problem statement | Aim at solo builders and founders who need a first working version. Show three different apps built from three prompts. |
| Use of Meta AI tooling | Did the team go beyond a basic integration? | Muse Spark for planning, Muse Spark Open for coding workers if it handles tools well (test on day one), Muse Voice Transcribe for spoken ideas. |
| Technical depth and execution | Sound engineering, real craft | Typed state, sandboxed execution, a test-gated loop, a budget guard, and an evaluation harness. |
| Demo | Working, impressive, holds up live | One reliable path, a cached fallback run, and a scripted 3-minute video. |

Two bonus prizes fit this project: Best Agentic Use-Case ($5,000) and Best Build on Meta Open Weight Models ($10,000, which needs Muse Glimmer or Muse Spark Open). Most Viral ($20,000) rewards engagement on an X post with the demo and is optional.

## Agent roles

There are seven roles, and only the runner is plain code, so test results cannot be argued with. Retry limits (N below) are set from the first evaluation runs.

| Role | Job | Model | Input to output | When it fails |
| --- | --- | --- | --- | --- |
| Requirements analyst | Turns the idea into user stories, acceptance criteria, a stack choice and a file plan | Muse Spark | Idea text to spec file | Asks a few questions at most, then proceeds on stated assumptions |
| PM planner | Splits the spec into tasks with dependencies and file ownership, assigns workers, re-plans | Muse Spark | Spec to task graph | A task that fails N times is split or reassigned |
| Test author | Writes tests from the acceptance criteria before code exists | Muse Spark | Spec to test files | Tests must at least collect and run before the coders start |
| Dev workers | Implement tasks in their own files using file and shell tools | Muse Spark Open, Muse Spark as fallback | Task, spec and tests to code | Can write only inside their owned paths |
| Runner (no model) | Runs the tests in Docker and captures logs | None | Workspace to structured pass/fail report | Time and resource caps, then reported as a failure |
| Fixer | Reads a failing log and patches the code | Muse Spark Open | Failing log and code to patch | After N failed fixes, escalates to the PM planner |
| Reporter | Writes the README, run summary and cost report | Muse Spark Open | Run log to documents | Falls back to a template |

## Model routing and budget

A single gateway module sends every model call, so cost is capped in code and not by hope. The guide's spend limit is a notification, so the hard stop has to live in the project.

- **One client wrapper:** it routes by role, sets `max_tokens` on every call, retries with backoff, and records tokens and cost per call. Confirm the API's format (OpenAI-compatible or not) in the quickstart first.
- **Budget guard:** the wrapper keeps a running total and refuses new calls past a configured ceiling. Each run has its own smaller ceiling.
- **Credit sources:** $150 Meta Model API credits, plus Modal ($100) and Baseten ($100, model ID `meta-models/muse-spark-open-1026`) if approved. Fireworks ($25) goes only to shortlisted teams, so do not plan on it.
- **Spending plan:** about 50% on building and debugging, 25% on evaluation runs, and 25% held back for final demo runs and recording. Adjust after the first week's real costs.
- **Cheap by default:** develop and debug on the open-weight model through partner credits, and reserve the larger Muse Spark model for planning and final runs.
- **Fallback:** if Muse Spark Open handles tool calls poorly, move the coders to Muse Spark and keep Muse Spark Open for the reporter and fixer. Decide this on day one from a small test, not by assumption.

## Sandbox and verification

Generated code runs only inside Docker, and a build counts as done only when tests the coders did not write turn green. This is also where the "Technical depth" score is earned.

- **Isolation:** one container per run, with CPU, memory and time limits, a mounted workspace, and no access to the host or to secrets. Pre-build base images with the common dependencies so runs do not need open network access.
- **Tests from the spec:** the test author writes tests from the acceptance criteria, and the coders cannot edit them. Otherwise the agents grade their own work.
- **Structured results:** the runner returns pass/fail per test with trimmed logs, and the fixer sees only that, which keeps prompts small and cheap.
- **Bounded loops:** a cap on fix attempts per task and per run. Past the cap the PM re-plans, and past the run cap the run stops with a clear report.
- **Path ownership:** each worker may write only its assigned files, which prevents two coders overwriting each other during parallel work.
- **Honest reporting:** the final report states which tests passed, which were skipped, and what it cost. Never present a run as green when tests were skipped.
- **Workshop tie-in:** the Oct 28 session on verifiable agents is a checkpoint to compare against this design.

## Repo layout and engineering standards

Start a fresh public repo after kickoff, with a layout that separates the model gateway, agents, orchestration and sandbox so each can be tested alone.

```markdown
repo/                 (name still open)
  README.md           what was built, attribution of any libraries
  NEWWORK.md          dated log of what was built and when
  src/
    gateway/          model client, routing, budget guard, token log
    agents/           requirements, planner, tester, coder, fixer, reporter
    graph/            orchestration and typed run state
    sandbox/          Docker runner, images, result parser
    tools/            file and shell tools the agents can call
    events/           event log and stream for the live view
  ui/                 live run viewer (built late)
  evals/              golden prompts, scoring script, results
  docker/             base images
  tests/              unit tests for gateway, runner and parser
```

- **Typed everything:** Pydantic models for every agent input and output. A reply that fails validation is retried once with the error shown, then counted as a failure.
- **Stack:** Python with FastAPI for the run API and event stream, and a small web page for the live view. This matches the stack you already work in.
- **Orchestration:** LangGraph as a library is a reasonable choice, but keep each node thin so it can be swapped for a plain state machine. Confirm library use in #questions.
- **Commits:** small and daily, so the history proves the work happened during the event. Add a line to NEWWORK.md each day.
- **Secrets and CI:** keys only in an ignored env file, never committed. A GitHub Actions job runs the unit tests on every push.
- **License:** add an open-source license on day one (MIT is the simple default) and credit every dependency of note in the README.

## Foundation checklist

The foundation is done when a script sends a spec to Muse Spark, gets a typed task list back, and the runner executes a sample test suite in Docker. Tonight is setup and questions; Tuesday is the technical groundwork.

**Tonight (Mon Oct 26)**

- [ ] Rewatch the kickoff recording and read #announcements for the credit link and any rule changes
- [ ] Post in #intros: solo, Indie track, multi-agent dev team
- [ ] Ask in #questions whether library use (such as LangGraph) and an open-source base are allowed on the Indie track
- [ ] Log in at dev.meta.ai, add the card, and set a spend alert
- [ ] Request the Modal $100 and Baseten $100 credits
- [ ] Create the public repo with a license, a README stub and the first NEWWORK.md entry

**Day one (Tue Oct 27)**

- [ ] Claim the $150 Meta credits as soon as the link is posted
- [ ] Make the first Muse Spark call, then test streaming, tool calling and structured JSON output
- [ ] Run the same tests on Muse Spark Open through Baseten or Modal, and record which model handles tools best
- [ ] Build the gateway wrapper with the budget guard and token log
- [ ] Build the Docker base image and a runner that executes tests and returns structured results
- [ ] Define the Pydantic models for the spec, tasks, run state and test results
- [ ] Pick the one fixed demo app and write three golden prompts
- [ ] Attend the 9:45 PM Kathmandu workshop, Agents 101 (multimodal agents)

## Evaluation and demo plan

An evaluation harness built early gives both the reliability and the evidence for the technical-depth score, and the demo video is one of the main things judges watch. Build the harness in week one, not at the end.

**Golden prompts (drafts to confirm)**

1. A to-do app with user accounts and a REST API
2. A URL shortener with click analytics
3. A bookstore inventory tracker with search

**Metrics per run:** all tests green or not, number of fix loops, tokens and cost, and wall-clock time. Run each golden prompt several times and put the results in a table in the README.

**Three-minute demo script**

1. 0:00 to 0:20: the problem, then type the idea into the tool.
2. 0:20 to 1:40: a live run. The spec appears, the PM board fills, coders work in parallel, one test fails, the fixer repairs it, and the tests turn green.
3. 1:40 to 2:20: open the generated app and use it.
4. 2:20 to 2:50: under the hood. Which Meta model does what, the cost counter, and the evaluation table.
5. 2:50 to 3:00: what was built during the hackathon, and what comes next.

Record several real runs and keep the best one. Do not stage results, and show a failure and recovery only if it actually happened.

## Risks and mitigations

The biggest risks are an unreliable worker model, running out of credits, and breaking the new-work rule, and each has an early check.

| Risk | Mitigation | Check by |
| --- | --- | --- |
| Muse Spark Open handles tool calls poorly | Run the same tool-call tests on both models. Fall back to Muse Spark for coders. | Oct 27 |
| Credits run out | Budget guard in the gateway, cheap models for development, 25% held back for demo runs | Daily cost review |
| Generated code is flaky | Tests from the spec, bounded loops, one fixed demo app, a narrow stack | Oct 29 first end-to-end run |
| Scope creep | Thin vertical slice first, feature freeze on the night of Nov 3 | Nov 3 |
| New-work rule broken | Fresh repo, NEWWORK.md log, README attribution, confirmation in #questions | Oct 26 to 27 |
| Payment card refused for the API | Test the card before the credits arrive and keep a backup card ready. Modal and Baseten credits are a second route. | Oct 27 |
| Rate limits stall long runs | Read the limits on day one, add queueing and backoff, and request the higher Baseten limit | Oct 27 |
| Demo fails or the API is down at recording time | Record several real runs ahead of time, record on Nov 4 daytime and not in the last hour | Nov 4 |
| Time shortage next to a full-time job | Fixed daily work blocks, with the weekend of Oct 31 to Nov 1 as the main build time (an assumption to confirm) | Oct 28 |

## Build schedule

The foundation covers the first two days; the remaining eight build the vertical slice, close the full loop, and leave the last two days for evaluation, recording and submission. The weekend of Oct 31 to Nov 1 is the main build window.

**Wed Oct 28**

- [ ] Wire the requirements analyst: idea text to a typed spec file, validated on every run
- [ ] Attend the verifiable agents workshop and compare its patterns against the sandbox design; adjust where the design falls short
- [ ] Confirm the daily work blocks for the rest of the event

**Thu Oct 29**

- [ ] Build the PM planner: spec to task graph with dependencies and file ownership
- [ ] First end-to-end run: spec to tasks to one coder to tests in Docker to a structured report. This is the checkpoint for the flaky-code risk.

**Fri Oct 30**

- [ ] Build the test author: acceptance criteria to test files that collect and run before any code exists
- [ ] Build the fixer: trimmed failing log to a patch, with the retry cap enforced by the runner

**Sat Oct 31 to Sun Nov 1 (main build window)**

- [ ] Parallel dev workers with path ownership, two at first, then more if the gateway holds
- [ ] Close the full loop: idea to spec to plan to tests to parallel build to run to fix to green
- [ ] Build the reporter: run log to README draft, run summary and cost report
- [ ] Stand up the evaluation harness: the three golden prompts, the scoring script, results written to `evals/`

**Mon Nov 2**

- [ ] Run each golden prompt several times and start the results table for the README
- [ ] Build the live run viewer from the event stream if the loop is stable; skip it if it is not
- [ ] Rehearse the demo script against a real run and time each segment

**Tue Nov 3**

- [ ] Feature freeze at night. From here on: bug fixes, evals and recording only
- [ ] Finish the evaluation table and the cost summary in the README
- [ ] Record several full demo runs and keep the best takes

**Wed Nov 4 (submission day)**

- [ ] Final recording in the daytime, not the last hour
- [ ] Edit the 3-minute video, upload it, and check the link from a logged-out browser
- [ ] Verify the repo is public, the README table is final, and NEWWORK.md is complete
- [ ] Submit well before 9:00 PM ET, then post the demo on X if going for Most Viral

## Submission checklist

Everything is due Wed Nov 4, 9:00 PM ET. The repo carries the evidence, so most of this is checking links rather than writing new material.

- [ ] Public repo link, with the license and README complete
- [ ] Demo video link in the submission, tested from a logged-out browser
- [ ] README with the evaluation table, the cost report, and attribution for every dependency of note
- [ ] NEWWORK.md updated through submission day, matching the commit history
- [ ] Three golden-prompt apps committed as evaluation results, not as hand-edited demos
- [ ] Submission form fields (title, description, track, bonus prizes) drafted a day early

## Open questions

Decisions that need an organizer answer or a day-one test result, collected here so they do not get lost in Discord threads.

| Question | Where to settle it | Needed by |
| --- | --- | --- |
| Are libraries such as LangGraph allowed on the Indie track? | #questions | Oct 27 |
| Is building on an open-source base within the new-work rule? | #questions | Oct 27 |
| Foreman or Greenlight as the name? | Pick before creating the repo | Oct 26 |
| Muse Spark Open or Muse Spark for the coders? | Day-one tool-call tests on both models | Oct 27 |
| Is the Meta Model API OpenAI-compatible? | The quickstart docs | Oct 27 |
| Weekend of Oct 31 to Nov 1 free for the main build? | Confirm the work schedule | Oct 28 |
