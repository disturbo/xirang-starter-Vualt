import crypto from 'node:crypto';

const XML_ENTITIES = new Map([
  ['amp', '&'],
  ['lt', '<'],
  ['gt', '>'],
  ['quot', '"'],
  ['apos', "'"]
]);

export function decodeXml(value = '') {
  return String(value)
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(Number.parseInt(code, 16)))
    .replace(/&([a-z]+);/gi, (match, entity) => XML_ENTITIES.get(entity) ?? match);
}

function attributes(fragment = '') {
  const result = {};
  for (const match of fragment.matchAll(/([\w:.-]+)="([^"]*)"/g)) {
    result[match[1]] = decodeXml(match[2]);
  }
  return result;
}

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function styleMap(style = '') {
  const result = {};
  for (const token of String(style).split(';')) {
    if (!token) continue;
    const splitAt = token.indexOf('=');
    if (splitAt < 0) result[token] = true;
    else result[token.slice(0, splitAt)] = token.slice(splitAt + 1);
  }
  return result;
}

function geometry(body = '') {
  const match = body.match(/<mxGeometry\b([^>]*?)(?:\/>|>)/);
  if (!match) return null;
  const attrs = attributes(match[1]);
  const allPoints = [...body.matchAll(/<mxPoint\b([^>]*?)\/?\s*>/g)].map((item) => attributes(item[1]));
  const waypointArray = [...body.matchAll(/<Array\b([^>]*)>([\s\S]*?)<\/Array>/g)]
    .find((item) => attributes(item[1]).as === 'points');
  const routePoints = waypointArray
    ? [...waypointArray[2].matchAll(/<mxPoint\b([^>]*?)\/?\s*>/g)].map((item) => attributes(item[1]))
    : [];
  return {
    x: numeric(attrs.x),
    y: numeric(attrs.y),
    width: numeric(attrs.width),
    height: numeric(attrs.height),
    relative: attrs.relative === '1',
    points: routePoints.map((point) => ({ x: numeric(point.x), y: numeric(point.y) })),
    sourcePoint: allPoints.find((point) => point.as === 'sourcePoint') || null,
    targetPoint: allPoints.find((point) => point.as === 'targetPoint') || null
  };
}

export function parseDrawio(xml) {
  if (!/<mxGraphModel\b/.test(xml)) {
    throw new Error('unsupported_drawio_encoding: expected an uncompressed mxGraphModel');
  }

  const cells = [];
  const cellPattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  for (const match of xml.matchAll(cellPattern)) {
    const attrs = attributes(match[1]);
    cells.push({
      ...attrs,
      id: attrs.id || '',
      parent: attrs.parent || '',
      value: decodeXml(attrs.value || '').replace(/<[^>]+>/g, '').trim(),
      styleMap: styleMap(attrs.style || ''),
      geometry: geometry(match[2] || '')
    });
  }
  return { cells };
}

export function isContainer(cell) {
  return cell?.styleMap?.swimlane === true
    || cell?.styleMap?.swimlane === '1'
    || cell?.styleMap?.shape === 'swimlane';
}

export function isText(cell) {
  return cell?.styleMap?.text === true || String(cell?.style || '').startsWith('text;');
}

function hasDecorativeIdentity(cell) {
  const identity = `${cell?.id || ''} ${cell?.value || ''}`.toLowerCase();
  return /(^|[-_\s])(legend|title|bg|note|rule|remark|caption|matrix|header)([-_\s]|$)/.test(identity)
    || /^(lg|grid|border|sep|decor)[-_]/.test(cell?.id || '')
    || /^(col|row)-sep[-_]/.test(cell?.id || '')
    || /^(图例|业务规则|功能名称|备注)$/.test(cell?.value || '');
}

function ancestorHas(cell, byId, predicate) {
  const seen = new Set();
  let cursor = cell;
  while (cursor && cursor.parent && !seen.has(cursor.parent)) {
    seen.add(cursor.parent);
    cursor = byId.get(cursor.parent);
    if (cursor && predicate(cursor)) return true;
  }
  return false;
}

export function isDecorative(cell, byId) {
  return hasDecorativeIdentity(cell) || ancestorHas(cell, byId, hasDecorativeIdentity);
}

export function absoluteOrigin(cell, byId, cache = new Map(), stack = new Set()) {
  if (!cell) return { x: 0, y: 0 };
  if (cache.has(cell.id)) return cache.get(cell.id);
  if (stack.has(cell.id)) return { x: 0, y: 0 };
  stack.add(cell.id);
  const parent = byId.get(cell.parent);
  const parentOrigin = parent ? absoluteOrigin(parent, byId, cache, stack) : { x: 0, y: 0 };
  const result = {
    x: parentOrigin.x + numeric(cell.geometry?.x),
    y: parentOrigin.y + numeric(cell.geometry?.y)
  };
  cache.set(cell.id, result);
  stack.delete(cell.id);
  return result;
}

export function bbox(cell, byId, cache = new Map()) {
  const origin = absoluteOrigin(cell, byId, cache);
  return {
    id: cell.id,
    left: origin.x,
    top: origin.y,
    right: origin.x + numeric(cell.geometry?.width),
    bottom: origin.y + numeric(cell.geometry?.height),
    width: numeric(cell.geometry?.width),
    height: numeric(cell.geometry?.height)
  };
}

export function portPoint(cell, prefix, byId, cache = new Map(), portStyle = cell?.styleMap) {
  const box = bbox(cell, byId, cache);
  const x = numeric(portStyle?.[`${prefix}X`], 0.5);
  const y = numeric(portStyle?.[`${prefix}Y`], 0.5);
  return { x: box.left + box.width * x, y: box.top + box.height * y };
}

function issue(code, severity, message, cellId, extra = {}) {
  return { code, severity, message, cell_id: cellId || null, ...extra };
}

function overlaps(a, b, padding = 0) {
  return Math.min(a.right, b.right) - Math.max(a.left, b.left) > -padding
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > -padding;
}

function containsBox(container, child, epsilon = 0.5) {
  return child.left >= container.left - epsilon
    && child.right <= container.right + epsilon
    && child.top >= container.top - epsilon
    && child.bottom <= container.bottom + epsilon;
}

function segmentHitsBox(a, b, box, padding = 0) {
  const left = box.left - padding;
  const right = box.right + padding;
  const top = box.top - padding;
  const bottom = box.bottom + padding;
  const epsilon = 0.5;
  if (Math.abs(a.x - b.x) < epsilon) {
    return a.x > left && a.x < right && Math.max(a.y, b.y) > top && Math.min(a.y, b.y) < bottom;
  }
  if (Math.abs(a.y - b.y) < epsilon) {
    return a.y > top && a.y < bottom && Math.max(a.x, b.x) > left && Math.min(a.x, b.x) < right;
  }
  return false;
}

function segmentOverlap(first, second) {
  const a1 = first.a;
  const a2 = first.b;
  const b1 = second.a;
  const b2 = second.b;
  const vertical = Math.abs(a1.x - a2.x) < 0.5 && Math.abs(b1.x - b2.x) < 0.5 && Math.abs(a1.x - b1.x) < 0.5;
  const horizontal = Math.abs(a1.y - a2.y) < 0.5 && Math.abs(b1.y - b2.y) < 0.5 && Math.abs(a1.y - b1.y) < 0.5;
  if (vertical) {
    return Math.min(Math.max(a1.y, a2.y), Math.max(b1.y, b2.y))
      - Math.max(Math.min(a1.y, a2.y), Math.min(b1.y, b2.y));
  }
  if (horizontal) {
    return Math.min(Math.max(a1.x, a2.x), Math.max(b1.x, b2.x))
      - Math.max(Math.min(a1.x, a2.x), Math.min(b1.x, b2.x));
  }
  return 0;
}

function edgeSegments(edge, byId, originCache) {
  const source = byId.get(edge.source);
  const target = byId.get(edge.target);
  if (!source || !target) return [];
  const edgeParent = byId.get(edge.parent);
  const edgeOrigin = edgeParent ? absoluteOrigin(edgeParent, byId, originCache) : { x: 0, y: 0 };
  const points = [
    portPoint(source, 'exit', byId, originCache, edge.styleMap),
    ...(edge.geometry?.points || []).map((point) => ({ x: point.x + edgeOrigin.x, y: point.y + edgeOrigin.y })),
    portPoint(target, 'entry', byId, originCache, edge.styleMap)
  ];
  return points.slice(0, -1).map((point, index) => ({ edgeId: edge.id, a: point, b: points[index + 1] }));
}

function classifyDiagram(cells, businessEdges, logicalNodes) {
  const laneCount = cells.filter(isContainer).length;
  const hasStateName = cells.some((cell) => /状态机|状态流转/.test(cell.value));
  if (hasStateName) return 'state-machine';
  if (laneCount) return 'swimlane-flow';
  if (logicalNodes.length <= 6 && businessEdges.length <= 7) return 'simple-flow';
  return 'flowchart';
}

export function auditDrawio(xml, options = {}) {
  let graph;
  try {
    graph = parseDrawio(xml);
  } catch (error) {
    return {
      status: 'unsupported',
      diagram_type: 'unknown',
      counts: { cells: 0, logical_nodes: 0, business_edges: 0, decorative_edges: 0, lanes: 0 },
      issues: [issue('unsupported_encoding', 'error', error.message)]
    };
  }

  const cells = graph.cells;
  const byId = new Map();
  const issues = [];
  for (const cell of cells) {
    if (!cell.id) {
      issues.push(issue('missing_cell_id', 'error', 'mxCell 缺少 id'));
      continue;
    }
    if (byId.has(cell.id)) issues.push(issue('duplicate_id', 'error', `重复 mxCell id: ${cell.id}`, cell.id));
    else byId.set(cell.id, cell);
  }

  const allEdges = cells.filter((cell) => cell.edge === '1');
  const decorativeEdges = allEdges.filter((edge) => isDecorative(edge, byId));
  const businessEdges = allEdges.filter((edge) => !isDecorative(edge, byId));
  const logicalNodes = cells.filter((cell) => cell.vertex === '1'
    && !isContainer(cell)
    && !isText(cell)
    && !isDecorative(cell, byId)
    && numeric(cell.geometry?.width) > 0
    && numeric(cell.geometry?.height) > 0);
  const originCache = new Map();
  const nodeBoxes = new Map(logicalNodes.map((node) => [node.id, bbox(node, byId, originCache)]));
  const explicitSegments = [];

  for (const node of logicalNodes) {
    const parent = byId.get(node.parent);
    if (!parent || !isContainer(parent)) continue;
    const nodeBox = nodeBoxes.get(node.id);
    const parentBox = bbox(parent, byId, originCache);
    if (!containsBox(parentBox, nodeBox)) {
      issues.push(issue('node_outside_container', 'error', `节点 ${node.id} 超出所属泳道 ${parent.id}，原生渲染可能裁剪`, node.id, {
        parent_id: parent.id,
        node_box: nodeBox,
        parent_box: parentBox
      }));
    }
  }

  if (logicalNodes.length > 1 && businessEdges.length === 0) {
    issues.push(issue('no_business_edges_recognized', 'error', '存在多个业务节点但未识别到业务连接器'));
  }

  for (const edge of businessEdges) {
    if (!edge.source || !edge.target) {
      issues.push(issue('unbound_edge', 'error', '业务连接器缺少 source 或 target 绑定', edge.id, { source: edge.source || null, target: edge.target || null }));
    }
    if (edge.geometry?.sourcePoint || edge.geometry?.targetPoint) {
      issues.push(issue('free_endpoint', 'error', '业务连接器使用 sourcePoint/targetPoint 自由端点', edge.id));
    }
    if (edge.source && !byId.has(edge.source)) issues.push(issue('missing_source_ref', 'error', `source 引用不存在: ${edge.source}`, edge.id));
    if (edge.target && !byId.has(edge.target)) issues.push(issue('missing_target_ref', 'error', `target 引用不存在: ${edge.target}`, edge.id));
    if (edge.styleMap?.edgeStyle !== 'orthogonalEdgeStyle') {
      issues.push(issue('non_orthogonal_style', 'error', '业务连接器未使用 orthogonalEdgeStyle', edge.id));
    }

    const requiredPorts = ['exitX', 'exitY', 'entryX', 'entryY', 'exitPerimeter', 'entryPerimeter'];
    const missingPorts = requiredPorts.filter((key) => edge.styleMap?.[key] === undefined);
    if (missingPorts.length) {
      issues.push(issue('incomplete_port_binding', options.strictPorts ? 'error' : 'warning', `端口声明不完整: ${missingPorts.join(',')}`, edge.id, { missing: missingPorts }));
    }

    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (source && target && source.parent !== target.parent && edge.parent !== '1') {
      issues.push(issue('cross_lane_parent', 'error', '跨泳道连接器 parent 必须为 1', edge.id, { source_parent: source.parent, target_parent: target.parent, edge_parent: edge.parent }));
    }

    const segments = edgeSegments(edge, byId, originCache);
    if (!edge.geometry?.points?.length && segments.length === 1) {
      const [segment] = segments;
      const horizontal = Math.abs(segment.a.y - segment.b.y) < 0.5;
      const vertical = Math.abs(segment.a.x - segment.b.x) < 0.5;
      if (!horizontal && !vertical) {
        issues.push(issue('implicit_auto_route', 'error', '非水平/垂直直连缺少显式 waypoints，渲染依赖自动补线', edge.id));
      }
    }
    if (edge.geometry?.points?.length) {
      for (let index = 0; index < segments.length; index += 1) {
        const segment = segments[index];
        const horizontal = Math.abs(segment.a.y - segment.b.y) < 0.5;
        const vertical = Math.abs(segment.a.x - segment.b.x) < 0.5;
        if (!horizontal && !vertical) issues.push(issue('diagonal_segment', 'error', `显式路径第 ${index + 1} 段不是正交线`, edge.id));
        explicitSegments.push(segment);
      }
      for (const node of logicalNodes) {
        if (node.id === edge.source || node.id === edge.target) continue;
        if (segments.some((segment) => segmentHitsBox(segment.a, segment.b, nodeBoxes.get(node.id), 0))) {
          issues.push(issue('edge_crosses_node', 'error', `连接器穿越节点 ${node.id}`, edge.id, { node_id: node.id }));
        }
      }
    }
  }

  const outgoing = new Map();
  for (const edge of businessEdges) {
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
    outgoing.get(edge.source).push(edge);
  }
  for (const node of logicalNodes.filter((cell) => cell.styleMap?.rhombus === true || cell.styleMap?.rhombus === '1')) {
    const edges = outgoing.get(node.id) || [];
    if (edges.length < 2) issues.push(issue('decision_missing_branch', 'error', '判断节点少于两个出口', node.id, { outgoing: edges.length }));
    for (const edge of edges.filter((item) => !item.value)) {
      issues.push(issue('decision_branch_without_label', 'error', '判断出口缺少条件标签', edge.id, { decision_id: node.id }));
    }
  }

  for (let index = 0; index < logicalNodes.length; index += 1) {
    for (let next = index + 1; next < logicalNodes.length; next += 1) {
      const first = logicalNodes[index];
      const second = logicalNodes[next];
      if (overlaps(nodeBoxes.get(first.id), nodeBoxes.get(second.id), 0)) {
        issues.push(issue('node_overlap', 'error', `节点 ${first.id} 与 ${second.id} 重叠`, first.id, { other_id: second.id }));
      } else if (first.parent === second.parent && overlaps(nodeBoxes.get(first.id), nodeBoxes.get(second.id), 20)) {
        issues.push(issue('node_spacing', 'warning', `同泳道节点 ${first.id} 与 ${second.id} 间距小于 20px`, first.id, { other_id: second.id }));
      }
    }
  }

  for (let index = 0; index < explicitSegments.length; index += 1) {
    for (let next = index + 1; next < explicitSegments.length; next += 1) {
      const first = explicitSegments[index];
      const second = explicitSegments[next];
      if (first.edgeId === second.edgeId) continue;
      const overlap = segmentOverlap(first, second);
      if (overlap > 20) {
        issues.push(issue('edge_overlap', 'error', `连接器 ${first.edgeId} 与 ${second.edgeId} 共线重叠 ${Math.round(overlap)}px`, first.edgeId, { other_edge_id: second.edgeId, overlap: Math.round(overlap) }));
      }
    }
  }

  const counts = {
    cells: cells.length,
    logical_nodes: logicalNodes.length,
    business_edges: businessEdges.length,
    decorative_edges: decorativeEdges.length,
    lanes: cells.filter(isContainer).length,
    decisions: logicalNodes.filter((cell) => cell.styleMap?.rhombus === true || cell.styleMap?.rhombus === '1').length,
    explicit_waypoint_edges: businessEdges.filter((edge) => edge.geometry?.points?.length).length
  };
  const errors = issues.filter((item) => item.severity === 'error').length;
  const warnings = issues.filter((item) => item.severity === 'warning').length;
  return {
    status: errors ? 'fail' : 'pass',
    diagram_type: classifyDiagram(cells, businessEdges, logicalNodes),
    counts,
    errors,
    warnings,
    issues
  };
}

export function contentHash(content) {
  return crypto.createHash('sha256').update(content).digest('hex');
}
