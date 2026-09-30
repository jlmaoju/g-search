import test from 'node:test';
import assert from 'node:assert/strict';
import {ParticleShapes} from '../src/vendor/particle-shapes.js';
import {logoGeometry,inside} from '../src/vendor/quote-geometry.js';
import {BRAND_MARKS,brandGeometry,inBrand} from '../src/vendor/brand-geometry.js';

function environment({noWebGL=false,reduced=false}={}){
  let nextFrame=1,boundBuffer=null;const frames=new Map(),allocated=new Set(),deleted=new Set(),draws=[],uniforms={},bindings={};
  const gl={VERTEX_SHADER:1,FRAGMENT_SHADER:2,COMPILE_STATUS:3,LINK_STATUS:4,ARRAY_BUFFER:5,STATIC_DRAW:6,DEPTH_TEST:7,BLEND:8,SRC_ALPHA:9,ONE_MINUS_SRC_ALPHA:10,ONE:11,FLOAT:12,POINTS:13,COLOR_BUFFER_BIT:14,
    createShader:()=>({}),shaderSource(){},compileShader(){},getShaderParameter:()=>true,deleteShader(){},
    createProgram(){const p={kind:'program'};allocated.add(p);return p;},attachShader(){},linkProgram(){},getProgramParameter:()=>true,useProgram(){},getUniformLocation:(_,n)=>n,getAttribLocation:(_,n)=>n,
    createBuffer(){const b={kind:'buffer'};allocated.add(b);return b;},bindBuffer(_,buffer){boundBuffer=buffer;},bufferData(){},disable(){},enable(){},blendFuncSeparate(){},clearColor(){},viewport(){},clear(){},uniform2f(){},uniform3f(){},uniform1f:(k,v)=>{uniforms[k]=v;},enableVertexAttribArray(){},vertexAttribPointer(name){bindings[name]=boundBuffer;},drawArrays:(mode,first,count)=>draws.push({mode,first,count,role:uniforms.uRole,source:bindings.aPosition,target:bindings.aTargetPosition}),deleteBuffer:b=>deleted.add(b),deleteProgram:p=>deleted.add(p),
  };
  class Host{constructor(){this.style={position:''};this.children=[];this.listeners=[];}getBoundingClientRect(){return{width:132,height:132,left:0,top:0};}appendChild(c){this.children.push(c);c.parent=this;}addEventListener(type,fn,options){this.listeners.push({type,fn,options});}dispatchEvent(){}closest(){return null;}}
  globalThis.HTMLElement=Host;globalThis.devicePixelRatio=1;globalThis.getComputedStyle=()=>({position:'static'});
  globalThis.matchMedia=()=>({matches:reduced,addEventListener(){}});
  globalThis.requestAnimationFrame=fn=>{const id=nextFrame++;frames.set(id,fn);return id;};globalThis.cancelAnimationFrame=id=>frames.delete(id);
  globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;}observe(){}disconnect(){this.disconnected=true;}};
  globalThis.document={hidden:false,addEventListener(){},createElement(){return{style:{},setAttribute(){},addEventListener(){},getContext:()=>noWebGL?null:gl,remove(){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1);}};}};
  if(!globalThis.CustomEvent)globalThis.CustomEvent=class{constructor(type,init){this.type=type;this.detail=init.detail;}};
  return{host:new Host(),gl,frames,draws,allocated,deleted,uniforms};
}

test('quote and target surfaces are deterministic, finite and have valid normals',()=>{
  for(const study of ['breath','reorder','ring'])for(const kind of [0,1]){
    const data=logoGeometry(kind,1000,study);assert.deepEqual(data,logoGeometry(kind,1000,study));assert.equal(data.length,10000);
    for(let i=0;i<1000;i++){const j=i*10;assert.ok(data.slice(j,j+6).every(Number.isFinite));assert.ok(Math.abs(Math.hypot(...data.slice(j+3,j+6))-1)<1e-5);if(kind===0&&Math.abs(data[j+5])===1)assert.ok(inside(data[j],data[j+1]));}
  }
});

test('one draw and one identity population at every phase, including reverse transitions',()=>{
  for(const study of ['breath','reorder','ring']){
    const env=environment();const scene=ParticleShapes.mount(env.host,{study,count:500,mobileCount:500,auto:false});
    for(const progress of [0,.25,.5,.75,1]){scene.seek(progress);env.draws.length=0;scene.render();assert.equal(env.draws.length,1);assert.equal(env.draws[0].count,500);}
    for(let i=0;i<500;i++)assert.deepEqual(scene.data[0].slice(i*10+6,i*10+10),scene.data[1].slice(i*10+6,i*10+10));
    scene.transitionTo(scene.from);assert.equal(scene.direction,-1);assert.equal(scene.scrubbing,false);scene.destroy();
  }
});

test('pause stops scheduling; destroy is idempotent and releases the canvas and GPU resources',()=>{
  const env=environment();const scene=ParticleShapes.mount(env.host,{count:50,mobileCount:50});assert.ok(env.frames.size>0);
  scene.setPaused(true);assert.equal(env.frames.size,0);scene.wake();assert.equal(env.frames.size,0);
  scene.setPaused(false);assert.ok(env.frames.size>0);scene.destroy();scene.destroy();assert.equal(env.frames.size,0);assert.equal(env.host.children.length,0);assert.equal(env.host.style.position,'');assert.deepEqual(env.deleted,env.allocated);assert.ok(scene.signal.aborted);assert.ok(scene.resizeObserver.disconnected);
});

test('next at a scrubbed endpoint starts the opposite direction',()=>{
  const env=environment();const scene=ParticleShapes.mount(env.host,{count:50,mobileCount:50,auto:false});
  scene.seek(1);scene.next();assert.equal(scene.from,1);assert.equal(scene.to,0);assert.equal(scene.progress,0);assert.equal(scene.active,true);scene.destroy();
});

test('reduced motion is static and manual shape selection completes immediately',()=>{
  const env=environment({reduced:true});const scene=ParticleShapes.mount(env.host,{count:50,mobileCount:50,auto:true});assert.equal(scene.auto,false);scene.transitionTo(1);assert.equal(scene.current,1);assert.equal(scene.active,false);scene.setAuto(true);assert.equal(scene.auto,false);scene.destroy();
});

test('WebGL unavailable removes temporary canvas and restores host styling',()=>{
  const env=environment({noWebGL:true});assert.throws(()=>ParticleShapes.mount(env.host),/WebGL/);assert.equal(env.host.children.length,0);assert.equal(env.host.style.position,'');
});

const brandModels=BRAND_MARKS.map(mark=>count=>brandGeometry(mark.id,count));
test('official brand silhouettes retain their negative space and valid 3D surface normals',()=>{
  for(const mark of BRAND_MARKS){
    const data=brandGeometry(mark.id,1200);
    assert.deepEqual(data,brandGeometry(mark.id,1200));
    for(let i=0;i<1200;i++){
      const j=i*10;assert.ok(data.slice(j,j+6).every(Number.isFinite));
      assert.ok(Math.abs(Math.hypot(...data.slice(j+3,j+6))-1)<1e-5);
      assert.ok(Math.abs(data[j+2])<=.096);
      if(Math.abs(data[j+5])===1)assert.ok(inBrand(mark.id,data[j],data[j+1]));
    }
  }
  assert.equal(inBrand('core',0,0),false,'core symbol keeps its open centre');
  assert.equal(inBrand('g',-.35,.20),false,'G keeps its counter');
});

test('all six brand directions bind the requested target with one persistent population',()=>{
  for(let from=0;from<3;from++)for(let to=0;to<3;to++){
    if(from===to)continue;
    const env=environment(),scene=ParticleShapes.mount(env.host,{models:brandModels,initialModel:from,count:240,mobileCount:240,auto:false});
    scene.transitionTo(to);
    for(const progress of [0,.25,.5,.75,1]){
      scene.seek(progress);env.draws.length=0;scene.render();assert.equal(env.draws.length,1);
      assert.equal(env.draws[0].count,240);
      assert.equal(env.draws[0].source,scene.buffers[progress===1?to:from]);
      if(progress>0&&progress<1)assert.equal(env.draws[0].target,scene.buffers[to]);
    }
    for(let i=0;i<240;i++)for(let model=1;model<3;model++)assert.deepEqual(scene.data[0].slice(i*10+6,i*10+10),scene.data[model].slice(i*10+6,i*10+10));
    scene.destroy();assert.deepEqual(env.deleted,env.allocated);
  }
});

test('three-shape sequencing wraps and a third target queues during an unfinished morph',()=>{
  const env=environment(),scene=ParticleShapes.mount(env.host,{models:brandModels,count:80,mobileCount:80,auto:false,transitionSeconds:.1});
  for(const target of [1,2,0]){scene.next();assert.equal(scene.to,target);scene.seek(1);}
  scene.next();scene.seek(.4);scene.transitionTo(2);assert.equal(scene.to,1);assert.equal(scene.pending,2);
  scene.setPaused(false);
  for(let i=0;i<30;i++)scene.frame(1000+i*17);
  assert.equal(scene.current,2);assert.equal(scene.active,false);assert.equal(scene.pending,null);
  scene.transitionTo(0);scene.seek(.6);scene.transitionTo(2);assert.equal(scene.direction,-1);
  scene.setPaused(false);for(let i=0;i<20;i++)scene.frame(2000+i*17);
  assert.equal(scene.current,2);assert.equal(scene.active,false);scene.destroy();
});

test('reduced motion handles every brand mark immediately and ignores invalid targets',()=>{
  const env=environment({reduced:true}),scene=ParticleShapes.mount(env.host,{models:brandModels,count:80,mobileCount:80});
  for(const target of [2,0,1]){scene.transitionTo(target);assert.equal(scene.current,target);assert.equal(scene.active,false);}
  for(const target of [-1,3,1.5,NaN])scene.transitionTo(target);
  assert.equal(scene.current,1);assert.equal(scene.auto,false);scene.destroy();
});

test('initially paused backgrounds render and resize without scheduling animation',()=>{
  const env=environment({reduced:true}),scene=ParticleShapes.mount(env.host,{models:brandModels,count:80,mobileCount:80,paused:true,auto:false});
  assert.equal(env.draws.length,1);assert.equal(env.frames.size,0);
  env.draws.length=0;scene.resizeObserver.callback();
  assert.equal(env.draws.length,1);assert.equal(env.frames.size,0);scene.destroy();
});
