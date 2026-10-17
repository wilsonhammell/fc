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
  return out.map((f) => ({
    name: f.name,
    group: f.group,
    path: path.relative(HANDOVER, f.file).split(path.sep).join('/'),
    sha1: crypto.createHash('sha1').update(fs.readFileSync(f.file)).digest('hex'),
  }));
}

function readVerdicts() {
  try {
    return JSON.parse(fs.readFileSync(VERDICTS, 'utf8'));
  } catch {
    return {};
  }
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
    if (p === '/api/review/verdicts' && req.method === 'PUT') {
      const { name, status, note, sha1 } = await body(req);
      if (!name || !['approved', 'changes', 'none'].includes(status)) return send(res, 400, { error: 'name and status' });
      const all = readVerdicts();
      if (status === 'none' && !note) delete all[name];
      else all[name] = { status, note: note ?? '', sha1: sha1 ?? '', at: new Date().toISOString() };
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
