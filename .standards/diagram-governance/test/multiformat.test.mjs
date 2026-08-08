import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { auditMermaid, buildMermaidCandidate, extractMermaidBlocks, semanticFromMermaid } from '../lib/mermaid.mjs';
import { auditExcalidraw, buildExcalidrawCandidate, parseExcalidrawMarkdown, semanticFromExcalidraw } from '../lib/excalidraw.mjs';
import { canonicalSemantic, checkDrift, semanticHash } from '../lib/semantic.mjs';
import { cosineSimilarity } from '../lib/embedding.mjs';

const vault = path.resolve(import.meta.dirname, '..', '..', '..');
const pdi = path.join(vault, '10-项目/基线/01-PDI管理/PRD.md');
const warranty = path.join(vault, '10-项目/基线/30-延保销售/延保销售-状态流转.excalidraw.md');
const compressedWarranty = path.join(vault, '10-项目/基线/02-保修管理/保修单-状态流转.excalidraw.md');

test('extracts stable Mermaid block locators and verifies real syntax', async () => {
  const blocks = extractMermaidBlocks(fs.readFileSync(pdi, 'utf8'));
  assert.deepEqual(blocks.map((block) => [block.line, block.type]), [[41, 'flowchart'], [93, 'stateDiagram-v2']]);
  for (const block of blocks) assert.equal((await auditMermaid(block.source)).syntax_verified, true);
});

test('Mermaid audit rejects executable click directives', async () => {
  const report = await auditMermaid('flowchart LR\nA-->B\nclick A "javascript:alert(1)"');
  assert.equal(report.status, 'fail');
  assert.ok(report.issues.some((item) => item.code === 'unsafe_click_directive'));
});

test('Mermaid candidate preserves statements and adds governed configuration', async () => {
  const source = extractMermaidBlocks(fs.readFileSync(pdi, 'utf8'))[0].source;
  const candidate = buildMermaidCandidate(source);
  assert.equal(candidate.report.source_statements_preserved, true);
  assert.match(candidate.source, /#fff8e1/);
  assert.match(candidate.source, /handDrawn/);
  assert.equal((await auditMermaid(candidate.source)).status, 'pass');
  const graph = semanticFromMermaid(candidate.source);
  assert.equal(graph.nodes.length, 21);
  assert.equal(graph.edges.length, 22);
});

test('parses both JSON and compressed-json Obsidian Excalidraw scenes', () => {
  assert.equal(parseExcalidrawMarkdown(fs.readFileSync(warranty, 'utf8')).encoding, 'json');
  assert.equal(parseExcalidrawMarkdown(fs.readFileSync(compressedWarranty, 'utf8')).encoding, 'compressed-json');
  assert.equal(auditExcalidraw(fs.readFileSync(compressedWarranty, 'utf8')).status, 'pass');
});

test('Excalidraw candidate preserves geometry, IDs and bindings while applying hand-drawn Feishu style', () => {
  const source = fs.readFileSync(warranty, 'utf8');
  const result = buildExcalidrawCandidate(source);
  assert.equal(result.report.element_ids_preserved, true);
  assert.equal(result.report.geometry_preserved, true);
  assert.equal(result.report.bindings_preserved, true);
  assert.equal(auditExcalidraw(result.markdown).status, 'pass');
  assert.ok(result.scene.elements.filter((item) => !item.isDeleted).every((item) => item.roughness === 1));
  const colored = result.scene.elements.filter((item) => ['rectangle', 'ellipse', 'diamond'].includes(item.type)).map((item) => item.backgroundColor);
  assert.ok(colored.includes('#e8f4fd'));
  assert.ok(colored.includes('#fff8e1'));
  assert.ok(colored.includes('#e8f8e8'));
  assert.ok(colored.includes('#f2f6f8'));
  assert.equal(semanticFromExcalidraw(result.scene).nodes.length, 8);
});

test('semantic hash ignores coordinates and source IDs', () => {
  const left = { nodes: [{ id: 'a', label: '创建工单', type: 'process' }, { id: 'b', label: '结束', type: 'terminal' }], edges: [{ id: 'x', from: 'a', to: 'b', label: '', kind: 'flow' }], lanes: [] };
  const right = { nodes: [{ id: 'n1', label: '创建工单', type: 'process', x: 999 }, { id: 'n2', label: '结束', type: 'terminal', x: 1 }], edges: [{ id: 'e9', from: 'n1', to: 'n2', label: '', kind: 'flow' }], lanes: [] };
  assert.deepEqual(canonicalSemantic(left), canonicalSemantic(right));
  assert.equal(semanticHash(left), semanticHash(right));
});

test('drift gate uses only declared process identities', () => {
  const manifest = { entries: [
    { asset_id: 'a', process_id: 'p', semantic_hash: '1', counts: { nodes: 2, edges: 1, lanes: 0 }, embedding: [1, 0] },
    { asset_id: 'b', process_id: 'p', semantic_hash: '2', counts: { nodes: 2, edges: 1, lanes: 0 }, embedding: [0.9, 0.1] },
    { asset_id: 'c', process_id: null, semantic_hash: '1', counts: { nodes: 2, edges: 1, lanes: 0 }, embedding: [1, 0] }
  ] };
  const drift = checkDrift(manifest, { similarityThreshold: 0.8 });
  assert.equal(drift.declared_processes, 1);
  assert.equal(drift.ungrouped_assets, 1);
  assert.equal(drift.processes[0].status, 'aligned');
  assert.ok(cosineSimilarity([1, 0], [0.9, 0.1]) > 0.9);
});
