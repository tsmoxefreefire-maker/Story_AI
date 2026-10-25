/* story-service runtime player (Section 7). Vanilla JavaScript (ES2019): no framework, no network, no postMessage.
   One master clock T (ms) drives everything: tweens are Web Animations kept paused and set to T on every frame, so
   seeking is exact; pose/expression/facing are state cues applied at T; lip-sync and captions are computed from T
   and the word timings; a line's audio starts when T reaches it and the clock follows the audio if they drift.
   The LLM never writes any of this: it only fills the declarative cues in story-data. */
(function () {
  'use strict';
  var I18N = /*@I18N*/{};
  var doc = document, root = doc.documentElement;
  var $ = function (id) { return doc.getElementById(id); };
  var D = JSON.parse($('story-data').textContent);
  var L = I18N[D.meta.language] || I18N.ar || {};
  var RTL = D.meta.dir === 'rtl';
  var DUR = D.meta.duration_ms;
  var reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var freeze = /(^|&)freeze=1(&|$)/.test((location.search || '').slice(1));
  if (freeze) root.classList.add('freeze');
  var errors = [];
  window.addEventListener('error', function (e) { errors.push(String(e.message || e)); });
  window.addEventListener('unhandledrejection', function (e) { errors.push('promise: ' + String(e.reason && e.reason.message || e.reason)); });

  var stage = $('stage'), wrap = $('stage-wrap'), capEl = $('captions'), overlay = $('overlay');
  var playBtn = $('btn-play'), speedBtn = $('btn-speed'), ccBtn = $('btn-cc');
  var SPEEDS = [0.75, 1, 1.25], MOUTHS = ['open_s', 'open_m', 'open_l'];
  var EASE = { linear: 'linear', 'ease-in': 'ease-in', 'ease-out': 'ease-out', 'ease-in-out': 'ease-in-out' };
  var TWEEN = { show: 1, hide: 1, fade: 1, move: 1, scale: 1, rotate: 1, keyframes: 1, highlight: 1, shake: 1, camera: 1, enter: 1, exit: 1 };

  /* ------------------------------------------------------------------------------------------------ state */
  var T = 0, playing = false, speed = 1, overlayOpen = false, ended = false, active = -1, lastNow = 0, lastSync = 0;
  var stageScale = 1, current = null, done = {}, audioLine = null;
  var tapBubble = doc.createElement('div'), tapTimer = 0;
  tapBubble.className = 'bubble tap-name';
  tapBubble.hidden = true;
  $('app').appendChild(tapBubble);

  var scenes = D.scenes.map(function (s, i) {
    return { d: s, i: i, svg: $(s.svg_id), anims: [], states: {}, init: {}, actors: [], ready: false };
  });
  var lines = [];
  scenes.forEach(function (sc) { sc.d.lines.forEach(function (l) { lines.push({ d: l, sc: sc, au: null }); }); });
  var inters = [];
  scenes.forEach(function (sc) { if (sc.d.interaction) inters.push({ d: sc.d.interaction, sc: sc }); });
  inters.sort(function (a, b) { return a.d.at_ms - b.d.at_ms; });

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function num(v, d) { return typeof v === 'number' && isFinite(v) ? v : d; }
  function tf(st) { return 'translate(' + st.x + 'px,' + st.y + 'px) rotate(' + st.r + 'deg) scale(' + st.s + ')'; }
  function camTf(c) { return 'translate(640px,360px) scale(' + c.z + ') translate(' + (-c.x) + 'px,' + (-c.y) + 'px)'; }
  function cueDur(c) { return c.dur == null ? 400 : Math.max(1, c.dur); }  /* dur 0 means "at once" */
  function mk(tag, cls, text) { var e = doc.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  /* ------------------------------------------------------------------------------------------------ cues → animations */
  function prepareScene(sc) {
    if (sc.ready) return;
    sc.ready = true;
    sc.actors = Array.prototype.slice.call(sc.svg.querySelectorAll('.actor'));
    sc.actors.forEach(function (a) {
      sc.init[a.id] = { 'data-pose': a.getAttribute('data-pose') || 'idle', 'data-expr': a.getAttribute('data-expr') || 'neutral',
        'data-facing': a.getAttribute('data-facing') || 'right' };
      sc.states[a.id] = [];
    });
    var byTarget = {};
    sc.d.cues.slice().sort(function (a, b) { return a.t - b.t; }).forEach(function (c) {
      (byTarget[c.target] = byTarget[c.target] || []).push(c);
    });
    Object.keys(byTarget).forEach(function (id) {
      var el = $(id);
      if (!el) { errors.push('cue target missing: ' + id); return; }
      if (el.classList.contains('cam')) prepareCamera(sc, el, byTarget[id]);
      else prepareElement(sc, el, byTarget[id]);
    });
    sc.states && Object.keys(sc.states).forEach(function (k) { sc.states[k].sort(function (a, b) { return a.t - b.t; }); });
  }

  function offstage(el, dir) {
    var b = el.getBBox();
    return dir === 'left' ? Math.min(-400, -(b.x + b.width + 40)) : Math.max(400, 1280 - b.x + 40);
  }

  function addState(sc, el, t, attr, v) {
    (sc.states[el.id] = sc.states[el.id] || []).push({ t: t, attr: attr, v: v });
  }

  function prepareElement(sc, el, cues) {
    var st = { x: 0, y: 0, s: 1, r: 0, o: el.getAttribute('data-initial') === 'hidden' ? 0 : 1 };
    var firstTween = null;
    for (var i = 0; i < cues.length; i++) { if (TWEEN[cues[i].action]) { firstTween = cues[i]; break; } }
    if (firstTween && firstTween.action === 'enter') st.x = offstage(el, (firstTween.params || {}).from);
    var cur = { x: st.x, y: st.y, s: st.s, r: st.r, o: st.o };
    var usesT = false, usesO = false;
    cues.forEach(function (c) {
      var p = c.params || {}, kf = null, dur = cueDur(c), from = { x: cur.x, y: cur.y, s: cur.s, r: cur.r, o: cur.o };
      switch (c.action) {
        case 'pose': addState(sc, el, c.t, 'data-pose', p.name); return;
        case 'expression': addState(sc, el, c.t, 'data-expr', p.name); return;
        case 'face': addState(sc, el, c.t, 'data-facing', p.dir); return;
        case 'show': cur.o = 1; kf = [{ opacity: 0 }, { opacity: 1 }]; usesO = true; break;
        case 'hide': cur.o = 0; kf = [{ opacity: from.o }, { opacity: 0 }]; usesO = true; break;
        case 'fade': cur.o = clamp(num(p.to, 1), 0, 1); kf = [{ opacity: from.o }, { opacity: cur.o }]; usesO = true; break;
        case 'move':
          if (p.from) { from.x = num(p.from[0], from.x); from.y = num(p.from[1], from.y); }
          cur.x = num((p.to || [])[0], cur.x); cur.y = num((p.to || [])[1], cur.y);
          kf = [{ transform: tf(from) }, { transform: tf(cur) }]; usesT = true; break;
        case 'scale':
          if (p.from != null) from.s = num(p.from, from.s);
          cur.s = num(p.to, cur.s); kf = [{ transform: tf(from) }, { transform: tf(cur) }]; usesT = true; break;
        case 'rotate':
          if (p.from != null) from.r = num(p.from, from.r);
          cur.r = num(p.to, cur.r); kf = [{ transform: tf(from) }, { transform: tf(cur) }]; usesT = true; break;
        case 'keyframes':
          kf = (p.frames || []).slice().sort(function (a, b) { return a.o - b.o; }).map(function (f) {
            cur = { x: num(f.x, cur.x), y: num(f.y, cur.y), s: num(f.scale, cur.s), r: num(f.rotate, cur.r), o: num(f.opacity, cur.o) };
            return { offset: clamp(f.o, 0, 1), transform: tf(cur), opacity: cur.o };
          });
          usesT = usesO = true; break;
        case 'highlight':
          if (reduce) return;
          var n = clamp(num(p.pulses, 1), 1, 3); kf = [];
          for (var k = 0; k < n; k++) {
            kf.push({ offset: k / n, transform: tf(cur) });
            kf.push({ offset: (k + 0.5) / n, transform: tf({ x: cur.x, y: cur.y, s: cur.s * 1.12, r: cur.r }) });
          }
          kf.push({ offset: 1, transform: tf(cur) }); usesT = true; break;
        case 'shake':
          if (reduce) return;
          var amp = clamp(num(p.amplitude, 8), 0, 12); kf = [];
          for (var j = 0; j <= 16; j++) kf.push({ offset: j / 16, transform: tf({ x: cur.x + amp * Math.sin(Math.PI * j / 2), y: cur.y, s: cur.s, r: cur.r }) });
          usesT = true; break;
        case 'enter':
          from.x = offstage(el, p.from); cur.x = 0;
          kf = [{ transform: tf(from) }, { transform: tf(cur) }]; usesT = true;
          addState(sc, el, c.t, 'data-pose', 'walk'); addState(sc, el, c.t + dur, 'data-pose', 'idle'); break;
        case 'exit':
          cur.x = offstage(el, p.to);
          kf = [{ transform: tf(from) }, { transform: tf(cur) }]; usesT = true;
          addState(sc, el, c.t, 'data-pose', 'walk'); break;
        default: errors.push('unknown cue action: ' + c.action); return;
      }
      var a = el.animate(kf, { duration: reduce ? 1 : dur, easing: EASE[p.easing] || 'ease-in-out', fill: 'forwards' });
      a.pause();
      sc.anims.push({ a: a, t: c.t, dur: reduce ? 1 : dur });
    });
    if (usesT) el.style.transform = tf(st);
    if (usesO) el.style.opacity = String(st.o);
  }

  function prepareCamera(sc, el, cues) {
    var v = (el.getAttribute('data-cam') || '640 360 1').split(' ').map(Number);
    var cur = { x: v[0], y: v[1], z: v[2] };
    cues.forEach(function (c) {
      if (c.action !== 'camera' || reduce) return;
      var p = c.params || {}, to = p.to || {}, fr = p.from;
      var from = fr ? { x: num(fr.x, cur.x), y: num(fr.y, cur.y), z: clamp(num(fr.zoom, cur.z), 1, 1.4) } : cur;
      var next = { x: num(to.x, cur.x), y: num(to.y, cur.y), z: clamp(num(to.zoom, cur.z), 1, 1.4) };
      var a = el.animate([{ transform: camTf(from) }, { transform: camTf(next) }],
        { duration: reduce ? 1 : cueDur(c), easing: EASE[p.easing] || 'ease-in-out', fill: 'forwards' });
      a.pause();
      sc.anims.push({ a: a, t: c.t, dur: reduce ? 1 : cueDur(c) });
      cur = next;
    });
  }

  /* ------------------------------------------------------------------------------------------------ applying T */
  function applyTweens(sc, t, prev, all) {
    for (var i = 0; i < sc.anims.length; i++) {
      var x = sc.anims[i], end = x.t + x.dur;
      /* the window contains T, or T crossed (part of) it since the last frame: only those need a new currentTime */
      if (all || (Math.min(prev, t) <= end && Math.max(prev, t) >= x.t)) x.a.currentTime = t < x.t ? t - x.t : Math.min(t - x.t, x.dur);
    }
  }

  var react = {};  /* tap reactions: actor id -> clock time (performance.now) until which it waves and smiles */
  function applyStates(sc, t) {
    var now = window.performance ? performance.now() : 0;
    sc.actors.forEach(function (a) {
      var list = sc.states[a.id] || [], want = {}, init = sc.init[a.id];
      for (var k in init) want[k] = init[k];
      for (var i = 0; i < list.length && list[i].t <= t; i++) want[list[i].attr] = list[i].v;
      if (react[a.id] > now) { want['data-pose'] = 'wave'; want['data-expr'] = 'happy'; }
      for (var attr in want) if (a.getAttribute(attr) !== want[attr]) a.setAttribute(attr, want[attr]);
    });
  }

  function lineAt(sc, t) {
    var ls = sc.d.lines;
    for (var i = 0; i < ls.length; i++) if (t >= ls[i].start_ms && t < ls[i].end_ms) return ls[i];
    return null;
  }

  function applyMouths(sc, t) {
    var l = lineAt(sc, t), speaker = null, mouth = null;
    if (l && l.speaker !== 'narrator') {
      speaker = $(sc.d.id + '-' + l.speaker);
      mouth = 'closed';
      for (var i = 0; i < l.words.length; i++) {
        var w = l.words[i];
        if (t >= w.s && t < w.e) { mouth = MOUTHS[Math.floor((t - w.s) / 120) % 3]; break; }
      }
    }
    sc.actors.forEach(function (a) {
      if (a === speaker) { if (a.getAttribute('data-mouth') !== mouth) a.setAttribute('data-mouth', mouth); }
      else if (a.hasAttribute('data-mouth')) a.removeAttribute('data-mouth');
    });
  }

  /* captions: the line stays up until the next line starts; the current word is highlighted */
  var capLine = null, capWords = [];
  function captionAt(sc, t) {
    var cur = null;
    sc.d.lines.forEach(function (l) { if (l.start_ms <= t) cur = l; });
    return t < sc.d.end_ms ? cur : null;
  }
  function renderCaption(sc, t) {
    var l = captionAt(sc, t);
    if (l !== capLine) {
      capLine = l; capWords = [];
      capEl.textContent = '';
      if (l) {
        var box = mk('span', 'cap');
        if (l.speaker !== 'narrator' && D.characters[l.speaker]) box.appendChild(mk('span', 'who', D.characters[l.speaker].name + ':'));
        var words = l.words && l.words.length ? l.words : [{ w: l.text, s: l.start_ms, e: l.end_ms }];
        words.forEach(function (w, i) {
          if (i || box.childNodes.length) box.appendChild(doc.createTextNode(' '));
          var s = mk('span', 'w', w.w); box.appendChild(s); capWords.push({ el: s, s: w.s, e: w.e });
        });
        capEl.appendChild(box);
        fitCaption(box);
      }
    }
    for (var i = 0; i < capWords.length; i++) {
      var on = t >= capWords[i].s && t < capWords[i].e;
      if (capWords[i].on !== on) { capWords[i].on = on; capWords[i].el.classList.toggle('now', on); }
    }
  }
  function fitCaption(box) { /* at most 2 lines: shrink down to 16px if needed (7.6) */
    capEl.style.fontSize = '';
    var size = parseFloat(getComputedStyle(capEl).fontSize);
    /* measure as a plain block: a -webkit-box without its clamp does not wrap, so it would read as one line */
    box.style.display = 'block'; box.style.webkitLineClamp = 'unset';
    while (box.scrollHeight > size * 1.6 * 2 + 2 && size > 16) { size -= 1; capEl.style.fontSize = size + 'px'; }
    box.style.display = ''; box.style.webkitLineClamp = '';
  }

  function sceneIndexAt(t) {
    for (var i = 0; i < scenes.length; i++) if (t < scenes[i].d.end_ms) return i;
    return scenes.length - 1;
  }

  function setActive(i) {
    scenes.forEach(function (sc, k) {
      sc.svg.classList.toggle('active', k === i);
      sc.svg.classList.toggle('gone', Math.abs(k - i) > 1);
    });
    active = i;
    tapBubble.hidden = true;
    prepareScene(scenes[i]);
  }

  function render(prev, all) {
    var i = sceneIndexAt(T), changed = i !== active;
    if (changed) setActive(i);
    var sc = scenes[i];
    applyTweens(sc, T, prev, all || changed);
    applyStates(sc, T);
    applyMouths(sc, T);
    renderCaption(sc, T);
    updateProgress();
  }

  /* ------------------------------------------------------------------------------------------------ audio */
  function audioFor(line) {
    if (!line.au) {
      var a = new Audio();
      a.preload = 'auto';
      a.src = line.d.audio;
      a.addEventListener('timeupdate', function () { if (audioLine === line) followAudio(); });
      a.addEventListener('loadedmetadata', function () { if (audioLine === line && !line.synced) placeAudio(line); });
      line.au = a;
    }
    return line.au;
  }
  function stopAudio() {
    if (audioLine && audioLine.au) { try { audioLine.au.pause(); } catch (e) { /* ignore */ } }
    audioLine = null;
  }
  function lineAtGlobal(t) {
    for (var i = 0; i < lines.length; i++) if (t >= lines[i].d.start_ms && t < lines[i].d.end_ms) return lines[i];
    return null;
  }
  function preloadAfter(l) { /* the next two lines start loading now, so their first word is never cut */
    var i = lines.indexOf(l);
    for (var k = i + 1; k <= i + 2 && k < lines.length; k++) audioFor(lines[k]);
  }
  function syncAudio() {
    if (!playing || overlayOpen || ended) { stopAudio(); return; }
    var l = lineAtGlobal(T);
    if (l === audioLine) return;
    stopAudio();
    if (!l) return;
    var a = audioFor(l);
    audioLine = l;
    l.synced = false; l.failed = false; l.fails = 0; l.waitSince = performance.now();
    if (a.readyState >= 1) placeAudio(l);
    a.playbackRate = speed;
    var p = a.play();
    if (p && p.catch) p.catch(function () { l.failed = true; /* blocked or unavailable: the clock keeps going */ });
    preloadAfter(l);
  }
  /* Put the audio at the clock's offset inside its line. At a line's own start that is 0 (the clock waits for the
     audio, see audioReady); only after a seek into the middle of a line does the audio jump forward. Before the
     file's metadata is known the position cannot be set, so `loadedmetadata` calls this again. */
  function placeAudio(l) {
    var off = (T - l.d.start_ms) / 1000;
    try { l.au.currentTime = off < 0.15 ? 0 : off; l.synced = true; } catch (e) { l.synced = false; }
  }
  function audioReady(l) { /* the clock may run: the line's audio plays, failed, or has kept us waiting 2 s */
    if (!l || !l.au || l.failed || performance.now() - l.waitSince > 2000) return true;
    return l.synced && !l.au.paused && !l.au.seeking && l.au.readyState >= 3;
  }
  function followAudio() { /* the clock follows the audio when they drift more than 120 ms */
    var l = audioLine;
    if (!l || !l.au || !l.synced || l.au.paused || l.au.seeking || l.au.readyState < 3 || overlayOpen) return;
    var pos = l.au.currentTime * 1000, want = T - l.d.start_ms, off = Math.abs(pos - want);
    if (off > 1000) {                       /* not drift but a seek the server could not serve (no HTTP Range) */
      l.fails = (l.fails || 0) + 1;
      if (l.fails <= 2) placeAudio(l); else l.synced = false;       /* then the clock leads and the audio follows */
      return;
    }
    if (off > 120 && pos < l.d.end_ms - l.d.start_ms) { var prev = T; T = l.d.start_ms + pos; render(prev, false); }
  }

  /* ------------------------------------------------------------------------------------------------ clock */
  function nextInteraction(prev, t) {
    for (var i = 0; i < inters.length; i++) {
      var it = inters[i];
      if (!done[it.d.id] && it.d.at_ms >= prev && it.d.at_ms <= t) return it;
    }
    return null;
  }
  function frame(now) {
    var dt = lastNow ? Math.min(100, now - lastNow) : 0;
    lastNow = now;
    if (playing && !overlayOpen && !ended && dt > 0 && audioReady(audioLine)) {
      var prev = T, nextT = Math.min(DUR, T + dt * speed), it = nextInteraction(prev, nextT);
      T = it ? it.d.at_ms : nextT;
      render(prev, false);
      if (it) openInteraction(it);
      else if (T >= DUR) finish();
      else {
        syncAudio();
        if (now - lastSync > 250) { lastSync = now; followAudio(); }
      }
    }
    requestAnimationFrame(frame);
  }
  function setPlayingUI() {
    playBtn.classList.toggle('paused', !playing);
    playBtn.setAttribute('aria-label', playing ? L.pause : L.resume);
  }
  function leaveStart() { $('start').hidden = true; }
  function play() {
    leaveStart();
    if (ended) seek(0);
    playing = true; setPlayingUI(); syncAudio();
  }
  function pause() { playing = false; setPlayingUI(); stopAudio(); }
  function finish() {
    T = DUR; playing = false; ended = true; stopAudio(); setPlayingUI(); updateProgress();
    $('end').hidden = false;
    $('btn-replay').focus();
  }
  function seek(ms) {
    var prev = T;
    T = clamp(Math.round(ms), 0, DUR);
    inters.forEach(function (it) { done[it.d.id] = it.d.at_ms < T; });
    if (overlayOpen) closeOverlay();
    ended = false; $('end').hidden = true;
    stopAudio();
    render(prev, true);
    if (playing) syncAudio();
  }
  function setSpeed(x) {
    speed = SPEEDS.indexOf(x) >= 0 ? x : 1;
    speedBtn.firstChild.textContent = speed + '×';
    speedBtn.setAttribute('aria-label', L.speed + ' ' + speed + '×');
    if (audioLine && audioLine.au) audioLine.au.playbackRate = speed;
  }

  /* ------------------------------------------------------------------------------------------------ progress bar */
  var prog = $('progress'), fill = $('progress-fill');
  function updateProgress() {
    var f = DUR ? T / DUR : 0;
    fill.style.width = (f * 100).toFixed(2) + '%';
    prog.setAttribute('aria-valuenow', String(Math.round(f * 100)));
  }
  (function ticks() {
    var box = $('progress-ticks');
    scenes.forEach(function (sc, i) { if (i) { var k = mk('i'); k.style.insetInlineStart = (sc.d.start_ms / DUR * 100) + '%'; box.appendChild(k); } });
    inters.forEach(function (it) { var k = mk('i', 'q'); k.style.insetInlineStart = (it.d.at_ms / DUR * 100) + '%'; box.appendChild(k); });
  })();
  function seekFromPointer(e) {
    var r = prog.getBoundingClientRect(), f = (e.clientX - r.left) / r.width;
    seek((RTL ? 1 - f : f) * DUR);
  }
  prog.addEventListener('pointerdown', function (e) { prog.setPointerCapture(e.pointerId); seekFromPointer(e); });
  prog.addEventListener('pointermove', function (e) { if (prog.hasPointerCapture && prog.hasPointerCapture(e.pointerId)) seekFromPointer(e); });

  /* ------------------------------------------------------------------------------------------------ interactions */
  function seedRand(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return function () { h = (Math.imul(h ^ (h >>> 15), 2246822507) + 0x6D2B79F5) >>> 0; return h / 4294967296; };
  }
  function shuffle(arr, id) {
    var r = seedRand(id), a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(r() * (i + 1)), x = a[i]; a[i] = a[j]; a[j] = x; }
    return a;
  }
  function button(cls, text, fn) {
    var b = mk('button', cls, text); b.type = 'button';
    b.addEventListener('click', function (e) { e.stopPropagation(); fn(); });
    return b;
  }

  function openInteraction(it) {
    if (current) closeOverlay();
    overlayOpen = true; stopAudio(); tapBubble.hidden = true;
    var d = it.d, ui = { it: it, d: d, complete: false };
    current = ui;
    overlay.textContent = '';
    overlay.className = d.type === 'tap_reveal' ? 'banner' : '';
    overlay.hidden = false;
    root.classList.add('overlay-open');
    var card = mk('div', 'card');
    card.setAttribute('role', 'dialog'); card.setAttribute('aria-modal', 'true'); card.setAttribute('aria-label', d.prompt);
    card.appendChild(mk('p', 'prompt', d.prompt));
    ui.body = mk('div', 'body'); card.appendChild(ui.body);
    ui.fb = mk('p', 'fb'); ui.fb.setAttribute('aria-live', 'polite'); card.appendChild(ui.fb);
    var row = mk('div', 'row');
    if (d.hint) row.appendChild(button('ctl hint', L.hint, function () { ui.fb.textContent = d.hint; }));
    ui.skip = button('ctl skip', L.skip, resume); ui.skip.hidden = true; row.appendChild(ui.skip);
    ui.cont = button('ctl go', L['continue'], resume); ui.cont.hidden = true; row.appendChild(ui.cont);
    card.appendChild(row);
    overlay.appendChild(card);
    if (d.type !== 'tap_reveal') card.addEventListener('keydown', trapTab);
    ui.timer = setTimeout(function () { if (current === ui && !ui.complete) ui.skip.hidden = false; }, 8000);
    (BUILD[d.type] || function () { errors.push('unknown interaction type: ' + d.type); completeIt(ui); })(ui);
    var first = card.querySelector('button:not([hidden])');
    if (first) first.focus({ preventScroll: true });
  }
  function resume() { /* Continue / Skip: the story goes on (7.7), also when it was paused meanwhile */
    closeOverlay(true);
    play();
    playBtn.focus({ preventScroll: true });
  }
  function completeIt(ui) {
    if (ui.complete) return;
    ui.complete = true; ui.skip.hidden = true; ui.cont.hidden = false;
    ui.cont.focus({ preventScroll: true });
  }
  function trapTab(e) { /* Tab stays inside a modal card */
    if (e.key !== 'Tab') return;
    var f = Array.prototype.filter.call(this.querySelectorAll('button'), function (b) { return !b.hidden && !b.disabled; });
    if (!f.length) return;
    var i = f.indexOf(doc.activeElement), next = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i + 1) % f.length;
    e.preventDefault(); f[next].focus();
  }
  function closeOverlay(markDone) {
    var ui = current;
    if (!ui) return;
    clearTimeout(ui.timer);
    if (ui.cleanup) ui.cleanup();
    if (markDone) done[ui.d.id] = true;
    current = null; overlayOpen = false;
    overlay.hidden = true; overlay.textContent = '';
    root.classList.remove('overlay-open');
    lastNow = 0;
    if (playing) syncAudio();
  }

  /* The tap_reveal bubble lives outside the scaled stage, in real CSS px, so its text stays readable on a phone.
     It sits above the tapped element, kept inside the app's width. */
  function showBubble(bubble, text, el) {
    var ar = $('app').getBoundingClientRect(), er = el.getBoundingClientRect();
    bubble.textContent = text; bubble.hidden = false;
    var bw = bubble.offsetWidth, bh = bubble.offsetHeight, cx = er.left + er.width / 2 - ar.left;
    cx = clamp(cx, bw / 2 + 8, ar.width - bw / 2 - 8);
    bubble.style.left = (cx - bw / 2) + 'px';
    bubble.style.top = Math.max(8, er.top - ar.top - bh - 14) + 'px';
  }

  var BUILD = {
    choose_answer: function (ui) {
      var d = ui.d, tries = 0, buttons = {};
      var box = mk('div', 'opts');
      shuffle(d.options, d.id).forEach(function (o) {
        var b = button('opt', o.text, function () { ui.answer({ option_id: o.id }); });
        buttons[o.id] = b; box.appendChild(b);
      });
      ui.body.appendChild(box);
      ui.answer = function (act) {
        if (ui.complete) return false;
        var o = null;
        d.options.forEach(function (x) { if (x.id === act.option_id) o = x; });
        if (!o || buttons[o.id].disabled) return false;
        if (o.correct) {
          buttons[o.id].classList.add('ok'); ui.fb.textContent = L.correct + ' — ' + o.feedback;
          Object.keys(buttons).forEach(function (k) { buttons[k].disabled = true; });
          completeIt(ui); return true;
        }
        tries++;
        buttons[o.id].classList.add('bad'); buttons[o.id].disabled = true;
        ui.fb.textContent = o.feedback;
        if (tries >= (d.max_attempts || 2)) {
          var right = d.options.filter(function (x) { return x.correct; })[0];
          buttons[right.id].classList.add('ok');
          Object.keys(buttons).forEach(function (k) { buttons[k].disabled = true; });
          ui.fb.textContent = o.feedback + ' ' + right.feedback;
          completeIt(ui);
        } else ui.fb.textContent = o.feedback + ' ' + L.try_again;
        return true;
      };
    },
    predict: function (ui) {
      var d = ui.d, buttons = [];
      var box = mk('div', 'opts');
      d.options.forEach(function (o) {
        var b = button('opt', o.text, function () { ui.answer({ option_id: o.id }); });
        b.dataset.id = o.id; buttons.push(b); box.appendChild(b);
      });
      ui.body.appendChild(box);
      ui.answer = function (act) {
        if (ui.complete || !d.options.some(function (o) { return o.id === act.option_id; })) return false;
        /* any guess is fine (7.7): the chosen option is marked as chosen, never as right or wrong */
        buttons.forEach(function (b) { b.disabled = true; if (b.dataset.id === act.option_id) b.classList.add('chosen'); });
        ui.fb.textContent = (act.option_id === d.expected_option_id ? L.good_guess : L.lets_see) + ' ' + d.reveal_text;
        completeIt(ui);
        return true;
      };
    },
    drag_match: function (ui) {
      var d = ui.d, placed = {}, wrong = {}, picked = null, chips = {}, slots = {};
      var grid = mk('div', 'match'), chipBox = mk('div', 'chips'), slotBox = mk('div', 'slots');
      shuffle(d.pairs, d.id + ':items').forEach(function (p) {
        var c = mk('button', 'chip', p.item); c.type = 'button'; c.dataset.id = p.id; chips[p.id] = c; chipBox.appendChild(c);
        c.addEventListener('click', function () {
          if (placed[p.id]) return;
          if (picked) chips[picked].classList.remove('picked');
          picked = picked === p.id ? null : p.id;
          if (picked) c.classList.add('picked');
        });
        c.addEventListener('pointerdown', function (e) { startDrag(e, p.id); });
      });
      d.pairs.forEach(function (p) {
        var s = mk('button', 'slot'); s.type = 'button'; s.dataset.id = p.id;
        s.appendChild(mk('span', 'tgt', p.target)); slots[p.id] = s; slotBox.appendChild(s);
        s.addEventListener('click', function () { if (picked) { var it = picked; picked = null; chips[it].classList.remove('picked'); ui.answer({ drop: { item_id: it, target_id: p.id } }); } });
      });
      grid.appendChild(chipBox); grid.appendChild(slotBox); ui.body.appendChild(grid);
      function place(id) {
        placed[id] = true; chips[id].classList.add('placed');
        slots[id].classList.add('filled'); slots[id].appendChild(mk('span', 'got', chips[id].textContent));
        if (d.pairs.every(function (p) { return placed[p.id]; })) { ui.fb.textContent = L.correct; completeIt(ui); }
      }
      ui.answer = function (act) {
        var dr = act.drop || {};
        if (ui.complete || !chips[dr.item_id] || placed[dr.item_id] || !slots[dr.target_id]) return false;
        if (dr.item_id === dr.target_id) { ui.fb.textContent = ''; place(dr.item_id); return true; }
        wrong[dr.item_id] = (wrong[dr.item_id] || 0) + 1;
        ui.fb.textContent = d.wrong_feedback;
        if (wrong[dr.item_id] >= 3) place(dr.item_id);
        return true;
      };
      var ghost = null, dragId = null, sx = 0, sy = 0, moved = false;
      function startDrag(e, id) {
        if (placed[id] || ui.complete) return;
        dragId = id; sx = e.clientX; sy = e.clientY; moved = false;
        doc.addEventListener('pointermove', onMove); doc.addEventListener('pointerup', onUp); doc.addEventListener('pointercancel', onUp);
      }
      function onMove(e) {
        if (!dragId) return;
        if (!moved && Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 8) return;
        if (!moved) {
          moved = true; ghost = chips[dragId].cloneNode(true); ghost.classList.add('drag');
          ghost.style.width = chips[dragId].offsetWidth + 'px'; doc.body.appendChild(ghost);
        }
        ghost.style.left = (e.clientX - ghost.offsetWidth / 2) + 'px'; ghost.style.top = (e.clientY - ghost.offsetHeight / 2) + 'px';
      }
      function onUp(e) {
        doc.removeEventListener('pointermove', onMove); doc.removeEventListener('pointerup', onUp); doc.removeEventListener('pointercancel', onUp);
        if (ghost) { ghost.remove(); ghost = null; }
        if (moved && dragId) {
          var target = doc.elementFromPoint(e.clientX, e.clientY), slot = target && target.closest ? target.closest('.slot') : null;
          if (slot) ui.answer({ drop: { item_id: dragId, target_id: slot.dataset.id } });
        }
        dragId = null;
      }
      ui.cleanup = function () { onUp({ clientX: -1, clientY: -1 }); };
    },
    tap_reveal: function (ui) {
      var d = ui.d, svg = ui.it.sc.svg, NS = svg.namespaceURI, made = [], seen = {}, union = null;
      var list = mk('div', 'opts'); ui.body.appendChild(list);
      var bubble = mk('div', 'bubble'); bubble.hidden = true; $('app').appendChild(bubble); made.push(bubble);
      /* Hotspots sit in the scene's own coordinates (the stage, 1280x720), outside the camera group, at the place the
         target is drawn NOW (after its move/scale cues and the camera; the clock is paused). At least 64 stage px and
         44 CSS px, so a finger can hit them on a phone where the stage is scaled down. */
      var layer = doc.createElementNS(NS, 'g'); layer.setAttribute('class', 'tap-layer'); svg.appendChild(layer); made.push(layer);
      var sr = svg.getBoundingClientRect(), k = sr.width / 1280 || 1, minSide = Math.max(64, 44 / k);
      (d.bindings || []).forEach(function (b) {
        var target = d.targets[b.index], el = $(b.element_id);
        if (!target || !el) { errors.push('tap_reveal target missing: ' + b.element_id); return; }
        var r = el.getBoundingClientRect();
        union = union ? { top: Math.min(union.top, r.top), bottom: Math.max(union.bottom, r.bottom) } : { top: r.top, bottom: r.bottom };
        var bx = (r.left - sr.left) / k, by = (r.top - sr.top) / k, bw = r.width / k, bh = r.height / k;
        var w = Math.max(minSide, bw), h = Math.max(minSide, bh), x = bx + bw / 2 - w / 2, y = by + bh / 2 - h / 2;
        var ring = doc.createElementNS(NS, 'rect'), hot = doc.createElementNS(NS, 'rect');
        [ring, hot].forEach(function (q) { q.setAttribute('x', x); q.setAttribute('y', y); q.setAttribute('width', w); q.setAttribute('height', h); q.setAttribute('rx', 16); });
        ring.setAttribute('class', 'tap-ring on'); hot.setAttribute('class', 'hotspot on');
        hot.setAttribute('data-target', b.element_id); hot.setAttribute('tabindex', '0'); hot.setAttribute('role', 'button');
        hot.setAttribute('aria-label', target.label);
        hot.addEventListener('click', function (e) { e.stopPropagation(); ui.answer({ reveal: b.element_id }); });
        hot.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ui.answer({ reveal: b.element_id }); } });
        layer.appendChild(ring); layer.appendChild(hot);
      });
      /* the banner goes to the half of the screen the targets are NOT in */
      if (union && (union.top + union.bottom) / 2 < window.innerHeight / 2) overlay.classList.add('at-bottom');
      ui.answer = function (act) {
        var b = null;
        (d.bindings || []).forEach(function (x) { if (x.element_id === act.reveal) b = x; });
        if (!b) return false;
        var target = d.targets[b.index];
        if (!seen[b.index]) {
          seen[b.index] = true;
          var p = mk('p', 'fb'); p.textContent = target.label + ': ' + target.reveal_text; list.appendChild(p);
        }
        showBubble(bubble, target.reveal_text, $(b.element_id));
        if (d.targets.every(function (t, i) { return seen[i]; })) completeIt(ui);
        return true;
      };
      ui.cleanup = function () { made.forEach(function (m) { if (m.parentNode) m.parentNode.removeChild(m); }); };
    }
  };

  /* ------------------------------------------------------------------------------------------------ layout & lifecycle */
  function layout() {
    var w = wrap.clientWidth, h = wrap.clientHeight;
    stageScale = Math.max(0.05, Math.min(w / 1280, h / 720));
    stage.style.transform = 'scale(' + stageScale + ')';
    if (capEl.firstChild) fitCaption(capEl.firstChild);
    /* the rotate hint shows once: after it was dismissed or the phone was turned, it stays away (7.9) */
    var narrowPortrait = window.innerHeight > window.innerWidth && window.innerWidth < 500;
    if (narrowPortrait && !layout.dismissed) { $('rotate').hidden = false; layout.shown = true; }
    else { $('rotate').hidden = true; if (layout.shown) layout.dismissed = true; }
  }
  if (window.ResizeObserver) new ResizeObserver(layout).observe(wrap);
  window.addEventListener('resize', layout);
  doc.addEventListener('visibilitychange', function () { if (doc.hidden) pause(); });
  window.addEventListener('pagehide', pause);

  $('btn-start').textContent = L.start;
  $('btn-replay').textContent = L.replay;
  $('rotate').firstChild.textContent = L.rotate_hint;
  [['btn-restart', 'restart'], ['btn-speed', 'speed'], ['btn-cc', 'captions'], ['btn-rotate-x', 'close']].forEach(function (x) { $(x[0]).setAttribute('aria-label', L[x[1]]); });
  prog.setAttribute('aria-label', D.meta.title);
  $('btn-start').addEventListener('click', play);
  $('btn-replay').addEventListener('click', function () { seek(0); play(); playBtn.focus({ preventScroll: true }); });
  $('btn-rotate-x').addEventListener('click', function () { layout.dismissed = true; $('rotate').hidden = true; });
  playBtn.addEventListener('click', function () { if (playing) pause(); else play(); });
  $('btn-restart').addEventListener('click', function () { seek(0); play(); });
  speedBtn.addEventListener('click', function () { setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]); });
  ccBtn.addEventListener('click', function () {
    var on = capEl.classList.toggle('off') === false;
    ccBtn.setAttribute('aria-pressed', String(on));
  });
  doc.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (overlayOpen || tag === 'INPUT' || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === ' ') {
      if (tag === 'BUTTON') return;                    /* Space already presses the focused button */
      e.preventDefault(); if ($('start').hidden && playing) pause(); else play();
    }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      var fwd = (e.key === 'ArrowRight') !== RTL; seek(T + (fwd ? 5000 : -5000));
    } else if (e.key === 'c' || e.key === 'C') ccBtn.click();
  });

  /* ------------------------------------------------------------------------------------------------ tap reactions */
  /* An addition to the plan (docs/DECISIONS.md): a tapped character waves, smiles and shows its name; a tapped prop hops
     and shows its name. They never pause the story, are off while a question is open, and are not one of the story's
     interactions (D7 is unchanged). Pointer only: the keyboard path through the story stays as it was. */
  function tapReact(el) {
    var sc = scenes[active];
    if (overlayOpen || ended || !el || !sc || !sc.svg.contains(el)) return false;
    var name = '';
    if (el.classList.contains('actor')) {
      var ch = D.characters[el.id.slice(sc.d.id.length + 1)];
      name = ch && ch.name || '';
      react[el.id] = performance.now() + 1500;
      applyStates(sc, T);
      setTimeout(function () { if (scenes[active] === sc) applyStates(sc, T); }, 1550);
    } else name = el.getAttribute('data-name') || '';
    if (!reduce && !freeze && el.animate) {
      try { el.animate([{ translate: '0 0' }, { translate: '0 -16px' }, { translate: '0 0' }], { duration: 460, easing: 'ease-out' }); }
      catch (e) { /* a browser without individual transform properties: the name still shows */ }
    }
    if (name) {
      showBubble(tapBubble, name, el);
      clearTimeout(tapTimer);
      tapTimer = setTimeout(function () { tapBubble.hidden = true; }, 1800);
    }
    return true;
  }
  stage.addEventListener('click', function (e) {
    tapReact(e.target && e.target.closest ? e.target.closest('.actor, .el.prop') : null);
  });

  /* ------------------------------------------------------------------------------------------------ test hooks (7.10) */
  window.__story = {
    getDuration: function () { return DUR; },
    getTime: function () { return T; },
    seek: function (ms) { leaveStart(); seek(ms); },
    play: play,
    pause: pause,
    setSpeed: setSpeed,
    listInteractions: function () { return inters.map(function (it) { return { id: it.d.id, type: it.d.type, at_ms: it.d.at_ms }; }); },
    openInteraction: function (id) {
      var it = inters.filter(function (x) { return x.d.id === id; })[0];
      if (!it) return false;
      leaveStart(); seek(it.d.at_ms); openInteraction(it); return true;
    },
    tap: function (id) { leaveStart(); return tapReact($(id)); },
    answerInteraction: function (id, act) {
      if (!current || current.d.id !== id || !current.answer) return false;
      return current.answer(act || {}) !== false;
    },
    getState: function () {
      return { t: T, playing: playing, activeScene: active >= 0 ? scenes[active].d.id : null, overlayOpen: overlayOpen,
        ended: ended, errors: errors.slice(), interaction: current ? current.d.id : null };
    },
    getFrameInfo: function () {
      var sc = scenes[active], sr = stage.getBoundingClientRect(), k = sr.width / 1280, out = { scene: sc.d.id, actors: [], props: [], widgets: [] };
      sc.svg.querySelectorAll('.actor, .el').forEach(function (e) {
        var r = e.getBoundingClientRect();
        if (!r.width || !r.height) return;
        var item = { id: e.id, key: e.id.slice(sc.d.id.length + 1), opacity: parseFloat(getComputedStyle(e).opacity),
          bbox: [(r.left - sr.left) / k, (r.top - sr.top) / k, (r.right - sr.left) / k, (r.bottom - sr.top) / k] };
        (e.classList.contains('actor') ? out.actors : e.classList.contains('widget') ? out.widgets : out.props).push(item);
      });
      return out;
    }
  };

  layout();
  render(0, true);
  setPlayingUI();
  requestAnimationFrame(frame);
})();
