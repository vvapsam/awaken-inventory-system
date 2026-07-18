// AWAKEN Mobile service worker — network-first so counter data is always fresh.
// Static assets and product images fall back to cache when offline. POSTs are
// never cached or intercepted.
const CACHE = 'awaken-m-v1';
const STATIC = [
  '/static/style.css',
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/logo.svg'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  // Never touch non-GET (sales, settlements, movements post multipart).
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const cacheable = url.pathname.startsWith('/static/') || url.pathname.startsWith('/product-image/');

  if (cacheable) {
    // cache-first for static + images (they rarely change; image URL is per-id)
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return res;
      }).catch(() => hit))
    );
    return;
  }

  // network-first for everything else (the app shell + JSON API)
  e.respondWith(
    fetch(req).then(res => {
      if (url.pathname === '/m') {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req))
  );
});
