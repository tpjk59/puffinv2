/**
 * Preload script (node --import) that patches globalThis.fetch for googleapis.com
 * requests to inject a Referer header.
 *
 * Node.js 22 uses undici for fetch, which treats `Referer` as a forbidden request
 * header per the WHATWG spec and silently strips it — so we cannot set it via the
 * normal fetch headers object. Instead we bypass undici entirely for googleapis.com
 * and use node:https directly, which has no such restriction.
 */

import { request } from 'node:https';

const _originalFetch = globalThis.fetch;

globalThis.fetch = function patchedFetch(resource, init = {}) {
  const url = typeof resource === 'string' ? resource : resource.toString();

  if (!url.includes('googleapis.com')) {
    return _originalFetch(resource, init);
  }

  return new Promise((resolve, reject) => {
    const parsed = new URL(url);

    // Normalise body to a Buffer
    const body =
      init.body != null ? Buffer.from(String(init.body), 'utf8') : null;

    // Flatten headers — init.headers may be a Headers instance or a plain object
    const rawHeaders =
      init.headers instanceof Headers
        ? Object.fromEntries(init.headers.entries())
        : (init.headers ?? {});

    const headers = {
      ...rawHeaders,
      Referer: 'https://sbs-diet-app.firebaseapp.com',
    };
    if (body) {
      headers['Content-Length'] = String(body.length);
    }

    const req = request(
      {
        hostname: parsed.hostname,
        port: 443,
        path: parsed.pathname + parsed.search,
        method: (init.method ?? 'GET').toUpperCase(),
        headers,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
            statusText: res.statusMessage ?? '',
            json: () => Promise.resolve(JSON.parse(text)),
            text: () => Promise.resolve(text),
          });
        });
        res.on('error', reject);
      },
    );

    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
};
