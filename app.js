'use strict';
const $ = id => document.getElementById(id);
const labels = ['Background', 'Barrier', 'Bollard', 'Person', 'Industrial vehicle'];
const colors = [[155,160,151],[152,96,227],[239,190,40],[52,180,190],[231,134,56]];
let session, current, image, pixels, selectedClass = 1, tool = 'brush';
let visible = [false,true,true,true,true], undoStack = [], redoStack = [], drawing = false, previousPoint, beforeStroke;
let busy = false, messageTimer, noteTimer, reviewerTimer;
let samBusy=false, samInfo={enabled:false,jobs:[]}, samJob=null;
let samSelectedJob=null, samOptionsReady=false, boxDraft=null;
const canvas = $('canvas'), context = canvas.getContext('2d'), scratch = document.createElement('canvas');
const scratchContext = scratch.getContext('2d', {willReadFrequently:true});
const conflictCanvas=document.createElement('canvas');
let hasConflicts=false;
let zoom=1, fitScale=1, panX=0, panY=0, spacePan=false, panning=null;
let teamEnabled=false, teamAuthenticated=false, teamStale=false, teamOffline=false, teamPolling=false;
let noteSavePromise=null, teamRevisionKey='', teamPollGeneration=0, lastTeamActivity=Date.now(), teamQueueSignature='';
function isTeam(){return teamEnabled;}
function isOwner(){return !isTeam()||session?.team?.user?.role==='owner';}
function claimFor(index=session?.active_sample){return session?.team?.claims?.find(c=>c.batch===session.active_batch&&c.index===index&&claimAlive(c));}
function claimAlive(claim){const expiry=typeof claim.expires_at==='number'?claim.expires_at*1000:Date.parse(claim.expires_at);return !Number.isFinite(expiry)||expiry>Date.now();}
function canEdit(){return !isTeam()||!!(teamAuthenticated&&!teamStale&&!teamOffline&&claimFor()?.session_owned);}
function requireEdit(){if(canEdit())return true;message(teamStale?'Reload this frame before making changes.':teamOffline?'Reconnect before making changes.':'Claim this frame before making changes.',true);return false;}
function markTeamStale(text='This frame changed. Reload to review the latest revision.'){teamStale=true;$('team-conflict').hidden=false;$('team-conflict-text').textContent=text;updateControls();}

function message(text, error=false) { $('message').textContent=text; $('message').className=error?'error':''; $('message').hidden=false; clearTimeout(messageTimer); messageTimer=setTimeout(()=>$('message').hidden=true,error?8500:3500); }
async function request(path, body, raw=false) {
  if(isTeam()&&['/api/mask','/api/note','/api/decision','/api/undo_review','/api/team/restore'].includes(path)&&!requireEdit())throw new Error('A current frame claim is required.');
  const response = await fetch(path,{method:'POST',headers:{'X-Review-Token':session.token,'Content-Type':raw?'application/zip':'application/json'},body:raw?body:JSON.stringify(body)});
  if(!response.ok) { const detail=await response.json().catch(()=>({}));if(isTeam()&&response.status===401)lockTeam('Your access has expired or was revoked. Sign in again.');if(isTeam()&&response.status===409)markTeamStale(detail.error||'The frame or claim changed. Reload before continuing.');const error=new Error(detail.error || 'Request failed');error.status=response.status;throw error; }
  return path==='/api/export'?response:response.json();
}
function setBusy(value, text) { busy=value; if(text) $('save-status').textContent=text; updateControls(); }
function asset(index,kind) { return `/asset/${session.active_batch}/${index}/${kind}`; }
function loadImage(url) { return new Promise((resolve,reject)=>{ const im=new Image(); im.onload=()=>resolve(im); im.onerror=()=>reject(new Error('Image could not be loaded')); im.src=url; }); }
function updateControls() {
  for(const id of ['brush','erase','reset','previous','next','approve','reject','skip','export','batch','zoom-in','zoom-out','zoom-fit']) $(id).disabled=busy || (!current && !['batch'].includes(id));
  $('undo').disabled=busy||drawing||!undoStack.length; $('redo').disabled=busy||drawing||!redoStack.length;
  $('undo-review').disabled=busy||drawing||!session?.undo_review;
  document.querySelectorAll('.history-item,.queue-item').forEach(row=>row.disabled=busy||drawing);
  $('import').disabled=busy; $('demo').disabled=busy; $('empty-import').disabled=busy;
  $('previous').disabled=busy||!current||session.active_sample===0;
  $('next').disabled=busy||!current||session.active_sample===session.batch.samples.length-1;
  $('export').disabled=busy||!session?.batch?.samples.some(s=>s.state==='approved');
  if($('sam-generate'))renderSam();
  if(isTeam())updateTeamControls();
}
function queue() {
  if(isTeam()&&current&&!teamStale&&session?.batch?.samples[session.active_sample])session.batch.samples[session.active_sample]=current;
  const samples=session.batch?.samples||[];
  $('queue').replaceChildren();
  samples.forEach((s,i)=>{
    const row=document.createElement('button'); row.className='queue-item'+(i===session.active_sample?' active':''); row.title=s.filename;
    const thumb=document.createElement('img'); thumb.src=asset(i,'thumb'); thumb.alt='';
    const info=document.createElement('div'); info.className='queue-info';
    const name=document.createElement('strong'); name.textContent=s.filename;
    const size=document.createElement('span'); size.textContent=`${s.width} × ${s.height}`;
    const state=document.createElement('span'); state.className='queue-state';
    const dot=document.createElement('span'); dot.className='dot '+s.state;
    state.append(dot,document.createTextNode(s.state[0].toUpperCase()+s.state.slice(1))); info.append(name,size,state); row.append(thumb,info);
    if(isTeam()){const claim=claimFor(i);if(claim){const chip=document.createElement('span');chip.className='claim-chip'+(claim.session_owned?' mine':'');chip.textContent=claim.session_owned?'Claimed by you':claim.name;chip.title=claim.session_owned?'This browser owns the frame claim':`Claimed by ${claim.name}`;info.append(chip);}}
    row.onclick=()=>navigate(i); $('queue').append(row);
  });
  const count=samples.filter(s=>['approved','rejected','skipped'].includes(s.state)).length;
  const approved=samples.filter(s=>s.state==='approved').length;
  $('position').textContent=samples.length?`${session.active_sample+1} of ${samples.length}`:'0 of 0';
  $('progress-fill').style.width=samples.length?`${100*count/samples.length}%`:'0%';
  $('summary').textContent=samples.length?`${count} reviewed · ${approved} approved`:'Import a batch to start';
  $('approved-count').textContent=approved;
  if(current) { $('frame-state').textContent=current.state; $('frame-state').className='state '+current.state; }
  renderHistory();
  updateControls();
  if(isTeam())teamQueueSignature=sharedQueueSignature();
}
function sharedQueueSignature(){return JSON.stringify([session.active_batch,session.active_sample,session.batch?.samples.map(s=>[s.filename,s.state,s.revision]),session.team?.claims.filter(claimAlive).map(c=>[c.batch,c.index,c.user_id,c.session_owned]),session.review_history]);}
const actionNames={approved:'Approved',rejected:'Rejected',skipped:'Skipped',undo_review:'Review undone',brush:'Painted',erase:'Erased',undo:'Edit undone',redo:'Edit redone',reset:'Suggestion restored'};
function renderHistory() {
  const target=session?.undo_review;
  $('undo-review').title=target?`Undo ${(actionNames[target.action]||target.action).toLowerCase()} for ${target.filename}; pixels stay intact`:'No current review to undo';
  $('last-review').textContent=target?`${actionNames[target.action]||target.action} · ${target.filename}`:'No current review to undo';
  const list=$('review-history');list.replaceChildren();
  for(const entry of session?.review_history||[]) {
    const row=document.createElement('button');row.className='history-item';row.dataset.index=entry.index;row.dataset.action=entry.action;
    row.setAttribute('aria-label',`Open ${entry.filename}, ${actionNames[entry.action]}, ${entry.reviewer||'unnamed reviewer'}, ${entry.at}`);
    const title=document.createElement('strong');title.textContent=entry.action==='undo_review'?`Undid ${actionNames[entry.undone_action]?.toLowerCase()||'review'}`:actionNames[entry.action];
    const frame=document.createElement('span');frame.className='history-frame';frame.textContent=entry.filename;
    const detail=document.createElement('span');detail.className='history-detail';detail.textContent=`${new Date(entry.at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} · ${entry.reviewer||'Unnamed'}`;detail.title=entry.at;
    row.append(title,frame,detail);row.onclick=()=>navigate(entry.index);list.append(row);
  }
  if(!list.children.length){const empty=document.createElement('p');empty.className='history-empty';empty.textContent='Decisions appear here. Select one to reopen its frame.';list.append(empty);}
  const edits=$('edit-history');edits.replaceChildren();
  for(const entry of [...(current?.history||[])].reverse()) {
    const row=document.createElement('p');row.textContent=`${actionNames[entry.action]||entry.action} · ${new Date(entry.at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} · ${entry.reviewer||'Unnamed'}`;row.title=entry.at;edits.append(row);
  }
  if(!edits.children.length)edits.textContent='No changes to this frame yet.';
}
async function syncHistory(){Object.assign(session,await getJSON(`/api/history/${session.active_batch}`));renderHistory();updateControls();}
function renderLabels() {
  $('classes').replaceChildren();
  labels.forEach((label,i)=>{
    const row=document.createElement('div'); row.className='class-row'+(selectedClass===i?' selected':'');
    const select=document.createElement('label');select.className='class-select';
    const radio=document.createElement('input'); radio.type='radio'; radio.name='class'; radio.value=i; radio.checked=selectedClass===i; radio.onclick=radio.onchange=()=>selectClass(i);
    const number=document.createElement('span'); number.className='class-number';number.textContent=i;
    const dot=document.createElement('span');dot.className='swatch';dot.style.background=`rgb(${colors[i]})`;
    const name=document.createElement('span');name.className='class-name';name.textContent=label;
    const eye=document.createElement('button');eye.type='button';eye.className='eye'+(visible[i]?'':' off');eye.textContent='◉';eye.title=`${visible[i]?'Hide':'Show'} ${label}`;eye.setAttribute('aria-label',eye.title);eye.setAttribute('aria-pressed',String(visible[i]));
    eye.onclick=()=>{visible[i]=!visible[i];updateLabelState();render();};select.append(radio,number,dot,name);row.append(select,eye);$('classes').append(row);
  });
}
function updateLabelState(){document.querySelectorAll('.class-row').forEach((row,i)=>{row.classList.toggle('selected',selectedClass===i);row.querySelector('input').checked=selectedClass===i;const eye=row.querySelector('.eye');eye.classList.toggle('off',!visible[i]);eye.title=`${visible[i]?'Hide':'Show'} ${labels[i]}`;eye.setAttribute('aria-label',eye.title);eye.setAttribute('aria-pressed',String(visible[i]));});}
function selectClass(i){
  if(busy||drawing)return;
  selectedClass=i;setTool('brush');
}
function render() {
  if(!pixels||!image)return;
  context.clearRect(0,0,canvas.width,canvas.height);context.drawImage(image,0,0);
  if($('overlay').checked){
    const rgba=scratchContext.createImageData(canvas.width,canvas.height);const alpha=Math.round(Number($('opacity').value)*2.55);
    for(let i=0;i<pixels.length;i++){ const label=pixels[i]; if(!visible[label])continue; const c=colors[label], o=i*4;rgba.data[o]=c[0];rgba.data[o+1]=c[1];rgba.data[o+2]=c[2];rgba.data[o+3]=alpha; }
    scratchContext.putImageData(rgba,0,0);context.drawImage(scratch,0,0);
  }
  if(hasConflicts&&$('sam-conflicts').checked)context.drawImage(conflictCanvas,0,0);
  if(boxDraft){const b=boxCoordinates();context.save();context.lineWidth=2/Math.max(.05,fitScale*zoom);context.strokeStyle='#245c43';context.fillStyle='#39855a22';context.setLineDash([4*context.lineWidth,2*context.lineWidth]);context.fillRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);context.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);context.restore();}
}
function fitCanvas(){if(!current)return;const area=$('canvas-area');fitScale=Math.min((area.clientWidth-2)/canvas.width,(area.clientHeight-2)/canvas.height);updateView();}
function updateView(){if(!current||fitScale<=0)return;const area=$('canvas-area'),width=canvas.width*fitScale*zoom,height=canvas.height*fitScale*zoom;panX=Math.max(-width/2-area.clientWidth/2+40,Math.min(width/2+area.clientWidth/2-40,panX));panY=Math.max(-height/2-area.clientHeight/2+40,Math.min(height/2+area.clientHeight/2-40,panY));canvas.style.width=width+'px';canvas.style.height=height+'px';canvas.style.transform=`translate(calc(-50% + ${panX}px), calc(-50% + ${panY}px))`;$('zoom-value').textContent=Math.round(fitScale*zoom*100)+'%';$('zoom-value').title=`${zoom.toFixed(2)} × Fit`;$('cursor').hidden=true;}
function zoomTo(value,x,y){if(!current||busy||drawing||panning)return;const area=$('canvas-area').getBoundingClientRect(),next=Math.max(.5,Math.min(8,value)),ratio=next/zoom;const anchorX=(x??area.left+area.width/2)-area.left-area.width/2,anchorY=(y??area.top+area.height/2)-area.top-area.height/2;panX=anchorX-(anchorX-panX)*ratio;panY=anchorY-(anchorY-panY)*ratio;zoom=next;updateView();}
function resetView(){zoom=1;panX=panY=0;fitCanvas();}
function updateHelp(){$('canvas-help').textContent=spacePan||panning?'Pan mode: drag to move the image.':tool==='box'?`Tight box for ${labels[selectedClass]}; exclude shadows and nearby objects. Confirm to run. Escape cancels.`:`${tool==='erase'?'Erase writes background ID 0.':'Brush uses native pixels.'} Wheel to zoom · Space-drag to pan.`;canvas.classList.toggle('pan-ready',spacePan);canvas.classList.toggle('panning',!!panning);}
new ResizeObserver(fitCanvas).observe($('canvas-area'));
function bytes64(bytes) { let text='';for(let i=0;i<bytes.length;i+=32768)text+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(text); }
async function savePixels(reason) {
  setBusy(true,'Saving correction…');
  try { if(isTeam())await saveNote();const result=await request('/api/mask',{batch:session.active_batch,index:session.active_sample,revision:current.revision,pixels:bytes64(pixels),reason});Object.assign(current,result);await syncHistory();queue();$('dimensions').textContent=`${current.width} × ${current.height} · revision ${current.revision} · native pixels`;$('save-status').textContent='Saved locally · approval required';if(isTeam())await loadTeamRevisions(); }
  catch(e){message(e.message,true); if(isTeam()){if(teamAuthenticated)markTeamStale('Correction was not saved. Your local pixels remain visible. Reload the saved frame to continue.');}else await refresh();}
  finally{setBusy(false);}
}
async function loadFrame(index) {
  if(isTeam()){teamPollGeneration++;teamStale=false;teamRevisionKey='';$('team-conflict').hidden=true;}
  boxDraft=null;if(tool==='box')setTool('brush');
  spacePan=false;panning=null;updateHelp();
  undoStack=[];redoStack=[];current=session.batch?.samples[index];
  renderLibrary();
  hasConflicts=false;$('conflict-control').hidden=true;
  $('empty').hidden=!!current;canvas.hidden=!current;
  $('batch-guidance').textContent=session.batch?.source_manifest?.description||'';$('batch-guidance').hidden=!$('batch-guidance').textContent;
  if(!current){queue();return;}
  setBusy(true,'Loading native pixels…');
  try{
    const isSam=!!current.provenance?.conflict_map;
    const [rgb,raw,conflictImage]=await Promise.all([loadImage(asset(index,'image')),fetch(asset(index,'pixels')).then(r=>{if(!r.ok)throw new Error('Label pixels could not be loaded');return r.arrayBuffer();}),isSam?loadImage(asset(index,'conflicts')):Promise.resolve(null)]);
    image=rgb;canvas.width=current.width;canvas.height=current.height;scratch.width=canvas.width;scratch.height=canvas.height;
    pixels=new Uint8Array(raw);
    if(conflictImage){
      conflictCanvas.width=canvas.width;conflictCanvas.height=canvas.height;
      const cx=conflictCanvas.getContext('2d',{willReadFrequently:true});cx.drawImage(conflictImage,0,0);
      const overlay=cx.getImageData(0,0,canvas.width,canvas.height);
      for(let i=0;i<overlay.data.length;i+=4){const marked=overlay.data[i]>0,x=(i/4)%canvas.width,y=Math.floor((i/4)/canvas.width);overlay.data[i]=195;overlay.data[i+1]=38;overlay.data[i+2]=146;overlay.data[i+3]=marked?((x+y)%12<4?220:55):0;}
      cx.putImageData(overlay,0,0);hasConflicts=true;$('conflict-control').hidden=false;
      $('conflict-count').textContent=`${Number(current.provenance.conflict_pixels).toLocaleString()} withheld pixels`;
    }
    $('filename').textContent=current.filename;$('dimensions').textContent=`${current.width} × ${current.height} · revision ${current.revision} · native pixels`;
    $('note').value=current.note;$('note-count').textContent=current.note.length;
    $('source').replaceChildren();
    for(const [key,label] of [['model','Model'],['dataset','Dataset'],['session','Session'],['run','Run'],['split','Split']]){const dt=document.createElement('dt');dt.textContent=label;const dd=document.createElement('dd');dd.textContent=current[key];$('source').append(dt,dd);}
    if(current.provenance?.conflict_map){const dt=document.createElement('dt');dt.textContent='Conflicts';const dd=document.createElement('dd');dd.textContent=`${Number(current.provenance.conflict_pixels).toLocaleString()} pixels withheld; check all missing foreground`;$('source').append(dt,dd);}
    if(current.provenance?.sam_assistance){const a=current.provenance.sam_assistance,dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent='Assistance';dd.textContent=`Human box · ${labels[a.class_id]} · [${a.box.join(', ')}] · base revision ${a.source_revision}`;$('source').append(dt,dd);}
    render();queue();resetView();$('save-status').textContent='Saved locally';if(isTeam())await loadTeamRevisions();
  }catch(e){current=null;pixels=null;image=null;canvas.hidden=true;hasConflicts=false;$('conflict-control').hidden=true;throw e;}finally{setBusy(false);}
}
async function refresh() {
  session=await getJSON('/api/state');
  try{sharedLibrary=await getJSON('/api/library');}catch{sharedLibrary={available:false};}
  renderLibrary();
  if(isTeam()){teamAuthenticated=true;teamOffline=false;session.reviewer=session.team.user.name;document.body.classList.remove('boot-gated');$('team-auth').hidden=true;}
  samInfo=session.sam3||{enabled:false,jobs:[]};
  $('empty').querySelector('.small').textContent=samInfo.enabled?'Offline review with optional local SAM assistance.':'Offline review. No live model inference.';
  document.querySelector('.source-label').parentElement.querySelector('p.small').textContent=samInfo.enabled?'Local SAM runs only when requested. No automatic training promotion.':'No live inference or automatic training promotion.';
  renderBatchOptions();
  $('reviewer').value=session.reviewer;await loadFrame(session.active_sample);if(isTeam())updateTeamControls();
}
let sharedLibrary={available:false};
function renderLibrary(){
  const available=!!sharedLibrary.available, section=$('shared-library');
  section.hidden=!available;
  for(const key of ['library','originals','labels','incoming']){
    const link=$('library-'+key);link.removeAttribute('href');
    if(available)link.href=sharedLibrary.folders[key];
  }
  $('library-status').textContent=available&&sharedLibrary.snapshot_at?`Snapshot ${sharedLibrary.snapshot_at.slice(0,10)} · manual transfer`:'Dated snapshots · manual transfer';
  const original=$('drive-original'),url=available&&current?sharedLibrary.originals[current.image_sha256]:null;
  original.hidden=!url;original.removeAttribute('href');if(url)original.href=url;
}
async function saveNote(){
  clearTimeout(noteTimer);if(noteSavePromise){await noteSavePromise;if(!current)return;}
  if(!current)return;const note=$('note').value,frame=current,batch=session.active_batch,index=session.active_sample;if(note===frame.note)return;
  if(isTeam()&&!requireEdit())throw new Error('Your pending note has not been saved. Reclaim or reload this frame before leaving.');
  noteSavePromise=(async()=>{const result=await request('/api/note',{batch,index,revision:frame.revision,note});if(isTeam())Object.assign(frame,result);frame.note=note;if(isTeam()){queue();$('dimensions').textContent=`${frame.width} × ${frame.height} · revision ${frame.revision} · native pixels`;$('save-status').textContent='Note saved · approval required';}})();
  try{await noteSavePromise;}catch(e){if(isTeam()&&teamAuthenticated)markTeamStale('Your note was not saved. It remains visible here. Reload the saved frame to continue.');throw e;}finally{noteSavePromise=null;}
}
async function saveReviewer(){clearTimeout(reviewerTimer);if(isTeam())return;const value=$('reviewer').value.trim();if(session.reviewer!==value){await request('/api/session',{reviewer:value});session.reviewer=value;}}
async function navigate(index){if(busy||drawing||!current||index<0||index>=session.batch.samples.length)return;if(isTeam()&&teamStale){message('Reload the changed frame before navigating.',true);return;}try{setBusy(true);await saveNote();await saveReviewer();if(isTeam())await releaseCurrentClaim();await request('/api/session',{active_sample:index});session.active_sample=index;await loadFrame(index);}catch(e){message(e.message,true);setBusy(false);}}
function setTool(value){if(value!=='box')boxDraft=null;tool=value;$('brush').classList.toggle('selected',tool==='brush');$('erase').classList.toggle('selected',tool==='erase');$('sam-box')?.classList.toggle('selected',tool==='box');updateHelp();updateLabelState();render();if($('sam-generate'))renderSam();}
function boxCoordinates(){const a=boxDraft.start,b=boxDraft.end;return [Math.floor(Math.min(a.x,b.x)),Math.floor(Math.min(a.y,b.y)),Math.min(canvas.width,Math.ceil(Math.max(a.x,b.x)+1)),Math.min(canvas.height,Math.ceil(Math.max(a.y,b.y)+1))];}
function cancelBox(){drawing=false;boxDraft=null;setTool('brush');updateControls();}
function point(event){const r=canvas.getBoundingClientRect();return{x:Math.max(0,Math.min(canvas.width-1,(event.clientX-r.left)*canvas.width/r.width)),y:Math.max(0,Math.min(canvas.height-1,(event.clientY-r.top)*canvas.height/r.height))};}
function paint(p){const radius=Number($('size').value)/2; const from=previousPoint||p;const steps=Math.max(1,Math.ceil(Math.hypot(p.x-from.x,p.y-from.y)/Math.max(1,radius/3)));const value=tool==='erase'?0:selectedClass;
  for(let t=0;t<=steps;t++){const x=from.x+(p.x-from.x)*t/steps,y=from.y+(p.y-from.y)*t/steps;for(let yy=Math.max(0,Math.floor(y-radius));yy<=Math.min(canvas.height-1,Math.ceil(y+radius));yy++)for(let xx=Math.max(0,Math.floor(x-radius));xx<=Math.min(canvas.width-1,Math.ceil(x+radius));xx++)if((xx+.5-x)**2+(yy+.5-y)**2<=radius**2)pixels[yy*canvas.width+xx]=value;}
  previousPoint=p;render();
}
function pushUndo(copy){undoStack.push(copy);const max=Math.max(1,Math.floor(40_000_000/pixels.length));while(undoStack.length>max)undoStack.shift();redoStack=[];}
canvas.onpointerdown=e=>{if(busy||!pixels||panning||spacePan||e.button!==0||!canEdit())return;drawing=true;canvas.setPointerCapture(e.pointerId);if(tool==='box'){const p=point(e);boxDraft={batch:session.active_batch,index:session.active_sample,source_revision:current.revision,class_id:selectedClass,start:p,end:p};render();updateControls();return;}beforeStroke=pixels.slice();previousPoint=null;paint(point(e));updateControls();};
canvas.onpointermove=e=>{if(!pixels||panning||spacePan||!canEdit()){$('cursor').hidden=true;return;}if(tool==='box'){$('cursor').hidden=true;if(drawing&&boxDraft){boxDraft.end=point(e);render();}return;}const r=canvas.getBoundingClientRect(),area=$('canvas-area').getBoundingClientRect();const size=Number($('size').value)*r.width/canvas.width;$('cursor').style.width=size+'px';$('cursor').style.height=size+'px';$('cursor').style.left=e.clientX-area.left+'px';$('cursor').style.top=e.clientY-area.top+'px';$('cursor').hidden=false;if(drawing)paint(point(e));};
canvas.onpointerleave=()=>{$('cursor').hidden=true;};
async function finishStroke(){if(!drawing)return;drawing=false;if(tool==='box'){const b=boxCoordinates();if(b[2]-b[0]<3||b[3]-b[1]<3){boxDraft=null;message('Draw a box at least 3 native pixels wide and high.',true);}render();updateControls();return;}previousPoint=null;const changed=pixels.some((value,index)=>value!==beforeStroke[index]);if(changed){pushUndo(beforeStroke);await savePixels(tool);}else updateControls();beforeStroke=null;}
canvas.onpointerup=finishStroke;canvas.onpointercancel=()=>tool==='box'?cancelBox():finishStroke();
const canvasArea=$('canvas-area');
canvasArea.addEventListener('pointerdown',e=>{if(busy||drawing||!current||!(e.button===1||(e.button===0&&spacePan)))return;e.preventDefault();e.stopPropagation();panning={id:e.pointerId,x:e.clientX,y:e.clientY,panX,panY};canvasArea.setPointerCapture(e.pointerId);$('cursor').hidden=true;updateHelp();},true);
canvasArea.addEventListener('pointermove',e=>{if(!panning||e.pointerId!==panning.id)return;e.preventDefault();e.stopPropagation();panX=panning.panX+e.clientX-panning.x;panY=panning.panY+e.clientY-panning.y;updateView();},true);
function finishPan(e){if(!panning||e.pointerId!==panning.id)return;e.stopPropagation();panning=null;updateHelp();}
canvasArea.addEventListener('pointerup',finishPan,true);canvasArea.addEventListener('pointercancel',finishPan,true);canvasArea.addEventListener('lostpointercapture',finishPan,true);
canvasArea.addEventListener('wheel',e=>{if(!current||busy||drawing||panning)return;e.preventDefault();zoomTo(zoom*Math.exp(-Math.max(-200,Math.min(200,e.deltaY))*.002),e.clientX,e.clientY);},{passive:false});
$('zoom-in').onclick=()=>zoomTo(zoom*1.25);$('zoom-out').onclick=()=>zoomTo(zoom/1.25);$('zoom-fit').onclick=()=>{if(!busy&&!drawing&&!panning)resetView();};
document.addEventListener('keydown',e=>{if(e.code!=='Space'||e.target.closest('input,textarea,select,button,[contenteditable=true]')||e.altKey||e.ctrlKey||e.metaKey||busy||drawing||!current)return;e.preventDefault();spacePan=true;$('cursor').hidden=true;updateHelp();});
document.addEventListener('keyup',e=>{if(e.code==='Space'){spacePan=false;updateHelp();}});
window.addEventListener('blur',()=>{spacePan=false;panning=null;updateHelp();});
document.addEventListener('visibilitychange',()=>{if(document.hidden){spacePan=false;panning=null;updateHelp();}});
async function history(direction){if(busy||drawing||!canEdit())return;const from=direction==='undo'?undoStack:redoStack,to=direction==='undo'?redoStack:undoStack;if(!from.length)return;to.push(pixels.slice());pixels=from.pop();render();await savePixels(direction);if(current)$('dimensions').textContent=`${current.width} × ${current.height} · revision ${current.revision} · native pixels`;}
async function decision(state){if(busy||drawing||!current||!requireEdit())return;const reviewer=isTeam()?session.team.user.name:$('reviewer').value.trim();if(!reviewer){message('Enter your name before recording a decision.',true);$('reviewer').focus();return;}
  try{setBusy(true,'Saving decision…');await saveReviewer();if(isTeam())await saveNote();clearTimeout(noteTimer);const result=await request('/api/decision',{batch:session.active_batch,index:session.active_sample,revision:current.revision,decision:state,reviewer,note:$('note').value});Object.assign(current,result);await syncHistory();queue();$('save-status').textContent='Decision saved locally';setBusy(false);if(isTeam())await nextTeamFrame();else if(session.active_sample<session.batch.samples.length-1)await navigate(session.active_sample+1);else message('Batch reviewed. Export the approved masks when ready.');}catch(e){message(e.message,true);setBusy(false);}}
async function undoReview(){if(busy||drawing||!session.undo_review||!requireEdit())return;const target={...session.undo_review};if(isTeam()&&target.index!==session.active_sample){message('Open and claim the last reviewed frame before undoing its decision.',true);return;}try{setBusy(true,'Undoing review…');await saveNote();await saveReviewer();session=await request('/api/undo_review',{batch:session.active_batch,event_id:target.event_id,revision:target.revision});await loadFrame(session.active_sample);$('save-status').textContent='Review undone · approval required';message(`Review undone for ${current.filename}. Check the mask, then record a new decision.`);}catch(e){message(e.message,true);if(!isTeam())await refresh();}finally{setBusy(false);}}
async function importFile(file){if(!file)return;try{setBusy(true,'Validating batch…');if(file.size>100*1024*1024)throw new Error('ZIP exceeds 100 MiB');await saveNote();await saveReviewer();await request('/api/import',file,true);await refresh();message('Batch imported. Suggestions are ready for human review.');}catch(e){message(e.message,true);}finally{setBusy(false);$('file').value='';}}
$('import').onclick=$('empty-import').onclick=()=>$('file').click();$('file').onchange=()=>importFile($('file').files[0]);
$('demo').onclick=async()=>{try{const response=await fetch('/demo.zip');if(!response.ok)throw new Error('Run fixture.py first to create the synthetic demo batch.');await importFile(new File([await response.blob()],'demo.zip'));}catch(e){message(e.message,true);}};
$('batch').onchange=async()=>{if(isTeam()&&teamStale){$('batch').value=session.active_batch;message('Reload the changed frame before switching batches.',true);return;}try{setBusy(true);await saveNote();await saveReviewer();if(isTeam())await releaseCurrentClaim();await request('/api/session',{active_batch:$('batch').value,active_sample:0});await refresh();}catch(e){message(e.message,true);if(session)$('batch').value=session.active_batch;setBusy(false);}};
$('brush').onclick=()=>setTool('brush');$('erase').onclick=()=>setTool('erase');$('undo').onclick=()=>history('undo');$('redo').onclick=()=>history('redo');
$('undo-review').onclick=undoReview;
$('reset').onclick=async()=>{if(busy||drawing||!requireEdit())return;try{const response=await fetch(asset(session.active_sample,'suggestion_pixels'));if(!response.ok)throw new Error('Suggestion pixels could not be loaded');const raw=new Uint8Array(await response.arrayBuffer());if(!requireEdit())return;pushUndo(pixels.slice());pixels=raw;render();await savePixels('reset');}catch(e){message(e.message,true);}};
$('size').oninput=()=>$('size-value').textContent=$('size').value+' px';$('opacity').oninput=()=>{$('opacity-value').textContent=$('opacity').value+'%';render();};$('overlay').onchange=render;
$('previous').onclick=()=>navigate(session.active_sample-1);$('next').onclick=()=>navigate(session.active_sample+1);$('approve').onclick=()=>decision('approved');$('reject').onclick=()=>decision('rejected');$('skip').onclick=()=>decision('skipped');
$('reviewer').oninput=()=>{clearTimeout(reviewerTimer);reviewerTimer=setTimeout(()=>saveReviewer().catch(e=>message(e.message,true)),500);};
$('note').oninput=()=>{$('note-count').textContent=$('note').value.length;clearTimeout(noteTimer);noteTimer=setTimeout(()=>saveNote().catch(e=>message(e.message,true)),500);};
$('export').onclick=async()=>{try{setBusy(true,'Preparing lossless export…');await saveNote();await saveReviewer();const response=await request('/api/export',{batch:session.active_batch});const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download=(response.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||'approved.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);message('Export saved locally and downloaded. Only approved labels are accepted.');$('save-status').textContent='Export saved locally';}catch(e){message(e.message,true);}finally{setBusy(false);}};
document.addEventListener('keydown',e=>{if(e.target.closest('input,textarea,select,[contenteditable=true]')||document.querySelector('dialog[open]')||e.altKey||e.ctrlKey||e.metaKey||e.shiftKey||e.repeat||busy||drawing||(isTeam()&&!teamAuthenticated))return;const actions={b:()=>{if(canEdit())setTool('brush');},e:()=>{if(canEdit())setTool('erase');},z:()=>history('undo'),y:()=>history('redo'),a:()=>decision('approved'),r:()=>decision('rejected'),s:()=>decision('skipped'),ArrowLeft:()=>navigate(session.active_sample-1),ArrowRight:()=>navigate(session.active_sample+1)};const action=actions[e.key]||actions[e.key.toLowerCase()];if(action&&current){e.preventDefault();action();}});
window.addEventListener('beforeunload',e=>{if(busy||drawing||noteSavePromise||(current&&$('note').value!==current.note)){e.preventDefault();e.returnValue='A correction or note is still saving.';}});
function setupSam(){
  const conflicts=document.createElement('div');conflicts.id='conflict-control';conflicts.hidden=true;
  conflicts.innerHTML='<label><input id="sam-conflicts" type="checkbox" checked> Show original SAM conflicts <span id="conflict-count"></span></label><span class="conflict-hint">Magenta hatching marks withheld overlaps. Check and repair these areas. ID 0 is unresolved, never safe floor. Display only; saved labels stay separate.</span>';
  document.querySelector('.view-controls').before(conflicts);$('sam-conflicts').onchange=render;
  const section=document.createElement('section');section.id='sam-panel';section.hidden=true;
  section.innerHTML='<h2>Suggestion review</h2><p class="section-hint">Import suggestion ZIPs for named human review. No model worker is bundled.</p><button id="sam-generate">Generate suggestions unavailable</button><p id="sam-status" role="status"></p><div class="sam-actions"><button id="sam-cancel" hidden>Request cancellation</button><button id="sam-retry" hidden>Retry as new job</button><button id="sam-review" hidden>Review results</button></div><p class="small">Inspect and repair every imported suggestion before named approval. ID 0 is unresolved, never verified free space.</p>';
  document.querySelector('.inspector').prepend(section);
  const options=document.createElement('details');options.id='sam-options';
  options.innerHTML='<summary>Prompt options</summary><p class="section-hint">Up to 4 alternatives per class, separated by |. Lower confidence may add false positives.</p><label class="field-label" for="sam-threshold">Confidence for all classes</label><select id="sam-threshold"><option value="0.25">0.25</option><option value="0.35">0.35</option><option value="0.5">0.50</option><option value="0.65">0.65</option></select><div id="sam-prompt-fields"></div><label class="sam-object-rule"><input id="sam-object-rule" type="checkbox"> Separate rail supports from freestanding posts</label><p class="section-hint">Uses predicted mask overlap, not verified physical attachment. Check results.</p><button id="sam-reset-options">Reset to defaults</button><p id="sam-settings-summary" class="small"></p>';
  $('sam-generate').before(options);
  const phraseHints={1:'Try railing or handrail. Guardrail was less consistent here.',2:'Try cylindrical post or protective post. Rail supports can be mistaken for bollards.',3:'Try person or worker.',4:'Try pallet jack, electric pallet jack or forklift. Use the actual vehicle type.'};
  for(let cid=1;cid<=4;cid++){const label=document.createElement('label');label.className='field-label';label.htmlFor=`sam-prompt-${cid}`;label.textContent=labels[cid];const input=document.createElement('input');input.id=label.htmlFor;input.type='text';input.maxLength=409;input.autocomplete='off';input.oninput=renderSam;const hint=document.createElement('p');hint.className='section-hint';hint.id=`sam-phrase-hint-${cid}`;hint.textContent=phraseHints[cid];input.setAttribute('aria-describedby',hint.id);const confidenceLabel=document.createElement('label');confidenceLabel.className='field-label';confidenceLabel.htmlFor=`sam-class-threshold-${cid}`;confidenceLabel.textContent=`${labels[cid]} confidence`;const confidence=document.createElement('select');confidence.id=confidenceLabel.htmlFor;confidence.innerHTML=$('sam-threshold').innerHTML;confidence.onchange=renderSam;$('sam-prompt-fields').append(label,input,hint,confidenceLabel,confidence);}
  $('sam-threshold').onchange=()=>{for(let cid=1;cid<=4;cid++)$(`sam-class-threshold-${cid}`).value=$('sam-threshold').value;renderSam();};$('sam-object-rule').onchange=renderSam;$('sam-reset-options').onclick=()=>{resetSamOptions();renderSam();};
  const historyLabel=document.createElement('label');historyLabel.className='field-label';historyLabel.htmlFor='sam-job-history';historyLabel.textContent='Saved jobs';
  const history=document.createElement('select');history.id='sam-job-history';history.onchange=()=>{samSelectedJob=history.value;renderSam();};
  const querySummary=document.createElement('p');querySummary.id='sam-query-summary';querySummary.className='small';
  $('sam-status').before(historyLabel,history,querySummary);
  const boxControls=document.createElement('div');boxControls.id='sam-box-controls';
  boxControls.innerHTML='<button id="sam-box">Draw box for selected class</button><div id="sam-box-confirmation" hidden><p id="sam-box-summary"></p><p class="small">Use a tight box excluding shadows and nearby objects. RGB and your box enter SAM. The current mask is used only afterwards: keep outside pixels, clear this class inside the box, then add clipped SAM pixels. Results open separately and require review.</p><button id="sam-box-run">Run box assistance</button><button id="sam-box-cancel">Cancel box</button></div>';
  $('sam-generate').after(boxControls);
$('sam-box').onclick=()=>{if(busy||drawing||!current||selectedClass===0||!canEdit()||!isOwner())return;boxDraft=null;setTool('box');message(`Draw a tight box around one ${labels[selectedClass].toLowerCase()}, excluding shadows and nearby objects. Then confirm.`);};
  $('sam-box-cancel').onclick=cancelBox;
  $('sam-box-run').onclick=async()=>{if(!boxDraft||busy||drawing||samBusy)return;const draft=boxDraft;if(draft.batch!==session.active_batch||draft.index!==session.active_sample||draft.source_revision!==current.revision){cancelBox();message('Frame changed. Draw a new box on the current revision.',true);return;}const assistance={mode:'human_box',index:draft.index,source_revision:draft.source_revision,class_id:draft.class_id,box:boxCoordinates()};const result=await samAction('generate',{batch:draft.batch,assistance});if(result)cancelBox();};
  $('sam-generate').onclick=()=>{try{samAction('generate',{batch:session.active_batch,profile:samProfile()});}catch(e){message(e.message,true);}};
  $('sam-cancel').onclick=()=>samAction('cancel',{job_id:samJob.job_id});
  $('sam-retry').onclick=()=>samAction('generate',{batch:samJob.batch,retry_of:samJob.job_id});
  $('sam-review').onclick=async()=>{
    if(busy||drawing||samBusy)return;
    try{samBusy=true;setBusy(true,'Validating SAM results…');await saveNote();await saveReviewer();const result=await request('/api/sam/import',{job_id:samJob.job_id});
      if(result.already_imported)await request('/api/session',{active_batch:result.imported_batch,active_sample:0});
      await refresh();message('SAM results opened as pending suggestions. Check every foreground object and conflict.');
    }catch(e){message(e.message,true);}finally{samBusy=false;setBusy(false);await pollSam();}
  };
}
function resetSamOptions(){if(!samInfo.defaults)return;for(let cid=1;cid<=4;cid++){const entries=samInfo.defaults.prompts.filter(p=>p.class_id===cid);$(`sam-prompt-${cid}`).value=entries.map(p=>p.prompt).join(' | ');$(`sam-class-threshold-${cid}`).value=String(entries[0]?.threshold??samInfo.defaults.threshold);}$('sam-threshold').value=String(samInfo.defaults.threshold);$('sam-object-rule').checked=samInfo.defaults.object_rules==='withhold_rail_overlapping_posts';samOptionsReady=true;}
function samProfile(){
  if(!samInfo.defaults)throw new Error('Local prompt defaults are unavailable.');
  const profile=structuredClone(samInfo.defaults);profile.prompts=[];profile.threshold=Number($('sam-threshold').value);
  if(![.25,.35,.5,.65].includes(profile.threshold))throw new Error('Choose a listed confidence threshold.');
  for(let cid=1;cid<=4;cid++){const alternatives=$(`sam-prompt-${cid}`).value.split('|').map(p=>p.trim()),confidence=Number($(`sam-class-threshold-${cid}`).value);if(![.25,.35,.5,.65].includes(confidence))throw new Error(`Choose a listed confidence for ${labels[cid]}.`);if(alternatives.length>4||alternatives.some(p=>!p||p.length>100))throw new Error(`${labels[cid]} needs 1 to 4 alternatives, each 1 to 100 characters.`);profile.prompts.push(...alternatives.map(prompt=>{const query={class_id:cid,prompt},original=samInfo.defaults.prompts.find(p=>p.class_id===cid&&p.prompt===prompt);if(confidence!==profile.threshold||original?.threshold!==undefined)query.threshold=confidence;return query;}));}
  if($('sam-object-rule').checked||samInfo.defaults.object_rules!==undefined)profile.object_rules=$('sam-object-rule').checked?'withhold_rail_overlapping_posts':'none';
  if(JSON.stringify(profile.prompts)!==JSON.stringify(samInfo.defaults.prompts)||profile.threshold!==samInfo.defaults.threshold||profile.object_rules!==samInfo.defaults.object_rules){profile.id='custom-local-options';profile.evidence={base_profile:samInfo.defaults.id||'local-default',scope:'User-selected annotation options; reliability is not established by the default profile evidence.'};}
  return profile;
}
function renderSam(){
  $('sam-panel').hidden=!samInfo.enabled||!isOwner();
  if(!samInfo.enabled||!isOwner())return;
  if(!samOptionsReady&&samInfo.defaults)resetSamOptions();
  samJob=samInfo.jobs.find(j=>j.job_id===samSelectedJob)||samInfo.jobs.find(j=>j.batch===session?.active_batch||j.imported_batch===session?.active_batch)||null;
  samSelectedJob=samJob?.job_id||null;
  const history=$('sam-job-history'),signature=JSON.stringify(samInfo.jobs.map(j=>[j.job_id,j.state]));
  if(history.dataset.signature!==signature){history.replaceChildren();for(const job of samInfo.jobs){const option=document.createElement('option');option.value=job.job_id;option.textContent=`${new Date(job.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} · ${job.assistance?'Box':'Text'} · ${job.state} · ${job.source_title||job.job_id.slice(0,8)}`;history.append(option);}history.dataset.signature=signature;}
  history.value=samSelectedJob||'';history.disabled=busy||drawing||samBusy||!samInfo.jobs.length;
  $('sam-query-summary').textContent=samJob?(samJob.assistance?`Human box · ${labels[samJob.assistance.class_id]} · [${samJob.assistance.box.join(', ')}] · base revision ${samJob.assistance.source_revision}`:`${samJob.profile.prompts.length} queries · `+samJob.profile.prompts.map(p=>`${labels[p.class_id]}: ${p.prompt} (${p.threshold??samJob.profile.threshold})`).join('; ')+(samJob.profile.object_rules==='withhold_rail_overlapping_posts'?' · Rail-overlapping posts withheld':'')):'';
  const active=samInfo.jobs.some(j=>['queued','loading','running'].includes(j.state));
  const unavailable=busy||drawing||samBusy||(isTeam()&&(teamStale||teamOffline));
  let settingsError='';try{const p=samProfile();$('sam-settings-summary').textContent=`${p.prompts.length} text queries · `+[1,2,3,4].map(cid=>`${labels[cid]} ${p.prompts.find(q=>q.class_id===cid).threshold??p.threshold}`).join(' · ');}catch(e){settingsError=e.message;$('sam-settings-summary').textContent=settingsError;}
  $('sam-generate').disabled=unavailable||!session?.batch||active||!!settingsError;
  $('sam-box').disabled=unavailable||!current||selectedClass===0||active||!canEdit();
  $('sam-box').textContent=selectedClass===0?'Select an object class to draw a box':`Draw box · ${labels[selectedClass]}`;
  $('sam-box-confirmation').hidden=!boxDraft;
  $('sam-box-run').disabled=unavailable||active||!boxDraft||!canEdit();
  $('sam-box-cancel').disabled=busy||samBusy;
  if(boxDraft)$('sam-box-summary').textContent=`${labels[boxDraft.class_id]} · native box [${boxCoordinates().join(', ')}] · revision ${boxDraft.source_revision}. Run this suggestion?`;
  $('sam-cancel').hidden=!samJob||!['queued','loading','running'].includes(samJob.state);
  $('sam-cancel').disabled=unavailable||!!samJob?.cancel_requested;
  $('sam-retry').hidden=!samJob||!['failed','cancelled','interrupted'].includes(samJob.state);
  $('sam-retry').disabled=unavailable||active;
  $('sam-review').hidden=samJob?.state!=='complete';$('sam-review').disabled=unavailable;
  $('sam-review').textContent=samJob?.imported_batch?'Open imported results':'Review results';
  $('sam-status').textContent=samJob?`${samJob.state[0].toUpperCase()+samJob.state.slice(1)} · ${samJob.completed||0}/${samJob.total}${samJob.current?' · '+samJob.current:''}${samJob.cancel_requested&&['queued','loading','running'].includes(samJob.state)?' · Cancellation requested; waiting for a safe boundary':''}${samJob.error?' · '+samJob.error:''}`:active?'Another batch has an active local SAM job.':'Ready. Original suggestions and reviews are retained.';
  if(samInfo.worker_error&&active)$('sam-status').textContent+=' · '+samInfo.worker_error;
}
async function pollSam(){
  if(!session||!samInfo.enabled||!isOwner()||(isTeam()&&!teamAuthenticated))return;
  try{const response=await fetch('/api/sam/jobs');if(!response.ok)throw new Error();samInfo=await response.json();renderSam();}
  catch(e){$('sam-status').textContent='Progress unavailable. Reconnecting; editing remains available.';}
}
async function samAction(action,body){
  if(samBusy||busy||drawing||!isOwner())return;
  samBusy=true;renderSam();
  try{const result=await request('/api/sam/'+action,body);if(action==='generate')samSelectedJob=result.job_id;await pollSam();return result;}catch(e){message(e.message,true);return null;}finally{samBusy=false;renderSam();}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&tool==='box'){e.preventDefault();e.stopImmediatePropagation();cancelBox();}},true);
async function getJSON(path){
  const response=await fetch(path,{cache:'no-store'});
  if(!response.ok){const detail=await response.json().catch(()=>({}));if(isTeam()&&response.status===401)lockTeam('Your access has expired or was revoked. Sign in again.');throw new Error(detail.error||'The workspace could not be loaded. Try again.');}
  return response.json();
}
function setupTeam(){
  for(const event of ['pointerdown','keydown','input'])document.addEventListener(event,e=>{if(e.isTrusted)lastTeamActivity=Date.now();},{capture:true,passive:true});
  const bar=document.createElement('div');bar.id='team-bar';bar.hidden=true;
  bar.innerHTML='<div class="team-identity"><strong id="team-identity"></strong><span id="team-status" role="status">Shared workspace</span></div><div class="team-actions"><button id="team-members-open" hidden>Members</button><button id="team-logout">Sign out</button></div>';
  document.querySelector('.topbar').after(bar);
  const claims=document.createElement('div');claims.id='team-claim-bar';claims.hidden=true;
  claims.innerHTML='<span id="team-claim-label">Claim a frame to edit</span><div class="team-actions"><button id="team-claim" class="primary">Claim frame</button><button id="team-release" hidden>Release</button><button id="team-next">Next available</button></div>';
  document.querySelector('.toolbar').before(claims);
  const conflict=document.createElement('div');conflict.id='team-conflict';conflict.hidden=true;conflict.setAttribute('role','alert');
  conflict.innerHTML='<span id="team-conflict-text"></span><button id="team-reload">Reload saved frame</button>';
  claims.after(conflict);
  const revisions=document.createElement('section');revisions.id='team-snapshots';revisions.hidden=true;
  revisions.innerHTML='<h2>Saved mask versions</h2><p class="section-hint">Named versions survive reload. Restore clears approval.</p><label class="field-label" for="team-revisions">Current frame versions</label><select id="team-revisions" size="4" aria-label="Saved mask versions"></select><button id="team-restore">Restore selected version</button><p id="team-revision-status" class="small" role="status"></p>';
  document.querySelector('.inspector').append(revisions);
  const members=document.createElement('dialog');members.id='team-members-dialog';
  members.innerHTML='<div class="dialog-heading"><h2>Workspace members</h2><button id="team-members-close" aria-label="Close members">Close</button></div><p class="section-hint">Individual codes keep each review tied to its author.</p><div id="team-members-list"></div><form id="team-invite-form"><label class="field-label" for="team-member-name">New member name</label><input id="team-member-name" maxlength="100" autocomplete="off" required><label class="field-label" for="team-member-role">Access</label><select id="team-member-role"><option value="reviewer">Reviewer</option><option value="owner">Owner</option></select><button id="team-invite" class="primary" type="submit">Create access code</button></form><div id="team-invite-result" hidden><p>Share this code privately. It is shown only now.</p><output id="team-invite-code"></output><button id="team-invite-copy">Copy code</button><button id="team-invite-dismiss">Done sharing</button></div><p id="team-members-error" role="alert"></p>';
  document.body.append(members);
  $('team-login').onsubmit=async e=>{
    e.preventDefault();$('team-login-submit').disabled=true;$('auth-error').textContent='';
    try{const code=$('team-code').value;$('team-code').value='';const response=await fetch('/api/team/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Check your access code and try again.');teamAuthenticated=true;await refresh();}
    catch(error){$('auth-error').textContent=error.message;}
    finally{$('team-login-submit').disabled=false;}
  };
  $('auth-retry').onclick=startWorkspace;
  $('team-logout').onclick=async()=>{if(busy||drawing)return;try{if(teamStale&&current&&$('note').value!==current.note){if(!confirm('Sign out and discard the unsaved note?'))return;}else await saveNote();await request('/api/team/logout',{});lockTeam('Signed out. Enter your code to return.');}catch(e){message(e.message,true);}};
  $('team-claim').onclick=async()=>{
    if(busy||drawing||teamStale)return;try{setBusy(true,'Claiming frame…');const frameRevision=current.revision;session.team=await request('/api/team/claim',{batch:session.active_batch,index:session.active_sample});const fresh=await getJSON('/api/state');session.team=fresh.team;const saved=fresh.batch.samples[session.active_sample];if(saved.revision!==frameRevision)markTeamStale('This frame changed before your claim. Reload the saved frame before editing.');else $('save-status').textContent='Claimed by you · ready to edit';queue();}catch(e){message(e.message,true);}finally{setBusy(false);}
  };
  $('team-release').onclick=async()=>{if(busy||drawing)return;try{setBusy(true);await saveNote();await releaseCurrentClaim();queue();}catch(e){message(e.message,true);}finally{setBusy(false);}};
  $('team-next').onclick=nextTeamFrame;
  $('team-reload').onclick=async()=>{if(busy||drawing)return;if((teamStale||$('note').value!==current?.note)&&!confirm('Reload the saved frame? Unsaved local pixels and notes will be discarded.'))return;try{setBusy(true);clearTimeout(noteTimer);if(noteSavePromise)await noteSavePromise.catch(()=>{});await refresh();message('Latest saved frame loaded.');}catch(e){message(e.message,true);}finally{setBusy(false);}};
  $('team-revisions').onchange=updateControls;$('team-restore').onclick=restoreTeamRevision;
  $('team-members-open').onclick=async()=>{members.showModal();await loadTeamMembers();};
  $('team-members-close').onclick=()=>members.close();
  members.addEventListener('close',()=>{clearInvite();$('team-member-name').value='';$('team-members-error').textContent='';});
  $('team-invite-form').onsubmit=async e=>{
    e.preventDefault();$('team-invite').disabled=true;$('team-members-error').textContent='';clearInvite();
    try{const result=await request('/api/team/invite',{name:$('team-member-name').value.trim(),role:$('team-member-role').value});$('team-invite-code').textContent=result.code;$('team-invite-result').hidden=false;$('team-member-name').value='';await loadTeamMembers();}
    catch(e){$('team-members-error').textContent=e.message;}finally{$('team-invite').disabled=false;}
  };
  $('team-invite-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('team-invite-code').textContent);$('team-invite-copy').textContent='Copied';}catch(e){$('team-members-error').textContent='Copy is unavailable here. Select the code and copy it manually.';}};
  $('team-invite-dismiss').onclick=clearInvite;
  setInterval(()=>{if(isTeam()&&teamAuthenticated)updateControls();},1000);
}
function clearInvite(){$('team-invite-code').textContent='';$('team-invite-result').hidden=true;$('team-invite-copy').textContent='Copy code';}
function renderBatchOptions(){
  const select=$('batch');select.replaceChildren();
  if(!session.batches.length){const option=document.createElement('option');option.textContent='No batch imported';select.append(option);}
  for(const batch of session.batches){const option=document.createElement('option');option.value=batch.id;option.textContent=batch.title;option.selected=batch.id===session.active_batch;select.append(option);}
}
function lockTeam(text){
  teamPollGeneration++;teamAuthenticated=false;teamOffline=false;clearTimeout(noteTimer);clearTimeout(reviewerTimer);clearInvite();
  sharedLibrary={available:false};renderLibrary();
  $('team-members-dialog').close();session=null;current=null;pixels=null;image=null;undoStack=[];redoStack=[];drawing=false;boxDraft=null;teamStale=false;
  canvas.width=canvas.width;canvas.hidden=true;$('queue').replaceChildren();$('review-history').replaceChildren();$('edit-history').replaceChildren();$('team-revisions').replaceChildren();$('source').replaceChildren();$('note').value='';$('reviewer').value='';$('filename').textContent='Ready for review';$('batch').replaceChildren();
  $('team-auth').hidden=false;document.body.classList.add('boot-gated');$('auth-fields').hidden=false;$('auth-description').textContent='Sign in to your private shared workspace.';$('auth-error').textContent=text||'';$('auth-retry').hidden=true;$('team-code').value='';$('team-code').focus();
}
async function startWorkspace(){
  try{$('auth-error').textContent='';$('auth-retry').hidden=true;const status=await getJSON('/api/team/status');teamEnabled=!!status.enabled;
    if(teamEnabled&&!status.authenticated){lockTeam();return;}
    teamAuthenticated=!!status.authenticated;await refresh();document.body.classList.remove('boot-gated');$('team-auth').hidden=true;
  }catch(e){$('auth-error').textContent=e.message;$('auth-retry').hidden=false;}
}
function updateTeamControls(){
  const owner=isOwner(),claim=claimFor(),editable=canEdit(),locked=busy||drawing;
  $('team-bar').hidden=false;$('team-claim-bar').hidden=false;$('team-snapshots').hidden=false;
  $('team-identity').textContent=session?.team?.user?`${session.team.user.name} · ${session.team.user.role}`:'Private workspace';
  $('team-status').textContent=teamOffline?'Connection interrupted · reconnecting':teamStale?'Saved frame changed · reload required':'Shared workspace · updates automatically';
  $('team-members-open').hidden=!owner;$('team-members-open').disabled=locked;$('team-logout').disabled=locked;
  $('reviewer').readOnly=true;$('reviewer-help').textContent='Signed-in identity. Approval applies to this exact revision.';
  $('reviewer').setAttribute('aria-label','Signed-in reviewer');
  for(const id of ['import','demo','export','empty-import']){$(id).hidden=!owner;if(!owner)$(id).disabled=true;}
  if(!owner&&!current){$('empty').querySelector('h2').textContent='No shared batch yet';$('empty').querySelector('p').textContent='The workspace owner can import a review batch.';}
  for(const id of ['brush','erase','reset','approve','reject','skip','undo','redo','undo-review'])if(!editable)$(id).disabled=true;
  if(session?.undo_review&&session.undo_review.index!==session.active_sample){$('undo-review').disabled=true;$('undo-review').title='Open and claim the last reviewed frame to undo this decision';}
  $('note').disabled=locked||!current||!editable;
  document.querySelectorAll('.class-select input').forEach(input=>input.disabled=locked||!editable);
  $('size').disabled=locked||!editable;canvas.classList.toggle('read-only',!editable);
  $('team-claim-label').textContent=!current?'Select a shared batch':teamStale?'Reload required':claim?.session_owned?'You hold this frame':claim?`Claimed by ${claim.name}`:'Read only · claim to edit';
  $('team-claim').hidden=!!claim?.session_owned;$('team-claim').disabled=locked||!current||!!claim||teamStale||teamOffline;
  $('team-release').hidden=!claim?.session_owned;$('team-release').disabled=locked||teamStale||teamOffline;
  $('team-next').disabled=locked||!session?.batch||teamStale||teamOffline;
  $('team-reload').disabled=locked;$('team-restore').disabled=locked||!editable||!$('team-revisions').value;
  if(teamStale){$('previous').disabled=true;$('next').disabled=true;$('batch').disabled=true;document.querySelectorAll('.history-item,.queue-item').forEach(row=>row.disabled=true);}
}
async function releaseCurrentClaim(){if(!claimFor()?.session_owned)return;session.team=await request('/api/team/release',{batch:session.active_batch,index:session.active_sample});}
async function nextTeamFrame(){
  if(busy||drawing||teamStale||!session?.batch)return;
  try{setBusy(true,'Finding an available frame…');await saveNote();const result=await request('/api/team/next',{batch:session.active_batch});session.team=result;
    if(result.index===null||result.index===undefined){queue();message('No undecided frames are available. Other reviewers may hold the remaining frames.');return;}
    session.active_sample=result.index;await refresh();
  }catch(e){message(e.message,true);}finally{setBusy(false);}
}
async function loadTeamRevisions(){
  if(!isTeam()||!current||!teamAuthenticated)return;const batch=session.active_batch,index=session.active_sample,key=`${batch}/${index}/${current.revision}`;
  try{const result=await getJSON(`/api/team/revisions/${encodeURIComponent(batch)}/${index}`);if(!session||batch!==session.active_batch||index!==session.active_sample)return;
    const list=$('team-revisions'),previous=list.value;list.replaceChildren();for(const entry of result.revisions){const option=document.createElement('option');option.value=entry.snapshot_id;option.textContent=`r${entry.revision} · ${actionNames[entry.action]||entry.action} · ${entry.reviewer||'Imported'} · ${new Date(entry.at).toLocaleString()}`;list.append(option);}
    if([...list.options].some(o=>o.value===previous))list.value=previous;else if(list.options.length)list.selectedIndex=0;
    teamRevisionKey=key;$('team-revision-status').textContent=result.revisions.length?`${result.revisions.length} recoverable versions`:'No saved mask versions yet.';updateControls();
  }catch(e){$('team-revision-status').textContent=e.message;}
}
async function restoreTeamRevision(){
  if(busy||drawing||!requireEdit()||!$('team-revisions').value)return;
  if(!confirm('Restore this saved mask version? The current version stays in history. You must review and approve the restored mask again.'))return;
  try{setBusy(true,'Restoring saved mask…');await saveNote();await request('/api/team/restore',{batch:session.active_batch,index:session.active_sample,revision:current.revision,snapshot_id:$('team-revisions').value});await refresh();$('save-status').textContent='Version restored · approval required';message('Mask restored. Review it and record a fresh decision.');}
  catch(e){message(e.message,true);}finally{setBusy(false);}
}
async function loadTeamMembers(){
  if(!isOwner()||!teamAuthenticated)return;
  try{const result=await getJSON('/api/team/members'),list=$('team-members-list');list.replaceChildren();
    for(const member of result.members){const row=document.createElement('div');row.className='member-row';const name=document.createElement('span');name.textContent=`${member.name} · ${member.role}${member.revoked?' · revoked':''}`;row.append(name);
      if(!member.revoked&&member.id!==session.team.user.id){const revoke=document.createElement('button');revoke.textContent='Revoke';revoke.dataset.userId=member.id;revoke.onclick=async()=>{if(!confirm(`Revoke access for ${member.name}? Their active sessions will end.`))return;revoke.disabled=true;try{await request('/api/team/revoke',{user_id:member.id});await loadTeamMembers();}catch(e){$('team-members-error').textContent=e.message;revoke.disabled=false;}};row.append(revoke);}list.append(row);}
  }catch(e){$('team-members-error').textContent=e.message;}
}
async function pollTeam(){
  if(!isTeam()||!teamAuthenticated||teamPolling||busy||drawing||noteSavePromise||!session)return;
  teamPolling=true;const generation=teamPollGeneration,batch=session.active_batch,index=session.active_sample,revision=current?.revision;
  try{const fresh=await getJSON('/api/state');if(!teamAuthenticated||!session||busy||drawing||noteSavePromise||generation!==teamPollGeneration||batch!==session.active_batch||index!==session.active_sample||revision!==current?.revision)return;
    teamOffline=false;session.team=fresh.team;session.batches=fresh.batches;renderBatchOptions();
    if(!current&&!session.batch&&fresh.batch){await refresh();return;}
    if(fresh.active_batch===batch){const latest=fresh.batch?.samples[index];if(current&&latest?.revision!==current.revision)markTeamStale();session.batch=fresh.batch;session.review_history=fresh.review_history;session.undo_review=fresh.undo_review;}
    if(sharedQueueSignature()!==teamQueueSignature)queue();else updateControls();if(current&&teamRevisionKey!==`${batch}/${index}/${current.revision}`)await loadTeamRevisions();
    const owned=claimFor();if(owned?.session_owned&&!document.hidden&&!teamStale&&Date.now()-lastTeamActivity<120000){const expiry=typeof owned.expires_at==='number'?owned.expires_at*1000:Date.parse(owned.expires_at);if(expiry-Date.now()<(session.team.claim_ttl_seconds||180)*500){const team=await request('/api/team/claim',{batch,index});if(session&&batch===session.active_batch&&index===session.active_sample)session.team=team;}}
  }catch(e){if(teamAuthenticated){teamOffline=true;updateControls();}}
  finally{teamPolling=false;}
}
setupSam();setupTeam();setInterval(pollSam,2500);setInterval(pollTeam,3500);
renderLabels();startWorkspace();
