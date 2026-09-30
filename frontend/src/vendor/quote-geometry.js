// Authored mark coordinates measured from the existing logo; Y points upward.
export const OUTLINE=[[-1,1.22],[1,1.22],[1,-.36],[.20,-1.22],[-1,-1.22],[-.34,-.36],[-1,-.36]];
export function seeded(seed){return()=>{seed|=0;seed=seed+0x6D2B79F5|0;let t=Math.imul(seed^seed>>>15,1|seed);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
export function inside(x,y){let hit=false;for(let i=0,j=OUTLINE.length-1;i<OUTLINE.length;j=i++){const [xi,yi]=OUTLINE[i],[xj,yj]=OUTLINE[j];if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)hit=!hit;}return hit;}
export function quoteGeometry(count){
  const rng=seeded(19742),data=new Float32Array(count*10),depth=.24;
  const edges=OUTLINE.map((a,i)=>{const b=OUTLINE[(i+1)%OUTLINE.length],dx=b[0]-a[0],dy=b[1]-a[1];return{a,b,length:Math.hypot(dx,dy),dx,dy};});
  const perimeter=edges.reduce((v,e)=>v+e.length,0);
  const area=Math.abs(OUTLINE.reduce((v,a,i)=>{const b=OUTLINE[(i+1)%OUTLINE.length];return v+a[0]*b[1]-b[0]*a[1];},0))/2;
  const frontFraction=area*2/(area*2+perimeter*depth);
  for(let i=0;i<count;i++){
    let x,y,z,nx=0,ny=0,nz=0;
    if(rng()<frontFraction){do{x=rng()*2-1;y=rng()*2.44-1.22;}while(!inside(x,y));nz=rng()<.5?1:-1;z=nz*depth/2;}
    else{let distance=rng()*perimeter,e=edges[0];for(const candidate of edges){e=candidate;if(distance<=e.length)break;distance-=e.length;}const t=distance/e.length;x=e.a[0]+e.dx*t;y=e.a[1]+e.dy*t;z=(rng()-.5)*depth;nx=-e.dy/e.length;ny=e.dx/e.length;}
    data.set([x,y,z,nx,ny,nz,0,0,0,0],i*10);
  }
  return data;
}
export function logoGeometry(kind,count,study='reorder'){
  const data=quoteGeometry(count);
  if(kind===0)return data;
  if(study==='ring'){
    const rng=seeded(22533),R=.86,r=.30;
    for(let i=0;i<count;i++){const u=rng()*Math.PI*2;let v;do{v=rng()*Math.PI*2;}while(rng()>(R+r*Math.cos(v))/(R+r));const nx=Math.cos(v)*Math.cos(u),ny=Math.cos(v)*Math.sin(u),nz=Math.sin(v);data.set([(R+r*Math.cos(v))*Math.cos(u),(R+r*Math.cos(v))*Math.sin(u),r*Math.sin(v),nx,ny,nz,0,0,0,0],i*10);}
  }else{
    for(let i=0;i<count;i++){const j=i*10,x=data[j],y=data[j+1];if(study==='breath'){data[j]*=1.015;data[j+2]*=1.65;}
      else{const band=Math.floor((1.22-y)/.41);data[j]+=Math.sin(band*1.05)*.075;data[j+1]+=Math.cos(band*.9)*.025;data[j+2]+=.21*Math.sin(y*2.2)+.055*x;}}
  }
  return data;
}
