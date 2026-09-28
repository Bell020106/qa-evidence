(() => {
  if (window !== window.top) return;
  const actions = [], limitations = new Set();
  function limit(reason) {
    if (limitations.has(reason)) return;
    limitations.add(reason);
    // Persist outside this document before a navigation destroys its memory.
    window.__qaManualLimit(reason).catch(() => {});
  }
  let stopped = false;
  const sensitive = e => !!e.closest('[data-sensitive],[data-private]') ||
    e.type === 'password' || /password|cc-|one-time-code/.test(e.autocomplete || '');
  const textInput = e => e instanceof HTMLTextAreaElement ||
    (e instanceof HTMLInputElement && ['text','search','email','tel','url',''].includes(e.type));
  const signature = e => ({tag:e.tagName, type:e.getAttribute('type') || '', name:e.getAttribute('name') || ''});
  function target(e) {
    if (!(e instanceof Element)) return null;
    if (e.id) {
      const selector = '#' + CSS.escape(e.id);
      if (document.querySelectorAll(selector).length !== 1) {
        limit('ambiguous duplicate id; action omitted'); return null;
      }
      return {selector, signature:signature(e)};
    }
    const parts = [];
    let node = e;
    while (node && node !== document.documentElement) {
      const siblings = [...node.parentElement.children].filter(x=>x.tagName===node.tagName);
      parts.unshift(node.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(node)+1)+')');
      node = node.parentElement;
    }
    const selector = 'html > ' + parts.join(' > ');
    if (document.querySelectorAll(selector).length !== 1) {
      limit('ambiguous element path; action omitted'); return null;
    }
    return {selector, signature:signature(e)};
  }
  function input(e) {
    if (sensitive(e)) { limit('sensitive field value omitted'); return; }
    if (!textInput(e)) { limit('unsupported input type; action omitted'); return; }
    const ref = target(e);
    if (!ref) return;
    const action = {action:'fill', ...ref, value:e.value};
    const last = actions.at(-1);
    if (last && last.action==='fill' && last.selector===ref.selector) actions[actions.length-1]=action;
    else actions.push(action);
  }
  document.addEventListener('input', e=>{if(!stopped) input(e.target);}, true);
  document.addEventListener('compositionend', e=>{if(!stopped) input(e.target);}, true);
  document.addEventListener('click', e=>{
    if (stopped) return;
    let element = e.target.closest('button,a,input,textarea,select,[role="button"]') || e.target;
    if (sensitive(element)) { limit('sensitive field interaction omitted'); return; }
    if (textInput(element) || element.tagName==='LABEL') return;
    if (element.matches('input,select') || element.isContentEditable || element.closest('[contenteditable="true"]')) {
      limit('unsupported control click omitted'); return;
    }
    if (e.button!==0 || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey || e.detail>1) {
      limit('unsupported modified or repeated click'); return;
    }
    const ref = target(element);
    if (ref) actions.push({action:'click', ...ref});
  }, true);
  document.addEventListener('keydown', e=>{
    if (stopped || e.isComposing) return;
    if (['Enter','Escape','F5'].includes(e.key) || e.altKey || e.metaKey ||
        (e.ctrlKey && !['a','c','v','x'].includes(e.key.toLowerCase())))
      limit('unsupported keyboard action: ' + e.key);
  }, true);
  for (const name of ['drop','dragstart','contextmenu'])
    document.addEventListener(name, ()=>{if(!stopped) limit('unsupported '+name);}, true);
  const documentId = Math.random().toString(36).slice(2);
  window.__qaManual = {
    snapshot(stop=false) {
      if (stop) stopped=true;
      if(document.querySelector('iframe,frame')) limit('iframe content not recorded');
      const inputs=[];
      const selectors = new Set(actions.filter(a=>a.action==='fill').map(a=>a.selector));
      for(const selector of selectors) {
        const matches=document.querySelectorAll(selector);
        if(matches.length!==1) {limit('recorded input missing or ambiguous at save');continue;}
        const e=matches[0];
        if(sensitive(e)) {limit('sensitive field value omitted');continue;}
        inputs.push({selector,signature:signature(e),value:e.value});
      }
      return {document_id:documentId, actions, limitations:[...limitations], final_url:location.href,
              observed:{inputs,text:document.body ? document.body.innerText : ''}};
    }
  };
})();

