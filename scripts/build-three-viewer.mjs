// Rebuild the checked-in offline iframe from its source (requires three@0.161.0 and esbuild@0.25.5).
import { build } from 'esbuild';
import { writeFile } from 'node:fs/promises';

const result = await build({entryPoints:['static/three-viewer-source.js'],bundle:true,minify:true,format:'iife',target:'es2020',write:false});
const script = result.outputFiles[0].text.replace(/[ \t]+$/gm, '');
if (/<\/script/i.test(script)) throw new Error('Unexpected closing script tag in viewer bundle');
const html = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'"><meta name="referrer" content="no-referrer"><style>html,body,#view{margin:0;width:100%;height:100%;overflow:hidden;background:#151820}canvas{display:block;width:100%;height:100%;touch-action:none}</style></head><body><div id="view"></div><script>${script}</script></body></html>`;
await writeFile('static/three-viewer.html', html);
