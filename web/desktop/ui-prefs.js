/* Desktop-local UI preferences — the single source of truth for settings that
   belong to *this machine*, not to the kernel.

   Nothing in here ever reaches the settings payload: the kernel PUT is a full
   replace that rejects unknown keys, so mixing local prefs into it would 400
   and take every other pane down with it (review finding A2).

   Every write goes through set() → one writer, one subscription event, so the
   menu, the settings pane and the shortcuts can never drift apart (A4).

   Loaded as a classic script (no build step). Single export: window.UiPrefs. */
(function () {
  'use strict';

  const STORE_PREFIX = 'om.';
  const root = document.documentElement;

  /* VS Code zoom levels: zoom = 1.2 ** level, level is an integer. */
  const ZOOM_BASE = 1.2;
  const ZOOM_MIN = 0.5;
  const ZOOM_MAX = 5;
  /* Levels are integers (the UI steps are buttons, not a text field). The
     bounds are chosen so both ends of the 0.5–5 range are actually reachable:
     1.2**9 = 5.16 clamps to 5, 1.2**-4 = 0.48 clamps to 0.5. */
  const LEVEL_MIN = -8;
  const LEVEL_MAX = 9;

  function clamp(n, lo, hi) { return n < lo ? lo : (n > hi ? hi : n); }

  function zoomFromLevel(level) {
    const n = clamp(Math.round(Number(level) || 0), LEVEL_MIN, LEVEL_MAX);
    return clamp(Math.round(Math.pow(ZOOM_BASE, n) * 1000) / 1000, ZOOM_MIN, ZOOM_MAX);
  }

  function levelFromZoom(zoom) {
    const z = clamp(Number(zoom) || 1, ZOOM_MIN, ZOOM_MAX);
    return clamp(Math.round(Math.log(z) / Math.log(ZOOM_BASE)), LEVEL_MIN, LEVEL_MAX);
  }

  /* The built-in font stacks, captured before anything can override them, so a
     custom family can be prepended instead of replacing the fallbacks. */
  let defaultSans = '';
  let defaultMono = '';
  try {
    const cs = getComputedStyle(root);
    defaultSans = (cs.getPropertyValue('--sans') || '').trim();
    defaultMono = (cs.getPropertyValue('--mono') || '').trim();
  } catch (e) { /* no layout yet — apply() will still work, stacks stay empty */ }

  /* A font name from a text field ends up inside a CSS value, so keep it inert:
     no declaration/section breaks, no newlines, bounded length. It is always
     written with setProperty(), never interpolated into a style string. */
  function safeFamily(name) {
    return String(name == null ? '' : name)
      .replace(/[;{}\n\r]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 64);
  }

  function fontStack(custom, fallback) {
    const fam = safeFamily(custom);
    if (!fam) return '';
    const quoted = /^["'].*["']$/.test(fam) ? fam : '"' + fam + '"';
    return fallback ? quoted + ', ' + fallback : quoted;
  }

  function noop() {}

  function boolish(v) {
    if (v === true || v === false) return v;
    const s = String(v == null ? '' : v).toLowerCase();
    if (s === '1' || s === 'true' || s === 'on' || s === 'yes') return true;
    if (s === '0' || s === 'false' || s === 'off' || s === 'no' || s === '') return false;
    return !!v;
  }

  /* WebView2 can zoom natively (crisp, dPR-aware). When the host bridge hands
     us a working hook we use it and leave CSS zoom alone — otherwise the two
     would multiply (review finding A1). Loaded before first paint on purpose:
     applying zoom in app.js init made a 1.5 zoom flash at 100% first (A5). */
  let nativeZoom = null;

  function setNativeZoomHook(fn) {
    nativeZoom = typeof fn === 'function' ? fn : null;
  }

  function applyZoom(level) {
    const z = zoomFromLevel(level);
    let handled = false;
    if (nativeZoom) {
      try { handled = nativeZoom(z) !== false; } catch (e) { handled = false; }
    }
    root.style.zoom = handled ? '' : String(z);
    return z;
  }

  function setStack(varName, custom, fallbackStack) {
    const stack = fontStack(custom, fallbackStack);
    if (!stack) root.style.removeProperty(varName);
    else root.style.setProperty(varName, stack);
  }

  /* key → how to validate, where to store, how to apply.
     Theme deliberately keeps its existing storage key so there is still exactly
     one place a theme is remembered. `apply: noop` means app.js owns the effect
     and subscribes to changes instead (wired in the 界面 pane step). */
  const PREFS = {
    uiZoomLevel: {
      def: 0,
      parse: (v) => clamp(Math.round(Number(v) || 0), LEVEL_MIN, LEVEL_MAX),
      store: (v) => String(v),
      apply: applyZoom,
    },
    theme: {
      storageKey: 'om.themeMode',
      def: 'system',
      parse: (v) => (v === 'dark' || v === 'light' || v === 'system' ? v : 'system'),
      store: (v) => String(v),
      apply: noop,
    },
    fontSans: { def: '', parse: safeFamily, store: (v) => v, apply: (v) => setStack('--sans', v, defaultSans) },
    fontMono: { def: '', parse: safeFamily, store: (v) => v, apply: (v) => setStack('--mono', v, defaultMono) },
    compact: { def: false, parse: boolish, store: (v) => (v ? '1' : '0'), apply: (v) => root.classList.toggle('compact', v) },
    reduceMotion: { def: false, parse: boolish, store: (v) => (v ? '1' : '0'), apply: (v) => root.classList.toggle('no-motion', v) },
    showStatusbar: { def: true, parse: boolish, store: (v) => (v ? '1' : '0'), apply: (v) => root.classList.toggle('no-statusbar', !v) },
    restoreLastSession: { def: true, parse: boolish, store: (v) => (v ? '1' : '0'), apply: noop },
    enterToSend: { def: true, parse: boolish, store: (v) => (v ? '1' : '0'), apply: noop },
  };

  const KEYS = Object.keys(PREFS);
  const state = {};
  const listeners = [];

  function storageKeyOf(key) {
    const e = PREFS[key];
    return e.storageKey || STORE_PREFIX + key;
  }

  function readRaw(key) {
    try { return localStorage.getItem(storageKeyOf(key)); }
    catch (e) { return null; }  /* private mode / blocked storage */
  }

  function writeRaw(key, value) {
    try { localStorage.setItem(storageKeyOf(key), value); } catch (e) { /* ignore */ }
  }

  function snapshot() {
    const out = {};
    for (let i = 0; i < KEYS.length; i++) out[KEYS[i]] = state[KEYS[i]];
    return out;
  }

  function emit(key, changed) {
    const snap = snapshot();
    for (let i = 0; i < listeners.length; i++) {
      try { listeners[i](key, state[key], snap, changed !== false); }
      catch (e) { /* a bad listener must not break the writer */ }
    }
  }

  function subscribe(fn) {
    if (typeof fn !== 'function') return noop;
    listeners.push(fn);
    let live = true;
    return function unsubscribe() {
      if (!live) return;
      live = false;
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  function get(key) { return state[key]; }

  function set(key, value) {
    const entry = PREFS[key];
    if (!entry) return undefined;
    const next = entry.parse ? entry.parse(value) : value;
    const same = state[key] === next;
    state[key] = next;
    entry.apply(next);
    if (!same) writeRaw(key, entry.store ? entry.store(next) : String(next));
    /* Emitted even when the value did not change: a re-render can drop the
       class/attribute we toggled, so callers get a chance to re-assert. */
    emit(key, !same);
    return next;
  }

  function applyAll() {
    for (let i = 0; i < KEYS.length; i++) {
      const key = KEYS[i];
      state[key] = PREFS[key].parse(readRaw(key) === null ? PREFS[key].def : readRaw(key));
      PREFS[key].apply(state[key]);
    }
    return snapshot();
  }

  /* ── zoom commands (menu / pane / shortcuts all land here) ── */
  function zoomLevel() { return state.uiZoomLevel; }
  function zoomValue() { return zoomFromLevel(state.uiZoomLevel); }
  function zoomPercent() { return Math.round(zoomValue() * 100); }
  function zoomLabel() { return zoomPercent() + '%'; }
  function zoomBy(delta) { return set('uiZoomLevel', state.uiZoomLevel + delta); }
  function zoomIn() { return zoomBy(1); }
  function zoomOut() { return zoomBy(-1); }
  function zoomReset() { return set('uiZoomLevel', 0); }
  function zoomCanIn() { return state.uiZoomLevel < LEVEL_MAX && zoomValue() < ZOOM_MAX; }
  function zoomCanOut() { return state.uiZoomLevel > LEVEL_MIN && zoomValue() > ZOOM_MIN; }

  window.UiPrefs = {
    keys: KEYS.slice(),
    get: get,
    set: set,
    all: snapshot,
    subscribe: subscribe,
    applyAll: applyAll,
    setNativeZoomHook: setNativeZoomHook,
    zoomFromLevel: zoomFromLevel,
    levelFromZoom: levelFromZoom,
    zoomLevel: zoomLevel,
    zoomValue: zoomValue,
    zoomPercent: zoomPercent,
    zoomLabel: zoomLabel,
    zoomIn: zoomIn,
    zoomOut: zoomOut,
    zoomReset: zoomReset,
    zoomBy: zoomBy,
    zoomCanIn: zoomCanIn,
    zoomCanOut: zoomCanOut,
    zoomMin: ZOOM_MIN,
    zoomMax: ZOOM_MAX,
    safeFamily: safeFamily,
    fontStack: fontStack,
    applyAllOnLoad: applyAll(),
  };
})();
