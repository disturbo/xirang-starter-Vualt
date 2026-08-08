#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildVerifiedInventory, summarizeIssues } from './lib/inventory.mjs';
import { contentHash } from './lib/drawio.mjs';
import { buildCandidate, generateVaultCandidates, planVaultDrawio } from './lib/pipeline.mjs';
import { captureBatchPreviews, captureDrawioPreview, latestDrawioPreview, PREVIEW_RECIPES } from './lib/preview.mjs';
import { captureFormatPreview } from './lib/format-preview.mjs';
import { generateMultiFormatCandidates, previewMultiFormatBatch, writeFormatCandidate } from './lib/multiformat-pipeline.mjs';
import { buildLineageManifest, checkDrift } from './lib/semantic.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const defaultVault = path.resolve(here, '..', '..');

function argumentsOf(argv) {
  const hasCommand = argv[0] && !argv[0].startsWith('-');
  const options = { command: hasCommand ? argv[0] : 'check', vault: defaultVault, strictPorts: false, failOnErrors: false };
  for (let index = hasCommand ? 1 : 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === '--vault') options.vault = path.resolve(argv[++index]);
    else if (item === '--out') options.out = path.resolve(argv[++index]);
    else if (item === '--out-dir') options.outDir = path.resolve(argv[++index]);
    else if (item === '--input') options.input = path.resolve(argv[++index]);
    else if (item === '--source') options.source = argv[++index];
    else if (item === '--format') options.format = argv[++index];
    else if (item === '--links') options.links = path.resolve(argv[++index]);
    else if (item === '--embedding-model') options.embeddingModel = argv[++index];
    else if (item === '--embedding-endpoint') options.embeddingEndpoint = argv[++index];
    else if (item === '--report') options.report = path.resolve(argv[++index]);
    else if (item === '--filter') options.filter = argv[++index];
    else if (item === '--limit') options.limit = Number(argv[++index]);
    else if (item === '--recipe') options.recipe = argv[++index];
    else if (item === '--chrome') options.chrome = path.resolve(argv[++index]);
    else if (item === '--timeout') options.timeout = Number(argv[++index]);
    else if (item === '--strict-ports') options.strictPorts = true;
    else if (item === '--theme') options.theme = true;
    else if (item === '--layout') options.layout = true;
    else if (item === '--json') options.json = true;
    else if (item === '--fail-on-errors') options.failOnErrors = true;
    else if (item === '--force') options.force = true;
    else if (item === '--no-embeddings') options.embeddings = false;
    else if (item === '--help' || item === '-h') options.help = true;
    else throw new Error(`unknown argument: ${item}`);
  }
  return options;
}

function usage() {
  return `Usage:
  node cli.mjs inventory [--vault PATH] [--out FILE] [--strict-ports]
  node cli.mjs audit     [--vault PATH] [--out FILE] [--strict-ports] [--fail-on-errors]
  node cli.mjs candidate --source FILE --out FILE [--report FILE] [--layout] [--theme] [--force] [--fail-on-errors]
  node cli.mjs format-candidate --format mermaid|excalidraw --source PATH[#LINE] --out FILE [--report FILE] [--force]
  node cli.mjs format-preview --format mermaid|excalidraw --source FILE --out-dir DIR [--chrome FILE] [--timeout MS]
  node cli.mjs multi-generate --out-dir DIR [--format FORMAT] [--filter TEXT] [--limit N] [--report FILE] [--force]
  node cli.mjs multi-preview --input BATCH_REPORT --out-dir DIR [--format FORMAT] [--filter TEXT] [--limit N] [--report FILE]
  node cli.mjs lineage [--links FILE] [--no-embeddings] [--embedding-model MODEL] [--out FILE]
  node cli.mjs drift-check --input LINEAGE_MANIFEST [--out FILE]
  node cli.mjs batch-plan [--vault PATH] [--filter TEXT] [--limit N] [--out FILE] [--json]
  node cli.mjs batch-generate --out-dir DIR [--filter TEXT] [--limit N] [--theme] [--force] [--report FILE] [--json]
  node cli.mjs batch-preview --input BATCH_REPORT --out-dir DIR [--filter TEXT] [--limit N] [--force] [--report FILE] [--json]
  node cli.mjs preview-recipes [--json]
  node cli.mjs preview-capture --source FILE --out-dir DIR [--recipe quick] [--chrome FILE] [--timeout MS] [--force] [--json]
  node cli.mjs preview-latest --out-dir DIR [--source FILE] [--recipe quick] [--json]

inventory and audit are read-only. candidate refuses to overwrite its source. By default
it updates business-edge style/geometry; --layout reflows acyclic swimlanes or applies
outer return channels to supported cyclic business flows; --theme applies the Feishu palette.

batch-plan is read-only. batch-generate writes derived candidates only and isolates failures.
preview-capture uses the real viewer.diagrams.net renderer through headless Google Chrome.`;
}

function printSummary(manifest) {
  const summary = manifest.summary;
  console.log(`assets=${summary.assets} drawio=${summary.formats.drawio || 0} mermaid=${summary.formats.mermaid || 0} excalidraw=${summary.formats.excalidraw || 0}`);
  console.log(`drawio checked=${summary.drawio.checked} pass=${summary.drawio.passed} fail=${summary.drawio.failed} unsupported=${summary.drawio.unsupported} errors=${summary.drawio.errors} warnings=${summary.drawio.warnings}`);
  console.log(`mermaid checked=${summary.mermaid.checked} pass=${summary.mermaid.passed} fail=${summary.mermaid.failed} errors=${summary.mermaid.errors} warnings=${summary.mermaid.warnings}`);
  console.log(`excalidraw checked=${summary.excalidraw.checked} pass=${summary.excalidraw.passed} fail=${summary.excalidraw.failed} errors=${summary.excalidraw.errors} warnings=${summary.excalidraw.warnings}`);
  const issueCounts = summarizeIssues(manifest);
  if (Object.keys(issueCounts.error).length || Object.keys(issueCounts.warning).length) console.log(`issues=${JSON.stringify(issueCounts)}`);
  for (const asset of manifest.assets.filter((item) => item.audit.status !== 'pass')) {
    console.log(`FAIL\t${asset.format}\t${asset.path}\terrors=${asset.audit.errors || 0}\twarnings=${asset.audit.warnings || 0}`);
  }
}

async function main() {
  const options = argumentsOf(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }
  const commands = ['inventory', 'audit', 'check', 'candidate', 'format-candidate', 'format-preview', 'multi-generate', 'multi-preview', 'lineage', 'drift-check', 'batch-plan', 'batch-generate', 'batch-preview', 'preview-recipes', 'preview-capture', 'preview-latest'];
  if (!commands.includes(options.command)) throw new Error(`unknown command: ${options.command}`);
  if (!fs.existsSync(options.vault)) throw new Error(`vault does not exist: ${options.vault}`);

  if (options.command === 'preview-recipes') {
    const result = { recipes: PREVIEW_RECIPES };
    console.log(options.json ? JSON.stringify(result) : Object.entries(PREVIEW_RECIPES).map(([name, recipe]) => `${name}\t${recipe.description}`).join('\n'));
    return;
  }

  if (options.command === 'format-candidate') {
    if (!['mermaid', 'excalidraw'].includes(options.format) || !options.source || !options.out) throw new Error('format-candidate requires --format mermaid|excalidraw --source and --out');
    const result = await writeFormatCandidate(options.vault, options.format, options.source, options.out, { reportPath: options.report, force: options.force });
    console.log(options.json ? JSON.stringify(result) : `candidate=${result.candidate_path}\nreport=${result.report_path}\nsource_hash_unchanged=${result.report.source_hash_unchanged}`);
    if (options.failOnErrors && Object.values(result.report.gates).includes('fail')) process.exitCode = 1;
    return;
  }

  if (options.command === 'format-preview') {
    if (!['mermaid', 'excalidraw'].includes(options.format) || !options.source || !options.outDir) throw new Error('format-preview requires --format mermaid|excalidraw --source and --out-dir');
    const sourcePath = path.resolve(options.vault, options.source);
    const result = await captureFormatPreview(options.format, sourcePath, options.outDir, { chrome: options.chrome, timeout: options.timeout });
    console.log(options.json ? JSON.stringify(result) : `status=${result.status} bundle=${result.bundle_dir} manifest=${result.manifest_path}`);
    return;
  }

  if (options.command === 'multi-generate') {
    if (!options.outDir) throw new Error('multi-generate requires --out-dir');
    const inventory = await buildVerifiedInventory(options.vault, { strictPorts: options.strictPorts });
    const result = await generateMultiFormatCandidates(options.vault, inventory, options.outDir, options);
    if (options.report) { fs.mkdirSync(path.dirname(options.report), { recursive: true }); fs.writeFileSync(options.report, `${JSON.stringify(result, null, 2)}\n`, 'utf8'); }
    console.log(options.json ? JSON.stringify(result) : `eligible=${result.summary.eligible} passed=${result.summary.passed} review=${result.summary.review_required} errors=${result.summary.errors} source_diagrams_modified=${result.source_diagrams_modified}`);
    if (options.failOnErrors && result.summary.errors) process.exitCode = 1;
    return;
  }

  if (options.command === 'multi-preview') {
    if (!options.input || !options.outDir) throw new Error('multi-preview requires --input and --out-dir');
    const batch = JSON.parse(fs.readFileSync(options.input, 'utf8'));
    const result = await previewMultiFormatBatch(options.vault, batch, options.outDir, options);
    if (options.report) { fs.mkdirSync(path.dirname(options.report), { recursive: true }); fs.writeFileSync(options.report, `${JSON.stringify(result, null, 2)}\n`, 'utf8'); }
    console.log(options.json ? JSON.stringify(result) : `eligible=${result.summary.eligible} passed=${result.summary.passed} failed=${result.summary.failed}`);
    if (options.failOnErrors && result.summary.failed) process.exitCode = 1;
    return;
  }

  if (options.command === 'lineage') {
    const inventory = await buildVerifiedInventory(options.vault, { strictPorts: options.strictPorts });
    const defaultLinks = path.join(here, 'config', 'process-links.json');
    const linksPath = options.links || defaultLinks;
    const links = fs.existsSync(linksPath) ? JSON.parse(fs.readFileSync(linksPath, 'utf8')) : { processes: [] };
    const result = await buildLineageManifest(options.vault, inventory, { links, embeddings: options.embeddings, embeddingOptions: { model: options.embeddingModel, endpoint: options.embeddingEndpoint } });
    if (options.out) { fs.mkdirSync(path.dirname(options.out), { recursive: true }); fs.writeFileSync(options.out, `${JSON.stringify(result, null, 2)}\n`, 'utf8'); }
    const compact = { entries: result.entries.length, embedding: result.embedding, drift: result.drift };
    console.log(options.json ? JSON.stringify(result) : JSON.stringify(compact));
    return;
  }

  if (options.command === 'drift-check') {
    if (!options.input) throw new Error('drift-check requires --input');
    const manifest = JSON.parse(fs.readFileSync(options.input, 'utf8'));
    const result = checkDrift(manifest);
    if (options.out) { fs.mkdirSync(path.dirname(options.out), { recursive: true }); fs.writeFileSync(options.out, `${JSON.stringify(result, null, 2)}\n`, 'utf8'); }
    console.log(options.json ? JSON.stringify(result) : JSON.stringify(result));
    if (options.failOnErrors && result.processes.some((item) => item.status === 'review-required')) process.exitCode = 1;
    return;
  }

  if (options.command === 'batch-plan') {
    const result = planVaultDrawio(options.vault, { filter: options.filter, limit: options.limit });
    if (options.out) {
      fs.mkdirSync(path.dirname(options.out), { recursive: true });
      fs.writeFileSync(options.out, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
    }
    if (options.json) console.log(JSON.stringify(result));
    else {
      console.log(`formal=${result.summary.formal_drawio} automatic=${result.summary.automatic} manual=${result.summary.manual_strategy_required} semantic_review=${result.summary.semantic_review_required}`);
      console.log(`strategies=${JSON.stringify(result.summary.strategies)}`);
      for (const item of result.items) console.log(`${item.automatic ? 'AUTO' : 'BLOCK'}\t${item.strategy}\t${item.path}`);
      if (options.out) console.log(`report=${options.out}`);
    }
    return;
  }

  if (options.command === 'batch-generate') {
    if (!options.outDir) throw new Error('batch-generate requires --out-dir');
    const result = generateVaultCandidates(options.vault, options.outDir, {
      filter: options.filter,
      limit: options.limit,
      theme: options.theme,
      force: options.force
    });
    if (options.report) {
      fs.mkdirSync(path.dirname(options.report), { recursive: true });
      fs.writeFileSync(options.report, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
    }
    if (options.json) console.log(JSON.stringify(result));
    else {
      console.log(`results=${JSON.stringify(result.summary)} source_diagrams_modified=${result.source_diagrams_modified}`);
      for (const item of result.items) console.log(`${item.status.toUpperCase()}\t${item.strategy}\t${item.path}`);
      if (options.report) console.log(`report=${options.report}`);
    }
    if (options.failOnErrors && (result.summary.error || result.summary.blocked)) process.exitCode = 1;
    return;
  }

  if (options.command === 'batch-preview') {
    if (!options.input || !options.outDir) throw new Error('batch-preview requires --input and --out-dir');
    const result = await captureBatchPreviews(options.vault, options.input, options.outDir, {
      filter: options.filter,
      limit: options.limit,
      recipe: options.recipe,
      chrome: options.chrome,
      timeout: options.timeout,
      force: options.force
    });
    if (options.report) {
      fs.mkdirSync(path.dirname(options.report), { recursive: true });
      fs.writeFileSync(options.report, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
    }
    if (options.json) console.log(JSON.stringify(result));
    else {
      console.log(`eligible=${result.summary.eligible} passed=${result.summary.passed} failed=${result.summary.failed} cached=${result.summary.cached}`);
      for (const item of result.items) console.log(`${item.status.toUpperCase()}\t${item.path}\t${item.bundle_dir || item.error}`);
      if (options.report) console.log(`report=${options.report}`);
    }
    if (options.failOnErrors && result.summary.failed) process.exitCode = 1;
    return;
  }

  if (options.command === 'preview-capture') {
    if (!options.source || !options.outDir) throw new Error('preview-capture requires --source and --out-dir');
    const sourcePath = path.resolve(options.vault, options.source);
    if (!fs.existsSync(sourcePath)) throw new Error(`source does not exist: ${sourcePath}`);
    const result = await captureDrawioPreview(sourcePath, options.outDir, {
      recipe: options.recipe,
      chrome: options.chrome,
      timeout: options.timeout,
      force: options.force
    });
    if (options.json) console.log(JSON.stringify(result));
    else console.log(`status=${result.status} cached=${result.cached} bundle=${result.bundle_dir} manifest=${result.manifest_path}`);
    return;
  }

  if (options.command === 'preview-latest') {
    if (!options.outDir) throw new Error('preview-latest requires --out-dir');
    const sourcePath = options.source ? path.resolve(options.vault, options.source) : null;
    const result = latestDrawioPreview(options.outDir, { recipe: options.recipe, sourcePath });
    console.log(options.json ? JSON.stringify(result) : `status=${result.status} bundle=${result.bundle_dir || ''} manifest=${result.manifest_path || ''}`);
    if (options.failOnErrors && result.status !== 'ok') process.exitCode = 1;
    return;
  }

  if (options.command === 'candidate') {
    if (!options.source || !options.out) throw new Error('candidate requires --source and --out');
    const sourcePath = path.resolve(options.vault, options.source);
    const outputPath = path.resolve(options.out);
    const reportPath = options.report || `${outputPath}.report.json`;
    if (!fs.existsSync(sourcePath)) throw new Error(`source does not exist: ${sourcePath}`);
    if (sourcePath === outputPath || sourcePath === reportPath) throw new Error('candidate output/report must not overwrite source');
    if (!options.force && (fs.existsSync(outputPath) || fs.existsSync(reportPath))) {
      throw new Error('candidate output or report already exists; pass --force to replace derived files');
    }
    const source = fs.readFileSync(sourcePath, 'utf8');
    const sourceHash = contentHash(source);
    const relativeSource = path.relative(options.vault, sourcePath).split(path.sep).join('/');
    const result = buildCandidate(source, {
      layout: options.layout ? 'auto' : false,
      theme: options.theme,
      metadata: { path: relativeSource }
    });
    const candidateXml = result.xml;
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(outputPath, candidateXml, 'utf8');
    const sourceHashAfter = contentHash(fs.readFileSync(sourcePath, 'utf8'));
    const report = {
      source_path: relativeSource,
      candidate_path: path.relative(options.vault, outputPath).split(path.sep).join('/'),
      source_hash_unchanged: sourceHashAfter === sourceHash,
      ...result.report
    };
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    if (options.json) console.log(JSON.stringify({ candidate: outputPath, report: reportPath, result: report }));
    else {
      console.log(`candidate=${outputPath}`);
      console.log(`report=${reportPath}`);
      console.log(`source_hash_unchanged=${report.source_hash_unchanged} changed_edges=${report.routing.changed_edges} before_errors=${report.routing.before.errors} after_errors=${report.routing.after.errors} unresolved=${report.routing.unresolved.length} layout=${report.gates.layout} theme=${report.gates.theme} semantic=${report.gates.semantic}`);
    }
    if (!report.source_hash_unchanged) throw new Error('source hash changed while generating candidate');
    if (options.failOnErrors && (report.gates.geometry === 'fail' || report.gates.layout === 'fail' || report.gates.theme === 'fail')) process.exitCode = 1;
    return;
  }

  const manifest = await buildVerifiedInventory(options.vault, { strictPorts: options.strictPorts });
  if (!options.json) printSummary(manifest);
  if (options.out) {
    fs.mkdirSync(path.dirname(options.out), { recursive: true });
    fs.writeFileSync(options.out, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    if (!options.json) console.log(`report=${options.out}`);
  }
  if (options.json) console.log(JSON.stringify(manifest));
  if (options.failOnErrors && ['drawio', 'mermaid', 'excalidraw'].some((format) => manifest.summary[format].failed > 0)) process.exitCode = 1;
}

try {
  await main();
} catch (error) {
  console.error(error.stack || error.message);
  process.exitCode = 2;
}
