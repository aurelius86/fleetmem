#!/usr/bin/env node
/*
 * Brain live graph endpoint  +  Infra (homelab) live endpoint
 * -----------------------------------------------------------
 *   GET /graph.json   -> knowledge graph (Quartz contentIndex-shaped)  [unchanged]
 *   GET /transcript.json?session=<id> -> redacted chat transcript for a session note
 *   GET /healthz      -> "ok"
 *
 * Pure Node stdlib, no external deps.
 */
'use strict';
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { execFileSync } = require('child_process');

// ---- response helpers (stdlib-only) --------------------------------------
// Kept here, near the requires and clear of any infra-fenced block, because
// these are used by EVERY JSON route, not just the infra ones. They previously
// sat inside the topology fence (SERVICES..getBrainHealth) and were stripped
// from the scrubbed fleetmem/fleetmem build by build-fleetmem.js — which broke
// every data endpoint in the shipped dashboard (sendJson not defined)..
const sendJson = (res, code, obj) => { res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify(obj)); };
// gzip large JSON payloads (graph.json ~2.4MB) when the client accepts it — big transfer win, browsers decode transparently.
const sendJsonGz = (req, res, code, obj) => {
  const body = Buffer.from(JSON.stringify(obj));
  const ae = String((req.headers && req.headers['accept-encoding']) || '');
  if (/\bgzip\b/.test(ae) && body.length > 1400) {
    const gz = zlib.gzipSync(body);
    res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store', 'content-encoding': 'gzip', 'vary': 'accept-encoding' });
    res.end(gz);
  } else {
    res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    res.end(body);
  }
};

const PORT       = parseInt(process.env.PORT || '8788', 10);
// Bind loopback by DEFAULT: this server holds a full-console brain credential (mTLS cert + token)
// and has no auth of its own, so it must sit behind an mTLS proxy. A same-host proxy or an SSH
// tunnel reaches 127.0.0.1 out of the box; exposing it wider is a deliberate BIND_HOST= opt-in
// (home sets BIND_HOST to a LAN-reachable value because a reverse proxy fronts it from another host).
const HOST       = process.env.BIND_HOST || '127.0.0.1';
const BRAIN_DIR  = process.env.BRAIN_DIR || '/opt/fleetmem-brain/shared';
const GIT_DIR    = process.env.GIT_DIR   || '/opt/fleetmem-brain';
// The systemd unit sets Environment=GIT_DIR=/opt/fleetmem-brain (meant as this const), but that leaks
// into git SUBPROCESSES as the real $GIT_DIR — git then treats it as the .git dir and every
// `git -C <dir>` fails with "not a git repository". Strip it (and GIT_WORK_TREE) for git children.
const GIT_ENV = Object.assign({}, process.env); delete GIT_ENV.GIT_DIR; delete GIT_ENV.GIT_WORK_TREE;
const PULL_TTL   = parseInt(process.env.PULL_TTL || '30', 10);
const CACHE_TTL  = parseInt(process.env.CACHE_TTL || '30', 10);
// brain-v2: the graph now reads the governed brain API (mTLS + token), not git markdown files.
const BRAIN_API  = process.env.BRAIN_API_URL || 'https://127.0.0.1:8443';
const BRAIN_PKI  = process.env.BRAIN_PKI_DIR || '/opt/fleetmem-dashboard/pki';
// Read-only mirror of the chat archive (per-session JSONL transcripts), pulled like the brain repo.
const ARCHIVE_DIR = process.env.CHAT_ARCHIVE_DIR || '/opt/fleetmem-chat-archive';

const EXCLUDE_SLUGS     = new Set(['memory/MEMORY', 'README']);
const EXCLUDE_BASENAMES = new Set(['index', 'readme']);
const isExcluded = (slug) => EXCLUDE_SLUGS.has(slug) || EXCLUDE_BASENAMES.has(slug.split('/').pop().toLowerCase());

const norm = (s) =>
  String(s).toLowerCase().replace(/\.md$/, '').replace(/[\s-]+/g, '_').replace(/[^a-z0-9_/]/g, '');

function walk(dir, root, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, root, out);
    else if (entry.isFile() && entry.name.endsWith('.md'))
      out.push(path.relative(root, full).replace(/\.md$/, '').split(path.sep).join('/'));
  }
  return out;
}
function titleOf(slug, body) {
  const fm = body.match(/^---\n([\s\S]*?)\n---/);
  if (fm) { const t = fm[1].match(/^title:\s*["']?(.+?)["']?\s*$/m); if (t) return t[1].trim(); }
  const h1 = body.replace(/^---\n[\s\S]*?\n---\n?/, '').match(/^#\s+(.+)$/m);
  if (h1) return h1[1].trim();
  return slug.split('/').pop();
}
const stripFrontmatter = (body) => body.replace(/^---\n[\s\S]*?\n---\n?/, '');

// per-body access map. sensitivity:high = manager-only; else worker-readable
// (workers, which share an identical boundary). Docs carry no frontmatter, so these 4 (the
// 1P/vault/cert map) are manager-only by the same rule seed_scoped.py applies.
const SENSITIVE_DOCS = new Set((process.env.SENSITIVE_DOCS || '').split(',').map(s => s.trim()).filter(Boolean));
function managerOnly(slug, raw) {
  const fm = (raw.match(/^---\n([\s\S]*?)\n---/) || [])[1] || '';
  const m = fm.match(/^\s*sensitivity:\s*["']?(\w+)/m);
  if (m) return m[1].toLowerCase() === 'high';
  return SENSITIVE_DOCS.has(slug);   // docs with no frontmatter: only the sensitive-4 are manager-only
}

// ---- brain graph build — reads the brain API (brain-v2), NOT git files --------
let _brainTls = null;
function brainTls() {
  if (!_brainTls) _brainTls = {
    cert: fs.readFileSync(path.join(BRAIN_PKI, 'client.crt')),
    key: fs.readFileSync(path.join(BRAIN_PKI, 'client.key')),
    ca: fs.readFileSync(path.join(BRAIN_PKI, 'ca.crt')),
    token: fs.readFileSync(path.join(BRAIN_PKI, 'brain.token'), 'utf8').trim(),
  };
  return _brainTls;
}
function brainApi(apiPath, timeoutMs) {
  return new Promise((resolve, reject) => {
    let t; try { t = brainTls(); } catch (e) { return reject(new Error('brain creds missing: ' + e.message)); }
    const u = new URL(BRAIN_API + apiPath);
    const req = https.request(u, {
      method: 'GET', cert: t.cert, key: t.key, ca: t.ca, servername: u.hostname,
      headers: { Authorization: 'Bearer ' + t.token },
    }, (res) => {
      let d = ''; res.on('data', (c) => (d += c));
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) return reject(new Error('brain API HTTP ' + res.statusCode + ' ' + apiPath));
        try { resolve(d ? JSON.parse(d) : {}); } catch (e) { reject(new Error('bad json from ' + apiPath)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs || 8000, () => req.destroy(new Error('brain API timeout ' + apiPath)));
    req.end();
  });
}
// POST to the brain API (mTLS + token). Used by /approve-save to record a proposal decision.
function brainPost(apiPath, bodyObj, timeoutMs) {
  return new Promise((resolve, reject) => {
    let t; try { t = brainTls(); } catch (e) { return reject(new Error('brain creds missing: ' + e.message)); }
    const u = new URL(BRAIN_API + apiPath);
    const payload = Buffer.from(JSON.stringify(bodyObj || {}));
    const req = https.request(u, {
      method: 'POST', cert: t.cert, key: t.key, ca: t.ca, servername: u.hostname,
      headers: { Authorization: 'Bearer ' + t.token, 'content-type': 'application/json', 'content-length': payload.length },
    }, (res) => {
      let d = ''; res.on('data', (c) => (d += c));
      res.on('end', () => {
        let j = null; try { j = d ? JSON.parse(d) : {}; } catch (_) {}
        if (res.statusCode < 200 || res.statusCode >= 300)
          return reject(new Error('brain API HTTP ' + res.statusCode + (j && j.error ? ' ' + j.error : '') + ' ' + apiPath));
        resolve(j || {});
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs || 8000, () => req.destroy(new Error('brain API timeout ' + apiPath)));
    req.write(payload); req.end();
  });
}
// PATCH to the brain API (mTLS + token). Used by the Config tab to write config knobs.
function brainPatch(apiPath, bodyObj, timeoutMs) {
  return new Promise((resolve, reject) => {
    let t; try { t = brainTls(); } catch (e) { return reject(new Error('brain creds missing: ' + e.message)); }
    const u = new URL(BRAIN_API + apiPath);
    const payload = Buffer.from(JSON.stringify(bodyObj || {}));
    const req = https.request(u, {
      method: 'PATCH', cert: t.cert, key: t.key, ca: t.ca, servername: u.hostname,
      headers: { Authorization: 'Bearer ' + t.token, 'content-type': 'application/json', 'content-length': payload.length },
    }, (res) => {
      let d = ''; res.on('data', (c) => (d += c));
      res.on('end', () => {
        let j = null; try { j = d ? JSON.parse(d) : {}; } catch (_) {}
        if (res.statusCode < 200 || res.statusCode >= 300)
          return reject(new Error('brain API HTTP ' + res.statusCode + (j && j.error ? ' ' + j.error : '') + ' ' + apiPath));
        resolve(j || {});
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs || 8000, () => req.destroy(new Error('brain API timeout ' + apiPath)));
    req.write(payload); req.end();
  });
}
// Build the name-keyed map the frontend expects: { name: {title, content, links[], mgr, svc} }.
// name = filename stem (the brain stores no folders). Source = /graph (nodes+edges) + /memories?full=1.
async function buildBrainIndex() {
  const [graph, mem] = await Promise.all([brainApi('/graph'), brainApi('/memories?full=1&limit=10000')]);
  const metaByName = new Map((mem.memories || []).map((m) => [m.name, m]));
  const nameById = new Map((graph.nodes || []).map((n) => [n.id, n.name]));
  const index = {};
  for (const n of (graph.nodes || [])) {
    if (!n.name) continue;   // a nameless memory (a personal note saved without a name) can't key this
                             // name-indexed graph and was crashing the build at n.name.split — skip it.
    const m = metaByName.get(n.name) || {};
    const sens = String(m.sensitivity || '').toLowerCase();
    index[n.name] = {
      title: (m.description && m.description.trim()) || n.name.split('/').pop(),
      content: m.body || '',
      links: [],
      mgr: sens === 'sensitive' || sens === 'secret',
      // carry the real memory metadata the graph node/meta already hold, so the SPA can
      // group by mtype (not a filename guess) and show it in the drawer instead of throwing it away.
      mtype: n.mtype || m.mtype || '',
      sens: sens || '',
      tier: m.mem_tier || '',
      updated: m.updated_at || '',
      // extra metadata from /graph — session provenance + trust/tags/origin/validity for the drawer
      ss: n.source_session || '',
      author: n.author_body || '',
      trust: n.trust || '',
      tags: Array.isArray(n.tags) ? n.tags : [],
      origin: n.origin_channel || '',
      valid: n.valid_at || '',
      invalid: n.invalid_at || '',
    };
  }
  for (const e of (graph.edges || [])) {
    const s = nameById.get(e.source), t = nameById.get(e.target);
    // keep the relation TYPE (relates_to / supersedes / conflicts_with / typed) per edge —
    // links is now [{to, rel}] not [name]. The SPA colours + filters links by rel.
    if (s && t && index[s] && s !== t) index[s].links.push({ to: t, rel: e.rel || 'relates_to' });
  }
  const hub = 'reference_service-catalog';   // group the catalog hub + its neighbors as Services
  if (index[hub]) { index[hub].svc = true; for (const l of index[hub].links) if (index[l.to]) index[l.to].svc = true; }
  return index;
}
let cache = null, cacheAt = 0, lastPull = 0;
function maybePull() {
  const now = Date.now();
  if (now - lastPull < PULL_TTL * 1000) return;
  lastPull = now;
  try { execFileSync('git', ['-C', GIT_DIR, 'pull', '--ff-only'], { stdio: 'ignore', timeout: 8000, env: GIT_ENV }); } catch (_) {}
}
let lastArchivePull = 0;
function maybePullArchive() {
  const now = Date.now();
  if (now - lastArchivePull < PULL_TTL * 1000) return;
  lastArchivePull = now;
  try { execFileSync('git', ['-C', ARCHIVE_DIR, 'pull', '--ff-only'], { stdio: 'ignore', timeout: 8000, env: GIT_ENV }); } catch (_) {}
}
async function getPayload() {
  const now = Date.now();
  if (cache && now - cacheAt < CACHE_TTL * 1000) return cache;
  cache = await buildBrainIndex(); cacheAt = now; return cache;
}

// ---- TRANSCRIPT: raw chat archive (read-only mirror), redacted on read ------
// Raw chat behind mTLS-only must NEVER leak a secret that was in-session, so every returned line is
// scrubbed of op:// refs, webhook URLs, and token-shaped strings before it leaves the server.
// no hardcoded fleet agent names in the shipped artifact. The local archive-file fallback is
// home-specific; the primary transcript path is the brain API. Default EMPTY (ship: fallback no-ops,
// brain-API path is used); home sets BODY_DIRS to its comma-separated body names in the dashboard env.
const BODY_DIRS = (process.env.BODY_DIRS || '').split(',').map((s) => s.trim()).filter(Boolean);
function redactSecrets(s) {
  return String(s)
    .replace(/op:\/\/[^\s"'`)]+/g, 'op://«redacted»')
    .replace(/https?:\/\/[^\s"'`)]*(?:webhook|hook)[^\s"'`)]*/gi, '«redacted-webhook-url»')
    .replace(/\b(?:sk-|ghp_|gho_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9._-]{8,}/g, '«redacted-token»')
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+/g, '«redacted-jwt»')
    // space-form "Authorization: Bearer <opaque-token>" — the [:=] rule below misses it (space, not :/=),
    // so opaque (non-JWT) bearer tokens (brain/HA tokens) would leak through /transcript.json. Mirrors the ingester rule.
    .replace(/\bbearer\s+[A-Za-z0-9._~+\/=-]{10,}/gi, 'Bearer «redacted»')
    .replace(/((?:secret|token|api[_-]?key|password|passwd|bearer)\b[^\S\n]*[:=][^\S\n]*)\S+/gi, '$1«redacted»');
}
function findTranscriptFile(shortId) {
  if (!/^[A-Za-z0-9-]{4,}$/.test(shortId)) return null;
  for (const d of BODY_DIRS) {
    let files; try { files = fs.readdirSync(path.join(ARCHIVE_DIR, d)); } catch { continue; }
    const hit = files.find((f) => f.endsWith('.jsonl') && f.startsWith(shortId));
    if (hit) return path.join(ARCHIVE_DIR, d, hit);
  }
  return null;
}
function blockText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter((b) => b && b.type === 'text' && b.text).map((b) => b.text).join('\n');
  return '';
}
// read the transcript from the BRAIN (Postgres the brain host, GET /session/<sid>/turns) — the same
// store the search uses. Falls back to the legacy local chat-archive file if the brain is unreachable
// or has no matching session, so the viewer never goes dark during the soak ( retires the archive).
async function getTranscript(shortId, q, limit) {
  try {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (q) qs.set('q', q);
    const b = await brainApi('/session/' + encodeURIComponent(shortId) + '/turns?' + qs.toString(), 8000);
    if (b && b.ok && Array.isArray(b.turns) && b.turns.length) {
      return { session: b.source_session || shortId, source: 'brain', total: b.total,
               returned: b.returned != null ? b.returned : b.turns.length, turns: b.turns };
    }
  } catch (e) { /* brain unreachable / no session (404) -> fall back to the local archive file */ }
  maybePullArchive();
  const file = findTranscriptFile(shortId);
  if (!file) return { error: 'no transcript found for ' + shortId, turns: [] };
  let raw; try { raw = fs.readFileSync(file, 'utf8'); } catch (e) { return { error: String((e && e.message) || e), turns: [] }; }
  const turns = [];
  for (const line of raw.split('\n')) {
    const ln = line.trim(); if (!ln) continue;
    let o; try { o = JSON.parse(ln); } catch { continue; }
    if (o.type !== 'user' && o.type !== 'assistant') continue;     // skip metadata/tool/system lines
    const msg = o.message || {};
    let text = redactSecrets(blockText(msg.content)).trim();
    if (!text) continue;                                            // drops thinking/tool_use/tool_result (no text block)
    if (q && !text.toLowerCase().includes(q.toLowerCase())) continue;
    turns.push({ role: o.type, ts: o.timestamp || null, text });
  }
  const capped = turns.slice(-limit);
  return { session: shortId, source: 'archive-file', file: path.basename(file), total: turns.length, returned: capped.length, turns: capped };
}

// ---- static: the dashboard SPA --------------------------------------------
const DASH_HTML = path.join(__dirname, 'index.html');
function serveDash(res) {
  fs.readFile(DASH_HTML, (err, buf) => {
    if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('index.html missing'); return; }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
    res.end(buf);
  });
}

// ---- static: vendored assets (fonts + JS libs) so the dashboard never phones home ----------
const VENDOR_DIR = path.join(__dirname, 'vendor');
const VENDOR_TYPES = { '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.woff2': 'font/woff2' };
function serveVendor(req, res) {
  const name = path.basename(req.url.split('?')[0]);        // basename() => path-traversal safe
  const type = VENDOR_TYPES[path.extname(name)];
  if (!type) { res.writeHead(404, { 'content-type': 'text/plain' }).end('not found'); return; }
  fs.readFile(path.join(VENDOR_DIR, name), (err, buf) => {
    if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('vendor asset missing'); return; }
    res.writeHead(200, { 'content-type': type, 'cache-control': 'public, max-age=31536000, immutable' });
    res.end(buf);
  });
}

// ---- server ----------------------------------------------------------------
http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  if (url === '/healthz') { res.writeHead(200).end('ok'); return; }
  if (url === '/' || url === '/index.html') { serveDash(res); return; }
  if (url.startsWith('/vendor/')) { serveVendor(req, res); return; }
  if (url === '/graph.json') {
    getPayload()
      .then((p) => sendJsonGz(req, res, 200, p))
      .catch((e) => { res.writeHead(502, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: String((e && e.message) || e) })); });
    return;
  }
  if (url === '/transcript.json') {
    const q = new URLSearchParams(req.url.split('?')[1] || '');
    const sid = (q.get('session') || '').trim();
    if (!/^[A-Za-z0-9-]{4,}$/.test(sid)) { sendJson(res, 400, { error: 'bad session id' }); return; }
    let lim = parseInt(q.get('lines') || '400', 10); if (!Number.isFinite(lim) || lim < 1) lim = 400; lim = Math.min(lim, 1200);
    getTranscript(sid, q.get('q') || '', lim)
      .then((t) => sendJson(res, 200, t))
      .catch((e) => sendJson(res, 500, { error: String((e && e.message) || e), turns: [] }));
    return;
  }
  // ---- memory-approval view (shared-brain Phase D) — LIVE off the brain proposal table ----
  // GET /approve       -> the approval page; the live proposal queue (all statuses) is fetched from
  //                       the brain API (viewer/approver-readable) and injected at __QUEUE__.
  // POST /approve-save -> records ONE decision ({id, decision: approved|rejected, reason?}) straight
  //                       into the brain proposal table via POST /proposal/<id>/decide (approver auth).
  //   The old review-queue.json file pipeline + /approve-sync button are retired (Phase D).
  if (url === '/approve') {
    fs.readFile(path.join(__dirname, 'approve.html'), 'utf8', (err, tpl) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('approve.html missing'); return; }
      // proposals (auto-learn queue) + provisional items (agent working memory + anchored tables)
      Promise.all([brainApi('/proposals?status='), brainApi('/provisional/pending'),
                   brainApi('/graph/edge-proposals').catch(() => ({ proposals: [] }))])   // never fail the page if edge-proposals errors
        .then(([p, pv, ep]) => {
          const out = tpl.replace('__QUEUE__', JSON.stringify((p && p.proposals) || []))
                         .replace('__PROVISIONAL__', JSON.stringify((pv && pv.pending) || []))
                         .replace('__EDGEPROPOSALS__', JSON.stringify((ep && ep.proposals) || []));   //
          res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
          res.end(out);
        })
        .catch((e) => { res.writeHead(502, { 'content-type': 'text/html; charset=utf-8' }); res.end('<p>brain API unreachable: ' + String((e && e.message) || e) + '</p>'); });
    });
    return;
  }
  if (url === '/approve-save' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      // provisional items (memory + its anchored tables) graduate/delete — same route, no proxy change
      if (u && u.kind === 'provisional') {
        if (!u.id || (u.decision !== 'graduate' && u.decision !== 'delete')) {
          sendJson(res, 400, { error: 'need {kind:provisional, id, decision: graduate|delete, reason?, name?, description?, body?}' }); return;
        }
        // graduation = curation: pass through optional rename/amend overrides
        const dp = { decision: u.decision, reason: u.reason || null };
        if (typeof u.name === 'string' && u.name.trim()) dp.name = u.name.trim();
        if (typeof u.description === 'string') dp.description = u.description;
        if (typeof u.body === 'string' && u.body.trim()) dp.body = u.body;
        brainPost('/provisional/' + encodeURIComponent(u.id) + '/decide', dp)
          .then((j) => sendJson(res, 200, { ok: true, id: u.id, decision: u.decision, error: j && j.error }))
          .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
        return;
      }
      // edge-type proposal decision -> brain /graph/edge-proposals/decide
      if (u && u.kind === 'edge') {
        if (!u.id || (u.decision !== 'approve' && u.decision !== 'reject')) {
          sendJson(res, 400, { error: 'need {kind:edge, id, decision: approve|reject}' }); return;
        }
        brainPost('/graph/edge-proposals/decide', { edge_id: u.id, decision: u.decision })
          .then((j) => sendJson(res, 200, { ok: !(j && j.error), id: u.id, decision: u.decision, error: j && j.error }))
          .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
        return;
      }
      if (!u || !u.id || (u.decision !== 'approved' && u.decision !== 'rejected')) {
        sendJson(res, 400, { error: 'need {id, decision: approved|rejected, reason?}' }); return;
      }
      brainPost('/proposal/' + encodeURIComponent(u.id) + '/decide', { decision: u.decision, reason: u.reason || null })
        .then((j) => sendJson(res, 200, { ok: true, id: u.id, status: j.status || u.decision }))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // POST /session-rebuild {sid} -> brain POST /session/<sid>/rebuild (manager-auth'd server-side).
  // Manual, source-grounded rebuild of ONE session's memories (writes NEW quarantined+personal notes
  // + grounded edges after dedup; nothing trusted is touched). Long (LLM extract) -> 120s timeout.
  if (url === '/session-rebuild' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      const sid = ((u && u.sid) || '').trim();
      if (!/^[0-9a-fA-F-]{8,64}$/.test(sid)) { sendJson(res, 400, { error: 'bad session id' }); return; }
      brainPost('/session/' + encodeURIComponent(sid) + '/rebuild', {}, 120000)
        .then((j) => sendJson(res, 200, j))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }

  // ---- enrollment & access admin — enrollment-queue + revoke, LIVE off the brain API ----
  // GET /enroll-admin  -> the enrollment page; pending applications (manager-readable) injected at
  //                       __PENDING__, the K-of-N approval threshold at __REQUIRED__.
  // POST /enroll-save  -> {kind:'enroll', id, decision: approve|reject, assign_role?} => brain
  //                       POST /enroll/<id>/approve; {kind:'revoke', name, unrevoke?} => brain
  //                       POST /agent/<name>/revoke. Both are manager-auth'd server-side.
  if (url === '/enroll-admin') {
    fs.readFile(path.join(__dirname, 'enroll-admin.html'), 'utf8', (err, tpl) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('enroll-admin.html missing'); return; }
      brainApi('/enroll/pending')
        .then((p) => {
          const out = tpl.replace('__PENDING__', JSON.stringify((p && p.pending) || []))
                         .replace('__REQUIRED__', JSON.stringify((p && p.required) || 1));
          res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
          res.end(out);
        })
        .catch((e) => { res.writeHead(502, { 'content-type': 'text/html; charset=utf-8' }); res.end('<p>brain API unreachable: ' + String((e && e.message) || e) + '</p>'); });
    });
    return;
  }
  if (url === '/enroll-save' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      if (u && u.kind === 'revoke') {
        if (!u.name) { sendJson(res, 400, { error: 'need {kind:revoke, name, unrevoke?}' }); return; }
        brainPost('/agent/' + encodeURIComponent(u.name) + '/revoke', { unrevoke: !!u.unrevoke })
          .then((j) => sendJson(res, 200, { ok: !(j && j.error), name: u.name, action: j && j.action, note: j && j.note, error: j && j.error }))
          .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
        return;
      }
      // default: an enrollment vote
      if (!u || !u.id || (u.decision !== 'approve' && u.decision !== 'reject')) {
        sendJson(res, 400, { error: 'need {kind:enroll, id, decision: approve|reject, assign_role?}' }); return;
      }
      const body = { decision: u.decision };
      if (typeof u.assign_role === 'string' && u.assign_role) body.assign_role = u.assign_role;
      brainPost('/enroll/' + encodeURIComponent(u.id) + '/approve', body)
        .then((j) => sendJson(res, 200, { ok: !(j && j.error), id: u.id, decision: u.decision,
          status: j && j.status, approvers: j && j.approvers, approve_count: j && j.approve_count,
          required: j && j.required, error: j && j.error }))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // audit-log viewer — read-only census of the brain action_log (manager/viewer/approver).
  if (url === '/audit-log') {
    fs.readFile(path.join(__dirname, 'audit-log.html'), 'utf8', (err, tpl) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('audit-log.html missing'); return; }
      brainApi('/audit?limit=200')
        .then((a) => {
          const out = tpl.replace('__AUDIT__', JSON.stringify((a && a.audit) || []));
          res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
          res.end(out);
        })
        .catch((e) => { res.writeHead(502, { 'content-type': 'text/html; charset=utf-8' }); res.end('<p>brain API unreachable: ' + String((e && e.message) || e) + '</p>'); });
    });
    return;
  }
  // ---- (Approval 2.0 step 4): the "needs-you" queue — ONLY genuine manager-needs ----
  // GET /needs-you       -> the page; brain GET /needs-you ({share_requests,needs_human}) injected at __NEEDS__.
  // POST /needs-you-save -> {kind:'vouch', id, verdict:'trusted'|'invalid'} => brain POST
  //                         /memory/<id>/validate {verdict, basis:'manager-vouch'} (no source needed);
  //                         {kind:'dismiss', subject} => brain POST /needs-you/dismiss. Both manager-auth'd.
  if (url === '/needs-you') {
    fs.readFile(path.join(__dirname, 'needs-you.html'), 'utf8', (err, tpl) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('needs-you.html missing'); return; }
      brainApi('/needs-you')
        .then((n) => {
          const out = tpl.replace('__NEEDS__', JSON.stringify(n || { share_requests: [], needs_human: [] }));
          res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
          res.end(out);
        })
        .catch((e) => { res.writeHead(502, { 'content-type': 'text/html; charset=utf-8' }); res.end('<p>brain API unreachable: ' + String((e && e.message) || e) + '</p>'); });
    });
    return;
  }
  if (url === '/needs-you-save' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      if (u && u.kind === 'dismiss') {
        if (!u.subject) { sendJson(res, 400, { error: 'need {kind:dismiss, subject}' }); return; }
        brainPost('/needs-you/dismiss', { subject: u.subject })
          .then((j) => sendJson(res, 200, { ok: !(j && j.error), cleared: j && j.cleared, error: j && j.error }))
          .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
        return;
      }
      // default: a manager vouch (trusted) / reject (invalid) of an escalated note — no source needed.
      if (!u || !u.id || (u.verdict !== 'trusted' && u.verdict !== 'invalid')) {
        sendJson(res, 400, { error: 'need {kind:vouch, id, verdict: trusted|invalid}' }); return;
      }
      brainPost('/memory/' + encodeURIComponent(u.id) + '/validate', { verdict: u.verdict, basis: 'manager-vouch' })
        .then((j) => sendJson(res, 200, { ok: !(j && j.error), id: u.id, verdict: u.verdict, trust: j && j.trust, deleted: j && j.deleted, error: j && j.error }))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // ---- (Approval 2.0 step 5): faceted memory explorer ----
  // GET /explorer       -> the page (fetches its data live client-side).
  // GET /explorer.json  -> proxy brain GET /memories/explore with whitelisted facet params.
  // GET /relations.json -> proxy brain GET /memory/<id>/relations. Both manager/approver-gated server-side.
  if (url === '/explorer') {
    fs.readFile(path.join(__dirname, 'explorer.html'), 'utf8', (err, tpl) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }).end('explorer.html missing'); return; }
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache' });
      res.end(tpl);
    });
    return;
  }
  if (url === '/explorer.json') {
    const q = new URLSearchParams(req.url.split('?')[1] || '');
    const out = new URLSearchParams();
    for (const k of ['author', 'mtype', 'sensitivity', 'validation', 'q', 'limit', 'offset']) {
      const v = (q.get(k) || '').trim();
      if (v) out.set(k, v);
    }
    const qs = out.toString();
    brainApi('/memories/explore' + (qs ? '?' + qs : ''))
      .then((j) => sendJson(res, 200, j))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/relations.json') {
    const q = new URLSearchParams(req.url.split('?')[1] || '');
    const id = (q.get('id') || '').trim();
    if (!/^[0-9a-fA-F-]{8,36}$/.test(id)) { sendJson(res, 400, { error: 'bad id' }); return; }
    brainApi('/memory/' + encodeURIComponent(id) + '/relations')
      .then((j) => sendJson(res, 200, j))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // work-item proxies for the Work board + graph nodes (projects/ideas). Same mTLS+token
  // identity as /config.json; tasks/projects/ideas are viewer-readable in the brain API. Read-only.
  if (url === '/projects.json') {
    brainApi('/projects', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/ideas.json') {
    brainApi('/ideas', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/tasks.json') {
    const qs = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';   // forward status/limit (from req.url — `url` is query-stripped)
    brainApi('/tasks' + qs, 8000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // brain-internals clarity — batch-job status, pending edge-type proposals, tag cloud.
  if (url === '/jobs.json') {
    brainApi('/jobs', 8000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // memory activity feed for the Timeline page (proxies the brain /timeline reader).
  if (url === '/timeline.json') {
    const qs = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';   // fix: read query from req.url (`url` is query-stripped)
    brainApi('/timeline' + qs, 8000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // GATED job control -> brain POST /jobs/<name>/<action> (brain re-validates whitelist+role+sudoers).
  if (url.startsWith('/jobs/') && req.method === 'POST') {
    const parts = url.split('/').filter(Boolean);   // ['jobs', <name>, <action>]
    if (parts.length !== 3) { sendJson(res, 400, { error: 'bad job path' }); return; }
    brainPost('/jobs/' + encodeURIComponent(parts[1]) + '/' + encodeURIComponent(parts[2]), {}, 15000)
      .then((j) => sendJson(res, 200, j))
      .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/edge-proposals.json') {
    brainApi('/graph/edge-proposals', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/tags.json') {
    brainApi('/tags', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/sessions.json') {   // all conversation sessions (for graph session nodes)
    brainApi('/sessions', 10000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/tool-usage.json') {   // per-agent brain-tool-usage counts (action_log) for the adoption panel
    const qs = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';
    brainApi('/stats/tool-usage' + qs, 8000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // decide on a queued edge-type proposal (approver identity). {edge_id, decision: approve|reject}
  if (url === '/edge-decide' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      brainPost('/graph/edge-proposals/decide', u, 8000)
        .then((j) => sendJson(res, 200, j))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // per-agent config surface — list, session-start injection preview, edit/revoke.
  if (url === '/agents.json') {
    brainApi('/agents', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url.startsWith('/agent-preview.json')) {
    const q = new URLSearchParams(req.url.split('?')[1] || '');
    const nm = q.get('agent') || '';
    if (!/^[A-Za-z0-9@._-]{1,64}$/.test(nm)) { sendJson(res, 400, { error: 'bad agent' }); return; }
    brainApi('/agent/' + encodeURIComponent(nm) + '/injection-preview', 8000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/agent-save' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      let p;
      if (u.kind === 'global_overlay') {
        p = brainPost('/session-overlay', { scope: 'global', text: String(u.text || '') }, 8000);
      } else {
        const nm = String(u.name || '');
        if (!/^[A-Za-z0-9@._-]{1,64}$/.test(nm)) { sendJson(res, 400, { error: 'bad agent name' }); return; }
        if (u.kind === 'revoke') {
          p = brainPost('/agent/' + encodeURIComponent(nm) + '/revoke', { unrevoke: !!u.unrevoke }, 8000);
        } else {
          const fields = {};
          ['role', 'lane', 'sensitivity', 'welcome'].forEach((k) => { if (k in u) fields[k] = u[k]; });
          if ('agent_tier' in u) fields.agent_tier = u.agent_tier;
          if ('autoapprove_own' in u) fields.autoapprove_own = !!u.autoapprove_own;
          p = brainPatch('/agent/' + encodeURIComponent(nm), fields, 8000);
        }
      }
      p.then((j) => sendJson(res, 200, Object.assign({ ok: true }, j)))
       .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // relation-classifier knobs (graph.yaml) for the Config tab.
  if (url === '/graph-config.json') {
    brainApi('/graph-config', 6000).then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/graph-config' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      brainPost('/graph-config', u, 8000)
        .then((j) => sendJson(res, 200, j))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  // fleetmem skill corpus browser (ships with fleetmem). /skills.json = light list of all skills;
  // /skill-body.json?name= = the full body of one skill (proxies POST /skill/get).
  if (url === '/skills.json') {
    brainApi('/skill/list', 6000)
      .then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/skill-body.json') {
    const name = new URLSearchParams(req.url.split('?')[1] || '').get('name');
    if (!name) { sendJson(res, 400, { error: 'name required' }); return; }
    brainPost('/skill/get', { name }, 6000)
      .then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  // brain runtime-config surface for the Config tab. GET proxies /config; POST proxies the
  // brain's PATCH /config. Both use the dashboard's own mTLS+token identity (role=approver, allowed).
  if (url === '/config.json') {
    brainApi('/config', 6000)
      .then((p) => sendJson(res, 200, p))
      .catch((e) => sendJson(res, 502, { error: String((e && e.message) || e) }));
    return;
  }
  if (url === '/config' && req.method === 'POST') {
    let b = ''; req.on('data', (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on('end', () => {
      let u; try { u = JSON.parse(b); } catch (_) { sendJson(res, 400, { error: 'bad json' }); return; }
      brainPatch('/config', u, 8000)
        .then((j) => sendJson(res, 200, j))
        .catch((e) => sendJson(res, 502, { ok: false, error: String((e && e.message) || e) }));
    });
    return;
  }
  res.writeHead(404).end('not found');
}).listen(PORT, HOST, () => {
  console.log(`fleetmem dashboard on ${HOST}:${PORT}`);
  if (!/^(127\.|::1$|localhost$)/.test(HOST)) {
    console.warn(`[SECURITY] dashboard bound to ${HOST} (non-loopback): it holds a full-console brain `
               + `credential — front it with an mTLS proxy or it exposes the entire brain.`);
  }
});
