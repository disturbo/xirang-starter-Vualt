import fs from 'node:fs';
import path from 'node:path';
import { auditDrawio, contentHash } from './drawio.mjs';
import { auditMermaid, auditMermaidStatic, extractMermaidBlocks } from './mermaid.mjs';
import { auditExcalidraw } from './excalidraw.mjs';

const IGNORED_DIRECTORIES = new Set(['.git', '.obsidian', '.trash', 'node_modules', '__pycache__']);
const IGNORED_RELATIVE_PREFIXES = ['.standards/diagram-governance'];

function walk(root) {
  const result = [];
  const stack = [root];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      if (entry.isDirectory() && IGNORED_DIRECTORIES.has(entry.name)) continue;
      const target = path.join(current, entry.name);
      const relativeTarget = path.relative(root, target).split(path.sep).join('/');
      if (IGNORED_RELATIVE_PREFIXES.some((prefix) => relativeTarget === prefix || relativeTarget.startsWith(`${prefix}/`))) continue;
      if (entry.isDirectory()) stack.push(target);
      else result.push(target);
    }
  }
  return result.sort();
}

function relative(vault, file) {
  return path.relative(vault, file).split(path.sep).join('/');
}

function inferTier(format, diagramType, relPath) {
  if (format === 'drawio' && /^10-项目\/(基线|迭代)\//.test(relPath)) return 'A-formal';
  if (format === 'mermaid' && ['sequenceDiagram', 'erDiagram', 'mindmap'].includes(diagramType)) return 'T-technical';
  if (format === 'mermaid') return 'B-inline';
  if (format === 'excalidraw') return 'C-exploratory';
  return 'unclassified';
}

function inferAuthority(format) {
  if (format === 'drawio') return { logic_authority: 'drawio', modification_policy: 'preserve-visual-layout' };
  if (format === 'mermaid') return { logic_authority: 'embedded-markdown', modification_policy: 'source-owned' };
  return { logic_authority: 'excalidraw', modification_policy: 'manual' };
}

function stableId(format, relPath, suffix = '') {
  return `${format}-${contentHash(`${relPath}${suffix}`).slice(0, 12)}`;
}

function classifyExcalidraw(relPath) {
  if (/状态(流转|机)/.test(relPath)) return 'state-machine';
  if (/架构|关联关系/.test(relPath)) return 'architecture';
  return 'sketch';
}

export function buildInventory(vault, options = {}) {
  const assets = [];
  const files = walk(vault);

  for (const file of files) {
    const relPath = relative(vault, file);
    if (file.endsWith('.drawio')) {
      const source = fs.readFileSync(file, 'utf8');
      const audit = auditDrawio(source, { strictPorts: Boolean(options.strictPorts) });
      assets.push({
        id: stableId('drawio', relPath),
        format: 'drawio',
        path: relPath,
        diagram_type: audit.diagram_type,
        tier: inferTier('drawio', audit.diagram_type, relPath),
        ...inferAuthority('drawio'),
        hash: contentHash(source),
        audit
      });
      continue;
    }
    if (file.endsWith('.excalidraw.md')) {
      const source = fs.readFileSync(file, 'utf8');
      const diagramType = classifyExcalidraw(relPath);
      assets.push({
        id: stableId('excalidraw', relPath),
        format: 'excalidraw',
        path: relPath,
        diagram_type: diagramType,
        tier: inferTier('excalidraw', diagramType, relPath),
        ...inferAuthority('excalidraw'),
        hash: contentHash(source),
        audit: auditExcalidraw(source)
      });
      continue;
    }
    if (file.endsWith('.md')) {
      const source = fs.readFileSync(file, 'utf8');
      for (const block of extractMermaidBlocks(source)) {
        const locator = `#L${block.line}`;
        assets.push({
          id: stableId('mermaid', relPath, locator),
          format: 'mermaid',
          path: `${relPath}${locator}`,
          diagram_type: block.type,
          tier: inferTier('mermaid', block.type, relPath),
          ...inferAuthority('mermaid'),
          hash: contentHash(block.source),
          metadata: { hand_drawn: block.hand_drawn },
          audit: auditMermaidStatic(block.source)
        });
      }
    }
  }

  const formats = assets.reduce((counts, asset) => {
    counts[asset.format] = (counts[asset.format] || 0) + 1;
    return counts;
  }, {});
  const formatSummary = (format) => {
    const selected = assets.filter((asset) => asset.format === format);
    return {
      checked: selected.length,
      passed: selected.filter((asset) => asset.audit.status === 'pass').length,
      failed: selected.filter((asset) => asset.audit.status === 'fail').length,
      unsupported: selected.filter((asset) => asset.audit.status === 'unsupported').length,
      errors: selected.reduce((sum, asset) => sum + (asset.audit.errors || 0), 0),
      warnings: selected.reduce((sum, asset) => sum + (asset.audit.warnings || 0), 0)
    };
  };
  return {
    schema_version: '0.6.0',
    generated_at: new Date().toISOString(),
    vault,
    policy: {
      formal_editable_baseline: 'drawio',
      writes_source_diagrams: false,
      drawio_strict_ports: Boolean(options.strictPorts)
    },
    summary: {
      assets: assets.length,
      formats,
      drawio: formatSummary('drawio'),
      mermaid: formatSummary('mermaid'),
      excalidraw: formatSummary('excalidraw')
    },
    assets
  };
}

export async function buildVerifiedInventory(vault, options = {}) {
  const manifest = buildInventory(vault, options);
  for (const asset of manifest.assets.filter((item) => item.format === 'mermaid')) {
    const match = asset.path.match(/#L(\d+)$/);
    const relativePath = match ? asset.path.slice(0, match.index) : asset.path;
    const markdown = fs.readFileSync(path.resolve(vault, relativePath), 'utf8');
    const block = extractMermaidBlocks(markdown).find((item) => item.line === Number(match?.[1]));
    asset.audit = block ? await auditMermaid(block.source) : { status: 'fail', parser: 'mermaid@11.16.0', errors: 1, warnings: 0, issues: [{ code: 'block_locator_stale', message: `No Mermaid block at ${asset.path}`, severity: 'error' }] };
  }
  for (const format of ['drawio', 'mermaid', 'excalidraw']) {
    const selected = manifest.assets.filter((asset) => asset.format === format);
    manifest.summary[format] = {
      checked: selected.length,
      passed: selected.filter((asset) => asset.audit.status === 'pass').length,
      failed: selected.filter((asset) => asset.audit.status === 'fail').length,
      unsupported: selected.filter((asset) => asset.audit.status === 'unsupported').length,
      errors: selected.reduce((sum, asset) => sum + (asset.audit.errors || 0), 0),
      warnings: selected.reduce((sum, asset) => sum + (asset.audit.warnings || 0), 0)
    };
  }
  manifest.policy.mermaid_parser = 'mermaid@11.16.0';
  manifest.policy.excalidraw_parser = 'obsidian-excalidraw/v2';
  return manifest;
}

export function summarizeIssues(manifest) {
  const counts = { error: {}, warning: {} };
  for (const asset of manifest.assets) {
    for (const item of asset.audit.issues || []) {
      const bucket = item.severity === 'warning' ? counts.warning : counts.error;
      bucket[item.code] = (bucket[item.code] || 0) + 1;
    }
  }
  for (const severity of Object.keys(counts)) {
    counts[severity] = Object.fromEntries(Object.entries(counts[severity]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])));
  }
  return counts;
}
