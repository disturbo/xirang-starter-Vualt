import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { auditDrawio } from '../lib/drawio.mjs';
import { buildInventory } from '../lib/inventory.mjs';
import { remediateDrawio } from '../lib/candidate.mjs';
import { auditDrawioTheme, normalizeDrawioTheme } from '../lib/theme.mjs';
import { auditDrawioLayout, classifyLayoutStrategy, reflowCyclicBusinessDrawio, reflowRankedCyclicBusinessDrawio, reflowSwimlaneDrawio } from '../lib/layout.mjs';
import { buildCandidate, planDrawioSource } from '../lib/pipeline.mjs';
import {
  PREVIEW_RECIPES,
  expectedDiagramLabels,
  selectPreviewEligible,
  validateNativeViewerEvidence
} from '../lib/preview.mjs';

const processStyle = 'rounded=1;whiteSpace=wrap;html=1;';
const edgeStyle = 'edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0.5;entryX=0;entryY=0.5;exitPerimeter=1;entryPerimeter=1;';

function graph(extra) {
  return `<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>${extra}</root></mxGraphModel>`;
}

function node(id, x, y, parent = '1', style = processStyle, value = id) {
  return `<mxCell id="${id}" value="${value}" style="${style}" parent="${parent}" vertex="1"><mxGeometry x="${x}" y="${y}" width="120" height="50" as="geometry"/></mxCell>`;
}

test('passes a bound horizontal business edge', () => {
  const xml = graph(`${node('a', 0, 0)}${node('b', 200, 0)}<mxCell id="e" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`);
  const report = auditDrawio(xml, { strictPorts: true });
  assert.equal(report.status, 'pass');
  assert.equal(report.counts.business_edges, 1);
});

test('audits edge attachment coordinates from the connector style rather than node style', () => {
  const source = node('a', 0, 0);
  const target = node('b', 200, 150);
  const ports = 'edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0.25;entryX=0;entryY=0.75;exitPerimeter=1;entryPerimeter=1;';
  const edge = `<mxCell id="e" style="${ports}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="160" y="12.5"/><mxPoint x="160" y="187.5"/></Array></mxGeometry></mxCell>`;
  const report = auditDrawio(graph(`${source}${target}${edge}`), { strictPorts: true });
  assert.ok(!report.issues.some((item) => item.code === 'diagonal_segment'));
});

test('does not parse an edge-label offset as an explicit route waypoint', () => {
  const source = node('a', 0, 0);
  const target = node('b', 200, 0);
  const edge = `<mxCell id="e" value="提交" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="160" y="25"/></Array><mxPoint x="0" y="-18" as="offset"/></mxGeometry></mxCell>`;
  const report = auditDrawio(graph(`${source}${target}${edge}`), { strictPorts: true });
  assert.equal(report.status, 'pass');
  assert.ok(!report.issues.some((item) => item.code === 'diagonal_segment'));
});

test('candidate preserves a valid corner attachment when completing perimeter flags', () => {
  const source = node('a', 0, 100);
  const target = node('b', 220, 0);
  const cornerStyle = 'edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0;entryX=0.5;entryY=1;';
  const edge = `<mxCell id="e" style="${cornerStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="160" y="100"/><mxPoint x="280" y="100"/></Array></mxGeometry></mxCell>`;
  const result = remediateDrawio(graph(`${source}${target}${edge}`));
  assert.equal(auditDrawio(result.xml, { strictPorts: true }).status, 'pass');
  assert.match(result.xml, /id="e"[^>]*exitX=1;exitY=0;/);
});

test('candidate preserves split bottom ports used by multiple outgoing branches', () => {
  const source = node('a', 100, 0);
  const leftTarget = node('b', 0, 160);
  const rightTarget = node('c', 260, 160);
  const edge = (id, target, x, pointX) => `<mxCell id="${id}" style="edgeStyle=orthogonalEdgeStyle;exitX=${x};exitY=1;entryX=0.5;entryY=0;exitPerimeter=1;entryPerimeter=1;" parent="1" source="a" target="${target}" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="${pointX}" y="100"/><mxPoint x="${target === 'b' ? 60 : 320}" y="100"/></Array></mxGeometry></mxCell>`;
  const xml = graph(`${source}${leftTarget}${rightTarget}${edge('left', 'b', 0.35, 142)}${edge('right', 'c', 0.65, 178)}`);
  const result = remediateDrawio(xml);
  assert.equal(auditDrawio(result.xml, { strictPorts: true }).status, 'pass');
  assert.match(result.xml, /id="left"[^>]*exitX=0.35;exitY=1;/);
  assert.match(result.xml, /id="right"[^>]*exitX=0.65;exitY=1;/);
});

test('fails an unbound business edge with a free endpoint', () => {
  const xml = graph(`${node('a', 0, 0)}${node('b', 200, 0)}<mxCell id="e" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="a" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="200" y="25" as="targetPoint"/></mxGeometry></mxCell>`);
  const report = auditDrawio(xml);
  assert.equal(report.status, 'fail');
  assert.ok(report.issues.some((item) => item.code === 'unbound_edge'));
  assert.ok(report.issues.some((item) => item.code === 'free_endpoint'));
});

test('candidate removes redundant free points from an otherwise bound business edge', () => {
  const xml = graph(`${node('a', 0, 0)}${node('b', 220, 120)}<mxCell id="e" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="120" y="25" as="sourcePoint"/><mxPoint x="220" y="145" as="targetPoint"/></mxGeometry></mxCell>`);
  const before = auditDrawio(xml, { strictPorts: true });
  assert.ok(before.issues.some((item) => item.code === 'free_endpoint'));
  const result = remediateDrawio(xml);
  assert.equal(auditDrawio(result.xml, { strictPorts: true }).status, 'pass');
  assert.doesNotMatch(result.xml, /as="(?:sourcePoint|targetPoint)"/);
});

test('candidate repairs nodes clipped by the wrong swimlane parent', () => {
  const laneStyle = 'swimlane;horizontal=0;startSize=30;';
  const laneA = `<mxCell id="lane-a" value="A" style="${laneStyle}" parent="1" vertex="1"><mxGeometry x="0" y="0" width="500" height="100" as="geometry"/></mxCell>`;
  const laneB = `<mxCell id="lane-b" value="B" style="${laneStyle}" parent="1" vertex="1"><mxGeometry x="0" y="100" width="500" height="100" as="geometry"/></mxCell>`;
  const source = node('a', 60, 20, 'lane-a');
  const clippedTarget = node('b', 260, 120, 'lane-a');
  const edge = `<mxCell id="e" style="${edgeStyle}" parent="lane-a" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const xml = graph(`${laneA}${source}${clippedTarget}${edge}${laneB}`);
  const before = auditDrawio(xml, { strictPorts: true });
  assert.ok(before.issues.some((item) => item.code === 'node_outside_container' && item.cell_id === 'b'));

  const result = remediateDrawio(xml);
  const after = auditDrawio(result.xml, { strictPorts: true });
  assert.equal(after.status, 'pass');
  assert.match(result.xml, /id="b"[^>]*parent="lane-b"[^>]*>[\s\S]*?<mxGeometry x="260" y="20"/);
  assert.match(result.xml, /id="e"[^>]*parent="1"/);
  assert.deepEqual(result.report.container_membership.changed_nodes, [{
    node_id: 'b',
    from_parent: 'lane-a',
    to_parent: 'lane-b',
    absolute_position_preserved: true
  }]);
});

test('candidate expands an aligned swimlane group for a small bottom overflow', () => {
  const laneStyle = 'swimlane;horizontal=0;startSize=30;';
  const laneA = `<mxCell id="lane-a" value="A" style="${laneStyle}" parent="1" vertex="1"><mxGeometry x="0" y="0" width="240" height="100" as="geometry"/></mxCell>`;
  const laneB = `<mxCell id="lane-b" value="B" style="${laneStyle}" parent="1" vertex="1"><mxGeometry x="240" y="0" width="240" height="100" as="geometry"/></mxCell>`;
  const source = node('a', 40, 20, 'lane-a');
  const overflowing = node('b', 40, 70, 'lane-b');
  const edge = `<mxCell id="e" style="edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0.5;entryX=0;entryY=0.5;exitPerimeter=1;entryPerimeter=1;" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="200" y="45"/><mxPoint x="200" y="95"/><mxPoint x="280" y="95"/></Array></mxGeometry></mxCell>`;
  const xml = graph(`${laneA}${source}${laneB}${overflowing}${edge}`);
  assert.ok(auditDrawio(xml, { strictPorts: true }).issues.some((item) => item.code === 'node_outside_container'));

  const result = remediateDrawio(xml);
  assert.equal(auditDrawio(result.xml, { strictPorts: true }).status, 'pass');
  assert.match(result.xml, /id="lane-a"[^>]*>[\s\S]*?<mxGeometry x="0" y="0" width="240" height="124"/);
  assert.match(result.xml, /id="lane-b"[^>]*>[\s\S]*?<mxGeometry x="240" y="0" width="240" height="124"/);
  assert.equal(result.report.container_membership.expanded_containers.length, 2);
});

test('does not treat legend sample lines as business edges', () => {
  const legend = node('legend-box', 0, 100, '1', processStyle, '图例');
  const sample = '<mxCell id="lg-line" style="endArrow=classic;" parent="legend-box" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="10" y="10" as="sourcePoint"/><mxPoint x="80" y="10" as="targetPoint"/></mxGeometry></mxCell>';
  const xml = graph(`${node('a', 0, 0)}${node('b', 200, 0)}<mxCell id="e" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>${legend}${sample}`);
  const report = auditDrawio(xml, { strictPorts: true });
  assert.equal(report.status, 'pass');
  assert.equal(report.counts.business_edges, 1);
  assert.equal(report.counts.decorative_edges, 1);
});

test('recognizes shape=swimlane containers and matrix separator lines', () => {
  const lane = '<mxCell id="lane-a" value="角色" style="shape=swimlane;horizontal=0;" parent="1" vertex="1"><mxGeometry x="0" y="0" width="400" height="100" as="geometry"/></mxCell>';
  const separator = '<mxCell id="col-sep-120" style="strokeColor=#ddd;" parent="1" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="120" y="0" as="sourcePoint"/><mxPoint x="120" y="200" as="targetPoint"/></mxGeometry></mxCell>';
  const xml = graph(`${lane}${node('a', 20, 20, 'lane-a')}${node('b', 200, 20, 'lane-a')}<mxCell id="e" style="${edgeStyle}" parent="lane-a" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>${separator}`);
  const report = auditDrawio(xml, { strictPorts: true });
  assert.equal(report.status, 'pass');
  assert.equal(report.counts.lanes, 1);
  assert.equal(report.counts.logical_nodes, 2);
  assert.equal(report.counts.decorative_edges, 1);
});

test('inventory registers Draw.io, Mermaid blocks and Excalidraw separately', () => {
  const vault = fs.mkdtempSync(path.join(os.tmpdir(), 'diagram-governance-'));
  fs.mkdirSync(path.join(vault, '10-项目', '基线', '01-demo'), { recursive: true });
  fs.writeFileSync(path.join(vault, '10-项目', '基线', '01-demo', '流程.drawio'), graph(`${node('a', 0, 0)}${node('b', 200, 0)}<mxCell id="e" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`));
  fs.writeFileSync(path.join(vault, 'notes.md'), '# demo\n```mermaid\nflowchart LR\nA --> B\n```\n');
  fs.writeFileSync(path.join(vault, '状态.excalidraw.md'), 'excalidraw-plugin: parsed\n');
  fs.mkdirSync(path.join(vault, '.standards', 'diagram-governance', 'candidates'), { recursive: true });
  fs.writeFileSync(path.join(vault, '.standards', 'diagram-governance', 'candidates', '派生图.drawio'), graph(node('derived', 0, 0)));
  const manifest = buildInventory(vault, { strictPorts: true });
  assert.deepEqual(manifest.summary.formats, { drawio: 1, mermaid: 1, excalidraw: 1 });
  assert.equal(manifest.assets.find((asset) => asset.format === 'drawio').tier, 'A-formal');
});

test('candidate completes ports and replaces implicit auto routing with explicit orthogonal waypoints', () => {
  const xml = graph(`${node('a', 0, 0)}${node('b', 220, 120)}<mxCell id="e" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`);
  const result = remediateDrawio(xml);
  const report = auditDrawio(result.xml, { strictPorts: true });
  assert.equal(report.status, 'pass');
  assert.match(result.xml, /exitPerimeter=1/);
  assert.match(result.xml, /<Array as="points">/);
  assert.equal(result.report.source_hash, result.report.source_hash);
  assert.notEqual(result.report.source_hash, result.report.candidate_hash);
  assert.deepEqual(result.report.unexpected_changed_cells, []);
  assert.equal(result.report.node_cells_changed, 0);
});

test('candidate router avoids a blocking node', () => {
  const blocker = node('blocker', 150, 0);
  const xml = graph(`${node('a', 0, 0)}${blocker}${node('b', 360, 0)}<mxCell id="e" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="210" y="25"/></Array></mxGeometry></mxCell>`);
  const before = auditDrawio(xml, { strictPorts: true });
  assert.ok(before.issues.some((item) => item.code === 'edge_crosses_node'));
  const result = remediateDrawio(xml);
  const after = auditDrawio(result.xml, { strictPorts: true });
  assert.ok(!after.issues.some((item) => item.code === 'edge_crosses_node'));
  assert.equal(result.report.changes.find((change) => change.edge_id === 'e').rerouted, true);
});

test('candidate may change the attachment side when the existing port has no safe exit corridor', () => {
  const source = node('source', 100, 170);
  const blocker = node('blocker', 100, 100);
  const target = node('target', 100, 0);
  const verticalPorts = 'edgeStyle=orthogonalEdgeStyle;exitX=0.5;exitY=0;entryX=0.5;entryY=1;exitPerimeter=1;entryPerimeter=1;';
  const xml = graph(`${source}${blocker}${target}<mxCell id="e" style="${verticalPorts}" parent="1" source="source" target="target" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="160" y="125"/></Array></mxGeometry></mxCell>`);
  const before = auditDrawio(xml, { strictPorts: true });
  assert.ok(before.issues.some((item) => item.code === 'edge_crosses_node' || item.code === 'diagonal_segment'));
  const result = remediateDrawio(xml);
  const after = auditDrawio(result.xml, { strictPorts: true });
  assert.ok(!after.issues.some((item) => item.code === 'edge_crosses_node' || item.code === 'diagonal_segment'));
  const change = result.report.changes.find((item) => item.edge_id === 'e');
  assert.equal(change.rerouted, true);
  assert.notEqual(change.source_port, 'top');
});

test('candidate leaves decorative free-endpoint lines unchanged', () => {
  const legend = node('legend-box', 0, 100, '1', processStyle, '图例');
  const sample = '<mxCell id="lg-line" style="endArrow=classic;" parent="legend-box" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="10" y="10" as="sourcePoint"/><mxPoint x="80" y="10" as="targetPoint"/></mxGeometry></mxCell>';
  const xml = graph(`${node('a', 0, 0)}${node('b', 200, 0)}<mxCell id="e" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>${legend}${sample}`);
  const result = remediateDrawio(xml);
  assert.ok(result.xml.includes(sample));
});

test('Feishu theme uses reference colors for entry, decision, success, and exception nodes', () => {
  const decisionStyle = 'rhombus;fillColor=#FAAD14;strokeColor=#D48806;fontColor=#1A1A1A;';
  const successStyle = 'rounded=1;fillColor=#52C41A;strokeColor=#389E0D;fontColor=#ffffff;';
  const exceptionStyle = 'rounded=1;fillColor=#FFF1F0;strokeColor=#FF4D4F;fontColor=#CF1322;';
  const xml = graph(`${node('start', 0, 0, '1', processStyle, '开始')}${node('decision', 180, 0, '1', decisionStyle, '是否通过')}${node('end', 360, 0, '1', successStyle, '结束')}${node('error', 180, 120, '1', exceptionStyle, '处理异常')}`);
  const result = normalizeDrawioTheme(xml);
  assert.equal(result.report.status, 'pass');
  assert.match(result.xml, /id="start"[^>]*fillColor=#E8F4FD[^>]*strokeColor=#4A90D9[^>]*fontColor=#2C5F8A/);
  assert.match(result.xml, /id="decision"[^>]*fillColor=#FFF8E1[^>]*strokeColor=#E6B800[^>]*fontColor=#5D4E00/);
  assert.match(result.xml, /id="end"[^>]*fillColor=#E8F8E8[^>]*strokeColor=#5CB85C[^>]*fontColor=#3D7A3D/);
  assert.match(result.xml, /id="error"[^>]*fillColor=#F2F6F8[^>]*strokeColor=#C64B4B[^>]*fontColor=#A33A3A/);
});

test('Feishu theme keeps ordinary actions white and system/external processing light gray', () => {
  const business = 'rounded=1;fillColor=#ffffff;strokeColor=#861B2F;fontColor=#1A1A1A;';
  const system = 'rounded=1;fillColor=#E3F2FD;strokeColor=#1565C0;fontColor=#1A1A1A;';
  const external = 'rounded=1;fillColor=#7B1FA2;strokeColor=#4A148C;fontColor=#ffffff;';
  const xml = graph(`${node('business', 0, 0, '1', business)}${node('system', 180, 0, '1', system)}${node('external', 360, 0, '1', external)}`);
  const result = normalizeDrawioTheme(xml);
  assert.match(result.xml, /id="business"[^>]*fillColor=#FFFFFF[^>]*strokeColor=#D0D0D0[^>]*fontColor=#333333/);
  assert.match(result.xml, /id="system"[^>]*fillColor=#F5F5F5[^>]*strokeColor=#BDBDBD[^>]*fontColor=#616161/);
  assert.match(result.xml, /id="external"[^>]*fillColor=#F5F5F5[^>]*strokeColor=#BDBDBD[^>]*fontColor=#616161/);
  assert.equal(auditDrawioTheme(result.xml).status, 'pass');
});

test('Feishu theme keeps normal connectors gray and reserves dashed gray for data sync', () => {
  const xml = graph(`${node('a', 0, 0)}${node('b', 200, 0)}${node('c', 400, 0)}<mxCell id="pass" value="审核通过" style="${edgeStyle}strokeColor=#52C41A;" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell><mxCell id="sync" value="数据同步" style="${edgeStyle}strokeColor=#FAAD14;" parent="1" source="b" target="c" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`);
  const result = normalizeDrawioTheme(xml);
  assert.match(result.xml, /id="pass"[^>]*strokeColor=#666666[^>]*fontColor=#1F2329/);
  assert.match(result.xml, /id="sync"[^>]*strokeColor=#999999[^>]*fontColor=#1F2329[^>]*dashed=1/);
});

test('layout gate rejects a tall complex swimlane scroll whose fit-to-view text is unreadable', () => {
  const lane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="240" height="2400" as="geometry"/></mxCell>`;
  const nodes = Array.from({ length: 10 }, (_, index) => node(`n${index}`, 60, 80 + index * 210, index % 2 ? 'lane-b' : 'lane-a')).join('');
  const xml = graph(`${lane('lane-a', 0)}${lane('lane-b', 240)}${nodes}`);
  const report = auditDrawioLayout(xml);
  assert.equal(report.status, 'fail');
  assert.ok(report.issues.some((item) => item.code === 'canvas_too_tall'));
  assert.ok(report.issues.some((item) => item.code === 'fit_font_too_small'));
});

test('swimlane reflow changes vertical role columns into readable horizontal lanes', () => {
  const lane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="240" height="2400" as="geometry"/></mxCell>`;
  const nodes = Array.from({ length: 10 }, (_, index) => node(`n${index}`, 60, 80 + index * 210, index % 2 ? 'lane-b' : 'lane-a')).join('');
  const edges = Array.from({ length: 9 }, (_, index) => `<mxCell id="e${index}" style="${edgeStyle}" parent="1" source="n${index}" target="n${index + 1}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`).join('');
  const xml = graph(`${lane('lane-a', 0)}${lane('lane-b', 240)}${nodes}${edges}`);
  const result = reflowSwimlaneDrawio(xml);
  assert.equal(result.report.before.status, 'fail');
  assert.equal(result.report.after.status, 'pass');
  assert.match(result.xml, /id="lane-a"[^>]*><mxGeometry[^>]*x="40"[^>]*y="135"[^>]*height="280"/);
  assert.match(result.xml, /id="lane-b"[^>]*><mxGeometry[^>]*x="40"[^>]*y="415"[^>]*height="280"/);
  assert.ok(result.report.after.aspect_ratio > 0.8);
  assert.ok(result.report.after.effective_font_size >= 8);
});

test('swimlane reflow uses dynamic rank widths for wide consecutive nodes', () => {
  const lane = '<mxCell id="lane-a" value="角色" style="swimlane=1;" parent="1" vertex="1"><mxGeometry x="0" y="100" width="400" height="900" as="geometry"/></mxCell>';
  const secondLane = '<mxCell id="lane-b" value="系统" style="swimlane=1;" parent="1" vertex="1"><mxGeometry x="400" y="100" width="400" height="900" as="geometry"/></mxCell>';
  const wide = (id, y, width) => `<mxCell id="${id}" value="${id}" style="${processStyle}" parent="lane-a" vertex="1"><mxGeometry x="40" y="${y}" width="${width}" height="60" as="geometry"/></mxCell>`;
  const edge = (id, source, target) => `<mxCell id="${id}" style="${edgeStyle}" parent="1" source="${source}" target="${target}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const result = reflowSwimlaneDrawio(graph(`${lane}${secondLane}${wide('a', 80, 270)}${wide('b', 300, 250)}${wide('c', 520, 230)}${edge('ab', 'a', 'b')}${edge('bc', 'b', 'c')}`));
  const geometry = auditDrawio(result.xml, { strictPorts: true });
  assert.ok(!geometry.issues.some((item) => item.code === 'node_overlap' || item.code === 'node_spacing'));
  assert.deepEqual(result.report.rank_widths, [270, 250, 230]);
});

test('planner marks a readable cyclic business flow as safe for outer return channels', () => {
  const lane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="240" height="600" as="geometry"/></mxCell>`;
  const a = node('a', 60, 100, 'lane-a');
  const b = node('b', 60, 300, 'lane-b');
  const forward = `<mxCell id="e1" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const returnEdge = `<mxCell id="e2" value="重新提交" style="${edgeStyle}" parent="1" source="b" target="a" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const result = classifyLayoutStrategy(graph(`${lane('lane-a', 0)}${lane('lane-b', 240)}${a}${b}${forward}${returnEdge}`));
  assert.equal(result.strategy, 'dedicated-cyclic-flow');
  assert.equal(result.automatic, true);
  assert.equal(result.reason, 'cyclic-business-flow-return-channel-supported');
  assert.deepEqual(result.feedback_edges, ['e2']);
});

test('cyclic reflow preserves nodes and separates multiple feedback channels', () => {
  const lane = '<mxCell id="lane" value="角色" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="40" y="100" width="1100" height="250" as="geometry"/></mxCell>';
  const flowNode = (id, x) => node(id, x, 90, 'lane', `${processStyle}fontSize=12;`);
  const edge = (id, source, target, value = '') => `<mxCell id="${id}" value="${value}" style="${edgeStyle}" parent="1" source="${source}" target="${target}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const xml = graph(`${lane}${flowNode('a', 60)}${flowNode('b', 300)}${flowNode('c', 540)}${flowNode('d', 780)}${edge('e1', 'a', 'b')}${edge('e2', 'b', 'c')}${edge('e3', 'c', 'd')}${edge('e4', 'd', 'b', '否')}${edge('e5', 'c', 'a', '否')}`);
  const originalNode = xml.match(/<mxCell id="a"[\s\S]*?<\/mxCell>/)?.[0];
  const result = reflowCyclicBusinessDrawio(xml);
  assert.deepEqual(result.report.feedback_edges, ['e4', 'e5']);
  assert.equal(result.report.node_cells_changed, 0);
  assert.equal(result.report.channels[1].separation, 36);
  assert.notDeepEqual(result.report.channels[0].ring, result.report.channels[1].ring);
  assert.match(result.xml, /id="e4"[^>]*governedReturn=1/);
  assert.match(result.xml, /id="e5"[^>]*governedReturn=1/);
  assert.match(result.xml, /<Array as="points"><mxPoint/);
  assert.equal(result.xml.match(/<mxCell id="a"[\s\S]*?<\/mxCell>/)?.[0], originalNode);
  const candidate = buildCandidate(xml, { layout: 'auto', theme: true });
  assert.deepEqual(candidate.report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'pass' });
});

test('planner selects ranked lane reflow for a tall cyclic business flow', () => {
  const tallLane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="240" height="2400" as="geometry"/></mxCell>`;
  const tallNodes = Array.from({ length: 10 }, (_, index) => node(`n${index}`, 60, 80 + index * 210, index % 2 ? 'lane-b' : 'lane-a')).join('');
  const tallEdges = Array.from({ length: 9 }, (_, index) => `<mxCell id="e${index}" style="${edgeStyle}" parent="1" source="n${index}" target="n${index + 1}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`).join('');
  const tallReturn = `<mxCell id="return" value="否" style="${edgeStyle}" parent="1" source="n9" target="n0" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const tall = classifyLayoutStrategy(graph(`${tallLane('lane-a', 0)}${tallLane('lane-b', 240)}${tallNodes}${tallEdges}${tallReturn}`));
  assert.equal(tall.strategy, 'dedicated-cyclic-flow');
  assert.equal(tall.automatic, true);
  assert.equal(tall.engine, 'ranked-lane-reflow');
  assert.equal(tall.reason, 'cyclic-business-flow-ranked-reflow-supported');

  const lane = '<mxCell id="lane" value="角色" style="swimlane=1;" parent="1" vertex="1"><mxGeometry x="0" y="100" width="600" height="300" as="geometry"/></mxCell>';
  const overlapping = `${node('a', 100, 100, 'lane')}${node('b', 100, 100, 'lane')}`;
  const cycle = `<mxCell id="ab" style="${edgeStyle}" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell><mxCell id="ba" value="否" style="${edgeStyle}" parent="1" source="b" target="a" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const overlap = classifyLayoutStrategy(graph(`${lane}${overlapping}${cycle}`));
  assert.equal(overlap.strategy, 'dedicated-cyclic-flow');
  assert.equal(overlap.automatic, false);
  assert.equal(overlap.reason, 'cyclic-business-flow-has-node-overlap');
});

test('ranked cyclic reflow wraps long flows, enlarges text, and passes deterministic gates', () => {
  const lane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="260" height="2600" as="geometry"/></mxCell>`;
  const nodes = Array.from({ length: 14 }, (_, index) => node(`n${index}`, 70, 80 + index * 170, index % 2 ? 'lane-b' : 'lane-a')).join('');
  const edges = Array.from({ length: 13 }, (_, index) => `<mxCell id="e${index}" style="${edgeStyle}" parent="1" source="n${index}" target="n${index + 1}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`).join('');
  const returnEdge = `<mxCell id="return" value="重新提交" style="${edgeStyle}" parent="1" source="n13" target="n0" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const xml = graph(`${lane('lane-a', 0)}${lane('lane-b', 260)}${nodes}${edges}${returnEdge}`);
  const result = reflowRankedCyclicBusinessDrawio(xml);
  assert.equal(result.report.tracks, 2);
  assert.equal(result.report.max_columns, 12);
  assert.equal(result.report.font_size, 13);
  assert.equal(result.report.before.status, 'fail');
  assert.equal(result.report.after.status, 'pass');
  assert.match(result.xml, /fontSize=13/);
  assert.match(result.xml, /governedReturn=1/);
  const candidate = buildCandidate(xml, { layout: 'auto', theme: true });
  assert.deepEqual(candidate.report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'pass' });
});

test('ranked cyclic reflow resolves node collisions when lane ownership is recoverable', () => {
  const lane = (id, y) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="0" y="${y}" width="900" height="180" as="geometry"/></mxCell>`;
  const a = node('a', 100, 60, 'lane-a');
  const b = node('b', 100, 60, 'lane-a');
  const c = node('c', 400, 60, 'lane-b');
  const edge = (id, source, target, value = '') => `<mxCell id="${id}" value="${value}" style="${edgeStyle}" parent="1" source="${source}" target="${target}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const xml = graph(`${lane('lane-a', 100)}${lane('lane-b', 280)}${a}${b}${c}${edge('ab', 'a', 'b')}${edge('bc', 'b', 'c')}${edge('ca', 'c', 'a', '重新提交')}`);
  const strategy = classifyLayoutStrategy(xml);
  assert.equal(strategy.automatic, true);
  assert.equal(strategy.engine, 'ranked-lane-reflow');
  const candidate = buildCandidate(xml, { layout: 'auto', theme: true });
  assert.equal(candidate.report.gates.geometry, 'pass');
  assert.ok(!candidate.report.issues.geometry.some((item) => item.code === 'node_overlap'));
});

test('small readable state machines use transition normalization without moving state nodes', () => {
  const title = '<mxCell id="title" value="订单状态机" style="text;html=1;" parent="1" vertex="1"><mxGeometry x="0" y="0" width="120" height="30" as="geometry"/></mxCell>';
  const a = node('draft', 80, 120, '1', `${processStyle}fontSize=13;`, '草稿');
  const b = node('pending', 280, 120, '1', `${processStyle}fontSize=13;`, '审批中');
  const rejected = node('rejected', 280, 240, '1', `${processStyle}fontSize=13;`, '已驳回');
  const forward = '<mxCell id="submit" value="提交" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="draft" target="pending" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>';
  const reject = '<mxCell id="reject" value="驳回" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="pending" target="rejected" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>';
  const retry = '<mxCell id="retry" value="重新提交" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="rejected" target="pending" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="240" y="265"/><mxPoint x="240" y="145"/></Array></mxGeometry></mxCell>';
  const xml = graph(`${title}${a}${b}${rejected}${forward}${reject}${retry}`);
  const originalNodes = ['draft', 'pending', 'rejected'].map((id) => {
    const cell = xml.match(new RegExp(`<mxCell id="${id}"[\\s\\S]*?<\\/mxCell>`))?.[0] || '';
    return {
      value: cell.match(/value="([^"]*)"/)?.[1],
      geometry: cell.match(/<mxGeometry[^>]*\/>/)?.[0]
    };
  });
  const strategy = classifyLayoutStrategy(xml);
  assert.equal(strategy.strategy, 'dedicated-state-machine');
  assert.equal(strategy.automatic, true);
  assert.equal(strategy.engine, 'state-transition-preserve-layout');
  const candidate = buildCandidate(xml, { layout: 'auto', theme: true });
  assert.equal(candidate.report.layout.strategy, 'state-transition-preserve-layout');
  assert.equal(candidate.report.layout.node_cells_changed, 0);
  assert.deepEqual(candidate.report.gates, { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'pass' });
  assert.equal(candidate.report.routing.changes.find((change) => change.edge_id === 'retry').source_port, 'left');
  assert.match(candidate.xml, /id="retry"[\s\S]*?<mxGeometry[^>]*x="-0.35"[^>]*y="-12"/);
  for (const [index, id] of ['draft', 'pending', 'rejected'].entries()) {
    const cell = candidate.xml.match(new RegExp(`<mxCell id="${id}"[\\s\\S]*?<\\/mxCell>`))?.[0] || '';
    assert.equal(cell.match(/value="([^"]*)"/)?.[1], originalNodes[index].value);
    assert.equal(cell.match(/<mxGeometry[^>]*\/>/)?.[0], originalNodes[index].geometry);
  }
});

test('state-machine self loops route outside their state with explicit orthogonal waypoints', () => {
  const title = '<mxCell id="title" value="结算状态机" style="text;html=1;" parent="1" vertex="1"><mxGeometry x="0" y="0" width="120" height="30" as="geometry"/></mxCell>';
  const state = node('claimed', 300, 180, '1', `${processStyle}fontSize=13;`, '已申报');
  const loop = '<mxCell id="retry" value="复核驳回（修改重提）" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="claimed" target="claimed" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="350" y="280"/><mxPoint x="430" y="280"/><mxPoint x="430" y="205"/></Array></mxGeometry></mxCell>';
  const candidate = buildCandidate(graph(`${title}${state}${loop}`), { layout: 'auto', theme: true });
  assert.equal(candidate.report.gates.geometry, 'pass');
  assert.equal(candidate.report.routing.changes.find((change) => change.edge_id === 'retry').self_loop, true);
  assert.match(candidate.xml, /id="retry"[\s\S]*?<Array as="points"><mxPoint/);
});

test('unsafe state machines remain blocked when states overlap', () => {
  const title = '<mxCell id="title" value="异常状态机" style="text;html=1;" parent="1" vertex="1"><mxGeometry x="0" y="0" width="120" height="30" as="geometry"/></mxCell>';
  const edge = '<mxCell id="ab" style="edgeStyle=orthogonalEdgeStyle;" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>';
  const strategy = classifyLayoutStrategy(graph(`${title}${node('a', 100, 100)}${node('b', 100, 100)}${edge}`));
  assert.equal(strategy.strategy, 'dedicated-state-machine');
  assert.equal(strategy.automatic, false);
  assert.equal(strategy.reason, 'state-machine-has-node-overlap');
});

test('pipeline reports semantic review separately from geometric gates', () => {
  const decision = node('decision', 0, 0, '1', 'rhombus;whiteSpace=wrap;html=1;', '是否通过');
  const yes = node('yes', 220, 0);
  const no = node('no', 220, 120);
  const edgeYes = `<mxCell id="yes-edge" value="是" style="${edgeStyle}" parent="1" source="decision" target="yes" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`;
  const edgeNo = `<mxCell id="no-edge" style="${edgeStyle}" parent="1" source="decision" target="no" edge="1"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="160" y="25"/><mxPoint x="160" y="145"/></Array></mxGeometry></mxCell>`;
  const xml = graph(`${decision}${yes}${no}${edgeYes}${edgeNo}`);
  const result = buildCandidate(xml, { theme: true });
  assert.equal(result.report.gates.geometry, 'pass');
  assert.equal(result.report.gates.semantic, 'review-required');
  assert.equal(result.report.issues.semantic[0].cell_id, 'no-edge');
});

test('preview recipe declares the truthful diagrams.net backend', () => {
  assert.match(PREVIEW_RECIPES.quick.description, /viewer\.diagrams\.net/);
  assert.equal(PREVIEW_RECIPES.quick.artifact_role, 'hero');
});

test('native preview evidence rejects browser error pages and requires source labels', () => {
  const xml = graph(`${node('start', 0, 0, '1', processStyle, '开始取送车')}${node('end', 200, 0, '1', processStyle, '完成订单')}`);
  const labels = expectedDiagramLabels(xml);
  const error = validateNativeViewerEvidence({
    href: 'https://app.diagrams.net/', title: '无法访问此网站', body_text: 'ERR_CONNECTION_CLOSED', svg_count: 0, svg_text_count: 0
  }, labels);
  assert.equal(error.status, 'fail');
  assert.ok(error.issues.includes('browser-error-page'));
  const rendered = validateNativeViewerEvidence({
    href: 'https://viewer.diagrams.net/?lightbox=1', title: 'Diagram', body_text: '', svg_count: 1, svg_text_count: 2,
    svg_text: '开始取送车\n完成订单'
  }, labels);
  assert.equal(rendered.status, 'pass');
  assert.deepEqual(rendered.matched_labels, ['开始取送车', '完成订单']);
});

test('batch preview selects only candidates whose deterministic A-layer gates pass', () => {
  const eligible = selectPreviewEligible({ items: [
    { candidate_path: 'a.drawio', gates: { geometry: 'pass', layout: 'pass', theme: 'pass', semantic: 'review-required' } },
    { candidate_path: 'b.drawio', gates: { geometry: 'fail', layout: 'pass', theme: 'pass', semantic: 'pass' } },
    { status: 'blocked' }
  ] });
  assert.equal(eligible.length, 1);
  assert.equal(eligible[0].candidate_path, 'a.drawio');
});

test('source planner chooses automatic horizontal reflow only when the readability gate fails', () => {
  const lane = (id, x) => `<mxCell id="${id}" value="${id}" style="swimlane=1;fontSize=12;" parent="1" vertex="1"><mxGeometry x="${x}" y="100" width="240" height="2400" as="geometry"/></mxCell>`;
  const nodes = Array.from({ length: 10 }, (_, index) => node(`n${index}`, 60, 80 + index * 210, index % 2 ? 'lane-b' : 'lane-a')).join('');
  const edges = Array.from({ length: 9 }, (_, index) => `<mxCell id="e${index}" style="${edgeStyle}" parent="1" source="n${index}" target="n${index + 1}" edge="1"><mxGeometry relative="1" as="geometry"/></mxCell>`).join('');
  const plan = planDrawioSource(graph(`${lane('lane-a', 0)}${lane('lane-b', 240)}${nodes}${edges}`));
  assert.equal(plan.strategy, 'horizontal-swimlane-reflow');
  assert.equal(plan.automatic, true);
  assert.equal(plan.gates.layout, 'fail');
});
