'use strict';
const $ = id => document.getElementById(id);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const token = location.hash.slice(1) || sessionStorage.getItem('member-intake-session') || '';
if (token) sessionStorage.setItem('member-intake-session', token);
history.replaceState(null, '', '/');
let state, activeTab = 'import', selectedId, countyId, confirmation, busy = false;
let checkedIds = new Set();
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', cache:'no-store', headers:{'X-App-Token':token, ...(data === undefined ? {} : {'Content-Type':'application/json'})}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The operation could not complete.');
  return result;
}
function notice(message, error=false, target='notice') {
  const node = $(target); node.textContent = message; node.hidden = !message; node.classList.toggle('error', error);
}
function records(status) { return (state?.records || []).filter(row => status.includes(row.status)); }
function destination() { return {subdomain:state.settings.subdomain.toLowerCase(), campaign_id:state.settings.campaign_id}; }
function destinationText() { return `${state.settings.subdomain}.actionbuilder.org · Campaign ${state.settings.campaign_id}`; }
function fieldLabel(key) { return ({entity_type:'Record type', address_line_1:'Street address', locality:'City', region:'State', postal_code:'ZIP code', given_name:'First name', family_name:'Last name', additional_name:'Middle initial', email:'Email', phone:'Phone'})[key] || key; }
function fullName(row) { return [row.person.given_name, row.person.additional_name, row.person.family_name].join(' '); }
function pill(text, kind='') { return `<span class="pill ${kind}">${escapeText(text)}</span>`; }
function empty(title, detail) { return `<div class="empty"><h3>${escapeText(title)}</h3><p>${escapeText(detail)}</p></div>`; }
function table(headings, rows) { return `<div class="table-wrap"><table><thead><tr>${headings.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`; }
function countyReference(row) { const r=row.residence || {}; const j=row.jurisdiction || {};return `<div class="county-reference"><p>${escapeText(r.county ? `${r.county}, ${r.region}` : 'County not entered')}</p>${pill(j.status==='inside'?'Inside by county reference':j.status==='outside'?'Outside by county reference':j.status==='unavailable'?'Local map not configured':r.county?'County needs map review':'County check available',j.status==='inside'?'good':j.status==='outside'||(j.status==='review'&&r.county)?'warn':'neutral')}<p><button type="button" data-county="${escapeText(row.id)}">County reference</button></p></div>`; }
function contact(row) { const p = row.person; return `<p>${escapeText(p.email)}</p><p>${escapeText(p.phone)}</p><details><summary>Address</summary><p>${escapeText(p.address_line_1)}<br>${escapeText(p.locality)}, ${escapeText(p.region)} ${escapeText(p.postal_code)}</p></details>${countyReference(row)}`; }
function showTab(tab) {
  activeTab = tab;
  for (const name of ['import','preview','results','review']) $(name+'-panel').hidden = name !== tab;
  document.querySelectorAll('[data-tab]').forEach(button => {if (button.dataset.tab===tab) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');});
}
function render() {
  const pending = records(['pending']), sent = records(['sent']), held = records(['review','uncertain']);
  $('destination').textContent = state.settings.configured ? destinationText() + (state.settings.residence_local ? ` · Residence local ${state.settings.residence_local} · Assessment 1` : ' · Member tags not configured') : 'Set up your Action Builder connection';
  $('downloads-path').textContent = state.settings.downloads;
  $('pending-count').textContent = pending.length ? `(${pending.length})` : '';
  $('sent-count').textContent = sent.length ? `(${sent.length})` : '';
  $('review-count').textContent = held.length ? `(${held.length})` : '';
  $('import-notices').replaceChildren(...state.notices.map(item => {const p=document.createElement('p');p.className='note';p.textContent=`${item.family}: ${item.reason}`;return p;}));
  $('preview-table').innerHTML = pending.length ? table(['Person and paperwork','Contact information','Status'], pending.map(row=>`<tr><td><strong>${escapeText(fullName(row))}</strong><p>${row.source_names.map(escapeText).join('<br>')}</p><p>Classification: ${escapeText(row.person.classification || 'Needs review')}</p></td><td>${contact(row)}</td><td>${pill(row.lookup?.outcome==='not_found' ? 'Checks passed' : 'Ready to check')}<p>Fresh checks run before sending.</p></td></tr>`)) : empty('No people waiting to be submitted', 'Find paperwork in Import, or view completed submissions in Results.');
  $('results-table').innerHTML = sent.length ? table(['Person','Result','Receipt'],sent.map(row=>`<tr data-status="sent"><td><strong>${escapeText(fullName(row))}</strong>${contact(row)}</td><td>${pill('Submission confirmed','good')}<p>${escapeText(new Date(row.updated_at).toLocaleString())}</p></td><td class="receipt">${(row.result?.identifiers || []).map(escapeText).join('<br>')}</td></tr>`)) : empty('No completed submissions yet','Confirmed submissions will appear here after a batch is sent.');
  $('check').disabled = $('send').disabled = !pending.length || !state.settings.configured;
  renderReview(held);
  showTab(activeTab);
}
function renderReview(held=records(['review','uncertain'])) {
  if (!held.length) { $('review-layout').innerHTML=empty('Nothing needs review','Any flagged people will stay here until you review them.'); return; }
  const row = held.find(row=>row.id===selectedId) || held[0]; selectedId=row.id;
  const lookup = row.lookup;
  $('review-layout').innerHTML = `<div class="review-grid"><aside class="people-list" aria-label="People needing review">${held.map(person=>`<button class="person-button ${person.id===row.id?'selected':''}" data-person="${person.id}" aria-pressed="${person.id===row.id}"><strong>${escapeText(fullName(person))}</strong>${pill(person.status==='uncertain'?'Check earlier attempt':'Needs review','warn')}</button>`).join('')}</aside><div class="review-detail"><h3>${escapeText(fullName(row))}</h3><div class="columns"><section class="card"><h3>From the paperwork</h3><div id="contact-detail"><dl>${state.fields.map(([key,label])=>`<dt>${escapeText(label)}</dt><dd>${escapeText(row.person[key] || 'Not set')}</dd>`).join('')}</dl><div class="action-bar"><button id="edit">Edit these details</button></div></div><form id="edit-form" hidden><div class="edit-fields">${state.fields.map(([key,label])=>`<label>${escapeText(label)}${key==='classification'?`<select name="classification" ${state.settings.residence_local?'required':''}><option value="">Choose a classification</option>${state.classifications.map(name=>`<option ${name===row.person.classification?'selected':''} value="${escapeText(name)}">${escapeText(name)}</option>`).join('')}</select>`:`<input name="${key}" value="${escapeText(row.person[key])}" required>`}</label>`).join('')}</div><div class="action-bar"><button type="button" id="edit-cancel">Cancel</button><button type="submit" class="primary">Save corrections</button></div></form></section><section class="card"><h3>Action Builder check</h3>${pill(lookup ? (lookup.outcome==='not_found'?'No email or phone matches':'Possible existing person') : 'Check required', lookup?.outcome==='not_found'?'good':'warn')}<p>${escapeText(lookup?.reason || row.reason)}</p>${lookup?`<p>Checked in ${escapeText(lookup.destination.subdomain)} · Campaign ${escapeText(lookup.destination.campaign_id)}</p>`:''}${(lookup?.candidates || []).map(candidate=>`<div class="candidate"><strong>Candidate record</strong><code>${escapeText(candidate.identifiers.join(', '))}</code><p>Matches: ${escapeText(candidate.matching_fields.map(fieldLabel).join(', '))}</p><p>Differs: ${escapeText(candidate.differing_fields.map(fieldLabel).join(', ') || 'None')}</p></div>`).join('')}${row.related_sent?'<div class="note">A related record was already submitted. Creating this person again is blocked.</div>':''}${row.related_uncertain?'<div class="note">An earlier attempt may have created this person. Check Action Builder before approving another submission.</div>':''}<div class="action-bar"><button id="review-check">Check revised details</button></div></section></div><p class="small source">${row.source_names.map(escapeText).join(' · ')}</p><div class="note">${escapeText(row.reason)}</div><div class="action-bar"><div><button id="keep">Keep for later</button><button id="discard" class="danger">Discard this import</button>${row.status==='uncertain'?'<button id="reconcile">Mark already created</button>':''}</div><button class="primary" id="approve" ${row.related_sent || !checkedIds.has(row.id) || !state.settings.configured ? 'disabled':''}>Review sending as a new person</button></div><p class="small">Run a fresh check above to enable approval. Submission checks again before sending.</p></div></div>`;
  document.querySelectorAll('[data-person]').forEach(button=>button.onclick=()=>{selectedId=button.dataset.person;renderReview();});
  $('edit').onclick=()=>{$('contact-detail').hidden=true;$('edit-form').hidden=false;};
  $('edit-cancel').onclick=()=>renderReview();
  $('edit-form').onsubmit=async event=>{event.preventDefault();const person=Object.fromEntries(new FormData(event.target));checkedIds.clear();await run('review-edit',{id:row.id,person});};
  $('review-check').disabled = !state.settings.configured;
  $('contact-detail').insertAdjacentHTML('beforeend', countyReference(row));
  $('review-check').onclick=async()=>{checkedIds.delete(row.id);if(await run('review-check',{id:row.id})){checkedIds.add(row.id);renderReview();}};
  $('keep').onclick=()=>{showTab('preview');notice('Kept for later. The record remains held for review.');};
  $('discard').onclick=()=>confirmAction('discard',row);
  $('approve').onclick=()=>confirmAction('review',row);
  if($('reconcile')){$('reconcile').disabled=!state.settings.configured;$('reconcile').onclick=()=>confirmAction('reconcile',row);}
}
async function refresh() {state=await api('state');render();}
async function run(action, data={}) {
  if(busy) return false;
  busy=true; document.body.classList.add('busy');
  const controls=[...document.querySelectorAll('button,input,textarea,select')].map(node=>[node,node.disabled]);
  controls.forEach(([node])=>node.disabled=true);
  const target = $('settings-dialog').open ? 'settings-message' : $('county-dialog').open ? 'county-message' : 'notice';
  notice('Working… Please keep the app open.',false,target);
  let succeeded=false, message='', failed=false;
  try {
    await api(action,data);
    let job;
    do {await new Promise(resolve=>setTimeout(resolve,500));job=await api('status');} while(job.running);
    message=job.message;failed=job.error;succeeded=!failed;
  } catch(error){message=error.message;failed=true;}
  finally {
    busy=false;document.body.classList.remove('busy');controls.forEach(([node,disabled])=>node.disabled=disabled);
    try {await refresh();} catch(error){message+='\n'+error.message;failed=true;succeeded=false;}
    notice(message,failed,target);
  }
  return succeeded;
}
function openSettings() {
  const form=$('settings-form');const s=state.settings;
  form.elements.subdomain.value=s.subdomain;form.elements.campaign_id.value=s.campaign_id;form.elements.api_key.value='';form.elements.downloads.value=s.downloads;form.elements.residence_local.value=s.residence_local;form.elements.county_lookup.value=s.county_lookup;
  form.elements.api_key.required=!s.has_key;
  $('key-hint').textContent=s.has_key?'A key is saved. Leave blank to keep it, or enter a replacement.':'Enter your authorized Action Builder API key.';
  $('setup-copy').textContent=s.configured?'Update the connection or choose your paperwork folder.':'Welcome. Save your credentials to connect this local workspace to Action Builder.';
  notice('',false,'settings-message');$('settings-dialog').showModal();
}
function confirmAction(kind,row) {
  confirmation={kind,row};
  $('reason').value='';$('ack').checked=false;
  $('ack-label').hidden=!['review','reconcile'].includes(kind);$('reason-label').hidden=['batch','reconcile'].includes(kind);
  $('ack').required=['review','reconcile'].includes(kind);$('reason').required=['review','discard'].includes(kind);
  $('ack-copy').textContent=kind==='reconcile'?'I verified that this person already exists with the correct contact details in this campaign.':'I checked the existing records and any earlier sending attempt, and confirmed this person should be created as a new person.';
  $('confirm-title').textContent=kind==='discard'?'Discard this import?':kind==='reconcile'?'Mark the earlier submission complete?':'Confirm new-person submission';
  $('confirm-destination').textContent=kind==='discard'?'Its queue history will be retained.':destinationText()+(state.settings.residence_local?` · Residence local ${state.settings.residence_local} · Assessment 1`:' · Member tags not configured');
  $('confirm-submit').textContent=kind==='discard'?'Discard import':kind==='reconcile'?'Verify and mark complete':'Confirm submission';
  const names=kind==='batch'?records(['pending']).map(fullName):[fullName(row)];
  $('confirm-content').innerHTML=`<ul>${names.map(name=>`<li>${escapeText(name)}</li>`).join('')}</ul><p>${kind==='batch'?'Fresh checks run for everyone. Only people with no email or phone matches are submitted; others stay held.':kind==='reconcile'?'A read-only check must find one exact match. Only your local queue is updated; nothing is sent again.':kind==='review'?'This creates a new person. It does not update an existing record.':'The local contact file will be removed. This action cannot be undone in the app.'}</p>`;
  if(kind==='batch') confirmation.ids=records(['pending']).map(row=>row.id);
  confirmation.destination=destination();confirmation.residence_local=state.settings.residence_local;
  $('confirm-dialog').showModal();
}
function openCounty(id) {
  const row=state.records.find(row=>row.id===id);
  if(!row){$('county-dialog').close();return;}
  if(countyId!==id)notice('',false,'county-message');
  countyId=id;
  const r=row.residence || {}, j=row.jurisdiction, p=row.person;
  $('county-title').textContent=`County reference · ${fullName(row)}`;
  $('county-address').textContent=`${p.address_line_1}, ${p.locality}, ${p.region} ${p.postal_code}`;
  $('county-input').value=r.county || '';
  $('county-summary').textContent=j.summary;
  $('county-summary').className='note '+(j.status==='inside'?'good':j.status==='unavailable'||!r.county?'neutral':'');
  $('county-map').href=j.map_url;
  $('county-source').href=j.source_url;
  $('county-matched').textContent=r.source==='census'?`Census matched: ${r.matched_address}. Check this against the paperwork.`:r.county?'County entered manually.':'';
  $('county-note').value=j.note;
  $('county-note-section').hidden=!j.note;
  $('county-lookup').hidden=state.settings.county_lookup!=='census';
  $('county-privacy').textContent=state.settings.county_lookup==='census'?'Look up county sends this street address, city, state, and ZIP to the U.S. Census Bureau. Names and union information are excluded.':'Enter the county from the residence address. This step stays on this computer. Optional Census lookup is available in Settings.';
  if(!$('county-dialog').open)$('county-dialog').showModal();
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-county]');if(button&&!busy)openCounty(button.dataset.county);});
$('county-close').onclick=()=>$('county-dialog').close();
$('county-input').oninput=()=>{$('county-summary').textContent='Save county to refresh the reference.';$('county-summary').className='note neutral';$('county-note').value='';$('county-note-section').hidden=true;notice('',false,'county-message');};
$('county-form').onsubmit=async event=>{event.preventDefault();const id=countyId;if(await run('county-save',{id,county:$('county-input').value}))openCounty(id);};
$('county-lookup').onclick=async()=>{const id=countyId;if(await run('county-lookup',{id}))openCounty(id);};
$('county-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('county-note').value);notice('Note copied. Paste it into the person’s Notes box in Action Builder.',false,'county-message');}catch(error){$('county-note').focus();$('county-note').select();notice('Select and copy the note, then paste it into Action Builder.',false,'county-message');}};
$('confirm-form').onsubmit=async event=>{
  event.preventDefault();const c=confirmation;const reason=$('reason').value;const checked=$('ack').checked;$('confirm-dialog').close();
  if(c.kind==='batch') {await run('send',{ids:c.ids,destination:c.destination,residence_local:c.residence_local,confirmed:true});showTab('results');}
  else if(c.kind==='discard'){checkedIds.delete(c.row.id);await run('review-discard',{id:c.row.id,reason});}
  else if(c.kind==='reconcile'){checkedIds.delete(c.row.id);await run('review-reconcile',{id:c.row.id,destination:c.destination,checked,confirmed:true});}
  else {checkedIds.delete(c.row.id);await run('review-send',{id:c.row.id,reason,checked,confirmed:true});}
};
$('cancel').onclick=()=>$('confirm-dialog').close();
$('settings-open').onclick=openSettings;
$('settings-close').onclick=()=>$('settings-dialog').close();
$('settings-form').onsubmit=async event=>{event.preventDefault();checkedIds.clear();if(await run('settings',Object.fromEntries(new FormData(event.target)))){event.target.elements.api_key.value='';event.target.elements.api_key.required=false;$('key-hint').textContent='A key is saved. Leave blank to keep it, or enter a replacement.';}};
$('test').onclick=()=>{const form=$('settings-form');if(form.reportValidity())run('test',Object.fromEntries(new FormData(form)));};
$('scan').onclick=async()=>{checkedIds.clear();if(await run('import'))showTab('preview');};
$('refresh').onclick=()=>refresh().catch(error=>notice(error.message,true));
$('check').onclick=()=>run('check',{ids:records(['pending']).map(row=>row.id),destination:destination(),residence_local:state.settings.residence_local});
$('send').onclick=()=>confirmAction('batch');
$('go-review').onclick=()=>showTab('review');
$('quit').onclick=async()=>{try{await api('quit',{});sessionStorage.removeItem('member-intake-session');document.querySelectorAll('button,input,textarea,select').forEach(node=>node.disabled=true);notice('Member Intake is closed. You can close this tab. Use the desktop shortcut to reopen it.');}catch(error){notice(error.message,true);}};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>showTab(button.dataset.tab));
for(const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
(async()=>{
  try {
    let job=await api('status');
    while(job.running){notice('An operation is running. Waiting for it to finish…');await new Promise(resolve=>setTimeout(resolve,750));job=await api('status');}
    await refresh();
    if(job.message)notice(job.message,job.error);
    if(records(['pending']).length)showTab('preview');
    if(!state.settings.configured)openSettings();
  }catch(error){notice(error.message,true);}
})();
