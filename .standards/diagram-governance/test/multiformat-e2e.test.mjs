import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { embedTexts, cosineSimilarity } from '../lib/embedding.mjs';
import { inspectPng } from '../lib/preview.mjs';

const tool = path.resolve(import.meta.dirname, '..');
const cli = path.join(tool, 'cli.mjs');
const vault = path.resolve(tool, '..', '..');
const pdiHost = path.join(vault, '10-项目/基线/01-PDI管理/PRD.md');
const warrantyHost = path.join(vault, '10-项目/基线/30-延保销售/延保销售-状态流转.excalidraw.md');
const hash = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const run = (args, timeout = 180000) => {
  const result = spawnSync(process.execPath, [cli, ...args], { cwd: tool, encoding: 'utf8', timeout, maxBuffer: 20 * 1024 * 1024 });
  if (result.status !== 0) throw new Error(`${result.stderr}\n${result.stdout}`);
  return JSON.parse(result.stdout);
};

test('full Vault audit implements Mermaid and Excalidraw checks', () => {
  const manifest = run(['audit', '--vault', vault, '--json']);
  assert.equal(manifest.summary.mermaid.failed, 0);
  assert.equal(manifest.summary.excalidraw.failed, 0);
  assert.ok(manifest.summary.mermaid.checked >= 145);
  assert.equal(manifest.summary.excalidraw.checked, 7);
  assert.ok(manifest.assets.every((asset) => asset.audit.status !== 'not-yet-implemented'));
});

test('real candidates and official renderers preserve host sources', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-multiformat-e2e-'));
  const pdiHash = hash(pdiHost); const warrantyHash = hash(warrantyHost);
  const mermaidCandidate = path.join(temp, 'pdi.candidate.mmd');
  const excalidrawCandidate = path.join(temp, 'warranty.candidate.excalidraw.md');
  const m = run(['format-candidate', '--vault', vault, '--format', 'mermaid', '--source', '10-项目/基线/01-PDI管理/PRD.md#L41', '--out', mermaidCandidate, '--force', '--json']);
  const x = run(['format-candidate', '--vault', vault, '--format', 'excalidraw', '--source', '10-项目/基线/30-延保销售/延保销售-状态流转.excalidraw.md', '--out', excalidrawCandidate, '--force', '--json']);
  assert.equal(m.report.source_hash_unchanged, true); assert.equal(x.report.source_hash_unchanged, true);
  const mp = run(['format-preview', '--vault', vault, '--format', 'mermaid', '--source', mermaidCandidate, '--out-dir', path.join(temp, 'previews'), '--json'], 240000);
  const xp = run(['format-preview', '--vault', vault, '--format', 'excalidraw', '--source', excalidrawCandidate, '--out-dir', path.join(temp, 'previews'), '--json'], 240000);
  for (const preview of [mp, xp]) {
    assert.equal(preview.manifest.native_viewer_evidence.status, 'pass');
    assert.match(preview.manifest.generator.renderer, /npm:/);
    const hero = path.join(preview.bundle_dir, 'artifacts/hero.png');
    assert.equal(inspectPng(hero).status, 'pass');
    assert.ok(fs.statSync(path.join(preview.bundle_dir, 'artifacts/diagram.svg')).size > 1000);
  }
  assert.equal(hash(pdiHost), pdiHash); assert.equal(hash(warrantyHost), warrantyHash);
});

test('local bge-m3 provides traceable semantic embeddings when available', async (t) => {
  let result;
  try { result = await embedTexts(['取送车服务：上门取车并送回客户', '取送车订单：司机接单后完成送车', '数据库索引维护与备份']); }
  catch (error) { t.skip(`optional local model unavailable: ${error.message}`); return; }
  assert.equal(result.model, 'bge-m3:latest');
  assert.equal(result.dimensions, 1024);
  assert.ok(cosineSimilarity(result.embeddings[0], result.embeddings[1]) > cosineSimilarity(result.embeddings[0], result.embeddings[2]));
});

test('full lineage keeps undeclared assets ungrouped and records embedding metadata', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-lineage-e2e-'));
  const output = path.join(temp, 'lineage.json');
  run(['lineage', '--vault', vault, '--links', path.join(tool, 'config/process-links.json'), '--embedding-model', 'bge-m3:latest', '--out', output, '--json'], 240000);
  const manifest = JSON.parse(fs.readFileSync(output, 'utf8'));
  assert.equal(manifest.entries.length, 176);
  assert.equal(manifest.embedding.model, 'bge-m3:latest');
  assert.equal(manifest.embedding.dimensions, 1024);
  assert.equal(manifest.drift.declared_processes, 3);
  assert.ok(manifest.drift.ungrouped_assets > 150);
  assert.ok(manifest.drift.processes.every((process) => process.status === 'aligned'));
});
