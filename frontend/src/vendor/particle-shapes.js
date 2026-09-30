import {logoGeometry} from './quote-geometry.js';
/*! Particle Shapes — portable WebGL point-cloud renderer. No external dependencies. */
export const ParticleShapes = (() => {
  'use strict';
  const DEFAULTS = Object.freeze({
    auto: true,
    study: 'reorder',
    paused: false,
    motionOverride: false,
    pointSize: .78,
    slow: false,
    initialModel: 0,
    models: null,
    modelRotations: null,
    pointerTarget: null,
    travel: null,
    center: [.5,.5],
    mobileCenter: [.5,.5],
    widthScale: .30,
    heightScale: .30,
    mobileWidthScale: .30,
    mobileHeightScale: .30,
    mouseTiltX: .16,
    mouseTiltY: .11,
    count: 30000,
    mobileCount: 20000,
    localFraction: 0.72,
    outerScatterFraction: 0.006,
    nearScatterFraction: 0.030,
    transitionSeconds: 2.6,
    holdSeconds: 4.8,
    slowRate: 0.35,
    maxPixelRatio: 2,
    color: [0.32, 0.40, 0.97]
  });
  const clamp = (v,a=0,b=1) => Math.min(b,Math.max(a,v));
  const rotations=study=>study==='ring'?[[-.025,-.045,0],[.52,-.32,-.12]]:study==='breath'?[[-.025,-.045,0],[-.10,.16,.015]]:[[-.025,-.045,0],[.05,.13,-.015]];
  function random(seed) {
    return () => {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  // Spatial ordering pairs nearby regions instead of sending local particles
  // randomly through the centre. Seeds and point appearance follow the same identity.
  function pairGeometries(source,count,config){
    function order(data,kind){
      const [rx,ry,rz]=(config.modelRotations||rotations(config.study))[kind];
      const sx=Math.sin(rx),cx=Math.cos(rx),sy=Math.sin(ry),cy=Math.cos(ry),sz=Math.sin(rz),cz=Math.cos(rz);
      const bits=n=>{n&=1023;n=(n|n<<16)&0x030000ff;n=(n|n<<8)&0x0300f00f;n=(n|n<<4)&0x030c30c3;n=(n|n<<2)&0x09249249;return n;};
      const key=new Uint32Array(count);
      for(let i=0;i<count;i++){
        const j=i*10,x=data[j],y=data[j+1],z=data[j+2];
        const y1=y*cx-z*sx,z1=y*sx+z*cx,x2=x*cy+z1*sy,z2=-x*sy+z1*cy;
        const quant=v=>Math.floor(clamp((v+2)/4)*1023);
        key[i]=bits(quant(x2*cz-y1*sz))|(bits(quant(x2*sz+y1*cz))<<1)|(bits(quant(z2))<<2);
      }
      return Array.from({length:count},(_,i)=>i).sort((i,j)=>key[i]-key[j]);
    }
    const indices=Array.from({length:count},(_,i)=>i);
    const orders=source.map((data,kind)=>config.models||config.study==='ring'?order(data,kind):indices);
    const paired=source.map(()=>new Float32Array(count*10)),rng=random(52497);
    for(let i=0;i<count;i++){
      const region=rng(),outer=region<config.outerScatterFraction;
      const halo=!outer&&region<config.outerScatterFraction+config.nearScatterFraction;
      const fuzz=outer?.24+Math.pow(rng(),.8)*.72:halo?.018+Math.pow(rng(),2)*.15:(rng()-.5)*.010;
      // The rare outer points have irregular 3D offsets, not a second smooth shell.
      const drift=outer?[(rng()-.5)*.24,(rng()-.5)*.24,(rng()-.5)*.24]:[0,0,0];
      const seeds=[rng(),rng(),rng()*(outer?.45:1),outer?.13+rng()*.07:halo?rng()*.12:.22+rng()*.78];
      for(let model=0;model<source.length;model++){
        const from=orders[model][i]*10,to=i*10;
        for(let axis=0;axis<3;axis++){
          const normal=source[model][from+3+axis];
          paired[model][to+axis]=source[model][from+axis]+normal*fuzz+drift[axis];
          paired[model][to+3+axis]=normal;
        }
        paired[model].set(seeds,to+6);
      }
    }
    return paired;
  }
  const vertex = `
    precision highp float;
    attribute vec3 aPosition;
    attribute vec3 aNormal;
    attribute vec4 aSeed;
    attribute vec3 aTargetPosition;
    attribute vec3 aTargetNormal;
    uniform vec2 uResolution;
    uniform vec2 uCenter;
    uniform vec3 uRotation;
    uniform vec3 uTargetRotation;
    uniform float uScale, uPixelRatio, uProgress, uRole, uTravel, uTime, uReduced, uLocalFraction, uPointSize;
    varying float vAlpha;
    varying float vTone;
    mat3 rotX(float a){float s=sin(a),c=cos(a);return mat3(1.,0.,0.,0.,c,s,0.,-s,c);}
    mat3 rotY(float a){float s=sin(a),c=cos(a);return mat3(c,0.,-s,0.,1.,0.,s,0.,c);}
    mat3 rotZ(float a){float s=sin(a),c=cos(a);return mat3(c,s,0.,-s,c,0.,0.,0.,1.);}
    void main(){
      mat3 rotation=rotZ(uRotation.z)*rotY(uRotation.y)*rotX(uRotation.x);
      vec3 p=rotation*aPosition;
      vec3 normal=rotation*aNormal;
      if(uRole>0.5 && uReduced<0.5){
        mat3 targetRotation=rotZ(uTargetRotation.z)*rotY(uTargetRotation.y)*rotX(uTargetRotation.x);
        vec3 target=targetRotation*aTargetPosition;
        vec3 targetNormal=targetRotation*aTargetNormal;
        bool local=aSeed.x<uLocalFraction;
        float start=local?(.04+.48*aSeed.y):(.02+.17*aSeed.y);
        float duration=local?(.20+.13*aSeed.z):(.57+.16*aSeed.z);
        float t=clamp((uProgress-start)/duration,0.,1.);
        float eased=t*t*(3.-2.*t);
        p=mix(p,target,eased);
        vec3 n=mix(normal,targetNormal,eased);
        normal=n/max(length(n),.0001);
        if(!local){
          // One curved path for one identity. Never render a second incoming copy.
          float arc=sin(t*3.14159265);
          float side=aSeed.z<.5?-1.:1.;
          p.x+=side*uTravel*arc*(.35+.45*aSeed.y);
          p.y+=arc*((aSeed.y-.5)*.40);
          p.z+=arc*(.12+.20*aSeed.z);
        }
      }
      // Very small coherent breathing; the geometry itself remains intact.
      p.y+=sin(uTime*.53)*.013*(1.-uReduced);
      float perspective=5.8/(5.8-p.z);
      vec2 pixel=uCenter+vec2(p.x,-p.y)*uScale*perspective;
      vec2 clip=pixel/uResolution*2.-1.;
      gl_Position=vec4(clip.x,-clip.y,0.,1.);
      gl_PointSize=uPointSize*(.82+.34*aSeed.z)*uPixelRatio;
      float facing=.34+.66*smoothstep(-.8,.85,normal.z);
      float light=.78+.22*max(0.,dot(normal,normalize(vec3(-.4,.7,1.))));
      // Intrinsic point opacity is independent of transition progress.
      vAlpha=.78+.16*aSeed.w;
      if(aSeed.w<.13) vAlpha*=.28;
      vTone=.08*(1.-facing)+.05*(1.-light);
    }
  `;
  const fragment = `
    precision mediump float;
    uniform vec3 uColor;
    varying float vAlpha;
    varying float vTone;
    void main(){
      float d=length(gl_PointCoord-.5);
      float coverage=1.-smoothstep(.32,.5,d);
      if(coverage<=0. || vAlpha<.001) discard;
      gl_FragColor=vec4(mix(uColor,vec3(1.),vTone),vAlpha*coverage);
    }
  `;
  function resolveOptions(options){
    const o={...DEFAULTS,...options};
    for(const k of ['count','mobileCount'])if(!Number.isInteger(o[k])||o[k]<1)throw new TypeError(k+' must be a positive integer');
    for(const k of ['transitionSeconds','holdSeconds','slowRate','maxPixelRatio','widthScale','heightScale','mobileWidthScale','mobileHeightScale'])if(!Number.isFinite(o[k])||o[k]<=0)throw new TypeError(k+' must be positive');
    for(const k of ['localFraction','outerScatterFraction','nearScatterFraction'])if(!Number.isFinite(o[k])||o[k]<0||o[k]>1)throw new TypeError(k+' must be in 0..1');
    if(o.outerScatterFraction+o.nearScatterFraction>1)throw new TypeError('scatter fractions must sum to at most 1');
    if(o.models!==null&&(!Array.isArray(o.models)||o.models.length<2||o.models.some(model=>typeof model!=='function')))throw new TypeError('models must contain at least two geometry functions');
    const modelCount=o.models?.length||2;
    if(!Number.isInteger(o.initialModel)||o.initialModel<0||o.initialModel>=modelCount)throw new TypeError('initialModel must identify a model');
    if(o.modelRotations!==null&&(!Array.isArray(o.modelRotations)||o.modelRotations.length!==modelCount||o.modelRotations.some(a=>!Array.isArray(a)||a.length!==3||a.some(v=>!Number.isFinite(v)))))throw new TypeError('modelRotations must contain one finite rotation per model');
    if(o.models&&!o.modelRotations)o.modelRotations=o.models.map(()=>[0,0,0]);
    if(o.travel!==null&&(!Number.isFinite(o.travel)||o.travel<0))throw new TypeError('travel must be nonnegative');
    for(const k of ['mouseTiltX','mouseTiltY','pointSize'])if(!Number.isFinite(o[k])||o[k]<0)throw new TypeError(k+' must be nonnegative');
    for(const [k,n] of [['color',3],['center',2],['mobileCenter',2]]){
      if(!Array.isArray(o[k])||o[k].length!==n||o[k].some(x=>!Number.isFinite(x)||x<0||x>1))throw new TypeError(k+' must contain '+n+' numbers in 0..1');
      o[k]=Object.freeze([...o[k]]);
    }
    o.auto=Boolean(o.auto);o.slow=Boolean(o.slow);
    return Object.freeze(o);
  }
  class ParticleScene {
    constructor(canvas,host,options={}){
      this.options=resolveOptions(options);this.destroyed=false;
      this.canvas=canvas;this.host=host;
      this.gl=canvas.getContext('webgl',{alpha:true,premultipliedAlpha:true,antialias:false,depth:false,powerPreference:'low-power'});
      if(!this.gl) throw new Error('此浏览器未能启用 WebGL。请在支持硬件加速的浏览器中打开这个文件。');
      this.events=new AbortController();this.signal=this.events.signal;
      this.paused=this.options.paused;this.override=this.options.motionOverride;
      this.motion=matchMedia('(prefers-reduced-motion: reduce)');this.reduced=this.motion.matches&&!this.override;
      this.count=host.getBoundingClientRect().width<=700?this.options.mobileCount:this.options.count;
      const geometries=this.options.models?this.options.models.map(model=>model(this.count)):[logoGeometry(0,this.count,this.options.study),logoGeometry(1,this.count,this.options.study)];
      if(geometries.some(data=>!(data instanceof Float32Array)||data.length!==this.count*10||data.some(v=>!Number.isFinite(v))))throw new TypeError('Geometry must have count × 10 finite floats');
      this.data=pairGeometries(geometries,this.count,this.options);this.modelCount=this.data.length;
      this.current=this.options.initialModel;this.from=this.current;this.to=(this.current+1)%this.modelCount;this.progress=0;this.active=false;this.scrubbing=false;
      this.auto=this.options.auto&&!this.reduced;this.slow=this.options.slow;this.hold=0;this.time=0;this.last=0;this.raf=0;this.pending=null;this.direction=1;
      this.pointer={x:0,y:0};this.tilt={x:0,y:0};this.velocity={x:0,y:0};
      this.initGL();this.resize();this.render();
      this.resizeObserver=new ResizeObserver(()=>{this.resize();this.render();this.wake();});this.resizeObserver.observe(host);
      const pointerHost=this.options.pointerTarget||host.closest('.brand')||host;
      pointerHost.addEventListener('pointermove',e=>{
        if(this.reduced)return;
        const r=host.getBoundingClientRect();
        this.pointer.x=clamp((e.clientX-r.left)/r.width*2-1,-1,1)*this.options.mouseTiltX;
        this.pointer.y=clamp((e.clientY-r.top)/r.height*2-1,-1,1)*this.options.mouseTiltY;
        this.wake();
      },{signal:this.signal,passive:true});
      pointerHost.addEventListener('pointerleave',()=>{this.pointer.x=this.pointer.y=0;this.wake();},{signal:this.signal});
      document.addEventListener('visibilitychange',()=>{this.last=0;if(document.hidden){cancelAnimationFrame(this.raf);this.raf=0;}else this.wake();},{signal:this.signal});
      this.motion.addEventListener('change',e=>{
        this.reduced=e.matches&&!this.override;
        if(this.reduced){this.setAuto(false);this.pointer.x=this.pointer.y=this.tilt.x=this.tilt.y=this.velocity.x=this.velocity.y=0;}
        this.emit('mode');this.wake();
      },{signal:this.signal});
      canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();cancelAnimationFrame(this.raf);this.raf=0;this.lost=true;this.emit('lost');},{signal:this.signal});
      canvas.addEventListener('webglcontextrestored',()=>{this.lost=false;this.initGL();this.resize();this.render();this.last=0;this.emit('restored');this.wake();},{signal:this.signal});
      this.wake();
    }
    initGL(){
      const gl=this.gl;
      const compile=(type,source)=>{
        const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);
        if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS)){const message=gl.getShaderInfoLog(shader);gl.deleteShader(shader);throw new Error(message);}
        return shader;
      };
      const vs=compile(gl.VERTEX_SHADER,vertex),fs=compile(gl.FRAGMENT_SHADER,fragment);
      this.program=gl.createProgram();gl.attachShader(this.program,vs);gl.attachShader(this.program,fs);gl.linkProgram(this.program);gl.deleteShader(vs);gl.deleteShader(fs);
      if(!gl.getProgramParameter(this.program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(this.program));
      gl.useProgram(this.program);
      this.locations={};
      for(const name of ['uResolution','uCenter','uRotation','uTargetRotation','uScale','uPixelRatio','uProgress','uRole','uTravel','uTime','uReduced','uLocalFraction','uColor','uPointSize'])this.locations[name]=gl.getUniformLocation(this.program,name);
      this.attributes=['aPosition','aNormal','aSeed','aTargetPosition','aTargetNormal'].map(name=>gl.getAttribLocation(this.program,name));
      this.buffers=this.data.map(data=>{const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);return b;});
      gl.disable(gl.DEPTH_TEST);gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA,gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
      gl.clearColor(0,0,0,0);
    }
    resize(){
      const r=this.host.getBoundingClientRect();this.width=Math.max(1,r.width);this.height=Math.max(1,r.height);
      this.dpr=Math.min(devicePixelRatio||1,this.options.maxPixelRatio);
      this.canvas.width=Math.round(this.width*this.dpr);this.canvas.height=Math.round(this.height*this.dpr);
      this.gl.viewport(0,0,this.canvas.width,this.canvas.height);
      this.mobile=this.width<=700;
      this.scale=this.mobile?Math.min(this.width*this.options.mobileWidthScale,this.height*this.options.mobileHeightScale):Math.min(this.width*this.options.widthScale,this.height*this.options.heightScale);
      const centre=this.mobile?this.options.mobileCenter:this.options.center;
      this.center=[this.width*centre[0],this.height*centre[1]];
      // Both end positions are outside the canvas, even after perspective and resize.
      this.travel=this.options.travel??(this.options.study==='breath'?.12:this.options.study==='ring'?.68:.28);
    }
    emit(type){this.host.dispatchEvent(new CustomEvent('particlechange',{detail:{type,...this.state}}));}
    get state(){return {current:this.current,from:this.from,to:this.to,modelCount:this.modelCount,progress:this.progress,active:this.active,scrubbing:this.scrubbing,auto:this.auto,slow:this.slow,reduced:this.reduced,count:this.count,pending:this.pending,direction:this.direction,paused:this.paused};}
    transitionTo(target){
      if(!Number.isInteger(target)||target<0||target>=this.modelCount)return;
      if(this.reduced){
        this.from=this.current;this.to=target;this.current=target;this.progress=1;
        this.active=false;this.scrubbing=false;this.pending=null;this.hold=0;
        this.emit('finish');this.wake();return;
      }
      if(this.scrubbing||this.active){
        this.scrubbing=false;
        this.pending=target===this.from||target===this.to?null:target;
        this.direction=target===this.from?-1:1;
        this.active=true;this.hold=0;this.emit('start');this.wake();return;
      }
      if(target===this.current)return;
      this.from=this.current;this.to=target;this.progress=0;this.direction=1;this.active=true;this.hold=0;this.emit('start');this.wake();
    }
    next(){
      this.setPaused(false);
      if(this.scrubbing&&(this.progress<=0||this.progress>=1)){
        this.current=this.progress>=1?this.to:this.from;this.active=false;this.scrubbing=false;this.progress=0;
      }
      this.transitionTo(this.active?this.to:(this.current+1)%this.modelCount);
    }
    seek(value){
      this.setAuto(false);
      this.setPaused(true);
      if(!this.active&&!this.scrubbing&&this.progress===0){this.from=this.current;this.to=(this.current+1)%this.modelCount;}
      this.active=true;this.scrubbing=true;this.direction=1;this.pending=null;this.progress=clamp(value);this.hold=0;
      this.emit('seek');this.render();
    }
    setAuto(value){this.auto=Boolean(value)&&!this.reduced;if(this.auto){this.scrubbing=false;this.direction=1;}this.hold=0;this.emit('mode');this.wake();}
    setSlow(value){this.slow=Boolean(value);this.emit('mode');this.wake();}
    setPaused(value){this.paused=Boolean(value);this.last=0;if(this.paused){cancelAnimationFrame(this.raf);this.raf=0;this.render();}else this.wake();this.emit('mode');}
    setMotionOverride(value){this.override=Boolean(value);this.reduced=this.motion.matches&&!this.override;if(this.reduced){this.auto=false;this.pointer.x=this.pointer.y=this.tilt.x=this.tilt.y=this.velocity.x=this.velocity.y=0;}this.render();this.emit('mode');this.wake();}
    wake(){if(!this.destroyed&&!this.paused&&!this.raf&&!this.lost&&!document.hidden)this.raf=requestAnimationFrame(t=>this.frame(t));}
    frame(now){
      this.raf=0;if(this.paused||this.destroyed||this.lost)return;
      const dt=this.last?Math.min((now-this.last)/1000,.05):1/60;this.last=now;
      const rate=this.slow?this.options.slowRate:1;
      if(!this.scrubbing)this.time+=dt*rate;
      // A damped spring follows the mouse with a small, bounded physical lag.
      for(const axis of ['x','y']){
        this.velocity[axis]+=(80*(this.pointer[axis]-this.tilt[axis])-18*this.velocity[axis])*dt;
        this.tilt[axis]+=this.velocity[axis]*dt;
      }
      if(this.active&&!this.scrubbing){
        this.progress=clamp(this.progress+this.direction*dt*rate/(this.reduced?.3:this.options.transitionSeconds));
        this.emit('progress');
        if(this.direction>0&&this.progress>=1||this.direction<0&&this.progress<=0){
          this.current=this.direction>0?this.to:this.from;this.active=false;this.hold=0;this.emit('finish');
          if(this.pending!==null){const target=this.pending;this.pending=null;this.transitionTo(target);}
        }
      }else if(!this.active&&this.auto){this.hold+=dt;if(this.hold>=this.options.holdSeconds)this.next();}
      this.render();
      if(!this.reduced||this.active&&!this.scrubbing||this.auto)this.wake();else this.last=0;
    }
    render(){
      if(this.destroyed||this.lost)return;
      const gl=this.gl,u=this.locations;
      gl.clear(gl.COLOR_BUFFER_BIT);gl.useProgram(this.program);
      gl.uniform2f(u.uResolution,this.width,this.height);gl.uniform2f(u.uCenter,...this.center);
      gl.uniform1f(u.uScale,this.scale);gl.uniform1f(u.uPixelRatio,this.dpr);gl.uniform1f(u.uProgress,this.progress);
      gl.uniform1f(u.uTime,this.time);gl.uniform1f(u.uReduced,this.reduced?1:0);gl.uniform1f(u.uTravel,this.travel);
      gl.uniform3f(u.uColor,...this.options.color);gl.uniform1f(u.uPointSize,this.options.pointSize*Math.min(1,Math.max(.55,this.width/132)));gl.uniform1f(u.uLocalFraction,this.options.localFraction);
      if(this.active&&this.reduced)this.draw(this.progress<.5?this.from:this.to,0);
      else if(this.active&&this.progress<=0)this.draw(this.from,0);
      else if(this.active&&this.progress>=1)this.draw(this.to,0);
      else if(this.active){this.draw(this.from,1);}else this.draw(this.current,0);
    }
    draw(model,role){
      const gl=this.gl,u=this.locations;
      gl.bindBuffer(gl.ARRAY_BUFFER,this.buffers[model]);
      for(let i=0;i<3;i++){gl.enableVertexAttribArray(this.attributes[i]);gl.vertexAttribPointer(this.attributes[i],i===2?4:3,gl.FLOAT,false,40,i===0?0:i===1?12:24);}
      const target=role?this.to:model;
      gl.bindBuffer(gl.ARRAY_BUFFER,this.buffers[target]);
      for(let i=3;i<5;i++){gl.enableVertexAttribArray(this.attributes[i]);gl.vertexAttribPointer(this.attributes[i],3,gl.FLOAT,false,40,i===3?0:12);}
      const idle=this.reduced?0:1;
      const angles=kind=>{const base=(this.options.modelRotations||rotations(this.options.study))[kind];return [base[0]+this.tilt.y+Math.sin(this.time*.27)*.018*idle,base[1]+this.tilt.x+Math.sin(this.time*.21)*.033*idle,base[2]];};
      gl.uniform3f(u.uRotation,...angles(model));gl.uniform3f(u.uTargetRotation,...angles(target));
      gl.uniform1f(u.uRole,role);gl.drawArrays(gl.POINTS,0,this.count);
    }
    destroy(){if(this.destroyed)return;this.destroyed=true;cancelAnimationFrame(this.raf);this.raf=0;this.events.abort();this.resizeObserver.disconnect();for(const b of this.buffers)this.gl.deleteBuffer(b);this.gl.deleteProgram(this.program);}
  }

  function mount(host,options={}){
    if(!(host instanceof HTMLElement))throw new TypeError('mount requires an HTML container');
    const canvas=document.createElement('canvas');
    canvas.setAttribute('aria-hidden','true');
    canvas.style.cssText='position:absolute;inset:0;width:100%;height:100%;pointer-events:none';
    const previousPosition=host.style.position;
    const adjustPosition=getComputedStyle(host).position==='static';
    if(adjustPosition)host.style.position='relative';
    host.appendChild(canvas);
    const cleanup=()=>{canvas.remove();if(adjustPosition&&host.style.position==='relative')host.style.position=previousPosition;};
    let scene;
    try{scene=new ParticleScene(canvas,host,options);}catch(error){cleanup();throw error;}
    const destroy=scene.destroy.bind(scene);
    scene.destroy=()=>{destroy();cleanup();};
    return scene;
  }
  return Object.freeze({mount,Scene:ParticleScene,defaults:resolveOptions({})});
})();
