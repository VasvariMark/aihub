'use strict';
const $ = id => document.getElementById(id);
let token = '', current = null, mission = null, editing = null, refreshing = false, lastHealth = 0;
let historyKey = '', taskKey = '';
const profileFields=['name','description','stack','functionality','constraints','notes','release','project_status','privacy','preferred_worker','instructions'];
let providerProfiles=[];
let historyPage=0;
let providers = {}, queue = [];
let projectsLoaded=false, projectEditing=null;
const roleNames = {researcher: 'Researcher', writer: 'Writer', coder: 'Coder', reviewer: 'Reviewer'};
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-AIHub-Token': token},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}
function shortDate(date) { return new Date(date).toLocaleString('en-GB', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}); }
async function health() {
  try {
    const state = await api('/api/health');
    $('connection').textContent = state.connected ? '● Ollama connected' : '○ Ollama unavailable';
    $('connection').className = 'connection ' + (state.connected ? 'online' : 'offline');
    $('connection').title = state.error || `Local Ollama ${state.version} · 127.0.0.1:11434`;
  } catch (error) {
    $('connection').textContent = '○ Connection unavailable';
    $('connection').className = 'connection offline';
    $('connection').title = error.message;
  }
  lastHealth = Date.now();
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const state = await api('/api/state'); token = state.token; providers=state.providers;
    $('provider-status').textContent=Object.values(providers).map(p=>p.label+' · '+p.status).join(' | ')+' · External APIs and CLI disabled';
    if ($('overview').open) await loadOverview();
    if (!projectsLoaded) await loadProjects();
    $('model-name').textContent = state.model;
    $('context-size').textContent = state.context.toLocaleString('en-US') + ' context';
    const page=await api('/api/mission-search',{search:$('mission-search').value,project:$('project-filter').value,page:historyPage});
    $('prev-page').disabled=!historyPage;$('next-page').disabled=!page.more;
    $('history-count').textContent = 'Page '+(historyPage+1);
    const nextHistoryKey = JSON.stringify([current, page.missions,$('mission-search').value,$('project-filter').value]);
    const history = $('history');
    if (nextHistoryKey !== historyKey) {
      historyKey = nextHistoryKey; history.replaceChildren();
      if (!page.missions.length) history.append(el('p', 'muted', 'No missions yet.'));
      for (const item of page.missions) {
      const button = el('button', item.id === current ? 'selected' : '', item.title);
      button.append(el('small', '', `${shortDate(item.created_at)} · ${item.status}`));
      button.onclick = () => selectMission(item.id);
      history.append(button);
      }
    }
    if (current && (!mission || mission.id!==current || !state.missions.some(m=>m.id===current && m.updated_at===mission.updated_at))) {
      const id = current, value = await api('/api/missions/' + id);
      if (current === id) { mission = value; renderMission(); }
    }
    if (Date.now() - lastHealth > 15000) await health();
  } catch (error) { notice(error.message); }
  finally { refreshing = false; }
}
function selectMission(id) {
  current = id; editing = null; taskKey = ''; notice();
  $('empty').hidden = true; $('mission-view').hidden = false;
  if (!refreshing) refresh();
}
function newMission() {
  current = null; mission = null; editing = null; taskKey = '';
  $('mission-view').hidden = true; $('empty').hidden = false;
  notice(); $('mission-input').focus(); refresh();
}
function actionButton(label, action, task, style = '') {
  const button = el('button', style, label);
  button.onclick = async () => {
    if (action === 'approve') { await reviewHandoff(task,button.closest('article')); return; }
    button.disabled = true; notice();
    try { await api(`/api/tasks/${task.id}/${action}`, {version: task.version}); await refresh(); }
    catch (error) { notice(error.message); await refresh(); }
    finally { button.disabled = false; }
  };
  return button;
}
function editForm(task, card) {
  editing = task.id;
  const form = el('form', 'editor');
  const controls = {};
  for (const [key, label, max] of [['title','Task title',160],['role','Role',0],['instructions','Instructions',2400],['expected_output','Expected output',800]]) {
    const wrapper = el('label', '', label);
    const input = el(key === 'role' ? 'select' : key === 'title' ? 'input' : 'textarea');
    if (key === 'role') {
      for (const [value, name] of Object.entries(roleNames)) { const option = el('option', '', name); option.value = value; input.append(option); }
    } else { input.maxLength = max; input.required = true; }
    input.value = task[key]; controls[key] = input; wrapper.append(input); form.append(wrapper);
  }
  const depsLabel=el('label','','Dependencies (Ctrl/Cmd-click for multiple; choose none for independent work)');
  const deps=el('select'); deps.multiple=true;
  for (const candidate of mission.tasks.filter(t=>t.id!==task.id)) {
    const option=el('option','',candidate.title+' · '+candidate.status); option.value=candidate.id; option.selected=task.dependencies.includes(candidate.id); deps.append(option);
  }
  depsLabel.append(deps); form.append(depsLabel);
  const actions = el('div', 'editor-actions'), save = el('button','primary','Save & require approval'), cancel = el('button','','Cancel');
  save.type = 'submit'; cancel.type = 'button';
  cancel.onclick = () => { editing = null; taskKey = ''; renderMission(); };
  actions.append(save, cancel); form.append(actions);
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true;
    try {
      const edited = Object.fromEntries(Object.entries(controls).map(([key,input]) => [key,input.value]));
      edited.dependencies=[...deps.selectedOptions].map(o=>o.value);
      await api(`/api/tasks/${task.id}/edit`, {version:task.version, task:edited});
      editing = null; notice(); await refresh();
    } catch (error) { notice(error.message); } finally { save.disabled = false; }
  };
  card.append(form); controls.title.focus();
}
function taskCard(task, index) {
  const card = el('article','task'), top = el('div','task-top');
  top.append(el('span',`role-icon ${task.role}`,task.role === 'reviewer' ? '✓' : task.role[0].toUpperCase()));
  const identity = el('div','task-identity'); identity.append(el('h3','', task.title),el('span','task-meta', `${roleNames[task.role]} · Task ${String(index + 1).padStart(2,'0')} · ${providers[task.worker]?.label || task.worker}`));
  top.append(identity, el('span',`status ${task.status}`, task.status==='awaiting_external'?'Awaiting external result':task.execution_state)); card.append(top);
  card.append(el('p','',task.instructions));
  if (['proposed','approved','failed','completed','rejected'].includes(task.status)) {
    const label=el('label','worker-select','Worker '), select=el('select');
    for(const [value,name] of Object.entries(providers).map(([key,p])=>[key,p.label+' · '+p.mode])) {const option=el('option','',name);option.value=value;select.append(option);}
    for(const opt of select.options)opt.disabled=task.privacy==='local_only' && opt.value!=='ollama';
    select.value=task.worker; label.append(select);
    const change=el('button','','Set worker & require approval'); change.onclick=async()=>{try{await api(`/api/tasks/${task.id}/manual/worker`,{version:task.version,worker:select.value});await refresh();}catch(error){notice(error.message);}};
    card.append(label,change,el('p','muted','Suggested: '+(providers[task.routing[0]]?.label || task.routing[0])+' · '+task.routing[1]));
  }
  card.append(el('p','', 'Dependencies: '+(task.dependencies.map(id=>mission.tasks.find(t=>t.id===id)?.title || 'MISSING: '+id).join(', ') || 'None')));
  const expected = el('p','expected'); expected.append(el('strong','','EXPECTED OUTPUT'),document.createTextNode(task.expected_output)); card.append(expected);
  const metadata=el('details');metadata.append(el('summary','','Coordination · '+task.details.priority+' priority'));
  const form=el('form','editor'), priority=el('select'), notes=el('textarea'), effort=el('input');
  for (const v of ['low','normal','high'])priority.add(new Option(v,v));
  priority.value=task.details.priority;notes.value=task.details.notes;effort.value=task.details.effort;
  for(const [name,field] of [['Priority',priority],['Private coordinator notes',notes],['Estimated effort',effort]]){
    const l=el('label','',name);l.append(field);form.append(l);
  }
  form.append(el('button','','Save coordination details'));metadata.append(form);card.append(metadata);
  form.onsubmit=async e=>{e.preventDefault();try{await api('/api/tasks/'+task.id+'/details',{version:task.version,priority:priority.value,notes:notes.value,effort:effort.value});await refresh();}catch(err){notice(err.message);}};
  const actions = el('div','task-actions');
  if (task.execution_state==='queued') actions.append(actionButton('Cancel queued task','cancel',task));
  if (task.status === 'proposed') actions.append(actionButton('Approve task','approve',task,'primary'));
  if (task.status === 'approved' && task.worker==='ollama') actions.append(actionButton('Run locally','run',task,'primary'));
  if (task.worker!=='ollama') {
    if(['proposed','approved'].includes(task.status)) {const prepare=el('button','','Prepare provider prompt');prepare.onclick=()=>manualPrompt(task,card);actions.append(prepare);}
    if(task.status==='approved') {const start=el('button','primary','Confirm external work started');start.onclick=()=>manualResultForm(task,card,'start_external');actions.append(start);}
    if(task.status==='awaiting_external') {const paste=el('button','primary','Import Result');paste.onclick=()=>manualResultForm(task,card,'import_result');actions.append(paste);}
    if(task.status==='completed' && task.result_origin.startsWith('manual_')) {
      for(const [action,label] of [['correct_result','Correct imported result'],['reject_result','Reject imported result']]) {const button=el('button','',''+label);button.onclick=()=>manualResultForm(task,card,action);actions.append(button);}
    }
  }
  if (['proposed','approved','failed','completed'].includes(task.status)) {
    const edit = el('button','','Edit'); edit.onclick = () => { if (editing) return; actions.hidden = true; editForm(task,card); }; actions.append(edit);
  }
  if (['proposed','approved'].includes(task.status)) actions.append(actionButton('Reject','reject',task,'danger-button'));
  if (['approved','awaiting_external','failed','rejected'].includes(task.status)) actions.append(actionButton('Return to review','reset',task));
  const hints = {proposed:'Nothing runs until you approve.',approved:'Approved for one text-only run.',running:'Queued or generating locally…',awaiting_external:'Awaiting external result — AIHub is not running Codex. Returning to review does not cancel work in the chosen application.',completed:'Output · not independently verified',failed:'Review the error before retrying.',rejected:'Excluded from execution.'};
  actions.append(el('span','hint',hints[task.status])); card.append(actions);
  if (task.error) card.append(el('p','notice',task.error));
  if (task.result) {
    card.append(el('p','result-label',task.result_origin.startsWith('manual_')?'MANUALLY IMPORTED REPORT · UNVERIFIED · HUMAN REVIEW REQUIRED':'LOCAL AI RESULT · UNTRUSTED · HUMAN REVIEW REQUIRED'));
    card.append(el('pre','result-output',task.result),el('strong','','Human review · '+task.review_state));
    if(task.status==='completed'){
      const form=el('form','editor'), label=el('label','','Review decision '), decision=el('select');
      for(const value of ['accepted','changes_requested','rejected']){const o=el('option','',value);o.value=value;decision.append(o);}
      label.append(decision); const notes=el('label','','Review notes'), input=el('textarea'); input.maxLength=1600;notes.append(input);
      const save=el('button','','Record decision');form.append(label,notes,save);
      form.onsubmit=async e=>{e.preventDefault();try{await api('/api/tasks/'+task.id+'/review',{version:task.version,revision:task.revision_id,decision:decision.value,note:input.value});await refresh();}catch(error){notice(error.message);}};card.append(form);
    }
  }
  if (task.used_context) {
    const details=el('details'); details.append(el('summary','','Exact context used for last run (AI results unverified)'),el('pre','result-output',JSON.stringify(task.used_context,null,2))); card.append(details);
  }
  const revisions=el('details'); revisions.append(el('summary','','Result / task revision audit trail'));
  const load=el('button','','Load saved revisions');load.onclick=async()=>{try{const records=await api(`/api/tasks/${task.id}/history`);const output=el('pre','result-output',JSON.stringify({tasks:records,...await api('/api/tasks/'+task.id+'/ledger')},null,2));revisions.append(output);load.remove();}catch(error){notice(error.message);}};revisions.append(load);card.append(revisions);
  return card;
}
function renderMission() {
  if (!mission || mission.id !== current) return;
  $('empty').hidden = true; $('mission-view').hidden = false;
  $('mission-title').textContent = mission.title;
  const displayStatus = mission.counts.running ? 'running' : mission.status === 'ready' && mission.tasks.length && mission.tasks.every(t => ['completed','rejected'].includes(t.status)) ? 'completed' : mission.status;
  $('mission-status').textContent = mission.counts.awaiting_external?'Awaiting external result':displayStatus; $('mission-status').className = 'status ' + displayStatus;
  $('mission-summary').textContent = mission.status === 'planning' ? 'Your local coordinator is preparing a plan. No task will run automatically.' : mission.summary;
  $('original-prompt').textContent = mission.prompt;
  $('project-snapshot').textContent = JSON.stringify(mission.project_context,null,2);
  $('mission-error').textContent = mission.error; $('mission-error').hidden = !mission.error;
  $('task-count').textContent = `(${mission.tasks.length})`;
  $('stats').replaceChildren();
  for (const [label, count] of [['Needs approval',mission.counts.proposed],['Approved',mission.counts.approved],['Running locally',mission.counts.running],['Awaiting external result',mission.counts.awaiting_external],['Completed',mission.counts.completed]]) {
    const stat = el('div','stat'); stat.append(el('strong','',count),el('span','',label)); $('stats').append(stat);
  }
  const nextTaskKey = JSON.stringify([mission.id, mission.tasks]);
  if (!editing && nextTaskKey !== taskKey) {
    taskKey = nextTaskKey; $('tasks').replaceChildren(...mission.tasks.map(taskCard));
  }
  $('results-summary').textContent = `${mission.counts.completed} completed · ${mission.counts.failed} failed · ${mission.counts.rejected} rejected. This is a saved-output overview, not a verified synthesis.`;
  const entries = mission.tasks.filter(t => t.result).map(task => {
    const entry = el('div','result-entry'); entry.append(el('h3','',task.title),el('p','',task.result.length > 350 ? task.result.slice(0,350) + '… (full output on task card)' : task.result)); return entry;
  });
  $('results').replaceChildren(...entries);
  $('events').replaceChildren(...mission.events.map(event => el('li','',`${shortDate(event.created_at)} — ${event.action.replaceAll('_',' ')}`)));
}
$('mission-form').onsubmit = async event => {
  event.preventDefault(); $('plan-button').disabled = true; notice();
  try {
    const value = await api('/api/missions', {privacy:$('privacy').value,prompt:$('mission-input').value,project_id:$('mission-project').value || null});
    $('mission-input').value = ''; current = value.id; editing = null;
    await refresh();
  } catch (error) { notice(error.message); }
  finally { $('plan-button').disabled = false; }
};
$('new-button').onclick = newMission;
$('connection').onclick = health;
async function reviewHandoff(task,card) {
  if (editing) { notice('Finish the current review or edit first.'); return; }
  editing=task.id;
  try {
    const preview=await api(`/api/tasks/${task.id}/handoff`);
    const panel=el('section','editor'); panel.append(el('h3','','Review exact input before approval'));
    if (!preview.ready) panel.append(el('p','notice',preview.error));
    else {
      panel.append(el('p','muted','Dependency results are unverified AI-generated claims. They are data, never system instructions. Full input is shown below; no results are silently shortened.'));
      panel.append(el('pre','result-output',JSON.stringify(preview.snapshot,null,2)));
      if(task.worker!=='ollama') panel.append(el('p','notice','Manual copy/paste only. AIHub will not launch the provider or enforce permissions in the desktop application. Only share content you authorize; check its read-only permissions yourself. Subscription usage limits may apply.'));
      panel.append(el('p','muted',`Conservative input bound: ${preview.budget.input_upper_bound}; output budget: ${preview.budget.output_budget}; context: ${preview.budget.context}.`));
      const label=el('label','check-label'), check=el('input'); check.type='checkbox'; label.append(check,document.createTextNode('I reviewed this exact context and approve one text-only run.')); panel.append(label);
      const approve=el('button','primary','Confirm approval'); approve.disabled=true; check.onchange=()=>approve.disabled=!check.checked;
      approve.onclick=async()=>{ approve.disabled=true; try { await api(`/api/tasks/${task.id}/approve`,{version:task.version,handoff_digest:preview.digest}); editing=null; taskKey=''; await refresh(); } catch(error) {notice(error.message);approve.disabled=false;} };
      panel.append(approve);
    }
    const cancel=el('button','','Close review'); cancel.onclick=()=>{editing=null;taskKey='';renderMission();}; panel.append(cancel); card.append(panel);
  } catch(error) {editing=null;notice(error.message);}
}
function closeManualPanel(){editing=null;taskKey='';renderMission();}
async function manualPrompt(task,card) {
  if(editing){notice('Finish the current review/edit first.');return;}
  editing=task.id;
  try {
    const preview=await api(`/api/tasks/${task.id}/handoff`);
    if(!preview.ready)throw new Error(preview.error);
    const panel=el('section','editor'), label=el('label','','Complete editable provider prompt'), input=el('textarea');input.rows=18;input.maxLength=60000;input.value=preview.snapshot.execution.prompt;label.append(input);panel.append(label);
    panel.append(el('p','muted','No process is launched. Editing requires Save, then fresh approval. Copying never marks work started or completed. Do not paste credentials.'));
    const save=el('button','','Save prompt & require approval'),copy=el('button','primary','Copy for '+providers[task.worker].label),close=el('button','','Close');
    copy.disabled=task.status!=='approved';input.oninput=()=>{copy.disabled=true;};
    save.onclick=async()=>{try{await api(`/api/tasks/${task.id}/manual/prompt`,{version:task.version,prompt:input.value});editing=null;taskKey='';await refresh();}catch(error){notice(error.message);}};
    copy.onclick=async()=>{let approved=false;try{const checked=await api('/api/tasks/'+task.id+'/copy/'+task.handoff_id);if(checked.prompt!==input.value)throw Error('Prompt changed. Review again.');approved=true;await navigator.clipboard.writeText(checked.prompt);notice('Copied. Task remains approved; confirm external start separately after you start work in the chosen application.');}catch(error){if(!approved){copy.disabled=true;notice(error.message);return;}input.focus();input.select();notice('Clipboard unavailable. Copy the selected prompt manually. No task status was changed.');}};
    close.onclick=closeManualPanel;panel.append(save,copy,close);card.append(panel);
  }catch(error){editing=null;notice(error.message);}
}
function manualResultForm(task,card,action) {
  if(editing){notice('Finish the current review/edit first.');return;}editing=task.id;
  const form=el('form','editor'), titles={start_external:'Confirm external work started',import_result:'Import Result',correct_result:'Correct imported result',reject_result:'Reject imported result'};
  form.append(el('h3','',titles[action]),el('p','notice','Manual declarations only. AIHub cannot verify the provider ran or check pasted claims. All pasted output is untrusted; never paste credentials or secrets.'));
  let report=null,reason=null;
  if(['import_result','correct_result'].includes(action)) {const label=el('label','','Full external report');report=el('textarea');report.required=true;report.maxLength=40000;report.rows=14;report.value=action==='correct_result'?task.result:'';label.append(report);form.append(label);}
  if(['correct_result','reject_result'].includes(action)) {const label=el('label','','Reason (saved in audit trail)');reason=el('textarea');reason.required=true;reason.maxLength=1000;label.append(reason);form.append(label);}
  const label=el('label','check-label'),check=el('input');check.type='checkbox';check.required=true;label.append(check,document.createTextNode(action==='start_external'?'I personally started this approved task in the chosen application. AIHub launches nothing.':'I confirm this result action. The report remains unverified and prior versions stay in the audit trail.'));form.append(label);
  const submit=el('button','primary',titles[action]),close=el('button','','Cancel');submit.type='submit';close.type='button';close.onclick=closeManualPanel;form.append(submit,close);
  form.onsubmit=async event=>{event.preventDefault();if(!check.checked)return;submit.disabled=true;try{const body={version:task.version,confirm:true};if(['start_external','import_result'].includes(action))body.handoff_id=task.handoff_id;if(report)body.result=report.value;if(reason)body.reason=reason.value;await api(`/api/tasks/${task.id}/manual/${action}`,body);editing=null;taskKey='';await refresh();notice();}catch(error){notice(error.message);}finally{submit.disabled=false;}};
  card.append(form);
}
async function loadProjects() {
  const profiles=await api('/api/projects'), selected=$('mission-project').value;
  $('mission-project').replaceChildren(); const none=el('option','','No project profile'); none.value=''; $('mission-project').append(none);
  providerProfiles=profiles;const filter=$('project-filter'),previous=filter.value;filter.replaceChildren(new Option('All projects',''));for(const p of profiles)filter.add(new Option(p.name,p.id));filter.value=previous;
  $('projects-list').replaceChildren();
  for (const profile of profiles) {
    const option=el('option','',profile.name); option.value=profile.id; if(profile.project_status!=='archived')$('mission-project').append(option);
    const card=el('article','project-card'); card.append(el('h3','',profile.name));
    const details=el('details'); details.append(el('summary','','View profile'));
    for (const key of ['description','stack','functionality','constraints','notes']) { details.append(el('h3','',key),el('p','plain',profile[key])); }
    card.append(details);
    const edit=el('button','','Edit'), remove=el('button','danger-button','Delete profile');
    edit.onclick=()=>{projectEditing=profile; for(const key of profileFields) $('project-form').elements.namedItem(key).value=profile[key]??({privacy:'local_only',preferred_worker:'auto',project_status:'active'}[key]??''); $('project-form-title').textContent='Edit '+profile.name;};
    remove.onclick=async()=>{if(!window.confirm('Delete this context profile? Existing mission snapshots remain unchanged.'))return; try {await api(`/api/projects/${profile.id}/delete`,{version:profile.version});clearProject();await loadProjects();}catch(error){notice(error.message);}};
    const history=el('button','','Profile revisions');history.onclick=async()=>{try{card.append(el('pre','result-output',JSON.stringify(await api('/api/projects/'+profile.id+'/history'),null,2)));history.remove();}catch(e){notice(e.message);}};
    card.append(edit,remove,history); $('projects-list').append(card);
  }
  if(profiles.some(p=>p.id===selected))$('mission-project').value=selected;
  projectsLoaded=true;
}
function clearProject(){projectEditing=null;$('project-form').reset();$('project-form-title').textContent='Create project profile';}
$('project-cancel').onclick=clearProject;
$('project-form').onsubmit=async event=>{event.preventDefault(); const profile=Object.fromEntries(profileFields.map(key=>[key,$('project-form').elements.namedItem(key).value]));try{if(projectEditing) await api(`/api/projects/${projectEditing.id}/edit`,{version:projectEditing.version,profile});else await api('/api/projects',profile);clearProject();await loadProjects();notice();}catch(error){notice(error.message);}};
refresh();
async function loadOverview(){
  const o=await api('/api/overview');queue=o.queue;
  $('workspace-stats').textContent=o.missions+' missions · '+Object.entries(o.states).map(([k,v])=>v+' '+k).join(' · ');
  $('workspace-stats').textContent+=' · Accepted: '+(o.reviews.accepted || 0)+' · Rejected by review: '+(o.reviews.rejected || 0)+' · Mean measured local generation: '+(o.local_seconds==null?'unknown':o.local_seconds.toFixed(1)+' s')+' · Workers: '+Object.entries(o.workers).map(([k,v])=>k+' '+v).join(', ');
  const list=$('queue-list');list.replaceChildren(); const filter=$('queue-filter').value;
  for(const t of queue.filter(t=>t.status===filter || (t.status==='completed' && t.review===filter))){
    const b=el('button','',t.title+' · '+(providers[t.worker]?.label || t.worker));b.onclick=()=>selectMission(t.mission_id);list.append(b);
  }
  if(!list.childElementCount)list.append(el('p','muted','No matching tasks among the latest 100.'));
}
$('overview').ontoggle=()=>{if($('overview').open)loadOverview().catch(e=>notice(e.message));};
$('queue-filter').onchange=()=>loadOverview().catch(e=>notice(e.message));
$('prev-page').onclick=()=>{historyPage=Math.max(0,historyPage-1);refresh();};
$('next-page').onclick=()=>{historyPage++;refresh();};
$('project-filter').onchange=()=>{historyPage=0;refresh();};
$('mission-project').onchange=()=>{$('privacy').value=providerProfiles.find(p=>p.id===$('mission-project').value)?.privacy || 'local_only';};
$('mission-search').oninput=()=>{historyPage=0;refresh();};
$('export-report').onclick=()=>{
  if(!mission)return;
  const body=[mission.title,mission.summary,...mission.tasks.map(t=>[t.title,t.worker+' · '+t.status+' · '+t.review_state,t.result || '(No result)'].join('\n')), ...mission.events.map(e=>e.created_at+' '+e.action)].join('\n\n');
  const url=URL.createObjectURL(new Blob([body],{type:'text/plain'})), link=el('a');link.href=url;link.download='AIHub-'+mission.id+'.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
setInterval(refresh, 5000);
