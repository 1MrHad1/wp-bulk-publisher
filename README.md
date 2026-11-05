# wp-bulk-publisher

Bulk publish articles from a CSV to WordPress through the REST API.

Reads a CSV, validates every row offline, posts the valid ones, and writes a
per-row report saying what happened to each.

## Why it is built the way it is

Three rules drove most of the decisions:

1. **A bad row must not stop the run.** If row 40 is broken, rows 41 to 100
   still need to go out, and there needs to be a record of why 40 did not.
2. **Running it twice must not duplicate everything.** Re-running after a
   half-finished run is the normal thing a person does, so it has to be safe.
3. **Nothing goes live on its own.** Everything lands as a draft unless
   scheduling is asked for explicitly.

## Running it

```bash
pip install requests

export WP_URL="https://client.example.com"
export WP_USER="automation"
export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"

python publish_articles.py articles.csv
```

| Flag | What it does |
|---|---|
| `--dry-run` | Validates every row and posts nothing. No credentials needed. |
| `--schedule` | Schedules posts for `publish_date` instead of leaving drafts. |
| `--rate 0.5` | Minimum seconds between API requests. Default 1.0. |
| `--report-prefix run-01` | Filename prefix for the report files. |

## Running it without a WordPress site

`mock_wp.py` stands in for the API. It deliberately returns a 503 on the first
POST and reports one slug as already existing, so the retry path and the
duplicate path actually run rather than just existing in the file.

```bash
python mock_wp.py &

WP_URL="http://127.0.0.1:8099" WP_USER="automation" WP_APP_PASSWORD="test test test test" \
  python publish_articles.py articles.csv --rate 0.1 --schedule
```

Output from that run:

```
    transient failure (HTTP 503), retrying in 2.0s [attempt 1/4]
[2] POST  audit-wordpress-crawl-waste: created as 501
[3] POST  five-schema-types-sites-get-wrong: created as 502
[4] SKIP  complete-technical-seo-audit-guide: title is 108 chars, max 60
[5] SKIP  internal-linking-programmatic-pages: meta_description is 247 chars, max 160
[6] SKIP  Core Web Vitals / INP_update!: slug must be lowercase letters, numbers and single hyphens
[7] SKIP  robots-txt-rules-that-deindex: publish_date 2026-10-01T09:00:00+00:00 is in the past
[8] SKIP  share-of-voice-ai-overviews: category 'press-releases' is not in ['blog', 'guides', 'news']
[9] SKIP  canonical-tags-paginated-archives: missing required field: body_html
[10] SKIP november-core-update-what-changed: slug already exists as post 9001

Summary: posted=2, skipped_duplicate=1, validation_failed=6
```

`report.json` and `report.csv` are the real output of that run.

## What the sample CSV covers

Nine rows: three valid, and one row per validation rule so every branch is hit.

| Row | What it tests |
|---|---|
| 2, 3 | Valid, posted |
| 4 | Title over 60 characters |
| 5 | Meta description over 160 characters |
| 6 | Slug with spaces, a slash and an underscore |
| 7 | `publish_date` in the past |
| 8 | Category outside the allow-list |
| 9 | Missing required field (`body_html` empty) |
| 10 | Valid, but the slug already exists on the site |

## The one thing the site needs first

WordPress silently drops post meta that has not been registered for REST.
Without this, the POST returns a clean 201 and the Yoast fields are simply not
saved, with no error anywhere. Drop this in
`wp-content/mu-plugins/register-yoast-meta.php`:

```php
<?php
add_action('init', function () {
    foreach (['_yoast_wpseo_title', '_yoast_wpseo_metadesc'] as $key) {
        register_post_meta('post', $key, [
            'show_in_rest'  => true,
            'single'        => true,
            'type'          => 'string',
            'auth_callback' => function () { return current_user_can('edit_posts'); },
        ]);
    }
});
```

The script reads the meta back off the create response and warns per row when
it did not stick, rather than assuming it worked.

## Design notes

- **Drafts by default.** `--schedule` is opt in.
- **Safe to re-run.** Every slug is checked before posting, so a re-run after a
  partial failure skips what exists instead of duplicating it. This also
  catches the quieter problem: WordPress does not reject a duplicate slug, it
  appends `-2` and returns 201, so the post lands at a URL nobody planned.
- **One row cannot kill the run.** Validation failures and API failures are
  both recorded and the loop continues.
- **Retries** on 429, 5xx and timeouts, with exponential backoff and jitter,
  honouring `Retry-After` when the server sends it.
- **Pacing between every request**, including the lookups, because they count
  against the same rate limit.
- **Categories resolved slug to ID** and cached, so 100 rows in one category
  cause one lookup rather than a hundred.
- **Credentials from the environment only**, never from a flag, because flags
  end up in shell history and in the process list.

## What I would add to productionise this

Secrets into a manager rather than env vars, one Application Password per
client on a dedicated Editor user. An external ID written to post meta so
idempotency survives a retitle, since a slug is a weak key. The report into
Postgres rather than a file, so "what went out last Tuesday" is a query. A
Slack summary after every run, alerting on any `api_failed`, on a high
validation failure rate, and on a run that posted zero rows, which almost
always means auth broke rather than that there was nothing to do. Scheduling on
GitHub Actions so it does not depend on a laptop. Tests against recorded API
responses for the validation and retry paths. And a `--limit` flag with a
confirmation above a certain row count, because the failure worth worrying
about is not a broken script, it is a working script pointed at the wrong
client's URL.
