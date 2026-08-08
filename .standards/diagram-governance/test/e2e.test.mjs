import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { inspectPng } from '../lib/preview.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const governanceRoot = path.resolve(here, '..');
const cli = path.join(governanceRoot, 'cli.mjs');
const vault = path.resolve(governanceRoot, '..', '..');
const pickupSource = path.join(vault, '10-项目/基线/05-取送车服务/流程01-取送车主流程.drawio');
const pdiSource = path.join(vault, '10-项目/基线/01-PDI管理/流程01-PDI主业务流.drawio');
const compensationSource = path.join(vault, '10-项目/基线/06-商务补偿/流程01-商务补偿端到端流程.drawio');
const compensationStateSource = path.join(vault, '10-项目/基线/06-商务补偿/商务补偿-状态机.drawio');
const extendedWarrantyStateSource = path.join(vault, '10-项目/基线/30-延保销售/延保销售-状态机.drawio');
const loanerSource = path.join(vault, '10-项目/迭代/260725迭代/25-代步服务/后续规划候选包-v2.0-待评审/流程01-代步车主流程.drawio');
const baselineAlertSource = path.join(vault, '10-项目/基线/31-服务工单管理/流程02-告警工单处理流程.drawio');
const compensationAreaSource = path.join(vault, '10-项目/基线/06-商务补偿/06-商务补偿-审核区域维护-flow.drawio');
const batteryTraceSource = path.join(vault, '10-项目/迭代/260725迭代/21-电池与关键零部件追溯/21-电池与关键零部件追溯-flow.drawio');
const pdiBatchCandidate = path.join(governanceRoot, 'candidates/batch-v0.3/drawio-8733be222b95-流程01-PDI主业务流.candidate.drawio');
const verifiedPreviewRoot = path.join(governanceRoot, 'previews/bundles');
const verifiedStatePreviewRoot = path.join(governanceRoot, 'previews/representatives/v0.4');
let verifiedPreviewFixture = null;

function run(args, options = {}) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd: options.cwd || os.tmpdir(),
    encoding: 'utf8',
    timeout: options.timeout || 120000
  });
  if (result.error) throw result.error;
  assert.equal(result.status, options.status ?? 0, `stderr=${result.stderr}\nstdout=${result.stdout}`);
  return result;
}

function hash(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

test('top-level help succeeds outside the project directory', () => {
  const result = run(['--help']);
  assert.match(result.stdout, /batch-plan/);
  assert.match(result.stdout, /preview-capture/);
});

test('batch plan returns every formal Draw.io exactly once as machine-readable JSON', () => {
  const before = hash(pickupSource);
  const result = run(['batch-plan', '--vault', vault, '--json']);
  const plan = JSON.parse(result.stdout);
  assert.equal(plan.summary.formal_drawio, plan.items.length);
  assert.ok(plan.summary.formal_drawio >= 24);
  assert.equal(new Set(plan.items.map((item) => item.path)).size, plan.items.length);
  assert.ok(plan.items.some((item) => item.strategy === 'horizontal-swimlane-reflow'));
  assert.ok(plan.items.some((item) => item.strategy === 'dedicated-state-machine'));
  assert.ok(plan.items.some((item) => item.strategy === 'dedicated-cyclic-flow'));
  assert.equal(plan.items.filter((item) => item.strategy === 'dedicated-cyclic-flow' && item.automatic).length, 8);
  assert.equal(plan.items.filter((item) => item.strategy === 'dedicated-cyclic-flow' && !item.automatic).length, 0);
  assert.equal(plan.items.filter((item) => item.strategy === 'dedicated-state-machine' && item.automatic).length, 2);
  assert.equal(plan.items.filter((item) => !item.automatic).length, 0);
  assert.equal(hash(pickupSource), before);
});

test('PDI cyclic candidate uses governed outer returns and preserves the formal source', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-cyclic-e2e-'));
  const candidate = path.join(temp, 'pdi.candidate.drawio');
  const before = hash(pdiSource);
  const result = run(['candidate', '--vault', vault, '--source', pdiSource, '--out', candidate, '--layout', '--theme', '--force', '--json']);
  const report = JSON.parse(result.stdout).result;
  assert.deepEqual(report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'pass' });
  assert.equal(report.layout.strategy, 'cyclic-business-flow-outer-return-channels');
  assert.equal(report.layout.node_cells_changed, 0);
  assert.ok(report.layout.feedback_edges.length > 0);
  assert.match(fs.readFileSync(candidate, 'utf8'), /governedReturn=1/);
  assert.equal(hash(pdiSource), before);
});

test('ranked cyclic CLI reflow fixes real tall and overlapping flows without changing sources', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-ranked-cyclic-e2e-'));
  const cases = [
    { source: compensationSource, name: 'compensation', semantic: 'pass' },
    { source: loanerSource, name: 'loaner', semantic: 'review-required' }
  ];
  for (const item of cases) {
    const before = hash(item.source);
    const candidate = path.join(temp, `${item.name}.candidate.drawio`);
    const output = JSON.parse(run(['candidate', '--vault', vault, '--source', item.source, '--out', candidate, '--layout', '--theme', '--force', '--json']).stdout);
    const report = output.result;
    assert.equal(report.layout.strategy, 'cyclic-business-flow-ranked-lanes-with-outer-returns');
    assert.equal(report.gates.geometry, 'pass');
    assert.equal(report.gates.layout, 'pass');
    assert.equal(report.gates.theme, 'pass');
    assert.equal(report.gates.semantic, item.semantic);
    assert.ok(report.layout.after.effective_font_size >= 10);
    assert.match(fs.readFileSync(candidate, 'utf8'), /governedReturn=1/);
    assert.equal(hash(item.source), before);
  }
});

test('real state machines preserve states, normalize transitions, and use verified native previews', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-state-machine-e2e-'));
  const cases = [
    { source: compensationStateSource, name: 'compensation-state' },
    { source: extendedWarrantyStateSource, name: 'extended-warranty-state' }
  ];
  for (const item of cases) {
    const before = hash(item.source);
    const candidate = path.join(temp, `${item.name}.candidate.drawio`);
    const output = JSON.parse(run(['candidate', '--vault', vault, '--source', item.source, '--out', candidate, '--layout', '--theme', '--force', '--json']).stdout);
    const report = output.result;
    assert.equal(report.layout.strategy, 'state-transition-preserve-layout');
    assert.equal(report.layout.node_cells_changed, 0);
    assert.deepEqual(report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'pass' });
    assert.ok(report.routing.changes.every((change) => change.rerouted));
    assert.equal(hash(item.source), before);

    const preview = JSON.parse(run(['preview-capture', '--vault', vault, '--source', candidate, '--out-dir', verifiedStatePreviewRoot, '--json'], { timeout: 360000 }).stdout);
    assert.equal(preview.status, 'ok');
    const manifest = JSON.parse(fs.readFileSync(preview.manifest_path, 'utf8'));
    assert.equal(manifest.native_viewer_evidence.status, 'pass');
    assert.ok(manifest.native_viewer_evidence.matched_labels.length > 0);
    assert.equal(inspectPng(path.join(preview.bundle_dir, manifest.artifacts[0].path)).status, 'pass');
  }
});

test('real clipped-lane cases repair membership or aligned lane bounds without changing sources', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-container-e2e-'));
  const cases = [
    { source: baselineAlertSource, name: 'baseline-alert', semantic: 'pass', changedNodes: 6, expanded: 0 },
    { source: compensationAreaSource, name: 'compensation-area', semantic: 'pass', changedNodes: 0, expanded: 4 },
    { source: batteryTraceSource, name: 'battery-trace', semantic: 'review-required', changedNodes: 0, expanded: 6 }
  ];
  for (const item of cases) {
    const before = hash(item.source);
    const candidate = path.join(temp, `${item.name}.candidate.drawio`);
    const report = JSON.parse(run(['candidate', '--vault', vault, '--source', item.source, '--out', candidate, '--layout', '--theme', '--force', '--json']).stdout).result;
    assert.deepEqual(report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: item.semantic });
    assert.equal(report.routing.container_membership.changed_nodes.length, item.changedNodes);
    assert.equal(report.routing.container_membership.expanded_containers.length, item.expanded);
    assert.equal(report.routing.unexpected_changed_cells.length, 0);
    assert.equal(hash(item.source), before);
  }
});

test('batch generation isolates output and preserves the formal source hash', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-governance-e2e-'));
  const out = path.join(temp, 'candidates');
  const before = hash(pickupSource);
  const result = run(['batch-generate', '--vault', vault, '--out-dir', out, '--filter', '取送车主流程', '--theme', '--force', '--json']);
  const batch = JSON.parse(result.stdout);
  assert.equal(batch.items.length, 1);
  assert.equal(batch.items[0].source_hash_unchanged, true);
  assert.ok(fs.existsSync(path.join(vault, batch.items[0].candidate_path)) || fs.existsSync(path.join(out, path.basename(batch.items[0].candidate_path))));
  assert.equal(hash(pickupSource), before);
});

test('verified Chrome and diagrams.net preview bundle satisfies native evidence gates', () => {
  assert.ok(fs.existsSync(pdiBatchCandidate), 'v0.3 batch candidate fixture is missing');
  const result = run(['preview-capture', '--vault', vault, '--source', pdiBatchCandidate, '--out-dir', verifiedPreviewRoot, '--json']);
  const preview = JSON.parse(result.stdout);
  assert.equal(preview.status, 'ok');
  assert.ok(fs.existsSync(preview.manifest_path));
  const manifest = JSON.parse(fs.readFileSync(preview.manifest_path, 'utf8'));
  assert.equal(manifest.protocol_version, 'preview-bundle/v1');
  assert.equal(manifest.generator.backend.includes('Chrome') || manifest.generator.backend.includes('Chromium'), true);
  assert.equal(manifest.generator.renderer, 'https://viewer.diagrams.net/');
  assert.equal(manifest.native_viewer_evidence.status, 'pass');
  assert.ok(manifest.native_viewer_evidence.matched_labels.length > 0);
  assert.ok(manifest.native_viewer_evidence.svg_text_count > 0);
  const hero = path.join(preview.bundle_dir, manifest.artifacts[0].path);
  const inspection = inspectPng(hero);
  assert.equal(inspection.status, 'pass');
  assert.equal(inspection.width, 2200);
  assert.equal(inspection.height, 900);
  console.log(`\n  Verified preview: ${hero} (${inspection.bytes.toLocaleString()} bytes)`);

  const latestResult = run(['preview-latest', '--vault', vault, '--source', pdiBatchCandidate, '--out-dir', verifiedPreviewRoot, '--json']);
  const latest = JSON.parse(latestResult.stdout);
  assert.equal(latest.status, 'ok');
  assert.equal(latest.bundle_dir, preview.bundle_dir);
  verifiedPreviewFixture = { root: verifiedPreviewRoot, candidate: pdiBatchCandidate, bundleDir: preview.bundle_dir };
});

test('preview capture reuses the verified fingerprinted bundle without another render', (t) => {
  if (!verifiedPreviewFixture) {
    t.skip('the preceding real-viewer test did not publish a bundle');
    return;
  }
  const { root, candidate, bundleDir } = verifiedPreviewFixture;
  const second = JSON.parse(run(['preview-capture', '--vault', vault, '--source', candidate, '--out-dir', root, '--json']).stdout);
  assert.equal(second.cached, true);
  assert.equal(second.bundle_dir, bundleDir);
});
