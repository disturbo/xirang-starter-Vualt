import { absoluteOrigin, auditDrawio, bbox, contentHash, isContainer, isDecorative, isText, parseDrawio } from './drawio.mjs';

const PORTS = {
  left: { x: 0, y: 0.5, outgoing: 'L', incoming: 'R' },
  leftTop: { x: 0, y: 1 / 3, outgoing: 'L', incoming: 'R' },
  leftBottom: { x: 0, y: 2 / 3, outgoing: 'L', incoming: 'R' },
  leftUpper: { x: 0, y: 0.25, outgoing: 'L', incoming: 'R' },
  leftLower: { x: 0, y: 0.75, outgoing: 'L', incoming: 'R' },
  right: { x: 1, y: 0.5, outgoing: 'R', incoming: 'L' },
  rightTop: { x: 1, y: 1 / 3, outgoing: 'R', incoming: 'L' },
  rightBottom: { x: 1, y: 2 / 3, outgoing: 'R', incoming: 'L' },
  rightUpper: { x: 1, y: 0.25, outgoing: 'R', incoming: 'L' },
  rightLower: { x: 1, y: 0.75, outgoing: 'R', incoming: 'L' },
  rightTopCorner: { x: 1, y: 0, outgoing: 'R', incoming: 'L' },
  rightBottomCorner: { x: 1, y: 1, outgoing: 'R', incoming: 'L' },
  leftTopCorner: { x: 0, y: 0, outgoing: 'L', incoming: 'R' },
  leftBottomCorner: { x: 0, y: 1, outgoing: 'L', incoming: 'R' },
  top: { x: 0.5, y: 0, outgoing: 'U', incoming: 'D' },
  topLeft: { x: 0.35, y: 0, outgoing: 'U', incoming: 'D' },
  topRight: { x: 0.65, y: 0, outgoing: 'U', incoming: 'D' },
  bottom: { x: 0.5, y: 1, outgoing: 'D', incoming: 'U' },
  bottomLeft: { x: 0.35, y: 1, outgoing: 'D', incoming: 'U' },
  bottomRight: { x: 0.65, y: 1, outgoing: 'D', incoming: 'U' }
};

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function graphContext(xml) {
  const graph = parseDrawio(xml);
  const byId = new Map(graph.cells.filter((cell) => cell.id).map((cell) => [cell.id, cell]));
  const businessEdges = graph.cells.filter((cell) => cell.edge === '1' && !isDecorative(cell, byId));
  const logicalNodes = graph.cells.filter((cell) => cell.vertex === '1'
    && !isContainer(cell)
    && !isText(cell)
    && !isDecorative(cell, byId)
    && numeric(cell.geometry?.width) > 0
    && numeric(cell.geometry?.height) > 0);
  return { graph, byId, businessEdges, logicalNodes, originCache: new Map() };
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

function setStyleValues(style, additions) {
  const parsed = parseStyle(style);
  for (const [key, value] of Object.entries(additions)) {
    if (!parsed.values.has(key)) parsed.order.push(key);
    parsed.values.set(key, String(value));
  }
  return `${parsed.order.map((key) => parsed.values.get(key) === null ? key : `${key}=${parsed.values.get(key)}`).join(';')};`;
}

function existingPort(edge, prefix) {
  const x = edge.styleMap?.[`${prefix}X`];
  const y = edge.styleMap?.[`${prefix}Y`];
  if (x === undefined || y === undefined) return null;
  const nx = numeric(x);
  const ny = numeric(y);
  for (const [name, port] of Object.entries(PORTS)) {
    if (Math.abs(port.x - nx) < 0.01 && Math.abs(port.y - ny) < 0.01) return name;
  }
  return null;
}

function center(box) {
  return { x: (box.left + box.right) / 2, y: (box.top + box.bottom) / 2 };
}

function choosePorts(edge, source, target, byId, cache) {
  const sourceBox = bbox(source, byId, cache);
  const targetBox = bbox(target, byId, cache);
  const from = center(sourceBox);
  const to = center(targetBox);
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  let sourcePort;
  let targetPort;
  if (Math.abs(dx) >= Math.abs(dy) * 1.15) {
    sourcePort = dx >= 0 ? 'right' : 'left';
    targetPort = dx >= 0 ? 'left' : 'right';
  } else {
    sourcePort = dy >= 0 ? 'bottom' : 'top';
    targetPort = dy >= 0 ? 'top' : 'bottom';
  }
  return {
    sourcePort: existingPort(edge, 'exit') || sourcePort,
    targetPort: existingPort(edge, 'entry') || targetPort
  };
}

function pointAtPort(box, portName) {
  const port = PORTS[portName];
  return { x: box.left + box.width * port.x, y: box.top + box.height * port.y };
}

function insideBox(point, box, epsilon = 0.1) {
  return point.x > box.left + epsilon && point.x < box.right - epsilon
    && point.y > box.top + epsilon && point.y < box.bottom - epsilon;
}

function segmentClear(a, b, obstacles) {
  const epsilon = 0.1;
  if (Math.abs(a.x - b.x) < epsilon) {
    return obstacles.every((box) => !(a.x > box.left + epsilon
      && a.x < box.right - epsilon
      && Math.max(a.y, b.y) > box.top + epsilon
      && Math.min(a.y, b.y) < box.bottom - epsilon));
  }
  if (Math.abs(a.y - b.y) < epsilon) {
    return obstacles.every((box) => !(a.y > box.top + epsilon
      && a.y < box.bottom - epsilon
      && Math.max(a.x, b.x) > box.left + epsilon
      && Math.min(a.x, b.x) < box.right - epsilon));
  }
  return false;
}

function segmentOverlapLength(a, b, c, d) {
  if (Math.abs(a.x - b.x) < 0.1 && Math.abs(c.x - d.x) < 0.1 && Math.abs(a.x - c.x) < 0.1) {
    return Math.max(0, Math.min(Math.max(a.y, b.y), Math.max(c.y, d.y)) - Math.max(Math.min(a.y, b.y), Math.min(c.y, d.y)));
  }
  if (Math.abs(a.y - b.y) < 0.1 && Math.abs(c.y - d.y) < 0.1 && Math.abs(a.y - c.y) < 0.1) {
    return Math.max(0, Math.min(Math.max(a.x, b.x), Math.max(c.x, d.x)) - Math.max(Math.min(a.x, b.x), Math.min(c.x, d.x)));
  }
  return 0;
}

function routeOverlap(points, occupiedSegments = []) {
  let overlap = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    for (const segment of occupiedSegments) overlap += segmentOverlapLength(points[index], points[index + 1], segment.a, segment.b);
  }
  return overlap;
}

function simpleForwardRoute(start, end, channelX, obstacleBoxes, padding = 20) {
  if (end.x <= start.x + 20) return null;
  const obstacles = obstacleBoxes.map((box) => ({
    left: box.left - padding,
    right: box.right + padding,
    top: box.top - padding,
    bottom: box.bottom + padding
  }));
  const route = compressPath([
    start,
    { x: channelX, y: start.y },
    { x: channelX, y: end.y },
    end
  ]);
  return route.slice(0, -1).every((point, index) => segmentClear(point, route[index + 1], obstacles)) ? route : null;
}

function direction(a, b) {
  if (Math.abs(a.x - b.x) < 0.1) return b.y >= a.y ? 'D' : 'U';
  return b.x >= a.x ? 'R' : 'L';
}

function compressPath(points) {
  const result = [];
  for (const point of points) {
    const previous = result[result.length - 1];
    if (previous && Math.abs(previous.x - point.x) < 0.1 && Math.abs(previous.y - point.y) < 0.1) continue;
    result.push(point);
    while (result.length >= 3) {
      const a = result[result.length - 3];
      const b = result[result.length - 2];
      const c = result[result.length - 1];
      if ((Math.abs(a.x - b.x) < 0.1 && Math.abs(b.x - c.x) < 0.1)
        || (Math.abs(a.y - b.y) < 0.1 && Math.abs(b.y - c.y) < 0.1)) {
        result.splice(result.length - 2, 1);
      } else break;
    }
  }
  return result;
}

function uniqueSorted(values) {
  return [...new Set(values.map((value) => Math.round(value * 1000) / 1000))].sort((a, b) => a - b);
}

function routeOrthogonal(start, end, sourcePort, targetPort, obstacleBoxes, options = {}) {
  const padding = options.padding ?? 20;
  const obstacles = obstacleBoxes.map((box) => ({
    left: box.left - padding,
    right: box.right + padding,
    top: box.top - padding,
    bottom: box.bottom + padding
  }));
  const xs = uniqueSorted([
    start.x,
    end.x,
    (start.x + end.x) / 2 + (options.channelOffset || 0),
    ...obstacles.flatMap((box) => [box.left, box.right])
  ]);
  const ys = uniqueSorted([
    start.y,
    end.y,
    (start.y + end.y) / 2,
    ...obstacles.flatMap((box) => [box.top, box.bottom])
  ]);
  const nodes = [];
  const nodeByKey = new Map();
  for (const x of xs) {
    for (const y of ys) {
      const point = { x, y };
      if (obstacles.some((box) => insideBox(point, box))) continue;
      const id = nodes.length;
      nodes.push(point);
      nodeByKey.set(`${x},${y}`, id);
    }
  }
  const startId = nodeByKey.get(`${start.x},${start.y}`);
  const endId = nodeByKey.get(`${end.x},${end.y}`);
  if (startId === undefined || endId === undefined) return null;

  const adjacency = Array.from({ length: nodes.length }, () => []);
  const connectLine = (ids, coordinate) => {
    ids.sort((left, right) => coordinate(nodes[left]) - coordinate(nodes[right]));
    for (let index = 0; index < ids.length - 1; index += 1) {
      const from = ids[index];
      const to = ids[index + 1];
      if (!segmentClear(nodes[from], nodes[to], obstacles)) continue;
      const length = Math.abs(nodes[from].x - nodes[to].x) + Math.abs(nodes[from].y - nodes[to].y);
      const overlap = routeOverlap([nodes[from], nodes[to]], options.occupiedSegments);
      adjacency[from].push({ to, length, overlap });
      adjacency[to].push({ to: from, length, overlap });
    }
  };
  for (const x of xs) connectLine(nodes.map((point, id) => ({ point, id })).filter((item) => item.point.x === x).map((item) => item.id), (point) => point.y);
  for (const y of ys) connectLine(nodes.map((point, id) => ({ point, id })).filter((item) => item.point.y === y).map((item) => item.id), (point) => point.x);

  const wantedStart = PORTS[sourcePort].outgoing;
  const wantedEnd = PORTS[targetPort].incoming;
  const queue = [{ id: startId, last: '-', cost: 0, path: [startId] }];
  const best = new Map([[`${startId}|-`, 0]]);
  while (queue.length) {
    queue.sort((a, b) => a.cost - b.cost);
    const current = queue.shift();
    if (current.id === endId) return compressPath(current.path.map((id) => nodes[id]));
    for (const next of adjacency[current.id]) {
      const move = direction(nodes[current.id], nodes[next.to]);
      if (current.id === startId && move !== wantedStart) continue;
      if (next.to === endId && move !== wantedEnd) continue;
      const bend = current.last !== '-' && current.last !== move ? 120 : 0;
      const cost = current.cost + next.length + bend + next.overlap * 1000;
      const key = `${next.to}|${move}`;
      if (cost >= (best.get(key) ?? Number.POSITIVE_INFINITY)) continue;
      best.set(key, cost);
      queue.push({ id: next.to, last: move, cost, path: [...current.path, next.to] });
    }
  }
  return null;
}

function pathScore(points) {
  let distance = 0;
  let bends = 0;
  let previousDirection = null;
  for (let index = 0; index < points.length - 1; index += 1) {
    distance += Math.abs(points[index].x - points[index + 1].x) + Math.abs(points[index].y - points[index + 1].y);
    const currentDirection = direction(points[index], points[index + 1]);
    if (previousDirection && previousDirection !== currentDirection) bends += 1;
    previousDirection = currentDirection;
  }
  return distance + bends * 120;
}

function routeWithPortFallback(sourceBox, targetBox, preferredPorts, obstacleBoxes, options = {}) {
  const sourceCandidates = [preferredPorts.sourcePort, 'right', 'rightTop', 'rightBottom', 'left', 'leftTop', 'leftBottom', 'top', 'bottom']
    .filter((value, index, all) => all.indexOf(value) === index);
  const targetCandidates = [preferredPorts.targetPort, 'left', 'leftTop', 'leftBottom', 'right', 'rightTop', 'rightBottom', 'top', 'bottom']
    .filter((value, index, all) => all.indexOf(value) === index);
  let best = null;
  for (const sourcePort of sourceCandidates) {
    for (const targetPort of targetCandidates) {
      const start = pointAtPort(sourceBox, sourcePort);
      const end = pointAtPort(targetBox, targetPort);
      const route = routeOrthogonal(start, end, sourcePort, targetPort, obstacleBoxes, options);
      if (!route) continue;
      const portChangePenalty = (sourcePort === preferredPorts.sourcePort ? 0 : 5000)
        + (targetPort === preferredPorts.targetPort ? 0 : 5000);
      const score = pathScore(route) + portChangePenalty + routeOverlap(route, options.occupiedSegments) * 1000;
      if (!best || score < best.score) best = { route, sourcePort, targetPort, score };
    }
  }
  return best;
}

function routeSelfLoop(sourceBox, obstacleBoxes, options = {}) {
  const padding = options.padding ?? 20;
  const clearance = Math.max(36, padding + 16);
  const paddedObstacles = obstacleBoxes.map((box) => ({
    left: box.left - padding,
    right: box.right + padding,
    top: box.top - padding,
    bottom: box.bottom + padding
  }));
  const candidates = [
    {
      sourcePort: 'rightTop', targetPort: 'rightBottom',
      route: [
        pointAtPort(sourceBox, 'rightTop'),
        { x: sourceBox.right + clearance, y: sourceBox.top + sourceBox.height / 3 },
        { x: sourceBox.right + clearance, y: sourceBox.top + sourceBox.height * 2 / 3 },
        pointAtPort(sourceBox, 'rightBottom')
      ]
    },
    {
      sourcePort: 'leftBottom', targetPort: 'leftTop',
      route: [
        pointAtPort(sourceBox, 'leftBottom'),
        { x: sourceBox.left - clearance, y: sourceBox.top + sourceBox.height * 2 / 3 },
        { x: sourceBox.left - clearance, y: sourceBox.top + sourceBox.height / 3 },
        pointAtPort(sourceBox, 'leftTop')
      ]
    }
  ];
  return candidates
    .filter((candidate) => candidate.route.slice(0, -1).every((point, index) => segmentClear(point, candidate.route[index + 1], paddedObstacles)))
    .map((candidate) => ({
      ...candidate,
      score: pathScore(candidate.route) + routeOverlap(candidate.route, options.occupiedSegments) * 1000
    }))
    .sort((left, right) => left.score - right.score)[0] || null;
}

function escapeAttribute(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function replaceAttribute(tag, name, value) {
  const pattern = new RegExp(`\\s${name}="[^"]*"`);
  if (pattern.test(tag)) return tag.replace(pattern, ` ${name}="${escapeAttribute(value)}"`);
  return tag.replace(/\s*\/?\s*>$/, (ending) => ` ${name}="${escapeAttribute(value)}"${ending}`);
}

function setWaypoints(cellXml, points) {
  const pointXml = points.map((point) => `<mxPoint x="${Math.round(point.x * 1000) / 1000}" y="${Math.round(point.y * 1000) / 1000}"/>`).join('');
  const arrayXml = `<Array as="points">${pointXml}</Array>`;
  if (/<Array\b[^>]*as="points"[^>]*>[\s\S]*?<\/Array>/.test(cellXml)) {
    return cellXml.replace(/<Array\b[^>]*as="points"[^>]*>[\s\S]*?<\/Array>/, arrayXml);
  }
  if (/<mxGeometry\b([^>]*?)\/>/.test(cellXml)) {
    return cellXml.replace(/<mxGeometry\b([^>]*?)\/>/, `<mxGeometry$1>${arrayXml}</mxGeometry>`);
  }
  return cellXml.replace(/(<mxGeometry\b[^>]*>)/, `$1${arrayXml}`);
}

function replaceCells(xml, replacements) {
  const pattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  return xml.replace(pattern, (cellXml, attrFragment) => {
    const id = attrFragment.match(/\bid="([^"]*)"/)?.[1];
    return replacements.get(id) || cellXml;
  });
}

function updateEdgeXml(original, newStyle, localWaypoints, geometryAttributes = null) {
  const opening = original.match(/^<mxCell\b[^>]*>/)?.[0];
  if (!opening) return original;
  const updatedOpening = replaceAttribute(opening, 'style', newStyle);
  let result = updatedOpening + original.slice(opening.length);
  result = result.replace(/<mxPoint\b[^>]*\bas="(?:sourcePoint|targetPoint)"[^>]*\/?\s*>/g, '');
  if (localWaypoints) result = setWaypoints(result, localWaypoints);
  if (geometryAttributes) {
    result = result.replace(/<mxGeometry\b[^>]*\/?\s*>/, (tag) => {
      let updated = tag;
      for (const [name, value] of Object.entries(geometryAttributes)) updated = replaceAttribute(updated, name, value);
      return updated;
    });
  }
  return result;
}

function cellXmlById(xml) {
  const result = new Map();
  const pattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  for (const match of xml.matchAll(pattern)) {
    const id = match[1].match(/\bid="([^"]*)"/)?.[1];
    if (id) result.set(id, match[0]);
  }
  return result;
}

function replaceGeometryPosition(cellXml, x, y) {
  return cellXml.replace(/<mxGeometry\b[^>]*\/?\s*>/, (tag) => {
    let updated = replaceAttribute(tag, 'x', Math.round(x * 1000) / 1000);
    updated = replaceAttribute(updated, 'y', Math.round(y * 1000) / 1000);
    return updated;
  });
}

function replaceGeometryDimension(cellXml, dimension, value) {
  return cellXml.replace(/<mxGeometry\b[^>]*\/?\s*>/, (tag) => replaceAttribute(tag, dimension, Math.round(value * 1000) / 1000));
}

function containsBox(container, child, epsilon = 0.5) {
  return child.left >= container.left - epsilon
    && child.right <= container.right + epsilon
    && child.top >= container.top - epsilon
    && child.bottom <= container.bottom + epsilon;
}

function normalizeSwimlaneMembership(xml) {
  const context = graphContext(xml);
  const originals = cellXmlById(xml);
  const replacements = new Map();
  const nodeChanges = [];
  const containers = context.graph.cells.filter(isContainer);
  for (const node of context.logicalNodes) {
    const currentParent = context.byId.get(node.parent);
    if (!currentParent || !isContainer(currentParent)) continue;
    const nodeBox = bbox(node, context.byId, context.originCache);
    if (containsBox(bbox(currentParent, context.byId, context.originCache), nodeBox)) continue;
    const containing = containers.filter((container) => container.parent === currentParent.parent
      && containsBox(bbox(container, context.byId, context.originCache), nodeBox));
    if (containing.length !== 1 || !originals.has(node.id)) continue;
    const target = containing[0];
    const targetOrigin = absoluteOrigin(target, context.byId, context.originCache);
    let updated = originals.get(node.id);
    const opening = updated.match(/^<mxCell\b[^>]*>/)?.[0];
    if (!opening) continue;
    updated = replaceAttribute(opening, 'parent', target.id) + updated.slice(opening.length);
    updated = replaceGeometryPosition(updated, nodeBox.left - targetOrigin.x, nodeBox.top - targetOrigin.y);
    replacements.set(node.id, updated);
    nodeChanges.push({
      node_id: node.id,
      from_parent: currentParent.id,
      to_parent: target.id,
      absolute_position_preserved: true
    });
  }

  let working = replaceCells(xml, replacements);
  const membershipCorrected = graphContext(working);
  const membershipOriginals = cellXmlById(working);
  const expansionRequests = [];
  for (const node of membershipCorrected.logicalNodes) {
    const parent = membershipCorrected.byId.get(node.parent);
    if (!parent || !isContainer(parent)) continue;
    const nodeBox = bbox(node, membershipCorrected.byId, membershipCorrected.originCache);
    const parentBox = bbox(parent, membershipCorrected.byId, membershipCorrected.originCache);
    if (containsBox(parentBox, nodeBox)) continue;
    const verticallyContained = nodeBox.top >= parentBox.top - 0.5 && nodeBox.bottom <= parentBox.bottom + 0.5;
    const horizontallyContained = nodeBox.left >= parentBox.left - 0.5 && nodeBox.right <= parentBox.right + 0.5;
    const rightOverflow = nodeBox.right - parentBox.right;
    const bottomOverflow = nodeBox.bottom - parentBox.bottom;
    if (verticallyContained && rightOverflow > 0.5 && rightOverflow <= 80) {
      expansionRequests.push({ parent, dimension: 'width', required: nodeBox.right - parentBox.left + 4, parentBox });
    } else if (horizontallyContained && bottomOverflow > 0.5 && bottomOverflow <= 80) {
      expansionRequests.push({ parent, dimension: 'height', required: nodeBox.bottom - parentBox.top + 4, parentBox });
    }
  }
  const expansionByContainer = new Map();
  for (const request of expansionRequests) {
    for (const container of membershipCorrected.graph.cells.filter(isContainer)) {
      if (container.parent !== request.parent.parent) continue;
      const box = bbox(container, membershipCorrected.byId, membershipCorrected.originCache);
      const aligned = request.dimension === 'width'
        ? Math.abs(box.left - request.parentBox.left) < 0.5 && Math.abs(box.width - request.parentBox.width) < 0.5
        : Math.abs(box.top - request.parentBox.top) < 0.5 && Math.abs(box.height - request.parentBox.height) < 0.5;
      if (!aligned) continue;
      const key = `${container.id}:${request.dimension}`;
      const current = expansionByContainer.get(key);
      const required = Math.max(request.required, current?.required || 0);
      expansionByContainer.set(key, { container, dimension: request.dimension, required });
    }
  }
  const expansionReplacements = new Map();
  const containerChanges = [];
  for (const request of expansionByContainer.values()) {
    const original = membershipOriginals.get(request.container.id);
    if (!original) continue;
    const before = numeric(request.container.geometry?.[request.dimension]);
    if (request.required <= before + 0.5) continue;
    expansionReplacements.set(request.container.id, replaceGeometryDimension(original, request.dimension, request.required));
    containerChanges.push({
      container_id: request.container.id,
      dimension: request.dimension,
      from: before,
      to: request.required,
      aligned_group_expansion: true
    });
  }
  working = replaceCells(working, expansionReplacements);
  const corrected = graphContext(working);
  const correctedOriginals = cellXmlById(working);
  const edgeReplacements = new Map();
  const edgeChanges = [];
  const changedNodeIds = new Set(nodeChanges.map((item) => item.node_id));
  for (const edge of corrected.businessEdges) {
    const source = corrected.byId.get(edge.source);
    const target = corrected.byId.get(edge.target);
    if (!source || !target || !correctedOriginals.has(edge.id)) continue;
    if (!changedNodeIds.has(source.id) && !changedNodeIds.has(target.id)) continue;
    const sharedContainer = source.parent === target.parent && isContainer(corrected.byId.get(source.parent));
    const desiredParent = sharedContainer ? source.parent : '1';
    if (edge.parent === desiredParent) continue;
    const oldParent = corrected.byId.get(edge.parent);
    const newParent = corrected.byId.get(desiredParent);
    const oldOrigin = oldParent ? absoluteOrigin(oldParent, corrected.byId, corrected.originCache) : { x: 0, y: 0 };
    const newOrigin = newParent ? absoluteOrigin(newParent, corrected.byId, corrected.originCache) : { x: 0, y: 0 };
    let updated = correctedOriginals.get(edge.id);
    const opening = updated.match(/^<mxCell\b[^>]*>/)?.[0];
    if (!opening) continue;
    updated = replaceAttribute(opening, 'parent', desiredParent) + updated.slice(opening.length);
    if (edge.geometry?.points?.length) {
      updated = setWaypoints(updated, edge.geometry.points.map((point) => ({
        x: point.x + oldOrigin.x - newOrigin.x,
        y: point.y + oldOrigin.y - newOrigin.y
      })));
    }
    edgeReplacements.set(edge.id, updated);
    edgeChanges.push({ edge_id: edge.id, from_parent: edge.parent, to_parent: desiredParent });
  }
  working = replaceCells(working, edgeReplacements);
  return { xml: working, nodeChanges, edgeChanges, containerChanges };
}

export function remediateDrawio(xml, options = {}) {
  const membership = normalizeSwimlaneMembership(xml);
  const context = graphContext(membership.xml);
  const before = auditDrawio(xml, { strictPorts: true });
  const rerouteCodes = new Set(['implicit_auto_route', 'edge_crosses_node', 'diagonal_segment', 'edge_overlap']);
  const rerouteReasons = new Map();
  for (const item of before.issues) {
    if (!rerouteCodes.has(item.code) || !item.cell_id) continue;
    if (!rerouteReasons.has(item.cell_id)) rerouteReasons.set(item.cell_id, new Set());
    rerouteReasons.get(item.cell_id).add(item.code);
    if (item.other_edge_id) {
      if (!rerouteReasons.has(item.other_edge_id)) rerouteReasons.set(item.other_edge_id, new Set());
      rerouteReasons.get(item.other_edge_id).add(item.code);
    }
  }

  const sourceCells = cellXmlById(xml);
  const originals = cellXmlById(membership.xml);
  const replacements = new Map();
  const changes = [];
  const unresolved = [];
  const routingEdges = options.stateMachine
    ? context.businessEdges.map((edge, index) => {
      const source = context.byId.get(edge.source);
      const target = context.byId.get(edge.target);
      if (!source || !target) return { edge, index, tier: 4 };
      if (source.id === target.id) return { edge, index, tier: 3 };
      const from = center(bbox(source, context.byId, context.originCache));
      const to = center(bbox(target, context.byId, context.originCache));
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const aligned = Math.abs(dx) < 0.1 || Math.abs(dy) < 0.1;
      const forward = dx > 0.1 || (Math.abs(dx) < 0.1 && dy > 0.1);
      return { edge, index, tier: aligned ? forward ? 0 : 1 : 2 };
    }).sort((left, right) => left.tier - right.tier || left.index - right.index).map((item) => item.edge)
    : context.businessEdges;
  const outgoingGroups = new Map();
  for (const edge of routingEdges) {
    if (!outgoingGroups.has(edge.source)) outgoingGroups.set(edge.source, []);
    outgoingGroups.get(edge.source).push(edge.id);
  }
  const occupiedSegments = [];
  const addOccupied = (edge, points) => {
    for (let index = 0; index < points.length - 1; index += 1) {
      occupiedSegments.push({ edge_id: edge.id, a: points[index], b: points[index + 1] });
    }
  };
  for (const edge of routingEdges) {
    if (edge.styleMap?.governedReturn !== '1') continue;
    const source = context.byId.get(edge.source);
    const target = context.byId.get(edge.target);
    if (!source || !target) continue;
    const ports = choosePorts(edge, source, target, context.byId, context.originCache);
    const parent = context.byId.get(edge.parent);
    const parentOrigin = parent ? absoluteOrigin(parent, context.byId, context.originCache) : { x: 0, y: 0 };
    const points = [
      pointAtPort(bbox(source, context.byId, context.originCache), ports.sourcePort),
      ...(edge.geometry?.points || []).map((point) => ({ x: numeric(point.x) + parentOrigin.x, y: numeric(point.y) + parentOrigin.y })),
      pointAtPort(bbox(target, context.byId, context.originCache), ports.targetPort)
    ];
    addOccupied(edge, points);
  }
  for (const edge of routingEdges) {
    const source = context.byId.get(edge.source);
    const target = context.byId.get(edge.target);
    if (!source || !target || !originals.has(edge.id)) {
      unresolved.push({ edge_id: edge.id, reason: 'missing_source_or_target' });
      continue;
    }
    const selfLoop = source.id === target.id;
    let ports = choosePorts(edge, source, target, context.byId, context.originCache);
    const reasons = [...(rerouteReasons.get(edge.id) || [])];
    const sourceBox = bbox(source, context.byId, context.originCache);
    const targetBox = bbox(target, context.byId, context.originCache);
    if (options.stateMachine && !selfLoop) {
      const sourceCenter = center(sourceBox);
      const targetCenter = center(targetBox);
      const sameColumnReturn = targetCenter.y < sourceCenter.y
        && Math.abs(targetCenter.x - sourceCenter.x) <= Math.max(sourceBox.width, targetBox.width) * 0.25;
      if (sameColumnReturn) ports = { sourcePort: 'left', targetPort: 'left' };
    }
    let start = pointAtPort(sourceBox, ports.sourcePort);
    let end = pointAtPort(targetBox, ports.targetPort);
    const aligned = Math.abs(start.x - end.x) < 0.1 || Math.abs(start.y - end.y) < 0.1;
    const governedReturn = edge.styleMap?.governedReturn === '1';
    const needsRoute = !governedReturn && (options.stateMachine || reasons.length > 0 || (!edge.geometry?.points?.length && !aligned));
    let localWaypoints = null;
    if (needsRoute) {
      const obstacleBoxes = context.logicalNodes
        .filter((node) => node.id !== source.id && node.id !== target.id)
        .map((node) => bbox(node, context.byId, context.originCache));
      const siblings = outgoingGroups.get(edge.source) || [edge.id];
      const siblingIndex = siblings.indexOf(edge.id);
      const channelOffset = (siblingIndex - (siblings.length - 1) / 2) * 20;
      const crossLaneSiblings = siblings
        .map((id) => context.businessEdges.find((item) => item.id === id))
        .filter((item) => item && context.byId.get(item.source)?.parent !== context.byId.get(item.target)?.parent);
      const crossLaneIndex = crossLaneSiblings.findIndex((item) => item.id === edge.id);
      const governed = edge.styleMap?.governedPorts === '1';
      const governedStart = pointAtPort(sourceBox, ports.sourcePort);
      const governedEnd = pointAtPort(targetBox, ports.targetPort);
      const channelFraction = crossLaneIndex >= 0 ? (crossLaneIndex + 1) / (crossLaneSiblings.length + 1) : 0.5;
      const governedRoute = governed
        ? simpleForwardRoute(
          governedStart,
          governedEnd,
          governedStart.x + (governedEnd.x - governedStart.x) * channelFraction,
          obstacleBoxes,
          options.padding ?? 20
        )
        : null;
      const routed = selfLoop && options.stateMachine
        ? routeSelfLoop(sourceBox, obstacleBoxes, { ...options, occupiedSegments })
        : governedRoute
          ? { route: governedRoute, sourcePort: ports.sourcePort, targetPort: ports.targetPort }
          : routeWithPortFallback(sourceBox, targetBox, ports, obstacleBoxes, { ...options, channelOffset, occupiedSegments });
      if (!routed) {
        unresolved.push({ edge_id: edge.id, reason: 'no_safe_orthogonal_route', triggers: reasons });
      } else {
        ports = { sourcePort: routed.sourcePort, targetPort: routed.targetPort };
        start = pointAtPort(sourceBox, ports.sourcePort);
        end = pointAtPort(targetBox, ports.targetPort);
        const parent = context.byId.get(edge.parent);
        const parentOrigin = parent ? absoluteOrigin(parent, context.byId, context.originCache) : { x: 0, y: 0 };
        localWaypoints = routed.route.slice(1, -1).map((point) => ({ x: point.x - parentOrigin.x, y: point.y - parentOrigin.y }));
      }
    }
    const sourcePort = PORTS[ports.sourcePort];
    const targetPort = PORTS[ports.targetPort];
    const sourceCenter = center(sourceBox);
    const targetCenter = center(targetBox);
    const stateReturnLabel = options.stateMachine
      && targetCenter.y < sourceCenter.y
      && Math.abs(targetCenter.x - sourceCenter.x) <= Math.max(sourceBox.width, targetBox.width) * 0.25;
    const additions = {
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
      entryPerimeter: 1
    };
    const newStyle = setStyleValues(edge.style || '', additions);
    const updated = updateEdgeXml(
      originals.get(edge.id),
      newStyle,
      localWaypoints,
      stateReturnLabel ? { x: -0.35, y: -12 } : null
    );
    replacements.set(edge.id, updated);
    const addedPorts = ['exitX', 'exitY', 'exitPerimeter', 'entryX', 'entryY', 'entryPerimeter']
      .filter((key) => edge.styleMap?.[key] === undefined);
    if (updated !== originals.get(edge.id)) {
      changes.push({
        edge_id: edge.id,
        added_or_completed_ports: addedPorts,
        source_port: ports.sourcePort,
        target_port: ports.targetPort,
        rerouted: Boolean(localWaypoints),
        self_loop: selfLoop,
        route_triggers: reasons,
        waypoint_count: localWaypoints?.length ?? edge.geometry?.points?.length ?? 0
      });
    }
    if (!governedReturn) {
      const parent = context.byId.get(edge.parent);
      const parentOrigin = parent ? absoluteOrigin(parent, context.byId, context.originCache) : { x: 0, y: 0 };
      const waypoints = localWaypoints
        ? localWaypoints.map((point) => ({ x: point.x + parentOrigin.x, y: point.y + parentOrigin.y }))
        : (edge.geometry?.points || []).map((point) => ({ x: numeric(point.x) + parentOrigin.x, y: numeric(point.y) + parentOrigin.y }));
      addOccupied(edge, [start, ...waypoints, end]);
    }
  }

  const candidate = replaceCells(membership.xml, replacements);
  const after = auditDrawio(candidate, { strictPorts: true });
  const candidateCells = cellXmlById(candidate);
  const changedCellIds = [...sourceCells.keys()].filter((id) => sourceCells.get(id) !== candidateCells.get(id));
  const businessEdgeIds = new Set(context.businessEdges.map((edge) => edge.id));
  const membershipNodeIds = new Set(membership.nodeChanges.map((item) => item.node_id));
  const membershipContainerIds = new Set(membership.containerChanges.map((item) => item.container_id));
  const unexpectedChangedCells = changedCellIds.filter((id) => !businessEdgeIds.has(id)
    && !membershipNodeIds.has(id)
    && !membershipContainerIds.has(id));
  const remainingSemantic = after.issues.filter((item) => ['decision_branch_without_label', 'decision_missing_branch'].includes(item.code));
  return {
    xml: candidate,
    report: {
      source_hash: contentHash(xml),
      candidate_hash: contentHash(candidate),
      source_modified: false,
      changed_edges: changes.length,
      changed_cell_ids: changedCellIds,
      unexpected_changed_cells: unexpectedChangedCells,
      node_cells_changed: changedCellIds.filter((id) => context.logicalNodes.some((node) => node.id === id)).length,
      decorative_edges_changed: changedCellIds.filter((id) => !businessEdgeIds.has(id)
        && !membershipNodeIds.has(id)
        && !membershipContainerIds.has(id)).length,
      container_membership: {
        changed_nodes: membership.nodeChanges,
        changed_edges: membership.edgeChanges,
        expanded_containers: membership.containerChanges
      },
      changes,
      unresolved: [...unresolved, ...remainingSemantic.map((item) => ({ edge_id: item.cell_id, reason: item.code, decision_id: item.decision_id || null }))],
      before: { status: before.status, errors: before.errors, warnings: before.warnings, issues: before.issues },
      after: { status: after.status, errors: after.errors, warnings: after.warnings, issues: after.issues }
    }
  };
}
