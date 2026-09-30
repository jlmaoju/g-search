import {BRAND_MARKS,brandGeometry} from '../vendor/brand-geometry.js';

self.onmessage=event=>{
  const count=event.data.count;
  if(!Number.isInteger(count)||count<1||count>36000)throw new TypeError('Invalid particle budget');
  const geometries=BRAND_MARKS.map(mark=>brandGeometry(mark.id,count));
  self.postMessage(geometries,geometries.map(data=>data.buffer));
};
