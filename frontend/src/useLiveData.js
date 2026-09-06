import {useEffect,useState} from 'react';
import {loadCatalog,searchLive} from './api.js';

export function useCatalog() {
  const [version,retry]=useState(0);
  const [state,setState]=useState({pending:true,error:'',meta:null,categories:[],people:[]});
  useEffect(()=>{
    const controller=new AbortController();
    setState(previous=>({...previous,pending:true,error:''}));
    loadCatalog(controller.signal).then(data=>{
      if (!controller.signal.aborted) setState({...data,pending:false,error:''});
    }).catch(error=>{
      if (!controller.signal.aborted) setState(previous=>({...previous,pending:false,error:error.message,errorDetail:error.detail||''}));
    });
    return ()=>controller.abort();
  },[version]);
  return {...state,retry:()=>retry(value=>value+1)};
}

export function useLiveSearch(request) {
  const [state,setState]=useState({request:null,results:[],pending:false,error:'',notice:null});
  useEffect(()=>{
    if (!request.query.trim()) return;
    const controller=new AbortController();
    setState({request,results:[],pending:true,error:'',notice:null});
    searchLive(request,controller.signal).then(data=>{
      if (!controller.signal.aborted) setState({...data,request,pending:false,error:''});
    }).catch(error=>{
      if (!controller.signal.aborted) setState({request,results:[],pending:false,error:error.message,errorDetail:error.detail||'',notice:null});
    });
    return ()=>controller.abort();
  },[request]);
  if (!request.query.trim()) return {request,results:[],pending:false,error:'',notice:null};
  // Do not label the previous response with the newly selected query or scope.
  return state.request===request?state:{request,results:[],pending:true,error:'',notice:null};
}
