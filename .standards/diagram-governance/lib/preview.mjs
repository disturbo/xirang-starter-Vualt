import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import zlib from 'node:zlib';
import { contentHash } from './drawio.mjs';

export const PREVIEW_RECIPES = Object.freeze({
  quick: {
    description: 'Real viewer.diagrams.net lightbox capture through headless Google Chrome',
    width: 2200,
    height: 900,
    artifact_role: 'hero'
  }
});

const BROWSER_ERROR_PATTERNS = [
  /ERR_[A-Z_]+/i,
  /This site can.t be reached/i,
  /Unable to access this website/i,
  /无法访问此网站/,
  /网页无法打开/,
  /Aw, Snap!/i
];

function utcCompact(date = new Date()) {
  return date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
}

export function chromeExecutable(explicit) {
  const candidates = [
    explicit,
    process.env.DRAWIO_CHROME,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    'google-chrome',
    'chromium'
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate.includes(path.sep) && fs.existsSync(candidate)) return candidate;
    if (!candidate.includes(path.sep)) {
      const probe = spawnSync('/usr/bin/env', ['which', candidate], { encoding: 'utf8' });
      if (probe.status === 0 && probe.stdout.trim()) return probe.stdout.trim();
    }
  }
  throw new Error('real_preview_backend_missing: install Google Chrome or set DRAWIO_CHROME');
}

function dimensionsAndPixels(png) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (png.length < 33 || !png.subarray(0, 8).equals(signature)) throw new Error('invalid_png_signature');
  let offset = 8;
  let width;
  let height;
  let bitDepth;
  let colorType;
  let interlace;
  const idat = [];
  while (offset + 12 <= png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.toString('ascii', offset + 4, offset + 8);
    const data = png.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === 'IDAT') idat.push(data);
    offset += 12 + length;
    if (type === 'IEND') break;
  }
  if (!width || !height || bitDepth !== 8 || ![2, 6].includes(colorType) || interlace !== 0) {
    throw new Error(`unsupported_png_format: ${width}x${height} depth=${bitDepth} color=${colorType} interlace=${interlace}`);
  }
  const bytesPerPixel = colorType === 6 ? 4 : 3;
  const rowBytes = width * bytesPerPixel;
  const inflated = zlib.inflateSync(Buffer.concat(idat));
  const previous = Buffer.alloc(rowBytes);
  const current = Buffer.alloc(rowBytes);
  let cursor = 0;
  let samples = 0;
  let sum = 0;
  let sumSquares = 0;
  let dark = 0;
  const paeth = (a, b, c) => {
    const p = a + b - c;
    const pa = Math.abs(p - a);
    const pb = Math.abs(p - b);
    const pc = Math.abs(p - c);
    return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
  };
  for (let y = 0; y < height; y += 1) {
    const filter = inflated[cursor++];
    for (let index = 0; index < rowBytes; index += 1) {
      const raw = inflated[cursor++];
      const left = index >= bytesPerPixel ? current[index - bytesPerPixel] : 0;
      const up = previous[index];
      const upperLeft = index >= bytesPerPixel ? previous[index - bytesPerPixel] : 0;
      if (filter === 0) current[index] = raw;
      else if (filter === 1) current[index] = (raw + left) & 255;
      else if (filter === 2) current[index] = (raw + up) & 255;
      else if (filter === 3) current[index] = (raw + Math.floor((left + up) / 2)) & 255;
      else if (filter === 4) current[index] = (raw + paeth(left, up, upperLeft)) & 255;
      else throw new Error(`unsupported_png_filter: ${filter}`);
    }
    if (y % 3 === 0) {
      for (let x = 0; x < width; x += 3) {
        const index = x * bytesPerPixel;
        const luminance = current[index] * 0.2126 + current[index + 1] * 0.7152 + current[index + 2] * 0.0722;
        samples += 1;
        sum += luminance;
        sumSquares += luminance * luminance;
        if (luminance < 235) dark += 1;
      }
    }
    current.copy(previous);
  }
  const mean = sum / samples;
  const variance = sumSquares / samples - mean * mean;
  return { width, height, mean, variance, darkFraction: dark / samples };
}

export function inspectPng(file) {
  const buffer = fs.readFileSync(file);
  const pixels = dimensionsAndPixels(buffer);
  const issues = [];
  if (buffer.length < 15000) issues.push('suspiciously-small-png');
  if (pixels.variance < 2 || pixels.darkFraction < 0.001) issues.push('blank-or-near-blank-render');
  return {
    status: issues.length ? 'fail' : 'pass',
    bytes: buffer.length,
    width: pixels.width,
    height: pixels.height,
    luminance_mean: Math.round(pixels.mean * 10) / 10,
    luminance_variance: Math.round(pixels.variance * 10) / 10,
    dark_fraction: Math.round(pixels.darkFraction * 10000) / 10000,
    issues
  };
}

function decodeXmlLabel(value) {
  return value
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, '&')
    .replace(/<br\s*\/?\s*>/gi, ' ').replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ').replace(/\s+/g, ' ').trim();
}

export function expectedDiagramLabels(xml) {
  const labels = [];
  for (const match of xml.matchAll(/\bvalue="([^"]+)"/g)) {
    const label = decodeXmlLabel(match[1]);
    if (label.length >= 2 && label.length <= 80 && !labels.includes(label)) labels.push(label);
  }
  return labels;
}

function normalized(value) {
  return String(value || '').replace(/\s+/g, '').toLowerCase();
}

export function validateNativeViewerEvidence(evidence, expectedLabels, options = {}) {
  const visible = `${evidence.body_text || ''}\n${evidence.svg_text || ''}`;
  const issues = [];
  if (BROWSER_ERROR_PATTERNS.some((pattern) => pattern.test(`${evidence.title || ''}\n${visible}`))) {
    issues.push('browser-error-page');
  }
  if (options.renderer !== 'offline'
    && !String(evidence.href || '').startsWith('https://viewer.diagrams.net/')) issues.push('unexpected-viewer-origin');
  // Draw.io may render labels as SVG <text> nodes or as HTML inside foreignObject.
  // SVG existence plus a source-label match is the stable cross-diagram invariant.
  if ((evidence.svg_count || 0) < 1) issues.push('drawio-svg-missing');
  const haystack = normalized(visible);
  const matchedLabels = expectedLabels.filter((label) => haystack.includes(normalized(label)));
  if (!matchedLabels.length) issues.push('source-labels-not-rendered');
  return {
    status: issues.length ? 'fail' : 'pass',
    issues,
    matched_labels: matchedLabels.slice(0, 10),
    svg_count: evidence.svg_count || 0,
    svg_text_count: evidence.svg_text_count || 0,
    href: evidence.href || '',
    title: evidence.title || ''
  };
}

export async function waitForDevtools(child, stderrState, deadline) {
  while (Date.now() < deadline) {
    const match = stderrState.value.match(/DevTools listening on (ws:\/\/[^\s]+)/);
    if (match) return match[1];
    if (child.exitCode !== null) break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`preview_backend_failed: DevTools endpoint unavailable; exit=${child.exitCode}; stderr=${stderrState.value.trim()}`);
}

export async function pageTarget(browserWebSocketUrl, deadline) {
  const endpoint = new URL(browserWebSocketUrl);
  const listUrl = `http://${endpoint.host}/json/list`;
  while (Date.now() < deadline) {
    try {
      const targets = await fetch(listUrl).then((response) => response.json());
      const target = targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) return target;
    } catch {
      // Chrome may advertise DevTools before the target endpoint is ready.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('preview_backend_failed: Chrome page target unavailable');
}

export async function cdpClient(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(`cdp_error: ${message.error.message}`));
    else resolve(message.result || {});
  });
  return {
    send(method, params = {}) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() { socket.close(); }
  };
}

function findOfflineDrawioPlugin(sourcePath, explicit) {
  if (explicit && fs.existsSync(explicit)) return explicit;
  let current = path.dirname(path.resolve(sourcePath));
  while (true) {
    const candidate = path.join(current, '.obsidian', 'plugins', 'drawio-obsidian', 'main.js');
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function extractOfflineDrawioScripts(pluginFile) {
  const source = fs.readFileSync(pluginFile, 'utf8');
  const marker = 'this.addScriptToFrame(';
  const scripts = [];
  let cursor = 0;
  while (scripts.length < 3) {
    const markerIndex = source.indexOf(marker, cursor);
    if (markerIndex < 0) break;
    const start = markerIndex + marker.length;
    const quote = source[start];
    if (!['\'', '"', '`'].includes(quote)) {
      cursor = start + 1;
      continue;
    }
    let escaped = false;
    let end = start + 1;
    for (; end < source.length; end += 1) {
      const character = source[end];
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === quote) break;
    }
    if (end >= source.length) throw new Error('offline_renderer_invalid: unterminated embedded Draw.io script');
    const literal = source.slice(start, end + 1);
    scripts.push(Function(`"use strict"; return (${literal});`)());
    cursor = end + 1;
  }
  if (scripts.length !== 3 || scripts[1].length < 1000000) {
    throw new Error('offline_renderer_invalid: expected Draw.io plugin runtime was not found');
  }
  return scripts;
}

function offlineDrawioHtml(pluginFile, xml) {
  const scripts = extractOfflineDrawioScripts(pluginFile);
  const safe = (script) => script.replace(/<\/script/gi, '<\\/script');
  const bootstrap = `
window.__diagramGovernanceXml = ${JSON.stringify(xml)};
window.__diagramGovernanceEvents = [];
window.__diagramGovernanceErrors = [];
window.addEventListener('error', (event) => window.__diagramGovernanceErrors.push(event.message || String(event.error || 'window-error')));
window.addEventListener('unhandledrejection', (event) => window.__diagramGovernanceErrors.push(String(event.reason || 'unhandled-rejection')));
window.addEventListener('message', (event) => {
  try {
    const message = JSON.parse(event.data);
    window.__diagramGovernanceEvents.push(message.event || message.action || 'unknown');
    if (message.event === 'init' && !window.__diagramGovernanceLoaded) {
      window.__diagramGovernanceLoaded = true;
      setTimeout(() => window.postMessage(JSON.stringify({ action: 'load', xml: window.__diagramGovernanceXml }), '*'), 0);
    }
  } catch {}
});`;
  const loader = `
const __diagramGovernanceScripts = ${JSON.stringify(scripts)};
window.addEventListener('load', () => {
  const inject = (source) => {
    const element = document.createElement('script');
    element.text = source;
    document.head.appendChild(element);
  };
  inject(__diagramGovernanceScripts[0]);
  window.postMessage(JSON.stringify({ action: 'frame-config', settings: { theme: { dark: false, layout: 'full' }, drawing: { sketch: false } } }), '*');
  inject(__diagramGovernanceScripts[1]);
  inject(__diagramGovernanceScripts[2]);
});`;
  return `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff}</style></head><body><script>${safe(bootstrap)}\n${safe(loader)}</script></body></html>`;
}

async function captureNativeOffline(chrome, pluginFile, xml, stagedHero, runtime, recipeSpec, labels, timeout) {
  const offlinePage = path.join(runtime, 'offline-drawio.html');
  fs.writeFileSync(offlinePage, offlineDrawioHtml(pluginFile, xml), 'utf8');
  const args = [
    '--headless', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--metrics-recording-only',
    '--disable-default-apps', '--disable-extensions', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    `--window-size=${recipeSpec.width},${recipeSpec.height}`, '--force-device-scale-factor=1',
    `--user-data-dir=${runtime}`, pathToFileURL(offlinePage).href
  ];
  const child = spawn(chrome, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  const stderrState = { value: '' };
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
  child.stderr.on('data', (chunk) => { stderrState.value += chunk.toString(); });
  const deadline = Date.now() + timeout;
  let client;
  try {
    const browserWebSocketUrl = await waitForDevtools(child, stderrState, deadline);
    const target = await pageTarget(browserWebSocketUrl, deadline);
    client = await cdpClient(target.webSocketDebuggerUrl);
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: recipeSpec.width, height: recipeSpec.height, deviceScaleFactor: 1, mobile: false
    });
    let evidence;
    let nativeValidation;
    while (Date.now() < deadline) {
      const evaluated = await client.send('Runtime.evaluate', {
        expression: `(() => ({
          ready_state: document.readyState,
          href: location.href,
          title: document.title,
          body_text: document.body ? document.body.innerText : '',
          svg_count: document.querySelectorAll('svg').length,
          svg_text_count: document.querySelectorAll('svg text').length,
          svg_text: Array.from(document.querySelectorAll('svg text')).map((node) => node.textContent || '').join('\\n'),
          events: window.__diagramGovernanceEvents || [],
          errors: window.__diagramGovernanceErrors || []
        }))()`,
        returnByValue: true
      });
      evidence = evaluated.result?.value || {};
      nativeValidation = validateNativeViewerEvidence(evidence, labels, { renderer: 'offline' });
      if (nativeValidation.status === 'pass') break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!nativeValidation || nativeValidation.status !== 'pass') {
      throw new Error(`offline_renderer_validation_failed: ${(nativeValidation?.issues || ['renderer-timeout']).join(',')}; events=${(evidence?.events || []).join(',')}; errors=${(evidence?.errors || []).slice(0, 3).join(' | ')}`);
    }
    await client.send('Runtime.evaluate', {
      expression: `(() => { try { window.drawioApp.editor.graph.fit(16); return true; } catch { return false; } })()`,
      returnByValue: true
    });
    await new Promise((resolve) => setTimeout(resolve, 500));
    const screenshot = await client.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false });
    fs.writeFileSync(stagedHero, Buffer.from(screenshot.data, 'base64'));
    return { nativeValidation, stdout, stderr: stderrState.value };
  } finally {
    if (client) client.close();
    if (child.exitCode === null) child.kill('SIGKILL');
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

async function captureNativeViewer(chrome, url, stagedHero, runtime, recipeSpec, labels, timeout) {
  const args = [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--metrics-recording-only',
    '--disable-default-apps', '--disable-extensions', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    `--window-size=${recipeSpec.width},${recipeSpec.height}`, '--force-device-scale-factor=1',
    `--user-data-dir=${runtime}`, 'about:blank'
  ];
  const child = spawn(chrome, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  const stderrState = { value: '' };
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
  child.stderr.on('data', (chunk) => { stderrState.value += chunk.toString(); });
  const deadline = Date.now() + timeout;
  let client;
  try {
    const browserWebSocketUrl = await waitForDevtools(child, stderrState, deadline);
    const target = await pageTarget(browserWebSocketUrl, deadline);
    client = await cdpClient(target.webSocketDebuggerUrl);
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: recipeSpec.width, height: recipeSpec.height, deviceScaleFactor: 1, mobile: false
    });
    await client.send('Page.navigate', { url });
    let evidence;
    let nativeValidation;
    while (Date.now() < deadline) {
      const evaluated = await client.send('Runtime.evaluate', {
        expression: `(() => ({
          ready_state: document.readyState,
          href: location.href,
          title: document.title,
          body_text: document.body ? document.body.innerText : '',
          svg_count: document.querySelectorAll('svg').length,
          svg_text_count: document.querySelectorAll('svg text').length,
          svg_text: Array.from(document.querySelectorAll('svg text')).map((node) => node.textContent || '').join('\\n')
        }))()`,
        returnByValue: true
      });
      evidence = evaluated.result?.value || {};
      nativeValidation = validateNativeViewerEvidence(evidence, labels);
      if (evidence.ready_state === 'complete' && nativeValidation.status === 'pass') break;
      if (nativeValidation.issues.includes('browser-error-page')) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    if (!nativeValidation || nativeValidation.status !== 'pass') {
      throw new Error(`native_viewer_validation_failed: ${(nativeValidation?.issues || ['viewer-timeout']).join(',')}`);
    }
    const screenshot = await client.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false });
    fs.writeFileSync(stagedHero, Buffer.from(screenshot.data, 'base64'));
    return { nativeValidation, stdout, stderr: stderrState.value };
  } finally {
    if (client) client.close();
    if (child.exitCode === null) child.kill('SIGKILL');
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function recipeRoot(root, recipe) {
  return path.join(root, 'drawio', recipe);
}

function manifests(root, recipe) {
  const base = recipeRoot(root, recipe);
  if (!fs.existsSync(base)) return [];
  return fs.readdirSync(base, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(base, entry.name, 'manifest.json'))
    .filter((file) => fs.existsSync(file))
    .sort()
    .reverse();
}

export function latestDrawioPreview(root, options = {}) {
  const recipe = options.recipe || 'quick';
  const expected = options.sourcePath ? `sha256:${contentHash(fs.readFileSync(options.sourcePath, 'utf8'))}` : null;
  for (const manifestPath of manifests(root, recipe)) {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (manifest.status !== 'ok') continue;
    const renderer = manifest.generator?.renderer || '';
    if (renderer !== 'https://viewer.diagrams.net/' && !renderer.startsWith('obsidian-plugin://drawio-obsidian/')) continue;
    if (manifest.native_viewer_evidence?.status !== 'pass') continue;
    if (expected && manifest.source.project_fingerprint !== expected) continue;
    const hero = manifest.artifacts?.find((artifact) => artifact.artifact_id === 'hero');
    if (!hero) continue;
    const heroPath = path.resolve(path.dirname(manifestPath), hero.path);
    if (!fs.existsSync(heroPath) || inspectPng(heroPath).status !== 'pass') continue;
    return { status: 'ok', bundle_dir: path.dirname(manifestPath), manifest_path: manifestPath, manifest };
  }
  return { status: 'not-found', bundle_dir: null, manifest_path: null };
}

export function selectPreviewEligible(batch) {
  return (batch.items || []).filter((item) => item.candidate_path
    && item.gates?.geometry === 'pass'
    && item.gates?.layout === 'pass'
    && item.gates?.theme === 'pass');
}

export async function captureBatchPreviews(vault, batchReportPath, root, options = {}) {
  const batch = JSON.parse(fs.readFileSync(batchReportPath, 'utf8'));
  let eligible = selectPreviewEligible(batch);
  if (options.filter) eligible = eligible.filter((item) => item.path.includes(options.filter));
  if (Number.isFinite(options.limit)) eligible = eligible.slice(0, options.limit);
  const items = [];
  for (const item of eligible) {
    const sourcePath = path.resolve(vault, item.candidate_path);
    try {
      const preview = await captureDrawioPreview(sourcePath, root, options);
      items.push({
        path: item.path,
        candidate_path: item.candidate_path,
        status: 'pass',
        semantic_gate: item.gates.semantic,
        cached: preview.cached,
        bundle_dir: preview.bundle_dir,
        manifest_path: preview.manifest_path
      });
    } catch (error) {
      items.push({ path: item.path, candidate_path: item.candidate_path, status: 'error', error: error.message });
    }
  }
  return {
    schema_version: '0.5.0',
    generated_at: new Date().toISOString(),
    source_batch_report: path.resolve(batchReportPath),
    preview_root: path.resolve(root),
    summary: {
      eligible: eligible.length,
      passed: items.filter((item) => item.status === 'pass').length,
      failed: items.filter((item) => item.status === 'error').length,
      cached: items.filter((item) => item.cached).length
    },
    items
  };
}

export async function captureDrawioPreview(sourcePath, root, options = {}) {
  const recipe = options.recipe || 'quick';
  const recipeSpec = PREVIEW_RECIPES[recipe];
  if (!recipeSpec) throw new Error(`unknown_preview_recipe: ${recipe}`);
  const xml = fs.readFileSync(sourcePath, 'utf8');
  const fingerprint = contentHash(xml);
  if (!options.force) {
    const cached = latestDrawioPreview(root, { recipe, sourcePath });
    if (cached.status === 'ok') return { ...cached, cached: true };
  }
  const chrome = chromeExecutable(options.chrome);
  // The Obsidian plugin runtime depends on Electron iframe semantics. Keep its
  // adapter opt-in until it has a dedicated Electron harness; network viewer is
  // the production backend for the plain Node CLI.
  const offlinePlugin = options.offlinePlugin ? findOfflineDrawioPlugin(sourcePath, options.offlinePlugin) : null;
  const bundleId = `${utcCompact()}_${fingerprint.slice(0, 8)}_${recipe}`;
  const bundleDir = path.join(recipeRoot(root, recipe), bundleId);
  const artifactDir = path.join(bundleDir, 'artifacts');
  fs.mkdirSync(artifactDir, { recursive: true });
  const hero = path.join(artifactDir, 'hero.png');
  const runtimeBase = fs.existsSync('/tmp') ? '/tmp' : os.tmpdir();
  const runtimeId = `${process.pid}-${Date.now()}`;
  const runtime = path.join(runtimeBase, `diagram-governance-chrome-${runtimeId}`);
  const stagedHero = path.join(runtimeBase, `diagram-governance-hero-${runtimeId}.png`);
  const labels = expectedDiagramLabels(xml);
  const started = new Date().toISOString();
  const timeout = options.timeout || 60000;
  let native;
  let lastBackendError;
  const attempts = offlinePlugin ? 1 : Math.max(1, Number(options.attempts) || 3);
  let attemptsUsed = 0;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    attemptsUsed = attempt;
    const attemptRuntime = `${runtime}-${attempt}`;
    fs.mkdirSync(attemptRuntime, { recursive: true });
    fs.rmSync(stagedHero, { force: true });
    try {
      if (offlinePlugin) {
        native = await captureNativeOffline(chrome, offlinePlugin, xml, stagedHero, attemptRuntime, recipeSpec, labels, timeout);
      } else {
        const encoded = zlib.deflateRawSync(Buffer.from(encodeURIComponent(xml), 'utf8')).toString('base64');
        const url = `https://viewer.diagrams.net/?lightbox=1&nav=1&layers=1#R${encodeURIComponent(encoded)}`;
        native = await captureNativeViewer(chrome, url, stagedHero, attemptRuntime, recipeSpec, labels, timeout);
      }
      break;
    } catch (error) {
      lastBackendError = error;
      const retryable = /browser-error-page|unexpected-viewer-origin|DevTools endpoint unavailable|page target unavailable/.test(error.message);
      if (!retryable || attempt === attempts) throw error;
      await new Promise((resolve) => setTimeout(resolve, attempt * 1500));
    } finally {
      fs.rmSync(attemptRuntime, { recursive: true, force: true });
    }
  }
  if (!native) throw lastBackendError || new Error('preview_backend_failed: native viewer unavailable');
  const finished = new Date().toISOString();
  if (!fs.existsSync(stagedHero)) {
    throw new Error(`preview_backend_failed: native viewer did not create a screenshot; stderr=${native?.stderr?.trim() || ''}; stdout=${native?.stdout?.trim() || ''}`);
  }
  const inspection = inspectPng(stagedHero);
  if (inspection.status !== 'pass') {
    fs.rmSync(stagedHero, { force: true });
    throw new Error(`preview_validation_failed: ${inspection.issues.join(',')}`);
  }
  fs.copyFileSync(stagedHero, hero);
  fs.rmSync(stagedHero, { force: true });
  const createdAt = new Date().toISOString();
  const summary = {
    headline: 'Draw.io candidate rendered successfully through a native Draw.io backend',
    facts: {
      recipe,
      resolution: `${inspection.width}x${inspection.height}`,
      bytes: inspection.bytes,
      luminance_variance: inspection.luminance_variance,
      dark_fraction: inspection.dark_fraction
    },
    warnings: [],
    next_actions: ['Review the hero image for reading order, label placement, and visual balance']
  };
  const manifest = {
    protocol_version: 'preview-bundle/v1',
    bundle_id: bundleId,
    bundle_kind: 'capture',
    software: 'drawio',
    recipe,
    status: 'ok',
    created_at: createdAt,
    generator: {
      entry_point: 'diagram-governance',
      harness_version: '0.5.0',
      backend: chrome,
      renderer: offlinePlugin ? `obsidian-plugin://drawio-obsidian/${path.resolve(offlinePlugin)}` : 'https://viewer.diagrams.net/',
      attempts: attemptsUsed,
      backend_started_at: started,
      backend_finished_at: finished,
      command: `diagram-governance preview-capture --source ${sourcePath} --recipe ${recipe}`
    },
    source: {
      project_path: path.resolve(sourcePath),
      project_fingerprint: `sha256:${fingerprint}`
    },
    metrics: inspection,
    native_viewer_evidence: native.nativeValidation,
    summary_path: 'summary.json',
    artifacts: [{
      artifact_id: 'hero',
      role: 'hero',
      kind: 'image',
      label: 'Native diagrams.net whole-diagram render',
      media_type: 'image/png',
      path: 'artifacts/hero.png',
      width: inspection.width,
      height: inspection.height,
      bytes: inspection.bytes
    }]
  };
  fs.writeFileSync(path.join(bundleDir, 'summary.json'), `${JSON.stringify(summary, null, 2)}\n`, 'utf8');
  fs.writeFileSync(path.join(bundleDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  return {
    status: 'ok',
    cached: false,
    bundle_dir: bundleDir,
    manifest_path: path.join(bundleDir, 'manifest.json'),
    summary_path: path.join(bundleDir, 'summary.json'),
    artifact_count: 1,
    inspection,
    manifest
  };
}
