const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function fixture() {
  const nodes=['A','B','C','D'].map(id=>({id,label:id,attrs:{members:[],source:'declared'}}));
  const edge=(s,t,derived=false)=>({s:nodes.find(n=>n.id===s),t:nodes.find(n=>n.id===t),derived});
  const edges=[edge('A','B'),edge('B','C'),edge('C','A'),edge('B','D',true)];
  const S={human:{on:false},features:nodes};
  const context=vm.createContext({S,W:1200,H:800,$:()=>null,buildFeatureView:()=>{S.nodes=nodes.map(n=>({...n}));S.edges=edges;}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../cms/ui_assets/connections.js'),'utf8'),context);
  return {context,S};
}
test('one-hop focus contains only incident evidence, not neighbor-to-neighbor claims',()=>{
  const {context,S}=fixture();vm.runInContext("CX.center='A';buildFeatureView()",context);
  assert.deepEqual(S.nodes.map(n=>n.id),['A','B','C']);
  assert.deepEqual(S.edges.map(e=>e.s.id+e.t.id),['AB','CA']);
});
test('incoming and outgoing directions remain distinct',()=>{
  const {context,S}=fixture();vm.runInContext("CX.center='A';CX.direction='incoming';buildFeatureView()",context);
  assert.deepEqual(S.nodes.map(n=>n.id),['A','C']);
  vm.runInContext("CX.direction='outgoing';buildFeatureView()",context);
  assert.deepEqual(S.nodes.map(n=>n.id),['A','B']);
});
test('directed path follows actual edges through cycles',()=>{
  const {context,S}=fixture();vm.runInContext("CX.center='A';CX.target='D';buildFeatureView()",context);
  assert.deepEqual(S.edges.map(e=>e.s.id+e.t.id),['AB','BD']);
});
test('filtering inferred links reports an unavailable path honestly',()=>{
  const {context,S}=fixture();vm.runInContext("CX.center='A';CX.target='D';CX.type='declared';buildFeatureView()",context);
  assert.equal(S.edges.length,0);assert.equal(vm.runInContext('CX.noPath',context),true);
});
test('overview preserves all nodes and edges with deterministic distinct positions',()=>{
  const {context,S}=fixture();vm.runInContext('buildFeatureView()',context);
  assert.equal(S.nodes.length,4);assert.equal(S.edges.length,4);
  const before=S.nodes.map(n=>[n.id,n.x,n.y]);vm.runInContext('buildFeatureView()',context);
  assert.deepEqual(S.nodes.map(n=>[n.id,n.x,n.y]),before);
  assert.equal(new Set(S.nodes.map(n=>`${n.x},${n.y}`)).size,4);
});
