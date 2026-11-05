#!/usr/bin/env python3
"""
Bulk publish articles from a CSV to WordPress through the REST API.

The shape of the whole thing is: read the CSV, check every row before touching
the network, post the rows that passed, write down what happened to each one.

Three rules drove most of the decisions in here:

  1. A bad row must not stop the run. If row 40 is broken, rows 41 to 100 still
     need to go out, and I need a record of why 40 did not.
  2. Running it twice must not create two copies of everything. That is the
     normal thing a person does after a half-finished run, so it has to be safe.
  3. Nothing goes live on its own. Everything lands as a draft unless I ask for
     scheduling explicitly.

Usage:
    export WP_URL="https://client.example.com"
    export WP_USER="automation"
    export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"

    python publish_articles.py articles.csv
    python publish_articles.py articles.csv --dry-run
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests


# ===========================================================================
# CONFIGURATION
# Everything tunable lives up here so I never have to hunt through the logic
# to change a limit.
# ===========================================================================

# Every one of these must be present and non-empty or the row is rejected.
REQUIRED_FIELDS = [
    "title", "slug", "meta_title", "meta_description",
    "body_html", "primary_keyword", "publish_date", "category",
]

# The brief fixed these three. A category outside the list is a rejected row,
# not a new category: creating categories automatically is how a client's site
# ends up with "Guides", "guides" and "Guide" as three separate things.
ALLOWED_CATEGORIES = ["blog", "guides", "news"]

TITLE_MAX = 60        # Google truncates titles past roughly this width.
META_TITLE_MAX = 60
META_DESC_MAX = 160   # Same idea for the description snippet.

# A valid slug: lowercase letters and digits, single hyphens between words,
# no hyphen at either end. "my-post" passes. "My Post", "my--post" and
# "my-post-" all fail.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Yoast keeps the SEO title and description in ordinary WordPress post meta.
# These are the exact keys it reads.
#
# The trap: WordPress silently ignores meta keys that have not been registered
# with show_in_rest. The POST comes back 201, the post exists, and the SEO
# fields are just not there. No error anywhere. The site needs the small
# mu-plugin in the README before these will save, and further down I read the
# meta back off the response so the script notices when they did not.
YOAST_TITLE_KEY = "_yoast_wpseo_title"
YOAST_DESC_KEY = "_yoast_wpseo_metadesc"

# Status codes worth trying again. 429 is "slow down", 5xx is the server
# having a bad moment. A 400 or a 401 is my fault and retrying just repeats
# the same mistake more times, so those are not in here.
RETRY_STATUSES = {429, 500, 502, 503, 504}

MAX_ATTEMPTS = 4   # The first try plus three retries.
TIMEOUT = 30       # Seconds. Without this, a hung server hangs the whole run.


# ===========================================================================
# VALIDATION
# This runs entirely offline, before any network call. Catching a bad row here
# costs nothing. Catching it after it is published costs a conversation with
# the client.
# ===========================================================================

def validate_row(row):
    """Check one row and return a list of reasons it cannot be posted.

    An empty list means the row is good. I return every reason rather than
    stopping at the first one, because if a row has three problems the person
    fixing the spreadsheet would rather learn all three now.
    """
    errors = []

    # Presence first. Everything below assumes the fields exist.
    for field in REQUIRED_FIELDS:
        if not (row.get(field) or "").strip():
            errors.append(f"missing required field: {field}")

    # If anything is missing, stop here. Running a length check on a field
    # that is not there would just produce a confusing second error.
    if errors:
        return errors

    if len(row["title"]) > TITLE_MAX:
        errors.append(f"title is {len(row['title'])} chars, max {TITLE_MAX}")

    if len(row["meta_title"]) > META_TITLE_MAX:
        errors.append(f"meta_title is {len(row['meta_title'])} chars, max {META_TITLE_MAX}")

    if len(row["meta_description"]) > META_DESC_MAX:
        errors.append(
            f"meta_description is {len(row['meta_description'])} chars, max {META_DESC_MAX}"
        )

    if not SLUG_RE.match(row["slug"]):
        errors.append(
            f"slug '{row['slug']}' must be lowercase letters, numbers and single hyphens"
        )

    if row["category"] not in ALLOWED_CATEGORIES:
        errors.append(f"category '{row['category']}' is not in {ALLOWED_CATEGORIES}")

    # The brief says these are scheduled for a future publish, so a date that
    # has already passed is a data problem. Posting it anyway would either
    # publish it immediately or back-date it, and both are wrong.
    publish_date = parse_date(row["publish_date"])
    if publish_date is None:
        errors.append(f"publish_date '{row['publish_date']}' is not a valid ISO 8601 date")
    elif publish_date <= datetime.now(timezone.utc):
        errors.append(f"publish_date {row['publish_date']} is in the past")

    return errors


def parse_date(value):
    """Parse an ISO 8601 date string and always hand back UTC.

    Returns None instead of raising, because an unparseable date is a
    validation result, not a crash.

    Two awkward details handled here:
      - Python's fromisoformat historically rejects a trailing "Z", so it gets
        swapped for "+00:00" first.
      - A date with no timezone is assumed to be UTC. Guessing the server's
        local timezone is how posts end up scheduled five and a half hours off.
    """
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


# ===========================================================================
# THE WORDPRESS CLIENT
# Everything that touches the network lives in this class, so the rest of the
# file can be read without thinking about HTTP at all.
# ===========================================================================

class WordPressClient:
    """Talks to the WP REST API, with pacing, retries and a duplicate check."""

    def __init__(self, base_url, user, app_password, rate_delay=1.0):
        # rstrip the trailing slash first, otherwise a URL ending in "/" gives
        # a double slash in every path and some hosts redirect or 404 on that.
        self.base = base_url.rstrip("/") + "/wp-json/wp/v2"

        # A Session reuses the underlying TCP connection across requests
        # instead of opening a new one per post. Over 100 posts that is a
        # noticeable difference, and it keeps the auth header in one place.
        self.session = requests.Session()

        # WordPress Application Passwords are sent as ordinary HTTP Basic auth.
        # They are not the user's login password: they are generated per
        # integration and revoked individually, which is the whole point.
        self.session.auth = (user, app_password)
        self.session.headers["User-Agent"] = "wp-bulk-publisher/1.0"

        self.rate_delay = rate_delay

        # Category slug to ID, remembered so 100 rows in "guides" cause one
        # lookup rather than one hundred identical ones.
        self._category_cache = {}

        # Used by _pace() to work out how long ago the last request went out.
        self._last_request_at = 0.0

    # -- the plumbing ------------------------------------------------------

    def _pace(self):
        """Wait, if needed, so requests are at least rate_delay apart.

        monotonic() rather than time() because it cannot jump backwards if the
        machine's clock is adjusted mid-run, which would make the maths
        produce a negative wait or an enormous one.
        """
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.rate_delay:
            time.sleep(self.rate_delay - elapsed)
        self._last_request_at = time.monotonic()

    def _request(self, method, path, **kwargs):
        """Make one request, retrying transient failures with backoff.

        Everything in this class goes through here, so pacing, timeouts and
        retries apply to the lookups as well as the posts. The lookups count
        against the same rate limit, so exempting them would defeat the point.

        Returns the response for anything that is not worth retrying, which
        includes real errors like 401 and 400. Judging those is the caller's
        job. Raises TransientFailure only when the retries are used up.
        """
        url = f"{self.base}{path}"
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()

            try:
                response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                # A timeout or a dropped connection is exactly the kind of
                # thing that works on the next attempt, so it is treated the
                # same as a 503 rather than as a crash.
                last_error = f"{type(exc).__name__}: {exc}"
                response = None

            if response is not None:
                if response.status_code not in RETRY_STATUSES:
                    return response          # Done, good or bad.
                last_error = f"HTTP {response.status_code}"

            if attempt == MAX_ATTEMPTS:
                break                        # Out of attempts, give up below.

            wait = self._backoff(attempt, response)
            log(f"    transient failure ({last_error}), retrying in {wait:.1f}s "
                f"[attempt {attempt}/{MAX_ATTEMPTS}]")
            time.sleep(wait)

        raise TransientFailure(last_error or "unknown transient failure")

    @staticmethod
    def _backoff(attempt, response):
        """How long to wait before trying again.

        If the server sent a Retry-After header it has told us exactly how
        long to wait, and guessing something shorter is just rude and usually
        gets throttled harder.

        Otherwise: 2s, 4s, 8s, doubling each time so a struggling server gets
        more room rather than less. The random fraction on the end is jitter.
        Without it, if several rows fail at the same moment they all retry at
        the same moment too, and the server gets the same spike again.
        """
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass  # Retry-After can be a date. Fall through to backoff.

        return (2 ** attempt) + random.uniform(0, 1)

    # -- the actual API calls ---------------------------------------------

    def category_id(self, slug):
        """Turn a category slug into the numeric ID the API wants.

        The posts endpoint takes category IDs, not slugs, so this lookup has
        to happen. The result is cached per slug for the life of the run.
        """
        if slug in self._category_cache:
            return self._category_cache[slug]

        response = self._request("GET", "/categories", params={"slug": slug})
        if response.status_code != 200:
            raise ApiFailure(response.status_code, truncate(response.text))

        results = response.json()
        if not results:
            # The brief says categories already exist, so an empty result
            # means the data is wrong. I do not create it: see the note on
            # ALLOWED_CATEGORIES above.
            raise ApiFailure(404, f"category '{slug}' does not exist on the site")

        self._category_cache[slug] = results[0]["id"]
        return results[0]["id"]

    def existing_post_id(self, slug):
        """Return the ID of a post already using this slug, or None.

        This single call is what makes the script safe to re-run, and it is
        the part I would defend hardest.

        Two separate problems it solves:

        First, re-running after a partial failure. Without this check, the
        second run creates a duplicate of everything that worked the first
        time, and somebody deletes them by hand.

        Second, and worse, WordPress does not reject a duplicate slug. It
        quietly appends "-2" and returns a cheerful 201. So you get the post,
        at a URL nobody planned, and every internal link and sitemap entry
        built around the original slug is now pointing at nothing. It does not
        look like a failure anywhere in the logs.

        status=any is important: a draft with that slug still occupies it, and
        the default search only looks at published posts.
        """
        response = self._request(
            "GET", "/posts", params={"slug": slug, "status": "any", "per_page": 1}
        )
        if response.status_code != 200:
            raise ApiFailure(response.status_code, truncate(response.text))

        results = response.json()
        return results[0]["id"] if results else None

    def create_post(self, payload):
        """Create one post. 200 and 201 both mean it worked."""
        response = self._request("POST", "/posts", json=payload)
        if response.status_code not in (200, 201):
            raise ApiFailure(response.status_code, truncate(response.text))
        return response.json()


# Two different failure types, because they need different handling and
# different wording in the report.

class TransientFailure(Exception):
    """Retries ran out. Probably worth running the row again later."""


class ApiFailure(Exception):
    """The API refused the request. Running it again will not help."""

    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


def truncate(text, limit=200):
    """Flatten an error body to one short line fit for a CSV cell.

    WordPress error responses can be an entire HTML page. The split/join
    collapses all the whitespace and newlines first.
    """
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


# ===========================================================================
# BUILDING THE REQUEST
# ===========================================================================

def build_payload(row, category_id, schedule):
    """Turn one validated row into the JSON body the posts endpoint expects."""
    payload = {
        "title": row["title"],
        "slug": row["slug"],
        "content": row["body_html"],
        "categories": [category_id],   # A list: a post can have several.

        # Draft by default, always. A script that publishes on its first run
        # gives nobody a chance to look at 100 articles before readers do.
        "status": "draft",

        "meta": {
            YOAST_TITLE_KEY: row["meta_title"],
            YOAST_DESC_KEY: row["meta_description"],
        },
    }

    if schedule:
        # Scheduling in WordPress needs both halves. status "future" with no
        # date never publishes, and a date with status "draft" is ignored
        # completely. Only the pair works.
        #
        # date_gmt rather than date, so the time means the same thing no
        # matter what timezone the site is configured for. parse_date has
        # already converted it to UTC.
        payload["status"] = "future"
        payload["date_gmt"] = parse_date(row["publish_date"]).strftime("%Y-%m-%dT%H:%M:%S")

    return payload


def yoast_fields_landed(created, row):
    """Did the Yoast meta actually save?

    Checked by reading the meta back off the create response rather than
    assuming, because unregistered meta keys are dropped without any error.
    This is the difference between "the script said it worked" and "it worked".
    """
    meta = created.get("meta") or {}
    return (
        meta.get(YOAST_TITLE_KEY) == row["meta_title"]
        and meta.get(YOAST_DESC_KEY) == row["meta_description"]
    )


# ===========================================================================
# THE RUN
# ===========================================================================

def run(args):
    # utf-8-sig, not plain utf-8. A CSV exported from Excel starts with an
    # invisible byte order mark, and with plain utf-8 the first column name
    # comes through as "﻿title", so every lookup of "title" fails and
    # every row looks like it is missing its title. utf-8-sig strips it.
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        log("No rows found in the CSV.")
        return 1

    # Check the headers once, up front. Finding out on row 1 that the whole
    # file has the wrong shape beats finding out 100 times.
    missing_columns = set(REQUIRED_FIELDS) - set(rows[0].keys())
    if missing_columns:
        log(f"CSV is missing these columns: {sorted(missing_columns)}")
        return 1

    # In dry-run there is no client at all, so there is no way for a stray
    # network call to sneak out and no credentials needed to run it.
    client = None
    if not args.dry_run:
        client = WordPressClient(
            base_url=require_env("WP_URL"),
            user=require_env("WP_USER"),
            app_password=require_env("WP_APP_PASSWORD"),
            rate_delay=args.rate,
        )

    results = []
    log(f"Processing {len(rows)} rows from {args.csv_path}"
        f"{' (dry run)' if args.dry_run else ''}\n")

    # start=2 so the number printed matches the spreadsheet row, counting the
    # header as row 1. When somebody has to go and fix the data, "row 7" should
    # mean row 7 in the file they are looking at.
    for index, row in enumerate(rows, start=2):
        # Strip every value once here. Trailing spaces from a spreadsheet
        # would otherwise break the slug pattern for no visible reason.
        row = {k: (v or "").strip() for k, v in row.items()}
        label = row.get("slug") or f"row {index}"

        errors = validate_row(row)
        if errors:
            log(f"[{index}] SKIP  {label}: {errors[0]}")
            results.append(result(index, row, "validation_failed", reason="; ".join(errors)))
            continue

        if args.dry_run:
            log(f"[{index}] OK    {label} (not posted, dry run)")
            results.append(result(index, row, "validated_ok"))
            continue

        # One try/except around the whole network section, per row. This is
        # rule 1 from the top of the file: a row that fails gets recorded and
        # the loop moves on, rather than taking the other 99 down with it.
        try:
            existing = client.existing_post_id(row["slug"])
            if existing:
                log(f"[{index}] SKIP  {label}: slug already exists as post {existing}")
                results.append(result(
                    index, row, "skipped_duplicate",
                    reason=f"slug already exists as post {existing}", post_id=existing,
                ))
                continue

            category_id = client.category_id(row["category"])
            created = client.create_post(build_payload(row, category_id, args.schedule))

            entry = result(index, row, "posted",
                           post_id=created.get("id"), link=created.get("link"))

            # Posted, but the SEO fields may still have been dropped. That is
            # a warning and not a failure: the post is real and the fix is on
            # the site, not in the data.
            if not yoast_fields_landed(created, row):
                entry["warning"] = (
                    "Yoast meta did not come back on the response. The meta keys are "
                    "probably not registered for REST on this site. See README.md."
                )
                log(f"[{index}] WARN  {label}: posted as {created.get('id')}, "
                    f"but the Yoast fields did not save")
            else:
                log(f"[{index}] POST  {label}: created as {created.get('id')}")

            results.append(entry)

        except ApiFailure as exc:
            # The API said no and meant it. Record the status code, because
            # 401 on every row and 400 on one row are very different stories.
            log(f"[{index}] FAIL  {label}: {exc}")
            results.append(result(index, row, "api_failed",
                                  status_code=exc.status_code, reason=exc.message))

        except TransientFailure as exc:
            # Tried four times and the server never came back. Worth re-running
            # this row later, which the report makes possible.
            log(f"[{index}] FAIL  {label}: gave up after {MAX_ATTEMPTS} attempts ({exc})")
            results.append(result(index, row, "api_failed", status_code=None,
                                  reason=f"retries exhausted: {exc}"))

    write_reports(results, args.report_prefix)
    summarise(results)
    return 0


def result(index, row, status, **extra):
    """Build one report entry.

    Every entry carries the same keys whatever happened, so the CSV has no
    ragged rows and a spreadsheet can filter the status column cleanly.
    """
    entry = {
        "row": index,
        "slug": row.get("slug", ""),
        "title": row.get("title", ""),
        "status": status,
        "reason": "",
        "status_code": "",
        "post_id": "",
        "link": "",
    }
    # Drop None values so they do not become the string "None" in the CSV.
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry


def write_reports(results, prefix):
    """Write the same results twice, in the two formats people actually use.

    JSON keeps the warnings and nests properly, and is what another script
    would read. CSV is what a human opens to see which rows failed and why.
    """
    with open(f"{prefix}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    columns = ["row", "slug", "title", "status", "reason", "status_code", "post_id", "link"]
    with open(f"{prefix}.csv", "w", newline="", encoding="utf-8") as f:
        # extrasaction="ignore" so an entry carrying a "warning" key does not
        # blow up the writer, since warning is not one of the CSV columns.
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for entry in results:
            writer.writerow(entry)

    log(f"\nReports written to {prefix}.json and {prefix}.csv")


def summarise(results):
    """One line at the end, because nobody scrolls back through 100 lines."""
    counts = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    log("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def require_env(name):
    """Read a required environment variable or exit with a useful message.

    Credentials come from the environment and never from a flag, because
    anything passed as a flag ends up in the shell history and in the process
    list where any other user on the machine can read it.
    """
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}. See the usage note at the top of this file.")
    return value


def log(message):
    # flush=True so output appears as it happens. Without it Python buffers
    # when the output is piped to a file, and a long run looks frozen.
    print(message, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Publish articles from a CSV to WordPress.")
    parser.add_argument("csv_path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate every row and post nothing.")
    parser.add_argument("--schedule", action="store_true",
                        help="Schedule posts for publish_date instead of leaving them as drafts.")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="Minimum seconds between API requests (default 1.0).")
    parser.add_argument("--report-prefix", default="report",
                        help="Filename prefix for the report files (default 'report').")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
