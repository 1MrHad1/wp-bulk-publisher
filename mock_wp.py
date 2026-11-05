#!/usr/bin/env python3
"""
A throwaway stand-in for the WordPress REST API, so publish_articles.py can be
run end to end without touching a client site.

It deliberately returns one 503 before succeeding, so the retry path gets
exercised rather than just existing in the file.

    python mock_wp.py            # listens on http://127.0.0.1:8099
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

CATEGORIES = {"blog": 11, "guides": 12, "news": 13}
EXISTING_SLUGS = {"november-core-update-what-changed": 9001}  # proves the dedupe path

state = {"next_id": 500, "failed_once": False}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        slug = (query.get("slug") or [""])[0]

        if url.path == "/wp-json/wp/v2/categories":
            if slug in CATEGORIES:
                return self._send(200, [{"id": CATEGORIES[slug], "slug": slug}])
            return self._send(200, [])

        if url.path == "/wp-json/wp/v2/posts":
            if slug in EXISTING_SLUGS:
                return self._send(200, [{"id": EXISTING_SLUGS[slug], "slug": slug}])
            return self._send(200, [])

        return self._send(404, {"message": "not found"})

    def do_POST(self):
        if self.path != "/wp-json/wp/v2/posts":
            return self._send(404, {"message": "not found"})

        # Fail the very first POST once, to exercise the retry and backoff path.
        if not state["failed_once"]:
            state["failed_once"] = True
            return self._send(503, {"message": "service unavailable"})

        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")

        state["next_id"] += 1
        post_id = state["next_id"]
        self._send(201, {
            "id": post_id,
            "slug": payload.get("slug"),
            "status": payload.get("status"),
            "link": f"https://client.example.com/{payload.get('slug')}/",
            "meta": payload.get("meta", {}),
        })

    def log_message(self, *args):
        pass  # keep the publisher's own output readable


if __name__ == "__main__":
    print("mock WordPress listening on http://127.0.0.1:8099")
    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
