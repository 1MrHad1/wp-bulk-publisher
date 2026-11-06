/**
 * The parts of the publisher that have no opinion about where they run.
 *
 * Nothing in here touches the filesystem or the network directly, so the same
 * code runs under Node and in a browser, and the validation rules can be tested
 * without a WordPress site anywhere near them. The CLI is in
 * publish-articles.mjs; the live demo page imports this file directly.
 */

export const REQUIRED = ['title', 'slug', 'meta_title', 'meta_description',
                         'body_html', 'primary_keyword', 'publish_date', 'category'];
export const CATEGORIES = ['blog', 'guides', 'news'];
export const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;   // lowercase, single hyphens between words

// Yoast keeps the SEO title and description in ordinary post meta. The trap is
// that WordPress silently ignores meta keys not registered with show_in_rest,
// so you get a clean 201 and no SEO fields and no error. The site needs the
// mu-plugin in the README, and yoastSaved() below checks rather than assumes.
export const YOAST_TITLE = '_yoast_wpseo_title';
export const YOAST_DESC = '_yoast_wpseo_metadesc';

// Worth retrying: the server having a bad moment. A 400 or 401 is my mistake,
// and retrying a mistake just makes the same mistake four times.
export const RETRY_ON = new Set([429, 500, 502, 503, 504]);
export const MAX_ATTEMPTS = 4;
export const TIMEOUT = 30_000;

export const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Minimal RFC4180 parse: handles quoted fields, embedded commas and newlines. */
export function parseCsv(text) {
  const rows = [];
  let row = [], field = '', quoted = false;
  text = text.replace(/^﻿/, '');  // Excel writes an invisible BOM; without this
                                       // the first column is named "﻿title" and
                                       // every row looks like it has no title.
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    }
    else if (c === '"') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else if (c !== '\r') field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }

  const [head, ...body] = rows;
  if (!head) return [];
  return body
    .filter(r => r.some(v => v.trim()))
    .map(r => Object.fromEntries(head.map((h, i) => [h.trim(), (r[i] ?? '').trim()])));
}

/** Every reason this row cannot be posted. Empty array means it is good. */
export function validate(row) {
  const errors = [];
  for (const f of REQUIRED) if (!row[f]) errors.push(`missing required field: ${f}`);
  if (errors.length) return errors;   // length checks on absent fields just confuse

  if (row.title.length > 60) errors.push(`title is ${row.title.length} chars, max 60`);
  if (row.meta_title.length > 60) errors.push(`meta_title is ${row.meta_title.length} chars, max 60`);
  if (row.meta_description.length > 160)
    errors.push(`meta_description is ${row.meta_description.length} chars, max 160`);
  if (!SLUG.test(row.slug))
    errors.push(`slug '${row.slug}' must be lowercase letters, numbers and single hyphens`);
  if (!CATEGORIES.includes(row.category))
    errors.push(`category '${row.category}' is not in ${JSON.stringify(CATEGORIES)}`);

  const date = new Date(row.publish_date);
  if (isNaN(date)) errors.push(`publish_date '${row.publish_date}' is not a valid date`);
  else if (date <= new Date()) errors.push(`publish_date ${row.publish_date} is in the past`);

  return errors;
}

export function buildPayload(row, categoryId, schedule) {
  const body = {
    title: row.title,
    slug: row.slug,
    content: row.body_html,
    categories: [categoryId],
    status: 'draft',    // always. A script that publishes on its first run gives
                        // nobody a chance to read 100 articles before readers do.
    meta: { [YOAST_TITLE]: row.meta_title, [YOAST_DESC]: row.meta_description },
  };
  if (schedule) {
    // Scheduling needs both halves. "future" with no date never publishes; a date
    // with status "draft" is ignored. date_gmt so the time means the same thing
    // whatever timezone the site is set to.
    body.status = 'future';
    body.date_gmt = new Date(row.publish_date).toISOString().slice(0, 19);
  }
  return body;
}

/** Did the Yoast meta actually save, or was it silently dropped? */
export function yoastSaved(created, row) {
  return created.meta?.[YOAST_TITLE] === row.meta_title
      && created.meta?.[YOAST_DESC] === row.meta_description;
}

export class Err extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export class WordPress {
  /**
   * @param {object} opts
   * @param {function} [opts.fetchImpl] injected for tests and for the demo page,
   *   which supplies a mock in place of a real WordPress site. Defaults to the
   *   platform's own fetch.
   * @param {function} [opts.log] where progress lines go. Defaults to console.
   */
  constructor({ url, user, password, rateMs = 1000, fetchImpl, log } = {}) {
    this.base = url.replace(/\/$/, '') + '/wp-json/wp/v2';
    this.auth = 'Basic ' + btoa(`${user}:${password}`);
    this.rateMs = rateMs;
    this.fetch = fetchImpl ?? globalThis.fetch;
    this.log = log ?? (m => console.log(m));
    this.lastCall = 0;
    this.categories = new Map();   // slug -> id, so 100 rows cause one lookup
  }

  /** Keep requests at least rateMs apart. Applies to lookups too: they count
   *  against the same rate limit, so exempting them would defeat the point. */
  async pace() {
    const wait = this.rateMs - (Date.now() - this.lastCall);
    if (wait > 0) await sleep(wait);
    this.lastCall = Date.now();
  }

  async request(method, path, { params, body } = {}) {
    const url = new URL(this.base + path);
    for (const [k, v] of Object.entries(params ?? {})) url.searchParams.set(k, v);

    let problem;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      await this.pace();
      let res;
      try {
        res = await this.fetch(url.toString(), {
          method,
          headers: { Authorization: this.auth, 'Content-Type': 'application/json' },
          body: body && JSON.stringify(body),
          signal: AbortSignal.timeout(TIMEOUT),
        });
      } catch (e) {
        problem = e.name === 'TimeoutError' ? 'timeout' : e.message;   // retry these
      }

      if (res && !RETRY_ON.has(res.status)) return res;   // done, good or bad
      if (res) problem = `HTTP ${res.status}`;
      if (attempt === MAX_ATTEMPTS) break;

      // Retry-After if the server sent one, otherwise 2s, 4s, 8s. The random part
      // is jitter: without it, rows that fail together retry together and the
      // server gets the same spike again.
      const after = Number(res?.headers.get('retry-after')) * 1000;
      const wait = after || 2 ** attempt * 1000 + Math.random() * 1000;
      this.log(`    transient failure (${problem}), retrying in ${(wait / 1000).toFixed(1)}s`
             + ` [attempt ${attempt}/${MAX_ATTEMPTS}]`);
      await sleep(wait);
    }
    throw new Err(null, `retries exhausted: ${problem}`);
  }

  async json(method, path, opts) {
    const res = await this.request(method, path, opts);
    if (!res.ok) throw new Err(res.status, (await res.text()).replace(/\s+/g, ' ').slice(0, 200));
    return res.json();
  }

  /** The posts endpoint takes category IDs, not slugs. */
  async categoryId(slug) {
    if (!this.categories.has(slug)) {
      const [found] = await this.json('GET', '/categories', { params: { slug } });
      if (!found) throw new Err(404, `category '${slug}' does not exist on the site`);
      this.categories.set(slug, found.id);
    }
    return this.categories.get(slug);
  }

  /** The line that makes a re-run safe, and the one I would defend hardest.
   *  WordPress does not reject a duplicate slug: it appends "-2" and returns a
   *  cheerful 201, so the post lands at a URL nobody planned and every internal
   *  link built around the original slug points at nothing. status=any matters,
   *  because a draft still occupies the slug. */
  async existingId(slug) {
    const [found] = await this.json('GET', '/posts',
      { params: { slug, status: 'any', per_page: 1 } });
    return found?.id ?? null;
  }
}

/**
 * Process every row. Returns the report.
 *
 * The CLI and the demo page both call this, so what you watch in the browser is
 * the same loop that runs against a real site.
 */
export async function publish(rows, { wp, schedule = false, dryRun = false, log = console.log } = {}) {
  const report = [];

  for (const [i, row] of rows.entries()) {
    const n = i + 2;                       // +2 so it matches the spreadsheet row
    const label = row.slug || `row ${n}`;
    const add = (status, extra) => report.push({ row: n, slug: row.slug, title: row.title, status, ...extra });

    const errors = validate(row);
    if (errors.length) {
      log(`[${n}] SKIP  ${label}: ${errors[0]}`);
      add('validation_failed', { reason: errors.join('; ') });
      continue;
    }
    if (dryRun) { log(`[${n}] OK    ${label} (not posted)`); add('validated_ok'); continue; }

    // One try/catch per row: a bad row is recorded and the loop moves on, rather
    // than taking the other 99 down with it.
    try {
      const existing = await wp.existingId(row.slug);
      if (existing) {
        log(`[${n}] SKIP  ${label}: slug already exists as post ${existing}`);
        add('skipped_duplicate', { reason: `slug already exists as post ${existing}`, post_id: existing });
        continue;
      }

      const created = await wp.json('POST', '/posts',
        { body: buildPayload(row, await wp.categoryId(row.category), schedule) });

      const saved = yoastSaved(created, row);
      log(saved
        ? `[${n}] POST  ${label}: created as ${created.id}`
        : `[${n}] WARN  ${label}: created as ${created.id}, but the Yoast fields did not save`);
      add('posted', { post_id: created.id, link: created.link,
                      ...(saved ? {} : { warning: 'Yoast meta not registered for REST, see README' }) });

    } catch (e) {
      log(`[${n}] FAIL  ${label}: ${e.status ?? ''} ${e.message}`);
      add('api_failed', { status_code: e.status ?? '', reason: e.message });
    }
  }

  return report;
}

export function reportCsv(report) {
  const cols = ['row', 'slug', 'title', 'status', 'reason', 'status_code', 'post_id', 'link'];
  const cell = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  return [cols.join(','), ...report.map(r => cols.map(c => cell(r[c])).join(','))].join('\n');
}

export function summarise(report) {
  const counts = {};
  for (const r of report) counts[r.status] = (counts[r.status] ?? 0) + 1;
  return counts;
}
