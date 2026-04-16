# cairn

A triage feed for the orchestrator. Each tick drops a stone on the pile; the pile tells you where you are on the trail.

## Why not a kanban board?

A kanban implies a factory: columns, WIP limits, things moving left-to-right until they're "done." That's a worker's view — useful when you're running the line. But Konrad isn't running the line. He steps away for hours, drops back in on a phone, and needs the page to answer one question first: **what does the orchestrator need from me right now?**

A feed answers that. A board makes you scan columns to find out.

## Five sections, ordered by "what matters now"

When you open cairn cold:

1. **Health strip** — single-row banner. Queue depth, last tick, calibration dots, deploy state per project. Glance-only.
2. **Needs you** — pinned red/amber. Pending gates, blocked items, waiting items. Empty state is a positive message: "nothing pending — you're clear."
3. **What shipped (last 24h)** — one card per completed item. Outcome, commit, `Rollback:` command pre-filled from the trailer, verify link if present.
4. **What's running** — in-progress items with start time and worker.
5. **What I'm watching** — idle-beat artifacts from the last 7 days. Bridge mining, vault hygiene, dep audits, lesson surfacing. Grouped by beat, newest first.

## Data sources

All reads, all local files:

- `/mnt/c/ObsidianNotes/.hive/work-queue.yaml` — queue items + metadata (gates, ticks, idle_beat_history)
- `/mnt/c/ObsidianNotes/.hive/iterations/*.jsonl` — iteration traces per item
- `/mnt/c/ObsidianNotes/.hive/idle-beats/*.jsonl` — idle beat traces
- `/mnt/c/ObsidianNotes/.hive/verifier-calibration-results.jsonl` — calibration history
- `git log` on registered project paths — for commit trailers (`Orchestrator-Item`, `Rollback`, `Verifier-Skill-Ref`)

## Build

```
make build    # python3 build.py -> index.html
make view     # open index.html in a browser
```

Python stdlib + PyYAML. No framework. One static file. Rerun when you want a fresh view.

## Status

v1 prototype — disposable, reaction-driven. If the static render isn't enough, that's the next iteration's problem. The name `cairn` is locked; the service will eventually live at `cairn.localhost`.
