import LZString from 'lz-string';
import { contentHash } from './drawio.mjs';

const DRAWING_PATTERN = /```(json|compressed-json)[ \t]*\r?\n([\s\S]*?)```/;
const FEISHU = Object.freeze({
  process: { backgroundColor: '#ffffff', strokeColor: '#d0d0d0' },
  decision: { backgroundColor: '#fff8e1', strokeColor: '#e6b800' },
  start: { backgroundColor: '#e8f4fd', strokeColor: '#4a90d9' },
  end: { backgroundColor: '#e8f8e8', strokeColor: '#5cb85c' },
  exception: { backgroundColor: '#f2f6f8', strokeColor: '#c64b4b' },
  line: '#666666', text: '#1f2329'
});

export function parseExcalidrawMarkdown(markdown) {
  const match = String(markdown).match(DRAWING_PATTERN);
  if (!match) throw new Error('excalidraw_drawing_block_missing');
  const encoding = match[1];
  const payload = match[2].trim();
  let json = payload;
  if (encoding === 'compressed-json') {
    json = LZString.decompressFromBase64(payload.replace(/\s+/g, ''));
    if (!json) throw new Error('excalidraw_compressed_json_invalid');
  }
  const scene = JSON.parse(json.replaceAll('&#91;', '['));
  return { scene, encoding, payload, range: { start: match.index, end: match.index + match[0].length } };
}

function issue(code, message, severity = 'error', details = {}) { return { code, message, severity, ...details }; }

export function auditExcalidraw(markdown) {
  const issues = [];
  let parsed;
  try { parsed = parseExcalidrawMarkdown(markdown); }
  catch (error) {
    const item = issue('excalidraw_parse_error', error.message);
    return { status: 'fail', parser: 'obsidian-excalidraw/v2', errors: 1, warnings: 0, issues: [item] };
  }
  const { scene, encoding } = parsed;
  if (scene.type !== 'excalidraw') issues.push(issue('invalid_scene_type', `Expected excalidraw, got ${scene.type}`));
  if (!Array.isArray(scene.elements)) issues.push(issue('elements_missing', 'Scene elements must be an array'));
  const elements = Array.isArray(scene.elements) ? scene.elements : [];
  const ids = new Map();
  for (const element of elements) {
    if (!element?.id) issues.push(issue('element_id_missing', 'Every element requires an id'));
    else if (ids.has(element.id)) issues.push(issue('duplicate_element_id', `Duplicate element id ${element.id}`, 'error', { element_id: element.id }));
    else ids.set(element.id, element);
    for (const key of ['x', 'y', 'width', 'height']) {
      if (!Number.isFinite(element?.[key])) issues.push(issue('invalid_geometry', `${element?.id || 'unknown'}.${key} is not finite`, 'error', { element_id: element?.id, field: key }));
    }
    if (element?.roughness === 0 && !element?.isDeleted) issues.push(issue('hand_drawn_style_missing', `${element.id} uses architectural roughness=0`, 'warning', { element_id: element.id }));
  }
  for (const element of elements) {
    if (element.containerId && !ids.has(element.containerId)) issues.push(issue('missing_container_reference', `${element.id} references missing ${element.containerId}`, 'error', { element_id: element.id }));
    for (const bindingName of ['startBinding', 'endBinding']) {
      const binding = element[bindingName];
      if (binding?.elementId && !ids.has(binding.elementId)) issues.push(issue('missing_binding_reference', `${element.id}.${bindingName} references missing ${binding.elementId}`, 'error', { element_id: element.id }));
    }
    for (const bound of element.boundElements || []) {
      if (!ids.has(bound.id)) issues.push(issue('missing_bound_element', `${element.id} references missing bound element ${bound.id}`, 'error', { element_id: element.id }));
    }
  }
  const errors = issues.filter((item) => item.severity === 'error').length;
  const warnings = issues.length - errors;
  return {
    status: errors ? 'fail' : 'pass', parser: 'obsidian-excalidraw/v2', encoding,
    errors, warnings, issues,
    counts: { elements: elements.length, nodes: elements.filter((item) => !item.isDeleted && ['rectangle', 'ellipse', 'diamond'].includes(item.type)).length, edges: elements.filter((item) => !item.isDeleted && ['arrow', 'line'].includes(item.type)).length, text: elements.filter((item) => !item.isDeleted && item.type === 'text').length }
  };
}

function textForContainer(scene, id) {
  return scene.elements.find((item) => !item.isDeleted && item.type === 'text' && item.containerId === id)?.text || '';
}

function paletteFor(element, scene) {
  if (element.type === 'diamond') return FEISHU.decision;
  const label = textForContainer(scene, element.id);
  if (/(失败|异常|驳回|拒绝|取消|作废|错误)/.test(label)) return FEISHU.exception;
  if (/(待审核|待处理|审核中|处理中|待确认)/.test(label)) return FEISHU.decision;
  if (/^(草稿|开始|发起|入口|创建)/.test(label)) return FEISHU.start;
  if (/(结束|完成|关闭|终止|已生效|成功|通过|竣工)$/.test(label)) return FEISHU.end;
  return FEISHU.process;
}

export function buildExcalidrawCandidate(markdown, options = {}) {
  const before = auditExcalidraw(markdown);
  if (before.errors) throw new Error(`excalidraw_candidate_blocked: ${before.issues.map((item) => item.code).join(',')}`);
  const parsed = parseExcalidrawMarkdown(markdown);
  const scene = structuredClone(parsed.scene);
  scene.elements = scene.elements.map((element) => {
    if (element.isDeleted) return element;
    if (element.type === 'text') return { ...element, strokeColor: FEISHU.text, roughness: 1 };
    if (['arrow', 'line', 'freedraw'].includes(element.type)) return { ...element, strokeColor: FEISHU.line, roughness: 1 };
    if (['rectangle', 'ellipse', 'diamond'].includes(element.type)) return { ...element, ...paletteFor(element, scene), fillStyle: 'solid', roughness: 1 };
    return { ...element, roughness: options.handDrawn === false ? element.roughness : 1 };
  });
  scene.appState = { ...(scene.appState || {}), theme: 'light', viewBackgroundColor: '#ffffff', currentItemRoughness: 1 };
  const json = JSON.stringify(scene, null, 2);
  const candidate = `${markdown.slice(0, parsed.range.start)}\`\`\`json\n${json}\n\`\`\`${markdown.slice(parsed.range.end)}`;
  return {
    markdown: candidate,
    scene,
    report: {
      format: 'excalidraw', source_hash: contentHash(markdown), candidate_hash: contentHash(candidate),
      source_encoding: parsed.encoding, candidate_encoding: 'json',
      element_ids_preserved: scene.elements.map((item) => item.id).join('|') === parsed.scene.elements.map((item) => item.id).join('|'),
      geometry_preserved: scene.elements.every((item, index) => ['x', 'y', 'width', 'height', 'angle'].every((key) => item[key] === parsed.scene.elements[index][key])),
      bindings_preserved: scene.elements.every((item, index) => JSON.stringify([item.containerId, item.startBinding, item.endBinding, item.boundElements]) === JSON.stringify([parsed.scene.elements[index].containerId, parsed.scene.elements[index].startBinding, parsed.scene.elements[index].endBinding, parsed.scene.elements[index].boundElements])),
      changes: ['palette:feishu', 'roughness:hand-drawn', 'encoding:plain-json'],
      audit_before: before,
      modification_policy: 'derived-candidate-only'
    }
  };
}

export function semanticFromExcalidraw(markdownOrScene) {
  const scene = typeof markdownOrScene === 'string' ? parseExcalidrawMarkdown(markdownOrScene).scene : markdownOrScene;
  const active = scene.elements.filter((item) => !item.isDeleted);
  const byId = new Map(active.map((item) => [item.id, item]));
  const texts = new Map(active.filter((item) => item.type === 'text' && item.containerId).map((item) => [item.containerId, item.text || item.originalText || '']));
  const nodes = active.filter((item) => ['rectangle', 'ellipse', 'diamond'].includes(item.type)).map((item) => ({ id: item.id, label: String(texts.get(item.id) || '').trim(), type: item.type === 'diamond' ? 'decision' : item.type === 'ellipse' ? 'terminal' : 'process' }));
  const nodeIds = new Set(nodes.map((item) => item.id));
  const edges = active.filter((item) => ['arrow', 'line'].includes(item.type)).map((item, index) => ({
    id: item.id || `e${index + 1}`,
    from: item.startBinding?.elementId || null,
    to: item.endBinding?.elementId || null,
    label: active.find((text) => text.type === 'text' && text.containerId === item.id)?.text || '',
    kind: item.strokeStyle === 'dashed' || item.strokeStyle === 'dotted' ? 'async' : 'flow'
  })).filter((edge) => !edge.from || nodeIds.has(edge.from)).filter((edge) => !edge.to || nodeIds.has(edge.to));
  return { nodes, edges, lanes: [], labels: nodes.map((node) => node.label).filter(Boolean), element_count: active.length, referenced_ids_valid: [...byId.keys()].length === active.length };
}
