'use strict';
// Loaded only by desktop_server.py. The production editor remains unchanged.
let driveState, drivePolling=false, driveDialogSignature='';
// Standard desktop shortcuts apply to masks, not text fields or open dialogs.
document.addEventListener('keydown',e=>{
  if(!(e.metaKey||e.ctrlKey)||e.altKey||e.repeat||busy||drawing||!current||!canEdit()
      ||e.target.closest('input,textarea,select,[contenteditable=true]')||document.querySelector('dialog[open]'))return;
  const key=e.key.toLowerCase();
  const action=key==='z'?(e.shiftKey?'redo':'undo'):key==='y'&&!e.shiftKey?'redo':null;
  if(action){e.preventDefault();history(action);}
});
$('undo').title='Undo mask edit: Command+Z or Ctrl+Z';
$('redo').title='Redo mask edit: Command+Shift+Z, Ctrl+Shift+Z or Ctrl+Y';
const driveBar=document.createElement('div');driveBar.className='drive-bar';driveBar.id='drive-bar';
driveBar.innerHTML='<span class="drive-dot" id="drive-dot"></span><strong>Google Drive</strong><span id="drive-summary" role="status">Checking local setup…</span><button id="drive-changes" hidden>Incoming changes</button><button id="drive-open">Google setup & library</button>';
document.querySelector('.topbar').after(driveBar);
$('demo').hidden=true;$('empty').querySelector('h2').textContent='Open your shared photo library';$('empty').querySelector('p').textContent='Connect Google to download the team library, or import a review ZIP from this computer.';
const driveDialog=document.createElement('dialog');driveDialog.className='drive-dialog';driveDialog.id='drive-dialog';
driveDialog.innerHTML=`<div class="drive-dialog-heading"><div><p class="eyebrow">LOCAL DESKTOP WORKSPACE</p><h2>Your shared photo library</h2></div><button id="drive-close" aria-label="Close Google setup">✕</button></div>
<p class="drive-intro">Photos and suggestions stay intact. Saved reviews queue on this computer, then sync while this app is open.</p>
<p id="drive-detail" role="status"></p><p id="drive-error" role="alert"></p>
<section id="drive-setup"><h3>Connect your Google account</h3><p>Choose the Desktop OAuth client JSON supplied by your team owner. Each teammate signs in with their own account.</p><label class="drive-file-label">Desktop client JSON <input id="drive-client" type="file" accept="application/json,.json"></label><p id="drive-client-status"></p>
<label class="drive-consent"><input id="drive-consent" type="checkbox"> I understand Google will request access to all Drive files my account can access. This app uses only the selected library folders. Existing folder permissions still apply.</label><p class="small">Google's restricted Drive scope is required to discover existing shared-folder children. Selecting a folder with drive.file alone does not grant those children. Your owner must enable Drive API and configure OAuth consent and test users or complete required Google verification.</p><button id="drive-connect" class="primary">Sign in with Google</button></section>
<section id="drive-connected" hidden><div class="drive-row"><h3 id="drive-account">Connected account</h3><button id="drive-disconnect">Disconnect</button></div><div class="drive-row"><button id="drive-sync-now">Sync now</button><button id="drive-pause">Pause sync</button></div></section>
<section><h3>Private library configuration</h3><p class="small">Ask your library owner for the private library configuration JSON. Import it separately from the Desktop OAuth client. A workspace stays bound to its first library.</p><label class="drive-file-label">Library configuration JSON <input id="drive-library-config" type="file" accept="application/json,.json"></label><p id="drive-library-status"></p><div id="drive-folders"></div></section>
<section id="drive-download"><h3>Open a saved library snapshot</h3><p>A snapshot downloads original photos, suggestions and working masks. Prior approvals become provenance and need a fresh review here.</p><button id="drive-list">Find library snapshots</button><div class="drive-row"><select id="drive-snapshots" aria-label="Library snapshot"><option value="">Choose a snapshot</option></select><button id="drive-download-selected" class="primary">Download & open</button></div></section>
<section><div class="drive-row"><h3>Incoming reviews</h3><button id="drive-refresh-changes">Refresh</button></div><p class="small">Incoming pixels never replace your open canvas automatically. Select a saved version after inspecting its native mask. Reconcile retains both branches and needs fresh approval.</p><div id="drive-updates"></div></section>
<section><h3>Saved versions of the open frame</h3><p class="small">Restore makes a new version and clears approval. All prior versions remain available.</p><div id="drive-versions"></div></section>
<section><h3>Incoming photos</h3><p class="small">New photos are quarantined until a human assigns capture details and rights/provenance. Development and validation only; no automatic training export.</p><label class="drive-file-label">Add a photo <input id="drive-photo" type="file" accept="image/png,image/jpeg"></label><div id="drive-incoming"></div></section>`;
document.body.append(driveDialog);
function driveError(error){$('drive-error').textContent=error?.message||String(error);}
async function driveAction(path,body={}){if(!session)throw new Error('Wait for the local workspace to open.');$('drive-error').textContent='';return request('/api/drive/'+path,body);}
async function drivePoll(){if(drivePolling)return;drivePolling=true;try{driveState=await getJSON('/api/drive/status');renderDrive();}catch(e){$('drive-summary').textContent='Local app unavailable. Reopen the launcher.';}finally{drivePolling=false;}}
function renderDrive(){
  const d=driveState;if(!d)return;
  if(d.authenticated&&d.account?.displayName&&session&&!$('reviewer').value.trim()&&!session.reviewer){$('reviewer').value=d.account.displayName;saveReviewer().catch(driveError);}
  $('drive-summary').textContent=d.error?`${d.queued} queued · needs attention`:!d.configured||!d.library_configured?'Setup required · local editing available':!d.authenticated?`${d.queued} queued · sign in to sync`:d.paused?`${d.queued} queued · sync paused`:d.activity||d.queued?`${d.queued} saved ${d.queued===1?'review':'reviews'} queued${d.activity?' · '+d.activity:''}`:`Synced${d.last_sync?' · '+new Date(d.last_sync).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):''}`;
  $('drive-dot').className='drive-dot '+(d.error?'error':d.authenticated&&!d.queued?'connected':'');
  $('drive-changes').hidden=!d.updates.length&&!d.incoming.length;$('drive-changes').textContent=`${d.updates.length} review ${d.updates.length===1?'update':'updates'} · ${d.incoming.length} incoming photos`;
  $('drive-detail').textContent=d.fixture?'Isolated test service. This does not verify actual Google Drive.':d.authenticated?`Signed in${d.account?.emailAddress?' as '+d.account.emailAddress:''}. ${d.queued} immutable uploads queued.`:'Local edits are saved here. Google connection has not been completed.';
  if(d.error)$('drive-error').textContent=d.error;
  $('drive-client-status').textContent=d.configured?'Desktop client configured.':'Setup needed: ask the team owner for the approved Desktop OAuth client JSON.';
  $('drive-library-status').textContent=d.library_configured?'Library configured on this computer.':'No library configured. Offline import and review are available.';
  $('drive-setup').hidden=d.authenticated;$('drive-connected').hidden=!d.authenticated;$('drive-connect').disabled=!d.configured||!d.library_configured||!$('drive-consent').checked;
  $('drive-account').textContent=d.account?.displayName||'Google account connected';$('drive-pause').textContent=d.paused?'Resume sync':'Pause sync';
  $('drive-list').disabled=$('drive-download-selected').disabled=!d.authenticated||!d.library_configured;
  const folders=$('drive-folders');folders.replaceChildren();for(const [key,id] of Object.entries(d.folders)){const a=document.createElement('a');a.href='https://drive.google.com/drive/folders/'+id;a.target='_blank';a.rel='noopener noreferrer';a.textContent=key[0].toUpperCase()+key.slice(1)+' ↗';folders.append(a);}
  const signature=JSON.stringify([d.updates,d.incoming,d.versions]);
  // Do not rebuild selectors or typed provenance while a person is using them.
  const editingForm=driveDialog.contains(document.activeElement)&&document.activeElement.matches('input:not([type=file]),textarea,select');
  if(signature!==driveDialogSignature&&!editingForm){driveDialogSignature=signature;renderDriveChanges();renderDriveIncoming();renderDriveVersions();}
}
function renderDriveChanges(){
  const list=$('drive-updates');list.replaceChildren();
  if(!driveState.updates.length){list.textContent='No incoming review changes.';return;}
  for(const update of driveState.updates){
    const row=document.createElement('article');row.className='drive-change';row.dataset.batch=update.batch;row.dataset.index=update.index;const h=document.createElement('h4');h.textContent=`${update.filename} · ${update.conflict?'conflicting branches':'saved update'}`;
    const select=document.createElement('select');select.setAttribute('aria-label','Version for '+update.filename);
    for(const choice of update.choices){const option=document.createElement('option');option.value=choice.id;option.textContent=`${choice.id===update.local?'This computer':'Incoming'} · ${choice.reviewer||'Unreviewed'} · ${choice.state} · ${choice.device} · ${new Date(choice.at).toLocaleString()}`;select.append(option);}
    const preview=document.createElement('a');preview.target='_blank';preview.rel='noopener';preview.textContent='Open native grayscale PNG ↗';const previewCanvas=document.createElement('canvas');previewCanvas.className='drive-preview';previewCanvas.setAttribute('aria-label','Selected revision over original photo');const previewUpdate=()=>{preview.href=update.choices.find(x=>x.id===select.value).mask_url;drivePreview(previewCanvas,update.batch,update.index,select.value).catch(driveError);};select.onchange=previewUpdate;previewUpdate();
    const button=document.createElement('button');button.textContent=update.conflict?'Use selected version & reconcile':'Apply selected version';
    // Head identities come from the server rather than inferred display choices.
    button.onclick=async()=>{if(busy||drawing){driveError(new Error('Finish the current edit before applying incoming work.'));return;}if(!confirm('Use this saved version? Both histories remain. The selected pixels replace this frame locally and require a fresh approval.'))return;try{await saveNote();const reviewer=$('reviewer').value.trim()||driveState.account?.displayName||'';if(!reviewer)throw new Error('Enter your reviewer name in the editor first.');const result=await driveAction('reconcile',{frame:update.frame,selected:select.value,heads:update.heads,reviewer});await request('/api/session',{active_batch:result.batch,active_sample:result.index});await refresh();await drivePoll();}catch(e){driveError(e);}};
    row.append(h,select,previewCanvas,preview,button);list.append(row);
  }
}
async function drivePreview(target,batch,index,revision){
  target.dataset.revision=revision;
  const [photo,mask]=await Promise.all([loadImage(`/asset/${batch}/${index}/image`),loadImage('/api/drive/preview/'+revision)]);
  if(target.dataset.revision!==revision)return;
  target.width=photo.naturalWidth;target.height=photo.naturalHeight;
  const cx=target.getContext('2d');cx.drawImage(mask,0,0);const overlay=cx.getImageData(0,0,target.width,target.height);
  for(let i=0;i<overlay.data.length;i+=4){const label=overlay.data[i],color=colors[label]||colors[0];overlay.data[i]=color[0];overlay.data[i+1]=color[1];overlay.data[i+2]=color[2];overlay.data[i+3]=label?145:0;}
  const layer=document.createElement('canvas');layer.width=target.width;layer.height=target.height;layer.getContext('2d').putImageData(overlay,0,0);cx.drawImage(photo,0,0);cx.drawImage(layer,0,0);
}
function renderDriveVersions(){
  const list=$('drive-versions');list.replaceChildren();const versions=driveState.versions;
  if(!versions?.items.length){list.textContent='Saved versions appear after the first edit or decision.';return;}
  const select=document.createElement('select');select.setAttribute('aria-label','Saved mask version');for(const rev of [...versions.items].reverse()){const option=document.createElement('option');option.value=rev.id;option.textContent=`${rev.id===versions.current?'Current · ':''}${rev.reviewer||'Unreviewed'} · ${rev.state} · ${new Date(rev.at).toLocaleString()}`;select.append(option);}
  const preview=document.createElement('canvas');preview.className='drive-preview';const draw=()=>drivePreview(preview,versions.batch,versions.index,select.value).catch(driveError);select.onchange=draw;draw();
  const button=document.createElement('button');button.textContent='Restore selected version';button.onclick=async()=>{if(busy||drawing)return;if(!confirm('Restore these pixels as a new version? Approval will be cleared.'))return;try{await saveNote();const reviewer=$('reviewer').value.trim()||driveState.account?.displayName||'';if(!reviewer)throw new Error('Enter your reviewer name in the editor first.');await driveAction('reconcile',{frame:versions.frame,selected:select.value,heads:versions.heads,reviewer});await refresh();await drivePoll();}catch(e){driveError(e);}};
  list.className='drive-change';list.append(select,preview,button);
}
function renderDriveIncoming(){
  const list=$('drive-incoming');list.replaceChildren();
  if(!driveState.incoming.length){list.textContent='No quarantined photos.';return;}
  for(const photo of driveState.incoming){const form=document.createElement('form');form.className='drive-change';const title=document.createElement('h4');title.textContent=`${photo.name} · ${photo.width} × ${photo.height} · ${photo.assignment_conflict?'conflicting capture assignments':'awaiting assignment'}`;form.append(title);
    if(photo.assignments?.length){
      const description=document.createElement('p');description.textContent=photo.assignment_conflict?'People assigned different capture metadata to this photo. Choose explicitly; prior assignments and reviews remain intact.':'A teammate supplied capture details. Open their assignment without retyping it; this computer starts pending.';form.append(description);
      for(const assignment of photo.assignments){const row=document.createElement('div');row.className='drive-assignment';const text=document.createElement('p');text.textContent=`${assignment.metadata.dataset} / ${assignment.metadata.session} / ${assignment.metadata.run} · ${assignment.metadata.split}\nRights / provenance: ${assignment.metadata.provenance}\nAssigned by ${assignment.reviewer||'Reviewer'}`;const open=document.createElement('button');open.type='button';open.textContent=photo.assignment_conflict?'Choose this capture assignment':'Open shared assignment';open.onclick=async()=>{try{await saveNote();await saveReviewer();const reviewer=$('reviewer').value.trim();if(!reviewer)throw new Error('Enter your reviewer name in the editor first.');const result=await driveAction('apply-source',{id:assignment.id,reviewer});await request('/api/session',{active_batch:result.batch,active_sample:0});await refresh();await drivePoll();message('Shared photo opened pending. Inspect it and record a fresh decision.');}catch(e){driveError(e);}};row.append(text,open);form.append(row);}
      list.append(form);continue;
    }
    for(const key of ['dataset','session','run','provenance']){const label=document.createElement('label');label.textContent=key==='provenance'?'Rights / provenance':key[0].toUpperCase()+key.slice(1);const input=document.createElement('input');input.name=key;input.required=true;input.maxLength=200;label.append(input);form.append(label);}
    const split=document.createElement('select');split.name='split';split.setAttribute('aria-label','Assigned split');for(const value of ['development','validation']){const option=document.createElement('option');option.value=value;option.textContent=value;split.append(option);}const submit=document.createElement('button');submit.textContent='Assign & open for review';submit.type='submit';form.append(split,submit);
    form.onsubmit=async e=>{e.preventDefault();try{await saveNote();await saveReviewer();await driveAction('assign',{id:photo.id,metadata:Object.fromEntries(new FormData(form))});await refresh();await drivePoll();}catch(error){driveError(error);}};list.append(form);
  }
}
$('drive-open').onclick=$('drive-changes').onclick=()=>{driveDialog.showModal();drivePoll();};$('drive-close').onclick=()=>driveDialog.close();
$('drive-consent').onchange=renderDrive;
$('drive-library-config').onchange=async()=>{try{const file=$('drive-library-config').files[0];if(!file)return;if(file.size>16384)throw new Error('Library configuration exceeds 16 KiB.');await driveAction('library-config',{library:JSON.parse(await file.text())});$('drive-library-config').value='';await drivePoll();}catch(e){driveError(e);}};
$('drive-client').onchange=async()=>{try{const file=$('drive-client').files[0];if(!file)return;if(file.size>65536)throw new Error('Desktop client JSON exceeds 64 KiB.');await driveAction('configure',{client:JSON.parse(await file.text())});$('drive-client').value='';await drivePoll();}catch(e){driveError(e);}};
$('drive-connect').onclick=async()=>{try{const result=await driveAction('connect',{acknowledge_scope:$('drive-consent').checked});const link=document.createElement('a');link.href=result.url;link.target='_blank';link.rel='noopener';link.click();$('drive-detail').textContent='Complete Google sign-in in the opened browser, then return here.';}catch(e){driveError(e);}};
$('drive-disconnect').onclick=async()=>{try{await driveAction('disconnect');await drivePoll();}catch(e){driveError(e);}};
$('drive-sync-now').onclick=async()=>{try{await driveAction('sync',{paused:false});await drivePoll();}catch(e){driveError(e);}};
$('drive-pause').onclick=async()=>{try{await driveAction('sync',{paused:!driveState.paused});await drivePoll();}catch(e){driveError(e);}};
$('drive-refresh-changes').onclick=drivePoll;
$('drive-list').onclick=async()=>{try{const result=await getJSON('/api/drive/library');const select=$('drive-snapshots');select.replaceChildren();for(const f of result.files){const option=document.createElement('option');option.value=f.id;option.textContent=f.name;select.append(option);}if(!result.files.length)throw new Error('No snapshots are visible in the selected label folder. Check your Google account permissions.');}catch(e){driveError(e);}};
$('drive-download-selected').onclick=async()=>{try{if(busy||drawing)throw new Error('Finish the current edit first.');await saveNote();const id=$('drive-snapshots').value;if(!id)throw new Error('Find and choose a snapshot first.');$('drive-detail').textContent='Downloading and validating native review files…';await driveAction('import',{file_id:id});await refresh();await drivePoll();driveDialog.close();message('Library downloaded. Prior approvals need a fresh review.');}catch(e){driveError(e);}};
$('drive-photo').onchange=async()=>{try{const file=$('drive-photo').files[0];if(!file)return;if(file.size>12*1024*1024)throw new Error('Choose a photo smaller than 12 MiB.');await driveAction('incoming',{name:file.name,bytes:bytes64(new Uint8Array(await file.arrayBuffer()))});$('drive-photo').value='';await drivePoll();}catch(e){driveError(e);}};
drivePoll();setInterval(drivePoll,3000);
