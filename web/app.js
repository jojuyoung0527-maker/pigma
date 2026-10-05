'use strict';
const $ = (s) => document.querySelector(s);
const state = { token: '', items: [], view: 'all', requestId: null, auto: new Set(), delivered: new Set(), polling: false };
const ACTIVE = new Set(['queued','resolving','downloading','merging','verifying','cancelling']);
const labels = {queued:'대기 중',resolving:'영상 정보 확인 중',downloading:'다운로드 중',merging:'영상·음원 결합 중',verifying:'파일 확인 중',cancelling:'취소 중',cancelled:'취소됨',failed:'다운로드 실패',completed:'다운로드 완료'};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const bytes = (n) => typeof n !== 'number' ? '—' : n >= 1024**3 ? `${(n/1024**3).toFixed(2)} GB` : n >= 1024**2 ? `${(n/1024**2).toFixed(1)} MB` : `${Math.round(n/1024)} KB`;
const platform = (name = '') => ({XiaoHongShu:'샤오홍슈',BiliBili:'빌리빌리',Douyin:'더우인',Weibo:'웨이보',Ixigua:'시과'}[name] || name.replace(/IE$/, '') || '링크 확인 중');
function notice(message, bad=false) { $('#message').textContent = message; $('#message').classList.toggle('error',bad); }
async function api(path, options={}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type':'application/json', 'X-SourceFlow-Token':state.token, ...options.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || '요청을 처리하지 못했습니다.');
  return data;
}
async function cookieText() {
  const file = $('#cookies').files[0];
  if (!file) return undefined;
  if (file.size > 1024**2) throw new Error('cookies.txt는 1MB 이하여야 합니다.');
  return await file.text();
}
function save(item) {
  if (!item.fileReady || !/^\/v1\/downloads\/[a-f0-9]{32}\/file$/.test(item.fileUrl || '')) return;
  const a = document.createElement('a'); a.href = item.fileUrl; a.download = item.result?.filename || 'SourceFlow-video';
  document.body.append(a); a.click(); a.remove();
}
function render() {
  $('#all-count').textContent = state.items.length;
  $('#active-count').textContent = state.items.filter(x=>ACTIVE.has(x.status)).length;
  $('#completed-count').textContent = state.items.filter(x=>x.status==='completed').length;
  const query = $('#search').value.trim().toLowerCase();
  const items = state.items.filter(x => (state.view==='all'||(state.view==='active'?ACTIVE.has(x.status):x.status==='completed')) && `${x.title} ${x.platform} ${platform(x.platform)} ${x.url}`.toLowerCase().includes(query));
  $('#visible-count').textContent = items.length;
  $('#list-title').firstChild.textContent = ({all:'다운로드 목록 ',active:'진행 중 ',completed:'완료된 다운로드 '}[state.view]);
  $('#empty').hidden = !!items.length;
  $('#empty h3').textContent = query ? '검색 결과가 없습니다.' : state.items.length ? '여기에 표시할 작업이 없습니다.' : '첫 번째 영상을 가져와 보세요.';
  $('#downloads').innerHTML = items.map(item => {
    const result = item.result || {}, active = ACTIVE.has(item.status), pct = typeof item.percent==='number'?Math.max(0,Math.min(100,item.percent)):null;
    const quality = result.width && result.height ? `${result.width} × ${result.height}` : '';
    const detail = item.status==='completed' ? [quality,result.format,bytes(result.bytes),result.hasAudio===false?'무음 영상':''].filter(Boolean).join(' · ') : [bytes(item.received),item.total?'/ '+bytes(item.total):'',item.speed?bytes(item.speed)+'/s':'',item.eta!=null?Math.ceil(item.eta)+'초 남음':''].filter(Boolean).join(' ');
    return `<article class="download-card ${esc(item.status)}" data-id="${item.id}"><div class="video-symbol">${item.status==='completed'?'✓':item.status==='failed'?'!':'↓'}</div><div class="card-body"><div class="card-meta"><span class="platform-tag">${esc(platform(item.platform))}</span><span class="status">${esc(labels[item.status]||item.status)}</span></div><h3>${esc(item.title)}</h3><p class="url">${esc(item.url)}</p>${active?`<progress max="100" ${pct!==null?`value="${pct}"`:''} aria-label="다운로드 진행률"></progress>`:''}<p class="details">${esc(detail)}</p>${item.error?.message?`<p class="error-text">${esc(item.error.message)}</p>`:''}${item.status==='completed'&&!item.fileReady?'<p class="error-text">보관된 파일이 없습니다.</p>':''}</div><div class="card-actions">${item.fileReady?'<button class="save-button" data-action="save">영상 저장 ↓</button>':''}${active?`<button data-action="cancel" ${item.status==='cancelling'?'disabled':''}>취소</button>`:''}${['failed','cancelled'].includes(item.status)?'<button data-action="retry">다시 시도</button>':''}<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">원래 페이지 ↗</a>${!active?'<button class="delete-button" data-action="delete">기록·보관 파일 삭제</button>':''}</div></article>`;
  }).join('');
}
async function refresh() {
  if (state.polling) return;
  state.polling = true;
  try {
    const data = await api('/v1/downloads'); state.items=data.downloads; render();
    $('#space').textContent = `남은 저장 공간 ${bytes(data.freeBytes)}`;
    for (const item of state.items) if (item.fileReady && state.auto.has(item.id) && !state.delivered.has(item.id)) {
      state.delivered.add(item.id); save(item); notice('다운로드가 완료됐습니다. 저장 창이 열리지 않으면 목록의 ‘영상 저장’을 눌러 주세요.');
    }
  } catch (e) { notice(e.message || '프로그램 연결을 확인해 주세요.',true); }
  finally { state.polling=false; }
}
$('#start').addEventListener('click', async () => {
  const text=$('#links').value.trim(); if (!text) { notice('영상 링크를 먼저 넣어 주세요.',true); $('#links').focus(); return; }
  $('#start').disabled=true; notice('다운로드 대기열에 추가하는 중…');
  state.requestId ||= crypto.randomUUID();
  try {
    const cookie_text=await cookieText();
    const data=await api('/v1/downloads',{method:'POST',body:JSON.stringify({text,request_id:state.requestId,...(cookie_text===undefined?{}:{cookie_text})})});
    if (data.downloads.length===1 && $('#autosave').checked) state.auto.add(data.downloads[0].id);
    $('#links').value=''; state.requestId=null;
    notice(`${data.downloads.length}개의 다운로드를 추가했습니다.`); await refresh();
  } catch(e) {notice(e.message,true);} finally {$('#start').disabled=false;}
});
$('#links').addEventListener('input',()=>{state.requestId=null;});
$('#paste').addEventListener('click',async()=>{try {$('#links').value=await navigator.clipboard.readText();state.requestId=null;$('#links').focus();notice('공유 링크를 붙여 넣었습니다.');}catch{notice('입력칸에서 Ctrl+V로 붙여 넣어 주세요.');$('#links').focus();}});
$('#clear-cookie').addEventListener('click',()=>{$('#cookies').value='';notice('선택한 로그인 파일을 해제했습니다.');});
$('#downloads').addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action]');if(!button)return;
  const item=state.items.find(x=>x.id===button.closest('[data-id]').dataset.id);if(!item)return;
  const action=button.dataset.action;
  if(action==='save'){save(item);return;}
  if(action==='delete'&&!confirm('이 작업의 기록과 앱에 보관된 영상 파일을 삭제할까요? 따로 저장한 파일은 유지됩니다.'))return;
  button.disabled=true;
  try {
    if(action==='delete')await api(`/v1/downloads/${item.id}`,{method:'DELETE'});
    else { const cookie_text=action==='retry'?await cookieText():undefined;
      await api(`/v1/downloads/${item.id}/${action}`,{method:'POST',body:JSON.stringify(cookie_text===undefined?{}:{cookie_text})});
    }
    await refresh();
  }catch(e){notice(e.message,true);button.disabled=false;}
});
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{state.view=button.dataset.view;document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x===button));render();});
$('#search').addEventListener('input',render);
$('#support-open').addEventListener('click',()=>$('#support').showModal());
$('#support-close').addEventListener('click',()=>$('#support').close());
(async()=>{try{const boot=await api('/v1/bootstrap');state.token=boot.token;const health=await api('/v1/health');$('#engine').textContent=`SourceFlow ${health.version}`;await refresh();setInterval(refresh,1300);}catch(e){notice('프로그램을 연결하지 못했습니다. SourceFlow를 다시 실행해 주세요.',true);$('#start').disabled=true;}})();
