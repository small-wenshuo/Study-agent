/* V4 论文学习界面：开关显式控制，恢复页面仅查询已有状态。 */
const paperUI = (() => {
  let mode = false, revision = 0, studyId = null, snapshotId = null;
  let currentArtifact = null, currentNote = null, currentOutline = null, stream = null;
  let pdfFiles = [], operation = false, unsaved = false;
  const toolbar = document.createElement('section');
  const label = document.createElement('label'); label.className = 'option';
  const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.id = 'paper-mode';
  label.append(toggle, document.createTextNode('论文模式（默认关闭）'));
  const picker = document.createElement('select'); picker.id = 'paper-file'; picker.setAttribute('aria-label', '选择论文');
  const hint = document.createElement('p'); hint.className = 'hint';
  hint.textContent = '开启并选择论文后生成导读；深入解释、笔记与 PPT 按需操作。';
  toolbar.append(label, picker, hint);
  document.querySelector('#files').before(toolbar);
  let panel = null, content = null, timeline = null, controls = null, editor = null;
  const json = (url, body, method = 'POST') => jsonRequest(url, {method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const context = () => ({session_id: sessionId, paper_mode_revision: revision});

  function button(text, action, parent) {
    // 所有标签使用 textContent；模型文本不能作为 HTML 或脚本。
    const b = document.createElement('button'); b.type = 'button'; b.textContent = text;
    b.onclick = () => Promise.resolve(action()).catch(error => alert(error.message));
    parent.append(b); return b;
  }
  function ensurePanel() {
    if (panel?.isConnected) return;
    panel = document.createElement('section'); panel.className = 'paper-panel'; panel.id = 'paper-panel';
    const heading = document.createElement('h2'); heading.textContent = '论文学习';
    controls = document.createElement('div'); controls.className = 'toolbar';
    content = document.createElement('div'); content.id = 'paper-content';
    const details = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = '执行过程（可回看）';
    timeline = document.createElement('ol'); timeline.id = 'paper-timeline';
    details.append(summary, timeline); panel.append(heading, controls, content, details); messages.prepend(panel);
  }
  function resetStream() {stream?.close(); stream = null;}
  function discardEdits() {
    // 切换笔记、产物或大纲前提醒，后台结果不应静默覆盖用户编辑。
    if (unsaved && !confirm('编辑尚未保存，继续切换？')) return false;
    unsaved = false; return true;
  }
  function clearContent() {ensurePanel(); content.replaceChildren(); currentArtifact = null; currentNote = null; currentOutline = null;}
  function updatePicker() {
    const old = picker.value; picker.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择一篇文本型 PDF'; picker.append(empty);
    for (const file of pdfFiles) {const o = document.createElement('option'); o.value = file.id; o.textContent = `${file.filename} · ${file.status}`; o.disabled = file.status !== 'ready'; picker.append(o);}
    picker.value = old; picker.disabled = !mode || operation; toggle.checked = mode;
  }
  async function filesChanged(list) {
    // 上传/切换知识库只更新可选项，不自动创建学习记录或模型任务。
    pdfFiles = list.filter(f => f.file_type === 'pdf'); updatePicker();
    if (studyId) {try {const s = await jsonRequest(`/api/paper-studies/${studyId}`); if (s.knowledge_base_id === kbSelect.value) picker.value = s.file_id;} catch {}}
  }
  async function change(enabled, selected = null) {
    if (busy || operation || !sessionId) return;
    if (unsaved && !confirm('笔记编辑尚未保存，继续切换？')) {toggle.checked = mode; return;}
    operation = true; toggle.disabled = true; picker.disabled = true;
    ensurePanel();controls.inert=true;content.inert=true;
    try {
      let target = selected;
      if (enabled && picker.value && !target) {
        target = (await json('/api/paper-studies', {knowledge_base_id: kbSelect.value, file_id: picker.value})).id;
      }
      const state = await json(`/api/sessions/${sessionId}/paper-mode`, {enabled, study_id: target, expected_revision: revision}, 'PATCH');
      mode = state.enabled; studyId = state.study_id; revision = state.revision; unsaved = false;
      resetStream(); clearContent(); timeline.replaceChildren();
      hint.textContent = mode ? (studyId ? '已选择论文，导读之外的操作按需触发。' : '请选择论文，尚未开始全文阅读。') : '普通问答模式，未启动论文学习。';
      if (studyId) await loadStudy();
      if (state.artifact_id) await showArtifact(state.artifact_id);
      if (state.task_id) watch(state.task_id);
    } catch (error) {toggle.checked = mode; alert(error.message); await restore(await jsonRequest(`/api/sessions/${sessionId}`));}
    finally {operation = false; toggle.disabled = false; controls.inert=false;content.inert=false;updatePicker();}
  }
  async function restore(session) {
    // 会话恢复不调用开启接口，不主动重试失败任务。
    resetStream(); mode = !!session.paper_mode; revision = session.paper_mode_revision || 0;
    studyId = session.paper_study_id || null; snapshotId = null; unsaved = false;
    clearContent(); timeline.replaceChildren(); controls.replaceChildren(); updatePicker();
    hint.textContent = mode ? '论文模式已恢复；不会重复生成导读。' : '普通问答模式，论文流程未启用。';
    if (studyId) {try {await loadStudy();} catch (error) {hint.textContent = error.message;}}
  }
  async function loadStudy() {
    if (!studyId) return;
    const s = await jsonRequest(`/api/paper-studies/${studyId}`); snapshotId = s.active_snapshot_id;
    if (s.knowledge_base_id === kbSelect.value) picker.value = s.file_id;
    controls.replaceChildren();
    const choice = document.createElement('select'); choice.setAttribute('aria-label', '历史学习产物');
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '查看已有导读、解释、自测或 PPT'; choice.append(placeholder);
    const names = {overview:'导读', explanation:'解释', self_test:'自测', presentation_outline:'PPT 大纲', presentation:'PPTX'};
    for (const a of s.artifacts) {const o = document.createElement('option'); o.value = a.id; o.textContent = `${names[a.kind] || a.kind} · ${a.created_at.slice(0,16)}`; choice.append(o);}
    choice.onchange = () => choice.value && showArtifact(choice.value).catch(e => alert(e.message)); controls.append(choice);
    const taskChoice = document.createElement('select'); taskChoice.setAttribute('aria-label', '历史学习任务');
    const first = document.createElement('option'); first.value=''; first.textContent='回看执行过程'; taskChoice.append(first);
    for (const t of s.tasks) {const o=document.createElement('option');o.value=t.id;o.textContent=`${names[t.kind]||t.kind} · ${t.status}`;taskChoice.append(o);}
    taskChoice.onchange=()=>taskChoice.value&&watch(taskChoice.value); controls.append(taskChoice);
    button('重新生成导读', async () => {const t = await json(`/api/paper-studies/${studyId}/overview`, {...context(), force:true}); watch(t.id);}, controls).disabled = !mode;
    if (s.notes.length) {
      const notes = document.createElement('select'); notes.setAttribute('aria-label', '已保存笔记');
      const e = document.createElement('option'); e.textContent = '打开已保存笔记'; e.value = ''; notes.append(e);
      for (const n of s.notes) {const o=document.createElement('option');o.value=n.id;o.textContent=`${n.title} · v${n.revision}`;notes.append(o);}
      notes.onchange=()=>notes.value&&showNote(notes.value).catch(e=>alert(e.message));controls.append(notes);
    }
    const active = s.tasks.find(t => ['queued','running'].includes(t.status) && t.session_id === sessionId);
    if (active) watch(active.id);
    else if (!currentArtifact && !currentNote && s.artifacts.length) {
      const latest = s.artifacts.find(a => a.kind === 'overview' && a.snapshot_id === snapshotId);
      if (latest) await showArtifact(latest.id);
    }
  }
  function eventItem(item) {const li=document.createElement('li');li.textContent=`${item.created_at.slice(11,19)} · ${item.message}`;timeline.append(li);}
  function watch(taskId) {
    // EventSource 断线自动按事件 ID 回放；去重保证时间线无重复项。
    resetStream(); ensurePanel(); timeline.replaceChildren(); let seen = 0; const viewedSession=sessionId;
    const viewedStudy=studyId;
    stream = new EventSource(`/api/tasks/${taskId}/stream`);
    stream.addEventListener('progress', e => {if(viewedSession!==sessionId||viewedStudy!==studyId)return;const item=JSON.parse(e.data);if(item.seq>seen){seen=item.seq;eventItem(item);}});
    stream.addEventListener('done', async e => {
      if(viewedSession!==sessionId||viewedStudy!==studyId)return;resetStream();const task=JSON.parse(e.data);
      if(task.result_id) await showArtifact(task.result_id); else {const p=document.createElement('p');p.textContent=task.message||task.status;content.append(p);}
      if(['failed','partial','cancelled','interrupted'].includes(task.status)) button('主动重试任务',async()=>{const t=await json(`/api/tasks/${taskId}/retry`,context());watch(t.id);},content).disabled=!mode;
      if(studyId) await loadStudy();
    });
    const cancel=button('取消当前学习任务',()=>json(`/api/tasks/${taskId}/cancel`,{}),controls);
    stream.addEventListener('done',()=>cancel.remove(),{once:true});
  }
  async function showArtifact(id) {
    if (!discardEdits()) return;
    const a=await jsonRequest(`/api/artifacts/${id}`); ensurePanel(); content.replaceChildren();currentArtifact=a;currentNote=null;
    if(a.kind==='presentation_outline'){showOutline(a);return;}
    if(a.kind==='presentation'){
      const p=document.createElement('p');p.textContent=`已生成 ${a.page_count} 页。${a.limitations.join(' ')}`;content.append(p);
      const link=document.createElement('a');link.href=`/api/artifacts/${a.id}/download`;link.textContent='下载 PPTX';content.append(link);return;
    }
    if(a.coverage){const p=document.createElement('p');p.className='hint';p.textContent=`覆盖：${a.coverage.status} · 已处理页 ${a.coverage.processed_pages.join(', ')} · 未覆盖页 ${a.coverage.missing_pages.join(', ')}。${a.coverage.limitations.join(' ')}`;content.append(p);}
    for(const block of a.blocks){
      const article=document.createElement('article'); article.className='paper-block';
      const h=document.createElement('h3');h.textContent=block.heading;
      const tag=document.createElement('p');tag.className='hint';tag.textContent={paper_fact:'论文原文结论',teaching_explanation:'辅助解释',external_evidence:'外部补充'}[block.type];
      const p=document.createElement('p');p.textContent=block.text;p.style.whiteSpace='pre-wrap';article.append(h,tag,p);
      for(const eid of block.evidence_ids){const source=a.evidence.find(e=>e.id===eid);if(!source)continue;
        const d=document.createElement('details'),s=document.createElement('summary'),q=document.createElement('p');
        s.textContent=source.source_type==='paper'?`原文：PDF 第 ${source.page} 页`:'外部正文';q.textContent=source.quote;d.append(s,q);
        if(source.source_type==='web'){const link=document.createElement('a');if(/^https?:\/\//.test(source.url)){link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=source.title;d.append(link);}}
        article.append(d);
      }
      for(const style of ['解释概念','拆解公式','举例说明'])button(style,async()=>{
        const question=prompt('希望深入理解什么？',`${style}：${block.heading}`);if(!question?.trim())return;
        const t=await json(`/api/paper-studies/${studyId}/explanations`,{...context(),question,style,target_block_id:`${a.id}:${block.id}`,snapshot_id:a.snapshot_id,allow_web:document.querySelector('#allow-web').checked});watch(t.id);
      },article).disabled=!mode;
      content.append(article);
    }
    for(const q of a.questions||[]){const d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('p');s.textContent=q.question;p.textContent=`提示：${q.hint}\n答案：${q.answer}`;p.style.whiteSpace='pre-wrap';d.append(s,p);content.append(d);}
    button('将这份内容加入笔记',async()=>{const title=prompt('笔记标题','论文学习笔记');if(!title?.trim())return;const note=await json(`/api/paper-studies/${studyId}/notes`,{title,artifact_ids:[a.id]});await showNote(note.id);await loadStudy();},content);
    button('针对这份内容自测',async()=>{const t=await json(`/api/paper-studies/${studyId}/self-tests`,{...context(),artifact_ids:[a.id]});watch(t.id);},content).disabled=!mode;
  }
  async function showNote(id) {
    if (!discardEdits()) return;
    const note=await jsonRequest(`/api/notes/${id}`);ensurePanel();content.replaceChildren();currentNote=note;currentArtifact=null;editor=[];
    const heading=document.createElement('h3');heading.textContent=`${note.title} · v${note.revision}`;content.append(heading);
    for(const block of note.blocks){const tag=document.createElement('p');tag.className='hint';tag.textContent=block.type==='user_note'?'用户编辑':block.heading;
      const area=document.createElement('textarea');area.value=block.text;area.maxLength=12000;area.style.width='100%';area.oninput=()=>unsaved=true;editor.push(area);content.append(tag,area);}
    button('保存编辑',async()=>{const updated=await json(`/api/notes/${id}`,{title:note.title,expected_revision:note.revision,texts:editor.map(e=>e.value)},'PUT');unsaved=false;await showNote(updated.id);},content);
    const link=document.createElement('a');link.href=`/api/notes/${id}/export?revision=${note.revision}`;link.textContent='下载 Markdown';content.append(link);
    button('生成可编辑 PPT 大纲',async()=>{const page_count=Number(prompt('希望生成几页？（3～30）','10'));if(!Number.isInteger(page_count)||page_count<3||page_count>30)return;
      const t=await json(`/api/notes/${id}/presentation-outline`,{...context(),note_revision:note.revision,page_count});watch(t.id);},content).disabled=!mode;
  }
  function showOutline(a){
    currentOutline=a;const h=document.createElement('h3');h.textContent=`PPT 大纲 v${a.revision} · 来源笔记 v${a.note_revision}`;content.append(h);
    const fields=[];
    for(const slide of a.slides){const section=document.createElement('section'),title=document.createElement('input'),bullets=document.createElement('textarea'),notes=document.createElement('textarea');
      title.value=slide.title;title.maxLength=50;title.setAttribute('aria-label','幻灯片标题');bullets.value=slide.bullets.join('\n');bullets.setAttribute('aria-label','每行一条要点');notes.value=slide.notes;notes.setAttribute('aria-label','讲解备注');
      let table=null;if(slide.table){
        const grid=document.createElement('table');grid.style.width='100%';const cells=[];
        for(const row of [slide.table.headers,...slide.table.rows]){const tr=document.createElement('tr'),inputs=[];
          for(const value of row){const td=document.createElement('td'),field=document.createElement('input');field.value=value;field.maxLength=35;field.style.width='100%';field.setAttribute('aria-label','表格内容');field.oninput=()=>unsaved=true;td.append(field);tr.append(td);inputs.push(field);}grid.append(tr);cells.push(inputs);}
        table=cells;section.append(grid);
      }
      for(const e of [title,bullets,notes]){e.style.width='100%';e.oninput=()=>unsaved=true;section.append(e);}fields.push({title,bullets,notes,table,ids:slide.evidence_ids});content.append(section);}
    const save=async()=>{const updated=await json(`/api/presentation-outlines/${currentOutline.id}`,{expected_revision:currentOutline.revision,slides:fields.map(f=>({title:f.title.value,bullets:f.bullets.value.split('\n').map(x=>x.trim()).filter(Boolean),notes:f.notes.value,evidence_ids:f.ids,...(f.table?{table:{headers:f.table[0].map(e=>e.value),rows:f.table.slice(1).map(row=>row.map(e=>e.value))}}:{})}))},'PUT');currentOutline=updated;return updated;};
    button('保存大纲',async()=>{const updated=await save();unsaved=false;await showArtifact(updated.id);},content);
    button('确认大纲并生成 PPTX',async()=>{const updated=await save();unsaved=false;const t=await json(`/api/presentation-outlines/${updated.id}/render`,context());watch(t.id);},content).disabled=!mode;
  }
  async function submit(question){
    if(!mode)return false;
    if(!studyId){alert('请先选择论文。');return true;}
    if(!snapshotId){alert('论文导读尚未就绪，请稍后再追问。');return true;}
    const t=await json(`/api/paper-studies/${studyId}/explanations`,{...context(),question,allow_web:document.querySelector('#allow-web').checked});
    add('user',question);input.value='';watch(t.id);return true;
  }
  toggle.onchange=()=>change(toggle.checked,studyId);
  picker.onchange=()=>change(true);
  window.addEventListener('beforeunload',e=>{if(unsaved){e.preventDefault();e.returnValue='';}});
  updatePicker();
  return {restore,filesChanged,submit,canLeave:()=>!unsaved||confirm('笔记尚未保存，继续离开？')};
})();

// 脚本加载较晚时恢复已打开的会话，仍然只读查询。
if(sessionId){const openingSession=sessionId;jsonRequest(`/api/sessions/${openingSession}`).then(data=>{if(openingSession===sessionId)return paperUI.restore(data)}).catch(e=>alert(e.message));}
