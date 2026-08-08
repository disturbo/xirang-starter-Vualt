---
name: diagram-governance
description: Govern, classify, repair, theme, batch-process, semantically link, and truthfully preview Draw.io, Mermaid, and Obsidian Excalidraw diagrams. Use for non-destructive Vault audits, Feishu-palette candidates, Draw.io routing, official renderer previews, lineage, and drift checks.
---

# Diagram governance

Operate from the Vault root and use `.standards/diagram-governance/cli.mjs`.

## Safety contract

- Treat `10-项目/基线/` and `10-项目/迭代/` Draw.io files as formal sources.
- Never overwrite a formal source. Write only candidates, reports, and previews under `.standards/diagram-governance/`.
- Mermaid remains owned by its host Markdown and Excalidraw remains a hand-drawn source. Generate isolated `.mmd` or `.excalidraw.md` candidates; never rewrite the host note.
- Only compare cross-format assets that are explicitly declared in `config/process-links.json`. Never infer authority or equivalence from embedding similarity alone.
- Run `batch-plan` before batch generation.
- Do not apply the acyclic swimlane reflow to `dedicated-cyclic-flow` or `dedicated-state-machine` items. For automatic cyclic items, dispatch only the planner-selected `outer-return-channels` or `ranked-lane-reflow` engine. For an automatic state machine, dispatch only `state-transition-preserve-layout`.
- Cyclic outer-channel routing must preserve every node geometry, use explicit orthogonal waypoints, and keep independent return channels at least `36px` apart.
- Ranked cyclic reflow may move nodes only when lane ownership is recoverable and removing the selected feedback edges yields a DAG. Keep labels/shapes, use at most `12` ranks per track, and keep semantic blockers unresolved.
- State-transition normalization must preserve every state node's geometry and text. Require a passing existing layout, no state overlap, all transitions bound, at most `12` states and `24` transitions. Recompute every transition, route self-loops outside the owning state, and pin upward-return labels away from the main state chain.
- Parse route waypoints only from `<Array as="points">`; never treat label offsets as path geometry. Preserve valid corner and split-edge ports, and remove free endpoints only when the business edge already has both source and target bindings.
- Treat `node_outside_container` as a hard geometry failure. Repair only when the node belongs to one unique containing sibling lane, or when a right/bottom overflow of at most `80px` can be resolved by uniformly expanding an aligned lane group with a `4px` safety margin. Preserve node absolute positions and normalize only affected connector parents.
- Ranked swimlane reflow must derive each business-rank column width from its widest node rather than assuming fixed columns.
- Keep missing decision labels as semantic blockers; never invent business conditions.

## Standard workflow

```bash
node .standards/diagram-governance/cli.mjs batch-plan \
  --vault /absolute/path/to/vault \
  --out .standards/diagram-governance/reports/batch-plan.json \
  --json

node .standards/diagram-governance/cli.mjs batch-generate \
  --vault /absolute/path/to/vault \
  --out-dir .standards/diagram-governance/candidates/batch \
  --theme --force \
  --report .standards/diagram-governance/reports/batch-generation.json

node .standards/diagram-governance/cli.mjs batch-preview \
  --vault /absolute/path/to/vault \
  --input .standards/diagram-governance/reports/batch-generation.json \
  --out-dir .standards/diagram-governance/previews/bundles \
  --report .standards/diagram-governance/reports/batch-preview.json
```

For Mermaid and Excalidraw use `multi-generate` followed by `multi-preview`. Use `lineage` for coordinate-free semantic extraction and optional local `bge-m3:latest` similarity. The deterministic gates remain authoritative; embeddings only rank or confirm declared relationships.

Use `--json` for agent parsing. Use `--filter` and `--limit` for representative-first delivery.

## Gates

- `geometry`: ports, orthogonal segments, node crossings, overlaps, container clipping, and routing correctness.
- `layout`: aspect ratio and minimum effective font in a 1920×1080 review viewport.
- `theme`: exact Feishu reference palette only.
- `semantic`: missing or incomplete business branches; human review may be required.

Only send geometry/layout/theme passing candidates to native preview. A semantic review item may be previewed, but must remain visibly marked as unresolved.

## Preview truthfulness

Use `preview-capture` or `batch-preview`. They invoke the official `viewer.diagrams.net` renderer through headless Google Chrome, then require Draw.io SVG content, a source-label match, no browser error signature, and valid non-blank PNG statistics before publishing immutable `preview-bundle/v1` bundles. Fail if Chrome or the real renderer is unavailable; do not substitute a fake renderer.

Preview batches are resumable: valid fingerprinted bundles are reused, transient viewer failures are retried with bounded backoff, and a later run renders only unresolved items.

Mermaid preview must identify `npm:mermaid@11.16.0`; Excalidraw preview must identify `npm:@excalidraw/excalidraw@0.18.1`. Both must publish the official SVG plus a Chrome PNG and match at least one source label. A generic custom SVG is not an accepted substitute.

## Validation

```bash
node --test .standards/diagram-governance/test/governance.test.mjs
node --test .standards/diagram-governance/test/e2e.test.mjs
node --test .standards/diagram-governance/test/multiformat.test.mjs
node --test .standards/diagram-governance/test/multiformat-e2e.test.mjs
```

Read `README.md` for command details and `test/TEST.md` for the verified workflow inventory.
