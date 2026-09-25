/* Fungi web UI */
marked.setOptions({ breaks: true, gfm: true });
const FC = window.FungiCommon;
const msgs = document.getElementById('messages'), input = document.getElementById('input'),
  btn = document.getElementById('send'), status = document.getElementById('status'),
  tray = document.getElementById('agent-tray');
let processing = false, currentSessionId = null, allSessions = [], sessionDirty = false;
let _liveCount = 0; // live-node count at last streaming render (motion: animate only fresh nodes)
let rawMessages = [];

function setCurrentSession(id) {
  currentSessionId = id;
  try { if (id) localStorage.setItem('fungi-session', id); else localStorage.removeItem('fungi-session'); } catch (e) {}
}

/* ---------- helpers ---------- */
const escapeHtml = FC.escapeHtml;
/* #messages has exactly one owner at a time. Every write below goes through a
   pane writer: S for the session transcript, F for the friend thread. A writer
   whose view does not own the pane paints nothing (FC.initPane). */
const pane = FC.initPane({
  msgs: () => msgs, tray: () => tray, isNearBottom, onPaint: updateScrollBtn,
});
const S = pane.of('session'), F = pane.of('friend');
function isNearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 60; }
function updateScrollBtn() {
  const b = document.getElementById('scroll-bottom');
  if (!b) return;
  if (isNearBottom(msgs)) b.classList.remove('visible'); else b.classList.add('visible');
}
document.getElementById('scroll-bottom').addEventListener('click', () => { msgs.scrollTop = msgs.scrollHeight; updateScrollBtn(); });
msgs.addEventListener('scroll', updateScrollBtn);

const getSessionTitle = list => FC.getSessionTitle(list, 55, 52);
/* 输出速率（§64）：喂它的只有本页自己那条流的 text/reasoning chunk */
const rateTok = FC.tokenRate(document.getElementById('tok-rate'));

/* ---------- confirm modal (shared impl in common.js) ---------- */
FC.initConfirmModal({ keyboard: 'desktop' });
const showConfirm = FC.showConfirm, closeConfirm = FC.closeConfirm;

/* ---------- sessions ---------- */
let _sessionsSeq = 0;
async function loadSessions() {
  const seq = ++_sessionsSeq;
  try {
    const r = await fetch('/sessions');
    if (!r.ok) throw new Error(r.status);
    const sessions = (await r.json()).sessions || [];
    if (seq !== _sessionsSeq) return; // a newer fetch superseded this response
    allSessions = sessions;
    // §61: the alert map from /session/seen outranks the payload's copy of it —
    // the claim that produced it also cleared what it covered (a session the
    // user just opened must not wear its dot again for this fetch).
    const known = Alerts.map();
    if (known) applyAlerts(known);
    renderSessionList();
  } catch (e) { console.error('loadSessions:', e); }
}
async function reloadSessionFromServer() {
  if (!currentSessionId) return;
  try {
    const r = await fetch('/session?id=' + encodeURIComponent(currentSessionId));
    if (!r.ok) return;
    const s = await r.json();
    rawMessages = s.messages || [];
    registerArchived(s.subagents);
    const stick = isNearBottom(msgs); // measure before the repaint replaces the DOM
    renderMessages(s);
    if (stick && S.stick()) updateScrollBtn();
    loadSessions();
  } catch (e) {}
}
async function closeCurrentSession() {
  if (!currentSessionId) return;
  if (processing && turn && turn.sessionId === currentSessionId) {
    // A turn is running in this session: never delete or save over it. The
    // server persists the whole turn on its own at turn end — just detach.
    // (2026-09-04: switching sessions before the first token arrived deleted
    // the session out from under the running turn — the response vanished.)
    setCurrentSession(null);
    return;
  }
  if (!sessionDirty) { setCurrentSession(null); return; }
  const list = rawMessages;
  if (!list || list.length <= 1) {
    try { await fetch('/session?id=' + encodeURIComponent(currentSessionId), { method: 'DELETE' }); } catch (e) {}
    setCurrentSession(null);
  } else {
    const title = getSessionTitle(list);
    try {
      await fetch('/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: currentSessionId, title, messages: list }) });
    } catch (e) {}
  }
  loadSessions();
}
async function switchSession(id) {
  leaveFriendView();
  if (id === currentSessionId) return;
  await closeCurrentSession();
  sessionDirty = false;
  try {
    const r = await fetch('/session?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    setCurrentSession(s.id);
    rawMessages = s.messages || [];
    S.clear();
    registerArchived(s.subagents);
    renderMessages(s);
    S.stick(); updateScrollBtn(); // a freshly opened session starts at the latest message
    renderSessionList(); loadSessions();
    reattachIfRunning(s.id);
    Alerts.tick(); // 用户正在看这个会话：立刻告诉服务端（红点该掉、铃该停）
  } catch (e) { console.error('switchSession:', e); }
}

function renderMessages(s) {
  // #messages has exactly one owner, and it is not us: the open friend view
  // owns it until it is left. A session turn finishing, a stream recovering
  // from a drop, or a session reload used to paint here while a friend
  // conversation was on screen — the friend thread was replaced by the session
  // (and the friend poll would not repaint it, because its payload had not
  // changed). The pane writers enforce this now; the early return keeps the
  // work from happening at all. 2026-09-10 real-machine finding.
  if (!pane.is('session')) return;
  _liveCount = 0; // full re-render: transcript replay is CSS-static, live nodes re-baseline
  // Full re-render must actually replace: renderTranscript only appends, so
  // without this clear every reload (done/ESC/retry) stacked a second copy of
  // the whole transcript below the live nodes. Clearing here makes the
  // reload path byte-for-byte the same render a page refresh does.
  S.clearMsgs();
  FC.renderTranscript(S, rawMessages, s.asks || [], renderOpts());
  if (turn && turn.sessionId === currentSessionId) {
    // The transcript just rendered comes from the disk copy, which (turn-
    // start save) already contains the running turn's user message — the
    // live userText bubble would draw it a second time (mid-turn switch
    // back). Mark it rendered so renderTurnLive skips the bubble.
    if (turn.userText && rawMessages.some(m => m.role === 'user' && m.content === turn.userText)) {
      turn.userRendered = true;
    }
    renderTurnLive();
  }
  placeAskCards(); // friend-view inline cards move back to the banner here
}

/* Timeline + transcript rendering live in common.js (FC.renderTranscript /
   FC.insertByTs): m.js is the same renderer with different cosmetics, and two
   copies of it is how the friend view's bugs got fixed twice. What cannot be
   shared is injected below. */
const renderOpts = extra => Object.assign({
  asks: { buildAnsweredAskCard },
  spawnLookup: callId => specByCall[callId] || archivedByCall[callId],
  argsMax: 80,
  my: MY_SIDE,   // a row sits on the side of the device it came from (§59)
  reasoningHtml: t => '<div style="white-space:pre-wrap;max-height:200px;overflow-y:auto">' + escapeHtml(t) + '</div>',
  liveText: r => { const text = FC.stripSilent(r.text); return text ? marked.parse(text) : ''; },
}, extra || {});
const FRIEND_SIDE = { user: ' friend-peer', agent: ' friend-mine' }; // peer left, courier right (style.css)
/* This shell *is* the computer: it renders its own rows on the right and the
   phone's on the left, and it tells the server the same thing when it sends, so
   a plain message row can be sided too (§59). */
const MY_SIDE = 'computer';

async function newSession() {
  rateTok.clear();  // 新对话从零开始：速率读数只在“有输出/刚输出过”时才有意义
  leaveFriendView();
  // Any untouched "(new session)" on disk? Focus it instead of creating
  // another one — repeated clicks and switches must not litter the list.
  const empty = allSessions.find(s => s.title === '(new session)' && (s.msgCount || 0) <= 1 && !s.running);
  if (empty) {
    if (empty.id !== currentSessionId) await switchSession(empty.id);
    return;
  }
  await closeCurrentSession();
  try {
    const r = await fetch('/new', { method: 'POST' });
    if (!r.ok) throw new Error(r.status);
    const { id } = await r.json();
    setCurrentSession(id); rawMessages = []; S.clear();
    sessionDirty = false;
    await loadSessions();
  } catch (e) { console.error('newSession:', e); }
}
async function deleteSession(id) {
  try {
    await fetch('/session?id=' + encodeURIComponent(id), { method: 'DELETE' });
    if (id === currentSessionId) { setCurrentSession(null); S.clear(); }
    document.getElementById('session-filter').value = '';
    await loadSessions();
  } catch (e) {}
}
/* The live list entry for a row, by id. Rows are reused keyed by id, so a row's
   handlers hold the session object of whatever /sessions fetch created that row
   — an older generation after the next poll. Write through this, never through
   the captured object: renderSessionList's title guard compares the painted name
   against the live entry, and a stale write is exactly how a rename silently
   reverted to the old name (2026-09-11 user report). */
const sessionById = id => allSessions.find(s => s.id === id) || null;

function renderSessionList() {
  const list = document.getElementById('session-list');
  const empty = document.getElementById('session-list-empty');
  const filter = (document.getElementById('session-filter')?.value || '').trim().toLowerCase();
  const filtered = filter ? allSessions.filter(s => (s.title || '').toLowerCase().includes(filter)) : allSessions;
  // Keyed row reconciliation: Flip needs persistent nodes to animate a
  // reorder/move — rebuilding rows every render would make every list change
  // look like remove+add.
  const mutate = () => {
    const existing = {};
    list.querySelectorAll('.session-row').forEach(r => { existing[r.dataset.sid] = r; });
    const wanted = filtered.map(s => {
      const row = existing[s.id];
      if (row) {
        delete existing[s.id];
        const titleEl = row.querySelector('.session-row-title');
        if (titleEl && !row.querySelector('.rename-input') && titleEl.textContent !== (s.title || 'Untitled'))
          titleEl.textContent = s.title || 'Untitled';
        // The reuse path rewrites the row in place: the transfer session's own
        // meta ("只搬文件") must survive it, or it turns back into a date the
        // moment the list repaints (§54).
        if (!s.shuttle) {
          row.querySelector('.session-row-meta').textContent = fmtDate(s.created) + (s.running ? ' \u25cf' : '');
        }
        paintAlert(row, s.alert);  // §61: reuse must refresh the dot too
        row.classList.toggle('active', s.id === currentSessionId);
        return row;
      }
      const fresh = document.createElement('div');
      fresh.dataset.sid = s.id;
      // The transfer session (§53/§54) is a channel between two devices, not a
      // chat: pinned, iconned, styled apart, and it has no rename/delete pair.
      fresh.className = 'session-row' + (s.shuttle ? ' shuttle' : '')
        + (s.id === currentSessionId ? ' active' : '');
      if (s.shuttle) fresh.title = '文件传输助手：两台设备之间只搬文件，不会改名也不会被删除';
      fresh.innerHTML = (s.shuttle ? '<span class="session-row-icon">&#x1F4C1;</span>' : '')
        + '<span class="session-row-title">' + escapeHtml(s.title || 'Untitled') + '</span>'
        + '<span class="session-row-meta">' + (s.shuttle ? '只搬文件' : fmtDate(s.created))
        + (s.running ? ' \u25cf' : '') + '</span>'
        + (s.shuttle ? ''
          : '<span class="session-row-actions"><button class="session-row-act" title="Rename">&#9998;</button>'
            + '<button class="session-row-act del" title="Delete">&#10005;</button></span>');
      if (!s.shuttle) {
        fresh.querySelector('.session-row-act.del').addEventListener('click', e => {
          e.stopPropagation();
          const cur = sessionById(fresh.dataset.sid);
          showConfirm({
            title: 'Delete session',
            message: '"' + ((cur && cur.title) || 'Untitled') + '" will be permanently removed. This cannot be undone.',
            confirmText: 'Delete',
            danger: true,
            onConfirm: () => deleteSession(fresh.dataset.sid)
          });
        });
        fresh.querySelector('.session-row-act:not(.del)').addEventListener('click', e => { e.stopPropagation(); startRename(fresh); });
      }
      paintAlert(fresh, s.alert);
      fresh.addEventListener('click', () => switchSession(s.id));
      return fresh;
    });
    Object.values(existing).forEach(r => r.remove());
    // 只搬**位置不对**的那几行（2026-09-25 用户报告：切换会话/停止会话都像在「视图重排」）。
    // 以前每行无条件 `list.appendChild(row)`：一次重绘就是 N 次 DOM 搬动（21 行 = 130 次
    // childList 变更），既让 Flip 每次都算一遍移动，也在滚动容器上把滚动位置顶掉。
    // 从后往前放，锚点就是「下一个该在它后面的行」——已经在位的行一个指头都不碰。
    let anchor = null;
    for (let i = wanted.length - 1; i >= 0; i--) {
      const row = wanted[i];
      // A row built above is still detached, and a detached node reports
      // nextElementSibling === null — so "not attached" is its own reason to insert.
      if (row.parentNode !== list || row.nextElementSibling !== anchor) list.insertBefore(row, anchor);
      anchor = row;
    }
    if (filtered.length === 0) { empty.style.display = ''; empty.textContent = filter ? 'No matches.' : 'No sessions yet.'; }
    else empty.style.display = 'none';
  };
  // 顺序没变就别进动画（2026-09-25 用户：「切换会话也好，停止会话也好，就会触发视图重排」）。
  // 一次纯重绘（切会话只改 `.active`、停一轮只改那个圆点）以前也会跑一遍 Flip：GSAP 为了
  // 量每一行，会把测量代理 `<div>` 往列表里插一遍再拔掉（5 行 = 24 次 childList 变更），
  // 再开一个 340ms 什么也不动的动画。进这里之前先比顺序：一样就原地更新，一个节点都不碰。
  const ids = filtered.map(s => s.id);
  const painted = Array.from(list.querySelectorAll('.session-row'), r => r.dataset.sid);
  const moved = ids.length !== painted.length || ids.some((id, i) => id !== painted[i]);
  if (moved && window.fungiMotion && !window.fungiMotion.reduced && window.fungiMotion.listFlip)
    window.fungiMotion.listFlip(list, mutate);
  else mutate();
}
const fmtDate = d => FC.fmtDate(d, 'en-US');
function startRename(row) {
  const s = sessionById(row.dataset.sid);
  if (!s) return;
  const titleEl = row.querySelector('.session-row-title');
  const inp = document.createElement('input');
  inp.className = 'rename-input'; inp.value = s.title || titleEl.textContent;
  inp.addEventListener('blur', () => finishRename(row, inp));
  inp.addEventListener('keydown', e => {
    // Enter swaps the input back out, which blurs it: one finish per rename
    // (finishRename's `done` flag absorbs the blur that its own teardown fires).
    if (e.key === 'Enter') { e.preventDefault(); finishRename(row, inp); }
    if (e.key === 'Escape') { row.replaceChild(titleEl, inp); titleEl.textContent = s.title || 'Untitled'; }
  });
  row.replaceChild(inp, titleEl); inp.focus(); inp.select();
}
async function finishRename(row, inp) {
  if (inp.dataset.done) return;
  inp.dataset.done = '1';
  const newTitle = inp.value.trim() || 'Untitled';
  try {
    await fetch('/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: row.dataset.sid, title: newTitle, messages: [] }) });
  } catch (e) {}
  if (inp.parentNode === row) {
    const titleEl = document.createElement('span');
    titleEl.className = 'session-row-title'; titleEl.textContent = newTitle;
    row.replaceChild(titleEl, inp);
  }
  // Onto the LIVE entry, so renderSessionList's guard agrees with the paint
  // instead of restoring the old name from a list object this row predates.
  const s = sessionById(row.dataset.sid);
  if (s) s.title = newTitle;
  renderSessionList();
}

/* ---------- sidebar wiring ---------- */
const burger = document.getElementById('hamburger-sidebar');
function toggleSidebar() {
  const collapsed = document.getElementById('sidebar').classList.toggle('collapsed');
  burger.textContent = collapsed ? '\u276F' : '\u276E'; /* collapsed: > (reopen), open: < (collapse) */
}
burger.addEventListener('click', toggleSidebar);
burger.textContent = document.getElementById('sidebar').classList.contains('collapsed') ? '\u276F' : '\u276E';
document.getElementById('session-filter').addEventListener('input', renderSessionList);
document.getElementById('btn-new-session').addEventListener('click', () => newSession());

/* ---------- agent tray (bubbles while running) + replayable modal ---------- */
const agents = {};         // live spec id -> {layer, goal, replyFormat, status, history, eventsEl}
const specByCall = {};     // live tool_call id -> spec id
let archived = {};         // persisted spec id -> same shape as live (history = events)
const archivedByCall = {}; // persisted tool_call id -> spec id

function agentBubble(id) {
  const a = agents[id];
  const b = document.createElement('div');
  b.className = 'agent-bubble ' + (a.layer === 3 ? 'layer3' : '') + ' ' + a.status;
  b.id = 'bubble-' + id;
  b.title = (a.tool === 'background' ? 'Bg' : (a.layer === 3 ? 'L3' : 'L2')) + ': ' + a.goal;
  b.innerHTML = (a.tool === 'background' ? 'Bg' : 'L' + a.layer) + '<span class="agent-status-dot"></span>';
  b.addEventListener('click', () => openAgentModal(id));
  S.inTray(b);
  window.fungiMotion?.float?.(b);
  return b;
}
function setAgentStatus(id, st) {
  const a = agents[id];
  if (!a) return;
  a.status = st;
  const b = document.getElementById('bubble-' + id);
  if (b) {
    b.classList.remove('running', 'done', 'failed');
    b.classList.add(st);
    if (st === 'done' || st === 'failed' || st === 'aborted') {
      // bubbles are transient: only visible while the subagent runs
      setTimeout(() => { b.remove(); }, 1500);
    }
  }
  if (a.eventsEl) {
    const label = { done: '\u2714 finished', failed: '\u2718 failed', aborted: '\u25A0 stopped' }[st];
    if (label) a.eventsEl.insertAdjacentHTML('beforeend', '<div class="ev final-ev">' + label + '</div>');
  }
}
function agentEvent(id, ev) {
  const a = agents[id] || archived[id];
  if (!a || !a.eventsEl) return;
  const body = a.eventsEl;
  if (ev.type === 'text' || ev.type === 'reasoning') {
    let last = body.lastElementChild;
    if (!last || !last.classList.contains('stream-ev')) {
      body.insertAdjacentHTML('beforeend', '<div class="ev stream-ev"></div>');
      last = body.lastElementChild;
    }
    last.textContent += ev.content || '';
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'tool') {
    body.insertAdjacentHTML('beforeend', '<div class="ev tool-ev">\u{1F527} ' + escapeHtml(ev.content.name) + ' ' + escapeHtml((ev.content.args || '').slice(0, 120)) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'tool_result') {
    const text = String(ev.content.content || '');
    body.insertAdjacentHTML('beforeend', '<div class="ev">' + escapeHtml(text.slice(0, 400)) + (text.length > 400 ? '...' : '') + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'agent_spawn') {
    body.insertAdjacentHTML('beforeend', '<div class="ev tool-ev">\u{1F9E9} spawn L' + (ev.content.layer || '?') + ': ' + escapeHtml((ev.content.goal || '').slice(0, 120)) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'agent_status') {
    body.insertAdjacentHTML('beforeend', '<div class="ev">' + escapeHtml(ev.content.status) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'error') {
    body.insertAdjacentHTML('beforeend', '<div class="ev err-ev">\u26A0 ' + escapeHtml(ev.content) + '</div>');
    body.scrollTop = body.scrollHeight;
  }
}
function openAgentModal(id) {
  const a = agents[id] || archived[id];
  if (!a) return;
  const overlay = document.getElementById('agent-modal-overlay');
  overlay.innerHTML = '<div id="agent-modal">'
    + '<div class="agent-modal-head"><h3>' + (a.tool === 'background' ? 'Bg Command' : (a.layer === 3 ? 'L3 Worker' : 'L2 Task Agent')) + ' \u00B7 ' + escapeHtml((a.goal || '').slice(0, 60)) + '</h3>'
    + '<button id="agent-modal-close">\u2715</button></div>'
    + '<div class="agent-modal-taskspec"><b>Goal:</b> ' + escapeHtml(a.goal || '')
    + '<br><b>Reply format:</b> ' + escapeHtml(a.replyFormat || '(free)')
    + '<br><b>Status:</b> ' + escapeHtml(a.status || 'unknown') + '</div>'
    + '<div class="agent-modal-body"></div></div>';
  const bodyEl = overlay.querySelector('.agent-modal-body');
  a.eventsEl = bodyEl;
  (a.history || []).forEach(ev => agentEvent(id, ev));
  overlay.querySelector('#agent-modal-close').addEventListener('click', () => {
    overlay.classList.remove('show'); a.eventsEl = null;
  });
  overlay.classList.add('show');
}
function registerArchived(subs) {
  archived = {};
  for (const k of Object.keys(archivedByCall)) delete archivedByCall[k];
  (subs || []).forEach(r => {
    archived[r.id] = {
      layer: r.layer, tool: r.tool, goal: r.goal, replyFormat: r.reply_format,
    };
    if (r.call_id) archivedByCall[r.call_id] = r.id;
  });
}

/* ---------- send / stream ---------- */
let abortCtrl = null;
let stopRequested = false; // first Esc: graceful stop via /stop; second Esc: hard disconnect
let stopTimer = null;
let turn = null; // active turn: {sessionId, buffer, reasoning, reasoningOpen, tools, asks, errors}

async function send() {
  const text = input.value.trim();
  if (!text || processing) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  if (!currentSessionId) {
    try {
      const r = await fetch('/new', { method: 'POST', signal: abortCtrl.signal });
      const { id } = await r.json();
      setCurrentSession(id); loadSessions();
    } catch (e) {}
  }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] }; // chronological: reasoning/text/tool/ask/error
  rawMessages.push({ role: 'user', content: text });
  sessionDirty = true;
  turn.userText = text;
  input.value = ''; btn.disabled = true; status.textContent = 'Thinking...';
  _liveCount = 0; window.fungiMotion?.waveOn?.(status);
  renderTurnLive();
  await pumpStream('/chat', { message: text, sessionId: sid, side: MY_SIDE });
  if (currentSessionId === sid) input.focus();
}

/* Alt+R: rerun the failed/stopped turn with no new prompt. */
async function retryTurn() {
  if (processing || !currentSessionId) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] };
  sessionDirty = true;
  btn.disabled = true;
  status.textContent = 'Retrying...';
  _liveCount = 0; window.fungiMotion?.waveOn?.(status);
  renderTurnLive();
  await pumpStream('/retry', { sessionId: sid });
  if (currentSessionId === sid) input.focus();
}

/* Background subagent(s) finished: re-activate the session with a resume
   turn whose server-injected input is their reports. Polled; a report that
   lands while a turn runs is retried on a later tick (server 409s if busy). */
async function resumeIfPending() {
  if (processing || turn || !currentSessionId) return;
  try {
    const r = await fetch('/spawn-pending?sessionId=' + encodeURIComponent(currentSessionId));
    const d = await r.json();
    if (!d.pending) return;
    processing = true;
    abortCtrl = new AbortController(); stopRequested = false;
    turn = { sessionId: currentSessionId, entries: [] };
    sessionDirty = true;
    btn.disabled = true;
    status.textContent = 'Background task finished - continuing...';
    _liveCount = 0; window.fungiMotion?.waveOn?.(status);
    renderTurnLive();
    await pumpStream('/resume', { sessionId: currentSessionId });
  } catch (e) {}
}

/* A session's turn may still be running server-side (the client reloaded or
   switched away): /events replays what was missed, then streams live. */
function reattachIfRunning(sid) {
  if (processing || turn || !sid) return;
  const s = allSessions.find(x => x.id === sid);
  if (!s || !s.running) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  turn = { sessionId: sid, entries: [] };
  btn.disabled = true; status.textContent = 'Turn still running on the server...';
  _liveCount = 0; window.fungiMotion?.waveOn?.(status);
  renderTurnLive();
  pumpStream('/events?sessionId=' + encodeURIComponent(sid), null, 'GET');
}
/* A stream that dies without a done event (user hard-abort, network drop,
   server crash) leaves live-node cards whose content never reached a disk-
   backed render. Reconcile with the persisted transcript immediately, then
   resume streaming if the turn still runs server-side — otherwise the cards
   vanish at the next renderTurnLive (new send / reattach), with no done ever
   arriving to reload them. */
async function recoverAfterDrop(sid) {
  if (!sid) return;
  // Reconcile the session pane only when it is the pane on screen: a friend
  // conversation must survive a chat stream dying behind it.
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
    if (!resp.ok) {
      // No early return: skipping the tail would leave the send button
      // disabled forever and the next send dead (no stream, no recovery).
      status.textContent = 'Error: ' + resp.status; turn = null;
    } else {
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
    if (e.name === 'AbortError') status.textContent = 'Aborted.';
    else status.textContent = 'Error: ' + e.message;
    turn = null;
    recoverAfterDrop(sid);
  }
  if (turn) {
    // Stream ended without a done event (server died mid-turn): the UI used
    // to stay stuck on "Writing..." with a phantom live turn.
    status.textContent = 'Connection lost. Press Alt+R to retry.';
    turn = null;
    recoverAfterDrop(sid);
  }
  processing = false; btn.disabled = false;
  window.fungiMotion?.waveOff?.();
}

/* Turn events update the model first, then touch the DOM only when the
   turn's session is on screen — switching sessions mid-turn keeps the turn
   running in the background (no detached-node errors). */
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
      rateTok.feed();
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
      rateTok.feed();
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
      if (visible) { status.textContent = 'Running ' + (obj.content.name || 'tool') + '...'; renderTurnLive(); }
      break;
    case 'tool_result': {
      const rec = t.entries.find(x => x.kind === 'tool' && x.id === obj.content.id)
        || [...t.entries].reverse().find(x => x.kind === 'tool' && !x.result);
      if (rec) rec.result = obj.content.content;
      if (visible) { status.textContent = 'Thinking...'; const block = document.getElementById('tool-' + obj.content.id); if (block) FC.fillToolResult(block, obj.content.content); else renderTurnLive(); }
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
      // Server-side progress notes (e.g. "queued behind a still-running turn").
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
      // A finished session turn may only repaint its own pane: with a friend
      // conversation open, refresh the session list and leave #messages alone
      // (renderMessages enforces this too — this keeps the fetch out as well).
      const viewing = currentSessionId === t.sessionId && pane.is('session');
      const failed = t.entries.length && t.entries[t.entries.length - 1].kind === 'error';
      turn = null; abortCtrl = null; stopRequested = false;
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      if (t.aborted) {
        // stop means stop: background subagents/commands were killed - their
        // final agent_status events had no live stream to ride on
        Object.keys(agents).forEach(id => {
          if (agents[id].status === 'running') setAgentStatus(id, 'aborted');
        });
      }
      // The line under the input is a progress readout, so the end of a turn has
      // to clear it: nothing else does, and a session that never wrote a word
      // (the transfer session: one row, no model — §53) left "Thinking..." on
      // screen for good.
      status.textContent = t.aborted ? 'Aborted.' : (failed ? 'Turn failed.' : '');
      if (viewing) reloadSessionFromServer();
      else loadSessions(); // the finished turn landed in a background session
      break;
    }
  }
  if (visible && currentSessionId === t.sessionId) { if (stick) S.stick(); updateScrollBtn(); }
}

function updateLastText() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('text');
  if (idx < 0) return renderTurnLive();
  const el = document.getElementById('live-text-' + idx);
  if (el) { const stick = isNearBottom(msgs); el.innerHTML = marked.parse(FC.stripSilent(t.entries[idx].content)); if (stick) S.stick(); }
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
      ad.innerHTML = marked.parse(text);
      S.append(ad);
    } else if (e.kind === 'tool') {
      const d = FC.buildToolCard({ id: e.id, name: e.name, args: e.args, result: e.result }, { argsMax: 80 });
      d.classList.add('live-node');
      S.append(d);
      if (e.name === 'spawn' || e.name === 'background') FC.attachSpawnClick(d, e.id, callId => specByCall[callId] || archivedByCall[callId]);
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
  S.stick();
  // Motion: animate only nodes that appeared since the previous streaming
  // re-render — every text chunk rebuilds .live-node, re-animating all of
  // them would flicker (docs/webui-ux.md contract).
  var _live = S.el().querySelectorAll('.live-node');
  if (window.fungiMotion && !window.fungiMotion.reduced) {
    for (var _li = _liveCount; _li < _live.length; _li++) {
      var _n = _live[_li];
      window.fungiMotion.msgIn(_n, _n.classList.contains('user') ? 'user'
        : _n.classList.contains('assistant') ? 'assistant' : 'other');
    }
  }
  _liveCount = _live.length;
}
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  if (e.key === 'Escape' && abortCtrl && turn) {
    const sid = turn.sessionId || currentSessionId;
    if (!stopRequested) {
      // First Esc: ask the server to stop the turn and keep the stream open —
      // the persisted partial reply comes back via done -> reload.
      stopRequested = true;
      status.textContent = 'Stopping...';
      if (sid) fetch('/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: sid }) }).catch(() => {});
      stopTimer = setTimeout(() => { if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; } }, 15000);
    } else {
      // Second Esc: server is stuck (e.g. long tool call) — disconnect hard.
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      stopRequested = false;
      abortCtrl.abort(); abortCtrl = null;
      status.textContent = 'Aborted.';
    }
  }
});
btn.addEventListener("click", send);
document.addEventListener('keydown', e => {
  if (e.altKey && (e.key === 'r' || e.key === 'R')) {
    if (processing || !currentSessionId) return;
    if (document.getElementById('confirm-overlay').classList.contains('show')) return;
    e.preventDefault();
    retryTurn();
  }
});
document.getElementById('btn-browse').addEventListener('click', async () => {
  try {
    const r = await fetch('/pickfile', { method: 'POST' });
    const d = await r.json();
    if (d.path) { input.value = (input.value ? input.value + ' ' : '') + d.path; input.focus(); }
  } catch (e) {}
});
/* The header's model dropdown (spec §66): it loads the list itself and reports a
   switch on the status line — which the next turn overwrites, hence the colour
   the picker keeps on itself. */
const modelPicker = FC.mountModelPicker({
  note: t => { status.textContent = t; },
  labels: { switching: 'Switching to', ok: 'answers', bad: 'did not answer', served: 'served as' },
});
loadSessions().then(() => {
  // Reload keeps the current session: restore it and reattach if its turn
  // is still running server-side (the /events tape replays what was missed).
  let saved = null;
  try { saved = localStorage.getItem('fungi-session'); } catch (e) {}
  if (saved && allSessions.some(s => s.id === saved)) switchSession(saved);
});

/* ---------- ask card (Inquire) ---------- */
const Asks = FC.initAsks({
  http: FC,
  getTurn: () => turn,
  labels: { submit: 'Submit', required: 'Required', customPlaceholder: 'Or type your own...', answerPlaceholder: 'Your answer...', noAnswer: '\u23F3 No answer' },
});
const saveAskCardState = Asks.saveAskCardState;
const buildActiveAskCard = Asks.buildActiveAskCard;
const buildAnsweredAskCard = Asks.buildAnsweredAskCard;


/* ---------- pending card asks (consent / cross-host asks, out-of-band) ---------- */
const PendingAsks = FC.initPendingAsks({
  http: FC,
  banner: () => document.getElementById('asks-banner'),
  inlineHost: () => friendView,
  msgs: () => msgs,
  isNearBottom,
  displayOf,
  animatePlace: true,
  motion: {
    cardIn: el => window.fungiMotion?.askCardIn?.(el),
    resolved: (card, ok, settle) => {
      const M = window.fungiMotion;
      if (M && !M.reduced && M.askResolved) {
        M.askResolved(card, ok);
        setTimeout(settle, 1000); // let the stamp read before collapsing
      } else settle();
    },
  },
  labels: {
    fromFallback: 'remote host',
    consentPlaceholder: '自定义回复（可选，留空直接点允许/禁止）',
    consentHint: '· 需要长期放行？在好友会话顶部把滑块拨到「允许」',
    allow: '允许', deny: '禁止', consentSend: 'Send', submit: 'Submit', required: 'Required',
    customPlaceholder: 'Or type your own...', answerPlaceholder: 'Your answer...',
  },
});
const placeAskCards = PendingAsks.place, pollPendingAsks = PendingAsks.poll;
setInterval(pollPendingAsks, 3000);
setInterval(resumeIfPending, 3000);
resumeIfPending();

/* ---------- config modal ---------- */
(async () => {
  try {
    const r = await fetch('/config-status');
    const d = await r.json();
    if (!d.configured) {
      document.getElementById('config-overlay').classList.add('show');
      document.getElementById('cfg-save').addEventListener('click', async () => {
        const api_key = document.getElementById('cfg-apikey').value.trim();
        const endpoint = document.getElementById('cfg-endpoint').value.trim();
        const model = document.getElementById('cfg-model').value.trim();
        if (!api_key) {
          const err = document.getElementById('cfg-err');
          err.textContent = 'API key is required.'; err.style.display = 'block';
          return;
        }
        try {
          const r2 = await fetch('/configure', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key, endpoint, model }) });
          const d2 = await r2.json();
          if (d2.ok) {
            document.getElementById('config-overlay').classList.remove('show');
            if (modelPicker) modelPicker.load();  // §66: the name just typed is now in the dropdown
          }
        } catch (e) {}
      });
      document.getElementById('cfg-skip').addEventListener('click', () => {
        document.getElementById('config-overlay').classList.remove('show');
      });
    }
  } catch (e) {}
})();

/* ---------- friends: room members + read-only comm clone conversations ---------- */
let friendView = null; // host name while viewing a friend conversation (wire identity)
let allPeers = []; // [{name, display}] — display is the nickname, "" falls back to name
let lastFriendPayload = null; // rendered /comm-log JSON: skip no-change repaints

function peerName(p) { return typeof p === 'string' ? p : (p && p.name) || ''; }
function peerDisplay(p) { return typeof p === 'string' ? p : ((p && p.display) || peerName(p)); }
function displayOf(host) {
  for (const p of allPeers) if (peerName(p) === host) return peerDisplay(p);
  return host; // no display -> wire name
}

/* The only place that changes which view owns #messages, so the pane flag and
   `friendView` cannot drift apart: a friend host takes the pane, null gives it
   back to the session. */
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
  document.getElementById('friend-input-area').hidden = true;
  document.getElementById('friend-title').textContent = '';
  document.getElementById('friend-bar').classList.remove('visible');
  renderFriendList();
  if (wasViewing) {
    // setView wiped the message area without touching session state: re-render
    // the session that was on screen.
    if (currentSessionId) reloadSessionFromServer();
  }
}

async function loadPeers() {
  try {
    const r = await fetch('/peers');
    if (!r.ok) return;
    const d = await r.json();
    allPeers = d.peers || [];
    document.getElementById('friends-count').textContent = allPeers.length ? '(' + allPeers.length + ')' : '';
    renderFriendList();
    if (pane.is('friend')) refreshFriendChat();
  } catch (e) {}
}


function renderFriendList() {
  const list = document.getElementById('friend-list');
  const empty = document.getElementById('friends-empty');
  // Plain rebuild (no keyed Flip): /peers polls every 5s and the roster is
  // tiny, so a simple re-render reads calmer than list animation.
  list.querySelectorAll('.friend-row').forEach(r => r.remove());
  if (!allPeers.length) { empty.style.display = ''; return; }
  empty.style.display = 'none';
  allPeers.forEach(p => {
    const name = peerName(p);
    const row = document.createElement('div');
    row.className = 'friend-row' + (name === friendView ? ' active' : '');
    row.title = name;
    const n = (name === friendView) ? 0 : (MailUnread.byPeer()[name] || 0); // viewing the thread = no badge
    row.innerHTML = '<span class="friend-dot"></span><span class="friend-name">' + escapeHtml(peerDisplay(p)) + '</span>'
      + (n ? '<span class="friend-unread">' + n + '</span>' : '');
    row.addEventListener('click', () => openFriendChat(name));
    list.appendChild(row);
  });
}
async function openFriendChat(host) {
  /* Nav audit: this must only ever run from the friend-row click. If a user
     ever reports an uninvited jump into the friend view, window.__navlog holds
     every entry with the JS stack that triggered it. */
  try { (window.__navlog = window.__navlog || [])
    .push({ t: new Date().toISOString(), host, stack: new Error().stack }); } catch (e) {}
  setView(host);
  lastFriendPayload = null;
  lastTransferCount = -1;
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('friend-input-area').hidden = false;
  document.getElementById('friend-title').textContent = ' \u2014 @' + displayOf(host);
  document.getElementById('friend-bar').classList.add('visible');
  renderFriendList();
  initConsentSlider();
  try {
    const cm = await (await fetch('/consent-mode?host=' + encodeURIComponent(host))).json();
    setConsentSlider(cm.mode || 'ask');
  } catch (e) { setConsentSlider('ask'); }
  await refreshFriendChat();
}

function setConsentSlider(mode) {
  const s = document.getElementById('consent-slider');
  if (!s) return;
  s.dataset.mode = mode;
  s._mode = mode;
}

function initConsentSlider() {
  const s = document.getElementById('consent-slider');
  if (!s || s._wired) return;
  s._wired = true;
  const apply = clientX => {
    const rect = s.getBoundingClientRect();
    const mode = (clientX - rect.left) < rect.width / 2 ? 'allow' : 'ask';
    if (mode === s._mode || !pane.is('friend')) return;
    setConsentSlider(mode);
    fetch('/consent-mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: friendView, mode }) }).catch(() => {});
  };
  s.addEventListener('pointerdown', e => { s.setPointerCapture(e.pointerId); apply(e.clientX); });
  s.addEventListener('pointermove', e => { if (s.hasPointerCapture && s.hasPointerCapture(e.pointerId)) apply(e.clientX); });
}

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
    const r = await fetch('/comm-log?host=' + encodeURIComponent(host));
    if (!r.ok) return;
    const d = await r.json();
    if (friendView !== host) return; // raced a switch away: never paint here
    const payload = JSON.stringify(d);
    if (payload === lastFriendPayload) return; // unchanged: no flicker
    lastFriendPayload = payload;
    try {
      renderFriendChat(d);
    } catch (e) {
      // A paint that threw must not freeze the view: uncache the payload so
      // the next poll retries instead of skipping it as "unchanged" forever.
      lastFriendPayload = null;
      console.error('renderFriendChat:', e);
    }
  } catch (e) {}
}

/* the live tape renderer is shared too (FC.renderLiveEvents) */
function renderFriendChat(d) {
  const p = F;
  const messages = d.messages || [];
  const events = d.events || [];
  const live = d.live || [];
  const mails = d.mails || [];
  if (!messages.length && !events.length && !mails.length && !live.length) {
    // Nothing to show (a room with no traffic yet, or a wiped transcript):
    // decide before clearing — wiping the pane with an empty payload is what
    // made it look like the conversation had been lost.
    return;
  }
  p.clear();
  registerArchived(d.subagents || []);
  const stick = isNearBottom(msgs); // measure before the repaint replaces the DOM
  const mailBodies = new Set(mails.map(m => String(m.body || '').trim()).filter(Boolean));
  const opts = renderOpts({
    friend: true, side: FRIEND_SIDE, mailBodies, report: true, feedbackHost: friendView,
  });
  FC.renderTranscript(F, messages, d.asks || [], opts);
  var fileNodes = [];
  events.forEach(row => {
    let node = null;
    if (row.kind === 'transfer')
      node = p.add('friend-event file', '&#x1F4C4 ' + escapeHtml(row.text || 'file transfer'));
    else if (row.kind === 'task')
      node = p.add('friend-event task', '&#x1F4E5 delegated to ' + escapeHtml(row.dst || '?') + ': ' + escapeHtml((row.text || '').slice(0, 200)));
    else if (row.kind === 'result')
      node = p.add('friend-event result', '&#x2714 ' + escapeHtml(row.src || '?') + ' replied: ' + escapeHtml((row.text || '').slice(0, 200)));
    if (node) {
      FC.markTs(node, row.ts);   // envelopes carry the hub's timestamp
      FC.insertByTs(p, node, row.ts);
      if (row.kind === 'transfer') fileNodes.push(node);
    }
  });
  // New file landed since the previous poll -> spore burst from its card
  // (first render of a view replays history silently: lastTransferCount < 0).
  if (fileNodes.length > lastTransferCount && lastTransferCount >= 0) {
    window.fungiMotion?.spores?.(fileNodes[fileNodes.length - 1]);
  }
  lastTransferCount = fileNodes.length;
  MailUnread.markPeerRead(friendView); // seeing the thread IS reading it
  mails.forEach(m => {
    const body = String(m.body || '');
    const subject = String(m.subject || '');
    const html = (subject && subject !== '(no subject)' && subject !== body
      ? '<strong>' + escapeHtml(subject) + '</strong><br>' : '') + marked.parse(body);
    const mine = !!m.mine;
    const who = mine ? '我'
      : (String(m.from || '').endsWith(':human')
        ? '来自 ' + displayOf(m.peer) + ' 的用户'
        : displayOf(m.peer) + ' 的 Agent');
    const bubble = p.add('user' + (mine ? ' friend-mine' : ' friend-peer'), html);
    const lab = document.createElement('div');
    lab.className = 'human-label';
    lab.textContent = who;
    bubble.prepend(lab);
    FC.markTs(bubble, m.ts);   // mail rows carry the mailbox timestamp
    FC.insertByTs(p, bubble, m.ts);
  });
  FC.renderLiveEvents(live, p, opts);
  placeAskCards(); // re-seat pending asks after the transcript repaint
  if (stick) p.stick();
  updateScrollBtn();
}

setInterval(loadPeers, 5000);
/* The file-transfer session (§53) is written by the *other* device too: poll it
   while it is open so a file the phone sent shows up here without a reload.
   Which id that is comes from /sessions (entry.shuttle), never a copy here.

   Incremental on purpose (§55): only the rows added since our cursor, appended;
   with nothing new the DOM is not touched at all (a full repaint every 3 s made
   the scrollbar come and go) and the session list is never reloaded from here. */
async function pollShuttle() {
  if (document.hidden || !currentSessionId) return;
  const open = (allSessions || []).find(s => s.id === currentSessionId);
  if (!open || !open.shuttle) return;
  try {
    const r = await fetch('/session?id=' + encodeURIComponent(open.id) + '&after=' + rawMessages.length);
    if (!r.ok) return;
    const fresh = (await r.json()).messages || [];
    if (!fresh.length) return;
    rawMessages = rawMessages.concat(fresh);
    const stick = isNearBottom(msgs);
    FC.renderTranscript(S, fresh, [], renderOpts());
    if (stick && S.stick()) updateScrollBtn();
  } catch (e) {}
}
setInterval(pollShuttle, 3000);
loadPeers();

/* theme: light default, persisted; the switch flips html[data-theme] */
const themeRoot = document.documentElement;
function applyTheme(t) {
  themeRoot.dataset.theme = t;
  try { localStorage.setItem('fungi-theme', t); } catch (e) {}
}
try { applyTheme(localStorage.getItem('fungi-theme') === 'dark' ? 'dark' : 'light'); } catch (e) { applyTheme('light'); }
document.getElementById('theme-switch').addEventListener('click', function () {
  var next = themeRoot.dataset.theme === 'dark' ? 'light' : 'dark';
  if (window.fungiMotion && !window.fungiMotion.reduced && window.fungiMotion.themeTo) {
    window.fungiMotion.themeTo(next, this, function () { applyTheme(next); });
    return;
  }
  themeRoot.classList.add('theme-anim'); // cross-fade colors, then back to instant
  applyTheme(next);
  setTimeout(() => themeRoot.classList.remove('theme-anim'), 500);
});

/* ---------- session alerts (§61): 会话列表上的红点 ----------
   What a session wants (a turn finished / failed / is asking a question) rides
   two places: /sessions carries it, and the /session/seen answer carries the
   live map — see FC.initSessionAlerts, which also tells the server which
   session this page is showing (that is what stops the ring). Painted on
   creation AND on every reuse: rows are reconciled in place keyed by id, so a
   dot added once would otherwise never come off again. */
const ALERT_LABEL = { ask: 'Agent 在等你回答', done: '回答完毕', error: '出错了' };

function paintAlert(row, kind) {
  let dot = row.querySelector('.session-dot');
  if (!kind) { if (dot) dot.remove(); return; }
  if (!dot) {
    dot = document.createElement('span');
    dot.className = 'session-dot';
    row.querySelector('.session-row-meta').before(dot);
  }
  dot.title = ALERT_LABEL[kind] || '有新消息';
}

function applyAlerts(map) {
  let changed = false;
  allSessions.forEach(s => {
    const kind = map[s.id] || null;
    if ((s.alert || null) !== kind) { s.alert = kind; changed = true; }
  });
  return changed;  // the caller owns the repaint: one render per change, never two
}

const Alerts = FC.initSessionAlerts({
  http: FC,
  // 好友视图握着 #messages：那时这个页面没在看任何会话，也就不该替它挡铃。
  showing: () => (!friendView && currentSessionId) ? currentSessionId : '',
  onChange: map => { if (applyAlerts(map)) renderSessionList(); },
});
Alerts.start();

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
    const r = await fetch('/comm-send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ host: friendView }, payload)) });
    const d = await r.json();
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
document.getElementById('friend-browse').addEventListener('click', async () => {
  try {
    const r = await fetch('/pickfile', { method: 'POST' });
    const d = await r.json();
    if (d.path) await sendFileToFriend(d.path);
  } catch (e) {}
});
/* One file to one friend, with the progress modal: the bytes leave from this
   host (the file is already on its disk) and the job the modal polls follows
   them to the hub, then waits for the peer's own verdict (§49) — the modal only
   says "已收到" once the file is on their disk. See FC.initTransfer. */
async function sendFileToFriend(path) {
  try {
    await Xfer.sendOne('发送文件给 ' + displayOf(friendView), friendView, path);
  } catch (e) {
    // the modal already shows it; nothing else to say here
  }
  setTimeout(refreshFriendChat, 300);
}
