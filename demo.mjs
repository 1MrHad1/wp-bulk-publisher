/**
 * The demo page's wiring.
 *
 * Everything that matters is imported from lib.mjs, which is the exact file in
 * the submission. The only thing this adds is a stand-in for WordPress: a fetch
 * implementation that answers the three endpoints the publisher calls. It
 * deliberately returns one 503 on the first POST and reports one slug as
 * already taken, so the retry path and the duplicate path actually run.
 */
import { REQUIRED, parseCsv, publish, reportCsv, summarise, WordPress } from './lib.mjs';

const $ = id => document.getElementById(id);

const CATEGORY_IDS = { blog: 11, guides: 12, news: 13 };
const TAKEN_SLUGS = { 'november-core-update-what-changed': 9001 };

/** A fake WordPress. Same interface as fetch, so lib.mjs cannot tell. */
function mockWordPress({ failFirstPost = true, dropYoastMeta = false } = {}) {
  let nextId = 500;
  let failed = false;

  const reply = (status, body) => new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

  return async (url, options = {}) => {
    const u = new URL(url);
    const slug = u.searchParams.get('slug');
    await new Promise(r => setTimeout(r, 40));   // a little latency, so it reads as a network call

    if (options.method === 'GET' || !options.method) {
      if (u.pathname.endsWith('/categories'))
        return reply(200, slug in CATEGORY_IDS ? [{ id: CATEGORY_IDS[slug], slug }] : []);
      if (u.pathname.endsWith('/posts'))
        return reply(200, slug in TAKEN_SLUGS ? [{ id: TAKEN_SLUGS[slug], slug }] : []);
    }

    if (options.method === 'POST' && u.pathname.endsWith('/posts')) {
      if (failFirstPost && !failed) { failed = true; return reply(503, { message: 'service unavailable' }); }
      const body = JSON.parse(options.body ?? '{}');
      return reply(201, {
        id: ++nextId,
        slug: body.slug,
        status: body.status,
        link: `https://client.example.com/${body.slug}/`,
        // The whole point of the Yoast check: a site without the mu-plugin
        // returns 201 with the meta quietly missing.
        meta: dropYoastMeta ? {} : (body.meta ?? {}),
      });
    }

    return reply(404, { message: 'not found' });
  };
}

// ---------------------------------------------------------------- output

const out = $('out');
let lines = [];

function log(message) {
  lines.push(message);
  const cls = /\bPOST\b/.test(message) ? 'ok'
            : /\bWARN\b/.test(message) ? 'warn'
            : /\bFAIL\b/.test(message) ? 'bad'
            : /transient/.test(message) ? 'dim'
            : /\bSKIP\b/.test(message) ? 'skip' : '';
  const row = document.createElement('div');
  row.className = 'line ' + cls;
  row.textContent = message || ' ';
  out.appendChild(row);
  out.scrollTop = out.scrollHeight;
}

function clearOut() {
  lines = [];
  out.textContent = '';
  $('report').innerHTML = '';
  $('summary').innerHTML = '';
}

const STATUS_LABEL = {
  posted: 'posted',
  validation_failed: 'validation failed',
  skipped_duplicate: 'skipped, duplicate',
  api_failed: 'api failed',
  validated_ok: 'validated',
};

function renderReport(report) {
  const counts = summarise(report);
  $('summary').innerHTML = Object.entries(counts).map(([k, v]) =>
    `<span class="chip ${k}"><b>${v}</b> ${STATUS_LABEL[k] ?? k}</span>`).join('');

  const head = '<tr><th>Row</th><th>Slug</th><th>Status</th><th>Reason / result</th></tr>';
  const body = report.map(r => {
    const detail = r.status === 'posted'
      ? `post ${r.post_id}${r.warning ? ' <span class="warn-inline">Yoast meta did not save</span>' : ''}`
      : (r.reason ?? '');
    return `<tr class="r-${r.status}"><td>${r.row}</td><td class="mono">${esc(r.slug)}</td>`
         + `<td><span class="pill ${r.status}">${STATUS_LABEL[r.status] ?? r.status}</span></td>`
         + `<td>${r.status === 'posted' ? detail : esc(detail)}</td></tr>`;
  }).join('');
  $('report').innerHTML = `<table>${head}${body}</table>`;

  $('dl-json').onclick = () => download('report.json', JSON.stringify(report, null, 2), 'application/json');
  $('dl-csv').onclick = () => download('report.csv', reportCsv(report), 'text/csv');
  $('downloads').hidden = false;
}

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------- run

let running = false;

async function run({ dryRun }) {
  if (running) return;
  running = true;
  setBusy(true);
  clearOut();

  try {
    const rows = parseCsv($('csv').value);
    if (!rows.length) { log('No rows in the CSV.'); return; }

    const missing = REQUIRED.filter(f => !(f in rows[0]));
    if (missing.length) { log(`CSV is missing columns: ${missing.join(', ')}`); return; }

    const wp = dryRun ? null : new WordPress({
      url: 'https://client.example.com',
      user: 'automation',
      password: 'demo demo demo demo',
      rateMs: Number($('rate').value),
      fetchImpl: mockWordPress({ dropYoastMeta: $('yoast').checked }),
      log,
    });

    log(`Processing ${rows.length} rows from articles.csv${dryRun ? ' (dry run)' : ''}`);
    log('');

    const report = await publish(rows, { wp, schedule: $('schedule').checked, dryRun, log });

    log('');
    log('Summary: ' + Object.entries(summarise(report)).map(([k, v]) => `${k}=${v}`).join(', '));
    renderReport(report);
  } catch (e) {
    log(`Unexpected error: ${e.message}`);
  } finally {
    running = false;
    setBusy(false);
  }
}

function setBusy(on) {
  for (const id of ['go', 'dry', 'reset']) $(id).disabled = on;
  $('go').textContent = on ? 'Running...' : 'Run against mock WordPress';
}

// ---------------------------------------------------------------- boot

const SAMPLE = await fetch('articles.csv').then(r => r.text());
$('csv').value = SAMPLE;

$('go').onclick = () => run({ dryRun: false });
$('dry').onclick = () => run({ dryRun: true });
$('reset').onclick = () => { $('csv').value = SAMPLE; clearOut(); $('downloads').hidden = true; };

// Break a row on purpose, so the validation is visibly doing something.
$('break').onclick = () => {
  $('csv').value = $('csv').value.replace(
    'audit-wordpress-crawl-waste,WordPress Crawl Waste Audit: A Practical Guide',
    'Audit WordPress Crawl Waste!,WordPress Crawl Waste Audit: A Practical Guide');
  log('Row 2 slug changed to "Audit WordPress Crawl Waste!". Run it again.');
};
