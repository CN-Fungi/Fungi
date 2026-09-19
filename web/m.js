/* Fungi mobile web UI — mirrors desktop app.js contracts:
   turn entries model, /events tape reattach, ask-card re-mount discipline.
   Mobile-specific: right-swipe session drawer (2/3 width, GSAP drag), token gate. */
marked.setOptions({ breaks: true, gfm: true });
const FC = window.FungiCommon;
const msgs = document.getElementById('messages'), input = document.getElementById('input'),
  btn = document.getElementById('btn-send'), status = document.getElementById('status'),
  banner = document.getElementById('asks-banner');
let processing = false, currentSessionId = null, allSessions = [], sessionDirty = false;
let _liveCount = 0; // live-node count at last streaming render (animate only fresh nodes)
let rawMessages = [];

/* ---------- token gate ---------- */
const TOKEN = (() => {
  const q = new URLSearchParams(location.search).get('t');
  if (q) { try { localStorage.setItem('fungi-token', q); } catch (e) {} return q; }
  try { return localStorage.getItem('fungi-token') || ''; } catch (e) { return ''; }
})();
FC.initHttp({
  prefix: p => TOKEN ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN) : p,
  onUnauthorized: () => document.getElementById('rescan-overlay').classList.remove('hide'),
});
const api = FC.url, fetchJSON = FC.fetchJSON;

/* ---------- helpers ---------- */
function setCurrentSession(id) {
  currentSessionId = id;
  try { if (id) localStorage.setItem('fungi-session-m', id); else localStorage.removeItem('fungi-session-m'); } catch (e) {}
}
const escapeHtml = FC.escapeHtml;
/* #messages 一次只有一个主人（桌面同款，见 common.js 的 initPane）：所有重绘都经
   pane 写入，非主人的写入落空。S=会话，F=好友。手机端不在每次 append 时钉底，
   由各渲染收尾自己 stick（pinOnAdd:false）。 */
const pane = FC.initPane({
  msgs: () => msgs,
  tray: () => null,   // 手机端没有托盘（子代理气泡走 bottom sheet，自管自己的元素）
  isNearBottom,
  pinOnAdd: false,
});
const S = pane.of('session'), F = pane.of('friend');
function isNearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
function updateScrollBtn() {
  const b = document.getElementById('scroll-bottom');
  if (!b) return;
  if (isNearBottom(msgs)) b.classList.remove('visible'); else b.classList.add('visible');
}
document.getElementById('scroll-bottom').addEventListener('click', () => { msgs.scrollTop = msgs.scrollHeight; updateScrollBtn(); });
msgs.addEventListener('scroll', updateScrollBtn);
function motionOn() { return window.gsap && !matchMedia('(prefers-reduced-motion: reduce)').matches; }
function msgIn(node, kind) {
  if (!motionOn()) return;
  gsap.from(node, { opacity: 0, y: kind === 'user' ? 14 : 18, scale: 0.96, duration: 0.4, ease: 'power3.out', clearProps: 'all' });
}
const getSessionTitle = list => FC.getSessionTitle(list, 40, 38);

/* ---------- sessions ---------- */
let _sessionsSeq = 0;
async function loadSessions() {
  const seq = ++_sessionsSeq;
  try {
    const r = await fetchJSON('/sessions');
    if (!r.ok) throw new Error(r.status);
    const sessions = (await r.json()).sessions || [];
    if (seq !== _sessionsSeq) return;
    allSessions = sessions;
    renderSessionList();
  } catch (e) { if (e.message !== 'unauthorized') console.error('loadSessions:', e); }
}
async function reloadSessionFromServer() {
  if (!currentSessionId) return;
  try {
    const r = await fetchJSON('/session?id=' + encodeURIComponent(currentSessionId));
    if (!r.ok) return;
    const s = await r.json();
    rawMessages = s.messages || [];
    const stick = isNearBottom(msgs);
    renderMessages(s);
    if (stick && S.stick()) updateScrollBtn();
    loadSessions();
  } catch (e) {}
}
async function closeCurrentSession() {
  if (!currentSessionId) return;
  if (processing && turn && turn.sessionId === currentSessionId) {
    // A turn is running here: the server persists on its own — just detach.
    setCurrentSession(null);
    return;
  }
  if (!sessionDirty) { setCurrentSession(null); return; }
  const list = rawMessages;
  if (!list || list.length <= 1) {
    try { await fetch(api('/session?id=' + encodeURIComponent(currentSessionId)), { method: 'DELETE' }); } catch (e) {}
    setCurrentSession(null);
  } else {
    const title = getSessionTitle(list);
    try {
      await fetch(api('/save'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: currentSessionId, title, messages: list }) });
    } catch (e) {}
  }
  loadSessions();
}
async function switchSession(id) {
  if (id === currentSessionId && pane.is('session')) { closeDrawer(); return; }
  leaveFriendView();
  await closeCurrentSession();
  sessionDirty = false;
  try {
    const r = await fetchJSON('/session?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    setCurrentSession(s.id);
    rawMessages = s.messages || [];
    S.clearMsgs();
    renderMessages(s);
    document.getElementById('session-title').textContent = s.title || getSessionTitle(rawMessages) || 'Fungi';
    S.stick(); updateScrollBtn();
    renderSessionList(); loadSessions();
    reattachIfRunning(s.id);
  } catch (e) { window.__swErr = String((e && e.stack) || e); if (e.message !== 'unauthorized') console.error('switchSession:', e); }
  closeDrawer();
}
async function newSession() {
  // Focus any untouched "(new session)" instead of littering the list.
  const empty = allSessions.find(s => s.title === '(new session)' && (s.msgCount || 0) <= 1 && !s.running);
  leaveFriendView();
  if (empty) {
    if (empty.id !== currentSessionId) await switchSession(empty.id);
    closeDrawer();
    return;
  }
  await closeCurrentSession();
  try {
    const r = await fetchJSON('/new', { method: 'POST' });
    if (!r.ok) throw new Error(r.status);
    const { id } = await r.json();
    setCurrentSession(id); rawMessages = []; S.clearMsgs();
    document.getElementById('session-title').textContent = 'Fungi';
    sessionDirty = false;
    await loadSessions();
  } catch (e) { if (e.message !== 'unauthorized') console.error('newSession:', e); }
  closeDrawer();
}
async function deleteSession(id) {
  try {
    await fetch(api('/session?id=' + encodeURIComponent(id)), { method: 'DELETE' });
    if (id === currentSessionId) {
      setCurrentSession(null); S.clearMsgs();
      document.getElementById('session-title').textContent = 'Fungi';
    }
    document.getElementById('session-filter').value = '';
    await loadSessions();
  } catch (e) {}
}

/* ---------- confirm modal (shared impl in common.js, mobile wiring) ---------- */
FC.initConfirmModal({ title: '确认？', confirmText: '确定', cancelText: '取消' });
const showConfirm = FC.showConfirm, closeConfirm = FC.closeConfirm;

/* ---------- rename (desktop startRename/finishRename contract) ---------- */
function startRename(row, s) {
  const titleEl = row.querySelector('.session-row-title');
  const old = titleEl.textContent;
  const inp = document.createElement('input');
  inp.className = 'rename-input'; inp.value = s.title || old;
  inp.addEventListener('blur', () => finishRename(row, s, inp, old));
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') inp.blur();
    if (e.key === 'Escape') { row.replaceChild(titleEl, inp); titleEl.textContent = old; }
  });
  row.replaceChild(inp, titleEl); inp.focus(); inp.select();
}
async function finishRename(row, s, inp, old) {
  const newTitle = inp.value.trim();
  if (!newTitle || newTitle === old) { renderSessionList(); return; }
  try {
    await fetch(api('/save'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: s.id, title: newTitle, messages: [] }) });
    s.title = newTitle;
    if (s.id === currentSessionId) document.getElementById('session-title').textContent = newTitle;
  } catch (e) {}
  renderSessionList();
}

/* Drawer list: keyed rows survive re-render so GSAP stagger doesn't rebuild everything */
function renderSessionList() {
  const list = document.getElementById('session-list');
  const empty = document.getElementById('session-list-empty');
  const filter = (document.getElementById('session-filter').value || '').trim().toLowerCase();
  const filtered = filter ? allSessions.filter(s => (s.title || '').toLowerCase().includes(filter)) : allSessions;
  const fresh = [];
  list.querySelectorAll('.session-row').forEach(r => r.remove());
  filtered.forEach(s => {
    const row = document.createElement('div');
    row.dataset.sid = s.id;
    // 文件传输助手（§53/§54）：不是聊天会话，而是两台设备之间的搬运通道 ——
    // 常驻置顶、带图标、样式不同，而且**没有改名/删除两个按钮**（服务端也拒绝）。
    row.className = 'session-row' + (s.shuttle ? ' shuttle' : '')
      + (s.id === currentSessionId ? ' active' : '');
    if (s.shuttle) row.title = '文件传输助手：两台设备之间只搬文件，不会改名也不会被删除';
    row.innerHTML = (s.shuttle ? '<span class="session-row-icon">&#x1F4C1;</span>' : '')
      + '<span class="session-row-title">' + escapeHtml(s.title || 'Untitled') + '</span>'
      + '<span class="session-row-meta">' + (s.shuttle ? '只搬文件' : fmtDate(s.created))
      + (s.running ? ' \u25cf' : '') + '</span>'
      + (s.shuttle ? ''
        : '<button class="session-row-act ren" title="重命名">&#9998;</button>'
          + '<button class="session-row-act del" title="删除">&#10005;</button>');
    if (!s.shuttle) {
      row.querySelector('.session-row-act.ren').addEventListener('click', e => {
        e.stopPropagation(); startRename(row, s);
      });
      row.querySelector('.session-row-act.del').addEventListener('click', e => {
        e.stopPropagation();
        showConfirm({
          title: '删除会话',
          message: '「' + (s.title || 'Untitled') + '」将被永久删除，不可恢复。',
          confirmText: '删除',
          danger: true,
          onConfirm: () => deleteSession(s.id)
        });
      });
    }
    row.addEventListener('click', () => switchSession(s.id));
    list.appendChild(row);
    fresh.push(row);
  });
  if (filtered.length === 0) { empty.style.display = ''; empty.textContent = filter ? '没有匹配的会话。' : '还没有会话。'; }
  else empty.style.display = 'none';
  if (motionOn()) gsap.from(fresh, { opacity: 0, x: -26, duration: 0.35, stagger: 0.04, ease: 'power3.out', clearProps: 'all' });
}
const fmtDate = d => FC.fmtDate(d, 'zh-CN');

/* ---------- transcript render (full re-render = refresh-grade) ---------- */
function renderMessages(s) {
  // #messages 一次只有一个主人：好友视图开着时任何会话侧重绘都不许落笔
  // （桌面同款，2026-09-10 真机 bug：回合 done 把好友对话整屏换成会话）。
  if (!pane.is('session')) return; // 好友视图握着 #messages
  _liveCount = 0;
  // Full re-render must replace: renderTranscript only appends (b8e3b65 contract).
  S.clearMsgs();
  registerArchived(s.subagents); // spawn cards from past sessions stay clickable
  FC.renderTranscript(S, rawMessages, s.asks || [], renderOpts());
  if (turn && turn.sessionId === currentSessionId) {
    if (turn.userText && rawMessages.some(m => m.role === 'user' && m.content === turn.userText)) {
      turn.userRendered = true; // disk copy already has it; live bubble would double it
    }
    renderTurnLive();
  }
  placeAskCards();
}
/* 时间轴与转录渲染都在 common.js（FC.renderTranscript / FC.insertByTs）：m.js 与
   桌面是同一个渲染器，只是外观不同——两份拷贝正是好友视图的 bug 被修两遍的原因。 */
const renderOpts = extra => Object.assign({
  asks: { buildAnsweredAskCard },
  spawnLookup: callId => specByCall[callId] || archivedByCall[callId],
  argsMax: 60,                       // 手机端工具卡参数更短
  spawnTitle: '点按查看子代理详情',
  reasoningHtml: t => '<div>' + escapeHtml(t) + '</div>',
  liveText: r => { const text = FC.stripSilent(r.text); return text ? escapeHtml(text) : ''; },
  fileLink: path => pullFromPc(path),   // 手机：点路径就把文件取过来（§52）
}, extra || {});
/* 好友视图的两侧：对面在左（素底），我方在右——与桌面同一套 side 类
   （顶栏的会话视图不受影响，它本来就不分侧）。 */
const FRIEND_SIDE = { user: ' friend-peer', agent: ' friend-mine' };

/* ---------- send / stream ---------- */
let abortCtrl = null;
let stopRequested = false;
let stopTimer = null;
let turn = null; // {sessionId, entries, userText, userRendered, aborted}

function setBusy(busy, stopping) {
  btn.classList.toggle('stopping', !!stopping);
  // Empty input = retry the last turn (↻); typed text = send (↑); running = stop (■).
  const has = !!input.value.trim();
  btn.innerHTML = busy ? '&#x25A0;' : (has ? '&#x2191;' : '&#x21BB;');
  btn.disabled = false;
  btn.title = busy ? (stopping ? '再点一次强制断开' : '停止') : (has ? '发送' : '重试上一回合');
}
async function send() {
  if (processing && turn) { // running: button = stop (graceful, then hard)
    const sid = turn.sessionId || currentSessionId;
    if (!stopRequested) {
      stopRequested = true; setBusy(true, true); status.textContent = '正在停止…';
      if (sid) fetch(api('/stop'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: sid }) }).catch(() => {});
      stopTimer = setTimeout(() => { if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; } }, 15000);
    } else {
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      stopRequested = false;
      abortCtrl.abort(); abortCtrl = null;
      status.textContent = '已断开。';
    }
    return;
  }
  const text = input.value.trim();
  if (!text) return retryTurn(); // empty box: the button is the retry icon
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  setBusy(true);
  if (!currentSessionId) {
    try {
      const r = await fetchJSON('/new', { method: 'POST', signal: abortCtrl.signal });
      const { id } = await r.json();
      setCurrentSession(id); loadSessions();
    } catch (e) { if (e.message !== 'unauthorized') console.error(e); }
  }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] };
  rawMessages.push({ role: 'user', content: text });
  sessionDirty = true;
  turn.userText = text;
  input.value = ''; autoGrow(); status.textContent = 'Thinking...';
  _liveCount = 0;
  renderTurnLive();
  await pumpStream(api('/chat'), { message: text, sessionId: sid });
}
/* Empty send button: rerun the failed/stopped turn with no new prompt —
   the desktop Alt+R contract (POST /retry, server strips the error tail). */
async function retryTurn() {
  if (processing || !currentSessionId) return;
  if (!rawMessages.some(m => m.role !== 'system')) return;
  const sid = currentSessionId;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  turn = { sessionId: sid, entries: [] };
  sessionDirty = true;
  setBusy(true);
  status.textContent = '重试中...';
  _liveCount = 0;
  renderTurnLive();
  await pumpStream(api('/retry'), { sessionId: sid });
}

/* Background subagent(s) finished: re-activate the session with a resume
   turn whose server-injected input is their reports (desktop contract). */
async function resumeIfPending() {
  if (processing && turn) return;
  if (!currentSessionId) return;
  try {
    const r = await fetchJSON('/spawn-pending?sessionId=' + encodeURIComponent(currentSessionId));
    const d = await r.json();
    if (!d.pending) return;
    processing = true;
    abortCtrl = new AbortController(); stopRequested = false;
    turn = { sessionId: currentSessionId, entries: [] };
    sessionDirty = true;
    status.textContent = 'Background task finished - continuing...';
    _liveCount = 0;
    renderTurnLive();
    await pumpStream(api('/resume'), { sessionId: currentSessionId });
  } catch (e) { if (e.message !== 'unauthorized') console.error(e); }
}
/* A session's turn may still run server-side (reload / switch): /events
   replays the tape, then streams live. */
function reattachIfRunning(sid) {
  if (processing || turn || !sid) return;
  const s = allSessions.find(x => x.id === sid);
  if (!s || !s.running) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  turn = { sessionId: sid, entries: [] };
  setBusy(true);
  status.textContent = '回合仍在服务器上运行，正在接续…';
  _liveCount = 0;
  renderTurnLive();
  pumpStream(api('/events?sessionId=' + encodeURIComponent(sid)), null, 'GET');
}
/* A stream that dies without a done event (user hard-abort, network drop,
   server crash) leaves live-node cards whose content never reached a disk-
   backed render. Reconcile with the persisted transcript immediately, then
   resume streaming if the turn still runs server-side — otherwise the cards
   vanish at the next renderTurnLive (new send / reattach), with no done ever
   arriving to reload them. */
async function recoverAfterDrop(sid) {
  if (!sid) return;
  // 好友视图开着时不抢 #messages：会话侧的补画留给离开好友视图时做。
  if (pane.is('session')) await reloadSessionFromServer();
  if (turn || processing) return; // user already started something else
  await loadSessions();           // fresh s.running for the reattach check
  reattachIfRunning(sid);
}
async function pumpStream(url, body, method = 'POST') {
  const sid = turn ? turn.sessionId : null; // the stream may die mid-turn
  try {
    const opts = { method, signal: abortCtrl.signal };
    if (body !== null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (resp.status === 403) { document.getElementById('rescan-overlay').classList.remove('hide'); turn = null; processing = false; setBusy(false); return; }
    if (!resp.ok) { status.textContent = '错误：HTTP ' + resp.status; turn = null; }
    else {
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let leftover = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        leftover += dec.decode(value, { stream: true });
        const lines = leftover.split('\n');
        leftover = lines.pop() || '';
        for (const line of lines) {
          if (!line) continue;
          let obj;
          try { obj = JSON.parse(line); } catch (e) { continue; }
          handleTurnEvent(obj);
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') status.textContent = '已中止。';
    else status.textContent = '错误：' + e.message;
    turn = null;
    recoverAfterDrop(sid);
  }
  if (turn) {
    // Stream ended without a done event: never leave a phantom live turn.
    status.textContent = '连接中断。';
    turn = null;
    recoverAfterDrop(sid);
  }
  processing = false; setBusy(false);
}

/* Turn events update the model first, then touch the DOM only when the
   turn's session is on screen (desktop contract, verbatim). */
function handleTurnEvent(obj) {
  const t = turn;
  if (!t) return;
  const visible = currentSessionId === t.sessionId && pane.is('session');
  const stick = isNearBottom(msgs); // measure before the event mutates the DOM
  switch (obj.type) {
    case 'text': {
      const last = t.entries[t.entries.length - 1];
      if (last && last.kind === 'text') last.content += obj.content;
      else t.entries.push({ kind: 'text', content: obj.content });
      status.textContent = 'Writing...';
      if (visible) updateLastText();
      break;
    }
    case 'reasoning_start':
      t.entries.push({ kind: 'reasoning', content: '', closed: false });
      if (visible) renderTurnLive();
      break;
    case 'reasoning': {
      const last = t.entries[t.entries.length - 1];
      if (last && last.kind === 'reasoning') last.content += obj.content;
      if (visible) updateLastReasoning();
      break;
    }
    case 'reasoning_end': {
      const idx = t.entries.map(x => x.kind).lastIndexOf('reasoning');
      if (idx >= 0) t.entries[idx].closed = true;
      if (visible) { const d = document.getElementById('live-details-' + idx); if (d) d.open = false; }
      break;
    }
    case 'tool':
      t.entries.push({ kind: 'tool', id: obj.content.id, name: obj.content.name, args: obj.content.args, result: '' });
      // A long tool used to leave a stale "Writing..." on the status bar.
      if (visible) { status.textContent = '⚙ ' + (obj.content.name || 'tool') + '…'; renderTurnLive(); }
      break;
    case 'tool_result': {
      const rec = t.entries.find(x => x.kind === 'tool' && x.id === obj.content.id)
        || [...t.entries].reverse().find(x => x.kind === 'tool' && !x.result);
      if (rec) rec.result = obj.content.content;
      if (visible) {
        status.textContent = 'Thinking...'; // tool done: the next LLM round starts
        const block = document.getElementById('tool-' + obj.content.id);
        if (block) FC.fillToolResult(block, obj.content.content, pullFromPc);
        else renderTurnLive();
      }
      break;
    }
    case 'agent_spawn':
      agents[obj.content.id] = {
        layer: obj.content.layer, tool: obj.content.tool, goal: obj.content.goal,
        replyFormat: obj.content.reply_format || '', status: 'running', history: [],
      };
      if (obj.content.call_id) specByCall[obj.content.call_id] = obj.content.id;
      agentBubble(obj.content.id);
      break;
    case 'agent_status': setAgentStatus(obj.content.id, obj.content.status); break;
    case 'agent_event': {
      const aid = obj.content.id, ev = obj.content.event;
      if (agents[aid]) agents[aid].history.push(ev);
      agentEvent(aid, ev);
      break;
    }
    case 'ask':
      t.entries.filter(x => x.kind === 'ask').forEach(a => a.active = false);
      t.entries.push({ kind: 'ask', id: obj.content.id, questions: obj.content.questions || [], answers: null, active: true });
      if (visible) renderTurnLive();
      break;
    case 'status':
      if (visible) status.textContent = obj.content;
      break;
    case 'error':
      if (obj.content === 'Aborted by user') t.aborted = true;
      t.entries.push({ kind: 'error', content: obj.content });
      if (visible) renderTurnLive();
      break;
    case 'sessionId':
      if (!t.sessionId) t.sessionId = obj.content;
      break;
    case 'done': {
      // 好友视图开着时只刷会话列表，不重绘 #messages（见 renderMessages 的所有权）。
      const viewing = currentSessionId === t.sessionId && pane.is('session');
      const failed = t.entries.length && t.entries[t.entries.length - 1].kind === 'error';
      turn = null; abortCtrl = null; stopRequested = false;
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      if (t.aborted) {
        // stop means stop: killed background tasks never send a final status
        Object.keys(agents).forEach(id => {
          if (agents[id].status === 'running') setAgentStatus(id, 'aborted');
        });
      }
      status.textContent = t.aborted ? '已中止。'
        : (failed ? '回合失败。' : '');
      if (viewing) reloadSessionFromServer();
      else loadSessions();
      break;
    }
  }
  if (visible && currentSessionId === t.sessionId && stick) S.stick();
}

function updateLastText() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('text');
  if (idx < 0) return renderTurnLive();
  const el = document.getElementById('live-text-' + idx);
  if (el) {
    const stick = isNearBottom(msgs);
    el.innerHTML = marked.parse(FC.stripSilent(t.entries[idx].content)) + '<span class="live-cursor"></span>';
    FC.linkifyPaths(el, pullFromPc);
    if (stick) S.stick();
  }
  else renderTurnLive();
}
function updateLastReasoning() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('reasoning');
  if (idx < 0) return;
  const el = document.getElementById('live-reasoning-' + idx);
  if (el) { el.textContent = t.entries[idx].content; el.scrollTop = el.scrollHeight; const d = document.getElementById('live-details-' + idx); if (d) d.open = true; }
  else renderTurnLive();
}
function renderTurnLive() {
  if (!turn || turn.sessionId !== currentSessionId || !pane.is('session')) return;
  const saved = saveAskCardState();
  const stick = isNearBottom(msgs); // measure before the repaint replaces the DOM
  S.el().querySelectorAll('.live-node').forEach(n => n.remove());
  if (turn.userText && !turn.userRendered) {
    const u = document.createElement('div');
    u.className = 'msg user live-node';
    u.innerHTML = marked.parse(turn.userText);
    S.append(u);
  }
  turn.entries.forEach((e, i) => {
    if (e.kind === 'reasoning') {
      const det = document.createElement('details');
      det.className = 'msg reasoning live-node'; det.id = 'live-details-' + i; det.open = !e.closed;
      det.innerHTML = '<summary>Thinking\u2026</summary><div id="live-reasoning-' + i + '"></div>';
      det.querySelector('#live-reasoning-' + i).textContent = e.content;
      S.append(det);
    } else if (e.kind === 'text') {
      const text = FC.stripSilent(e.content);
      if (!text) return; // the abstention marker alone leaves no bubble
      const ad = document.createElement('div');
      ad.className = 'msg assistant live-node'; ad.id = 'live-text-' + i;
      ad.innerHTML = marked.parse(text) + '<span class="live-cursor"></span>';
      FC.linkifyPaths(ad, pullFromPc);
      S.append(ad);
    } else if (e.kind === 'tool') {
      const d = FC.buildToolCard({ id: e.id, name: e.name, args: e.args, result: e.result }, { argsMax: 60 });
      d.classList.add('live-node');
      S.append(d);
      if (e.name === 'spawn' || e.name === 'background') FC.attachSpawnClick(d, e.id, callId => specByCall[callId] || archivedByCall[callId], '点按查看子代理详情');
    } else if (e.kind === 'ask') {
      const card = e.active ? buildActiveAskCard(e, saved) : buildAnsweredAskCard(e);
      card.classList.add('live-node');
      S.append(card);
    } else if (e.kind === 'error') {
      const d = document.createElement('div');
      d.className = 'msg error live-node';
      d.innerHTML = '&#x26A0; ' + escapeHtml(e.content);
      S.append(d);
    }
  });
  if (stick) S.stick();
  // Animate only nodes that appeared since the previous streaming re-render —
  // re-animating all live nodes per chunk would flicker (desktop contract).
  const live = S.el().querySelectorAll('.live-node');
  for (let li = _liveCount; li < live.length; li++) {
    const n = live[li];
    msgIn(n, n.classList.contains('user') ? 'user' : n.classList.contains('assistant') ? 'assistant' : 'other');
  }
  _liveCount = live.length;
}

/* ---------- ask cards (in-turn Inquire) ---------- */
const Asks = FC.initAsks({
  http: FC,
  getTurn: () => turn,
  labels: { submit: '提交', required: '必填', customPlaceholder: '或者自己输入…', answerPlaceholder: '你的回答…', noAnswer: '\u23F3 未回答' },
});
const saveAskCardState = Asks.saveAskCardState;
const buildActiveAskCard = Asks.buildActiveAskCard;
const buildAnsweredAskCard = Asks.buildAnsweredAskCard;

/* ---------- pending card asks (consent / cross-host, out-of-band) ----------
   Contract (common.js): register in pendingAskCards + re-mount after every
   full repaint (placeAskCards). Missing either makes cards vanish mid-stream. */
const PendingAsks = FC.initPendingAsks({
  http: FC,
  banner: () => banner,
  inlineHost: () => friendView,
  msgs: () => msgs,
  isNearBottom,
  displayOf: h => h, // mobile shows the raw wire name on ask cards
  animatePlace: false,
  motion: {
    cardIn: el => { if (motionOn()) gsap.from(el, { opacity: 0, y: -14, duration: 0.4, ease: 'power3.out', clearProps: 'all' }); },
    resolved: (card, ok, settle) => {
      if (motionOn()) gsap.to(card, { opacity: 0, scale: 0.92, duration: 0.35, ease: 'power2.in', onComplete: settle });
      else settle();
    },
  },
  labels: {
    fromFallback: 'remote host',
    consentPlaceholder: '自定义回复（可选）',
    allow: '允许', deny: '禁止', consentSend: '发送', submit: '提交', required: '必填',
    customPlaceholder: '或者自己输入…', answerPlaceholder: '你的回答…',
  },
});
const placeAskCards = PendingAsks.place, pollPendingAsks = PendingAsks.poll;
setInterval(pollPendingAsks, 3000);
setInterval(resumeIfPending, 3000);
resumeIfPending();

/* ---------- friends: room members + read-only comm clone conversations ---------- */
let friendView = null;   // host name while viewing a friend conversation
let allPeers = [];       // [{name, display}]
let lastFriendPayload = null; // rendered /comm-log JSON: skip no-change repaints

function peerName(p) { return typeof p === 'string' ? p : (p && p.name) || ''; }
function peerDisplay(p) { return typeof p === 'string' ? p : ((p && p.display) || peerName(p)); }
function displayOf(host) {
  for (const p of allPeers) if (peerName(p) === host) return peerDisplay(p);
  return host;
}

/* 唯一更换 #messages 主人的地方：pane 标志与 friendView 不会各说各话。 */
function setView(host) {
  friendView = host;
  pane.take(host ? 'friend' : 'session');
}

function leaveFriendView() {
  const wasViewing = friendView !== null;
  setView(null);
  lastFriendPayload = null;
  clearTimeout(friendLiveTimer);
  document.getElementById('input-area').style.display = '';
  document.getElementById('friend-input-area').classList.add('hidden');
  document.getElementById('friend-bar').classList.add('hidden');
  document.getElementById('btn-back').hidden = true;
  renderFriendList();
  if (wasViewing) {
    // setView wiped the message area without touching session state: re-render
    // the session that was on screen.
    if (currentSessionId) reloadSessionFromServer();
  }
}

async function loadPeers() {
  try {
    const r = await fetchJSON('/peers');
    if (!r.ok) return;
    const d = await r.json();
    allPeers = d.peers || [];
    document.getElementById('friends-count').textContent = allPeers.length ? '(' + allPeers.length + ')' : '';
    document.getElementById('friends-empty').style.display = allPeers.length ? 'none' : '';
    renderFriendList();
    if (pane.is('friend')) refreshFriendChat();
  } catch (e) {}
}

function renderFriendList() {
  const list = document.getElementById('friend-list');
  list.querySelectorAll('.friend-row').forEach(r => r.remove());
  allPeers.forEach(p => {
    const name = peerName(p);
    const row = document.createElement('div');
    row.className = 'friend-row' + (name === friendView ? ' active' : '');
    const n = (name === friendView) ? 0 : (MailUnread.byPeer()[name] || 0); // viewing the thread = no badge
    row.innerHTML = '<span class="friend-dot"></span><span>' + escapeHtml(peerDisplay(p)) + '</span>'
      + (n ? '<span class="friend-unread">' + n + '</span>' : '');
    row.addEventListener('click', () => { openFriendChat(name); closeDrawer(); });
    list.appendChild(row);
  });
}

async function openFriendChat(host) {
  /* Nav audit: friend-row click is the only legit entry (see app.js twin). */
  try { (window.__navlog = window.__navlog || [])
    .push({ t: new Date().toISOString(), host, stack: new Error().stack }); } catch (e) {}
  leaveFriendView();
  setView(host);
  lastFriendPayload = null;
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('friend-input-area').classList.remove('hidden');
  document.getElementById('btn-back').hidden = false;
  document.getElementById('friend-bar').classList.remove('hidden');
  document.getElementById('session-title').textContent = '@' + displayOf(host);
  renderFriendList();
  try {
    const cm = await (await fetchJSON('/consent-mode?host=' + encodeURIComponent(host))).json();
    setConsentSeg(cm.mode || 'ask');
  } catch (e) { setConsentSeg('ask'); }
  await refreshFriendChat();
}

function setConsentSeg(mode) {
  document.querySelectorAll('#consent-seg button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === mode));
}
document.querySelectorAll('#consent-seg button').forEach(b => {
  b.addEventListener('click', () => {
    if (!pane.is('friend')) return;
    setConsentSeg(b.dataset.v);
    fetch(api('/consent-mode'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: friendView, mode: b.dataset.v }) }).catch(() => {});
  });
});

let friendLiveTimer = null;
async function refreshFriendChat() {
  const host = friendView;
  if (!host) return;
  // keep the open friend view near-real-time: the hub delivers instantly,
  // only this poll gates the paint. Reschedule FIRST: every early return
  // below (!r.ok, raced switch) must not kill the polling chain.
  clearTimeout(friendLiveTimer);
  friendLiveTimer = setTimeout(refreshFriendChat, 1500);
  try {
    const r = await fetchJSON('/comm-log?host=' + encodeURIComponent(host));
    if (!r.ok) return;
    const d = await r.json();
    if (friendView !== host) return; // raced a switch away: never paint here
    const payload = JSON.stringify(d);
    if (payload === lastFriendPayload) return; // unchanged: no flicker
    lastFriendPayload = payload;
    try {
      renderFriendChat(d);
    } catch (e) {
      // 重绘抛错必须解缓存，否则一次异常把视图冻死到刷新为止（桌面同款）。
      lastFriendPayload = null;
      console.error('renderFriendChat:', e);
    }
  } catch (e) {}
}

/* 实时磁带渲染也共用（FC.renderLiveEvents） */
function renderFriendChat(d) {
  const p = F;
  const messages = d.messages || [];
  const events = d.events || [];
  const live = d.live || [];
  const mails = d.mails || [];
  if (!messages.length && !events.length && !mails.length && !live.length) {
    return; // 先判空后清屏：空载荷不得擦掉已有画面
  }
  p.clearMsgs();
  const stick = isNearBottom(msgs); // measure before the repaint replaces the DOM
  const mailBodies = new Set(mails.map(m => String(m.body || '').trim()).filter(Boolean));
  const opts = renderOpts({
    friend: true, side: FRIEND_SIDE, mailBodies, report: true, feedbackHost: friendView,
  });
  FC.renderTranscript(p, messages, d.asks || [], opts);
  events.forEach(row => {
    let node = null;
    if (row.kind === 'transfer')
      node = p.add('friend-event', '&#x1F4C4 ' + escapeHtml(row.text || 'file transfer'));
    else if (row.kind === 'task')
      node = p.add('friend-event', '&#x1F4E5 delegated to ' + escapeHtml(row.dst || '?') + ': ' + escapeHtml((row.text || '').slice(0, 200)));
    else if (row.kind === 'result')
      node = p.add('friend-event', '&#x2714 ' + escapeHtml(row.src || '?') + ' replied: ' + escapeHtml((row.text || '').slice(0, 200)));
    if (node) { FC.markTs(node, row.ts); FC.insertByTs(p, node, row.ts); }  // 信封自带 hub 时间戳
  });
  MailUnread.markPeerRead(friendView); // seeing the thread IS reading it
  mails.forEach(m => {
    const body = String(m.body || '');
    const subject = String(m.subject || '');
    const html = (subject && subject !== '(no subject)' && subject !== body
      ? '<strong>' + escapeHtml(subject) + '</strong><br>' : '') + marked.parse(body);
    const who = m.mine ? '我'
      : (String(m.from || '').endsWith(':human')
        ? '来自 ' + displayOf(m.peer) + ' 的用户'
        : displayOf(m.peer) + ' 的 Agent');
    const bubble = p.add('user' + (m.mine ? ' friend-mine' : ' friend-peer'), html);
    const lab = document.createElement('div');
    lab.className = 'human-label';
    lab.textContent = who;
    bubble.prepend(lab);
    FC.markTs(bubble, m.ts);   // 邮件行自带邮箱时间戳
    FC.insertByTs(p, bubble, m.ts);
  });
  FC.renderLiveEvents(live, p, opts);
  placeAskCards(); // re-seat pending asks after the transcript repaint
  if (stick) p.stick();
  updateScrollBtn();
}

/* ---------- agent tray (live subagent bubbles + bottom-sheet replay) ---------- */
const agents = {};         // live + archived spec id -> {layer, goal, replyFormat, status, history}
const specByCall = {};     // live tool_call id -> spec id
const archivedByCall = {}; // replayed spawn tool_call id -> spec id

function registerArchived(subs) {
  // Persisted subagent records (session replay): keeps spawn cards clickable
  // and the bottom-sheet populated after a reload.
  (subs || []).forEach(r => {
    if (!r || !r.id) return;
    agents[r.id] = { layer: r.layer, goal: r.goal || '', replyFormat: r.reply_format || '', status: r.status || 'done', history: r.events || [] };
    if (r.call_id) archivedByCall[r.call_id] = r.id;
  });
}
function agentBubble(id) {
  const tray = document.getElementById('agent-tray');
  let el = tray.querySelector('[data-agent="' + id + '"]');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'agent-bubble';
  el.dataset.agent = id;
  el.dataset.st = 'running';
  const a = agents[id];
  const label = a.tool === 'background' ? 'Bg' : 'L' + (a.layer || 2);
  el.innerHTML = '<span class="lb">' + label + '</span><span class="st"></span>';
  el.addEventListener('click', () => openAgentModal(id));
  tray.appendChild(el);
  if (motionOn()) gsap.from(el, { opacity: 0, y: 10, duration: 0.3, ease: 'power3.out', clearProps: 'all' });
  return el;
}
function setAgentStatus(id, st) {
  if (!agents[id]) return;
  agents[id].status = st;
  const el = agentBubble(id);
  el.dataset.st = st;
  if (st !== 'running') setTimeout(() => el.remove(), 12000); // finished: fade out of the tray
}
function agentEvent(id, ev) {
  // Server payload: {type, content} — "kind"/"text" never existed.
  const el = document.getElementById('agent-tray').querySelector('[data-agent="' + id + '"]');
  if (!el || !ev) return;
  const c = ev.content;
  const label = typeof c === 'string' ? c
    : (c && (c.text || c.name || c.status || (c.goal ? 'spawn L' + c.layer : ''))) || ev.type || '';
  el.querySelector('.nm').textContent = String(label).slice(0, 24);
}
function evLine(ev) {
  const c = ev && ev.content;
  if (!ev || !ev.type) return '?';
  if (ev.type === 'agent_spawn') return '\u{1F9E9} spawn L' + (c.layer || '?') + ': ' + String(c.goal || '').slice(0, 160);
  if (ev.type === 'agent_status') return 'status: ' + ((c && c.status) || '?');
  const text = typeof c === 'string' ? c : (c && (c.text || c.name)) || JSON.stringify(c) || '';
  return ev.type + ' \u00B7 ' + String(text).slice(0, 300);
}
function openAgentModal(id) {
  const a = agents[id];
  if (!a) return;
  const ov = document.getElementById('agent-modal-overlay');
  // text/reasoning deltas arrive one recorded event per sink call; merge
  // adjacent same-type runs so the modal reads as flowing paragraphs
  // instead of one word per bordered row.
  const rows = [];
  for (const ev of a.history || []) {
    const c = ev && ev.content;
    const last = rows[rows.length - 1];
    if ((ev.type === 'text' || ev.type === 'reasoning') && last && last.type === ev.type) {
      last.text += typeof c === 'string' ? c : (c && (c.text || c.name)) || '';
    } else {
      rows.push({ ev, type: ev.type, text: typeof c === 'string' ? c : (c && (c.text || c.name)) || '' });
    }
  }
  const evs = rows.slice(-40).map(r =>
    '<div class="am-ev">' + escapeHtml(r.type === 'text' || r.type === 'reasoning'
      ? r.type + ' \u00B7 ' + r.text
      : evLine(r.ev)) + '</div>').join('');
  ov.innerHTML = '<div id="agent-modal"><div class="agent-modal-head"><h3>'
    + escapeHtml((a.tool === 'background' ? 'Bg' : 'L' + (a.layer || '?')) + ' · ' + String(a.goal || '').slice(0, 40))
    + '</h3><button id="agent-modal-close">\u2715</button></div>'
    + '<div class="am-goal">' + escapeHtml(a.goal) + '</div>'
    + '<div class="am-status">' + escapeHtml(a.status) + (a.replyFormat ? ' · 回复格式: ' + escapeHtml(a.replyFormat) : '') + '</div>'
    + (evs || '<div class="am-ev">（暂无事件）</div>') + '</div>';
  ov.classList.add('show');
  const close = () => { ov.classList.remove('show'); ov.innerHTML = ''; };
  ov.querySelector('#agent-modal-close').addEventListener('click', close);
  ov.onclick = e => { if (e.target === ov) close(); }; // backdrop tap also closes
}

/* ---------- drawer gestures (right-swipe open, left-swipe close) ---------- */
const drawer = document.getElementById('drawer'), scrim = document.getElementById('drawer-scrim');
const chatPage = document.getElementById('chat-page');
let drawerOpen = false, drag = null;
function drawerW() { return drawer.offsetWidth; }
function setDrawer(x, scrimOp) {
  gsap.set(drawer, { x });
  gsap.set(scrim, { opacity: scrimOp });
}
function applyDrawer(open, opts = {}) {
  const wasOpen = drawerOpen;
  drawerOpen = open;
  const dur = opts.instant ? 0 : 0.42;
  gsap.to(drawer, { x: open ? 0 : -drawerW(), duration: dur, ease: open ? 'power3.out' : 'power3.in', overwrite: 'auto' });
  gsap.to(scrim, {
    opacity: open ? 1 : 0, duration: dur, overwrite: 'auto',
    onStart: () => { if (open) scrim.style.pointerEvents = 'auto'; },
    onComplete: () => { if (!open) scrim.style.pointerEvents = 'none'; },
  });
  // Stagger only on a real closed->open transition: replaying it on every
  // drawer touchend would shift rows 24px under a finger and suppress the
  // synthesized click on the row buttons.
  if (open && !wasOpen && opts.instant !== true && motionOn()) {
    const rows = drawer.querySelectorAll('.session-row');
    if (rows.length) gsap.from(rows, { opacity: 0, x: -24, duration: 0.38, stagger: 0.035, ease: 'power3.out', clearProps: 'all', overwrite: 'auto' });
  }
}
function openDrawer() { applyDrawer(true); }
function closeDrawer() { applyDrawer(false); }
scrim.addEventListener('click', closeDrawer);
document.getElementById('btn-menu').addEventListener('click', openDrawer);
function inHorizScroller(el) {
  /* The innermost horizontally scrollable element under the touch, if any
     (long tool output, agent tray). */
  for (let n = el; n && n !== chatPage; n = n.parentElement) {
    if (n.scrollWidth > n.clientWidth + 2) {
      const ox = getComputedStyle(n).overflowX;
      if (ox === 'auto' || ox === 'scroll') return n;
    }
  }
  return null;
}
let pscroll = null; // touch on a horizontal card: JS scrolls it, edge overflow chains into the drawer
chatPage.addEventListener('touchstart', e => {
  const t = e.touches[0];
  // NO "if (drag) return" guard: a gesture the webview swallows (WeChat X5's
  // native edge handling often fires no end/cancel at all) used to wedge
  // drag truthy forever and silently kill every later swipe. A new touch
  // always supersedes stale state. The swipe may start ANYWHERE: the old 48px
  // edge wedge never triggered in real use and fought the phone's own edge
  // gesture; the vertical-lock in touchmove keeps normal scrolling intact.
  const hs = inHorizScroller(e.target);
  if (hs) {
    // The card scrolls first — but native pan-x never chains across elements,
    // so scrolling is manual here: once the content hits its edge, the
    // leftover delta drives the drawer.
    pscroll = { el: hs, x0: t.clientX, y0: t.clientY, start: hs.scrollLeft, locked: null };
    return;
  }
  pscroll = null;
  drag = { x0: t.clientX, y0: t.clientY, base: drawerOpen ? 0 : -drawerW(), w: drawerW(), locked: null, lastX: t.clientX, lastT: performance.now(), vx: 0 };
}, { passive: true });
chatPage.addEventListener('touchmove', e => {
  const t = e.touches[0];
  if (pscroll) {
    const dx = t.clientX - pscroll.x0, dy = t.clientY - pscroll.y0;
    if (pscroll.locked === null) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      pscroll.locked = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
      if (pscroll.locked === 'v') { pscroll = null; return; } // vertical: native pan-y takes over
    }
    const el = pscroll.el, max = el.scrollWidth - el.clientWidth;
    const want = pscroll.start - dx;              // finger right => content scrolls left
    el.scrollLeft = Math.max(0, Math.min(max, want));
    const over = want - el.scrollLeft;            // leftover after the content edge
    const base = drawerOpen ? 0 : -drawerW();
    const x = Math.max(-drawerW(), Math.min(0, base - over));
    setDrawer(x, 1 + x / drawerW());
    return;
  }
  if (!drag) return;
  const dx = t.clientX - drag.x0, dy = t.clientY - drag.y0;
  if (drag.locked === null) {
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    drag.locked = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
    if (drag.locked === 'v') { drag = null; return; }
  }
  const x = Math.max(-drag.w, Math.min(0, drag.base + dx));
  const now = performance.now();
  drag.vx = (t.clientX - drag.lastX) / Math.max(1, now - drag.lastT);
  drag.lastX = t.clientX; drag.lastT = now;
  setDrawer(x, 1 + x / drag.w);
}, { passive: true });
function settleDrag(velSign) {
  // Shared finish for touchend AND touchcancel: position decides, but a fast
  // flick in the drag direction opens/closes even from a shallow drag —
  // touchcancel means the browser stole the gesture mid-flick, so position
  // alone would always settle closed.
  const d = drag; drag = null;
  if (!d) return;
  const x = parseFloat(gsap.getProperty(drawer, 'x'));
  // Opening still needs the halfway point (or a flick); closing is
  // symmetric with the drawer-side swipe: ANY leftward displacement or a
  // fast left flick closes, from wherever the finger started.
  const opened = d.base === 0
    ? !(x < 0 || (d.vx || 0) < -0.35)
    : x > -d.w / 2 || (d.vx || 0) > 0.35;
  applyDrawer(opened);
}
function settlePSwipe() {
  const p = pscroll; pscroll = null;
  if (!p) return;
  const x = parseFloat(gsap.getProperty(drawer, 'x'));
  applyDrawer(x > -drawerW() / 2);
}
chatPage.addEventListener('touchend', () => { if (pscroll) settlePSwipe(); if (drag) settleDrag(1); }, { passive: true });
chatPage.addEventListener('touchcancel', () => { if (pscroll) settlePSwipe(); if (drag) settleDrag(1); }, { passive: true });
// drawer-side left swipe (finger starts on the drawer itself)
drawer.addEventListener('touchstart', e => {
  const t = e.touches[0];
  drawer._sw = { x0: t.clientX, y0: t.clientY, lastX: t.clientX, lastT: performance.now(), vx: 0 };
}, { passive: true });
drawer.addEventListener('touchmove', e => {
  const sw = drawer._sw;
  if (!sw || !drawerOpen) return;
  const t = e.touches[0];
  const dx = t.clientX - sw.x0, dy = t.clientY - sw.y0;
  if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 10) { drawer._sw = null; return; }
  const now = performance.now();
  sw.vx = (t.clientX - sw.lastX) / Math.max(1, now - sw.lastT);
  sw.lastX = t.clientX; sw.lastT = now;
  const x = Math.max(-drawerW(), Math.min(0, dx));
  if (x < 0) setDrawer(x, 1 + x / drawerW());
}, { passive: true });
drawer.addEventListener('touchend', () => {
  const sw = drawer._sw; drawer._sw = null;
  if (!sw || !drawerOpen) return;
  // A clean tap (row buttons!) must not re-run the drawer settle: the row
  // entrance shift would move the button away and swallow the click.
  if (Math.abs(sw.lastX - sw.x0) < 8 && Math.abs(sw.vx) < 0.1) return;
  const x = parseFloat(gsap.getProperty(drawer, 'x'));
  applyDrawer(!(x < 0 || sw.vx < -0.35)); // any leftward displacement closes (was: past half width)
}, { passive: true });
drawer.addEventListener('touchcancel', () => { drawer._sw = null; }, { passive: true });

/* ---------- wiring ---------- */
document.getElementById('btn-new-session').addEventListener('click', newSession);
document.getElementById('session-filter').addEventListener('input', renderSessionList);
document.getElementById('btn-back').addEventListener('click', leaveFriendView);
btn.addEventListener('click', send);
function autoGrow() {
  input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  if (!processing) setBusy(false); // ↻/↑ follows the input content responsively
}
input.addEventListener('input', autoGrow);
/* ---------- file pull: a path on screen -> the phone (§52) ---------- */
async function pullFromPc(path) {
  const name = String(path).split(/[\\/]/).pop() || '文件';
  if (!Xfer.open('从电脑取文件', ['下载到手机 · ' + name])) return;
  Xfer.note(0, '正在取…');
  try {
    const saved = await Xfer.download(path, (done, total) => Xfer.progress(0, done, total));
    Xfer.finish(saved === '交给浏览器下载' ? '大文件已交给浏览器下载' : '已保存到手机：' + saved);
  } catch (e) {
    Xfer.fail('下载失败：' + (e.message || String(e)), 0);
  }
}

/* ---------- file upload: phone picker -> PC inbox, path dropped in the box ---------- */
const fileInput = document.getElementById('file-input');
document.getElementById('btn-file').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  const files = Array.from(fileInput.files || []);
  fileInput.value = '';
  if (!files.length) return;
  Xfer.open('上传文件到电脑', files.map(f => '上传到电脑 · ' + f.name));
  for (const [i, f] of files.entries()) {
    Xfer.note(i, '正在上传…');
    try {
      const path = await Xfer.upload(f, (done, total) => Xfer.progress(i, done, total));
      Xfer.finish('已保存到电脑'); // 完成后自动关闭
      input.value = (input.value ? input.value + ' ' : '') + path;
      autoGrow();
    } catch (e) {
      Xfer.fail('上传失败：' + (e.message || f.name), i);
      return;
    }
  }
  input.focus();
});
// Keep the transcript pinned when the mobile keyboard resizes the viewport.
if (window.visualViewport) {
  visualViewport.addEventListener('resize', () => { if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight; });
}
/* theme: light default, persisted (same key as desktop) */
function applyTheme(t) { document.documentElement.setAttribute('data-theme', t); document.querySelector('meta[name="theme-color"]').content = t === 'dark' ? '#12141c' : '#f0f2f8'; }
try { applyTheme(localStorage.getItem('fungi-theme') === 'dark' ? 'dark' : 'light'); } catch (e) { applyTheme('light'); }
document.getElementById('theme-switch').addEventListener('click', function () {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  const apply = () => {
    applyTheme(next);
    try { localStorage.setItem('fungi-theme', next); } catch (e) {}
  };
  // Day-night wash: an accent-tinted circle expands from the switch, the
  // color flip happens once covered, then the wash fades (desktop themeTo).
  if (!motionOn()) { apply(); return; }
  const wash = document.createElement('div');
  wash.className = 'motion-theme-wash ' + next;
  const r = this.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const R = Math.hypot(Math.max(cx, innerWidth - cx), Math.max(cy, innerHeight - cy)) + 40;
  wash.style.clipPath = 'circle(0px at ' + cx + 'px ' + cy + 'px)';
  document.body.appendChild(wash);
  gsap.to(wash, {
    clipPath: 'circle(' + R + 'px at ' + cx + 'px ' + cy + 'px)',
    duration: 0.34, ease: 'power2.in',
    onComplete: () => {
      apply();
      gsap.to(wash, { opacity: 0, duration: 0.3, ease: 'power1.out', delay: 0.06, onComplete: () => wash.remove() });
    }
  });
});

/* ---------- boot ---------- */
setBusy(false);
if (!TOKEN) document.getElementById('rescan-overlay').classList.remove('hide'); // no token: rescan required
else document.getElementById('rescan-overlay').classList.add('hide'); // valid token: drop the rescan card
fetchJSON('/model').then(r => r.json()).then(d => { document.getElementById('model-name').textContent = d.model || ''; }).catch(() => {});
loadSessions().then(() => {
  let saved = null;
  try { saved = localStorage.getItem('fungi-session-m'); } catch (e) {}
  if (saved && allSessions.some(s => s.id === saved)) switchSession(saved);
  else renderSessionList();
});
pollPendingAsks();
loadPeers();
setInterval(loadPeers, 5000);
/* 文件传输助手（§53）：两个设备往同一个会话里落行（手机上传的落点、电脑要发给
   手机的文件），谁都不点刷新也得看得到 —— 只在这个会话里轮询，别的会话一个都不动。
   哪个 id 是它由服务端在 /sessions 里标（entry.shuttle），前端不重复写一份常量。 */
setInterval(() => {
  if (document.hidden || processing || !currentSessionId) return;
  if (!pane.is('session')) return;   // 好友视图握着 #messages
  const open = (allSessions || []).find(s => s.id === currentSessionId);
  if (open && open.shuttle) reloadSessionFromServer();
}, 3000);

/* ---------- mail unread: per-peer badges on the friend list ---------- */
const MailUnread = FC.initMailUnread({ http: FC, onChange: renderFriendList });
MailUnread.start();

/* ---------- send-file progress modal (shared implementation) ---------- */
const Xfer = FC.initTransfer({ http: FC });


/* ---------- friend view composer: human direct sends ---------- */
const friendInput = document.getElementById('friend-input');
async function commSend(payload) {
  if (!pane.is('friend')) return;
  const btn = document.getElementById('friend-send');
  btn.disabled = true;
  try {
    const d = await (await fetchJSON('/comm-send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ host: friendView }, payload)) })).json();
    if (d.error) F.add('friend-event', '&#x26A0 ' + escapeHtml(d.error));
  } catch (e) {} finally { btn.disabled = false; }
  setTimeout(refreshFriendChat, 300); // pull the new message/file event in quickly
}
document.getElementById('friend-send').addEventListener('click', () => {
  const t = friendInput.value.trim();
  if (!t) return;
  friendInput.value = '';
  commSend({ text: t });
});
friendInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); document.getElementById('friend-send').click(); }
});
const friendFileInput = document.getElementById('friend-file-input');
document.getElementById('friend-file').addEventListener('click', () => friendFileInput.click());
friendFileInput.addEventListener('change', async () => {
  const files = Array.from(friendFileInput.files || []);
  friendFileInput.value = '';
  for (const f of files) {
    try {
      // two hops on a phone: browser -> this host, then this host -> the peer
      await Xfer.sendFromPhone('发送文件给 ' + displayOf(friendView), friendView, f);
    } catch (e) {
      return; // the modal carries the reason and stays open until it is closed
    }
  }
  setTimeout(refreshFriendChat, 300);
});
