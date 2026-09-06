import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {buildSearchParams,fetchJson,mapResult,mediaUrl,sortResults,serviceError,degradationNotice,searchLive,loadCatalog} from '../src/api.js';

test('all three scopes use production doc types and preserve repeated Unicode filters',()=>{
  for (const [mode,types] of Object.entries({content:['scene_summary','episode_card'],title:['item_title'],timeline:['timeline_note']})) {
    const params=new URLSearchParams(buildSearchParams({query:' 日剧 ',mode,categories:['Gadio Life','核市奇谭'],people:['四十二','白广大'],limit:50}).toString());
    assert.equal(params.get('q'),'日剧');
    assert.equal(params.get('scope'),mode);
    assert.deepEqual(params.getAll('doc_types'),types);
    assert.deepEqual(params.getAll('categories'),['Gadio Life','核市奇谭']);
    assert.deepEqual(params.getAll('participants'),['四十二','白广大']);
  }
});

test('API fixtures preserve content, context, offsets and image routes',async()=>{
  for (const path of ['./fixtures/search.json','./fixtures/timeline.json']) {
    const payload=JSON.parse((await readFile(new URL(path,import.meta.url),'utf8')).replace(/^\uFEFF/,''));
    for (const source of payload.results) {
      const result=mapResult(source);
      assert.equal(result.id,source.doc_id);
      assert.equal(result.title,source.item_title);
      assert.equal(result.time,source.display_timestamp||'');
      assert.equal(result.transcript.length,source.neighbor_atoms?.length||0);
      assert.ok(result.cover.startsWith('/media/gcores/'));
      if (source.doc_type==='timeline_note') assert.equal(result.timelineTitle,source.timeline_title||'');
      else if (source.doc_type==='item_title') assert.equal(result.context,source.excerpt||source.desc||'');
      else assert.equal(result.context,source.display_text||source.excerpt||'');
    }
  }
});

test('empty optional fields and external URLs cannot produce broken or unsafe links',()=>{
  const result=mapResult({doc_id:'test',item_title:'节目',source_url:'javascript:alert(1)'});
  assert.equal(result.date,'');assert.equal(result.time,'');assert.equal(result.url,'');assert.deepEqual(result.transcript,[]);
  assert.equal(mediaUrl('https://image.gcores.com/cover.jpg?x=1'),'/media/gcores/cover.jpg?x=1');
  assert.equal(mediaUrl('https://unrelated.invalid/cover.jpg'),'');
});

test('time sorting never mutates relevance order',()=>{
  const rows=[{id:'new',date:'2025-01-01',offset:0},{id:'old',date:'2020-01-01',offset:0}];
  assert.deepEqual(sortResults(rows,'oldest').map(r=>r.id),['old','new']);
  assert.deepEqual(sortResults(rows,'relevance').map(r=>r.id),['new','old']);
});

test('network failures, malformed payloads, cancellation and timeouts remain distinguishable',async()=>{
  const original=globalThis.fetch;
  try {
    globalThis.fetch=async()=>new Response('{}',{status:503});
    await assert.rejects(fetchJson('/test'),{code:'offline'});
    globalThis.fetch=async()=>new Response('not json');
    await assert.rejects(fetchJson('/test'),/无法读取/);
    globalThis.fetch=async(_url,{signal})=>new Promise((_resolve,reject)=>{
      if (signal.aborted) reject(new DOMException('Aborted','AbortError'));
      else signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
    });
    await assert.rejects(fetchJson('/test',{timeoutMs:10}),/超时/);
    const controller=new AbortController();
    const pending=fetchJson('/test',{signal:controller.signal});
    controller.abort();
    await assert.rejects(pending,{name:'AbortError'});
  } finally {globalThis.fetch=original;}
});

test('backend offline, request rate limits and provider quota exhaustion give distinct actionable notices',()=>{
  for (const status of [502,503,504]) {
    const error=serviceError(status,null);
    assert.equal(error.code,'offline');assert.match(error.message,/后端/);assert.match(error.detail,/YQBelmont/);
  }
  assert.equal(serviceError(429,{}).code,'rate_limit');
  const quota=serviceError(429,{error:{code:'1113',message:'余额不足，请购买资源包'}});
  assert.equal(quota.code,'quota');assert.match(quota.detail,/机核私信 YQBelmont贲/);
  assert.doesNotMatch(quota.message,/1113/);
});

test('fallback responses keep quota and timeout warnings even if the result list is empty',()=>{
  assert.equal(degradationNotice({results:[]}),null);
  const quota=degradationNotice({degraded:true,degraded_reason:'embedding_quota_exhausted',results:[]});
  assert.match(quota.title,/额度不足/);assert.match(quota.message,/关键词/);assert.match(quota.detail,/机核私信/);
  assert.match(degradationNotice({recall_stats:{lexical_fallback:true},semantic_error:'timed out'}).title,/超时/);
  assert.match(degradationNotice({degraded:true}).message,/相关性和排序质量/);
});

test('invalid JSON shapes fail politely rather than rendering a blank screen',async()=>{
  const original=globalThis.fetch;
  try {
    for (const payload of [null,{}, {results:[null]}, {results:[{doc_id:'broken',item_title:{bad:true}}]}]) {
      globalThis.fetch=async()=>new Response(JSON.stringify(payload));
      await assert.rejects(searchLive({query:'test',mode:'content'}),/搜索结果格式不正确/);
    }
    globalThis.fetch=async()=>new Response(JSON.stringify({participants:[null],program_types:[]}));
    await assert.rejects(loadCatalog(),/筛选列表格式不正确/);
    globalThis.fetch=async()=>{throw new TypeError('Failed to fetch');};
    await assert.rejects(fetchJson('/test'),{code:'network'});
  } finally {globalThis.fetch=original;}
});

test('result limit never exceeds the production endpoint limit',()=>{
  assert.equal(buildSearchParams({query:'test',mode:'content',limit:999}).get('limit'),'200');
  assert.equal(buildSearchParams({query:'test',mode:'content',limit:0}).get('limit'),'1');
});

test('empty welcome queries do not call the search API',async()=>{
  const original=globalThis.fetch;
  let calls=0;
  try {
    globalThis.fetch=async()=>{calls++;throw new Error('Unexpected search request');};
    for (const query of ['', '   ', '\n\t']) {
      for (const mode of ['content','title','timeline']) {
        const result=await searchLive({query,mode,categories:['核市奇谭'],people:['四十二'],limit:200});
        assert.deepEqual(result,{results:[],notice:null});
      }
    }
    assert.equal(calls,0);
  } finally {globalThis.fetch=original;}
});
