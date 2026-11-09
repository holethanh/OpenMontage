# Backlot — the living storyboard

A read-only local board that shows a production happening: pipeline stages
lighting up, the script as a screenplay page, the scene plan as a filmstrip
that fills in as assets generate, decisions, spend, and activity — all
derived from what the pipeline already writes to `projects/<id>/`.

```bash
python -m backlot open <project-id>   # start server if needed + open browser
python -m backlot open                # library view (all projects)
python -m backlot serve --port 4750   # run the server in the foreground
```

Backlot binds to `127.0.0.1` and accepts HTTP requests only for `127.0.0.1`
or `localhost`, on the selected port. Other `Host` headers receive HTTP 400
to protect local project data from DNS rebinding.

## How it stays live

No agent involvement. A `watchfiles` watcher on `projects/` publishes change
notifications over SSE; the browser refetches board state. State sources:

| Board element | Disk source |
|---|---|
| identity / rail order | `project.json` + `pipeline_defs/<type>.yaml` |
| stage states, gates, versions | `checkpoint_<stage>.json` + `history/` |
| script card / modal | `artifacts/script.json` |
| filmstrip cards | `scene_plan × script × asset_manifest` join |
| generating shimmer, activity | `events.jsonl` (written by `BaseTool` instrumentation) |
| cost meter | checkpoint `cost_snapshot` |
| renders | `renders/*.mp4` (+ root-level mp4 heuristic) |

Projects without checkpoints degrade gracefully to a "what the watcher
found" view — media, snapshots, renders.

**Replay**: a completed run can be scrubbed end-to-end (▶ REPLAY RUN on the
board) — reconstructed from checkpoint history and event timestamps.

Try it without a real production:

```bash
python scripts/backlot_simulate_run.py          # live demo run (~1 min)
python -m backlot open backlot-demo-run
```

## Series

Several projects can be episodes of one show. Add an optional `series` label
to each episode's `project.json` (or pass `series=` to
`lib.checkpoint.init_project`). Folders stay flat in `projects/`; nothing moves.

```json
{ "title": "Episode 3: The Heist", "pipeline_type": "animation", "series": "my-show" }
```

- The library shows one card per series next to the loose projects.
- `/?series=my-show` opens the series: the usual project grid, filtered, with
  links back to the library and to the series list (`/?view=series`).
- An episode's board links back to its series.

Projects without `series` (including older projects with no `project.json`)
show as loose cards, exactly as before.

Design doc: `internal/design/LIVING_STORYBOARD.md`.

## Affiliate toolkit

A local workspace for affiliate-marketing videos at `/affiliate` (linked from the library).
It is the only part of Backlot that writes: it keeps a product list in
`.backlot/affiliate/store.json` (override with `OPENMONTAGE_AFFILIATE_DIR`) and creates
projects through `lib.checkpoint.init_project`. Write routes reject non-local `Origin`s.

| Tab | What it does |
|---|---|
| Sản phẩm | Product hub: name, affiliate link, price, network, commission %, selling points |
| Tạo video | 5 templates (review, top-N, unboxing/demo, comparison, vertical UGC), hook + CTA generator (vi/en), per-platform caption + hashtags + disclosure, **batch** project creation (max 30 variants) |
| Tuân thủ | Disclosure check, risky-claim scan (guarantees, health/income claims, fake urgency), pre-post checklist |
| Kết quả | Log views/clicks/orders/commission/cost per video; CTR, CVR, EPC, profit, ROI by video, template and hook angle |

Each created project gets `artifacts/affiliate_brief.json`; the agent follows
`skills/creative/affiliate-marketing.md`. Compliance checks are heuristics, not legal advice.
Caption/hook generation is rule-based (offline, no API key needed).
Tests: `python -m pytest tests/backlot/test_affiliate.py`.
