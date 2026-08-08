import fs from 'node:fs';
import path from 'node:path';
import { contentHash, decodeXml, isContainer, isDecorative, parseDrawio } from './drawio.mjs';
import { semanticFromMermaid } from './mermaid.mjs';
import { semanticFromExcalidraw } from './excalidraw.mjs';
import { cosineSimilarity, embedTexts } from './embedding.mjs';

function normalize(value) { return String(value || '').normalize('NFKC').replace(/<[^>]+>/g, ' ').replace(/[\s、，。；：:,.!?！？()（）【】\[\]{}]/g, '').toLowerCase(); }
function sorted(values, selector) { return [...values].sort((a, b) => selector(a).localeCompare(selector(b), 'zh-CN')); }

export function semanticFromDrawio(xml) {
  const { cells } = parseDrawio(xml);
  const byId = new Map(cells.map((cell) => [cell.id, cell]));
  const containers = cells.filter((cell) => isContainer(cell));
  const lanes = containers.map((cell) => ({ id: cell.id, label: decodeXml(cell.value || '').trim() }));
  const nodes = cells.filter((cell) => cell.vertex === '1' && cell.geometry && !isContainer(cell) && !isDecorative(cell, byId) && cell.value).map((cell) => ({
    id: cell.id, label: cell.value,
    type: cell.styleMap?.rhombus === true || cell.styleMap?.shape === 'rhombus' ? 'decision' : /ellipse|terminator/.test(cell.style || '') ? 'terminal' : 'process',
    lane: containers.some((container) => container.id === cell.parent) ? cell.parent : null
  }));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = cells.filter((cell) => cell.edge === '1' && (cell.source || cell.target) && !isDecorative(cell, byId)).map((cell) => ({ id: cell.id, from: cell.source || null, to: cell.target || null, label: cell.value || '', kind: /dashed=1|dashPattern/.test(cell.style || '') ? 'async' : 'flow' })).filter((edge) => !edge.from || nodeIds.has(edge.from)).filter((edge) => !edge.to || nodeIds.has(edge.to));
  return { nodes, edges, lanes, labels: nodes.map((node) => node.label).filter(Boolean) };
}

export function canonicalSemantic(graph) {
  const nodeKey = (node) => `${normalize(node.type)}|${normalize(node.label)}|${normalize(node.lane)}`;
  const byId = new Map((graph.nodes || []).map((node) => [node.id, node]));
  const canonical = {
    nodes: sorted((graph.nodes || []).map((node) => ({ label: normalize(node.label), type: normalize(node.type), lane: normalize(node.lane) })), (node) => JSON.stringify(node)),
    edges: sorted((graph.edges || []).map((edge) => ({ from: nodeKey(byId.get(edge.from) || { label: edge.from }), to: nodeKey(byId.get(edge.to) || { label: edge.to }), label: normalize(edge.label), kind: normalize(edge.kind) })), (edge) => JSON.stringify(edge)),
    lanes: sorted((graph.lanes || []).map((lane) => ({ label: normalize(lane.label) })), (lane) => lane.label)
  };
  return canonical;
}

export function semanticHash(graph) { return contentHash(JSON.stringify(canonicalSemantic(graph))); }
export function semanticText(graph) {
  const labels = (graph.nodes || []).map((node) => node.label).filter(Boolean);
  const transitions = (graph.edges || []).map((edge) => edge.label).filter(Boolean);
  const lanes = (graph.lanes || []).map((lane) => lane.label).filter(Boolean);
  return [...lanes, ...labels, ...transitions].join('；');
}

export function extractSemantic(format, source) {
  if (format === 'drawio') return semanticFromDrawio(source);
  if (format === 'mermaid') return semanticFromMermaid(source);
  if (format === 'excalidraw') return semanticFromExcalidraw(source);
  throw new Error(`unsupported_semantic_format: ${format}`);
}

function declaredProcess(asset, links) {
  for (const process of links?.processes || []) {
    if ((process.assets || []).some((entry) => entry.format === asset.format && (entry.path === asset.path || (entry.path_prefix && asset.path.startsWith(entry.path_prefix))))) return process.id;
  }
  return null;
}

export async function buildLineageManifest(vault, inventory, options = {}) {
  const links = options.links || { processes: [] };
  const entries = [];
  for (const asset of inventory.assets) {
    const locator = asset.path.match(/#L(\d+)$/);
    const relativePath = locator ? asset.path.slice(0, locator.index) : asset.path;
    const sourcePath = path.resolve(vault, relativePath);
    const fileSource = fs.readFileSync(sourcePath, 'utf8');
    let source = fileSource;
    if (asset.format === 'mermaid') {
      const { extractMermaidBlocks } = await import('./mermaid.mjs');
      source = extractMermaidBlocks(fileSource).find((block) => block.line === Number(locator?.[1]))?.source || '';
    }
    const graph = extractSemantic(asset.format, source);
    entries.push({
      asset_id: asset.id, process_id: declaredProcess(asset, links), format: asset.format, path: asset.path,
      authority: asset.logic_authority, modification_policy: asset.modification_policy,
      source_hash: contentHash(source), host_file_hash: contentHash(fileSource),
      semantic_hash: semanticHash(graph), semantic_text: semanticText(graph),
      counts: { nodes: graph.nodes.length, edges: graph.edges.length, lanes: graph.lanes.length },
      graph
    });
  }
  let embedding = { status: 'disabled' };
  if (options.embeddings !== false && entries.length) {
    try {
      const result = await embedTexts(entries.map((entry) => entry.semantic_text || path.basename(entry.path)), options.embeddingOptions);
      entries.forEach((entry, index) => { entry.embedding = result.embeddings[index]; entry.embedding_input_hash = result.input_hashes[index]; });
      embedding = { status: 'available', model: result.model, endpoint: result.endpoint, dimensions: result.dimensions };
    } catch (error) { embedding = { status: 'unavailable', error: error.message }; }
  }
  const manifest = { schema_version: 'diagram-lineage/v1', generated_at: new Date().toISOString(), vault: path.resolve(vault), embedding, entries };
  manifest.drift = checkDrift(manifest, options);
  return manifest;
}

export function checkDrift(manifest, options = {}) {
  const threshold = options.similarityThreshold ?? 0.82;
  const groups = new Map();
  for (const entry of manifest.entries || []) if (entry.process_id) {
    if (!groups.has(entry.process_id)) groups.set(entry.process_id, []);
    groups.get(entry.process_id).push(entry);
  }
  const processes = [];
  for (const [processId, entries] of groups) {
    const comparisons = [];
    for (let left = 0; left < entries.length; left += 1) for (let right = left + 1; right < entries.length; right += 1) {
      const a = entries[left]; const b = entries[right];
      const similarity = cosineSimilarity(a.embedding, b.embedding);
      const state = a.semantic_hash === b.semantic_hash ? 'exact' : similarity != null && similarity >= threshold ? 'semantic-similar' : 'drift';
      comparisons.push({ left: a.asset_id, right: b.asset_id, state, similarity: similarity == null ? null : Math.round(similarity * 10000) / 10000, structural_delta: { nodes: b.counts.nodes - a.counts.nodes, edges: b.counts.edges - a.counts.edges, lanes: b.counts.lanes - a.counts.lanes } });
    }
    const status = comparisons.some((item) => item.state === 'drift') ? 'review-required' : comparisons.length ? 'aligned' : 'single-source';
    processes.push({ process_id: processId, status, assets: entries.map((entry) => entry.asset_id), comparisons });
  }
  return { threshold, declared_processes: processes.length, ungrouped_assets: (manifest.entries || []).filter((entry) => !entry.process_id).length, processes };
}
