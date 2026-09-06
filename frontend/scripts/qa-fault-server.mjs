// Isolated browser QA server. It serves the built client with controlled API failures.
// No production state, proxy configuration, or application bundle is modified.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const dist=path.join(root,'dist/client');
const source=JSON.parse(await readFile(path.join(root,'tests/fixtures/search.json'),'utf8'));
const cases=['normal','offline','recover','timeout','rate-limit','quota-error','quota-fallback','quota-empty','semantic-timeout','invalid-json','invalid-results','filters-fail','filters-recover','empty','empty-catalog','images-fail','maximum'];
const attempts=new Map();
const searchRequests=new Map();
const meta={library_updated_at:'2026-09-04T18:59:30+08:00',participants_count:1,program_types_count:2};
const filters={program_types:[{name:'核市奇谭',count:1},{name:'Gadio Life',count:1}],participants:[{name:'四十二',count:1}]};
const json=(res,status,payload)=>{res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(payload));};
const mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2','.png':'image/png','.jpg':'image/jpeg'};
http.createServer(async(req,res)=>{
  try {
    const url=new URL(req.url,'http://127.0.0.1');
    const pageCase=url.pathname.match(/^\/cases\/([a-z-]+)$/)?.[1];
    if(url.pathname==='/') {
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
      return res.end(`<h1>Gsearch isolated fault QA</h1>${cases.map(name=>`<p><a href="/cases/${name}">${name}</a></p>`).join('')}`);
    }
    if(pageCase&&cases.includes(pageCase)) {
      attempts.set(pageCase,0);
      searchRequests.set(pageCase,0);
      res.writeHead(200,{'Content-Type':mime['.html'],'Cache-Control':'no-store','Set-Cookie':`qa_case=${pageCase}; Path=/; SameSite=Strict; HttpOnly`});
      return res.end(await readFile(path.join(dist,'index.html')));
    }
    const scenario=(req.headers.cookie||'').match(/(?:^|;\s*)qa_case=([a-z-]+)/)?.[1]||'normal';
    if(url.pathname==='/__qa/stats') return json(res,200,{searchRequests:Object.fromEntries(searchRequests)});
    if(url.pathname.startsWith('/api/')) {
      if(scenario==='offline') return json(res,503,{error:'Backend is offline'});
      if(url.pathname==='/api/meta') return json(res,200,meta);
      if(url.pathname==='/api/participants') {
        if(scenario==='filters-fail') return json(res,503,{});
        if(scenario==='filters-recover'&&!(attempts.get(scenario)||0)) {attempts.set(scenario,1);return json(res,503,{});}
        return json(res,200,scenario==='empty-catalog'?{program_types:[],participants:[]}:filters);
      }
      if(url.pathname==='/api/search') {
        searchRequests.set(scenario,(searchRequests.get(scenario)||0)+1);
        if(scenario==='recover'&&!(attempts.get(scenario)||0)) {attempts.set(scenario,1);return json(res,503,{});}
        if(scenario==='timeout') {const timer=setTimeout(()=>{if(!res.destroyed)json(res,200,{results:[]});},35000);res.on('close',()=>clearTimeout(timer));return;}
        if(scenario==='rate-limit') return json(res,429,{error:'too many requests'});
        if(scenario==='quota-error') return json(res,500,{error:{code:1113,message:'余额不足'}});
        if(scenario==='invalid-json') {res.writeHead(200,{'Content-Type':'application/json'});return res.end('not json');}
        if(scenario==='invalid-results') return json(res,200,{results:[null]});
        let results=scenario==='empty'||scenario==='quota-empty'?[]:source.results.slice(0,3);
        if(scenario==='maximum') results=Array.from({length:Math.min(200,Number(url.searchParams.get('limit'))||50)},(_,i)=>({...source.results[0],doc_id:`qa-result-${i}`}));
        if(scenario==='images-fail') results=[{...source.results[0],doc_type:'timeline_note',timeline_title:'配图故障测试',timeline_content:'测试配图暂不可用时的提示',timeline_asset_url:'https://image.gcores.com/qa-missing.jpg'}];
        const payload={results};
        if(scenario.startsWith('quota-')) Object.assign(payload,{degraded:true,degraded_reason:'embedding_quota_exhausted',semantic_error:'1113 余额不足'});
        if(scenario==='semantic-timeout') Object.assign(payload,{degraded:true,semantic_error:'timed out'});
        return json(res,200,payload);
      }
      return json(res,404,{});
    }
    if(url.pathname.startsWith('/media/gcores/')) {
      if(scenario==='images-fail') {res.writeHead(404);return res.end();}
      res.writeHead(200,{'Content-Type':'image/svg+xml'});
      return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320"><rect width="320" height="320" fill="#ed1242"/><text x="160" y="170" text-anchor="middle" fill="white" font-size="48">QA</text></svg>');
    }
    const file=path.resolve(dist,'.'+decodeURIComponent(url.pathname));
    if(!file.startsWith(dist+path.sep)) {res.writeHead(403);return res.end();}
    res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream'});
    res.end(await readFile(file));
  } catch {if(!res.headersSent)res.writeHead(404);res.end();}
}).listen(18766,'127.0.0.1',()=>process.stdout.write('Fault QA: http://127.0.0.1:18766/\n'));
