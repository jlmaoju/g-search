import {useEffect,useRef,useState} from 'react';
import {RiArrowRightUpLine,RiArrowDownSLine,RiEqualizerLine,RiCloseLine,RiArrowRightLine} from '@remixicon/react';
import '@fontsource-variable/oswald';
import {MAX_RESULTS,MODES,sortResults} from './api.js';
import {useCatalog,useLiveSearch} from './useLiveData.js';

const INITIAL_REQUEST={query:'',mode:'content',categories:[],people:[],limit:50};
const MODE_GUIDANCE={content:'搜索节目里聊过的话题、片段和记忆线索。',title:'记得节目名或其中几个字，可以从标题找起。',timeline:'搜索节目时间轴里的注释、作品名和知识点。'};
const REPOSITORY_URL='https://github.com/jlmaoju/g-search';
const typeLabel=name=>name==='会员专享'?'会员专享（免费部分）':name;

function Highlight({text='',query}) {
  if(!query||!text) return text;
  const i=text.toLowerCase().indexOf(query.toLowerCase());
  return i<0?text:<>{text.slice(0,i)}<mark>{text.slice(i,i+query.length)}</mark>{text.slice(i+query.length)}</>;
}

function Cover({entry}) {
  const [failed,setFailed]=useState(false);
  return <a className="cover-link" href={entry.url||undefined} target="_blank" rel="noreferrer" aria-label={`打开节目：${entry.title}`}>
    {entry.cover&&!failed?<img className="cover" src={entry.cover} alt={`${entry.title}封面`} loading="lazy" onError={()=>setFailed(true)}/>:<span className="cover-unavailable">封面暂不可用</span>}
  </a>;
}

function TimelineAsset({entry}) {
  const [failed,setFailed]=useState(false);
  return failed?<p className="asset-error" role="status">时间轴配图暂时无法加载，可打开原页查看。</p>:<img className="timeline-asset" src={entry.timelineAsset} alt={entry.timelineTitle||'时间轴配图'} loading="lazy" onError={()=>setFailed(true)}/>;
}

function ResultRow({entry,query,expanded,onToggle,mode}) {
  const [transcript,setTranscript]=useState(false);
  const contextId=`context-${entry.id.replaceAll(':','-')}`;
  const canExpand=Boolean(entry.context||entry.transcript.length||entry.timelineAsset||entry.timelineLink);
  return <article className={`result-row ${expanded?'is-expanded':''}`}>
    {expanded&&<span className="row-marker" aria-hidden="true"><img src="/assets/quote-marker.png" alt=""/></span>}
    <Cover entry={entry}/>
    <div className="result-copy">
      <h2><a href={entry.url||undefined} target="_blank" rel="noreferrer"><Highlight text={entry.title} query={mode==='title'?query:''}/></a></h2>
      <p className="metadata">{entry.category&&<span>{entry.category}</span>}{entry.date&&<span>{entry.date.replaceAll('-','/')}</span>}{mode!=='title'&&entry.time&&<span>{entry.time}</span>}</p>
      <p className="summary"><strong>{mode==='timeline'?'时间轴：':mode==='title'?'简介：':'摘要：'}</strong><Highlight text={mode==='timeline'?`${entry.timelineTitle}${entry.summary&&entry.summary!==entry.timelineTitle?` · ${entry.summary}`:''}`:entry.summary} query={query}/></p>
      {expanded&&canExpand&&<div className="context" id={contextId}>
        <p className="context-label">{mode==='content'?'命中片段':'节目介绍'}</p><p className="context-text"><Highlight text={entry.context||entry.summary} query={query}/></p>
        {mode==='timeline'&&entry.timelineAsset&&<TimelineAsset entry={entry}/>}
        {mode==='timeline'&&entry.timelineLink&&<a className="underlined-link timeline-reference" href={entry.timelineLink} target="_blank" rel="noreferrer">查看时间轴引用<RiArrowRightUpLine size={17}/></a>}
        {transcript&&entry.transcript.length>0&&<div className="transcript" id={`${contextId}-transcript`}><p className="transcript-note">节目转录 · 可能存在识别误差</p>{entry.transcript.map((line,i)=><p key={i}><time>{line.time}</time><span><Highlight text={line.text} query={query}/></span></p>)}</div>}
      </div>}
      <div className="result-actions">{canExpand&&<button className={`underlined-link ${expanded?'is-red':''}`} onClick={onToggle} aria-expanded={expanded} aria-controls={contextId}>{expanded?'收起上下文':'展开上下文'}</button>}{entry.url&&<a className="underlined-link" href={entry.url} target="_blank" rel="noreferrer">打开原页<RiArrowRightUpLine size={18} aria-hidden="true"/></a>}{expanded&&entry.transcript.length>0&&<button className="transcript-toggle" aria-expanded={transcript} aria-controls={`${contextId}-transcript`} onClick={()=>setTranscript(!transcript)}>{transcript?'收起逐字稿':'查看逐字稿'}</button>}</div>
    </div>
    {mode!=='title'&&entry.time&&<div className="hit-time"><strong>{entry.time}</strong><span>命中位置</span></div>}
  </article>;
}

function FilterOptions({options,selected,onToggle,kind}) {
  return options.map(({name})=><label key={name}><input type="checkbox" checked={selected.includes(name)} onChange={()=>onToggle(name)}/><span>{kind==='category'?typeLabel(name):name}</span></label>);
}

export function App() {
  const [request,setRequest]=useState(INITIAL_REQUEST);
  const [input,setInput]=useState(''),[sort,setSort]=useState('relevance');
  const [expansion,setExpansion]=useState({request:null,values:{}});
  const [modal,setModal]=useState(null),[personQuery,setPersonQuery]=useState('');
  const [draft,setDraft]=useState({categories:[],people:[]});
  const [limit,setLimit]=useState(50),[autoExpand,setAutoExpand]=useState(true),[fontSize,setFontSize]=useState('normal');
  const [inputError,setInputError]=useState('');
  const dialog=useRef(),inputRef=useRef();
  const catalog=useCatalog(),live=useLiveSearch(request);
  const {query,mode,categories,people}=request;
  const hasSearch=Boolean(query.trim());
  const results=sortResults(live.results,sort),activeCount=categories.length+people.length;
  const visiblePeople=catalog.people.filter(({name})=>name.toLowerCase().includes(personQuery.trim().toLowerCase()));
  const updated=String(catalog.meta?.library_updated_at||'').slice(0,10).replaceAll('-','/');
  useEffect(()=>{if(modal&&!dialog.current.open) dialog.current.showModal();if(!modal&&dialog.current.open) dialog.current.close();},[modal]);

  function performSearch(nextMode=mode,nextQuery=input,overrides={}) {
    const text=nextQuery.trim();
    if(!text){setInputError('先写下一点你记得的线索。');inputRef.current.focus();return;}
    setInput(text);setInputError('');setSort('relevance');
    setRequest({...request,query:text,mode:nextMode,limit,...overrides});
  }
  function resetSearch(){setInput('');setInputError('');setSort('relevance');setPersonQuery('');setExpansion({request:null,values:{}});setRequest({...INITIAL_REQUEST,limit});}
  function changeMode(nextMode){if(hasSearch)performSearch(nextMode,input.trim()||query);else {setInputError('');setRequest({...request,mode:nextMode});}}
  function changeFilters(filters){if(hasSearch)performSearch(mode,query,filters);else setRequest({...request,...filters});}
  function isExpanded(entry){return expansion.request===request&&entry.id in expansion.values?expansion.values[entry.id]:autoExpand&&entry.id===live.results[0]?.id;}
  function toggleExpanded(entry){const value=!isExpanded(entry);setExpansion(previous=>({request,values:{...(previous.request===request?previous.values:{}),[entry.id]:value}}));}
  function closeModal(){setModal(null);}
  function openFilters(){setDraft({categories:[...categories],people:[...people]});setPersonQuery('');setModal('filters');}
  function toggleDraft(key,value){setDraft(previous=>({...previous,[key]:previous[key].includes(value)?previous[key].filter(v=>v!==value):[...previous[key],value]}));}
  function clearFilters(){changeFilters({categories:[],people:[]});}
  function retrySearch(){setRequest({...request});}
  const status=live.pending?'正在寻找这段记忆…':live.error?'检索暂未完成':live.notice?`关键词匹配：当前显示 ${results.length} 条`:`当前显示 ${results.length} 条${sort==='relevance'?'（按相关度）':''}`;

  return <div className={`app-shell text-${fontSize} ${hasSearch?'has-search':'is-welcome'}`}>
    <a className="skip-link" href={hasSearch?'#results':'#search'}>{hasSearch?'跳到搜索结果':'跳到搜索'}</a>
    <header className="masthead"><div className="brand-line"><button className="brand" onClick={resetSearch} aria-label="Gsearch，回到首页"><img src="/assets/gsearch-logo.png" alt="gsearch"/></button><p className="tagline">想起一句，找到那期。</p></div><div className="utilities"><nav aria-label="页面选项"><button onClick={()=>setModal('about')}>关于</button><span aria-hidden="true">|</span><button onClick={()=>setModal('settings')}>设置</button></nav><p>{updated?`数据库更新于 ${updated}`:catalog.pending?'正在读取数据库信息…':'数据库信息暂不可用'}</p></div></header>
    <main>
      {!hasSearch&&<section className="welcome-intro" aria-labelledby="welcome-title"><h1 id="welcome-title">哪一期来着？</h1><p>输入记得的片段，找回那期节目。</p></section>}
      <section className="search-section" id="search" aria-label="检索机核节目"><form className="search-form" aria-busy={live.pending} onSubmit={e=>{e.preventDefault();performSearch();}}><input ref={inputRef} type="search" aria-label="搜索节目或记忆线索" aria-describedby={inputError?'query-error':!hasSearch?'mode-guidance':undefined} aria-invalid={!!inputError} value={input} onChange={e=>{setInput(e.target.value);setInputError('');}} placeholder={MODES.find(m=>m.id===mode).placeholder}/><button type="submit" className="cut-button search-submit">{live.pending?'搜索中':'搜索'}</button></form>
        <div className="search-options"><fieldset className="mode-selector"><legend className="sr-only">搜索方式，三选一</legend>{MODES.map(m=><label className={mode===m.id?'selected':''} key={m.id}><input type="radio" name="search-mode" value={m.id} checked={mode===m.id} onChange={()=>changeMode(m.id)}/><span>{m.label}</span></label>)}</fieldset><button className={`filter-button ${activeCount?'has-filters':''}`} aria-haspopup="dialog" onClick={openFilters}><RiEqualizerLine size={22} aria-hidden="true"/>筛选{activeCount>0&&<span className="filter-count">{activeCount}</span>}</button></div>
        {!hasSearch&&<p className="mode-guidance" id="mode-guidance" aria-live="polite">{MODE_GUIDANCE[mode]}</p>}
        {inputError&&<p className="input-error" id="query-error" role="alert">{inputError}</p>}
        {activeCount>0&&<div className="active-filters" aria-label="已选筛选条件">{categories.map(v=><button key={v} onClick={()=>changeFilters({categories:categories.filter(x=>x!==v)})}>{typeLabel(v)}<RiCloseLine size={15} aria-label="移除"/></button>)}{people.map(v=><button key={v} onClick={()=>changeFilters({people:people.filter(x=>x!==v)})}>{v}<RiCloseLine size={15} aria-label="移除"/></button>)}<button className="clear-filters" onClick={clearFilters}>清空筛选</button></div>}
      </section>
      {hasSearch&&<section className="results-section" id="results" aria-label="搜索结果" aria-busy={live.pending}>
        <div className="results-heading"><p role="status" aria-live="polite">{status}</p><span className="heading-rule"/><label className="sort-control"><span>排序：</span><select aria-label="结果排序" title="对当前返回的结果排序" value={sort} onChange={e=>setSort(e.target.value)}><option value="relevance">相关度</option><option value="oldest">时间从早到晚</option><option value="newest">时间从晚到早</option></select><RiArrowDownSLine size={17} aria-hidden="true"/></label></div>
        {live.notice&&<section className="search-notice degraded-notice" role="alert"><strong>{live.notice.title}</strong><p>{live.notice.message}</p><p>{live.notice.detail}</p><button className="underlined-link" onClick={retrySearch}>重新搜索</button></section>}
        {sort!=='relevance'&&!live.pending&&results.length>0&&<p className="search-notice">已对当前返回的 {results.length} 条结果按时间排序。</p>}
        {live.error&&<div className="empty-state" role="alert"><p className="empty-kicker">暂时没能完成检索</p><h2>{live.error}</h2>{live.errorDetail&&<p>{live.errorDetail}</p>}<div className="empty-actions"><button className="underlined-link" onClick={retrySearch}>重新搜索<RiArrowRightLine size={19}/></button></div></div>}
        <div className="results-list">{results.map(entry=><ResultRow key={`${mode}-${entry.id}`} entry={entry} query={query} mode={mode} expanded={isExpanded(entry)} onToggle={()=>toggleExpanded(entry)} />)}</div>
        {!results.length&&!live.pending&&!live.error&&<div className="empty-state"><p className="empty-kicker">换一条线索，再找找。</p><h2>{live.notice?'暂时没有找到关键词匹配结果':'暂时没有找到匹配结果'}</h2><p>{mode==='title'?'标题模式以节目名为检索范围。记得的是节目里的一句话，可以切回「内容」。':'试试更短的关键词，或放宽筛选条件。'}</p><div className="empty-actions">{activeCount>0&&<button className="underlined-link" onClick={clearFilters}>清空筛选<RiArrowRightLine size={19}/></button>}<button className="underlined-link" onClick={resetSearch}>回到首页</button></div></div>}
        {!live.pending&&!live.error&&results.length>=request.limit&&request.limit<MAX_RESULTS&&<button className="load-more underlined-link" onClick={()=>setRequest({...request,limit:Math.min(MAX_RESULTS,request.limit+50)})}>继续浏览<RiArrowRightLine size={20}/></button>}
        {!live.pending&&results.length>=MAX_RESULTS&&<p className="result-limit-note">已显示本次检索的前 {MAX_RESULTS} 条结果，可添加筛选缩小范围。</p>}
      </section>}
    </main>
    <footer>非官方机核电台检索<span aria-hidden="true">·</span><button onClick={()=>setModal('about')}>关于本库</button></footer>
    <dialog ref={dialog} className={`dialog ${modal==='filters'?'filter-dialog':''}`} onCancel={closeModal} onClick={e=>{if(e.target===e.currentTarget)closeModal();}} onClose={()=>setModal(null)} aria-labelledby="dialog-title">
      <div className="dialog-head"><h2 id="dialog-title">{modal==='filters'?'缩小寻找的范围':modal==='settings'?'阅读与显示':'关于 Gsearch'}</h2><button className="close-button" onClick={closeModal} aria-label="关闭面板"><RiCloseLine size={26}/></button></div>
      {modal==='filters'&&<div className="dialog-body filter-content">
        {catalog.pending?<p role="status">正在加载完整筛选列表…</p>:catalog.error?<div role="alert"><p>{catalog.error}</p>{catalog.errorDetail&&<p>{catalog.errorDetail}</p>}<button className="underlined-link" onClick={catalog.retry}>重新加载筛选</button></div>:<>
          <fieldset><legend>节目类型 <span className="option-total">{catalog.categories.length} 种</span></legend><div className="check-grid category-grid"><FilterOptions options={catalog.categories} selected={draft.categories} onToggle={value=>toggleDraft('categories',value)} kind="category"/>{!catalog.categories.length&&<p className="filter-empty">暂无可用的节目类型。</p>}</div></fieldset>
          <fieldset className="people-fieldset"><legend>参与者 <span className="option-total">{catalog.people.length} 位</span></legend><input className="person-search" type="search" placeholder="搜索全部参与者" aria-label="搜索参与者" value={personQuery} onChange={e=>setPersonQuery(e.target.value)}/><div className="check-grid people-grid"><FilterOptions options={visiblePeople} selected={draft.people} onToggle={value=>toggleDraft('people',value)}/>{!visiblePeople.length&&<p className="filter-empty">没有匹配的参与者</p>}</div></fieldset>
        </>}
        <div className="filter-selection" aria-live="polite">已选 {draft.categories.length} 种类型、{draft.people.length} 位参与者</div>
        <div className="dialog-actions"><button className="underlined-link" onClick={()=>setDraft({categories:[],people:[]})}>清空筛选</button><button className="cut-button" disabled={catalog.pending||!!catalog.error} onClick={()=>{changeFilters(draft);closeModal();}}>应用筛选</button></div>
      </div>}
      {modal==='settings'&&<div className="dialog-body settings-content"><label>每次检索条数<select value={limit} onChange={e=>{const value=Number(e.target.value);setLimit(value);setRequest({...request,limit:value});}}>{[20,50,100,200].map(value=><option key={value} value={value}>{value} 条结果</option>)}</select></label><label>正文字号<select value={fontSize} onChange={e=>setFontSize(e.target.value)}><option value="normal">标准</option><option value="large">较大</option></select></label><label className="check-setting"><input type="checkbox" checked={autoExpand} onChange={e=>setAutoExpand(e.target.checked)}/>搜索后自动展开第一条结果</label><p className="setting-note">时间排序作用于当前返回的结果。设置在本次打开期间保留。</p><button className="cut-button" onClick={closeModal}>完成</button></div>}
      {modal==='about'&&<div className="dialog-body about-content"><p className="about-lead">想起一句，找到那期。</p><p>一个帮助你找回机核节目与片段的检索工具。本项目仅覆盖免费节目，会不定期更新，来自一个野生怀旧老机组。</p><p>「内容」搜索节目片段与整期摘要；「标题」搜索节目名；「时间轴」搜索节目注释和知识点。</p>{catalog.meta&&<p>当前提供 {catalog.categories.length} 种节目类型、{catalog.people.length} 位参与者。数据库更新于 {updated}。</p>}<p><a className="underlined-link github-link" href={REPOSITORY_URL} target="_blank" rel="noopener noreferrer">GitHub 仓库<RiArrowRightUpLine size={18} aria-hidden="true"/></a></p><p>封面与节目信息来自机核，摘要与逐字稿来自现有检索数据；逐字稿可能存在识别误差。</p><div className="about-example"><span>可以试试</span>{['日剧','雪崩','人间拾录'].map(word=><button key={word} onClick={()=>{closeModal();performSearch('content',word);}}>{word}<RiArrowRightUpLine size={16}/></button>)}</div><p className="setting-note">非官方项目，与机核无隶属关系。</p></div>}
    </dialog>
  </div>;
}
