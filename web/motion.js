/* Fungi motion — the single GSAP-driven motion module.
   Optional by contract: app.js only ever calls window.fungiMotion?.x?.(), so
   removing this file (or web/vendor/) returns the UI to pure-CSS behavior.
   prefers-reduced-motion: the whole module collapses to { reduced: true } and
   every effect becomes a no-op. */
(function () {
  'use strict';
  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || typeof gsap === 'undefined') {
    window.fungiMotion = { reduced: true };
    return;
  }
  document.documentElement.classList.add('motion-on');
  gsap.defaults({ overwrite: 'auto' });

  function clear(el) {
    gsap.set(el, { clearProps: 'transform,opacity' });
  }

  /* Chat choreography. Only genuinely-new nodes are animated (app.js tracks
     the live-node count across streaming re-renders); kind comes from the
     node's own class so call sites stay dumb. */
  function msgIn(el, kind) {
    if (!el) return;
    if (kind === 'user') {
      gsap.fromTo(el, { opacity: 0, x: 22, y: 6, scale: 0.98 },
        { opacity: 1, x: 0, y: 0, scale: 1, duration: 0.32, ease: 'expo.out',
          onComplete: function () { clear(el); } });
    } else if (kind === 'assistant') {
      gsap.fromTo(el, { opacity: 0, x: -18, y: 6, scale: 0.98 },
        { opacity: 1, x: 0, y: 0, scale: 1, duration: 0.34, ease: 'expo.out',
          onComplete: function () { clear(el); } });
    } else {
      gsap.fromTo(el, { opacity: 0, y: 8 },
        { opacity: 1, y: 0, duration: 0.26, ease: 'power2.out',
          onComplete: function () { clear(el); } });
    }
  }

  /* Pending ask card drops into the banner (or a friend view). */
  function askCardIn(el) {
    if (!el) return;
    gsap.fromTo(el, { opacity: 0, y: -18, scale: 0.96 },
      { opacity: 1, y: 0, scale: 1, duration: 0.42, ease: 'back.out(1.5)',
        onComplete: function () { clear(el); } });
  }

  /* Consent resolution: 3D flip around X, then a rubber stamp lands.
     ok=true -> 已放行 (green), ok=false -> 已拒绝 (red). */
  function askResolved(card, ok) {
    if (!card) return;
    gsap.to(card, { rotationX: 90, duration: 0.2, ease: 'power2.in',
      transformPerspective: 700, onComplete: function () {
        var stamp = document.createElement('div');
        stamp.className = 'motion-stamp ' + (ok ? 'ok' : 'no');
        stamp.textContent = ok ? '已放行' : '已拒绝';
        card.appendChild(stamp);
        gsap.fromTo(stamp,
          { scale: 2.4, opacity: 0, rotation: -20 },
          { scale: 1, opacity: 0.92, rotation: -8, duration: 0.32, ease: 'expo.out' });
        gsap.fromTo(card, { rotationX: -90 },
          { rotationX: 0, duration: 0.3, ease: 'back.out(1.3)',
            onComplete: function () { clear(card); } });
      } });
  }

  /* Spore burst from a card (file landed / consent granted): mycelium ping. */
  function spores(fromEl) {
    if (!fromEl) return;
    var r = fromEl.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    for (var i = 0; i < 9; i++) {
      (function (i) {
        var s = document.createElement('div');
        s.className = 'motion-spore';
        s.style.left = cx + 'px';
        s.style.top = cy + 'px';
        document.body.appendChild(s);
        gsap.fromTo(s, { x: 0, y: 0, opacity: 0.9, scale: 1 },
          { x: gsap.utils.random(-100, 100), y: gsap.utils.random(-90, -24),
            opacity: 0, scale: gsap.utils.random(0.4, 1.2),
            duration: gsap.utils.random(0.8, 1.4), ease: 'power2.out',
            delay: i * 0.045, onComplete: function () { s.remove(); } });
      })(i);
    }
  }

  /* Flip-driven list reordering. `mutate` performs the DOM change between
     state capture and playback; falls back to a plain call without GSAP. */
  function listFlip(container, mutate) {
    if (!container) return;
    if (typeof Flip === 'undefined') { mutate(); return; }
    var state = Flip.getState(container.children);
    mutate();
    /* 没有 `absolute: true`（2026-09-25 用户报告，两处症状同一个根）：
       - 它把要移动的行在动画期间`position:absolute`拿出文档流，于是列表的**内容高度
         当场塌掉**，`#session-list` 是个滚动容器 —— 浏览器把 scrollTop 夹回去，
         动画结束行回到流里，滚动位置却留在顶上：用户看到的就是「切一下会话，视图重排」。
       - 而且绝对定位的行不再被 `#session-list{overflow:auto}` 裁剪（它的包含块跑到外面
         去了），动画期间会画到下面的好友列表上。
       - 第三处：muta 里**新插入**的那一行不在 `state` 里（Flip 从没移动过它），
         它就在塌掉的流里排版 —— 新建会话时会**正好画在「文件传输助手」那一格上**，
         等动画把别人挪走才落到自己的位置（用户：「会话会短暂停留在文件传输助手，然后再下移」）。
       这个列表的行尺寸不变，所以 transform 就够了：行的包含块还在列表里，被照常裁剪。 */
    Flip.from(state, {
      duration: 0.34, ease: 'expo.out',
      onEnter: function (els) {
        gsap.fromTo(els, { opacity: 0, y: -6 }, { opacity: 1, y: 0, duration: 0.24 });
      }
    });
  }

  /* Day-night theme wash: an accent-tinted circle expands from the theme
     switch, the color flip happens once the screen is covered, then the
     wash fades out. `apply` is the caller's data-theme setter. */
  function themeTo(t, originEl, apply) {
    var wash = document.createElement('div');
    wash.className = 'motion-theme-wash ' + t;
    var r = (originEl || document.body).getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var R = Math.hypot(Math.max(cx, window.innerWidth - cx),
      Math.max(cy, window.innerHeight - cy)) + 40;
    wash.style.clipPath = 'circle(0px at ' + cx + 'px ' + cy + 'px)';
    document.body.appendChild(wash);
    gsap.to(wash, {
      clipPath: 'circle(' + R + 'px at ' + cx + 'px ' + cy + 'px)',
      duration: 0.34, ease: 'power2.in',
      onComplete: function () {
        if (typeof apply === 'function') apply();
        gsap.to(wash, { opacity: 0, duration: 0.3, ease: 'power1.out',
          delay: 0.06, onComplete: function () { wash.remove(); } });
      }
    });
  }

  /* Number counter for badges (friends count etc.). */
  function counter(el, to) {
    if (!el) return;
    var from = parseInt(el.textContent, 10) || 0;
    if (from === to) { el.textContent = to; return; }
    var obj = { v: from };
    gsap.to(obj, { v: to, duration: 0.5, ease: 'power1.out',
      onUpdate: function () { el.textContent = Math.round(obj.v); } });
  }

  /* Agent bubble idle drift: gentle orbital bob. */
  function float(el) {
    if (!el) return;
    gsap.to(el, { y: 4, duration: 1.6, ease: 'sine.inOut', yoyo: true, repeat: -1 });
  }

  /* Thinking wave: elastic bars prepended into the status line. */
  var waveEl = null;
  function waveOn(statusEl) {
    if (!statusEl || waveEl) return;
    waveEl = document.createElement('span');
    waveEl.className = 'motion-wave';
    waveEl.innerHTML = '<i></i><i></i><i></i>';
    statusEl.insertBefore(waveEl, statusEl.firstChild);
  }
  function waveOff() {
    if (!waveEl) return;
    var w = waveEl; waveEl = null;
    gsap.to(w, { opacity: 0, duration: 0.18, onComplete: function () { w.remove(); } });
  }

  /* Liquid send button: white ripple from the press point. Delegated here so
     app.js needs no wiring for it. */
  (function liquid() {
    var btn = document.getElementById('send');
    if (!btn) return;
    btn.addEventListener('click', function (e) {
      var r = btn.getBoundingClientRect();
      var rip = document.createElement('span');
      rip.className = 'motion-ripple';
      rip.style.left = (e.clientX - r.left) + 'px';
      rip.style.top = (e.clientY - r.top) + 'px';
      btn.appendChild(rip);
      gsap.fromTo(rip,
        { scale: 0, opacity: 0.5, xPercent: -50, yPercent: -50 },
        { scale: Math.max(r.width, r.height) / 6, opacity: 0, duration: 0.55,
          ease: 'power2.out', onComplete: function () { rip.remove(); } });
    });
  })();

  window.fungiMotion = {
    reduced: false,
    msgIn: msgIn,
    askCardIn: askCardIn,
    askResolved: askResolved,
    spores: spores,
    listFlip: listFlip,
    themeTo: themeTo,
    waveOn: waveOn,
    waveOff: waveOff,
    counter: counter,
    float: float
  };
})();
