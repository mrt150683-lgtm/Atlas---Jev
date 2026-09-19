const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const assets = path.join(__dirname, '../cms/ui_assets');
const pages = ['index', 'context', 'constellation', 'ideas', 'library', 'sentinel', 'setup'];
function scripts(html) {
  return [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)]
    .filter(match => !/type=["']text\/atlas-app["']/.test(match[1])).map(match => match[2]);
}

for (const page of pages) {
  test(`opening ${page}.html directly offers recovery before any application access`, () => {
    const html = fs.readFileSync(path.join(assets, page + '.html'), 'utf8');
    const redirects = [];
    const blocked = () => { throw Error('File mode must not load assets, query APIs, or initialize application DOM'); };
    const context = vm.createContext({location:{protocol:'file:', replace:url => redirects.push(url)},
      fetch:blocked, setTimeout:blocked, setInterval:blocked,
      document:new Proxy({}, {get:blocked}), window:new Proxy({}, {get:blocked})});
    for (const source of scripts(html)) vm.runInContext(source, context);
    assert.deepEqual(redirects, ['open-atlas.html']);
    // Even the HTML preload scanner must not find a root-relative resource.
    assert.doesNotMatch(html, /<(?:script|link|img|iframe)\b[^>]*(?:src|href)=["']\//i);
  });
}

test('HTTP bootstrap preserves classic global scope and loads connection code before booting', () => {
  const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
  const appended = [], events = [];
  const context = vm.createContext({location:{protocol:'http:'},
    document:{createElement:tag => ({tag}), getElementById:() => ({textContent:'const sharedState = 7;'}),
      head:{append(node) {appended.push(node);}},
      body:{append(node) {appended.push(node); if(node.textContent) vm.runInContext(node.textContent, context);}}},
    boot:() => {events.push('boot'); return {then(callback) {callback();}};},
    viewerDeepLink:() => events.push('deep-link'), initConnectionExplorer:() => events.push('connections')});
  for (const source of scripts(html)) vm.runInContext(source, context);
  assert.equal(vm.runInContext('sharedState', context), 7);
  assert.equal(appended[0].href, '/assets/connections.css');
  assert.equal(appended[2].src, '/assets/connections.js');
  assert.deepEqual(events, []);
  appended[2].onload();
  assert.deepEqual(events, ['boot', 'deep-link', 'connections']);
});

test('recovery page is self-contained and connecting is an explicit user action', () => {
  const html = fs.readFileSync(path.join(assets, 'open-atlas.html'), 'utf8');
  assert.match(html, /CMS\.bat/);
  assert.match(html, /Setup-Atlas\.bat/);
  assert.match(html, /href="http:\/\/127\.0\.0\.1:7717\/"/);
  assert.equal(scripts(html).length, 0);
  assert.doesNotMatch(html, /<(?:script|link|img|iframe)\b/i);
});
