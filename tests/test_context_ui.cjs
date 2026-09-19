const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

// Exercise the actual page's requests and rendered output without a browser dependency.
// Browser layout and keyboard checks remain a separate visual verification step.
const html = fs.readFileSync(path.join(__dirname, '../cms/ui_assets/context.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];

class Element {
  constructor(tag = 'div', value = '') {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this._text = value;
    this.value = '';
    this.hidden = false;
    this.disabled = false;
  }
  set innerHTML(_) { throw new Error('Untrusted content must never be rendered as HTML'); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...children) { this.children.push(...children.map(c => typeof c === 'string' ? new Element('#text', c) : c)); }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() { this.focused = true; }
  click() { return this.listeners.click?.({preventDefault() {}}); }
  remove() {}
}

const receipt = (overrides = {}) => ({
  id: 'decision-1', created_at: '2026-09-19T11:00:00Z', query: 'Find task context',
  status: 'shadow', mode: 'shadow', reason: 'Comparison only', provider: 'typesafe',
  model: 'jev-1.13', elapsed_ms: 21, cache_hit: false, source: 'query_codebase',
  baseline_ids: ['file:a.py'], selected_ids: ['file:a.py'], proposed_ids: ['file:b.py', 'file:a.py'],
  candidates: [
    {id: 'file:a.py', name: 'a.py', path: 'a.py', kind: 'file', local_rank: 1, proposed_rank: 2, selected_rank: 1, selected: true, mandatory: true, relevance: 0.4, start_line: 1, end_line: 30, reason: 'Explicit path'},
    {id: 'file:b.py', name: 'b.py', path: 'b.py', kind: 'file', local_rank: 2, proposed_rank: 1, selected: false, relevance: 0.9, start_line: 4, end_line: 12, reason: 'Relevance estimate'},
  ], ...overrides,
});

async function settle() { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }

async function fixture({status = {mode:'off', allow_cloud:false, ready:false}, history = [], receipts = {}, search = '', handler} = {}) {
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
  elements.topK.value = '8';
  elements.receiptContent.hidden = true;
  const requests = [], copied = [];
  const document = {
    getElementById(id) { assert.ok(elements[id], 'Known page element: ' + id); return elements[id]; },
    createElement: tag => new Element(tag), createTextNode: text => new Element('#text', text), body: new Element('body'),
  };
  const context = vm.createContext({document, AbortController, setTimeout, clearTimeout, Blob, URLSearchParams, location:{search},
    URL: {createObjectURL: () => 'blob:receipt', revokeObjectURL() {}},
    navigator: {clipboard: {async writeText(value) { copied.push(value); }}},
    async fetch(url, options = {}) {
      requests.push({url, options});
      let data;
      if (handler) data = await handler(url, options);
      if (data === undefined) {
        if (url === '/api/context/status') data = status;
        else if (url === '/api/context/history') data = {items:history};
        else if (url.startsWith('/api/context/receipt?id=')) data = receipts[decodeURIComponent(url.split('=')[1])];
        else throw new Error('Unexpected request: ' + url);
      }
      return {ok:true, status:200, json:async () => data};
    },
  });
  vm.runInContext(script, context);
  await settle();
  return {elements, requests, copied, async submit(query) {
    elements.query.value = query;
    await elements.previewForm.listeners.submit({preventDefault() {}});
    await settle();
  }};
}

test('loading the page only reads local status and history, without running a selection', async () => {
  const {elements, requests} = await fixture();
  assert.deepEqual(requests.map(r => r.url), ['/api/context/status', '/api/context/history']);
  assert.equal(elements.previewButton.disabled, false);
  assert.match(elements.previewPrivacy.textContent, /does not send your task to Jev/);
  assert.match(elements.historyList.textContent, /No saved decisions/);
});

test('receipt deep link opens its requested decision instead of the newest history entry', async () => {
  const requested = receipt({id:'0123456789abcdef0123456789abcdef',query:'Linked decision'});
  const newest = receipt({id:'newest',query:'Newest decision'});
  const {elements,requests} = await fixture({search:'?receipt=' + requested.id,history:[newest,requested],receipts:{[requested.id]:requested,newest}});
  assert.equal(elements.receiptQuery.textContent,'Linked decision');
  assert.equal(requests.filter(r => r.url.startsWith('/api/context/receipt')).length,1);
  assert.ok(!requests.some(r => r.options.method === 'POST'));
});

test('query deep link prefills a bounded preview without automatically selecting or posting', async () => {
  const query = '<script>untrusted</script> ' + 'x'.repeat(5000);
  const r = receipt();
  const {elements,requests} = await fixture({search:'?q=' + encodeURIComponent(query),history:[r],receipts:{[r.id]:r}});
  assert.equal(elements.query.value,query.slice(0,4000));
  assert.deepEqual(requests.map(r => r.url),['/api/context/status','/api/context/history']);
  assert.equal(elements.receiptContent.hidden,true);
});

test('missing deep-linked receipt reports expiry and does not substitute a different decision', async () => {
  const id = '0123456789abcdef0123456789abcdef';
  const newest = receipt();
  const {elements,requests} = await fixture({search:'?receipt=' + id,history:[newest],handler(url) {
    if (url === '/api/context/receipt?id=' + id) throw Object.assign(new Error('Receipt not found'),{status:404});
  }});
  assert.match(elements.message.textContent,/missing or has expired/);
  assert.equal(elements.receiptContent.hidden,true);
  assert.equal(requests.filter(r => r.url.startsWith('/api/context/receipt')).length,1);
});

test('malformed receipt links are explained without fetching arbitrary identifiers', async () => {
  const {elements,requests} = await fixture({search:'?receipt=' + encodeURIComponent('../private')});
  assert.match(elements.message.textContent,/receipt link is invalid/);
  assert.ok(!requests.some(r => r.url.startsWith('/api/context/receipt')));
});

test('shadow receipt keeps actual selection distinct from Jev proposal and shows all local ranks', async () => {
  const r = receipt();
  const {elements, copied} = await fixture({history:[r], receipts:{[r.id]:r}});
  assert.equal(elements.receiptStatus.textContent, 'Shadow comparison');
  assert.match(elements.receiptExplanation.textContent, /kept the local selection/);
  assert.equal(elements.selectedCount.textContent, '1');
  assert.equal(elements.protectedCount.textContent, '1');
  const rows = elements.candidateRows.children;
  assert.deepEqual(rows.map(row => row.children.slice(1).map(cell => cell.textContent)), [['1','2','1','0.40'],['2','1','—','0.90']]);
  await elements.showSelected.click();
  assert.equal(elements.candidateRows.children.length, 1);
  assert.match(elements.candidateRows.textContent, /a.py/);
  await elements.copyReferences.click();
  assert.deepEqual(copied, ['a.py:1–30 · a.py']);
});

test('source strings remain plain text and malformed relevance is not presented as a score', async () => {
  const attack = '<img src=x onerror=alert(1)>';
  const r = receipt({query:attack, reason:attack, candidates:[{id:'file:a.py', name:attack, path:attack, reason:attack, local_rank:1, relevance:3}]});
  const {elements} = await fixture({history:[r],receipts:{[r.id]:r}});
  assert.equal(elements.receiptQuery.textContent, attack);
  assert.match(elements.candidateRows.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(elements.candidateRows.children[0].children[4].textContent, '—');
});

test('preview uses the chosen query and count, records fallback and refreshes history', async () => {
  const r = receipt({status:'fallback',mode:'assist',reason:'Provider timeout',cache_hit:true});
  const {elements, requests, submit} = await fixture({status:{mode:'assist',allow_cloud:true,ready:true}, handler(url) {
    if (url === '/api/context/preview') return {results:[],selection:r};
  }});
  elements.topK.value = '12';
  assert.match(elements.previewPrivacy.textContent, /may send your task and candidate metadata/);
  await submit('  inspect routing  ');
  const post = requests.find(r => r.url === '/api/context/preview');
  assert.equal(post.options.method, 'POST');
  assert.deepEqual(JSON.parse(post.options.body), {query:'inspect routing',top_k:12});
  assert.equal(elements.receiptStatus.textContent, 'Local fallback');
  assert.match(elements.receiptReason.textContent, /Provider timeout/);
  assert.match(elements.receiptMeta.textContent, /Cached ranking reused/);
  assert.equal(elements.previewButton.disabled, false);
  assert.equal(requests.filter(r => r.url === '/api/context/history').length, 2);
});

test('empty result explains retrieval limits without asserting the feature is absent', async () => {
  const r = receipt({status:'local',candidates:[],baseline_ids:[],selected_ids:[],proposed_ids:[]});
  const {elements} = await fixture({history:[r],receipts:{[r.id]:r}});
  assert.match(elements.candidateRows.textContent, /does not prove that the behavior is absent/);
  assert.equal(elements.selectedCount.textContent, '0');
  assert.equal(elements.copyReferences.disabled, true);
});

test('Jev abstention labels local results for inspection and never invents proposed ranks', async () => {
  const r = receipt({status:'no_match',proposed_ids:[]});
  const {elements} = await fixture({history:[r],receipts:{[r.id]:r}});
  assert.equal(elements.receiptStatus.textContent,'No Jev recommendation');
  assert.equal(elements.selectedCountLabel.textContent,'items returned for inspection');
  assert.equal(elements.actualRankHeader.textContent,'Returned');
  assert.match(elements.receiptExplanation.textContent,/without a model-backed relevance claim/);
  assert.deepEqual(elements.candidateRows.children.map(row => row.children[2].textContent),['—','—']);
});

test('candidate freshness reports unchanged, stale and unknown sources without claiming verification', async () => {
  const r = receipt({graph_fingerprint:'input-evidence-digest',candidates:[
    {id:'file:a.py',name:'Current',local_rank:1,source_freshness:'current'},
    {id:'file:b.py',name:'Stale',local_rank:2,source_freshness:'stale'},
    {id:'file:c.py',name:'Unknown',local_rank:3,source_freshness:'unknown'},
    {id:'file:d.py',name:'Missing field',local_rank:4},
  ]});
  const {elements} = await fixture({history:[r],receipts:{[r.id]:r}});
  const text = elements.candidateRows.children.map(row => row.textContent);
  assert.match(text[0],/Source unchanged since indexing/);
  assert.match(text[1],/Source changed · refresh required/);
  assert.match(text[2],/Freshness unknown/);
  assert.match(text[3],/Freshness unknown/);
  assert.ok(text.every(value => !/verified/i.test(value)));
  assert.match(elements.auditDetails.textContent,/Evidence fingerprintinput-evidence-digest/);
  assert.doesNotMatch(elements.auditDetails.textContent,/Graph fingerprint/);
});

test('failed settings load disables preview until a successful refresh', async () => {
  let failed = true;
  const {elements} = await fixture({handler(url) {
    if (url === '/api/context/status' && failed) throw new Error('Connection unavailable');
  }});
  assert.equal(elements.previewButton.disabled, true);
  assert.match(elements.modeText.textContent, /Settings unavailable/);
  failed = false;
  elements.refreshHistory.click();
  await settle();
  assert.equal(elements.previewButton.disabled, false);
});

test('failed preview preserves previous receipt and returns form to an usable state', async () => {
  const r = receipt();
  const {elements, submit} = await fixture({history:[r],receipts:{[r.id]:r},handler(url) {
    if (url === '/api/context/preview') throw new Error('Memory layer unavailable');
  }});
  await submit('new task');
  assert.equal(elements.receiptQuery.textContent, r.query);
  assert.match(elements.message.textContent, /Memory layer unavailable/);
  assert.equal(elements.message.className, 'notice error');
  assert.equal(elements.previewButton.disabled, false);
  assert.equal(elements.receiptPane.attributes['aria-busy'], 'false');
});

test('unsaved preview never claims persistence and keeps its warning visible after refresh errors', async () => {
  const r = receipt({persistence_warning:'This context decision could not be saved; existing history was preserved.'});
  let failHistory = false;
  const {elements,submit} = await fixture({handler(url) {
    if (url === '/api/context/preview') return {results:[],selection:r};
    if (url === '/api/context/history' && failHistory) throw new Error('History is unreadable');
  }});
  await submit('inspect context');
  assert.doesNotMatch(elements.message.textContent,/Preview saved/);
  assert.match(elements.message.textContent,/could not be saved/);
  assert.equal(elements.receiptWarning.hidden,false);
  assert.match(elements.receiptWarning.textContent,/Receipt not saved/);
  assert.match(elements.receiptWarning.textContent,/existing history was preserved/);
  assert.equal(elements.downloadReceipt.disabled,false);
  failHistory = true;
  elements.refreshHistory.click();
  await settle();
  assert.equal(elements.receiptWarning.hidden,false);
  assert.match(elements.receiptWarning.textContent,/Receipt not saved/);
});

test('later history selection wins even when an earlier request finishes last', async () => {
  const first = receipt({id:'first',query:'First'}), second = receipt({id:'second',query:'Second'});
  const wait = deferred();
  const {elements} = await fixture({history:[first,second],receipts:{first,second},handler(url) {
    if (url === '/api/context/receipt?id=first') return wait.promise;
  }});
  const secondButton = elements.historyList.children[1];
  secondButton.click();
  await settle();
  assert.equal(elements.receiptQuery.textContent,'Second');
  wait.resolve(first);
  await settle();
  assert.equal(elements.receiptQuery.textContent,'Second');
  assert.equal(elements.receiptPane.attributes['aria-busy'],'false');
});
