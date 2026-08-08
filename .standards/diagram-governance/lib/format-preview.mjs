import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { contentHash } from './drawio.mjs';
import { parseExcalidrawMarkdown, semanticFromExcalidraw } from './excalidraw.mjs';
import { semanticFromMermaid } from './mermaid.mjs';
import { cdpClient, chromeExecutable, inspectPng, pageTarget } from './preview.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const toolRoot = path.resolve(here, '..');

function safeScript(source) { return source.replace(/<\/script/gi, '<\\/script'); }
function normalized(value) { return String(value || '').replace(/\s+/g, '').toLowerCase(); }

function mermaidHtml(source) {
  const runtime = fs.readFileSync(path.join(toolRoot, 'node_modules', 'mermaid', 'dist', 'mermaid.min.js'), 'utf8');
  return `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff}body{display:grid;place-items:center}#diagram{width:96vw;height:94vh;display:grid;place-items:center}svg{max-width:96vw!important;max-height:94vh!important;width:auto!important;height:auto!important}</style></head><body><div id="diagram"></div><script>${safeScript(runtime)}</script><script>
window.__dgDone=false;window.__dgError='';
(async()=>{try{mermaid.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true});const out=await mermaid.render('dg-diagram',${JSON.stringify(source)});document.getElementById('diagram').innerHTML=out.svg;window.__dgDone=true;}catch(e){window.__dgError=String(e&&e.message||e);window.__dgDone=true;}})();
</script></body></html>`;
}

async function excalidrawHtml(runtimeDir, scene) {
  const entry = path.join(runtimeDir, 'excalidraw-entry.js');
  const bundle = path.join(runtimeDir, 'excalidraw-render.js');
  fs.writeFileSync(entry, `import { exportToSvg, restoreElements } from '@excalidraw/excalidraw';
window.addEventListener('load', async()=>{try{const scene=window.__dgScene;const elements=restoreElements(scene.elements||[],null,{refreshDimensions:false,repairBindings:true});const svg=await exportToSvg({elements,appState:{...(scene.appState||{}),exportBackground:true,viewBackgroundColor:'#ffffff',exportPadding:24},files:scene.files||{},exportPadding:24});document.getElementById('diagram').appendChild(svg);window.__dgDone=true;}catch(e){window.__dgError=String(e&&e.stack||e);window.__dgDone=true;}});`, 'utf8');
  await build({ entryPoints: [entry], outfile: bundle, bundle: true, platform: 'browser', format: 'iife', sourcemap: false, logLevel: 'silent', nodePaths: [path.join(toolRoot, 'node_modules')], define: { 'process.env.NODE_ENV': '"production"' } });
  const assetPath = `${pathToFileURL(path.join(toolRoot, 'node_modules', '@excalidraw', 'excalidraw', 'dist', 'prod')).href}/`;
  return `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff}body{display:grid;place-items:center}#diagram{width:96vw;height:94vh;display:grid;place-items:center}svg{max-width:96vw!important;max-height:94vh!important;width:auto!important;height:auto!important}</style></head><body><div id="diagram"></div><script>window.EXCALIDRAW_ASSET_PATH=${JSON.stringify(assetPath)};window.__dgScene=${safeScript(JSON.stringify(scene))};window.__dgDone=false;window.__dgError='';</script><script src="${pathToFileURL(bundle).href}"></script></body></html>`;
}

function labels(format, source) {
  const graph = format === 'mermaid' ? semanticFromMermaid(source) : semanticFromExcalidraw(source);
  return graph.labels.filter((label) => label.length >= 2).slice(0, 20);
}

async function renderWithChrome(page, png, svgFile, expected, options = {}) {
  const chrome = chromeExecutable(options.chrome);
  const width = options.width || 1800; const height = options.height || 1000;
  const runtime = path.dirname(page);
  const profile = path.join(runtime, 'chrome');
  const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--metrics-recording-only', '--disable-default-apps', '--disable-extensions', '--allow-file-access-from-files', '--disable-web-security', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', `--window-size=${width},${height}`, '--force-device-scale-factor=1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'pipe', 'pipe'] });
  const stderr = { value: '' }; child.stderr.on('data', (chunk) => { stderr.value += chunk.toString(); }); child.stdout.on('data', (chunk) => { stderr.value += chunk.toString(); });
  const deadline = Date.now() + (options.timeout || 90000); let client;
  try {
    let browserUrl;
    while (Date.now() < deadline) {
      const advertised = stderr.value.match(/DevTools listening on (ws:\/\/[^\s]+)/)?.[1];
      if (advertised) { browserUrl = advertised; break; }
      const portFile = path.join(profile, 'DevToolsActivePort');
      if (fs.existsSync(portFile)) {
        const [port, socketPath] = fs.readFileSync(portFile, 'utf8').trim().split(/\r?\n/);
        if (port && socketPath) { browserUrl = `ws://127.0.0.1:${port}${socketPath}`; break; }
      }
      if (child.exitCode !== null) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!browserUrl) throw new Error(`preview_backend_failed: DevTools endpoint unavailable; exit=${child.exitCode}; output=${stderr.value.trim()}`);
    const target = await pageTarget(browserUrl, deadline); client = await cdpClient(target.webSocketDebuggerUrl);
    await client.send('Page.enable'); await client.send('Runtime.enable');
    await client.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    await client.send('Page.navigate', { url: pathToFileURL(page).href });
    let evidence;
    while (Date.now() < deadline) {
      const result = await client.send('Runtime.evaluate', { expression: `({done:window.__dgDone,error:window.__dgError||'',svg_count:document.querySelectorAll('svg').length,text:(document.body&&document.body.innerText||'')+' '+Array.from(document.querySelectorAll('svg text')).map(x=>x.textContent||'').join(' ')})`, returnByValue: true });
      evidence = result.result?.value || {};
      if (evidence.done) break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    if (!evidence?.done) throw new Error('format_renderer_timeout');
    if (evidence.error) throw new Error(`format_renderer_error: ${evidence.error}`);
    if (!evidence.svg_count) throw new Error('format_renderer_svg_missing');
    const haystack = normalized(evidence.text);
    const matched = expected.filter((label) => haystack.includes(normalized(label)));
    if (expected.length && !matched.length) throw new Error('format_renderer_source_labels_missing');
    const svg = await client.send('Runtime.evaluate', { expression: `document.querySelector('#diagram svg').outerHTML`, returnByValue: true });
    fs.writeFileSync(svgFile, svg.result?.value || '', 'utf8');
    const screenshot = await client.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false });
    fs.writeFileSync(png, Buffer.from(screenshot.data, 'base64'));
    return { status: 'pass', svg_count: evidence.svg_count, matched_labels: matched, chrome };
  } finally {
    if (client) client.close(); if (child.exitCode === null) child.kill('SIGKILL');
  }
}

export async function captureFormatPreview(format, sourcePath, root, options = {}) {
  if (!['mermaid', 'excalidraw'].includes(format)) throw new Error(`unsupported_preview_format: ${format}`);
  const source = fs.readFileSync(sourcePath, 'utf8');
  const fingerprint = contentHash(source);
  const bundleId = `${new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')}_${fingerprint.slice(0, 8)}_quick`;
  const bundleDir = path.join(root, format, 'quick', bundleId); const artifacts = path.join(bundleDir, 'artifacts');
  fs.mkdirSync(artifacts, { recursive: true });
  const runtime = fs.mkdtempSync(path.join(os.tmpdir(), `diagram-governance-${format}-`));
  const page = path.join(runtime, 'index.html'); const png = path.join(artifacts, 'hero.png'); const svg = path.join(artifacts, 'diagram.svg');
  try {
    let diagramSource = source;
    if (format === 'excalidraw') {
      const scene = parseExcalidrawMarkdown(source).scene;
      fs.writeFileSync(page, await excalidrawHtml(runtime, scene), 'utf8');
    } else fs.writeFileSync(page, mermaidHtml(diagramSource), 'utf8');
    const evidence = await renderWithChrome(page, png, svg, labels(format, diagramSource), options);
    const inspection = inspectPng(png);
    if (inspection.status !== 'pass') throw new Error(`preview_validation_failed: ${inspection.issues.join(',')}`);
    const renderer = format === 'mermaid' ? 'npm:mermaid@11.16.0' : 'npm:@excalidraw/excalidraw@0.18.1';
    const manifest = {
      protocol_version: 'preview-bundle/v1', bundle_id: bundleId, bundle_kind: 'capture', software: format, recipe: 'quick', status: 'ok', created_at: new Date().toISOString(),
      generator: { entry_point: 'diagram-governance', harness_version: '0.6.0', backend: evidence.chrome, renderer },
      source: { project_path: path.resolve(sourcePath), project_fingerprint: `sha256:${fingerprint}` },
      metrics: inspection, native_viewer_evidence: evidence,
      artifacts: [
        { artifact_id: 'hero', role: 'hero', kind: 'image', media_type: 'image/png', path: 'artifacts/hero.png', width: inspection.width, height: inspection.height, bytes: inspection.bytes },
        { artifact_id: 'svg', role: 'source-render', kind: 'vector', media_type: 'image/svg+xml', path: 'artifacts/diagram.svg', bytes: fs.statSync(svg).size }
      ]
    };
    fs.writeFileSync(path.join(bundleDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    return { status: 'ok', cached: false, bundle_dir: bundleDir, manifest_path: path.join(bundleDir, 'manifest.json'), inspection, manifest };
  } finally { fs.rmSync(runtime, { recursive: true, force: true }); }
}
