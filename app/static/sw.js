/* Gabia AI 허브 서비스워커
 * - 앱 셸(/)은 network-first: 배포 갱신을 바로 반영하되 오프라인이면 캐시 사용
 * - /api 는 절대 캐시하지 않음 (채팅/인증 데이터)
 * - 정적 리소스는 cache-first
 */
const CACHE = 'aihub-shell-v1';
const SHELL = ['/', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api')) return; // API는 항상 네트워크

  // 페이지 내비게이션: network-first, 실패 시 캐시된 셸
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put('/', copy));
          return r;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // 정적 리소스: cache-first
  if (url.origin === location.origin && url.pathname.startsWith('/static')) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return r;
      }))
    );
  }
});
