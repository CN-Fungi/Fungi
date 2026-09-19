/* Fungi shared web layer — one implementation for the pieces app.js (desktop)
   and m.js (mobile) used to maintain in parallel. Exposed as window.FungiCommon.
   Contracts preserved here:
   - pendingAskCards registration + placeAskCards re-mount (cards vanish mid-
     stream if either half is missing — see initPendingAsks).
   - buildToolCard fills #tool-<id> blocks by selector; fillToolResult patches
     .tool-result in place so streaming never re-renders the transcript. */
(function () {
  'use strict';

  /* Build marker: bump per web/ change so any WebUI instance can self-identify
     (console + window.__FUNGI_WEB_VER) — stale cache vs new server is otherwise
     indistinguishable from the outside. */
  window.__FUNGI_WEB_VER = 'web-delivery-verdict';
  try { console.info('[fungi-web]', window.__FUNGI_WEB_VER); } catch (e) {}
  /* ---------- http ---------- */
  /* One fetch wrapper. Mobile inits a token prefix + 403 hook; desktop inits
     nothing and gets plain relative fetches back. */
  let _prefix = p => p;
  let _onUnauthorized = null;

  function initHttp(opts) {
    opts = opts || {};
    if (opts.prefix) _prefix = opts.prefix;
    if (opts.onUnauthorized) _onUnauthorized = opts.onUnauthorized;
  }
  function url(path) { return _prefix(path); }
  async function fetchJSON(path, opts) {
    const r = await fetch(_prefix(path), opts);
    if (r.status === 403 && _onUnauthorized) { _onUnauthorized(); throw new Error('unauthorized'); }
    return r;
  }
  function postJSON(path, body) {
    return fetchJSON(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  /* ---------- helpers ---------- */
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function fmtDate(d, locale) {
    if (!d) return '';
    const diff = Date.now() - new Date(d).getTime();
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'now';
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h';
    const days = Math.floor(h / 24);
    if (days < 7) return days + 'd';
    return new Date(d).toLocaleDateString(locale || 'en-US', { month: 'short', day: 'numeric' });
  }
  function getSessionTitle(list, wrap, keep) {
    const u = (list || []).find(m => m.role === 'user');
    if (!u) return 'Empty';
    const t = String(u.content).replace(/\s+/g, ' ').trim();
    return t.length > wrap ? t.slice(0, keep) + '...' : t;
  }

  /* ---------- confirm modal (基建) ----------
     Same #confirm-overlay markup on both shells; desktop adds Escape/Enter
     keys + mousedown-outside close (Enter never confirms danger), mobile
     closes on backdrop tap. */
  let _confirmState = null;
  let _confirmDefaults = { title: 'Are you sure?', confirmText: 'OK', cancelText: 'Cancel' };

  function showConfirm(opts) {
    const overlay = document.getElementById('confirm-overlay');
    const ok = document.getElementById('confirm-ok');
    document.getElementById('confirm-title').textContent = opts.title || _confirmDefaults.title;
    document.getElementById('confirm-message').textContent = opts.message || '';
    ok.textContent = opts.confirmText || _confirmDefaults.confirmText;
    document.getElementById('confirm-cancel').textContent = opts.cancelText || _confirmDefaults.cancelText;
    ok.classList.toggle('danger', !!opts.danger);
    _confirmState = { onConfirm: opts.onConfirm, danger: !!opts.danger };
    overlay.classList.add('show');
    const cancel = document.getElementById('confirm-cancel');
    if (cancel.focus) cancel.focus();
  }
  function closeConfirm(confirmed) {
    const overlay = document.getElementById('confirm-overlay');
    if (!overlay.classList.contains('show')) return;
    overlay.classList.remove('show');
    const state = _confirmState;
    _confirmState = null;
    if (confirmed && state && state.onConfirm) state.onConfirm();
  }
  function initConfirmModal(opts) {
    opts = opts || {};
    _confirmDefaults = {
      title: opts.title || 'Are you sure?',
      confirmText: opts.confirmText || 'OK',
      cancelText: opts.cancelText || 'Cancel',
    };
    const overlay = document.getElementById('confirm-overlay');
    document.getElementById('confirm-ok').addEventListener('click', () => closeConfirm(true));
    document.getElementById('confirm-cancel').addEventListener('click', () => closeConfirm(false));
    if (opts.keyboard === 'desktop') {
      overlay.addEventListener('mousedown', e => { if (e.target === overlay) closeConfirm(false); });
      document.addEventListener('keydown', e => {
        if (!overlay.classList.contains('show')) return;
        if (e.key === 'Escape') { e.preventDefault(); closeConfirm(false); }
        // Enter confirms non-danger prompts only; destructive actions need a real click.
        if (e.key === 'Enter' && _confirmState && !_confirmState.danger) {
          e.preventDefault();
          closeConfirm(true);
        }
      });
    } else {
      overlay.addEventListener('click', e => { if (e.target.id === 'confirm-overlay') closeConfirm(false); });
    }
  }

  /* ---------- send-file progress modal (基建) ----------
     One modal for every file the user sends. The page never sees the bytes:
     the browser mints a job id, hands it to POST /comm-send, and polls
     /transfer-progress while the SERVER moves the upload to the hub
     (fungi/xfer.py). The phone has one hop more — the browser's own push to
     this host — which XHR measures client-side, so it gets a step of its own.
     The last step belongs to the OTHER end (§49): the upload finishing only
     means the room has the bytes, so the modal waits for the peer's verdict
     (landed with a path, or refused/truncated with a reason) and never calls a
     send "done" on the strength of its own upload.
     Both shells pass their step labels; every step shows its own bar + note.
     A shell without the modal markup still sends — the flows just skip the
     card (open() returns false and they fall back to the bare request). */
  function initTransfer(opts) {
    opts = opts || {};
    const http = opts.http || {};
    const MB = 1024 * 1024;
    let steps = [];
    let autoClose = null;
    let cancelled = false;  // the user closed the modal: stop watching the send

    function human(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + ' B';
      if (n < MB) return (n / 1024).toFixed(1) + ' KB';
      return (n / MB).toFixed(1) + ' MB';
    }
    function row(label) {
      const el = document.createElement('div');
      el.className = 'xf-step';
      el.innerHTML = '<div class="xf-lab"></div><div class="xf-bar"><i></i></div><div class="xf-note"></div>';
      el.querySelector('.xf-lab').textContent = label;
      return { el, bar: el.querySelector('.xf-bar > i'), note: el.querySelector('.xf-note') };
    }
    function open(title, labels) {
      const overlay = document.getElementById('xfer-overlay');
      if (!overlay) return false;
      if (autoClose) { clearTimeout(autoClose); autoClose = null; }
      cancelled = false;  // a new send: this modal is watching again
      document.getElementById('xfer-title').textContent = title;
      const box = document.getElementById('xfer-steps');
      box.innerHTML = '';
      steps = labels.map(label => {
        const s = row(label);
        s.note.textContent = '等待…';
        box.appendChild(s.el);
        return s;
      });
      overlay.classList.add('show');
      return true;
    }
    function progress(i, done, total) {
      const s = steps[i];
      if (!s) return;
      const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
      s.bar.style.width = pct + '%';
      s.note.textContent = total ? human(done) + ' / ' + human(total) + ' · ' + pct + '%' : human(done);
    }
    function note(i, text) {
      const s = steps[i];
      if (s) s.note.textContent = text;
    }
    function markDone(i) {
      const s = steps[i];
      if (!s) return;
      s.bar.style.width = '100%';
      s.el.classList.add('done');
    }
    function finish(text) {
      // every hop is through by the time a flow finishes: close them all, and
      // say the last word on the last one
      steps.forEach((s, i) => {
        s.bar.style.width = '100%';
        s.el.classList.add('done');
        if (i === steps.length - 1) s.note.textContent = text || '完成';
      });
      autoClose = setTimeout(close, 900); // 完成后自动关闭
    }
    function fail(text, at) {
      const s = steps[at === undefined ? steps.length - 1 : at];
      if (s) {
        s.note.textContent = text || '失败';
        s.el.classList.add('failed');
      }
    }
    function close() {
      if (autoClose) { clearTimeout(autoClose); autoClose = null; }
      // the delivery pump checks this on its next tick and ends itself; the
      // timer is never cleared from here (that would leave it unwakeable)
      cancelled = true;
      const overlay = document.getElementById('xfer-overlay');
      if (overlay) overlay.classList.remove('show');
    }
    document.getElementById('xfer-close')?.addEventListener('click', close);

    /* hop 1 (phone): push the picked file to this host's inbox.
       One connection does not fill a phone's uplink on its own, and a window is
       also what makes a dropped one cheap, so the file is cut into windows that
       travel at once and the host puts them together (§51). Raw bytes with the
       name in a header either way — the host streams them to disk, so a phone
       video of any size lands (no cap, §48), and a percent-encoded name survives
       the ascii-only header line. The host has the last word: it lands the file
       only when every byte is covered, and answers with the ranges it is still
       missing, so a repair re-sends exactly those instead of guessing from what
       the socket said it had handed over. A host that does not take windows
       (anything before §51) answers the one-and-only probe and gets one body,
       which is exactly what this used to send. */
    const PART_MIN = 4 * 1024 * 1024;  // keep in step with fungi/server.py
    const PART_COUNT = 4;
    const PART_TRIES = 3;
    const PART_ROUNDS = 3;  // send, then repair what the host reports missing
    let partsAsked = null;  // the capability answer, asked once per page

    /* `?parts=N` on the page URL pins how many windows may travel at once, in
       both directions — the measurement knob (§51/§52), where 1 is the
       single-connection shape this had before any of it. */
    function pinned() {
      return Number(new URLSearchParams(location.search).get('parts')) || 0;
    }

    function partsFor(file) {
      const asked = pinned() || PART_COUNT;
      return Math.max(1, Math.min(Math.floor(asked), Math.floor(file.size / PART_MIN)));
    }

    function sendBody(target, blob, name, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', target);
        xhr.setRequestHeader('X-Fungi-Filename', encodeURIComponent(name));
        if (xhr.upload && onProgress) {
          xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded); };
        }
        xhr.onload = () => {
          if (xhr.status === 403) {
            if (_onUnauthorized) _onUnauthorized();
            reject(new Error('unauthorized'));
            return;
          }
          let d = {};
          try { d = JSON.parse(xhr.responseText || '{}'); } catch (e) {}
          resolve({ status: xhr.status, body: d });
        };
        xhr.onerror = () => reject(new Error('upload failed'));
        xhr.send(blob);
      });
    }

    function uploadWhole(file, onProgress) {
      return sendBody(url('/upload'), file, file.name, n => onProgress && onProgress(n, file.size))
        .then(r => {
          if (r.status === 200 && r.body.path) return r.body.path;
          throw new Error(r.body.error || ('upload failed: HTTP ' + r.status));
        });
    }

    function uploadWindows(file, windows, onProgress) {
      const sid = 'u' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      const total = file.size;
      const peak = [];  // what each window has most recently reported: the sum only grows
      const drawn = () => {
        if (!onProgress) return;
        let done = 0;
        for (const n of peak) done += n || 0;
        onProgress(Math.min(done, total), total);
      };
      const sendWindow = (index, lo, hi, tries) =>
        sendBody(
          // the query goes INSIDE the path: the shell's prefix appends its own
          // token with `?` or `&`, and a second `?` would swallow the window
          url('/upload?sid=' + sid + '&offset=' + lo + '&size=' + total),
          file.slice(lo, hi),
          file.name,
          n => { peak[index] = Math.max(peak[index] || 0, n); drawn(); }
        ).then(r => {
          if (r.status === 200) return r.body;
          if (tries > 0) {
            peak[index] = 0;  // this window starts over, so its count does too
            return new Promise(res => setTimeout(res, tries * 500)).then(() =>
              sendWindow(index, lo, hi, tries - 1)
            );
          }
          throw new Error(r.body.error || ('upload failed: HTTP ' + r.status));
        });

      const round = (ranges, base) =>
        Promise.all(ranges.map(([lo, hi], i) => sendWindow(base + i, lo, hi, PART_TRIES)));

      const repair = n => {
        if (n >= PART_ROUNDS) return Promise.reject(new Error('上传未完成：还有分片没到'));
        return http.fetchJSON('/upload?sid=' + sid)
          .then(r => {
            if (r.ok || r.status === 404) return r.json();
            throw new Error('upload failed: HTTP ' + r.status);
          })
          .then(d => {
            if (d.done && d.path) return d.path;
            // A session the host no longer knows (it restarted, or swept it as
            // stale) is not something to report: sending everything again
            // reopens it under the same id.
            const ranges = (d.missing || [[0, total]]).filter(g => g[1] > g[0]);
            if (!ranges.length) throw new Error(d.error || '上传未完成');
            return round(ranges, n * 1000).then(() => repair(n + 1));
          });
      };

      return round(windows, 0).then(replies => {
        const landed = replies.filter(r => r && r.done && r.path).pop();
        return landed ? landed.path : repair(1);
      });
    }

    function upload(file, onProgress) {
      const count = partsFor(file);
      if (count < 2) return uploadWhole(file, onProgress);
      if (partsAsked === null) {
        partsAsked = http.fetchJSON('/upload')
          .then(r => (r.ok ? r.json() : {}))
          .then(d => !!d.parts)
          .catch(() => false);
      }
      return partsAsked.then(ok => {
        if (!ok) return uploadWhole(file, onProgress);
        const step = Math.ceil(file.size / count);
        const windows = [];
        for (let lo = 0; lo < file.size; lo += step) {
          windows.push([lo, Math.min(lo + step, file.size)]);
        }
        return uploadWindows(file, windows, onProgress);
      });
    }

    /* the other way round: PC -> phone (§52). The same cut — windows travel at
       once, and a window that dies resumes from the byte it reached — but the
       pieces are put together in the page, because a phone cannot write at an
       offset over plain HTTP (the File System Access API wants a secure
       context, and this page is http:// on the LAN). So the assembly costs
       memory, and a file too big for that goes to the browser's own downloader
       in one connection, which streams to disk (no progress, no memory). */
    const PULL_MIN = 4 * 1024 * 1024;
    const PULL_COUNT = 4;
    const PULL_TRIES = 3;
    const PULL_MEM_MAX = 192 * 1024 * 1024;  // what a phone should hold in RAM

    function saveBlob(blob, name) {
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = name || 'download.bin';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 60000);  // the save needs a moment
    }

    function saveViaBrowser(path) {
      const a = document.createElement('a');
      a.href = url('/download?path=' + encodeURIComponent(path));
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    function pullWindows(path, meta, windows, onProgress) {
      const total = meta.size;
      const got = new Array(windows.length).fill(0);
      const pieces = windows.map(() => []);
      const drawn = () => {
        if (!onProgress) return;
        let done = 0;
        for (const n of got) done += n;
        onProgress(Math.min(done, total), total);
      };
      const pull = (index, lo, hi, tries) =>
        new Promise((resolve, reject) => {
          const from = lo + got[index];
          fetch(url('/download?path=' + encodeURIComponent(path)), {
            headers: { Range: 'bytes=' + from + '-' + (hi - 1) },
          })
            .then(resp => {
              if (!resp.ok) throw new Error('HTTP ' + resp.status);
              // A host that ignores Range answers 200 with the whole file: only
              // a window that starts at byte zero can use that.
              if (resp.status === 200 && index !== 0) throw new Error('range ignored');
              const reader = resp.body.getReader();
              const pump = () =>
                reader.read().then(({ done, value }) => {
                  if (done) return;
                  pieces[index].push(value);
                  got[index] += value.length;
                  drawn();
                  return pump();
                });
              return pump();
            })
            .then(() => {
              if (lo + got[index] >= hi) return;
              throw new Error('中断在 ' + (lo + got[index]) + ' 字节');
            })
            .catch(err => {
              if (tries > 0) {
                return new Promise(res => setTimeout(res, tries * 500)).then(() =>
                  pull(index, lo, hi, tries - 1)
                );
              }
              throw err;
            })
            .then(resolve, reject);
        });

      return Promise.all(windows.map(([lo, hi], i) => pull(i, lo, hi, PULL_TRIES))).then(() => {
        const reached = got.reduce((a, b) => a + b, 0);
        if (reached !== total) {
          // Never hand over a file that is not whole: this is the §49 rule at
          // the last hop, and the only check the phone itself can make.
          throw new Error('只收到 ' + reached + ' / ' + total + ' 字节');
        }
        saveBlob(new Blob(pieces.flat(), { type: 'application/octet-stream' }), meta.name);
        return meta.name;
      });
    }

    function download(path, onProgress) {
      return http.fetchJSON('/download?meta=1&path=' + encodeURIComponent(path))
        .then(r => r.json())
        .then(meta => {
          if (!meta.ok) throw new Error(meta.error || '取不到这个文件');
          const total = Number(meta.size) || 0;
          const count = Math.max(1, Math.min(pinned() || PULL_COUNT, Math.floor(total / PULL_MIN)));
          if (count < 2 || total > PULL_MEM_MAX) {
            saveViaBrowser(path);  // the browser's downloader: one connection, to disk
            return '交给浏览器下载';
          }
          const step = Math.ceil(total / count);
          const windows = [];
          for (let lo = 0; lo < total; lo += step) {
            windows.push([lo, Math.min(lo + step, total)]);
          }
          return pullWindows(path, meta, windows, onProgress);
        });
    }

    /* hop 2 (both): this host's copy goes out to the peer through the hub.
       The POST answers only when the upload is done, so polling it is what
       feeds the bar; the job record is authoritative once the reply lands.
       The pump is never cancelled from outside — the job settling, or the user
       closing the modal, ends it on its own next tick (clearing its timer would
       leave the promise with nothing to wake it).

       It is DETACHED, deliberately: the page's own flow resolves at the end of
       the upload (the chat can refresh, the picker returns) while the modal
       keeps watching for the peer's verdict. Awaiting the peer here would hang
       the caller until the other human clicks — or forever (§49). */
    function sendToPeer(host, path, upStep, onProgress) {
      const job = 'j-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      // the job id on the element: the one handle onto the server-side record,
      // for a console and for the browser tests to read back
      const overlay = document.getElementById('xfer-overlay');
      if (overlay) overlay.dataset.job = job;
      let uploadFailed = false;
      const pump = () => {
        if (cancelled || uploadFailed) return;
        http.fetchJSON('/transfer-progress?id=' + encodeURIComponent(job))
          .then(r => r.json())
          .then(j => {
            if (cancelled) return;
            if (j.state === 'sent') {
              // every byte is in the room; what is left is the peer's own step,
              // and the hub's count of what it has handed over is the only real
              // progress for it — without it this step looked dead for minutes
              // on a 1 GB file (§49)
              markDone(upStep);
              if (j.delivery_total) progress(upStep + 1, j.delivered || 0, j.delivery_total);
              else note(upStep + 1, '等待对方接收…');
              setTimeout(pump, 1000);
              return;
            }
            if (j.state === 'done' || j.state === 'error') return settle(upStep, j);
            if (j.done && onProgress) onProgress(j.done, j.total);
            setTimeout(pump, 200);
          })
          .catch(() => { setTimeout(pump, 400); });
      };
      setTimeout(pump, 200);
      return http.postJSON('/comm-send', { host: host, file: path, job: job })
        .then(r => r.json())
        .then(
          d => {
            if (d.error) { uploadFailed = true; throw new Error(d.error); }
            return d;
          },
          e => { uploadFailed = true; throw e; }
        );
    }

    /* The verdict is the only ending the sender may claim: "对方已收到" needs
       the landing, and a failure has to stay on screen with its reason. */
    function settle(upStep, j) {
      const last = upStep + 1;
      if (cancelled) return;
      if (j.state === 'done') {
        markDone(last);
        finish(j.saved ? '对方已收到：' + j.saved : '对方已收到');
      } else if (j.state === 'error') {
        fail(j.error || '对方没有收下', last);
      } else {
        // still receiving when the modal stopped watching — never claim it landed
        note(last, '对方仍在接收，结果会出现在会话里');
      }
    }

    /* the two flows the shells use */
    function sendOne(title, host, path) {
      // a refused upload fails on ITS step, with the server's own words
      const send = () =>
        sendToPeer(host, path, 0, (done, total) => progress(0, done, total)).catch(e => {
          fail(e.message || String(e), 0);
          throw e;
        });
      if (!open(title, ['① 上传到房间', '② 对方接收'])) return send();
      note(0, '正在发送…');
      return send();
    }
    function sendFromPhone(title, host, file) {
      const send = (path, onProgress) =>
        sendToPeer(host, path, 1, onProgress).catch(e => {
          fail(e.message || String(e), 1);
          throw e;
        });
      if (!open(title, ['① 上传到电脑', '② 上传到房间', '③ 对方接收'])) {
        return upload(file, null).then(path => send(path, null));
      }
      note(0, '正在上传…');
      return upload(file, (done, total) => progress(0, done, total)).then(
        path => {
          progress(0, file.size, file.size);  // ① done, with the file's real bytes
          markDone(0);
          note(1, '正在送往对方…');
          return send(path, (done, total) => progress(1, done, total));
        },
        e => {  // browser -> this host never made it
          fail(e.message || String(e), 0);
          throw e;
        }
      );
    }

    return { open, close, finish, fail, progress, note, upload, download, sendToPeer, sendOne, sendFromPhone };
  }

  /* ---------- tool card rendering ----------
     spec: { id?, name, args?, result? } — id renders the #tool-<id> hook the
     streaming tool_result patches via fillToolResult; result pre-fills the
     output (live turn replay path). opts.argsMax: args snippet cap (80 desktop
     / 60 mobile — trimmed content, both styles come from CSS). */
  function buildToolCard(spec, opts) {
    const argsMax = (opts && opts.argsMax) || 80;
    const d = document.createElement('div');
    d.className = 'msg tool';
    if (spec.id) d.id = 'tool-' + spec.id;
    const args = spec.args || '';
    const argsHtml = args ? ' <code>' + escapeHtml(args.length > argsMax ? args.slice(0, argsMax) + '...' : args) + '</code>' : '';
    d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(spec.name || 'tool') + argsHtml + '</div><div class="tool-result">' + (spec.result ? '<pre>' + escapeHtml(spec.result) + '</pre>' : '') + '</div>';
    if (spec.result && opts && opts.fileLink) linkifyPaths(d, opts.fileLink);
    return d;
  }
  function fillToolResult(block, content, onPick) {
    const out = block.querySelector('.tool-result');
    out.innerHTML = '<pre>' + escapeHtml(content) + '</pre>';
    linkifyPaths(out, onPick);  // tool output is where the paths show up (§52)
  }
  /* Spawn/background cards deep-link into the client's agent replay store.
     lookup(callId) -> spec id; the modal itself stays client-side (different
     data shapes on desktop vs mobile). */
  function attachSpawnClick(el, callId, lookup, title) {
    el.classList.add('spawn-block');
    el.title = title || 'Click to view subagent details';
    el.addEventListener('click', () => {
      const id = lookup(callId);
      if (id && typeof window.openAgentModal === 'function') window.openAgentModal(id);
    });
  }

  /* ---------- ask cards (in-turn Inquire) ---------- */
  /* labels: { submit, required, customPlaceholder, answerPlaceholder, noAnswer } */
  function initAsks(opts) {
    const http = opts.http;
    const t = opts.labels;

    function saveAskCardState() {
      const card = document.getElementById('ask-card');
      if (!card) return null;
      return (card._askQuestions || []).map((q, qi) => {
        const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
        const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
        return { sel: sel ? sel.querySelector('b').textContent : null, val: inp ? inp.value : '' };
      });
    }

    function askQuestionHtml(q, qi) {
      const opts2 = (q.options || []).map(o =>
        '<button type="button" class="ask-option" data-q="' + qi + '"><b>' + escapeHtml(o.label) + '</b>'
        + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
      return '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
        + '<div class="ask-options">' + opts2 + '</div>'
        + (((q.options || []).length === 0 || q.allow_custom !== false)
          ? '<input class="ask-input" data-q="' + qi + '" placeholder="' + ((q.options || []).length ? t.customPlaceholder : t.answerPlaceholder) + '">'
          : '');
    }

    /* Clicking the selected option clears it: an answer may be typed text alone
       (see collectVals), so every choice has to be undoable — a second click on
       the picked item must not be a no-op. */
    function bindOptionToggles(card) {
      card.querySelectorAll('.ask-option').forEach(btn => {
        btn.addEventListener('click', () => {
          const was = btn.classList.contains('selected');
          card.querySelectorAll('.ask-option[data-q="' + btn.dataset.q + '"]').forEach(b => b.classList.remove('selected'));
          if (!was) btn.classList.add('selected');
        });
      });
    }

    /* "Required" normally lands in the input's placeholder; a question that
       allow_custom:false left option-only has no input to focus, so highlight
       its block for a beat instead. */
    function flagMissing(block) {
      if (!block) return;
      block.classList.add('ask-missing');
      setTimeout(() => block.classList.remove('ask-missing'), 1600);
    }

    /* Read every answer off a card, or null after flagging the first
       unanswered question. Selected option + typed note compose ("Label:
       note"); either alone stands as-is. No silent wiping in either
       direction. */
    function collectVals(card, questions) {
      const vals = [];
      for (let qi = 0; qi < questions.length; qi++) {
        const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
        const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
        const label = sel ? sel.querySelector('b').textContent : '';
        const note = inp ? inp.value.trim() : '';
        const v = label && note ? label + ': ' + note : (label || note);
        if (!v) {
          if (inp) { inp.focus(); inp.placeholder = t.required; }
          else flagMissing(card.querySelectorAll('.ask-block')[qi]);
          return null;
        }
        vals.push(v);
      }
      return vals;
    }

    function buildActiveAskCard(a, saved) {
      const card = document.createElement('div');
      card.className = 'msg ask-card live-node'; card.id = 'ask-card';
      card._askId = a.id; card._askQuestions = a.questions;
      card.innerHTML = a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
        + '<div class="ask-actions"><button id="ask-submit">' + escapeHtml(t.submit) + '</button></div>';
      (saved || []).forEach((s, qi) => {
        if (s.sel) card.querySelectorAll('.ask-option[data-q="' + qi + '"]').forEach(b => {
          if (b.querySelector('b').textContent === s.sel) b.classList.add('selected');
        });
        if (s.val) { const inp = card.querySelector('.ask-input[data-q="' + qi + '"]'); if (inp) inp.value = s.val; }
      });
      bindOptionToggles(card);
      card.querySelectorAll('.ask-input').forEach(inp => {
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') collectAskAnswers(card); });
      });
      card.querySelector('#ask-submit').addEventListener('click', () => collectAskAnswers(card));
      return card;
    }

    function buildAnsweredAskCard(rec) {
      const card = document.createElement('div');
      card.className = 'msg ask-card answered';
      if (rec.id) card.dataset.askId = rec.id;
      const qs = rec.questions || [];
      const ans = Array.isArray(rec.answers) ? rec.answers : (rec.answers != null ? [rec.answers] : null);
      card.innerHTML = qs.map((q, qi) => {
        const labels = (q.options || []).map(o => o.label);
        const a = ans ? (ans[qi] ?? '') : '';
        const isCustom = a && !labels.includes(a);
        const opts2 = (q.options || []).map(o =>
          '<button type="button" class="ask-option' + (!isCustom && o.label === a ? ' selected' : '') + '" style="cursor:default"><b>' + escapeHtml(o.label) + '</b>'
          + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
        let row = '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
          + '<div class="ask-options">' + opts2 + '</div>';
        if (rec.status && rec.status !== 'answered') row += '<div class="ask-a">' + t.noAnswer + '</div>';
        else if (isCustom || !labels.length) row += '<div class="ask-a">' + (a === 'no' ? '\u274C ' : '\u2705 ') + escapeHtml(a) + '</div>';
        return '<div class="ask-block">' + row + '</div>';
      }).join('');
      return card;
    }

    function collectAskAnswers(card) {
      const qs = card._askQuestions || [];
      const vals = collectVals(card, qs);
      if (!vals) return;
      const turn = opts.getTurn ? opts.getTurn() : null;
      const rec = (turn && turn.entries || []).find(x => x.kind === 'ask' && x.id === card._askId);
      if (rec) { rec.answers = vals; rec.active = false; }
      const answered = buildAnsweredAskCard({ questions: qs, answers: vals, status: 'answered' });
      answered.classList.add('live-node');
      card.replaceWith(answered);
      http.postJSON('/answer', { id: card._askId, value: vals }).catch(() => {});
    }

    return { saveAskCardState, askQuestionHtml, buildActiveAskCard, buildAnsweredAskCard, bindOptionToggles, collectVals };
  }

  /* ---------- pending card asks (consent / cross-host asks, out-of-band) ----------
     Contract: every new ask is registered in pendingAskCards AND re-mounted by
     placeAskCards after every full repaint. Missing either makes cards vanish
     mid-stream. ctx: {
       http,                 // {fetchJSON, postJSON}
       banner,               // () -> #asks-banner element
       inlineHost,           // () -> host name while a friend view is open, else null
       msgs,                 // () -> #messages element
       isNearBottom,         // fn(el) -> bool (client-specific threshold)
       displayOf,            // fn(host) -> display name
       motion,               // { cardIn?(el), resolved?(card, ok, settle) } — both optional
       animatePlace,         // animate remounts during place() (desktop yes, mobile no)
       labels: { fromFallback, consentPlaceholder, consentHint?, allow, deny, consentSend, submit, required, customPlaceholder, answerPlaceholder }
     } */
  function initPendingAsks(ctx) {
    const asks = initAsks({ http: ctx.http, labels: { submit: ctx.labels.submit, required: ctx.labels.required, customPlaceholder: ctx.labels.customPlaceholder, answerPlaceholder: ctx.labels.answerPlaceholder, noAnswer: '' } });
    const pendingAskIds = new Set();
    const pendingAskCards = new Map(); // ask id -> {rec, el} while the card is live
    const resolvedAskCards = new Map(); // answered card asks: verdict stays visible

    function mount(el, conv, animate) {
      const inline = ctx.inlineHost() && conv === ctx.inlineHost();
      if (inline) {
        const stick = ctx.isNearBottom(ctx.msgs()); // measure before the card changes layout
        ctx.msgs().appendChild(el);
        if (stick) ctx.msgs().scrollTop = ctx.msgs().scrollHeight;
      } else if (el.parentElement !== ctx.banner()) {
        ctx.banner().appendChild(el);
      }
      if (animate && ctx.motion && ctx.motion.cardIn) ctx.motion.cardIn(el);
    }

    function place() {
      // A pending ask belongs to the conversation that raised it: inline in the
      // matching open friend view, otherwise the global banner. Answered cards
      // follow the same rule until the durable transcript record (saved by the
      // server) renders — that copy then replaces the floating one.
      pendingAskCards.forEach(({ rec, el }) => mount(el, rec.conv, ctx.animatePlace));
      resolvedAskCards.forEach(({ rec, el }) => {
        if (ctx.inlineHost() && rec.conv === ctx.inlineHost()) {
          if (ctx.msgs().querySelector('.ask-card[data-ask-id="' + rec.id + '"]')) {
            el.remove();
            resolvedAskCards.delete(rec.id);
          } else mount(el, rec.conv, false);
        } else if (el.parentElement !== ctx.banner()) {
          ctx.banner().appendChild(el);
        }
      });
    }

    function buildPendingAskCard(a) {
      const card = document.createElement('div');
      card.className = 'msg ask-card pending-ask';
      const fromRaw = String(a.from || a.src || ctx.labels.fromFallback);
      const from = escapeHtml(ctx.displayOf(fromRaw.split(':')[0]));
      if (a.kind === 'consent') {
        const q = a.questions[0] || { question: '(consent request)' };
        card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + ' \u00b7 consent</div>'
          + '<div class="ask-block"><div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div></div>'
          + '<div class="ask-consent-actions"><input placeholder="' + escapeHtml(ctx.labels.consentPlaceholder) + '">'
          + '<button class="ask-allow">' + escapeHtml(ctx.labels.allow) + '</button>'
          + '<button class="ask-deny">' + escapeHtml(ctx.labels.deny) + '</button><button class="ask-send">' + escapeHtml(ctx.labels.consentSend) + '</button></div>'
          + (ctx.labels.consentHint ? '<div class="ask-hint">' + escapeHtml(ctx.labels.consentHint) + '</div>' : '');
        const inp = card.querySelector('input');
        const send = v => answerPendingAsk(a, card, v);
        card.querySelector('.ask-allow').addEventListener('click', () => send(inp.value.trim() ? 'yes: ' + inp.value.trim() : 'yes'));
        card.querySelector('.ask-deny').addEventListener('click', () => send(inp.value.trim() ? 'no: ' + inp.value.trim() : 'no'));
        const custom = () => { if (inp.value.trim()) send(inp.value.trim()); else inp.focus(); };
        card.querySelector('.ask-consent-actions input').addEventListener('keydown', e => { if (e.key === 'Enter') custom(); });
        card.querySelector('.ask-send').addEventListener('click', custom);
      } else {
        card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + '</div>'
          + a.questions.map((q, qi) => '<div class="ask-block">' + asks.askQuestionHtml(q, qi) + '</div>').join('')
          + '<div class="ask-actions"><button class="ask-send">' + escapeHtml(ctx.labels.submit) + '</button></div>';
        asks.bindOptionToggles(card);
        card.querySelector('.ask-send').addEventListener('click', () => {
          const vals = asks.collectVals(card, a.questions || []);
          if (!vals) return;
          answerPendingAsk(a, card, vals.length === 1 ? vals[0] : vals);
        });
      }
      return card;
    }

    function answerPendingAsk(a, card, value) {
      ctx.http.postJSON('/answer', { id: a.id, value }).catch(() => {});
      pendingAskIds.delete(a.id);
      pendingAskCards.delete(a.id);
      // The verdict stays as a real answered card (same builder as the in-turn
      // replay path) instead of evaporating after 8 seconds; the server also
      // files it into the friend transcript so it survives reload.
      const done = asks.buildAnsweredAskCard({
        id: a.id,
        questions: a.questions,
        answers: Array.isArray(value) ? value : [value],
        status: 'answered',
      });
      const settle = () => {
        card.replaceWith(done);
        resolvedAskCards.set(a.id, { rec: a, el: done });
      };
      if (ctx.motion && ctx.motion.resolved) ctx.motion.resolved(card, value !== 'no', settle);
      else settle();
    }

    async function poll() {
      try {
        const d = await (await ctx.http.fetchJSON('/asks')).json();
        (d.asks || []).forEach(a => {
          if (pendingAskIds.has(a.id)) return;
          pendingAskIds.add(a.id);
          const el = buildPendingAskCard(a);
          pendingAskCards.set(a.id, { rec: a, el });
          mount(el, a.conv, true);
        });
      } catch (e) {}
    }

    return { poll, place, pendingAskIds, pendingAskCards, resolvedAskCards };
  }

  /* ---------- mail unread (留言未读计数, per-peer) ----------
     Polls this host's mailbox and exposes per-peer unread counts for the
     friend list. Reading happens in the friend view: markPeerRead clears
     everything a peer sent.
     opts: { http, onChange(map) }
     Backend: GET /mail -> {host, mails:[{id, from, peer, body, ts, read, mine}], unread};
     POST /mail/read {id} -> {ok}. */
  function initMailUnread(opts) {
    let mails = [];
    let timer = null;

    function byPeer() {
      const map = {};
      for (const m of mails) {
        if (!m.mine && !m.read) {
          const k = m.peer || String(m.from || "").split(":")[0];
          map[k] = (map[k] || 0) + 1;
        }
      }
      return map;
    }

    function changed() { if (opts.onChange) opts.onChange(byPeer()); }

    async function poll() {
      try {
        const d = await (await opts.http.fetchJSON("/mail")).json();
        mails = d.mails || [];
        changed();
      } catch (e) {} // backend not up yet / transient: retry on the next tick
    }

    async function markPeerRead(peer) {
      const targets = mails.filter(m => !m.mine && !m.read
        && (m.peer || String(m.from || "").split(":")[0]) === peer);
      if (!targets.length) return;
      await Promise.all(targets.map(async m => {
        try { await opts.http.postJSON("/mail/read", { id: m.id }); m.read = true; }
        catch (e) {}
      }));
      changed();
    }

    function start() { poll(); timer = setInterval(poll, 2000); } // badge latency: 2s is plenty on LAN
    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    return { start, stop, poll, byPeer, markPeerRead };
  }

  /* ---------- #messages ownership ----------
     One container, one owner at a time. Every render path used to write into
     the same #messages/#tray and trust separate entry guards to keep the other
     view out; a guard someone forgot painted the session transcript over an
     open friend conversation, and the friend poll would not repaint it (its
     payload had not changed) — the thread only came back on a page refresh
     (2026-09-10 real-machine finding).

     Now the writes go through a view-bound writer: `pane.of('friend').add(...)`
     writes only while the friend view owns the pane, and is a no-op otherwise
     (the node is built but never attached). Ownership is one flag instead of a
     convention spread across the render sites, and `pane.owner()` can be
     asserted directly.

     ctx: { msgs() -> el, tray() -> el, isNearBottom(el) -> bool, onPaint?() } */
  function initPane(ctx) {
    let owner = 'session';
    const refused = []; // view:paint pairs dropped for not owning the pane
    const painted = () => { if (ctx.onPaint) ctx.onPaint(); };
    // The mobile page has no persistent tray (its bubbles live in a bottom
    // sheet it manages itself), so the tray side is optional.
    const clearTray = () => {
      const t = ctx.tray && ctx.tray();
      if (t) t.innerHTML = '';
    };
    function of(view) {
      const active = () => owner === view;
      const attach = (node, ref) => {
        if (!active()) { refused.push(view + ':' + (node.nodeName || '?') + '.' + (node.className || '')); return node; }
        const el = ctx.msgs();
        if (ref) el.insertBefore(node, ref); else el.appendChild(node);
        painted();
        return node;
      };
      return {
        view,
        active,
        /* The live container. Reads (children, querySelectorAll) are safe from
           any view; a write through it is not — use the methods below. */
        el: () => ctx.msgs(),
        clear() {
          if (!active()) { refused.push(view + ':clear'); return false; }
          ctx.msgs().innerHTML = '';
          clearTray();
          painted();
          return true;
        },
        /* Messages only: a transcript repaint must not wipe the live subagent
           bubbles sitting in the tray. */
        clearMsgs() {
          if (!active()) { refused.push(view + ':clear-msgs'); return false; }
          ctx.msgs().innerHTML = '';
          painted();
          return true;
        },
        /* Build a .msg node and append it, keeping the reader pinned to the
           bottom the way a chat should. A refused write still hands the node
           back (detached), so callers can decorate what they get. pinOnAdd
           false = the caller pins once at the end of its own render. */
        add(cls, html, id) {
          const d = document.createElement('div');
          d.className = 'msg ' + cls;
          if (id) d.id = id;
          if (html) d.innerHTML = html;
          const pin = ctx.pinOnAdd !== false && active() && ctx.isNearBottom(ctx.msgs());
          attach(d);
          if (pin) { const el = ctx.msgs(); el.scrollTop = el.scrollHeight; }
          return d;
        },
        append: node => attach(node),
        before: (node, ref) => attach(node, ref),
        prepend(node) {
          if (!active()) { refused.push(view + ':prepend'); return node; }
          ctx.msgs().prepend(node);
          painted();
          return node;
        },
        /* Keep the bottom pinned after a repaint that replaced the pane. */
        stick() {
          if (!active()) return false;
          const el = ctx.msgs();
          el.scrollTop = el.scrollHeight;
          return true;
        },
        inTray(node) {
          const t = ctx.tray && ctx.tray();
          if (!active() || !t) { refused.push(view + ':tray'); return node; }
          t.appendChild(node);
          return node;
        },
      };
    }
    return {
      owner: () => owner,
      is: view => owner === view,
      /* Hand the pane to a view and wipe it: whoever takes over starts empty. */
      take(view) {
        owner = view;
        ctx.msgs().innerHTML = '';
        clearTray();
        painted();
      },
      of,
      refused,
    };
  }

  /* A human message shows up twice: the mailbox (authoritative — both sides, the
     sender's nickname, a hub timestamp) and the courier's transcript of the turn
     it woke (`[来自 <host> 的用户] text`, built by clone/base.py render_input).
     The transcript copy carries the WIRE name, so with a nickname set the same
     line looked like two messages signed by two different people (2026-09-10
     user report). The mailbox copy is the one to keep; this recognises the echo. */
  function humanEcho(content) {
    const m = /^\[来自 (.+?) 的用户\]\s?([\s\S]*)$/.exec(String(content || ''));
    return m ? { who: m[1], text: m[2].trim() } : null;
  }

  /* Owner feedback on a courier report: the box under every report in the
     friend view, and it is between the owner and their OWN courier — the note
     wakes that courier's turn locally and never reaches the peer (2026-09-11
     user instruction: 「与对面没有关系」). An empty box sends nothing: leaving
     it alone is allowed, so an empty submit is a no-op rather than an error.
     The button's label is CSS content, not a text node: a row's textContent
     stays the row's own words (the same rule the 信使汇报 label follows, and
     the row-text probes in tests/test_webui_friend.py depend on). */
  function attachReportFeedback(row, host) {
    const form = document.createElement('form');
    form.className = 'report-feedback';
    form.innerHTML =
      '<input type="text" placeholder="评价一下" />' +
      '<button type="submit" aria-label="发送"></button>' +
      '<span class="fb-hint"></span>';
    const input = form.querySelector('input');
    const hint = form.querySelector('.fb-hint');
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text) { hint.textContent = ''; return; }   // 允许为空：没写就不发
      hint.textContent = '发送中…';
      try {
        // postJSON hands back the Response (its callers so far just fire and
        // forget), so the body decides whether the note actually landed —
        // a 200 carrying {"error": ...} must not read as success.
        const res = await postJSON('/comm-note', { host, text });
        const out = await res.json().catch(() => null);
        if (out && out.ok) {
          input.value = '';
          hint.textContent = '已发给信使';
        } else {
          const why = (out && out.error) || '发送失败';
          hint.textContent = /no comm clone/.test(why) ? '信使还没就绪，稍后再试' : why;
        }
      } catch (e) {
        hint.textContent = '发送失败';
      }
    });
    row.appendChild(form);
    return form;
  }

  /* The courier's abstention marker is a delivery control token, not text to
     read: `<<SILENT>>` ends a comm turn silently (clone/comm.py). When one is
     left in a transcript — an older file, or the model adding it after real
     words — the browser throws the unknown tag away and the reader sees `<>`
     (2026-09-10 user report). Strip it wherever a body is rendered. */
  const SILENT_MARKER = '<<SILENT>>';
  function stripSilent(text) {
    const s = String(text == null ? '' : text);
    return s.includes(SILENT_MARKER) ? s.split(SILENT_MARKER).join('').trim() : s;
  }

  /* ---------- transcript rendering (both clients) ----------
     Session-style rendering: markdown text, reasoning details, tool blocks and
     answered ask cards — plus the friend thread's timeline, where rows carrying
     a `ts` merge into the transcript by time and ask cards sit at the tool call
     that raised them. The two clients differ only in cosmetics, so those come
     in through opts:

       asks            the client's FC.initAsks() bundle (answered cards)
       spawnLookup     callId -> subagent spec (the client's replay store)
       friend          true for the friend thread (mail echo dedupe on)
       side            {user, agent} extra classes for the friend thread
       mailBodies      Set of mailbox bodies already on screen (echo dedupe)
       argsMax         tool-card argument preview length
       spawnTitle      tooltip wording for spawn cards (mobile says 点按…)
       reasoningHtml   (text) -> inner html of the reasoning <details>
       feedbackHost    friend view only: the peer whose courier gets the
                       feedback box on every report row
       liveText        (run) -> html for a streaming text run
  */
  function markTs(el, ts) {
    if (el && typeof ts === 'number') {
      el.dataset.ts = String(ts);
      el.dataset.when = whenLabel(ts); // hover label (style.css ::after)
    }
    return el;
  }
  const WEEKDAYS = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
  /* When a message was sent, in words: 今天/昨天/前天 up close, the weekday
     inside the last seven days, 年月日 beyond that. 24h clock. Computed at
     paint time and re-painted with the row, so a "今天" that has since turned
     into yesterday is corrected on the next poll. */
  function whenLabel(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const pad = n => String(n).padStart(2, '0');
    const sameDay = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((sameDay(now) - sameDay(d)) / 86400000);
    let date;
    if (days === 0) date = '今天';
    else if (days === 1) date = '昨天';
    else if (days === 2) date = '前天';
    else if (days >= 0 && days < 7) date = WEEKDAYS[d.getDay()];
    else date = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    return date + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  /* The friend thread has two sides: the peer's rows and ours (the courier's).
     Everything a turn emits belongs to the side that produced it — its words,
     but also its reasoning, its tool calls and its errors (2026-09-10 user
     report: our prose sat right while our reasoning still hugged the left). */
  function sided(el, extra) {
    if (el && extra) el.className += extra;
    return el;
  }
  function insertByTs(p, el, ts) {
    if (!el || !p.active()) return el;
    if (typeof ts !== 'number') return el; // no stamp: keep it where it landed
    for (const kid of [...p.el().children]) {
      const kts = kid.dataset && kid.dataset.ts ? parseFloat(kid.dataset.ts) : null;
      if (kts !== null && kts > ts) { p.before(el, kid); return el; }
    }
    return el;
  }
  /* The question text a tool call itself carries — the only way to place an ask
     record from before records carried `call_id`. Exact match, no fuzzy. */
  function askTextOfCall(tc) {
    try {
      const args = JSON.parse(tc.function && tc.function.arguments || '{}');
      const first = Array.isArray(args.questions) && args.questions.length ? args.questions[0] : args;
      return String((first && first.question) || args.question || '').trim();
    } catch (e) { return ''; }
  }

  /* Absolute paths in the transcript become taps (§52): on the phone, tapping
     one pulls that file over. Only the shells that pass `fileLink` get links —
     the desktop already has the file it is looking at.

     The rewrite walks text nodes, never the HTML string: what comes in is
     marked's output, and a regex over markup is how tags get broken (or a link
     ends up inside an href). */
  const PATH_RE = /[A-Za-z]:[\\/][^\s"'<>|*?\u3002\uff0c\uff09\uff1f\uff01\u3011\u3015\uff1b\uff1a]+/g;

  function linkifyPaths(root, onPick) {
    if (!root || !onPick) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    for (const node of texts) {
      const parent = node.parentNode;
      if (!parent || (parent.closest && parent.closest('a'))) continue;
      const text = node.nodeValue || '';
      const frag = document.createDocumentFragment();
      let at = 0;
      let m;
      PATH_RE.lastIndex = 0;
      while ((m = PATH_RE.exec(text)) !== null) {
        const path = m[0].replace(/[\\.,;:\uff09)]+$/, '');  // trailing punctuation is prose
        if (m.index > at) frag.appendChild(document.createTextNode(text.slice(at, m.index)));
        const a = document.createElement('a');
        a.className = 'file-link';
        a.href = '#';
        a.textContent = path;
        a.addEventListener('click', e => { e.preventDefault(); onPick(path); });
        frag.appendChild(a);
        at = m.index + path.length;
        PATH_RE.lastIndex = at;
      }
      if (!at) continue;  // nothing matched: the node stays untouched
      if (at < text.length) frag.appendChild(document.createTextNode(text.slice(at)));
      parent.replaceChild(frag, node);
    }
  }

  function renderTranscript(p, messages, asks, opts) {
    const side = opts.side || {};
    const userSide = side.user || '';
    const agentSide = side.agent || '';
    const mailBodies = opts.mailBodies;
    let toolBlocks = {};
    const askByCall = new Map();  // ask record -> the tool call that raised it
    const askQueue = [];          // records without a call id, in stored order
    (asks || []).forEach(rec => {
      if (rec && rec.call_id) askByCall.set(rec.call_id, rec);
      else if (rec) askQueue.push(rec);
    });
    for (const m of messages || []) {
      if (m.role === 'user') {
        const c = String(m.content || '');
        const echo = opts.friend ? humanEcho(c) : null; // only the friend thread merges mail
        if (echo) {
          if (mailBodies && mailBodies.has(echo.text)) continue; // the mailbox copy is already on screen
          const bubble = markTs(p.add('user' + userSide, marked.parse(echo.text)), m.ts);
          const lab = document.createElement('div');
          lab.className = 'human-label';
          lab.textContent = '来自 ' + echo.who + ' 的用户';
          bubble.prepend(lab);
        }
        else if (c.startsWith('[background report]')) markTs(p.add('sys-note', escapeHtml(c)), m.ts);
        else if (m.sender === 'human' && !m.mine) {
          const bubble = markTs(p.add('user' + userSide, marked.parse(c)), m.ts);
          const lab = document.createElement('div');
          lab.className = 'human-label';
          lab.textContent = '来自 ' + (m.sender_name || '?') + ' 的用户';
          bubble.prepend(lab);
        }
        else markTs(p.add('user' + userSide, marked.parse(c)), m.ts);
      }
      else if (m.role === 'assistant') {
        if (m.reasoning) {
          const det = document.createElement('details');
          det.className = 'msg reasoning' + agentSide;
          det.innerHTML = '<summary>Thinking\u2026</summary>' + opts.reasoningHtml(m.reasoning);
          p.append(markTs(det, m.ts));
        }
        const text = stripSilent(m.content);
        if (text) {
          if (text.startsWith('(LLM error:') || text.startsWith('(Hit max tool rounds'))
            markTs(p.add('error' + agentSide, '&#x26A0; ' + escapeHtml(text)), m.ts);
          else {
            // Friend thread: our courier's turn text is a report to *us* — it
            // never went to the peer (comm._chat_end), so the stylesheet marks
            // it as one (a ::before label, so it stays out of the row's text),
            // and the row carries the feedback box back to that courier.
            const cls = 'assistant' + agentSide + (opts.report ? ' report' : '');
            const row = markTs(p.add(cls, marked.parse(text)), m.ts);
            if (opts.report && opts.feedbackHost) attachReportFeedback(row, opts.feedbackHost);
          }
        }
        if (m.tool_calls) m.tool_calls.forEach(tc => {
          const d = buildToolCard({ id: tc.id, name: tc.function?.name, args: tc.function?.arguments || '' }, { argsMax: opts.argsMax || 80 });
          p.append(markTs(sided(d, agentSide), m.ts));
          if (tc.function?.name === 'spawn' || tc.function?.name === 'background') attachSpawnClick(d, tc.id, opts.spawnLookup, opts.spawnTitle);
          if (tc.function?.name === 'inquire' || tc.function?.name === 'confirm' || tc.function?.name === 'ask_user') { // ask_user: pre-rename transcripts
            // Anchored by the tool call that raised it; a record without a call
            // id falls back to stored order, then to the call's own question text
            // (records written before `call_id` existed).
            let rec = askByCall.get(tc.id);
            if (rec) askByCall.delete(tc.id);
            else {
              const qtext = askTextOfCall(tc);
              const idx = qtext ? askQueue.findIndex(r => String(((r.questions || [])[0] || {}).question || '').trim() === qtext) : -1;
              rec = idx >= 0 ? askQueue.splice(idx, 1)[0] : (askQueue.length ? askQueue.shift() : null);
            }
            if (rec) p.append(markTs(opts.asks.buildAnsweredAskCard(rec), rec.ts));
          }
          toolBlocks[tc.id] = d;
        });
      } else if (m.role === 'tool') {
        const block = toolBlocks[m.tool_call_id];
        if (block) fillToolResult(block, m.content || '');
        else markTs(p.add('tool' + agentSide, '<pre>' + escapeHtml(m.content || '') + '</pre>'), m.ts);
      }
    }
    // Leftovers: their tool call is gone from the transcript, so they belong to
    // a turn older than anything on screen (legacy records carry no ts) — a
    // timestamped one (card asks) slots into the timeline like any other row.
    const leftover = [...askQueue, ...askByCall.values()];
    leftover.forEach(rec => {
      const node = markTs(opts.asks.buildAnsweredAskCard(rec), rec.ts);
      if (typeof rec.ts === 'number') insertByTs(p, node, rec.ts);
      else p.prepend(node);
    });
    // Last, over the finished pane: the phone turns absolute paths into taps
    // (opts.fileLink); a shell that passes none is left untouched (§52).
    linkifyPaths(p.el(), opts.fileLink);
  }

  /* In-flight turn tape (a comm clone's live events, friend view): merge
     adjacent text/reasoning deltas into runs so streaming reads as paragraphs
     instead of one row per fragment. */
  function renderLiveEvents(live, p, opts) {
    const agentSide = opts.side ? opts.side.agent : '';
    const runs = [];
    for (const ev of live || []) {
      const k = ev && ev.kind;
      if (k === 'reasoning_start') continue;
      const last = runs[runs.length - 1];
      if (k === 'reasoning_end') {
        // The session view collapses a reasoning block the moment its stream
        // ends and leaves only the one still being written open; the friend
        // view spectates the same turn, so it must read the same way
        // (2026-09-11 user report: the friend view only ever showed the
        // "Thinking…" line, expanded on a click).
        if (last && last.kind === 'reasoning') last.closed = true;
        continue;
      }
      if ((k === 'text' || k === 'reasoning') && last && last.kind === k) {
        last.text += liveEvText(ev);
        continue;
      }
      runs.push({ kind: k, text: liveEvText(ev), ev });
    }
    for (const r of runs) {
      if (r.kind === 'text') {
        const html = opts.liveText(r);
        if (html) p.add('friend-live' + agentSide, html);
      } else if (r.kind === 'reasoning') {
        const det = document.createElement('details');
        det.className = 'msg reasoning' + agentSide;
        det.open = !r.closed;   // still streaming: open, exactly like the session view
        det.innerHTML = '<summary>Thinking\u2026</summary>' + opts.reasoningHtml(r.text);
        p.append(det);
      } else if (r.kind === 'tool') {
        const c = (r.ev && r.ev.content) || {};
        p.add('tool' + agentSide, '<div class="tool-label">&#x1F527; ' + escapeHtml(c.name || 'tool')
          + (c.args ? ' <code style="font-size:0.82rem;opacity:0.7">' + escapeHtml(String(c.args).slice(0, 80)) + '</code>' : '') + '</div>');
      } else if (r.kind === 'tool_result') {
        const t = String(liveEvText(r.ev) || '');
        p.add('friend-live' + agentSide, '<pre>' + escapeHtml(t.slice(0, 400)) + (t.length > 400 ? '...' : '') + '</pre>');
      } else if (r.kind === 'status') {
        p.add('friend-event', '⏳ ' + escapeHtml(r.text || 'running…'));
      } else if (r.kind === 'error') {
        p.add('friend-event', '⚠ ' + escapeHtml(r.text || 'error'));
      }
    }
  }
  function liveEvText(ev) {
    const c = ev && ev.content;
    if (typeof c === 'string') return c;
    if (c && typeof c === 'object') return c.text || c.content || c.name || '';
    return '';
  }

  window.FungiCommon = {
    initHttp, url, fetchJSON, postJSON,
    escapeHtml, fmtDate, getSessionTitle,
    initConfirmModal, showConfirm, closeConfirm,
    initTransfer,
    buildToolCard, fillToolResult, attachSpawnClick,
    initAsks, initPendingAsks, initMailUnread,
    initPane, stripSilent, humanEcho,
    markTs, insertByTs, askTextOfCall, linkifyPaths, renderTranscript, renderLiveEvents, whenLabel,
  };
})();
