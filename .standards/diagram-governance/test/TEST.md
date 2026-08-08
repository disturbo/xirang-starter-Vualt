# Diagram Governance CLI Test Plan

## v0.6 multi-format completion contract

Scope: extend the proven Draw.io pipeline to Mermaid and Obsidian Excalidraw without
changing any Vault source diagram. The official Mermaid and Excalidraw libraries are
the only accepted renderers. `bge-m3:latest` through the local Ollama API is an optional
semantic matching accelerator; deterministic extraction and drift checks must still run
when Ollama is unavailable.

Required unit coverage:

- Extract stable Mermaid block locators and validate every block with `mermaid.parse`.
- Preserve Mermaid business statements while producing a Feishu-palette candidate;
  reject unsafe click directives and malformed diagrams.
- Parse Obsidian Excalidraw Markdown, validate IDs, bindings, bound-text references,
  finite geometry, and the hand-drawn `roughness` contract.
- Produce Excalidraw candidates that preserve element IDs, positions, bindings and
  text while normalizing only the requested palette/hand-drawn style.
- Extract a coordinate-free semantic graph (`nodes`, `edges`, `lanes`, `labels`) from
  all three formats and compute a stable canonical hash.
- Build lineage entries with source hash, semantic hash, authority, modification policy,
  renderer identity and optional embedding metadata.
- Detect exact, structurally changed and semantically similar drift states. Embedding
  failures must degrade to deterministic matching rather than fail the whole inventory.
- Keep all existing Draw.io tests and commands backwards compatible.

Required real Vault E2E coverage:

- Audit all Mermaid and Excalidraw assets and eliminate every
  `not-yet-implemented` audit status.
- Generate isolated candidates for one real PDI Mermaid block and one real extended
  warranty Excalidraw drawing; source hashes must remain unchanged.
- Render the Mermaid candidate through official Mermaid and the Excalidraw candidate
  through official `@excalidraw/excalidraw`, then verify SVG evidence and non-blank PNG.
- Call local Ollama `bge-m3:latest`, record its model and vector dimension, and prove
  a same-domain pair scores above an unrelated-domain pair. If the model is unavailable,
  mark only this optional E2E assertion skipped.
- Build the full Vault lineage manifest and run drift checking without assigning false
  cross-format equivalence to assets that have no declared process identity.
- Preserve hashes of all formal Draw.io, Mermaid-host Markdown and Excalidraw sources.

## Test inventory plan

- `governance.test.mjs`: existing geometry, routing, layout, theme, classification, and preview-bundle unit tests; target 20+ tests.
- `e2e.test.mjs`: real Draw.io files, CLI subprocesses, batch-plan isolation, candidate generation, clipped-lane repair, and real Chrome/diagrams.net preview; 9 end-to-end workflows.

## Unit test plan

### `lib/pipeline.mjs`

- Classify formal Draw.io files into preserve, horizontal-swimlane reflow, dedicated state-machine strategy, or unsupported/manual review.
- Never select acyclic reflow for cyclic/state-machine diagrams.
- Keep semantic blockers separate from geometric and visual failures.
- Produce deterministic output names and collision-safe IDs.

### `lib/preview.mjs`

- Stable source fingerprint and cache key.
- Preview bundle manifest uses only relative artifact paths.
- PNG signature, dimensions, file size, and non-blank pixel/content checks.
- `latest` is read-only and returns the newest valid bundle.

### CLI parsing

- Top-level `--help` succeeds.
- Every machine-oriented command supports `--json`.
- Unknown options fail loudly.

## E2E test plan

### Workflow: vault batch planning

- Simulates: scanning all formal Draw.io files before any generation.
- Operations: `batch plan --vault ... --json`.
- Verified: every formal source appears once; no source hash changes; strategy and blockers are explicit.

### Workflow: isolated batch candidate generation

- Simulates: producing candidates without touching formal sources.
- Operations: generate into a temporary output directory.
- Verified: candidate/report pairs exist, failed items do not stop other items, source hashes remain unchanged.

### Workflow: truthful Draw.io preview

- Simulates: visual QA of a generated candidate.
- Operations: call real Google Chrome against the official `viewer.diagrams.net` lightbox, require Draw.io SVG/source-label DOM evidence, and publish a preview bundle.
- Verified: PNG signature, dimensions, non-trivial file size, non-blank pixel/content check, valid `manifest.json` and `summary.json`, artifact paths exist.
- Backend limitation: Google Chrome and network access to `viewer.diagrams.net` are hard requirements; browser error pages and source-label mismatches fail rather than silently passing as non-blank images.

### Workflow: CLI from outside the project directory

- Simulates: an agent invoking the tool by absolute path.
- Operations: run help, plan, candidate, preview capture as subprocesses without relying on the current directory.
- Verified: JSON output is parseable and paths are absolute or vault-relative as documented.

## Realistic acceptance workflow

1. Scan Vault assets.
2. Classify each formal Draw.io by safe automatic strategy.
3. Generate derived candidates only for supported strategies.
4. Run geometry, layout, theme, and semantic gates independently.
5. Render A-layer-passing candidates through the real diagrams.net backend.
6. Publish immutable preview bundles and a batch summary.
7. Preserve formal source hashes throughout.

## Results

Verified on 2026-07-18 for `v0.6`:

- Unit suite: `42/42` passed (`35` retained Draw.io tests + `7` multi-format tests).
- Real-file E2E suite: `13/13` passed (`9` retained Draw.io workflows + `4` multi-format workflows).
- Full Vault specialized audit: `145/145` Mermaid and `7/7` Excalidraw passed; no `not-yet-implemented` status remains.
- Full multi-format candidate generation: `152/152` passed, `0` errors and `0` source diagrams modified.
- Official representative renders: PDI through `mermaid@11.16.0`; extended-warranty state flow through `@excalidraw/excalidraw@0.18.1`; both produced label-verified SVG and non-blank PNG.
- Full lineage: `176` entries; local `bge-m3:latest` returned `1024` dimensions. Three declared cross-format processes passed the `0.82` similarity threshold; all undeclared assets remained ungrouped.

Verified on 2026-07-18 for `v0.5`:

- Unit suite: `35/35` passed.
- Real-file E2E suite: `9/9` passed.
- Full Vault batch plan: `24` formal Draw.io files classified exactly once; `24` automatic and `0` blocked.
- Cyclic business flows: `8` detected and `8` automatic; dedicated state machines: `2` detected and `2` automatic through the preserve-layout transition engine.
- Batch generation: `18 pass`, `6 review-required`, `0 blocked`, `0 geometry/layout/theme failures`, `0 source files modified`.
- Native batch preview: `24/24` real diagrams.net bundles passed, `0` failed; the final resumable run reused `19` verified bundles and rendered only the `5` changed fingerprints.
- Preview verification includes browser-error rejection, Draw.io SVG presence, source-label matches, PNG signature, `2200×900` dimensions, non-trivial byte size, luminance variance, dark-pixel fraction, and cross-bundle image uniqueness.

Validation commands:

```bash
node --test .standards/diagram-governance/test/governance.test.mjs
node --test .standards/diagram-governance/test/e2e.test.mjs
```

Known coverage gap: unsafe/oversized state machines that require moving states, and complex cyclic flows whose lane ownership cannot be recovered, remain intentionally blocked rather than approximated.

## v0.4 state-transition normalization result

Scope: unlock only small dedicated state machines whose existing node layout already passes readability and overlap gates. Preserve every state node's geometry and text; normalize transition anchors, orthogonal paths, return transitions, and self-loops. Formal sources remain immutable and business semantics are never inferred.

Verified unit coverage:

- Classify a dedicated state machine as automatic only when all transitions are bound, node layout passes, no node overlap exists, and the graph stays within the small-state safety limit.
- Preserve all state-node geometry and text while allowing theme styles and transition-edge geometry to change.
- Keep the main state progression on the shortest readable route; place rejection, resubmission, deletion, expiration, and other return transitions on separate orthogonal channels.
- Route self-loops outside the owning state and reject loops that cross another node or collapse through the state itself.
- Require explicit source/target ports, zero diagonal segments, zero edge-through-node findings, and zero coincident transition segments.
- Keep unsafe, overlapping, unbound, or oversized state machines blocked rather than approximated.

Verified real-file E2E coverage:

- Generate isolated candidates for the 商务补偿 and 延保销售 state machines without modifying either formal source.
- Verify both candidates preserve node geometry and pass geometry/layout/theme gates.
- Render both candidates through the real diagrams.net viewer, require source-label DOM evidence, and inspect the immutable PNG bundles.
- Re-run the full Vault batch; only report `24/24` automatic if both real state-machine candidates and the complete regression suite pass.

## v0.5 deterministic geometry-closure result

Scope: eliminate only deterministic A-layer false positives and safely repairable geometry failures. Do not infer free-endpoint business ownership or missing decision semantics.

Required coverage:

- Parse explicit edge waypoints only from `<Array as="points">`; never treat label `<mxPoint as="offset">` as a route segment.
- After completing missing ports, detect and reroute any newly exposed diagonal path in a bounded second routing pass.
- Keep genuinely free business endpoints as blockers unless the owning source/target node is unambiguous.
- Fail a logical node whose geometry falls outside its declared swimlane parent with `node_outside_container`; candidate remediation must reparent it to the unique containing lane, or uniformly expand an aligned lane group for a small right/bottom overflow, preserve node positions, and normalize affected connector parents so the native renderer cannot clip the node.
- Verify the four current geometry-review candidates individually before changing full-batch counts.
- Re-run native preview only for newly A-layer-eligible fingerprints and preserve every formal source hash.

Real-file E2E coverage:

- Baseline alert flow must repair six nodes assigned to the wrong lane and pass all deterministic gates.
- Compensation-area maintenance must expand its aligned vertical lane group without moving business nodes.
- Battery traceability must expand its aligned horizontal lane group while retaining semantic review and passing geometry/layout/theme.
- All three real sources preserve their original hashes, and all unexpected cell-change lists remain empty.

## v0.3a cyclic business-flow refinement result

Scope: unlock only cyclic diagrams whose existing node layout already passes readability and has no node overlap. Preserve node positions and route feedback edges through isolated outer return channels. Do not infer missing business labels.

Verified unit coverage:

- Detect strongly connected components and feedback edges without classifying ordinary cross-lane forward edges as returns.
- Prefer natural top/bottom U-shaped feedback paths, while allowing a mixed-side outer path when obstacle clearance requires it.
- Keep return channels separated by `36px`, use explicit orthogonal waypoints, and preserve all node geometry.
- Mark readable, non-overlapping cyclic business flows as automatic.
- Continue blocking tall cyclic scrolls and cyclic diagrams with node overlap.
- Keep semantic decision blockers independent from geometry/layout success.

Verified E2E coverage:

- Generate a PDI cyclic candidate through the CLI and verify formal source hash preservation.
- Verify the candidate contains governed return-channel metadata and passes geometry/layout/theme gates.
- Render the PDI candidate through the real diagrams.net viewer and require source-label DOM evidence plus a valid PNG bundle.
- Re-run the full Vault batch and confirm the previously blocked cyclic count decreases without modifying formal sources.

## v0.3b cyclic node-reflow result

Scope: unlock cyclic business flows that are structurally valid but currently fail because the canvas is too tall or nodes overlap. Formal sources and dedicated state machines remain immutable/blocked.

Verified unit coverage:

- Select directed DFS back edges as the minimum feedback set; do not misclassify ordinary cross-role forward edges merely because their old coordinates move upward/leftward.
- Remove feedback edges and require the remaining business graph to be acyclic before ranked node movement is allowed.
- Infer lane ownership from existing parent or geometric containment without inferring business semantics.
- Convert vertical role columns to horizontal swimlanes; preserve already-horizontal lane order.
- Place nodes by business rank, cap each reading track at `12` ranks, and use a second track for longer flows.
- Raise reflowed node text to `13px`, maintain at least `20px` node separation, and preserve node labels/shapes.
- Route feedback and track-wrap transitions through explicit outer orthogonal channels.
- Keep incomplete decision branches in `semantic: review-required` even when geometry/layout/theme pass.

Verified E2E coverage:

- Generate and render representative candidates for the blocked 商务补偿 and 代步车 flows.
- Require both candidates to pass geometry/layout/theme, with 代步车 remaining semantic review-required.
- Confirm the Vault plan decreases to only dedicated state-machine blockers and every formal source hash remains unchanged.
