import { contentHash, isContainer, isText, parseDrawio } from './drawio.mjs';

export const DRAWIO_THEME = Object.freeze({
  title: { fillColor: '#F2F6F8', strokeColor: '#D0D0D0', fontColor: '#1F2329' },
  entry: { fillColor: '#E8F4FD', strokeColor: '#4A90D9', fontColor: '#2C5F8A' },
  action: { fillColor: '#FFFFFF', strokeColor: '#D0D0D0', fontColor: '#333333' },
  processed: { fillColor: '#F5F5F5', strokeColor: '#BDBDBD', fontColor: '#616161' },
  decision: { fillColor: '#FFF8E1', strokeColor: '#E6B800', fontColor: '#5D4E00' },
  async: { fillColor: '#F5F5F5', strokeColor: '#BDBDBD', fontColor: '#616161' },
  success: { fillColor: '#E8F8E8', strokeColor: '#5CB85C', fontColor: '#3D7A3D' },
  exception: { fillColor: '#F2F6F8', strokeColor: '#C64B4B', fontColor: '#A33A3A' },
  neutral: { fillColor: '#F5F5F5', strokeColor: '#BDBDBD', fontColor: '#616161' },
  line: { strokeColor: '#666666', fontColor: '#1F2329' },
  data: { strokeColor: '#999999', fontColor: '#1F2329' },
  lane: { fillColor: '#F5F5F5', strokeColor: '#E0E0E0', fontColor: '#333333' },
  background: { fillColor: '#FFFFFF', strokeColor: '#E0E0E0' },
  legend: { fillColor: '#FAFAFA', strokeColor: '#E0E0E0', fontColor: '#1F2329' }
});

const COLOR_FAMILIES = {
  brand: new Set(['#861B2F', '#6A1525', '#A61D3D', '#2C3E50', '#1A252F']),
  system: new Set(['#E3F2FD', '#1565C0', '#E6F7FF', '#1890FF', '#E6F4FF', '#1677FF', '#0958D9', '#E8F4FD', '#4A90D9', '#2C5F8A']),
  external: new Set(['#F3E5F5', '#7B1FA2', '#4A148C', '#F9F0FF', '#722ED1', '#531DAB']),
  decision: new Set(['#FFF7E6', '#FAAD14', '#D48806', '#AD6800', '#874D00', '#FFFBE6', '#D4B106', '#7C5C00', '#FFF8E1', '#E6B800', '#5D4E00']),
  success: new Set(['#F6FFED', '#52C41A', '#389E0D', '#237804', '#E8F8E8', '#5CB85C', '#3D7A3D']),
  exception: new Set(['#FFF1F0', '#FF4D4F', '#CF1322', '#A8071A', '#C64B4B', '#A33A3A']),
  neutral: new Set(['#FFFFFF', '#FAFAFA', '#F5F5F5', '#F2F6F8', '#BFBFBF', '#BDBDBD', '#999999', '#8C8C8C', '#666666', '#616161', '#595959', '#333333', '#1F2329', '#1A1A1A', '#E0E0E0', '#E5E7EB', '#D9D9D9', '#D0D0D0'])
};

const ALLOWED_COLORS = new Set(Object.values(DRAWIO_THEME).flatMap((entry) => Object.values(entry)));

function normalizeHex(value) {
  if (!/^#[0-9a-f]{6}$/i.test(value || '')) return value;
  return value.toUpperCase();
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
    if (value === undefined) continue;
    if (!parsed.values.has(key)) parsed.order.push(key);
    parsed.values.set(key, String(value));
  }
  return `${parsed.order.map((key) => parsed.values.get(key) === null ? key : `${key}=${parsed.values.get(key)}`).join(';')};`;
}

function familyOf(cell) {
  const colors = ['fillColor', 'strokeColor', 'fontColor']
    .map((key) => normalizeHex(cell.styleMap?.[key]))
    .filter(Boolean);
  for (const family of ['exception', 'external', 'system', 'success', 'decision', 'brand', 'neutral']) {
    if (colors.some((color) => COLOR_FAMILIES[family].has(color))) return family;
  }
  return 'neutral';
}

function compactValue(cell) {
  return String(cell.value || '').replace(/\s+/g, '');
}

function edgeRole(cell) {
  const value = compactValue(cell);
  const family = familyOf(cell);
  if (cell.styleMap?.dashed === '1' || family === 'decision' || /同步|回传|通知|监控/.test(value)) return 'data';
  return 'line';
}

function vertexRole(cell) {
  const value = compactValue(cell);
  const family = familyOf(cell);
  if (cell.id === 'bg') return 'background';
  if (cell.id === 'title-bg') return 'title';
  if (cell.id === 'legend-box') return 'legend';
  if (cell.id === 'title') return 'titleText';
  if (isContainer(cell)) return 'lane';
  if (cell.styleMap?.rhombus === true || cell.styleMap?.rhombus === '1') return 'decision';
  if (/^(开始|起点)$/.test(value) || /^(进入|发起|车辆入库)/.test(value)) return 'entry';
  if (/^(结束|终点|完成|已生效|判断可交车)$/.test(value)) return 'success';
  if (/^(取消|已失效|草稿)$/.test(value)) return 'neutral';
  if (/已驳回|已退款/.test(value)) return 'exception';
  if (/待审核|待处理|待确认/.test(value)) return 'decision';
  if (/同步|监控|回传/.test(value)) return 'async';
  if (family === 'exception') return 'exception';
  if (family === 'external') return 'processed';
  if (family === 'system') return normalizeHex(cell.styleMap?.fillColor) === '#FFFFFF' ? 'action' : 'processed';
  if (family === 'success') return 'success';
  if (family === 'decision') return cell.styleMap?.dashed === '1' ? 'async' : 'decision';
  if (family === 'brand') return normalizeHex(cell.styleMap?.fillColor) === '#FFFFFF' ? 'action' : 'processed';
  return normalizeHex(cell.styleMap?.fillColor) === '#FFFFFF' ? 'action' : 'processed';
}

function textColor(cell) {
  const family = familyOf(cell);
  if (cell.id === 'title') return DRAWIO_THEME.title.fontColor;
  if (family === 'exception') return DRAWIO_THEME.exception.fontColor;
  if (family === 'success') return DRAWIO_THEME.success.fontColor;
  if (family === 'decision') return DRAWIO_THEME.decision.fontColor;
  if (family === 'system') return DRAWIO_THEME.entry.fontColor;
  return DRAWIO_THEME.line.fontColor;
}

function themedStyle(cell) {
  if (!cell.style) return null;
  if (cell.edge === '1') {
    const role = edgeRole(cell);
    const theme = DRAWIO_THEME[role];
    const additions = { strokeColor: theme.strokeColor };
    if (cell.value || cell.styleMap?.fontColor) additions.fontColor = theme.fontColor || theme.strokeColor;
    if (role === 'data') additions.dashed = 1;
    return { style: setStyleValues(cell.style, additions), role: `edge:${role}` };
  }
  if (isText(cell)) return { style: setStyleValues(cell.style, { fontColor: textColor(cell) }), role: 'text' };
  const role = vertexRole(cell);
  if (role === 'titleText') return { style: setStyleValues(cell.style, { fontColor: DRAWIO_THEME.title.fontColor }), role };
  if (role === 'lane') {
    const laneTheme = DRAWIO_THEME.lane;
    return {
      style: setStyleValues(cell.style, {
        fillColor: laneTheme.fillColor,
        strokeColor: laneTheme.strokeColor,
        fontColor: laneTheme.fontColor,
        swimlaneFillColor: '#FFFFFF'
      }),
      role
    };
  }
  const theme = DRAWIO_THEME[role] || DRAWIO_THEME.neutral;
  return { style: setStyleValues(cell.style, theme), role };
}

function escapeAttribute(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function replaceStyle(cellXml, style) {
  const opening = cellXml.match(/^<mxCell\b[^>]*>/)?.[0];
  if (!opening) return cellXml;
  const attribute = ` style="${escapeAttribute(style)}"`;
  const updated = /\sstyle="[^"]*"/.test(opening)
    ? opening.replace(/\sstyle="[^"]*"/, attribute)
    : opening.replace(/>$/, `${attribute}>`);
  return updated + cellXml.slice(opening.length);
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

function explicitColors(xml) {
  const counts = {};
  for (const match of xml.matchAll(/(?:fillColor|strokeColor|fontColor)=(#[0-9a-f]{6})/gi)) {
    const color = normalizeHex(match[1]);
    counts[color] = (counts[color] || 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort(([left], [right]) => left.localeCompare(right)));
}

export function auditDrawioTheme(xml) {
  const colors = explicitColors(xml);
  const unsupported = Object.keys(colors).filter((color) => !ALLOWED_COLORS.has(color));
  return { status: unsupported.length ? 'fail' : 'pass', colors, unsupported_colors: unsupported };
}

export function normalizeDrawioTheme(xml) {
  const graph = parseDrawio(xml);
  const originals = cellsById(xml);
  const replacements = new Map();
  const roleCounts = {};
  for (const cell of graph.cells) {
    if (!cell.id || !originals.has(cell.id) || !cell.style) continue;
    const result = themedStyle(cell);
    if (!result?.style) continue;
    roleCounts[result.role] = (roleCounts[result.role] || 0) + 1;
    const updated = replaceStyle(originals.get(cell.id), result.style);
    if (updated !== originals.get(cell.id)) replacements.set(cell.id, updated);
  }
  const pattern = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  const candidate = xml.replace(pattern, (cellXml, attrFragment) => {
    const id = attrFragment.match(/\bid="([^"]*)"/)?.[1];
    return replacements.get(id) || cellXml;
  });
  const after = auditDrawioTheme(candidate);
  const graphById = new Map(graph.cells.map((cell) => [cell.id, cell]));
  const changedIds = [...replacements.keys()];
  return {
    xml: candidate,
    report: {
      source_hash: contentHash(xml),
      candidate_hash: contentHash(candidate),
      changed_cells: changedIds.length,
      changed_vertices: changedIds.filter((id) => graphById.get(id)?.vertex === '1').length,
      changed_edges: changedIds.filter((id) => graphById.get(id)?.edge === '1').length,
      roles: Object.fromEntries(Object.entries(roleCounts).sort(([left], [right]) => left.localeCompare(right))),
      colors_before: explicitColors(xml),
      colors_after: after.colors,
      unsupported_colors: after.unsupported_colors,
      status: after.status
    }
  };
}
