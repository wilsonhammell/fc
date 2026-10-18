// The review station: the bulk designer, opened on every form of the
// handover, with a verdict per form - approved, or needs changes and why -
// written to designer/verdicts.json, where the next round of fixes reads it.
//
//   node designer/review_server.mjs [--port 5175] [--no-open]
//   (or Review.cmd, in the repo root)
//
// Serves the bulk designer's build (form_requirements_bulk/dist, built with
// `npm run build` there), the repo's dbFields.json in place of the build's,
// and:
//   GET  api/review/forms              every form: name, where, sha1 of its bytes
//   GET  api/review/file?path=...      a form's bytes (handover files only)
//   GET  api/review/verdicts           verdicts.json
//   PUT  api/review/verdicts           {name, status, note, sha1}: one form's verdict
//   GET  api/review/changes?name=...   what changed on the form since its verdict
// A verdict keeps the sha1 of the file it was given on, so a form rebuilt
// since reads as "rebuilt since" rather than still approved.
//
// Depends on nothing but Node.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { exec } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.dirname(HERE);
const HANDOVER = path.join(REPO, 'handover');
const APP = path.resolve(REPO, '..', 'form_requirements_bulk', 'dist');
const VERDICTS = path.join(HERE, 'verdicts.json');
const args = process.argv.slice(2);
const PORT = Number(args[args.indexOf('--port') + 1]) || 5175;

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.pdf': 'application/pdf', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };

// The designer output of each form in the table, and of the five that are not
// (their own folder, the designer output beside the cleaned one).
function forms() {
  const out = [];
  const designed = path.join(HANDOVER, '2 designer output');
  for (const name of fs.existsSync(designed) ? fs.readdirSync(designed) : []) {
    const pdf = path.join(designed, name, `${name}.pdf`);
    if (fs.existsSync(pdf)) out.push({ name, file: pdf, group: 'in the form table' });
  }
  const apart = path.join(HANDOVER, 'not in the form table');
  for (const name of fs.existsSync(apart) ? fs.readdirSync(apart) : []) {
    const pdf = path.join(apart, name, `${name} - designer output.pdf`);
    if (fs.existsSync(pdf)) out.push({ name, file: pdf, group: 'not in the form table' });
  }
  // make_handover.py's fingerprints: what each form is, the same across
  // builds of an unchanged form. The bytes differ on every build (the
  // designer's generatedAt, the file ID), so a byte hash put every approved
  // form back up for review after each round.
  let prints = {};
  try {
    prints = JSON.parse(fs.readFileSync(path.join(HANDOVER, 'fingerprints.json'), 'utf8'));
  } catch { /* none: fall back to the bytes */ }
  return out.map((f) => {
    const rel = path.relative(HANDOVER, f.file).split(path.sep).join('/');
    return {
      name: f.name,
      group: f.group,
      path: rel,
      sha1: prints[rel] ?? crypto.createHash('sha1').update(fs.readFileSync(f.file)).digest('hex'),
    };
  });
}

function readJson(file, fallback = {}) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function readVerdicts() {
  return readJson(VERDICTS);
}

// What changed since a verdict. Each verdict keeps the snapshot of the build
// it was given on (verdict_snapshots.json); make_handover.py writes the
// current ones (handover/snapshots.json). Verdicts given before snapshots
// existed fall back to the list worked out from the mappings' history
// (verdict_changes.json).
const SNAPSHOTS = path.join(HERE, 'verdict_snapshots.json');
const TRANSITION = path.join(HERE, 'verdict_changes.json');
const currentSnapshots = () => readJson(path.join(HANDOVER, 'snapshots.json'));

function rule(f) {
  if (f.custom) return `custom: ${f.note}`;
  return f.save ? 'required' : 'not required';
}

function diffSnapshots(old, cur) {
  const out = [];
  const names = [...new Set([...Object.keys(old.fields), ...Object.keys(cur.fields)])].sort();
  for (const n of names) {
    const a = old.fields[n];
    const b = cur.fields[n];
    if (!a) { out.push({ field: n, what: `added (${rule(b)})` }); continue; }
    if (!b) { out.push({ field: n, what: 'removed' }); continue; }
    const ch = [];
    if (rule(a) !== rule(b)) ch.push(`save validation: ${rule(a)} → ${rule(b)}`);
    if (a.load !== b.load) ch.push(b.load ? 'now load validated' : 'no longer load validated');
    if (a.editable !== b.editable) ch.push(b.editable ? 'now editable' : 'now read only');
    if (a.format !== b.format) ch.push(`format: ${a.format} → ${b.format}`);
    if ((a.maxLength || 0) !== (b.maxLength || 0)) ch.push(`character limit: ${a.maxLength || 'none'} → ${b.maxLength || 'none'}`);
    if (!!a.multiline !== !!b.multiline) ch.push(b.multiline ? 'now multiline' : 'now one line');
    if ((a.label || '') !== (b.label || '') && (b.save || b.custom)) ch.push(`error message name: "${b.label}"`);
    if (JSON.stringify(a.boxes) !== JSON.stringify(b.boxes)) {
      ch.push(a.boxes.length !== b.boxes.length ? `placed ${b.boxes.length}× (was ${a.boxes.length}×)` : 'moved or resized');
    }
    if (ch.length) out.push({ field: n, what: ch.join('; ') });
  }
  const pages = Math.max(old.pages.length, cur.pages.length);
  if (old.pages.length !== cur.pages.length) out.push({ what: `${cur.pages.length} page(s), was ${old.pages.length}` });
  else for (let i = 0; i < pages; i += 1) {
    if (old.pages[i] !== cur.pages[i]) out.push({ page: i + 1, what: `page ${i + 1}: printed content changed` });
  }
  return out;
}

function changesFor(name) {
  const old = readJson(SNAPSHOTS)[name];
  const cur = currentSnapshots()[name];
  if (old && cur) return { basis: 'snapshot', changes: diffSnapshots(old, cur) };
  const listed = readJson(TRANSITION)[name];
  if (listed) return { basis: 'history', changes: listed };
  return { basis: 'none', changes: [] };
}

function send(res, code, body, type = 'application/json') {
  res.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store' });
  res.end(typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}

function body(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch (e) { reject(e); } });
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = decodeURIComponent(url.pathname);
  try {
    if (p === '/api/review/forms') return send(res, 200, forms());
    if (p === '/api/review/file') {
      const file = path.resolve(HANDOVER, url.searchParams.get('path') ?? '');
      if (!file.startsWith(HANDOVER + path.sep) || !file.endsWith('.pdf') || !fs.existsSync(file)) {
        return send(res, 404, { error: 'no such form' });
      }
      return send(res, 200, fs.readFileSync(file), 'application/pdf');
    }
    if (p === '/api/review/verdicts' && req.method === 'GET') return send(res, 200, readVerdicts());
    if (p === '/api/review/changes') return send(res, 200, changesFor(url.searchParams.get('name') ?? ''));
    if (p === '/api/review/verdicts' && req.method === 'PUT') {
      const { name, status, note, sha1 } = await body(req);
      if (!name || !['approved', 'changes', 'none'].includes(status)) return send(res, 400, { error: 'name and status' });
      const all = readVerdicts();
      if (status === 'none' && !note) delete all[name];
      else all[name] = { status, note: note ?? '', sha1: sha1 ?? '', at: new Date().toISOString() };
      // the build this verdict is given on, to say later what changed since
      const snaps = readJson(SNAPSHOTS);
      const cur = currentSnapshots()[name];
      if (status !== 'none' && cur) snaps[name] = cur;
      else delete snaps[name];
      fs.writeFileSync(SNAPSHOTS, JSON.stringify(snaps) + '\n');
      // sorted, so a diff of the file shows what a review session changed
      const sorted = Object.fromEntries(Object.keys(all).sort().map((k) => [k, all[k]]));
      fs.writeFileSync(VERDICTS, JSON.stringify(sorted, null, 1) + '\n');
      return send(res, 200, sorted[name] ?? {});
    }
    // the repo's DB list, the real one, in place of whatever the build carries
    if (p === '/dbFields.json') return send(res, 200, fs.readFileSync(path.join(REPO, 'dbFields.json')));
    const file = path.resolve(APP, '.' + (p === '/' ? '/index.html' : p));
    if (file.startsWith(APP) && fs.existsSync(file) && fs.statSync(file).isFile()) {
      return send(res, 200, fs.readFileSync(file), TYPES[path.extname(file)] ?? 'application/octet-stream');
    }
    return send(res, 404, 'not found', 'text/plain');
  } catch (err) {
    return send(res, 500, { error: String(err) });
  }
});

if (!fs.existsSync(path.join(APP, 'index.html'))) {
  console.error(`No build at ${APP}. Run "npm run build" in form_requirements_bulk first.`);
  process.exit(1);
}
server.listen(PORT, '127.0.0.1', () => {
  const at = `http://localhost:${PORT}/`;
  console.log(`Review station at ${at} - ${forms().length} forms; verdicts go to ${VERDICTS}`);
  console.log('Leave this window open while you review.');
  if (!args.includes('--no-open')) exec(`start "" "${at}"`);
});
