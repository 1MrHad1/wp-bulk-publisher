#!/usr/bin/env node
/**
 * Publish articles from a CSV to WordPress via the REST API.
 *
 * Read the CSV, check every row offline, post the ones that pass, write a
 * report. Three rules shaped it: a bad row must not stop the run, running it
 * twice must not duplicate everything, and nothing goes live on its own.
 *
 *   WP_URL=https://client.com WP_USER=automation WP_APP_PASSWORD="xxxx ...." \
 *     node publish-articles.mjs articles.csv [--dry-run] [--schedule] [--rate=1]
 *
 * Node 18+. No dependencies: fetch is built in.
 *
 * This file is only the command line wrapper. The parsing, validation and
 * publishing logic lives in lib.mjs, which has no filesystem or network calls
 * of its own, so it can be tested without a WordPress site and run in a
 * browser. The live demo page imports exactly that file.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { REQUIRED, parseCsv, publish, reportCsv, summarise, WordPress } from './lib.mjs';

function need(name) {
  const v = process.env[name];
  // Credentials come from the environment and never from a flag: flags end up in
  // shell history and in the process list where other users can read them.
  if (!v) { console.error(`Missing environment variable ${name}.`); process.exit(1); }
  return v;
}

async function main() {
  const args = process.argv.slice(2);
  const file = args.find(a => !a.startsWith('--'));
  const dryRun = args.includes('--dry-run');
  const schedule = args.includes('--schedule');
  const rateMs = Number(args.find(a => a.startsWith('--rate='))?.split('=')[1] ?? 1) * 1000;

  if (!file) {
    console.error('usage: node publish-articles.mjs <csv> [--dry-run] [--schedule] [--rate=1]');
    process.exit(1);
  }

  const rows = parseCsv(readFileSync(file, 'utf8'));
  if (!rows.length) { console.error('No rows in the CSV.'); process.exit(1); }

  const missing = REQUIRED.filter(f => !(f in rows[0]));
  if (missing.length) { console.error(`CSV is missing columns: ${missing}`); process.exit(1); }

  const wp = dryRun ? null : new WordPress({
    url: need('WP_URL'), user: need('WP_USER'), password: need('WP_APP_PASSWORD'), rateMs,
  });

  console.log(`Processing ${rows.length} rows from ${file}${dryRun ? ' (dry run)' : ''}\n`);
  const report = await publish(rows, { wp, schedule, dryRun });

  writeFileSync('report.json', JSON.stringify(report, null, 2));
  writeFileSync('report.csv', reportCsv(report));
  console.log('\nReports written to report.json and report.csv');
  console.log('Summary: ' + Object.entries(summarise(report)).map(([k, v]) => `${k}=${v}`).join(', '));
}

main();
