import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

const root = document.getElementById('view');
let renderer, scene, camera, controls, object, raf, observer, token = '';
function reply(type, payload = {}, transfer = []) { parent.postMessage({type, token, ...payload}, '*', transfer); }
function clear(){
  if (raf) cancelAnimationFrame(raf); raf = 0;
  if (observer) observer.disconnect(); observer = null;
  if (controls) controls.dispose(); controls = null;
  if (scene) scene.traverse(o => { if (o.geometry) o.geometry.dispose(); const mats=Array.isArray(o.material)?o.material:[o.material]; mats.forEach(m=>m?.dispose()); });
  if (renderer) { renderer.dispose(); renderer.forceContextLoss(); } renderer = scene = camera = object = null;
  root.replaceChildren();
}
function draw(code){
  clear();
  // The generator supplies a function body returning a THREE.Object3D. This
  // runs ONLY inside an opaque-origin sandbox with network access disabled.
  const result = new Function('THREE', '"use strict";\n' + code)(THREE);
  if (!result || !result.isObject3D) throw new Error('The generated code did not return a 3D object.');
  object = result;
  let nodes=0; object.traverse(()=>{ if (++nodes > 10000) throw new Error('The model is too complex to preview.'); });
  const bounds = new THREE.Box3().setFromObject(object), sphere = bounds.getBoundingSphere(new THREE.Sphere());
  if (!Number.isFinite(sphere.radius) || sphere.radius <= 0 || sphere.radius > 10000) throw new Error('The generated model has invalid dimensions.');
  renderer = new THREE.WebGLRenderer({antialias:true,alpha:false});
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace; root.append(renderer.domElement);
  scene = new THREE.Scene(); scene.background = new THREE.Color(0x151820);
  scene.add(new THREE.HemisphereLight(0xe4efff, 0x5d5573, 2.2));
  const light = new THREE.DirectionalLight(0xffffff, 2); light.position.set(4, 8, 6); scene.add(light);
  scene.add(object); camera = new THREE.PerspectiveCamera(40, 1, 0.01, Math.max(100, sphere.radius * 30));
  const radius=Math.max(sphere.radius,.4), direction=new THREE.Vector3(1.5,.85,1.9).normalize();
  camera.position.copy(sphere.center).addScaledVector(direction,radius*3.8);
  controls=new OrbitControls(camera,renderer.domElement); controls.target.copy(sphere.center); controls.enableDamping=true; controls.update();
  function resize(){ const w=Math.max(1,root.clientWidth),h=Math.max(1,root.clientHeight);renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix(); }
  observer=new ResizeObserver(resize);observer.observe(root);resize();
  const animate=()=>{controls.update();renderer.render(scene,camera);raf=requestAnimationFrame(animate);};animate();
}
window.addEventListener('message', async event=>{
  if(event.source !== parent || !event.data || typeof event.data !== 'object') return;
  const data=event.data;
  if (data.type === 'render' && typeof data.code === 'string' && data.code.length <= 100000 && typeof data.token === 'string'){
    token = data.token;
    try { draw(data.code); reply('rendered'); }
    catch(e){ clear(); reply('error'); }
  } else if(data.type === 'export' && data.token === token && object){
    try {
      const bytes = await new GLTFExporter().parseAsync(object,{binary:true});
      if (!(bytes instanceof ArrayBuffer) || bytes.byteLength > 20*1024*1024) throw new Error('invalid export');
      reply('exported',{bytes},[bytes]);
    } catch(e){reply('export-error');}
  }
});
parent.postMessage({type:'viewer-ready'},'*');
