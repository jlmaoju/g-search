import {BRAND_CONTOURS} from './brand-contours.js';
import {seeded} from './quote-geometry.js';

export const BRAND_MARKS=Object.freeze([
  {id:'g',name:'G 标识',en:'G / ORIGIN'},
  {id:'he',name:'核',en:'核 / CHARACTER'},
  {id:'core',name:'核心符号',en:'CORE / CONNECT'},
]);

export function inContour(x,y,polygon){
  let hit=false;
  for(let i=0,j=polygon.length-1;i<polygon.length;j=i++){
    const [xi,yi]=polygon[i],[xj,yj]=polygon[j];
    if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)hit=!hit;
  }
  return hit;
}
export function inBrand(id,x,y){
  return BRAND_CONTOURS[id].reduce((hit,polygon)=>hit!==inContour(x,y,polygon),false);
}

// Thin extrusions of the official outlines, sampled by physical surface area.
// Shape data contains positions and normals; the renderer assigns persistent IDs.
export function brandGeometry(id,count){
  const contours=BRAND_CONTOURS[id];
  if(!contours)throw new TypeError('Unknown brand mark: '+id);
  const data=new Float32Array(count*10),rng=seeded(43187),depth=.19;
  let area=0,perimeter=0;const edges=[];
  for(const polygon of contours){
    const signedArea=polygon.reduce((sum,a,i)=>{const b=polygon[(i+1)%polygon.length];return sum+a[0]*b[1]-b[0]*a[1];},0)/2;
    area+=Math.abs(signedArea);
    for(let i=0;i<polygon.length;i++){
      const a=polygon[i],b=polygon[(i+1)%polygon.length],dx=b[0]-a[0],dy=b[1]-a[1],length=Math.hypot(dx,dy);
      if(length<1e-8)continue;
      perimeter+=length;
      edges.push({a,dx,dy,length,end:perimeter,nx:Math.sign(signedArea)*dy/length,ny:-Math.sign(signedArea)*dx/length});
    }
  }
  const faceFraction=2*area/(2*area+perimeter*depth);
  for(let i=0;i<count;i++){
    let x,y,z,nx=0,ny=0,nz=0;
    if(rng()<faceFraction){
      do{x=(rng()-.5)*2.5;y=(rng()-.5)*2.5;}while(!inBrand(id,x,y));
      nz=rng()<.5?1:-1;z=nz*depth/2;
    }else{
      const distance=rng()*perimeter;
      let lo=0,hi=edges.length-1;
      while(lo<hi){const mid=(lo+hi)>>1;if(edges[mid].end<distance)lo=mid+1;else hi=mid;}
      const edge=edges[lo],t=(distance-edge.end+edge.length)/edge.length;
      x=edge.a[0]+edge.dx*t;y=edge.a[1]+edge.dy*t;z=(rng()-.5)*depth;nx=edge.nx;ny=edge.ny;
    }
    data.set([x,y,z,nx,ny,nz,0,0,0,0],i*10);
  }
  return data;
}
