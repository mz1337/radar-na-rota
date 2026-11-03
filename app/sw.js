/* Radar na Rota: service worker.
   Guarda o app, a biblioteca do mapa, a base de radares e os pedaços de mapa já vistos,
   para o app abrir e alertar mesmo com sinal fraco na estrada. */
const VERSAO = 'rnr-v9';
const APP = `${VERSAO}-app`, LIB = `${VERSAO}-lib`, DADOS = `${VERSAO}-dados`, MAPA = 'rnr-mapa', VOZ = 'rnr-voz';
const MAX_MAPA = 4000;
const CASCA = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png'];
const BIBLIOTECAS = ['https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js', 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css'];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    await (await caches.open(APP)).addAll(CASCA);
    const lib = await caches.open(LIB);
    await Promise.all(BIBLIOTECAS.map((u) => lib.add(new Request(u, { mode: 'cors' })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== MAPA && k !== VOZ && !k.startsWith(VERSAO)) await caches.delete(k);
    await self.clients.claim();
  })());
});

async function redePrimeiro(req, nome) {
  const cache = await caches.open(nome);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

async function cacheprimeiro(req, nome) {
  const cache = await caches.open(nome);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === 'opaque') cache.put(req, res.clone());
  return res;
}

let gravacoes = 0;
async function aparar(cache) {
  const chaves = await cache.keys();
  for (let i = 0; i < chaves.length - MAX_MAPA; i++) await cache.delete(chaves[i]);
}
async function mapa(req, ev) {
  const cache = await caches.open(MAPA);
  const hit = await cache.match(req);
  const rede = fetch(req).then((res) => {
    if (res.ok) {
      cache.put(req, res.clone());
      if (++gravacoes % 100 === 0) aparar(cache);
    }
    return res;
  });
  if (hit) {
    ev.waitUntil(rede.catch(() => {}));
    return hit;
  }
  return rede;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (url.pathname.endsWith('/data/radares.json')) return e.respondWith(redePrimeiro(req, DADOS));
    if (url.pathname.includes('/audio/') && !url.pathname.endsWith('.json')) return e.respondWith(cacheprimeiro(req, VOZ));
    if (url.pathname.includes('/audio/')) return e.respondWith(redePrimeiro(req, VOZ));
    if (req.mode === 'navigate') return e.respondWith(redePrimeiro(req, APP).catch(() => caches.match('index.html')));
    return e.respondWith(redePrimeiro(req, APP));
  }
  if (url.hostname === 'unpkg.com' || url.hostname === 'cdn.jsdelivr.net' || url.hostname === 'fonts.gstatic.com') {
    return e.respondWith(cacheprimeiro(req, LIB));
  }
  if (url.hostname === 'fonts.googleapis.com') return e.respondWith(redePrimeiro(req, LIB));
  if (url.hostname === 'tiles.openfreemap.org') return e.respondWith(mapa(req, e));
  /* busca de endereços, rotas e fontes de radares: sempre pela rede */
});
