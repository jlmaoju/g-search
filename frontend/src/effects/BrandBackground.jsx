import {useEffect,useRef,useState} from 'react';
import './brand-background.css';

export function useBackgroundMotion(){
  const [choice,setChoice]=useState(null);
  const [reduced,setReduced]=useState(()=>matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(()=>{
    const media=matchMedia('(prefers-reduced-motion: reduce)'),change=()=>setReduced(media.matches);
    media.addEventListener('change',change);
    return()=>media.removeEventListener('change',change);
  },[]);
  return [choice??!reduced,setChoice];
}

function applyMotion(scene,animated){
  scene.setMotionOverride(animated);
  scene.setAuto(animated);
  scene.setPaused(!animated);
}

export function BrandBackground({animated}){
  const hostRef=useRef(null),sceneRef=useRef(null),animatedRef=useRef(animated);
  animatedRef.current=animated;
  const [state,setState]=useState('loading');
  useEffect(()=>{
    const host=hostRef.current,controller=new AbortController();
    const receive=event=>{
      if(event.detail.type==='lost')setState('unavailable');
      if(event.detail.type==='restored')setState('ready');
    };
    host.addEventListener('particlechange',receive,{signal:controller.signal});
    async function start(){
      try{
        // Load decoration after the first paint; sample contours in a worker.
        const {createBrandScene}=await import('./brand-runtime.js');
        if(controller.signal.aborted)return;
        const scene=await createBrandScene(host,{signal:controller.signal,getAnimated:()=>animatedRef.current});
        if(controller.signal.aborted){scene.destroy();return;}
        sceneRef.current=scene;applyMotion(scene,animatedRef.current);setState('ready');
      }catch{
        // A decorative effect failing must never take down the search UI.
        if(!controller.signal.aborted)setState('unavailable');
      }
    }
    const idle=typeof requestIdleCallback==='function';
    const scheduled=idle?requestIdleCallback(start,{timeout:1200}):setTimeout(start,120);
    return()=>{
      if(idle)cancelIdleCallback(scheduled);else clearTimeout(scheduled);
      controller.abort();sceneRef.current?.destroy();sceneRef.current=null;
    };
  },[]);
  useEffect(()=>{if(sceneRef.current)applyMotion(sceneRef.current,animated);},[animated]);
  return <div ref={hostRef} className="brand-background" aria-hidden="true" data-state={state} data-motion={animated?'on':'off'}/>;
}
