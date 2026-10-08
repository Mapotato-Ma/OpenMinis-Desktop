/* 桌面本地偏好模块（ui-prefs.js）的回归检查。
 *
 *   node --test web/scripts/ui-prefs.check.mjs
 *
 * 为什么值得钉：这些值全部**不进内核 settings payload**（全量替换 PUT 会把
 * 未知字段整单拒掉），全部只在 localStorage 里，错了不会报错、只会「看起来
 * 设了但没生效」。缩放又有两个坑：档位换算（VS Code 的 1.2^n）与「宿主原生
 * 缩放已接管时不能叠加 CSS zoom」。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const SRC = readFileSync(new URL('../desktop/ui-prefs.js', import.meta.url), 'utf8');

/** 最小 DOM/存储替身：ui-prefs 只用到这几个口子。 */
function harness(initial) {
  const store = new Map(Object.entries(initial || {}));
  const classes = new Set();
  const props = {};
  const style = {
    zoom: '',
    setProperty: (k, v) => { props[k] = v; },
    removeProperty: (k) => { delete props[k]; },
  };
  const root = {
    style,
    classList: {
      toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); },
      contains: (name) => classes.has(name),
    },
  };
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  const getComputedStyle = () => ({
    getPropertyValue: (name) => (name === '--sans' ? '-apple-system, "Segoe UI"' : 'Consolas, monospace'),
  });
  const win = { innerHeight: 900, addEventListener: (t, fn) => { win.handlers[t] = fn; }, handlers: {} };
  new Function('module', 'window', 'document', 'localStorage', 'getComputedStyle', SRC)(
    { exports: {} }, win, { documentElement: root }, localStorage, getComputedStyle);
  return { P: win.UiPrefs, store, props, classes, style, root, win };
}

test('档位换算 = VS Code 的 1.2^n', () => {
  const { P } = harness();
  assert.equal(P.zoomFromLevel(0), 1);
  assert.equal(P.zoomFromLevel(1), 1.2);
  assert.equal(P.zoomFromLevel(2), 1.44);
  assert.equal(P.zoomFromLevel(3), 1.728);
  assert.equal(P.zoomFromLevel(-1), 0.833);
  assert.equal(P.levelFromZoom(1), 0);
  assert.equal(P.levelFromZoom(1.2), 1);
});

test('0.5–5 两端都真的够得着（靠夹取）', () => {
  const { P } = harness();
  assert.equal(P.zoomFromLevel(9), 5);            // 1.2^9 = 5.16 → 夹到 5
  assert.equal(P.zoomFromLevel(8), 4.3);
  assert.equal(P.zoomFromLevel(-4), 0.5);         // 1.2^-4 = 0.482 → 夹到 0.5
  assert.equal(P.zoomFromLevel(-99), 0.5);        // 档位本身也被夹
  assert.equal(P.zoomFromLevel(99), 5);
});

test('非法/脏值一律回落到合法档位', () => {
  const { P } = harness({ 'om.uiZoomLevel': 'abc', 'om.compact': 'maybe-not' });
  assert.equal(P.get('uiZoomLevel'), 0);
  assert.equal(P.get('compact'), true);           // 非空且不在否定词里 → true（不静默变 false）
  assert.equal(P.set('uiZoomLevel', 999), 9);
  assert.equal(P.set('uiZoomLevel', '-3'), -3);
});

test('缩放落到 documentElement.style.zoom，并持久化', () => {
  const { P, style, store } = harness();
  P.set('uiZoomLevel', 2);
  assert.equal(style.zoom, '1.44');
  assert.equal(store.get('om.uiZoomLevel'), '2');
  P.zoomReset();
  assert.equal(style.zoom, '1');
  assert.equal(P.zoomPercent(), 100);
  assert.equal(P.zoomLabel(), '100%');
});

test('原生缩放只有**证实落到本窗口**后才接管（否则会相乘，或者画面一动不动）', () => {
  const { P, style } = harness();
  const seen = [];
  P.setNativeZoomHook((z) => { seen.push(z); return true; });
  P.set('uiZoomLevel', 1);
  assert.deepEqual(seen, [1.2], '钩子该被调用（后台去试原生）');
  assert.equal(style.zoom, '1.2', '还没证实之前必须留着 CSS —— 不然双开时画面根本没有反应');
  assert.equal(P.trustNativeZoom(true), true);
  assert.equal(style.zoom, '', '证实接管后不能留 CSS zoom（会乘起来）');
  P.zoomIn();
  assert.equal(style.zoom, '', '快路径：已信任的窗口每次缩放都不该再垫一层 CSS');
  assert.equal(seen.length, 2);
  // 钩子抛错 / 明确说不支持 → 必须退回 CSS 路径，而不是没有缩放
  P.setNativeZoomHook(() => { throw new Error('no native handle'); });
  P.set('uiZoomLevel', 2);
  assert.equal(style.zoom, '1.44');
  P.setNativeZoomHook(() => false);
  P.zoomReset();
  assert.equal(style.zoom, '1');
  P.setNativeZoomHook(null);
  assert.equal(P.isNativeTrusted(), false, '换钩子（含清空）都要重新证');
});

test('证实失败 = 宿主的 ok 只证明了「它自己那个窗口被缩了」→ 回落 CSS 且仍能缩放', () => {
  // 2026-10-08 PC 双开实测：后端进程只能操作它自己窗口的 WebView2，
  // 第二个窗口的 POST 拿到 ok:true，可本窗口的视口宽度一点没变。
  const { P, style } = harness();
  P.setNativeZoomHook(() => true);
  P.zoomIn();
  assert.equal(P.trustNativeZoom(false), false);
  assert.equal(style.zoom, '1.2', '回落时必须真有 CSS zoom，否则用户看到的就是「缩放失效」');
  assert.equal(P.isNativeTrusted(), false);
  P.zoomIn();
  assert.equal(style.zoom, '1.44', '回落之后继续缩放还得能用（走 CSS）');
});

test('档位边界处不许越界（菜单置灰用）', () => {  const { P } = harness();
  P.set('uiZoomLevel', 9);
  assert.equal(P.zoomCanIn(), false);
  assert.equal(P.zoomCanOut(), true);
  P.set('uiZoomLevel', -4);
  assert.equal(P.zoomCanOut(), false);            // 已到 0.5，再缩没意义
  assert.equal(P.zoomCanIn(), true);
});

test('三入口共用一个 setter：订阅者收到广播，退订后不再收到', () => {
  const { P } = harness();
  const got = [];
  const off = P.subscribe((key, value, snap, changed) => got.push([key, value, changed]));
  P.zoomIn();
  P.zoomOut();
  assert.equal(got.length, 2);
  assert.deepEqual(got[0], ['uiZoomLevel', 1, true]);
  P.set('uiZoomLevel', 3);
  assert.deepEqual(got[2], ['uiZoomLevel', 3, true]);
  // 值没变也要广播（DOM 可能被重渲染冲掉），但 changed=false
  P.set('uiZoomLevel', 3);
  assert.deepEqual(got[3], ['uiZoomLevel', 3, false]);
  off();
  P.zoomIn();
  assert.equal(got.length, 4);
});

test('字体名进 CSS 变量前被消毒（不能带分号/花括号/换行）', () => {
  const { P, props } = harness();
  P.set('fontSans', 'Foo; body{display:none}\nX');
  assert.equal(P.get('fontSans'), 'Foo body display:none X');   // 分号/花括号/换行都被抹平 → 注入不成立
  assert.equal(props['--sans'], '"Foo body display:none X", -apple-system, "Segoe UI"');
  P.set('fontMono', '   ');
  assert.equal(props['--mono'], undefined);                     // 空值 = 用回内置栈
  assert.equal(P.fontStack('', 'A, B'), '');
  assert.equal(P.fontStack('"Quoted Name"', ''), '"Quoted Name"');
  assert.equal(P.safeFamily('x'.repeat(200)).length, 64);
});

test('开关类偏好走 <html> 上的 class（不进 body，head 里就能生效）', () => {
  const { P, classes } = harness({ 'om.compact': '1', 'om.showStatusbar': '0' });
  assert.equal(classes.has('compact'), true);
  assert.equal(classes.has('no-statusbar'), true);
  assert.equal(classes.has('no-motion'), false);
  P.set('reduceMotion', true);
  assert.equal(classes.has('no-motion'), true);
  P.set('showStatusbar', true);
  assert.equal(classes.has('no-statusbar'), false);
});

test('主题沿用既有的 om.themeMode 键（不另起一份）', () => {
  const { P, store } = harness({ 'om.themeMode': 'light' });
  assert.equal(P.get('theme'), 'light');
  P.set('theme', 'dark');
  assert.equal(store.get('om.themeMode'), 'dark');
  assert.equal(store.has('om.theme'), false);
  assert.equal(P.set('theme', 'nonsense'), 'system');
});

test('缩放时补偿根高度 --ui-h（否则页面比视口高、焦点滚动会把整页顶上去）', () => {
  // 用户实测（144%）：点「新会话」后标题栏消失、整页上移。根因是 vh 不跟着 zoom 缩小，
  // 100vh 的 #app 按放大后的视口铺满 → 页面比视口高一截。补偿 = innerHeight / zoom。
  const h = harness();
  h.P.zoomIn();                                   // 120%
  assert.equal(h.style.zoom, '1.2');
  assert.equal(h.props['--ui-h'], '750.00px', '120% 时根高度应写成 900/1.2');
  h.P.zoomReset();
  assert.equal(h.props['--ui-h'], undefined, '回到 100% 就该把补偿去掉（否则根高度被写死）');
  h.P.zoomIn(); h.P.zoomIn();                      // 144%
  assert.equal(h.props['--ui-h'], '625.00px');
  assert.equal(typeof h.win.handlers.resize, 'function', '没有监听窗口变化，改窗口大小后补偿会失真');
  h.win.innerHeight = 450;
  h.win.handlers.resize();
  assert.equal(h.props['--ui-h'], '312.50px', '窗口变高变矮后没有重算补偿');
});

test('宿主原生缩放接管时不写 --ui-h（原生缩放自己就管视口）', () => {
  const h = harness();
  h.P.setNativeZoomHook(() => true);
  h.P.trustNativeZoom(true);
  h.P.zoomIn();
  assert.equal(h.style.zoom, '', '原生缩放接管了还留着 CSS zoom 会乘起来');
  assert.equal(h.props['--ui-h'], undefined, '原生缩放下不该再补偿 CSS 的 vh 语义');
});
