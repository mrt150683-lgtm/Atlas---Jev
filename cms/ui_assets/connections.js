/* Evidence-preserving, deterministic navigation of the canonical feature graph. */
const CX = {center:'', type:'all', direction:'both', depth:1, target:'', selectedEdge:null, total:0};
const baseFeatureView = buildFeatureView;
const cxEligible = e => CX.type === 'all' || (CX.type === 'declared' ? !e.derived : e.derived);
function cxNeighborhood(edges, start, direction, depth) {
  const seen = new Set([start]), levels = new Map([[start,0]]);
  let frontier = [start];
  for(let step=1;step<=depth && frontier.length;step++) {
    const next=[];
    for(const id of frontier) for(const e of edges) {
      const other = e.s.id===id && direction!=='incoming' ? e.t.id : e.t.id===id && direction!=='outgoing' ? e.s.id : null;
      if(other && !seen.has(other)){seen.add(other);levels.set(other,step);next.push(other);}
    }
    frontier=next;
  }
  return {seen,levels};
}
function cxPath(edges, start, end) {
  const parents=new Map([[start,null]]), queue=[start];
  for(let i=0;i<queue.length;i++) {
    if(queue[i]===end) break;
    for(const e of edges) if(e.s.id===queue[i] && !parents.has(e.t.id)){parents.set(e.t.id,queue[i]);queue.push(e.t.id);}
  }
  if(!parents.has(end)) return [];
  const result=[]; for(let id=end;id!==null;id=parents.get(id))result.unshift(id);
  return result;
}
buildFeatureView = function() {
  baseFeatureView();
  if(S.human.on) return;
  const allNodes=S.nodes.filter(n=>!n.hull), edges=S.edges.filter(cxEligible);
  CX.total=S.edges.length; CX.selectedEdge=null;
  let visible=allNodes, path=[];
  if(CX.center) {
    const neighborhood=cxNeighborhood(edges,CX.center,CX.direction,Number(CX.depth));
    if(CX.target) path=cxPath(edges,CX.center,CX.target);
    const included=CX.target ? new Set(path.length ? path : [CX.center,CX.target]) : neighborhood.seen;
    visible=allNodes.filter(n=>included.has(n.id));
    const columns=new Map();
    for(const n of visible) {
      let col=path.length ? path.indexOf(n.id) : n.id===CX.center ? 0 : neighborhood.levels.get(n.id)||1;
      if(!path.length && col && CX.direction==='incoming') col=-col;
      if(!path.length && col && CX.direction==='both' && edges.some(e=>e.s.id===n.id && e.t.id===CX.center)) col=-col;
      if(!columns.has(col))columns.set(col,[]); columns.get(col).push(n);
    }
    for(const [col,nodes] of columns) nodes.sort((a,b)=>a.label.localeCompare(b.label)).forEach((n,i)=>{n.x=col*280;n.y=(i-(nodes.length-1)/2)*86;});
    S.edges=edges.filter(e=>included.has(e.s.id)&&included.has(e.t.id)&&
      (!CX.target || path.length) &&
      (path.length ? path.indexOf(e.t.id)===path.indexOf(e.s.id)+1 : Number(CX.depth)>1||e.s.id===CX.center||e.t.id===CX.center));
  } else {
    // A stable reading order keeps every label separate; focus exposes the
    // complete neighborhood without an arbitrary connection sample.
    visible.sort((a,b)=>(a.attrs.source===b.attrs.source?a.label.localeCompare(b.label):a.attrs.source==='declared'?-1:1));
    const aspect=Math.max(.35,(W-350)/Math.max(200,H-180));
    const cols=Math.max(1,Math.ceil(Math.sqrt(visible.length*aspect*88/196)));
    visible.forEach((n,i)=>{n.x=(i%cols)*196;n.y=Math.floor(i/cols)*88;});
    S.edges=edges;
  }
  S.nodes=visible;
  for(const n of S.nodes){const count=(n.attrs.members||[]).length;n.shape='feature';n.meta=`${count} member${count===1?'':'s'}`;n.r=10;n.color=n.attrs.source==='declared'?'#73b9ff':'#62d3b3';}
  for(const e of S.edges)e.overview=true;
  CX.noPath=Boolean(CX.center&&CX.target&&!path.length);
  cxRender();
};
function cxFit() {
  if(!S.nodes.length)return;
  const xs=S.nodes.map(n=>n.x),ys=S.nodes.map(n=>n.y);
  const explorer=$('connectionExplorer');
  const panel=explorer?.querySelector('.cx-panel');
  const left=explorer?.classList.contains('collapsed') ? 35 : (panel?.getBoundingClientRect().width || 298)+36;
  const usableW=Math.max(100,W-left-35),usableH=Math.max(100,H-180);
  const x0=Math.min(...xs)-84,x1=Math.max(...xs)+84,y0=Math.min(...ys)-40,y1=Math.max(...ys)+40;
  S.view.k=Math.max(.08,Math.min(usableW/(x1-x0),usableH/(y1-y0),1.35));
  S.view.x=left+usableW/2-(x0+x1)/2*S.view.k;
  S.view.y=140+usableH/2-(y0+y1)/2*S.view.k;
}
function cxRefresh(){if(!S.featMode)setFeatMode(true);else buildFeatureView();cxFit();draw();requestAnimationFrame(()=>{resize();cxFit();draw();});}
function cxFocus(id){CX.center=id;CX.target='';$('cxCenter').value=id;$('cxTarget').value='';cxRefresh();selectFeature(id);requestAnimationFrame(()=>{resize();cxFit();});}
function cxRender() {
  if(!$('cxStats'))return;
  $('cxStats').textContent=`${S.nodes.length} of ${S.features.length} features · ${S.edges.length} of ${CX.total} connections`;
  $('cxCount').textContent=CX.center?'Connections in this view':'Choose a feature to untangle its connections';
  $('cxMessage').textContent=CX.noPath?'No directed path exists with these connection filters. Try a different target or include both evidence types.':CX.center?'Arrows run from source to destination. Declared links express intent; inferred links reflect code structure. Neither proves behavior.':'Overview keeps every feature visible. Search or choose a feature to see its immediate neighborhood, then follow the evidence.';
  const host=$('cxList');host.replaceChildren();
  const edges=CX.center?S.edges:[];
  const fragment=document.createDocumentFragment();
  for(const e of edges.slice(0,80)) {
    const button=document.createElement('button');
    button.append(`${e.s.label} → ${e.t.label}`);
    const small=document.createElement('small');small.textContent=e.derived?'Inferred from code':'Declared connection';button.append(small);
    button.onclick=()=>cxInspect(e);fragment.append(button);
  }
  host.append(fragment);
  if(CX.center&&!edges.length){const empty=document.createElement('p');empty.className='cx-empty';empty.textContent='No connections match this view. The selected feature remains available in the inspector.';host.append(empty);}
  if(edges.length>80){const note=document.createElement('p');note.className='cx-note';note.textContent=`Showing the first 80 of ${edges.length} links here; all are drawn. Narrow the direction or depth to inspect a smaller set.`;host.append(note);}
  $('cxDetail').hidden=true;
}
function cxInspect(e) {
  CX.selectedEdge=e;S.hoverEdge=e;
  const detail=$('cxDetail');detail.hidden=false;detail.replaceChildren();
  const title=document.createElement('strong');title.textContent=`${e.s.label} → ${e.t.label}`;detail.append(title);
  const evidence=document.createElement('p');evidence.textContent=typeof e.via==='string'?e.via:JSON.stringify(e.via);detail.append(evidence);
  for(const n of [e.s,e.t]){const button=document.createElement('button');button.textContent=`Explore ${n.label}`;button.onclick=()=>cxFocus(n.id);detail.append(button);}
}
function initConnectionExplorer() {
  const host=document.createElement('section');host.id='connectionExplorer';host.setAttribute('aria-label','Connection explorer');
  host.innerHTML=`<div class="cx-top"><div class="cx-heading"><div class="cx-eyebrow">Atlas / Architecture</div><h2>Explore the connections</h2><div class="cx-stats" id="cxStats"></div></div><button id="cxOverview">Overview</button><button id="cxFit">Fit view</button><button id="cxToggle" aria-expanded="true">Hide controls</button></div><div class="cx-panel"><label for="cxSearch">Find a feature</label><input id="cxSearch" type="search" placeholder="Search feature names…"><label for="cxCenter">Focus</label><select id="cxCenter"><option value="">All features</option></select><div class="cx-pair"><div><label for="cxDirection">Direction</label><select id="cxDirection"><option value="both">Both ways</option><option value="outgoing">Outgoing →</option><option value="incoming">← Incoming</option></select></div><div><label for="cxDepth">Distance</label><select id="cxDepth"><option value="1">1 connection</option><option value="2">2 connections</option><option value="3">3 connections</option></select></div></div><label for="cxType">Connection evidence</label><select id="cxType"><option value="all">Declared + inferred</option><option value="declared">Declared only</option><option value="inferred">Inferred only</option></select><label for="cxTarget">Trace a directed path to</label><select id="cxTarget"><option value="">No destination</option></select><p class="cx-note" id="cxMessage"></p><div class="cx-count" id="cxCount"></div><div class="cx-detail" id="cxDetail" hidden></div><div class="cx-list" id="cxList"></div></div>`;
  $('canvasWrap').append(host);
  const legend=document.createElement('div');legend.className='cx-note';
  legend.innerHTML='<span style="color:#73b9ff">● Declared feature</span> · <span style="color:#62d3b3">● Discovered feature</span><br>Solid link: declared · Dashed link: inferred';
  $('cxStats').after(legend);
  const ordered=[...S.features].sort((a,b)=>a.name.localeCompare(b.name));
  function fill(query='') {
    $('cxCenter').replaceChildren(new Option('All features',''));
    for(const f of ordered.filter(f=>f.name.toLowerCase().includes(query.toLowerCase())||f.id===CX.center))$('cxCenter').add(new Option(f.name,f.id));
    $('cxCenter').value=CX.center;
  }
  fill();for(const f of ordered)$('cxTarget').add(new Option(f.name,f.id));
  $('cxSearch').oninput=e=>fill(e.target.value);
  $('cxSearch').onkeydown=e=>{if(e.key==='Enter'){const options=[...$('cxCenter').options].filter(o=>o.value);if(options.length)cxFocus(options[0].value);}};
  for(const [id,key] of [['cxCenter','center'],['cxDirection','direction'],['cxDepth','depth'],['cxType','type'],['cxTarget','target']])$(id).onchange=e=>{CX[key]=e.target.value;S.selected=CX.center||null;cxRefresh();};
  $('cxCenter').onchange=e=>{if(e.target.value)cxFocus(e.target.value);else{CX.center='';CX.target='';S.selected=null;renderInspectorPlaceholder();cxRefresh();}};
  $('cxOverview').onclick=()=>{CX.center='';CX.target='';$('cxCenter').value='';$('cxTarget').value='';S.selected=null;S.featureFiles=null;renderInspectorPlaceholder();cxRefresh();requestAnimationFrame(()=>{resize();cxFit();});};
  $('cxFit').onclick=cxFit;
  $('cxToggle').onclick=()=>{const hidden=host.classList.toggle('collapsed');$('cxToggle').textContent=hidden?'Show controls':'Hide controls';$('cxToggle').setAttribute('aria-expanded',String(!hidden));cxFit();};
  const originalSelect=selectFeature;
  selectFeature=function(id){originalSelect(id);if(S.featMode&&!S.human.on&&CX.center!==id)cxFocus(id);};
  const originalDraw=draw;
  let framedNodes=null,framedSize='';
  draw=function(){
    host.hidden=!S.featMode||S.human.on;
    const wrap=$('canvasWrap'),size=`${wrap.clientWidth}:${wrap.clientHeight}`;
    if(!host.hidden&&(framedNodes!==S.nodes||framedSize!==size)){
      resize();cxFit();framedNodes=S.nodes;framedSize=size;
    }
    originalDraw();
  };
  canvas.addEventListener('click',e=>{if(!S.featMode||S.human.on||nodeAt(e.offsetX,e.offsetY))return;const edge=edgeAt(e.offsetX,e.offsetY);if(edge)cxInspect(edge);});
  canvas.setAttribute('aria-label','Feature connection map. Use the connection explorer controls for keyboard navigation.');
  const requested=new URLSearchParams(location.search).get('feature');
  const selected=S.selected||ordered.find(f=>f.name===requested)?.id;
  if(!S.human.on && !new URLSearchParams(location.search).has('file')){setFeatMode(true);if(selected&&ordered.some(f=>f.id===selected))cxFocus(selected);else cxRefresh();}
}
