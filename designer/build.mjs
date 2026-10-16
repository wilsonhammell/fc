// The PDF Requirement Designer's "BA Development" step, run headless on the
// designer's own code: for each job designer/requirements.py wrote, open the
// named form the way the designer does, set what a BA would set, and export
// what the BA exports - the PDF after coding and the requirements workbook.
//
// Run from the form_requirements root, which is where its code lives:
//   node_modules/.bin/vite-node <this file> <jobs folder> <dbFields.json>
// then designer/finish.py, which puts Tab back in reading order.
//
// Left alone, as a BA leaves them: the bodies of CustomSaveValidation and the
// form hooks, which the developer writes. Fields needing custom code carry
// Custom Save Validation and a note saying what the rule is.
import fs from 'node:fs';
import path from 'node:path';
import { importPdf } from '/src/core/pdfImport.js';
import { exportPdf } from '/src/core/pdfExport.js';
import { normalizeSystemFields, buildSystemIndex } from '/src/core/systemFields.js';
import { signingButtons, buildButtonIndex } from '/src/core/signingButtons.js';
import { normalizeDbFields, buildDbIndex } from '/src/core/dbFields.js';
import { createField, createConfig, reconcileFields, serializeDocument, fieldIssues } from '/src/core/schema.js';
import { createMarker, markerIssues } from '/src/core/markers.js';
import { esignClusterRects, ESIGN_ANCHOR, isRuntimeFilledDropdown, specialFieldIssues } from '/src/core/specialFields.js';
import { requirementsWorkbookBytes } from '/src/core/excelExport.js';
import { normalizeFormat } from '/src/core/fieldFormat.js';

const [jobsDir, dbPath] = process.argv.slice(2);
const systemFields = normalizeSystemFields(JSON.parse(fs.readFileSync('public/systemFields.json', 'utf8')));
const systemIndex = buildSystemIndex(systemFields);
const dbIndex = buildDbIndex(normalizeDbFields(JSON.parse(fs.readFileSync(dbPath, 'utf8'))));
const buttons = signingButtons();
const buttonIndex = buildButtonIndex(buttons);

// What a BA sets on a field. Everything else on it is what the form has.
const SET = ['editable', 'loadValidated', 'customerFacingName', 'saveValidated',
  'customSaveValidated', 'note', 'multiline'];

const report = [];
const jobs = fs.readdirSync(jobsDir).filter((n) => n.endsWith('.json')).sort();
for (const name of jobs) {
  const job = JSON.parse(fs.readFileSync(path.join(jobsDir, name), 'utf8'));
  const said = [];
  try {
    const bytes = new Uint8Array(fs.readFileSync(job.source));
    const opened = await importPdf(bytes, dbIndex, systemFields, buttons);

    let fields = opened.fields.map((f) => {
      const want = job.fields[f.name.trim()];
      if (!want || f.systemPlacement) return f;
      const patch = {};
      for (const key of SET) if (key in want) patch[key] = want[key];
      // a format the form already carries is its own, as the designer reads it
      if (want.format && f.type === 'text' && (f.format?.category ?? 'none') === 'none') {
        patch.format = normalizeFormat(want.format);
      }
      return { ...f, ...patch };
    });
    const missing = Object.keys(job.fields).filter((n) => !fields.some((f) => f.name.trim() === n));
    if (missing.length) said.push(`settings for fields not on the form: ${missing.join(', ')}`);

    // the signing buttons the form table gives the form, and the Remote eSign
    // label and code box with its button, hidden as the designer places them
    for (const [id, rect] of Object.entries(job.buttons)) {
      fields.push(createField({ name: id, type: 'button', page: 0, rect, hidden: true, editable: false }));
      if (id === ESIGN_ANCHOR) {
        for (const part of esignClusterRects(rect)) {
          if (fields.some((f) => f.name.trim() === part.name)) continue;
          fields.push(createField({ name: part.name, type: 'text', page: 0, rect: part.rect,
            hidden: true, editable: part.editable }));
        }
      }
    }
    for (const c of job['button clash'] ?? []) said.push(`BUTTON_CLASH: ${c}`);

    fields = reconcileFields(fields, dbIndex);
    const markers = job.markers.map((m) => createMarker(m));
    const config = createConfig(job.config);
    const sourceFileName = `${job['production name']}.pdf`;

    const issues = [
      ...fieldIssues(fields, dbIndex, systemIndex, isRuntimeFilledDropdown),
      ...markerIssues(markers),
      ...specialFieldIssues(fields).map((text) => ({ level: 'error', text })),
    ];
    for (const i of issues) said.push(`${i.level.toUpperCase()}: ${i.text}`);

    const out = await exportPdf({
      originalBytes: bytes, fields, buttons, systemFields,
      meta: { sourceFileName, config, markers },
    });
    for (const w of out.warnings) said.push(`export: ${w}`);
    fs.mkdirSync(job.out, { recursive: true });
    fs.writeFileSync(path.join(job.out, sourceFileName), out.pdfBytes);

    const pageImages = job.images.map((im) => ({
      page: im.page, data: new Uint8Array(fs.readFileSync(im.path)), width: im.width, height: im.height,
    }));
    const xlsx = await requirementsWorkbookBytes({
      doc: serializeDocument(fields, { sourceFileName, config, markers }),
      buttons, systemIndex, buttonIndex, dbIndex, pageImages,
      sheetName: job['production name'],
    });
    fs.writeFileSync(path.join(job.out, `${job['production name']}.xlsx`), xlsx);

    // opened again the way the designer opens an exported form: every
    // setting has to come back, or a BA reopening it would lose it
    const back = await importPdf(out.pdfBytes, dbIndex, systemFields, buttons);
    const count = (list, key) => list.filter((f) => !f.systemPlacement && f[key]).length;
    for (const key of ['loadValidated', 'saveValidated', 'customSaveValidated']) {
      if (count(back.fields, key) !== count(fields, key)) {
        said.push(`ROUND_TRIP: ${key} ${count(fields, key)} set, ${count(back.fields, key)} came back`);
      }
    }
    if (back.source !== 'embedded') said.push(`ROUND_TRIP: reopened from ${back.source}, not its requirements`);
    report.push({ form: job['production name'], ok: true, fields: fields.length,
      load: count(fields, 'loadValidated'), save: count(fields, 'saveValidated'),
      custom: count(fields, 'customSaveValidated'), buttons: Object.keys(job.buttons),
      markers: markers.length, said });
  } catch (err) {
    report.push({ form: job['production name'], ok: false, said: [...said, `FAILED: ${err.stack ?? err}`] });
  }
  const r = report[report.length - 1];
  console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.form}` + (r.ok ? ` - ${r.load} load, ${r.save} save, ${r.custom} custom` : ''));
  for (const s of r.said) console.log(`       ${s.split('\n')[0]}`);
}
fs.writeFileSync(path.join(jobsDir, '..', 'build_report.json'), JSON.stringify(report, null, 1));
console.log(`${report.filter((r) => r.ok).length} of ${report.length} built`);
