import {ParticleShapes} from '../vendor/particle-shapes.js';

export function loadBrandGeometries(count,{signal,workerFactory=()=>new Worker(new URL('./brand-worker.js',import.meta.url),{type:'module'})}={}){
  return new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(new DOMException('Aborted','AbortError'));return;}
    const worker=workerFactory();
    let timer;
    function finish(error,data){
      clearTimeout(timer);signal?.removeEventListener('abort',abort);
      worker.removeEventListener('message',message);worker.removeEventListener('error',failed);worker.removeEventListener('messageerror',failed);
      worker.terminate();
      if(error)reject(error);else resolve(data);
    }
    const abort=()=>finish(new DOMException('Aborted','AbortError'));
    const failed=()=>finish(new Error('Particle geometry unavailable'));
    const message=event=>{
      const data=event.data;
      if(!Array.isArray(data)||data.length!==3||data.some(shape=>!(shape instanceof Float32Array)||shape.length!==count*10))return failed();
      finish(null,data);
    };
    worker.addEventListener('message',message);
    worker.addEventListener('error',failed);
    worker.addEventListener('messageerror',failed);
    signal?.addEventListener('abort',abort,{once:true});
    timer=setTimeout(failed,10000);
    try{worker.postMessage({count});}catch(error){finish(error);}
  });
}

export async function createBrandScene(host,{signal,getAnimated}){
  const count=host.getBoundingClientRect().width<=700?16000:36000;
  const geometries=await loadBrandGeometries(count,{signal});
  if(signal.aborted)throw new DOMException('Aborted','AbortError');
  const animated=getAnimated();
  return ParticleShapes.mount(host,{
    models:geometries.map(data=>()=>data),modelRotations:[[-.06,.12,-.06],[.045,-.12,.025],[.10,.16,.08]],initialModel:1,
    count,mobileCount:count,color:[.93,.025,.22],
    center:[.77,.44],mobileCenter:[.76,.28],
    widthScale:.245,heightScale:.345,mobileWidthScale:.32,mobileHeightScale:.24,
    pointSize:1.18,localFraction:.76,outerScatterFraction:.004,nearScatterFraction:.024,
    mouseTiltX:.11,mouseTiltY:.08,travel:.66,transitionSeconds:3.8,holdSeconds:8,
    pointerTarget:host.closest('.site-surface'),maxPixelRatio:1.75,
    auto:animated,paused:!animated,motionOverride:animated,
  });
}
