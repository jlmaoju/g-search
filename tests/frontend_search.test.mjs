import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(new URL('../gcores_crawler/frontend/assets/app.js', import.meta.url), 'utf8').replace(/\nbootstrap\(\);\s*$/, '\n');

function setup(t, fetchImpl, { fastTimeout = false } = {}) {
  class Element {
    textContent = ''; innerHTML = ''; value = ''; dataset = {}; disabled = false;
    attributes = new Map(); classes = new Set();
    classList = { add: x => this.classes.add(x), remove: x => this.classes.delete(x), toggle: (x, on) => on ? this.classes.add(x) : this.classes.delete(x) };
    setAttribute(k, v) { this.attributes.set(k, v); }
    removeAttribute(k) { this.attributes.delete(k); }
    querySelectorAll() { return []; }
  }
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const timers = new Set();
  const sandbox = {
    document: { getElementById: get, querySelector: get, title: '' },
    window: {
      localStorage: { getItem: () => null, setItem: () => {} },
      setTimeout: (fn, ms) => { const id = setTimeout(fn, fastTimeout && ms === 30000 ? 5 : ms); timers.add(id); return id; },
      clearTimeout: id => { timers.delete(id); clearTimeout(id); },
    },
    fetch: fetchImpl, URL, URLSearchParams, AbortController, Error, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const app = vm.runInContext('({ runSearch, state, els, fetchJsonWithTimeout, proxiedMediaAsset })', sandbox);
  app.state.meta = { mode: 'server' };
  t.after(() => { for (const timer of timers) clearTimeout(timer); });
  return app;
}

const response = (query, status = 200) => ({ ok: status === 200, status, text: async () => JSON.stringify({ query, results: [{ doc_id: query, item_id: query, item_title: query, doc_type: 'item_title' }] }) });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test('configured public image proxy preserves encoded paths and transforms without using the local backend', t => {
  const app = setup(t, () => { throw new Error('Unexpected fetch'); });
  app.state.runtimeConfig.imageProxyBase = '/media/gcores/';
  assert.equal(app.proxiedMediaAsset('https://image.gcores.com/a%20b.jpg?x-oss-process=image%2Fresize%2Cw_200&v=1'), '/media/gcores/a%20b.jpg?x-oss-process=image%2Fresize%2Cw_200&v=1');
  assert.equal(app.proxiedMediaAsset('http://image.gcores.com/a.jpg'), '/media/gcores/a.jpg');
  assert.equal(app.proxiedMediaAsset(''), '');
});

test('local viewers and non-CDN URLs retain validation through the existing media API', t => {
  const app = setup(t, () => { throw new Error('Unexpected fetch'); });
  app.state.runtimeConfig.apiBase = 'http://127.0.0.1:8765';
  const url = 'https://image.gcores.com/a.jpg';
  assert.equal(app.proxiedMediaAsset(url), 'http://127.0.0.1:8765/api/media-asset?url='+encodeURIComponent(url));
  app.state.runtimeConfig.imageProxyBase = '/media/gcores';
  for (const candidate of ['https://alioss.gcores.com/a.png', 'https://image.gcores.com.evil.example/a', 'https://user@image.gcores.com/a', 'https://image.gcores.com:1234/a', '/relative.png']) {
    assert.equal(app.proxiedMediaAsset(candidate), 'http://127.0.0.1:8765/api/media-asset?url='+encodeURIComponent(candidate));
  }
});

test('late older success cannot overwrite a newer search, even if transport ignores abort', async t => {
  const older = deferred(); let calls = 0; let oldSignal;
  const app = setup(t, (_url, options) => { if (++calls === 1) { oldSignal = options.signal; return older.promise; } return Promise.resolve(response('second')); });
  app.els.query.value = 'first'; const pending = app.runSearch();
  app.els.query.value = 'second'; await app.runSearch();
  assert.equal(oldSignal.aborted, true);
  older.resolve(response('first')); await pending;
  assert.equal(app.state.lastPayload.query, 'second');
  assert.match(app.els.results.innerHTML, /second/);
  assert.equal(app.els.searchForm.attributes.has('aria-busy'), false);
});

test('late older error cannot mark a successful new query offline', async t => {
  const older = deferred(); let calls = 0;
  const app = setup(t, () => ++calls === 1 ? older.promise : Promise.resolve(response('second')));
  app.els.query.value = 'first'; const pending = app.runSearch();
  app.els.query.value = 'second'; await app.runSearch();
  older.reject(new Error('Failed to fetch')); await pending;
  assert.equal(app.state.online, true);
  assert.equal(app.state.lastPayload.query, 'second');
});

test('clearing the query cancels a pending search without a late result', async t => {
  const older = deferred(); const app = setup(t, () => older.promise);
  app.els.query.value = 'first'; const pending = app.runSearch();
  app.els.query.value = ''; await app.runSearch(); older.resolve(response('first')); await pending;
  assert.equal(app.state.lastPayload, null);
  assert.equal(app.els.searchForm.attributes.has('aria-busy'), false);
});

test('a 503 can recover by submitting the same search again', async t => {
  let searches = 0;
  const app = setup(t, url => {
    if (url.includes('/api/search')) return Promise.resolve(response('recovered', ++searches === 1 ? 503 : 200));
    return Promise.resolve({ ok: true, text: async () => JSON.stringify({ mode: 'server', participants: [], program_types: [] }) });
  });
  app.els.query.value = 'recovered'; await app.runSearch();
  assert.equal(app.state.online, false);
  assert.equal(app.els.searchSubmit.attributes.has('aria-disabled'), false);
  await app.runSearch();
  assert.equal(app.state.online, true);
  assert.equal(app.state.lastPayload.query, 'recovered');
  assert.equal(app.els.offlineNotice.classes.has('hidden'), true);
  assert.equal(app.els.backendToast.textContent, '');
});

test('hung search times out and clears busy state', async t => {
  const app = setup(t, (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('Aborted')))), { fastTimeout: true });
  app.els.query.value = 'timeout'; await app.runSearch();
  assert.match(app.els.statusLine.textContent, /超时/);
  assert.equal(app.els.results.attributes.has('aria-busy'), false);
});

test('malformed success is an error, not a misleading empty search result', async t => {
  const app = setup(t, () => Promise.resolve({ ok: true, status: 200, text: async () => '<html>proxy error</html>' }));
  app.els.query.value = 'query'; await app.runSearch();
  assert.match(app.els.statusLine.textContent, /无效响应/);
  assert.equal(app.state.lastPayload, null);
});
