import test from 'node:test';
import assert from 'node:assert/strict';
import {loadBrandGeometries} from '../src/effects/brand-runtime.js';

class GeometryWorker extends EventTarget{
  terminated=0;
  postMessage(message){this.request=message;}
  terminate(){this.terminated++;}
  respond(data){const event=new Event('message');event.data=data;this.dispatchEvent(event);}
}

test('background geometry is prepared off-thread and the worker is released after transfer',async()=>{
  const worker=new GeometryWorker(),controller=new AbortController();
  const pending=loadBrandGeometries(20,{signal:controller.signal,workerFactory:()=>worker});
  assert.deepEqual(worker.request,{count:20});
  const data=Array.from({length:3},()=>new Float32Array(200));worker.respond(data);
  assert.equal(await pending,data);assert.equal(worker.terminated,1);
  controller.abort();assert.equal(worker.terminated,1,'completion detaches the abort listener');
});

test('unmount aborts background preparation and ignores a late worker response',async()=>{
  const worker=new GeometryWorker(),controller=new AbortController();
  const pending=loadBrandGeometries(20,{signal:controller.signal,workerFactory:()=>worker});
  controller.abort();await assert.rejects(pending,{name:'AbortError'});
  worker.respond(Array.from({length:3},()=>new Float32Array(200)));
  assert.equal(worker.terminated,1);
});

test('unsupported workers, malformed data and worker errors fail cleanly',async()=>{
  await assert.rejects(loadBrandGeometries(20,{workerFactory:()=>{throw new Error('Worker unavailable');}}));
  for(const response of [null,[],[new Float32Array(200)],Array.from({length:3},()=>new Float32Array(10))]){
    const worker=new GeometryWorker(),pending=loadBrandGeometries(20,{workerFactory:()=>worker});
    worker.respond(response);await assert.rejects(pending);assert.equal(worker.terminated,1);
  }
  const worker=new GeometryWorker(),pending=loadBrandGeometries(20,{workerFactory:()=>worker});
  worker.dispatchEvent(new Event('error'));await assert.rejects(pending);assert.equal(worker.terminated,1);
});

test('already aborted preparation never creates a worker',async()=>{
  const controller=new AbortController();controller.abort();let created=false;
  await assert.rejects(loadBrandGeometries(20,{signal:controller.signal,workerFactory:()=>{created=true;return new GeometryWorker();}}),{name:'AbortError'});
  assert.equal(created,false);
});
