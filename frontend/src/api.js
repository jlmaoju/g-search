export const MAX_RESULTS = 200;
export const MODES = [
  {id:'content',label:'内容',placeholder:'记得的一句话、一个人，或一段话题…'},
  {id:'title',label:'标题',placeholder:'输入节目标题，例如：日剧2020'},
  {id:'timeline',label:'时间轴',placeholder:'输入时间轴里的注释或知识点…'},
];
const DOC_TYPES = {content:['scene_summary','episode_card'],title:['item_title'],timeline:['timeline_note']};
export const AUTHOR_CONTACT = '如果你看到这条提示，请到机核私信 YQBelmont贲 告知一声，感谢。';

export class ServiceError extends Error {
  constructor(message,detail='',code='service') {super(message);this.name='ServiceError';this.detail=detail;this.code=code;}
}

function quotaExhausted(payload) {
  const detail=[payload?.semantic_error,payload?.error,payload?.detail,payload?.message].map(value=>typeof value==='string'?value:JSON.stringify(value)||'').join(' ');
  return payload?.degraded_reason==='embedding_quota_exhausted'||/1113|余额不足|资源包|insufficient[_ ]?(quota|credit|balance)|quota.{0,20}(exhaust|exceed)|credit.{0,20}exhaust/i.test(detail);
}

export function serviceError(status,payload) {
  if (quotaExhausted(payload)) return new ServiceError('语义检索 API 额度不足，暂时无法完成搜索。',AUTHOR_CONTACT,'quota');
  if (status===429) return new ServiceError('请求较多，请稍等片刻再试。','请稍后再点击“重新搜索”。','rate_limit');
  if ([502,503,504].includes(status)) return new ServiceError('搜索后端暂时不可用，请稍后重试。','后端可能正在维护或已离线；若持续出现，请联系作者 YQBelmont贲。','offline');
  return new ServiceError(`服务暂时无法完成请求（${status}），请重试。`,'','http');
}

export function degradationNotice(payload) {
  if (!payload?.degraded&&!payload?.recall_stats?.lexical_fallback) return null;
  const quota=quotaExhausted(payload);
  const timeout=/timed out|timeout/i.test(String(payload.semantic_error||''));
  return {
    title:quota?'语义检索 API 额度不足':timeout?'语义检索响应超时':'语义检索暂不可用',
    message:'当前仅显示关键词匹配结果，相关性和排序质量可能下降。',
    detail:quota?AUTHOR_CONTACT:'可以稍后重新搜索，尝试恢复语义检索。',
  };
}

export function buildSearchParams({query,mode,categories=[],people=[],limit=50}) {
  const params = new URLSearchParams({q:query.trim(),scope:mode,limit:String(Math.min(MAX_RESULTS,Math.max(1,limit)))});
  for (const type of DOC_TYPES[mode] || DOC_TYPES.content) params.append('doc_types',type);
  for (const value of categories) params.append('categories',value);
  for (const value of people) params.append('participants',value);
  return params;
}

export async function fetchJson(url,{signal,timeoutMs=30000}={}) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  signal?.addEventListener('abort',cancel,{once:true});
  let timedOut = false;
  const timer = setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);
  try {
    const response = await fetch(url,{signal:controller.signal,cache:'no-store'});
    if (!response.ok) {
      let payload;
      try {payload=await response.json();} catch {payload=null;}
      throw serviceError(response.status,payload);
    }
    try { return await response.json(); }
    catch { throw new Error('服务返回了无法读取的数据，请重试。'); }
  } catch (error) {
    if (timedOut) throw new ServiceError('请求超时，请再次尝试。','服务响应时间较长；请稍后重试。','timeout');
    if (error.name==='TypeError') throw new ServiceError('暂时连接不上检索服务，请检查网络后重试。','后端也可能暂时离线；若持续出现，请联系作者 YQBelmont贲。','network');
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort',cancel);
  }
}

export function publicUrl(value) {
  try {const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url.href:'';}
  catch {return '';}
}

export function mediaUrl(value) {
  const safe=publicUrl(value);
  if (!safe) return '';
  const url=new URL(safe);
  if (url.hostname==='image.gcores.com'&&!url.username&&!url.password&&!url.port) return `/media/gcores${url.pathname}${url.search}`;
  if (url.hostname==='gcores.com'||url.hostname.endsWith('.gcores.com')) return `/api/media-asset?url=${encodeURIComponent(safe)}`;
  return '';
}

export function mapResult(result,index=0) {
  if (!result||typeof result!=='object'||typeof result.doc_id!=='string'||typeof result.item_title!=='string') throw new ServiceError('搜索结果格式不正确，请重试。');
  const lines=String(result.display_text||'').split('\n').filter(Boolean);
  const timeline=result.doc_type==='timeline_note';
  const titleOnly=result.doc_type==='item_title';
  const summary=timeline ? result.timeline_content||result.timeline_title||'' : titleOnly ? result.desc||result.excerpt||'' : lines[1]||lines[0]||result.desc||'';
  return {
    id:result.doc_id||`${result.item_id}-${index}`,itemId:result.item_id,title:result.item_title||result.title||'未命名节目',
    category:result.category||'',users:result.users||[],date:String(result.published_at||'').slice(0,10),
    time:result.display_timestamp||'',offset:result.start_ms??result.timeline_ms??0,
    cover:mediaUrl(result.cover_url||result.thumb_url),url:publicUrl(result.source_url),summary,
    context:timeline||titleOnly?result.excerpt||result.desc||'':result.display_text||result.excerpt||'',
    timelineTitle:result.timeline_title||'',timelineAsset:mediaUrl(result.timeline_asset_url),timelineLink:publicUrl(result.timeline_quote_href),
    transcript:(Array.isArray(result.neighbor_atoms)?result.neighbor_atoms:[]).filter(line=>line&&typeof line.text==='string').map(line=>({time:line.display_timestamp||'',text:line.text})),
  };
}

export async function searchLive(request,signal) {
  if (!request.query.trim()) return {results:[],notice:null};
  const payload=await fetchJson(`/api/search?${buildSearchParams(request)}`,{signal});
  if (payload?.error) throw serviceError(500,payload);
  if (!Array.isArray(payload?.results)) throw new Error('搜索结果格式不正确，请重试。');
  return {results:payload.results.map(mapResult),notice:degradationNotice(payload)};
}

export async function loadCatalog(signal) {
  const [meta,filters]=await Promise.all([
    fetchJson('/api/meta',{signal,timeoutMs:15000}),
    fetchJson('/api/participants',{signal,timeoutMs:15000}),
  ]);
  if (!meta||typeof meta!=='object'||!Array.isArray(filters?.program_types)||!Array.isArray(filters?.participants)||[...filters.program_types,...filters.participants].some(item=>!item||typeof item.name!=='string'||!item.name.trim())) throw new Error('筛选列表格式不正确，请重试。');
  return {meta,categories:filters.program_types,people:filters.participants};
}

export function sortResults(results,sort) {
  if (sort==='relevance') return results;
  return [...results].sort((a,b)=>(sort==='oldest'?a.date.localeCompare(b.date):b.date.localeCompare(a.date))||a.offset-b.offset);
}
