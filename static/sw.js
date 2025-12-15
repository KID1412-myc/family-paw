self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
    // 直接透传，不缓存，保证你改代码后刷新就能看到
    e.respondWith(fetch(e.request));
});