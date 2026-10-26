# New work log

Dated record of what was built during the Meta hackathon. This file should stay aligned with the commit history.

## 2026-10-26

- Initialized Kiln as a new Python 3.12 project. No application code was imported from another product.
- Added the package layout for the gateway, agents, orchestration graph, sandbox, tools, and event log.
- Wired FastAPI, Pydantic models for the spec, task graph, and test report, and a LangGraph pipeline whose nodes do not call models yet.
- Added the model gateway: OpenAI-compatible client for Meta Model API, role routing, retry, token log, and a hard token ceiling.
- Added the Docker sandbox contract: no network, resource caps, no secrets in the container, and a JUnit result parser.
- Meta Model API credits were already requested. The GitHub repo `Uknowme-h/kiln` is still private.
- Added the floor view: a typed SSE event contract (`events/schema.py`), a scripted demo feed (`events/demo.py`), and a no-build-step office UI (`ui/`) served by FastAPI at `/`. Agents sit at desks, envelopes fly on hand-offs, a CI rack shows red/green, and a token meter tracks spend. The demo feed calls no models and writes no files; real events replace it once the LangGraph nodes emit them.
- Rebuilt the UI as a pixel-art office in the style of office-floor agent viewers. Every visual is drawn procedurally in code (`ui/engine.js`, `characters.js`, `props.js`, `scene.js`): a custom cast of seven agent sprites (one per role), desks with PCs, a cork task board, plants, and the CI rack. No image assets are bundled, so nothing is licensed or pre-dates the hackathon. Studied the referenced repo's design system for the palette and the procedural-sprite technique; the character designs, layout, and all code are our own.
- Furnished the office into named zones, all procedural: a meeting corner (conference table with chairs, whiteboard with a scribbled flowchart, easel task board), a wall with windows, a rocket poster, the KILN studio sign, and a wall clock that shows real time; a docs corner (bookshelf, file cabinet, printer table) next to the reporter; a break nook (couch, water cooler, coffee counter, ficus); hanging zone signs and floor decals. Agents sit behind their desks with the PC in front of them.

## 2026-10-27

- Separated the office into real rooms, following the reference repo's approach of wall segments with door gaps: the meeting room is walled with two doorways, cubicle partitions divide the plan desks, a divider with a doorway marks the docs corner, and the break nook sits on warm wood flooring. Regrouped the furniture (break items in a row, bins against walls) so nothing floats.
- Added event-driven station visits: the requirements analyst walks to the whiteboard, the fixer walks to a failing worker's desk when a help message arrives, and the reporter walks to the printer; each walks home when its status goes done. Desks stay home while bodies move. Task assignments pin a color-coded sticky note on the cork board, and notes get a green check when the run goes green. The demo feed now emits `done` for the planning roles so the floor settles at the end.
- Added `scripts/cdp_shot.py`, a dev-only helper that screenshots the floor mid-run over the Chrome DevTools Protocol (used to verify the animations; not part of the app).
- Replaced the ad-hoc walks with a logical office choreography. Every movement maps to a real office action: a run starts with a kickoff meeting (analyst + PM walk to the conference table), the analyst drafts the spec at the meeting-room whiteboard, the PM takes the spec back to his desk to plan, assigned tasks pin on the sprint board and each worker walks over to collect their card (standup), the failing dev's desk gets a pair-debugging visit from the fixer, and the reporter walks to the printer. The run ends with a celebrate hop from everyone. The meeting room was re-laid (whiteboard left, table right) so routes never cross furniture, and desks glow only while their owner is seated and working.
- Demo runs can no longer interleave: a new `POST /demo/run` cancels the run still playing.
