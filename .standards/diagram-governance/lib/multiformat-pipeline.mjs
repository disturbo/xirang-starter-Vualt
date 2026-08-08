import fs from 'node:fs';
import path from 'node:path';
import { contentHash } from './drawio.mjs';
import { auditMermaid, buildMermaidCandidate, extractMermaidBlocks } from './mermaid.mjs';
import { auditExcalidraw, buildExcalidrawCandidate } from './excalidraw.mjs';
import { captureFormatPreview } from './format-preview.mjs';

function safeName(value) { return value.normalize('NFKC').replace(/[\\/:*?"<>|#]/g, '-').replace(/\s+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 100) || 'diagram'; }

export function readAssetSource(vault, assetPath, format) {
  const locator = assetPath.match(/#L(\d+)$/);
  const relativePath = locator ? assetPath.slice(0, locator.index) : assetPath;
  const hostPath = path.resolve(vault, relativePath);
  const hostSource = fs.readFileSync(hostPath, 'utf8');
  let source = hostSource;
  if (format === 'mermaid') {
    const blocks = extractMermaidBlocks(hostSource);
    const block = locator ? blocks.find((item) => item.line === Number(locator[1])) : blocks.length === 1 ? blocks[0] : null;
    if (!block) throw new Error(`mermaid_locator_required_or_stale: ${assetPath}`);
    source = block.source;
  }
  return { source, hostSource, hostPath, relativePath, locator: locator?.[0] || null };
}

export async function buildFormatCandidate(format, source) {
  if (format === 'mermaid') {
    const audit = await auditMermaid(source);
    if (audit.status !== 'pass') throw new Error(`mermaid_candidate_blocked: ${audit.issues.map((item) => item.code).join(',')}`);
    const result = buildMermaidCandidate(source);
    const after = await auditMermaid(result.source);
    return { content: result.source, report: { ...result.report, audit_after: after, gates: { syntax: after.status, semantics: result.report.source_statements_preserved ? 'pass' : 'fail', theme: 'pass' } } };
  }
  if (format === 'excalidraw') {
    const result = buildExcalidrawCandidate(source);
    const after = auditExcalidraw(result.markdown);
    return { content: result.markdown, report: { ...result.report, audit_after: after, gates: { structure: after.status, geometry_preservation: result.report.geometry_preserved ? 'pass' : 'fail', bindings_preservation: result.report.bindings_preserved ? 'pass' : 'fail', theme: 'pass' } } };
  }
  throw new Error(`unsupported_candidate_format: ${format}`);
}

export async function writeFormatCandidate(vault, format, assetPath, outputPath, options = {}) {
  const read = readAssetSource(vault, assetPath, format);
  const hostHash = contentHash(read.hostSource); const result = await buildFormatCandidate(format, read.source);
  const reportPath = options.reportPath || `${outputPath}.report.json`;
  if (path.resolve(outputPath) === read.hostPath || path.resolve(reportPath) === read.hostPath) throw new Error('candidate_must_not_overwrite_source');
  if (!options.force && (fs.existsSync(outputPath) || fs.existsSync(reportPath))) throw new Error('candidate_exists_pass_force_to_replace');
  fs.mkdirSync(path.dirname(outputPath), { recursive: true }); fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(outputPath, result.content, 'utf8');
  const sourceHashUnchanged = contentHash(fs.readFileSync(read.hostPath, 'utf8')) === hostHash;
  const report = { schema_version: '0.6.0', format, source_path: assetPath, candidate_path: path.relative(vault, outputPath).split(path.sep).join('/'), source_hash_unchanged: sourceHashUnchanged, ...result.report };
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  if (!sourceHashUnchanged) throw new Error('source_hash_changed_during_candidate_generation');
  return { candidate_path: outputPath, report_path: reportPath, report };
}

export async function generateMultiFormatCandidates(vault, inventory, outDir, options = {}) {
  let assets = inventory.assets.filter((asset) => ['mermaid', 'excalidraw'].includes(asset.format));
  if (options.format) assets = assets.filter((asset) => asset.format === options.format);
  if (options.filter) assets = assets.filter((asset) => asset.path.includes(options.filter));
  if (Number.isFinite(options.limit)) assets = assets.slice(0, options.limit);
  const items = [];
  for (const asset of assets) {
    const extension = asset.format === 'mermaid' ? '.candidate.mmd' : '.candidate.excalidraw.md';
    const output = path.join(outDir, asset.format, `${asset.id}-${safeName(asset.path.replace(/#L\d+$/, ''))}${extension}`);
    try {
      const written = await writeFormatCandidate(vault, asset.format, asset.path, output, { force: options.force });
      items.push({ id: asset.id, format: asset.format, path: asset.path, status: written.report.audit_after.status === 'pass' ? 'pass' : 'review-required', candidate_path: path.relative(vault, output).split(path.sep).join('/'), report_path: path.relative(vault, written.report_path).split(path.sep).join('/'), source_hash_unchanged: written.report.source_hash_unchanged, gates: written.report.gates });
    } catch (error) { items.push({ id: asset.id, format: asset.format, path: asset.path, status: 'error', error: error.message }); }
  }
  return { schema_version: '0.6.0', generated_at: new Date().toISOString(), source_diagrams_modified: false, summary: { eligible: assets.length, passed: items.filter((item) => item.status === 'pass').length, review_required: items.filter((item) => item.status === 'review-required').length, errors: items.filter((item) => item.status === 'error').length, formats: Object.fromEntries(['mermaid', 'excalidraw'].map((format) => [format, items.filter((item) => item.format === format).length])) }, items };
}

export async function previewMultiFormatBatch(vault, batch, outDir, options = {}) {
  let eligible = batch.items.filter((item) => item.candidate_path && item.status !== 'error');
  if (options.format) eligible = eligible.filter((item) => item.format === options.format);
  if (options.filter) eligible = eligible.filter((item) => item.path.includes(options.filter));
  if (Number.isFinite(options.limit)) eligible = eligible.slice(0, options.limit);
  const items = [];
  for (const item of eligible) {
    try {
      const preview = await captureFormatPreview(item.format, path.resolve(vault, item.candidate_path), outDir, options);
      items.push({ ...item, status: 'pass', bundle_dir: preview.bundle_dir, manifest_path: preview.manifest_path });
    } catch (error) { items.push({ ...item, status: 'error', error: error.message }); }
  }
  return { schema_version: '0.6.0', generated_at: new Date().toISOString(), summary: { eligible: eligible.length, passed: items.filter((item) => item.status === 'pass').length, failed: items.filter((item) => item.status === 'error').length }, items };
}
