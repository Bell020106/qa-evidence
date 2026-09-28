(() => {
  if (window !== window.top) return;
  const documentId = Math.random().toString(36).slice(2), events=[];
  let dropped=0;
  let unsupportedFrames=false;
  new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)
    if(node instanceof Element && (node.matches('iframe,frame')||node.querySelector('iframe,frame')))unsupportedFrames=true;
  }).observe(document,{childList:true,subtree:true});
  const sensitive=e=>!!e.closest('[data-sensitive],[data-private]')||e.type==='password'||/password|cc-|one-time-code/.test(e.autocomplete||'');
  const secrets=()=>{const values=[...document.querySelectorAll('input,textarea')].filter(sensitive).map(e=>e.value).filter(Boolean);
    return {secrets:values.filter(v=>v.length<=10000).slice(0,1000),secret_limit_exceeded:values.length>1000||values.some(v=>v.length>10000)};};
  function receive(event) {
    const e=event.target;
    if (!(e instanceof Element)) return;
    const payload={kind:'action',document:documentId,time:performance.now(),...secrets(),
      details:{action:event.type,target:sensitive(e)?'[sensitive element]':(e.id?'#'+e.id:e.tagName),value:'[not collected]'}};
    if (window.__qaTimelineSend) window.__qaTimelineSend(payload).catch(()=>{});
    else if(events.length<3000) events.push(payload); else dropped++;
  }
  for (const name of ['click','input']) document.addEventListener(name,receive,true);
  window.__qaTimeline={drain(){const result={events:events.splice(0),...secrets(),dropped,unsupported_frames:unsupportedFrames||!!document.querySelector('iframe,frame')};dropped=0;return result;}};
})();
