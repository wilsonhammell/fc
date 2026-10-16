// Every row of the business's forms workbook, read the way the designer's
// "Import Configuration from Form Table" reads it, with what picking that row
// would set: the Config answers and the signing buttons.
//
// Run from the form_requirements root, which is where its code lives:
//   node_modules/.bin/vite-node <this file> <workbook.xlsx> <out.json>
import fs from 'node:fs';
import { readFormTable, prefillFromRow, defaultSheet } from '/src/core/formTable.js';

const [book, out] = process.argv.slice(2);
const sheets = await readFormTable(new Uint8Array(fs.readFileSync(book)));
const first = defaultSheet(sheets)?.name ?? null;
const rows = [];
for (const sheet of sheets) {
  for (const row of sheet.rows) {
    const { config, buttons, notes } = prefillFromRow(row, sheet.titles);
    rows.push({ sheet: sheet.name, row: row.row, formName: row.values.formName, config, buttons, notes });
  }
}
fs.writeFileSync(out, JSON.stringify({ defaultSheet: first, rows }, null, 1));
console.log(`${rows.length} rows from ${sheets.length} sheet(s); the designer opens on ${first}`);
