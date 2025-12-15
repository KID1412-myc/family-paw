const CACHE_NAME = 'family-paw-v1';

// 安装时立即激活
self.addEventListener('install', (e) => {
    self.skipWaiting();
});

// 激活时接管页面
self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

// 拦截请求：什么都不存，直接走网络 (Network Only)
// 这样既满足了 PWA 标准，又防止了烦人的缓存问题
self.addEventListener('fetch', (e) => {
    e.respondWith(fetch(e.request));
});