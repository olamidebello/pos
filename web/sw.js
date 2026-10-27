'use strict';
const CACHE='global-pos-shell-v2';
const SHELL=['/offline.html','/style.css','/assets/icon-192.png'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)));});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('global-pos-shell-')&&key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.origin!==self.location.origin||url.pathname.startsWith('/api/'))return;
  if(event.request.mode==='navigate'){
    event.respondWith(fetch(event.request).catch(()=>caches.match('/offline.html')));
  }else if(SHELL.includes(url.pathname)){
    event.respondWith(fetch(event.request).catch(()=>caches.match(event.request)));
  }
});
