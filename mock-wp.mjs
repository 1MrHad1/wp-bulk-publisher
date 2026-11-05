#!/usr/bin/env node
/**
 * A stand-in for the WordPress REST API, so publish-articles.mjs can be run end
 * to end without touching a client site.
 *
 * It returns one 503 on the first POST and reports one slug as already taken,
 * so the retry path and the duplicate path actually run rather than just
 * existing in the file.
 *
 *   node mock-wp.mjs      # http://127.0.0.1:8099
 */
import { createServer } from 'node:http';

const CATEGORIES = { blog: 11, guides: 12, news: 13 };
const TAKEN = { 'november-core-update-what-changed': 9001 };

let nextId = 500;
let failedOnce = false;

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const slug = url.searchParams.get('slug');
  const send = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };

  if (req.method === 'GET' && url.pathname === '/wp-json/wp/v2/categories')
    return send(200, slug in CATEGORIES ? [{ id: CATEGORIES[slug], slug }] : []);

  if (req.method === 'GET' && url.pathname === '/wp-json/wp/v2/posts')
    return send(200, slug in TAKEN ? [{ id: TAKEN[slug], slug }] : []);

  if (req.method === 'POST' && url.pathname === '/wp-json/wp/v2/posts') {
    if (!failedOnce) { failedOnce = true; return send(503, { message: 'service unavailable' }); }

    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');

    return send(201, {
      id: ++nextId,
      slug: body.slug,
      status: body.status,
      link: `https://client.example.com/${body.slug}/`,
      meta: body.meta ?? {},
    });
  }

  send(404, { message: 'not found' });
}).listen(8099, '127.0.0.1', () => console.log('mock WordPress on http://127.0.0.1:8099'));
