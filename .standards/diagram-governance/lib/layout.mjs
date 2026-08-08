import { auditDrawio, bbox, contentHash, isContainer, isDecorative, isText, parseDrawio } from './drawio.mjs';

const VIEWPORT = Object.freeze({ width: 1920, height: 1080, padding: 80 });
const LAYOUT = Object.freeze({
  left: 40,
  top: 135,
  laneHeight: 280,
  laneHeader: 40,
  rankGap: 180,
  firstRankX: 80,
  singleNodeY: 100,
  stackedNodeY: [58, 142],
  minFontSize: 17
});

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function logicalContext(xml) {
  const graph = parseDrawio(xml);
  const byId = new Map(graph.cells.filter((cell) => cell.id).map((cell) => [cell.id, cell]));
  const nodes = graph.cells.filter((cell) => cell.vertex === '1'
    && !isContainer(cell)
    && !isText(cell)
    && !isDecorative(cell, byId)
    && numeric(cell.geometry?.width) > 0
    && numeric(cell.geometry?.height) > 0);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = graph.cells.filter((cell) => cell.edge === '1'
    && !isDecorative(cell, byId)
    && nodeIds.has(cell.source)
    && nodeIds.has(cell.target));
  const lanes = graph.cells.filter(isContainer).sort((left, right) => {
    const a = bbox(left, byId);
    const b = bbox(right, byId);
    return a.left - b.left || a.top - b.top;
  });
  return { graph, byId, nodes, edges, lanes };
}

function diagramBounds(context) {
  const boxes = context.lanes.length
    ? context.lanes.map((lane) => bbox(lane, context.byId))
    : context.nodes.map((node) => bbox(node, context.byId));
  if (!boxes.length) return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
  const left = Math.min(...boxes.map((box) => box.left));
  const top = Math.min(...boxes.map((box) => box.top));
  const right = Math.max(...boxes.map((box) => box.right));
  const bottom = Math.max(...boxes.map((box) => box.bottom));
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

export function auditDrawioLayout(xml, options = {}) {
  const context = logicalContext(xml);
  const bounds = diagramBounds(context);
  const viewport = { ...VIEWPORT, ...(options.viewport || {}) };
  const scale = Math.min(
    1,
    viewport.width / Math.max(1, bounds.width + viewport.padding),
    viewport.height / Math.max(1, bounds.height + viewport.padding)
  );
  const fontSizes = context.nodes.map((node) => numeric(node.styleMap?.fontSize, 12));
  const minimumFontSize = fontSizes.length ? Math.min(...fontSizes) : 0;
  const effectiveFontSize = minimumFontSize * scale;
  const aspectRatio = bounds.height ? bounds.width / bounds.height : 0;
  const issues = [];
  if (context.nodes.length >= 10 && aspectRatio < 0.8) {
    issues.push({ code: 'canvas_too_tall', severity: 'error', message: `复杂流程画布过高，宽高比仅 ${aspectRatio.toFixed(2)}` });
  }
  if (context.nodes.length >= 10 && aspectRatio > 4.5) {
    issues.push({ code: 'canvas_too_wide', severity: 'error', message: `复杂流程画布过宽，宽高比达到 ${aspectRatio.toFixed(2)}` });
  }
  if (context.nodes.length && effectiveFontSize < 8) {
    issues.push({ code: 'fit_font_too_small', severity: 'error', message: `整图适配 1920×1080 后最小字体仅 ${effectiveFontSize.toFixed(1)}px` });
  }
  const errors = issues.filter((item) => item.severity === 'error').length;
  return {
    status: errors ? 'fail' : 'pass',
    errors,
    issues,
    bounds,
    aspect_ratio: Math.round(aspectRatio * 100) / 100,
    fit_scale: Math.round(scale * 1000) / 1000,
    minimum_font_size: minimumFontSize,
    effective_font_size: Math.round(effectiveFontSize * 10) / 10,
    logical_nodes: context.nodes.length,
    lanes: context.lanes.length
  };
}

function topologicalRanks(nodes, edges) {
  const ids = new Set(nodes.map((node) => node.id));
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
    indegree.set(edge.target, indegree.get(edge.target) + 1);
    outgoing.get(edge.source).push(edge.target);
  }
  const queue = nodes.filter((node) => indegree.get(node.id) === 0).map((node) => node.id);
  const ranks = new Map(queue.map((id) => [id, 0]));
  let visited = 0;
  while (queue.length) {
    const id = queue.shift();
    visited += 1;
    for (const target of outgoing.get(id)) {
      ranks.set(target, Math.max(ranks.get(target) || 0, (ranks.get(id) || 0) + 1));
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    }
  }
  if (visited !== nodes.length) throw new Error('layout_requires_acyclic_flow: detected a cycle; use an explicit state-machine layout');
  return ranks;
}

function stronglyConnectedComponents(nodes, edges) {
  const adjacency = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) adjacency.get(edge.source)?.push(edge.target);
  let index = 0;
  const stack = [];
  const onStack = new Set();
  const indexes = new Map();
  const lowLinks = new Map();
  const components = [];
  const visit = (id) => {
    indexes.set(id, index);
    lowLinks.set(id, index);
    index += 1;
    stack.push(id);
    onStack.add(id);
    for (const target of adjacency.get(id) || []) {
      if (!indexes.has(target)) {
        visit(target);
        lowLinks.set(id, Math.min(lowLinks.get(id), lowLinks.get(target)));
      } else if (onStack.has(target)) lowLinks.set(id, Math.min(lowLinks.get(id), indexes.get(target)));
    }
    if (lowLinks.get(id) !== indexes.get(id)) return;
    const component = [];
    let cursor;
    do {
      cursor = stack.pop();
      onStack.delete(cursor);
      component.push(cursor);
    } while (cursor !== id);
    components.push(component);
  };
  for (const node of nodes) if (!indexes.has(node.id)) visit(node.id);
  return components;
}

function cyclicFeedbackEdges(context) {
  const components = stronglyConnectedComponents(context.nodes, context.edges);
  const feedback = [];
  const feedbackScore = (edge) => {
    const label = String(edge.value || '').replace(/<[^>]+>/g, '');
    let score = label ? 100 : 0;
    if (/重新|重提|重试|复检|回到|修正|补传|回流/.test(label)) score += 20000;
    else if (/退回|驳回|返修/.test(label)) score += 10000;
    else if (/否|不通过|失败|异常|不完整|拒绝/.test(label)) score += 5000;
    if (edge.source === edge.target) score += 1000;
    return score;
  };
  const findCycle = (nodeIds, edges) => {
    const outgoing = new Map([...nodeIds].map((id) => [id, []]));
    for (const edge of edges) outgoing.get(edge.source)?.push(edge);
    const state = new Map();
    const nodeStack = [];
    const edgeStack = [];
    const visit = (id) => {
      state.set(id, 'active');
      nodeStack.push(id);
      for (const edge of outgoing.get(id) || []) {
        if (state.get(edge.target) === 'active') {
          const start = nodeStack.lastIndexOf(edge.target);
          return [...edgeStack.slice(start), edge];
        }
        if (!state.has(edge.target)) {
          edgeStack.push(edge);
          const found = visit(edge.target);
          edgeStack.pop();
          if (found) return found;
        }
      }
      nodeStack.pop();
      state.set(id, 'done');
      return null;
    };
    for (const node of context.nodes) {
      if (!nodeIds.has(node.id) || state.has(node.id)) continue;
      const found = visit(node.id);
      if (found) return found;
    }
    return null;
  };
  for (const component of components) {
    const ids = new Set(component);
    const internal = context.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
    const cyclic = component.length > 1 || internal.some((edge) => edge.source === edge.target);
    if (!cyclic) continue;
    let remaining = internal.slice();
    let cycle;
    while ((cycle = findCycle(ids, remaining))) {
      const selected = cycle.slice().sort((left, right) => feedbackScore(right) - feedbackScore(left)
        || left.id.localeCompare(right.id))[0];
      feedback.push(selected);
      remaining = remaining.filter((edge) => edge.id !== selected.id);
    }
  }
  return [...new Map(feedback.map((edge) => [edge.id, edge])).values()];
}

function cyclicRankedPlan(context, feedbackEdges) {
  if (context.lanes.length < 2 || context.nodes.length > 40 || !feedbackEdges.length) return null;
  const laneBoxes = context.lanes.map((lane) => ({ lane, box: bbox(lane, context.byId) }));
  const laneIds = new Set(context.lanes.map((lane) => lane.id));
  const laneByNode = new Map();
  for (const node of context.nodes) {
    if (laneIds.has(node.parent)) {
      laneByNode.set(node.id, node.parent);
      continue;
    }
    const box = bbox(node, context.byId);
    const center = { x: (box.left + box.right) / 2, y: (box.top + box.bottom) / 2 };
    const containing = laneBoxes.filter(({ box: laneBox }) => center.x >= laneBox.left && center.x <= laneBox.right
      && center.y >= laneBox.top && center.y <= laneBox.bottom)
      .sort((left, right) => left.box.width * left.box.height - right.box.width * right.box.height);
    if (!containing.length) return null;
    laneByNode.set(node.id, containing[0].lane.id);
  }
  const feedbackIds = new Set(feedbackEdges.map((edge) => edge.id));
  let ranks;
  try {
    ranks = topologicalRanks(context.nodes, context.edges.filter((edge) => !feedbackIds.has(edge.id)));
  } catch {
    return null;
  }
  if (ranks.size !== context.nodes.length) return null;
  return { laneByNode, ranks, feedbackEdges };
}

export function classifyLayoutStrategy(xml) {
  try {
    const context = logicalContext(xml);
    const layout = auditDrawioLayout(xml);
    let cyclic = false;
    try {
      topologicalRanks(context.nodes, context.edges);
    } catch (error) {
      if (!String(error.message).startsWith('layout_requires_acyclic_flow')) throw error;
      cyclic = true;
    }
    const stateNamed = context.graph.cells.some((cell) => /状态机|状态流转/.test(cell.value || ''));
    const directLaneOwnership = context.nodes.every((node) => context.lanes.some((lane) => lane.id === node.parent));
    if (cyclic || stateNamed) {
      const stateMachine = stateNamed || (context.lanes.length === 0 && context.nodes.length <= 12);
      const geometry = auditDrawio(xml, { strictPorts: false });
      const nodeOverlap = geometry.issues.some((item) => item.code === 'node_overlap');
      const unboundTransition = geometry.issues.some((item) => ['unbound_edge', 'free_endpoint'].includes(item.code));
      const stateMachineSupported = stateMachine
        && layout.status === 'pass'
        && !nodeOverlap
        && !unboundTransition
        && context.nodes.length <= 12
        && context.edges.length <= 24;
      const feedbackEdges = stateMachine ? [] : cyclicFeedbackEdges(context);
      const outerChannelSupported = !stateMachine && layout.status === 'pass' && !nodeOverlap && feedbackEdges.length > 0;
      const rankedReflow = stateMachine || outerChannelSupported ? null : cyclicRankedPlan(context, feedbackEdges);
      const rankedReflowSupported = Boolean(rankedReflow);
      const automaticCyclic = outerChannelSupported || rankedReflowSupported;
      return {
        strategy: stateMachine ? 'dedicated-state-machine' : 'dedicated-cyclic-flow',
        automatic: stateMachine ? stateMachineSupported : automaticCyclic,
        engine: stateMachine ? stateMachineSupported ? 'state-transition-preserve-layout' : null : outerChannelSupported ? 'outer-return-channels' : rankedReflowSupported ? 'ranked-lane-reflow' : null,
        reason: stateMachine
          ? stateMachineSupported
            ? 'state-machine-transition-normalization-supported'
            : layout.status !== 'pass'
              ? 'state-machine-layout-gate-failed'
              : nodeOverlap
                ? 'state-machine-has-node-overlap'
                : unboundTransition
                  ? 'state-machine-has-unbound-transition'
                  : context.nodes.length > 12 || context.edges.length > 24
                    ? 'state-machine-exceeds-safe-size'
                    : 'state-machine-requires-dedicated-layout'
          : outerChannelSupported
            ? 'cyclic-business-flow-return-channel-supported'
            : rankedReflowSupported
              ? 'cyclic-business-flow-ranked-reflow-supported'
            : layout.status !== 'pass'
              ? 'cyclic-business-flow-requires-node-reflow'
              : nodeOverlap
                ? 'cyclic-business-flow-has-node-overlap'
                : 'cyclic-business-flow-feedback-edge-unresolved',
        cyclic,
        feedback_edges: feedbackEdges.map((edge) => edge.id),
        layout
      };
    }
    if (layout.status === 'fail' && context.lanes.length >= 2 && directLaneOwnership) {
      return {
        strategy: 'horizontal-swimlane-reflow',
        automatic: true,
        reason: 'layout-readability-gate-failed',
        cyclic: false,
        layout
      };
    }
    return {
      strategy: 'preserve-layout',
      automatic: true,
      reason: layout.status === 'pass' ? 'layout-gate-already-passes' : 'no-safe-generic-reflow-strategy',
      cyclic: false,
      layout
    };
  } catch (error) {
    return {
      strategy: 'unsupported',
      automatic: false,
      reason: error.message,
      cyclic: null,
      layout: null
    };
  }
}

function parseStyle(style = '') {
  const order = [];
  const values = new Map();
  for (const token of String(style).split(';')) {
    if (!token) continue;
    const splitAt = token.indexOf('=');
    const key = splitAt < 0 ? token : token.slice(0, splitAt);
    const value = splitAt < 0 ? null : token.slice(splitAt + 1);
    if (!values.has(key)) order.push(key);
    values.set(key, value);
  }
  return { order, values };
}

function updateStyle(style, additions = {}, removals = []) {
  const parsed = parseStyle(style);
  for (const key of removals) {
    parsed.values.delete(key);
    const index = parsed.order.indexOf(key);
    if (index >= 0) parsed.order.splice(index, 1);
  }
  for (const [key, value] of Object.entries(additions)) {
    if (!parsed.values.has(key)) parsed.order.push(key);
    parsed.values.set(key, String(value));
  }
  return `${parsed.order.map((key) => parsed.values.get(key) === null ? key : `${key}=${parsed.values.get(key)}`).join(';')};`;
}

function escapeAttribute(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function replaceAttribute(tag, name, value) {
  const pattern = new RegExp(`\\s${name}="[^"]*"`);
  if (pattern.test(tag)) return tag.replace(pattern, ` ${name}="${escapeAttribute(value)}"`);
  return tag.replace(/\s*\/?\s*>$/, (ending) => ` ${name}="${escapeAttribute(value)}"${ending}`);
}

function updateCellXml(cellXml, changes) {
  const opening = cellXml.match(/^<mxCell\b[^>]*>/)?.[0];
  if (!opening) return cellXml;
  let updatedOpening = opening;
  if (changes.style !== undefined) updatedOpening = replaceAttribute(updatedOpening, 'style', changes.style);
  for (const [name, value] of Object.entries(changes.attributes || {})) updatedOpening = replaceAttribute(updatedOpening, name, value);
  let result = updatedOpening + cellXml.slice(opening.length);
  if (changes.geometry) {
    result = result.replace(/<mxGeometry\b([^>]*?)(?:\/>|>)/, (geometryTag, fragment) => {
      let tag = `<mxGeometry${fragment}>`;
      for (const [key, value] of Object.entries(changes.geometry)) tag = replaceAttribute(tag, key, value);
      return geometryTag.endsWith('/>') ? tag.replace(/>$/, '/>') : tag;
    });
  }
  if (changes.clearWaypoints) result = result.replace(/<Array\b[^>]*as="points"[^>]*>[\s\S]*?<\/Array>/g, '');
  if (changes.waypoints) {
    const points = changes.waypoints.map((point) => `<mxPoint x="${Math.round(point.x * 1000) / 1000}" y="${Math.round(point.y * 1000) / 1000}"/>`).join('');
    const array = `<Array as="points">${points}</Array>`;
    if (/<Array\b[^>]*as="points"[^>]*>[\s\S]*?<\/Array>/.test(result)) {
      result = result.replace(/<Array\b[^>]*as="points"[^>]*>[\s\S]*?<\/Array>/, array);
    } else if (/<mxGeometry\b([^>]*?)\/>/.test(result)) {
      result = result.replace(/<mxGeometry\b([^>]*?)\/>/, `<mxGeometry$1>${array}</mxGeometry>`);
    } else result = result.replace(/(<mxGeometry\b[^>]*>)/, `$1${array}`);
  }
  return result;
}

function cellsById(xml) {
  const result = new Map();
  const pattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  for (const match of xml.matchAll(pattern)) {
    const id = match[1].match(/\bid="([^"]*)"/)?.[1];
    if (id) result.set(id, match[0]);
  }
  return result;
}

function replaceCells(xml, replacements) {
  const pattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  return xml.replace(pattern, (cellXml, fragment) => {
    const id = fragment.match(/\bid="([^"]*)"/)?.[1];
    return replacements.get(id) || cellXml;
  });
}

const SIDE_PORT = Object.freeze({
  top: { x: 0.5, y: 0 },
  bottom: { x: 0.5, y: 1 },
  left: { x: 0, y: 0.5 },
  right: { x: 1, y: 0.5 }
});

function sidePoint(box, side) {
  const port = SIDE_PORT[side];
  return { x: box.left + box.width * port.x, y: box.top + box.height * port.y };
}

function clearSegment(a, b, boxes, padding = 12) {
  const vertical = Math.abs(a.x - b.x) < 0.1;
  const horizontal = Math.abs(a.y - b.y) < 0.1;
  if (!vertical && !horizontal) return false;
  return boxes.every((box) => {
    const padded = { left: box.left - padding, right: box.right + padding, top: box.top - padding, bottom: box.bottom + padding };
    if (vertical) return !(a.x > padded.left && a.x < padded.right
      && Math.max(a.y, b.y) > padded.top && Math.min(a.y, b.y) < padded.bottom);
    return !(a.y > padded.top && a.y < padded.bottom
      && Math.max(a.x, b.x) > padded.left && Math.min(a.x, b.x) < padded.right);
  });
}

function compressOrthogonal(points) {
  const result = [];
  for (const point of points) {
    const previous = result.at(-1);
    if (previous && Math.abs(previous.x - point.x) < 0.1 && Math.abs(previous.y - point.y) < 0.1) continue;
    result.push(point);
    while (result.length >= 3) {
      const [a, b, c] = result.slice(-3);
      const vertical = Math.abs(a.x - b.x) < 0.1 && Math.abs(b.x - c.x) < 0.1;
      const horizontal = Math.abs(a.y - b.y) < 0.1 && Math.abs(b.y - c.y) < 0.1;
      const between = vertical
        ? b.y >= Math.min(a.y, c.y) - 0.1 && b.y <= Math.max(a.y, c.y) + 0.1
        : horizontal
          ? b.x >= Math.min(a.x, c.x) - 0.1 && b.x <= Math.max(a.x, c.x) + 0.1
          : false;
      if (!between) break;
      result.splice(result.length - 2, 1);
    }
  }
  return result;
}

function perimeterOptions(first, firstSide, second, secondSide, ring) {
  if (firstSide === secondSide) return [[first, second]];
  const horizontalSide = (side) => side === 'top' || side === 'bottom';
  if (horizontalSide(firstSide) !== horizontalSide(secondSide)) {
    const corner = { x: second.x, y: first.y };
    return [[first, corner, second]];
  }
  if (horizontalSide(firstSide)) {
    return [
      [first, { x: ring.left, y: first.y }, { x: ring.left, y: second.y }, second],
      [first, { x: ring.right, y: first.y }, { x: ring.right, y: second.y }, second]
    ];
  }
  return [
    [first, { x: first.x, y: ring.top }, { x: second.x, y: ring.top }, second],
    [first, { x: first.x, y: ring.bottom }, { x: second.x, y: ring.bottom }, second]
  ];
}

function routeScore(points) {
  let length = 0;
  let bends = 0;
  let lastDirection = null;
  for (let index = 0; index < points.length - 1; index += 1) {
    const current = points[index];
    const next = points[index + 1];
    length += Math.abs(current.x - next.x) + Math.abs(current.y - next.y);
    const direction = Math.abs(current.x - next.x) < 0.1 ? 'vertical' : 'horizontal';
    if (lastDirection && lastDirection !== direction) bends += 1;
    lastDirection = direction;
  }
  return length + bends * 120;
}

function feedbackPortPenalty(sourceSide, targetSide) {
  // Feedback paths read most naturally as a U-shaped return above or below the
  // main flow. Side-to-side returns can become collinear with a node's normal
  // incoming/outgoing edge even when the perimeter segment itself is clear.
  if (sourceSide === targetSide && (sourceSide === 'top' || sourceSide === 'bottom')) return 0;
  if (sourceSide === targetSide) return 2400;
  return 1200;
}

function outerReturnRoute(sourceBox, targetBox, obstacleBoxes, ring) {
  let best = null;
  for (const sourceSide of Object.keys(SIDE_PORT)) {
    const start = sidePoint(sourceBox, sourceSide);
    const sourceAnchor = sourceSide === 'top' ? { x: start.x, y: ring.top }
      : sourceSide === 'bottom' ? { x: start.x, y: ring.bottom }
        : sourceSide === 'left' ? { x: ring.left, y: start.y }
          : { x: ring.right, y: start.y };
    if (!clearSegment(start, sourceAnchor, obstacleBoxes)) continue;
    for (const targetSide of Object.keys(SIDE_PORT)) {
      const end = sidePoint(targetBox, targetSide);
      const targetAnchor = targetSide === 'top' ? { x: end.x, y: ring.top }
        : targetSide === 'bottom' ? { x: end.x, y: ring.bottom }
          : targetSide === 'left' ? { x: ring.left, y: end.y }
            : { x: ring.right, y: end.y };
      if (!clearSegment(targetAnchor, end, obstacleBoxes)) continue;
      for (const perimeter of perimeterOptions(sourceAnchor, sourceSide, targetAnchor, targetSide, ring)) {
        const route = compressOrthogonal([start, ...perimeter, end]);
        if (!route.slice(0, -1).every((point, index) => clearSegment(point, route[index + 1], obstacleBoxes))) continue;
        const score = routeScore(route) + feedbackPortPenalty(sourceSide, targetSide);
        if (!best || score < best.score) best = { route, sourceSide, targetSide, score };
      }
    }
  }
  return best;
}

export function reflowSwimlaneDrawio(xml) {
  const context = logicalContext(xml);
  if (context.lanes.length < 2) throw new Error('layout_requires_multiple_swimlanes');
  if (context.nodes.some((node) => !context.lanes.some((lane) => lane.id === node.parent))) {
    throw new Error('layout_requires_nodes_directly_owned_by_swimlanes');
  }
  const before = auditDrawioLayout(xml);
  const ranks = topologicalRanks(context.nodes, context.edges);
  const maxRank = Math.max(...ranks.values());
  const rankWidths = Array.from({ length: maxRank + 1 }, (_, rank) => Math.max(
    120,
    ...context.nodes.filter((node) => ranks.get(node.id) === rank).map((node) => numeric(node.geometry?.width, 120))
  ));
  const rankX = [];
  let columnCursor = LAYOUT.firstRankX;
  for (const width of rankWidths) {
    rankX.push(columnCursor);
    columnCursor += width + 40;
  }
  const laneWidth = Math.ceil(columnCursor + 40);
  const canvasBottom = LAYOUT.top + context.lanes.length * LAYOUT.laneHeight;
  const originals = cellsById(xml);
  const replacements = new Map();
  const positions = [];

  for (const [laneIndex, lane] of context.lanes.entries()) {
    if (!originals.has(lane.id)) continue;
    replacements.set(lane.id, updateCellXml(originals.get(lane.id), {
      geometry: { x: LAYOUT.left, y: LAYOUT.top + laneIndex * LAYOUT.laneHeight, width: laneWidth, height: LAYOUT.laneHeight }
    }));
  }

  const groups = new Map();
  for (const node of context.nodes) {
    const key = `${node.parent}:${ranks.get(node.id)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(node);
  }
  for (const group of groups.values()) group.sort((left, right) => numeric(left.geometry?.y) - numeric(right.geometry?.y));

  for (const node of context.nodes) {
    if (!originals.has(node.id)) continue;
    const rank = ranks.get(node.id);
    const group = groups.get(`${node.parent}:${rank}`);
    const groupIndex = group.findIndex((item) => item.id === node.id);
    const height = numeric(node.geometry?.height, 40);
    const yCenter = group.length === 1 ? LAYOUT.singleNodeY : LAYOUT.stackedNodeY[groupIndex] || (58 + groupIndex * 70);
    const y = Math.max(LAYOUT.laneHeader + 12, yCenter - height / 2);
    const x = rankX[rank];
    const style = updateStyle(node.style || '', { fontSize: Math.max(LAYOUT.minFontSize, numeric(node.styleMap?.fontSize, 12)) });
    replacements.set(node.id, updateCellXml(originals.get(node.id), { style, geometry: { x, y } }));
    positions.push({ id: node.id, lane: node.parent, rank, x, y });
  }

  const portKeys = ['exitX', 'exitY', 'exitDx', 'exitDy', 'exitPerimeter', 'entryX', 'entryY', 'entryDx', 'entryDy', 'entryPerimeter'];
  const outgoing = new Map(context.nodes.map((node) => [node.id, []]));
  const incoming = new Map(context.nodes.map((node) => [node.id, []]));
  for (const edge of context.edges) {
    outgoing.get(edge.source)?.push(edge);
    incoming.get(edge.target)?.push(edge);
  }
  const laneIndex = new Map(context.lanes.map((lane, index) => [lane.id, index]));
  const nodeById = new Map(context.nodes.map((node) => [node.id, node]));
  const edgeOrder = (direction) => (left, right) => {
    const leftNode = nodeById.get(direction === 'out' ? left.target : left.source);
    const rightNode = nodeById.get(direction === 'out' ? right.target : right.source);
    return (laneIndex.get(leftNode?.parent) ?? 0) - (laneIndex.get(rightNode?.parent) ?? 0)
      || (ranks.get(leftNode?.id) ?? 0) - (ranks.get(rightNode?.id) ?? 0)
      || left.id.localeCompare(right.id);
  };
  for (const edges of outgoing.values()) edges.sort(edgeOrder('out'));
  for (const edges of incoming.values()) edges.sort(edgeOrder('in'));
  const fraction = (edges, edge) => (edges.findIndex((item) => item.id === edge.id) + 1) / (edges.length + 1);
  for (const edge of context.edges) {
    if (!originals.has(edge.id)) continue;
    const exitY = fraction(outgoing.get(edge.source), edge);
    const entryY = fraction(incoming.get(edge.target), edge);
    replacements.set(edge.id, updateCellXml(originals.get(edge.id), {
      style: updateStyle(edge.style || '', {
        exitX: 1,
        exitY,
        exitDx: 0,
        exitDy: 0,
        exitPerimeter: 1,
        entryX: 0,
        entryY,
        entryDx: 0,
        entryDy: 0,
        entryPerimeter: 1,
        governedPorts: 1
      }, portKeys),
      clearWaypoints: true
    }));
  }

  const frameGeometries = {
    bg: { x: 20, y: -14, width: laneWidth + 40, height: canvasBottom + 34 },
    'title-bg': { x: 40, y: 6, width: laneWidth, height: 40 },
    title: { x: 40, y: 6, width: laneWidth, height: 40 },
    'legend-box': { x: 40, y: 50, width: laneWidth, height: 70 }
  };
  for (const [id, geometry] of Object.entries(frameGeometries)) {
    if (originals.has(id)) replacements.set(id, updateCellXml(originals.get(id), { geometry }));
  }

  const candidate = replaceCells(xml, replacements);
  const after = auditDrawioLayout(candidate);
  return {
    xml: candidate,
    report: {
      source_hash: contentHash(xml),
      candidate_hash: contentHash(candidate),
      status: after.status,
      strategy: 'horizontal-swimlanes-left-to-right-ranked-flow',
      changed_cells: replacements.size,
      max_rank: maxRank,
      rank_widths: rankWidths,
      lane_width: laneWidth,
      before,
      after,
      positions
    }
  };
}

export function reflowCyclicBusinessDrawio(xml) {
  const context = logicalContext(xml);
  const beforeLayout = auditDrawioLayout(xml);
  const beforeGeometry = auditDrawio(xml, { strictPorts: true });
  if (beforeLayout.status !== 'pass') throw new Error('cyclic_layout_requires_node_reflow');
  if (beforeGeometry.issues.some((item) => item.code === 'node_overlap')) throw new Error('cyclic_layout_requires_node_overlap_resolution');
  const feedbackEdges = cyclicFeedbackEdges(context);
  if (!feedbackEdges.length) throw new Error('cyclic_layout_feedback_edge_not_found');
  const originals = cellsById(xml);
  const replacements = new Map();
  const nodeBounds = context.nodes.map((node) => bbox(node, context.byId));
  const bounds = {
    left: Math.min(...nodeBounds.map((box) => box.left)),
    right: Math.max(...nodeBounds.map((box) => box.right)),
    bottom: Math.max(...nodeBounds.map((box) => box.bottom))
  };
  const nodeById = new Map(context.nodes.map((node) => [node.id, node]));
  const channels = [];
  const portKeys = ['exitX', 'exitY', 'exitDx', 'exitDy', 'exitPerimeter', 'entryX', 'entryY', 'entryDx', 'entryDy', 'entryPerimeter'];
  const sorted = feedbackEdges.slice().sort((left, right) => left.id.localeCompare(right.id));
  for (const [index, edge] of sorted.entries()) {
    if (!originals.has(edge.id)) continue;
    const sourceBox = bbox(nodeById.get(edge.source), context.byId);
    const targetBox = bbox(nodeById.get(edge.target), context.byId);
    const ring = {
      left: bounds.left - 60 - index * 36,
      right: bounds.right + 60 + index * 36,
      top: Math.min(...nodeBounds.map((box) => box.top)) - 60 - index * 36,
      bottom: bounds.bottom + 60 + index * 36
    };
    const obstacles = nodeBounds.filter((box) => box.id !== edge.source && box.id !== edge.target);
    const routed = outerReturnRoute(sourceBox, targetBox, obstacles, ring);
    if (!routed) throw new Error(`cyclic_layout_no_clear_outer_route: ${edge.id}`);
    const sourcePort = SIDE_PORT[routed.sourceSide];
    const targetPort = SIDE_PORT[routed.targetSide];
    const waypoints = routed.route.slice(1, -1);
    replacements.set(edge.id, updateCellXml(originals.get(edge.id), {
      attributes: { parent: '1' },
      style: updateStyle(edge.style || '', {
        edgeStyle: 'orthogonalEdgeStyle',
        exitX: sourcePort.x,
        exitY: sourcePort.y,
        exitDx: 0,
        exitDy: 0,
        exitPerimeter: 1,
        entryX: targetPort.x,
        entryY: targetPort.y,
        entryDx: 0,
        entryDy: 0,
        entryPerimeter: 1,
        governedReturn: 1
      }, portKeys),
      waypoints
    }));
    channels.push({
      edge_id: edge.id,
      source: edge.source,
      target: edge.target,
      source_port: routed.sourceSide,
      target_port: routed.targetSide,
      ring,
      waypoint_count: waypoints.length,
      score: routed.score,
      separation: index ? 36 : null
    });
  }
  const candidate = replaceCells(xml, replacements);
  const afterLayout = auditDrawioLayout(candidate);
  const afterGeometry = auditDrawio(candidate, { strictPorts: true });
  return {
    xml: candidate,
    report: {
      source_hash: contentHash(xml),
      candidate_hash: contentHash(candidate),
      status: afterLayout.status,
      strategy: 'cyclic-business-flow-outer-return-channels',
      changed_cells: replacements.size,
      feedback_edges: sorted.map((edge) => edge.id),
      channels,
      node_cells_changed: 0,
      before: beforeLayout,
      after: afterLayout,
      geometry_before: { status: beforeGeometry.status, errors: beforeGeometry.errors },
      geometry_after: { status: afterGeometry.status, errors: afterGeometry.errors }
    }
  };
}

export function reflowRankedCyclicBusinessDrawio(xml) {
  const context = logicalContext(xml);
  const beforeLayout = auditDrawioLayout(xml);
  const feedbackEdges = cyclicFeedbackEdges(context);
  const ranked = cyclicRankedPlan(context, feedbackEdges);
  if (!ranked) throw new Error('cyclic_ranked_reflow_not_supported');

  const maxColumns = 12;
  const trackHeight = 100;
  const lanePadding = 30;
  const nodeGap = 20;
  const maxRank = Math.max(...ranked.ranks.values());
  const tracks = Math.floor(maxRank / maxColumns) + 1;
  const originals = cellsById(xml);
  const replacements = new Map();
  const groups = new Map();

  for (const node of context.nodes) {
    const rank = ranked.ranks.get(node.id);
    const key = `${ranked.laneByNode.get(node.id)}:${rank}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(node);
  }
  for (const group of groups.values()) {
    group.sort((left, right) => {
      const a = bbox(left, context.byId);
      const b = bbox(right, context.byId);
      return a.top - b.top || a.left - b.left;
    });
  }

  const columnWidths = Array.from({ length: Math.min(maxColumns, maxRank + 1) }, () => 120);
  for (const [key, group] of groups) {
    const rank = Number(key.slice(key.lastIndexOf(':') + 1));
    const column = rank % maxColumns;
    const groupWidth = group.reduce((sum, node) => sum + numeric(node.geometry?.width, 120), 0) + nodeGap * Math.max(0, group.length - 1);
    columnWidths[column] = Math.max(columnWidths[column], groupWidth);
  }
  const columnX = [];
  let cursor = 80;
  for (const width of columnWidths) {
    columnX.push(cursor);
    cursor += width + 30;
  }
  const laneWidth = cursor + 70;
  const laneTracks = new Map(context.lanes.map((lane) => [lane.id, []]));
  for (const node of context.nodes) {
    const laneId = ranked.laneByNode.get(node.id);
    const track = Math.floor(ranked.ranks.get(node.id) / maxColumns);
    if (!laneTracks.get(laneId).includes(track)) laneTracks.get(laneId).push(track);
  }
  for (const values of laneTracks.values()) values.sort((left, right) => left - right);
  const laneHeights = new Map(context.lanes.map((lane) => [lane.id, lanePadding * 2 + Math.max(1, laneTracks.get(lane.id).length) * trackHeight]));
  const laneTops = new Map();
  let canvasBottom = LAYOUT.top;
  for (const lane of context.lanes) {
    laneTops.set(lane.id, canvasBottom);
    canvasBottom += laneHeights.get(lane.id);
  }
  const positions = [];

  for (const lane of context.lanes) {
    if (!originals.has(lane.id)) continue;
    replacements.set(lane.id, updateCellXml(originals.get(lane.id), {
      geometry: { x: LAYOUT.left, y: laneTops.get(lane.id), width: laneWidth, height: laneHeights.get(lane.id) }
    }));
  }

  for (const [key, group] of groups) {
    const split = key.lastIndexOf(':');
    const laneId = key.slice(0, split);
    const rank = Number(key.slice(split + 1));
    const column = rank % maxColumns;
    const track = Math.floor(rank / maxColumns);
    const localTrack = laneTracks.get(laneId).indexOf(track);
    const totalWidth = group.reduce((sum, node) => sum + numeric(node.geometry?.width, 120), 0) + nodeGap * Math.max(0, group.length - 1);
    let groupX = columnX[column] + (columnWidths[column] - totalWidth) / 2;
    for (const node of group) {
      if (!originals.has(node.id)) continue;
      const width = numeric(node.geometry?.width, 120);
      const height = numeric(node.geometry?.height, 50);
      const y = lanePadding + localTrack * trackHeight + (trackHeight - height) / 2;
      replacements.set(node.id, updateCellXml(originals.get(node.id), {
        attributes: { parent: laneId },
        style: updateStyle(node.style || '', { fontSize: 13 }),
        geometry: { x: groupX, y, width, height }
      }));
      positions.push({ id: node.id, lane_id: laneId, rank, track, local_track: localTrack, column, x: groupX, y, width, height });
      groupX += width + nodeGap;
    }
  }

  const frameGeometries = {
    bg: { x: 20, y: -14, width: laneWidth + 40, height: canvasBottom + 34 },
    'title-bg': { x: 40, y: 6, width: laneWidth, height: 40 },
    title: { x: 40, y: 6, width: laneWidth, height: 40 },
    'legend-box': { x: 40, y: 50, width: laneWidth, height: 70 }
  };
  for (const [id, geometry] of Object.entries(frameGeometries)) {
    if (originals.has(id)) replacements.set(id, updateCellXml(originals.get(id), { geometry }));
  }

  const rankedXml = replaceCells(xml, replacements);
  const outer = reflowCyclicBusinessDrawio(rankedXml);
  const afterLayout = auditDrawioLayout(outer.xml);
  return {
    xml: outer.xml,
    report: {
      source_hash: contentHash(xml),
      candidate_hash: contentHash(outer.xml),
      status: afterLayout.status,
      strategy: 'cyclic-business-flow-ranked-lanes-with-outer-returns',
      changed_cells: replacements.size + outer.report.changed_cells,
      feedback_edges: outer.report.feedback_edges,
      channels: outer.report.channels,
      node_cells_changed: context.nodes.length,
      max_rank: maxRank,
      max_columns: maxColumns,
      tracks,
      lane_width: laneWidth,
      lane_height: Math.max(...laneHeights.values()),
      lane_heights: Object.fromEntries(laneHeights),
      font_size: 13,
      before: beforeLayout,
      after: afterLayout,
      positions
    }
  };
}
