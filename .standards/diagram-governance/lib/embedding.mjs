import { contentHash } from './drawio.mjs';

export function cosineSimilarity(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length || !a.length) return null;
  let dot = 0; let left = 0; let right = 0;
  for (let index = 0; index < a.length; index += 1) { dot += a[index] * b[index]; left += a[index] ** 2; right += b[index] ** 2; }
  return left && right ? dot / Math.sqrt(left * right) : null;
}

export async function embedTexts(inputs, options = {}) {
  const endpoint = options.endpoint || process.env.OLLAMA_HOST || 'http://127.0.0.1:11434';
  const model = options.model || 'bge-m3:latest';
  const normalized = inputs.map((item) => String(item || '').trim());
  const response = await fetch(`${endpoint.replace(/\/$/, '')}/api/embed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model, input: normalized })
  });
  if (!response.ok) throw new Error(`ollama_embed_failed: HTTP ${response.status}`);
  const payload = await response.json();
  if (!Array.isArray(payload.embeddings) || payload.embeddings.length !== normalized.length) throw new Error('ollama_embed_invalid_response');
  return {
    model: payload.model || model,
    endpoint,
    dimensions: payload.embeddings[0]?.length || 0,
    input_hashes: normalized.map(contentHash),
    embeddings: payload.embeddings,
    load_duration: payload.load_duration || null,
    total_duration: payload.total_duration || null
  };
}
