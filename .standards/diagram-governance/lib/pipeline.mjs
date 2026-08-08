import fs from 'node:fs';
import path from 'node:path';
import { remediateDrawio } from './candidate.mjs';
import { auditDrawio, contentHash } from './drawio.mjs';
import { buildInventory } from './inventory.mjs';
import { auditDrawioLayout, classifyLayoutStrategy, reflowCyclicBusinessDrawio, reflowRankedCyclicBusinessDrawio, reflowSwimlaneDrawio } from './layout.mjs';
import { auditDrawioTheme, normalizeDrawioTheme } from './theme.mjs';

function portable(relativePath) {
  return relativePath.split(path.sep).join('/');
}

function safeName(asset) {
  const base = path.basename(asset.path, '.drawio').replace(/[^\p{L}\p{N}._-]+/gu, '-');
  return `${asset.id}-${base}`;
}

function semanticIssues(audit) {
  return (audit.issues || []).filter((item) => ['decision_branch_without_label', 'decision_missing_branch'].includes(item.code));
}

export function planDrawioSource(xml, metadata = {}) {
  const geometry = auditDrawio(xml, { strictPorts: true });
  const strategy = classifyLayoutStrategy(xml);
  const theme = auditDrawioTheme(xml);
  return {
    id: metadata.id || null,
    path: metadata.path || null,
    source_hash: contentHash(xml),
    diagram_type: geometry.diagram_type,
    counts: geometry.counts,
    strategy: strategy.strategy,
    layout_engine: strategy.engine || null,
    automatic: strategy.automatic,
    reason: strategy.reason,
    gates: {
      geometry: geometry.status,
      layout: strategy.layout?.status || 'unsupported',
      theme: theme.status,
      semantic: semanticIssues(geometry).length ? 'review-required' : 'pass'
    },
    blockers: [
      ...(!strategy.automatic ? [{ type: 'layout-strategy', reason: strategy.reason }] : []),
      ...semanticIssues(geometry).map((item) => ({ type: 'business-semantic', code: item.code, cell_id: item.cell_id }))
    ]
  };
}

export function planVaultDrawio(vault, options = {}) {
  const inventory = buildInventory(vault, { strictPorts: true });
  const formal = inventory.assets.filter((asset) => asset.format === 'drawio' && asset.tier === 'A-formal');
  const items = formal.map((asset) => {
    const xml = fs.readFileSync(path.join(vault, asset.path), 'utf8');
    return planDrawioSource(xml, asset);
  });
  const filtered = options.filter
    ? items.filter((item) => item.path.includes(options.filter) || item.diagram_type.includes(options.filter))
    : items;
  const limited = Number.isFinite(options.limit) ? filtered.slice(0, options.limit) : filtered;
  const strategies = limited.reduce((counts, item) => {
    counts[item.strategy] = (counts[item.strategy] || 0) + 1;
    return counts;
  }, {});
  return {
    schema_version: '0.5.0',
    generated_at: new Date().toISOString(),
    vault,
    writes_source_diagrams: false,
    summary: {
      formal_drawio: limited.length,
      automatic: limited.filter((item) => item.automatic).length,
      manual_strategy_required: limited.filter((item) => !item.automatic).length,
      semantic_review_required: limited.filter((item) => item.gates.semantic !== 'pass').length,
      strategies
    },
    items: limited
  };
}

export function buildCandidate(xml, options = {}) {
  const sourceHash = contentHash(xml);
  const plan = planDrawioSource(xml, options.metadata || {});
  let layout = null;
  let working = xml;
  if ((options.layout === true || options.layout === 'auto') && plan.strategy === 'dedicated-cyclic-flow') {
    if (!plan.automatic) throw new Error(plan.reason);
    layout = plan.layout_engine === 'ranked-lane-reflow'
      ? reflowRankedCyclicBusinessDrawio(working)
      : reflowCyclicBusinessDrawio(working);
    working = layout.xml;
  } else if ((options.layout === true || options.layout === 'auto') && plan.strategy === 'dedicated-state-machine') {
    if (!plan.automatic) throw new Error(plan.reason);
    const stateLayout = auditDrawioLayout(working);
    layout = {
      xml: working,
      report: {
        applied: true,
        strategy: 'state-transition-preserve-layout',
        node_cells_changed: 0,
        before: stateLayout,
        after: stateLayout
      }
    };
  } else if (options.layout === true || (options.layout === 'auto' && plan.strategy === 'horizontal-swimlane-reflow')) {
    layout = reflowSwimlaneDrawio(working);
    working = layout.xml;
  }
  const routed = remediateDrawio(working, { stateMachine: plan.strategy === 'dedicated-state-machine' });
  const themed = options.theme ? normalizeDrawioTheme(routed.xml) : null;
  const candidateXml = themed?.xml || routed.xml;
  const geometry = auditDrawio(candidateXml, { strictPorts: true });
  const layoutAudit = auditDrawioLayout(candidateXml);
  const themeAudit = auditDrawioTheme(candidateXml);
  const semantic = semanticIssues(geometry);
  const nonSemanticGeometry = (geometry.issues || []).filter((item) => !semantic.includes(item));
  return {
    xml: candidateXml,
    report: {
      generated_at: new Date().toISOString(),
      source_hash: sourceHash,
      candidate_hash: contentHash(candidateXml),
      source_modified: false,
      plan,
      layout: layout?.report || { applied: false },
      routing: routed.report,
      theme: themed?.report || { applied: false, ...themeAudit },
      gates: {
        geometry: nonSemanticGeometry.some((item) => item.severity === 'error') ? 'fail' : 'pass',
        layout: layoutAudit.status,
        theme: themeAudit.status,
        semantic: semantic.length ? 'review-required' : 'pass'
      },
      issues: {
        geometry: nonSemanticGeometry,
        semantic
      }
    }
  };
}

export function generateVaultCandidates(vault, outDir, options = {}) {
  const plan = planVaultDrawio(vault, options);
  fs.mkdirSync(outDir, { recursive: true });
  const results = [];
  for (const item of plan.items) {
    if (!item.automatic) {
      results.push({ path: item.path, status: 'blocked', strategy: item.strategy, blockers: item.blockers });
      continue;
    }
    const asset = { id: item.id, path: item.path };
    const stem = safeName(asset);
    const candidatePath = path.join(outDir, `${stem}.candidate.drawio`);
    const reportPath = `${candidatePath}.report.json`;
    try {
      if (!options.force && (fs.existsSync(candidatePath) || fs.existsSync(reportPath))) {
        results.push({ path: item.path, status: 'exists', candidate_path: portable(path.relative(vault, candidatePath)) });
        continue;
      }
      const sourcePath = path.join(vault, item.path);
      const source = fs.readFileSync(sourcePath, 'utf8');
      const sourceHash = contentHash(source);
      const candidate = buildCandidate(source, { layout: 'auto', theme: Boolean(options.theme), metadata: item });
      fs.writeFileSync(candidatePath, candidate.xml, 'utf8');
      const unchanged = contentHash(fs.readFileSync(sourcePath, 'utf8')) === sourceHash;
      const report = {
        source_path: item.path,
        candidate_path: portable(path.relative(vault, candidatePath)),
        source_hash_unchanged: unchanged,
        ...candidate.report
      };
      fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
      results.push({
        path: item.path,
        status: Object.values(report.gates).every((value) => value === 'pass') ? 'pass' : 'review-required',
        strategy: item.strategy,
        gates: report.gates,
        candidate_path: report.candidate_path,
        report_path: portable(path.relative(vault, reportPath)),
        source_hash_unchanged: unchanged
      });
    } catch (error) {
      results.push({ path: item.path, status: 'error', strategy: item.strategy, error: error.message });
    }
  }
  const summary = results.reduce((counts, item) => {
    counts[item.status] = (counts[item.status] || 0) + 1;
    return counts;
  }, {});
  return {
    schema_version: '0.5.0',
    generated_at: new Date().toISOString(),
    vault,
    out_dir: outDir,
    source_diagrams_modified: false,
    summary,
    items: results
  };
}
