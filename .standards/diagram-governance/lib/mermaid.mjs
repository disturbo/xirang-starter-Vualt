import { contentHash } from './drawio.mjs';

const TYPES = new Set(['flowchart', 'graph', 'stateDiagram', 'stateDiagram-v2', 'sequenceDiagram', 'classDiagram', 'erDiagram', 'journey', 'gantt', 'pie', 'mindmap', 'timeline', 'gitGraph', 'quadrantChart', 'requirementDiagram', 'packet-beta', 'architecture-beta', 'block-beta', 'kanban', 'xychart-beta', 'sankey-beta', 'radar-beta', 'treemap-beta', 'zenuml']);

const FEISHU_INIT = Object.freeze({
  theme: 'base',
  look: 'handDrawn',
  securityLevel: 'strict',
  themeVariables: {
    primaryColor: '#ffffff', primaryBorderColor: '#d0d0d0', primaryTextColor: '#333333',
    secondaryColor: '#fff8e1', secondaryBorderColor: '#e6b800', tertiaryColor: '#e8f8e8',
    lineColor: '#666666', textColor: '#1f2329', mainBkg: '#ffffff', nodeBorder: '#d0d0d0',
    clusterBkg: '#ffffff', clusterBorder: '#e0e0e0', edgeLabelBackground: '#ffffff'
  },
  flowchart: { curve: 'linear', htmlLabels: false }
});

function lineNumberAt(source, offset) {
  return source.slice(0, offset).split('\n').length;
}

export function extractMermaidBlocks(markdown) {
  const blocks = [];
  const pattern = /```mermaid[ \t]*\r?\n([\s\S]*?)```/g;
  for (const match of markdown.matchAll(pattern)) {
    const source = match[1].trim();
    const meaningful = source.split(/\r?\n/).find((line) => line.trim() && !line.trim().startsWith('%%'))?.trim() || '';
    const type = meaningful.split(/\s+/)[0] || 'unknown';
    blocks.push({
      source,
      index: blocks.length,
      line: lineNumberAt(markdown, match.index),
      type,
      hand_drawn: /(?:look\s*['"]?\s*:\s*['"]handDrawn['"]|look\s*:\s*handDrawn)/i.test(source),
      hash: contentHash(source)
    });
  }
  return blocks;
}

function issue(code, message, severity = 'error', details = {}) {
  return { code, message, severity, ...details };
}

export function auditMermaidStatic(source) {
  const issues = [];
  const meaningful = String(source).split(/\r?\n/).find((line) => line.trim() && !line.trim().startsWith('%%'))?.trim() || '';
  const type = meaningful.split(/\s+/)[0] || 'unknown';
  if (!String(source).trim()) issues.push(issue('empty_diagram', 'Mermaid block is empty'));
  if (!TYPES.has(type)) issues.push(issue('unknown_diagram_type', `Unknown Mermaid diagram type: ${type}`));
  if (/^\s*click\s+/im.test(source)) issues.push(issue('unsafe_click_directive', 'Click directives are forbidden in governed diagrams'));
  if (/javascript\s*:/i.test(source)) issues.push(issue('unsafe_javascript_url', 'JavaScript URLs are forbidden in governed diagrams'));
  const errors = issues.filter((item) => item.severity === 'error').length;
  const warnings = issues.length - errors;
  return { status: errors ? 'fail' : 'pass', parser: 'static-preflight', type, errors, warnings, issues };
}

let mermaidPromise;
async function mermaidRuntime() {
  if (!mermaidPromise) mermaidPromise = (async () => {
    if (!globalThis.window || !globalThis.document) {
      const { JSDOM } = await import('jsdom');
      const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
      globalThis.window = dom.window;
      globalThis.document = dom.window.document;
      Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
      globalThis.Element = dom.window.Element;
      globalThis.HTMLElement = dom.window.HTMLElement;
      globalThis.SVGElement = dom.window.SVGElement;
    }
    const runtime = (await import('mermaid')).default;
    runtime.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true });
    return runtime;
  })();
  return mermaidPromise;
}

export async function auditMermaid(source) {
  const report = auditMermaidStatic(source);
  if (report.errors) return report;
  const mermaid = await mermaidRuntime();
  try {
    await mermaid.parse(source, { suppressErrors: false });
    return { ...report, parser: `mermaid@11.16.0`, syntax_verified: true };
  } catch (error) {
    const parseIssue = issue('mermaid_syntax_error', String(error?.message || error).split('\n').slice(0, 4).join(' '));
    return { ...report, status: 'fail', parser: 'mermaid@11.16.0', syntax_verified: false, errors: report.errors + 1, issues: [...report.issues, parseIssue] };
  }
}

export function buildMermaidCandidate(source, options = {}) {
  const before = auditMermaidStatic(source);
  if (before.errors) throw new Error(`mermaid_candidate_blocked: ${before.issues.map((item) => item.code).join(',')}`);
  const body = String(source).replace(/^\s*%%\{init\s*:[\s\S]*?\}%%\s*/i, '').trim();
  let semanticStyles = '';
  if (/^(?:flowchart|graph)\b/im.test(body)) {
    const graph = semanticFromMermaid(body);
    const groups = { dgStart: [], dgDecision: [], dgEnd: [], dgException: [] };
    for (const node of graph.nodes) {
      if (node.type === 'decision') groups.dgDecision.push(node.id);
      else if (/(失败|异常|驳回|拒绝|取消|作废|错误)/.test(node.label)) groups.dgException.push(node.id);
      else if (/(待审核|待处理|审核中|处理中|待确认)/.test(node.label)) groups.dgDecision.push(node.id);
      else if (/^(草稿|开始|发起|入口|创建)/.test(node.label)) groups.dgStart.push(node.id);
      else if (/(结束|完成|关闭|终止|已生效|成功|通过|竣工)$/.test(node.label)) groups.dgEnd.push(node.id);
    }
    const declarations = [
      'classDef dgStart fill:#e8f4fd,stroke:#4a90d9,color:#2c5f8a',
      'classDef dgDecision fill:#fff8e1,stroke:#e6b800,color:#5d4e00',
      'classDef dgEnd fill:#e8f8e8,stroke:#5cb85c,color:#3d7a3d',
      'classDef dgException fill:#f2f6f8,stroke:#c64b4b,color:#a33a3a'
    ];
    for (const [className, ids] of Object.entries(groups)) if (ids.length) declarations.push(`class ${ids.join(',')} ${className}`);
    semanticStyles = `\n${declarations.join('\n')}`;
  }
  const init = `%%{init: ${JSON.stringify({ ...FEISHU_INIT, look: options.handDrawn === false ? 'classic' : 'handDrawn' })}}%%`;
  const candidate = `${init}\n${body}${semanticStyles}\n`;
  return {
    source: candidate,
    report: {
      format: 'mermaid', source_hash: contentHash(source), candidate_hash: contentHash(candidate),
      source_statements_preserved: body === String(source).replace(/^\s*%%\{init\s*:[\s\S]*?\}%%\s*/i, '').trim(),
      changes: ['configuration:feishu-palette', 'semantic-classes:feishu-palette', options.handDrawn === false ? 'look:classic' : 'look:handDrawn'],
      audit_before: before,
      modification_policy: 'derived-candidate-only'
    }
  };
}

function cleanLabel(value = '') {
  return String(value).replace(/^['"]|['"]$/g, '').replace(/<br\s*\/?\s*>/gi, ' ').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
}

export function semanticFromMermaid(source) {
  const nodes = new Map();
  const edges = [];
  const lanes = [];
  const lines = String(source).split(/\r?\n/);
  const addNode = (id, label = id, type = 'process') => {
    if (!id || ['end', 'subgraph'].includes(id)) return;
    if (!nodes.has(id)) nodes.set(id, { id, label: cleanLabel(label) || id, type });
  };
  const idPattern = '[\\p{L}\\p{N}_.*-]+';
  const nodeToken = new RegExp(`^(${idPattern})(?:\\s*(?:\\[([^\\]]+)\\]|\\(([^)]+)\\)|\\{([^}]+)\\}|\\[\\[([^\\]]+)\\]\\]))?`, 'u');
  const parseChain = (line) => {
    let rest = line; const first = rest.match(nodeToken); if (!first) return false;
    let current = first[1]; addNode(current, first[2] || first[3] || first[4] || first[5] || current, first[4] ? 'decision' : 'process'); rest = rest.slice(first[0].length);
    let found = false;
    while (rest.trim()) {
      let label = ''; let kind = 'flow'; let arrow;
      if ((arrow = rest.match(/^\s*--\s+(.+?)\s+-->\s*/))) label = arrow[1];
      else if ((arrow = rest.match(/^\s*-\.\s*(.+?)\s*\.->\s*/))) { label = arrow[1]; kind = 'async'; }
      else if ((arrow = rest.match(/^\s*-->(?:\|([^|]+)\|)?\s*/))) label = arrow[1] || '';
      else if ((arrow = rest.match(/^\s*(---|==>)\s*/))) kind = arrow[1] === '==>' ? 'emphasis' : 'flow';
      else break;
      rest = rest.slice(arrow[0].length); const target = rest.match(nodeToken); if (!target) break;
      const targetId = target[1]; addNode(targetId, target[2] || target[3] || target[4] || target[5] || targetId, target[4] ? 'decision' : 'process');
      edges.push({ id: `e${edges.length + 1}`, from: current, to: targetId, label: cleanLabel(label), kind });
      current = targetId; rest = rest.slice(target[0].length); found = true;
    }
    return found;
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith('%%') || /^(flowchart|graph|stateDiagram|stateDiagram-v2|classDef|class|style|linkStyle|click)\b/.test(line)) continue;
    const subgraph = line.match(/^subgraph\s+(?:([\w.-]+)\s*)?(?:\[([^\]]+)\]|"([^"]+)")?/i);
    if (subgraph) { lanes.push({ id: subgraph[1] || `lane-${lanes.length + 1}`, label: cleanLabel(subgraph[2] || subgraph[3] || subgraph[1] || '') }); continue; }
    const state = line.match(new RegExp(`^state\\s+"([^"]+)"\\s+as\\s+(${idPattern})`, 'iu'));
    if (state) { addNode(state[2], state[1], 'state'); continue; }
    const transition = line.match(new RegExp(`^(${idPattern})\\s*--?>\\s*(${idPattern})(?:\\s*:\\s*(.+))?$`, 'u'));
    if (transition) {
      addNode(transition[1], transition[1], 'state'); addNode(transition[2], transition[2], 'state');
      edges.push({ id: `e${edges.length + 1}`, from: transition[1], to: transition[2], label: cleanLabel(transition[3] || ''), kind: 'transition' });
      continue;
    }
    if (parseChain(line)) continue;
    const node = line.match(new RegExp(`^(${idPattern})\\s*(?:\\[([^\\]]+)\\]|\\(([^)]+)\\)|\\{([^}]+)\\})`, 'u'));
    if (node) addNode(node[1], node[2] || node[3] || node[4], node[4] ? 'decision' : 'process');
  }
  return { nodes: [...nodes.values()], edges, lanes, labels: [...nodes.values()].map((node) => node.label).filter(Boolean) };
}
