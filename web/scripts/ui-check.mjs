/* 桌面界面的「能不能跑起来」检查 —— 专门盯住那类**静默死掉**的错。
 *
 *   node --test web/scripts/ui-check.mjs
 *
 * 为什么需要它：桌面界面是没有构建步骤的多个 classic script，**共享同一个全局作用域**。
 * 于是「第二个脚本顶层再声明一个同名 const/function」不是覆盖，而是 SyntaxError ——
 * 整个脚本不执行，页面渲染得出静态 HTML 但**点什么都点不动**。
 * 这类错 node --check 单文件看不出来（每个文件自己都是合法的），冒烟测试也看不出来
 * （它只断言静态资源返回 200，不执行 JS）。实测：把 app.js 改成坏的时候，
 * 前端检查 27 项 + 冒烟 21 项**全绿**。所以这里补上。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import path from 'node:path';

const DESKTOP = new URL('../desktop/', import.meta.url);
const read = (name) => readFileSync(new URL(name, DESKTOP), 'utf8');

/* 磁盘上不存在、由服务端路由现算的脚本：`/_desktop/window-bootstrap.js`
   （告诉界面「你在原生窗口里」）。它没有文件，内容就是下面这一行。 */
const SERVER_COMPUTED = { 'window-bootstrap.js': 'window.__OPENMINIS_DESKTOP__ = true;\n' };

const sourceOf = (name) => (name in SERVER_COMPUTED ? SERVER_COMPUTED[name] : read(name));

/** index.html 里的脚本，带 type（module 与 classic 的作用域规则不同，要分开处理）。 */
function scripts() {
  const html = read('index.html');
  return [...html.matchAll(/<script([^>]*)\ssrc="([^"]+)"([^>]*)>/g)].map((m) => ({
    src: m[2].replace(/^\.\//, ''),
    module: /type="module"/.test(m[1] + m[3]),
  }));
}
const classicScripts = () => scripts().filter((s) => !s.module).map((s) => s.src);

test('index.html 引的脚本都存在（少一个就是 404 + 界面半死）', () => {
  const all = scripts();
  assert.ok(all.length > 0, 'index.html 里没找到 <script src>');
  for (const { src } of all) {
    if (src in SERVER_COMPUTED) continue;      // 见上：它由服务端现算
    assert.ok(existsSync(new URL(src, DESKTOP)), `index.html 引了 ${src}，但磁盘上没有`);
  }
});

test('启动画面必须自包含（它渲染时后端还不存在）', () => {
  const html = read('splash.html');
  // splash 由壳层以 html= 直接塞进 WebView，那一刻没有任何 HTTP 服务：
  // 任何外链（脚本/样式/字体/图片）都会 404，表现是白屏一把。
  const external = [...html.matchAll(/\s(?:src|href)="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(external, [], `splash.html 不能引用外部资源：${external.join(', ')}`);
  // 这两个名字是壳层（desktop/launcher.py）推 JS 时用的约定，改一边忘另一边就是转圈转到天荒地老。
  for (const hook of ['__bootStatus', '__bootFail']) {
    assert.ok(html.includes(hook), `splash.html 里没有 ${hook}`);
  }
  // 数据目录是内核决定的（Windows 上是 %USERPROFILE%\openminis，不是
  // %LOCALAPPDATA%\openminis）。页面里写死路径 = 让用户按错的路径去找日志；
  // 界面里唯一一处该显示它的地方（设置页）是问后端要的。
  assert.ok(!/%LOCALAPPDATA%|%USERPROFILE%|%APPDATA%/.test(html), 'splash.html 不该硬编码数据目录');
});

/* 我们自己的代码里唯一允许写成 module 的：它必须 import 组件库的 ESM 注册 API，
   classic script 做不到这件事。白名单长度被钉在 1 —— 它是例外，不是新惯例。 */
const MODULE_ALLOWLIST = ['icons.js'];

test('module 脚本只能是 vendor 进来的第三方库（外加一个钉死的例外）', () => {
  // 我们自己的代码全是 classic script —— 上面那条「拼起来解析」的检查只对
  // classic 有效（module 有独立作用域，撞名规则完全不同）。
  // 把界限钉住：一旦有人给我们自己的代码加上 type="module"，那条检查就悄悄失效了。
  for (const { src, module: isModule } of scripts()) {
    if (!isModule) continue;
    assert.ok(src.startsWith('vendor/') || MODULE_ALLOWLIST.includes(src),
      `${src} 是 module，但既不是 vendor 的第三方库，也不在白名单里`);
  }
  assert.equal(MODULE_ALLOWLIST.length, 1, '白名单里不该再多了 —— 加之前先想清楚为什么');
});

test('菜单栏接线没掉（三个菜单、页签名真实存在）', () => {
  const html = read('index.html');
  assert.match(html, /<nav class="menubar"/, 'index.html 里没有菜单栏');
  const app = read('app.js');
  for (const group of ['file:', 'view:', 'help:']) {
    assert.ok(app.includes(group), `MENUS 里缺 ${group} 这组菜单`);
  }
  // 曾经踩过：菜单里写 openSettings('info')，但设置页的页签叫 about ——
  // 'info' 是左侧栏那个面板，两边不是一回事，点了什么也不会发生。
  const panes = new Set([...html.matchAll(/data-pane="([^"]+)"/g)].map((m) => m[1]));
  const used = [...app.matchAll(/openSettings\('([^']+)'\)/g)].map((m) => m[1]);
  for (const name of used) {
    assert.ok(panes.has(name), `openSettings('${name}') 指向不存在的设置页签（有：${[...panes].join('/')}）`);
  }
  assert.ok(app.includes('packagedOnly'), '「退出」这类只在打包版有的项没有 packagedOnly 标记');
});

test('多标签接线没掉（含「上限要在读盘之前判」）', () => {
  assert.match(read('index.html'), /id="editorTabs"/, 'index.html 里没有标签栏');
  const app = read('app.js');
  assert.match(app, /openFiles: \[\]/, 'state 里没有 openFiles');
  assert.match(app, /OPEN_FILE_MAX = \d+/, '同时打开的上限没定义');
  // 复核点：上限必须在发请求之前判，否则第 11 个文件会白读一次再丢掉
  const start = app.indexOf('async function openFile');
  const body = app.slice(start, start + 1500);
  const cap = body.indexOf('OPEN_FILE_MAX');
  const readAt = body.indexOf("fsUrl('read'");   // 别叫 read：会遮蔽模块顶部的 read() 助手
  assert.ok(cap > 0 && readAt > 0, 'openFile 里找不到上限判断或读盘调用');
  assert.ok(cap < readAt, '上限判断必须在读盘之前');
  assert.match(app, /state\.openFiles = \[\]/, 'closeFileView 没有清空标签（切工作区会留下旧根的文件）');
});

test('图标库接线没掉（本地 Lucide + 注册模块都在）', () => {
  const html = read('index.html');
  assert.ok(html.includes('src="./icons.js"'), 'index.html 没有再引 icons.js');
  assert.ok(existsSync(new URL('icons.js', DESKTOP)), 'icons.js 不在磁盘上');
  assert.ok(existsSync(new URL('vendor/lucide/', DESKTOP)), '本地图标目录不在（vendor-lucide.mjs 没跑？）');
  const icons = readdirSync(new URL('vendor/lucide/', DESKTOP)).filter((f) => f.endsWith('.svg'));
  assert.ok(icons.length > 20, `本地图标只有 ${icons.length} 个，像是 vendor 没跑完整`);
  assert.match(read('icons.js'), /registerIconLibrary\('om'/,
    'icons.js 没有注册本地图标库');
});

test('组件库接线没掉（样式表在、主题类跟着 data-theme 走）', () => {
  const html = read('index.html');
  for (const need of ['vendor/webawesome/styles/webawesome.css', 'wa-theme.css']) {
    assert.ok(html.includes(need), `index.html 没有再引 ${need}`);
    assert.ok(existsSync(new URL(need, DESKTOP)), `${need} 不在磁盘上（vendor 没跑？）`);
  }
  assert.match(html, /class="wa-theme-default"/,
    'Web Awesome 的主题类不在 <html> 上 —— 组件会退回它自己的默认配色');
  const app = read('app.js');
  for (const cls of ["'wa-dark'", "'wa-light'"]) {
    assert.ok(app.includes(cls),
      `app.js 没有把 data-theme 同步到 ${cls} —— 切主题时组件会停在上一次的配色`);
  }
});

test('文件面板与中间预览的接线没掉', () => {
  const html = read('index.html');
  // 文件名从文件头搬到了标签上（2026-09-29），所以这里要的是面包屑 + 标签栏，不再是 fileTitle
  for (const need of ['id="fileView"', 'id="fileBody"', 'id="wsPicker"', 'id="btnPickRoot"',
                      'id="btnCloseFile"', 'id="fileCrumbs"', 'id="editorTabs"']) {
    assert.ok(html.includes(need), `index.html 里没有 ${need}`);
  }
  const app = read('app.js');
  // 关键回归点：每一次 /fs 调用都得走 fsUrl()（带当前工作区）。
  // 漏一处的表现是「在指定目录里点文件夹是空的」—— 我第一版就漏了展开那一处。
  const raw = app.split('\n').filter((l) => /api\(`\/fs\//.test(l));
  assert.deepEqual(raw, [], `这些 /fs 调用没走 fsUrl()：\n${raw.join('\n')}`);
  assert.ok(app.includes('function fsUrl('), 'app.js 里没有 fsUrl()');
  assert.ok(app.includes("send('/chats/workspaces'".replace("send", "api")) || app.includes("api('/chats/workspaces'"),
    '工作区接口路径不对（内核挂在 /api/chats 下面）');
});

test('全部脚本拼起来能解析（跨脚本的重复声明会在这里现形）', () => {
  const scripts = classicScripts().filter((s) => s.endsWith('.js'));
  // 关键：**拼成一个程序再解析** —— 每个文件单独看都是合法的，只有放回
  // 「共享全局作用域」这个真实条件下，重复声明才会报 SyntaxError。
  const combined = scripts.map((s) => `\n/* ===== ${s} ===== */\n${sourceOf(s)}`).join('\n');
  try {
    // 只解析不执行：new Function 把整段当函数体解析，语法或声明冲突会抛。
    new Function(combined); // eslint-disable-line no-new-func
  } catch (e) {
    assert.fail(`桌面脚本无法解析（页面会整块不动）：${e.message}\n脚本顺序：${scripts.join(' → ')}`);
  }
});

test('每个脚本自己也要能解析（拼起来之前先分清是谁的错）', () => {
  for (const s of classicScripts().filter((s) => s.endsWith('.js'))) {
    try {
      new Function(sourceOf(s)); // eslint-disable-line no-new-func
    } catch (e) {
      assert.fail(`${s} 自身语法有错：${e.message}`);
    }
  }
});

test('settings-model.js 只往外放一个全局名字', () => {
  // 「模块不许漏名字到全局」是它包 IIFE 的理由：漏出去就可能和 app.js 撞名，
  // 而撞名的表现是 SyntaxError（上面那条测的就是这个），不是覆盖。
  const fake = {};
  const mod = { exports: {} };
  new Function('module', 'window', sourceOf('settings-model.js'))(mod, fake);

  assert.deepEqual(Object.keys(fake), ['SettingsModel'],
    `settings-model.js 往 window 上放了这些名字：${Object.keys(fake).join(', ')}`);
  assert.equal(typeof fake.SettingsModel.reduce, 'function');
  assert.equal(typeof fake.SettingsModel.toPayload, 'function');
  assert.equal(typeof fake.SettingsModel.readinessBanner, 'function');
});

test('界面依赖的内核接口都真的被调用了（避免「画了但没人喂数据」）', () => {
  const app = read('app.js');
  for (const endpoint of ['/desktop/chat-readiness', '/settings', '/desktop/info']) {
    assert.ok(app.includes(endpoint), `app.js 里没有再调用 ${endpoint}`);
  }
});

test('ui-prefs.js 只往外放一个全局名字，且 head 里同步跑不会抛', () => {
  // 它在 <head> 里同步执行，那时 <body> 还不存在 —— 所以开关只许碰 <html>。
  const classes = new Set();
  const props = {};
  const documentEl = {
    style: {
      setProperty: (k, v) => { props[k] = v; },
      removeProperty: (k) => { delete props[k]; },
    },
    classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
  };
  const fake = {};
  new Function('module', 'window', 'document', 'localStorage', 'getComputedStyle',
    sourceOf('ui-prefs.js'))(
    undefined,
    fake,
    { documentElement: documentEl },
    { getItem: () => null, setItem: () => {} },
    () => ({ getPropertyValue: () => 'sans-serif' }),
  );
  assert.deepEqual(Object.keys(fake), ['UiPrefs'],
    `ui-prefs.js 往 window 上放了：${Object.keys(fake).join(', ')}`);
  assert.equal(typeof fake.UiPrefs.set, 'function');
  assert.equal(typeof fake.UiPrefs.zoomLabel, 'function');
});

test('桌面本地偏好：head 里同步加载，且在 app.js 之前', () => {
  const html = read('index.html');
  const i = html.indexOf('<script src="./ui-prefs.js"></script>');
  const j = html.indexOf('<script src="./app.js"></script>');
  assert.ok(i > 0, 'index.html 里没有引入 ui-prefs.js');
  assert.ok(j > i, 'ui-prefs.js 必须在 app.js 之前');
  assert.ok(html.indexOf('<body>') > i,
    'ui-prefs.js 要放在 <head> 里同步执行，否则高缩放档会先闪一帧 100%');
});

test('界面缩放只有一个出口：写 style.zoom 的只许是 ui-prefs.js', () => {
  for (const s of classicScripts()) {
    if (s === 'ui-prefs.js') {
      assert.ok(sourceOf(s).includes('style.zoom'), 'ui-prefs.js 不再写 style.zoom');
      continue;
    }
    assert.ok(!/\.style\.zoom\s*=/.test(sourceOf(s)),
      `${s} 里也在写 style.zoom —— 缩放出现第二个出口`);
  }
  // 快捷键判 ev.code，不判 ev.key：Shift 下 ev.key 会变成 '+'，小键盘也对不上
  const app = read('app.js');
  for (const code of ["'Equal'", "'Minus'", "'Digit0'"]) {
    assert.ok(app.includes(`ev.code === ${code}`), `app.js 缺少 ev.code === ${code} 的缩放分支`);
  }
});

test('桌面本地偏好不进内核 payload（那边全量替换、未知字段会被拒）', () => {
  const model = read('settings-model.js');
  for (const bad of ['UiPrefs', 'uiZoomLevel', 'fontSans', 'showStatusbar']) {
    assert.ok(!model.includes(bad),
      `settings-model.js 里出现了本地偏好 ${bad} —— 会被当成内核设置提交，整单被拒`);
  }
});


test('桌面界面不许再用原生 confirm/alert（WebView2 会弹系统框、标题是页面地址）', () => {
  for (const s of classicScripts()) {
    const src = sourceOf(s);
    assert.ok(!/(?<!Dialog)\bconfirm\(/.test(src), `${s} 里还有原生 confirm()，请改用 confirmDialog()`);
    assert.ok(!/[^.\w]alert\(/.test(src), `${s} 里还有原生 alert()`);
  }
  // 确认框基于组件库的 wa-dialog，别再手搓遮罩
  assert.ok(read('app.js').includes("document.createElement('wa-dialog')"), '确认框不再基于 wa-dialog');
  assert.ok(read('index.html').includes('components/dialog/dialog.js'), 'index.html 没有加载 dialog 组件');
});

test('根节点缩放不会把整页撑高（vh 不跟着 zoom 缩小，实测 144% 时高出 44%）', () => {
  // 用户实测：144% 下点「新会话」后整页被顶上去 —— 根因是 #app 用了 100vh：
  // 它按放大后的视口铺满，页面比视口高一截，输入框一聚焦浏览器就把根容器滚下去。
  // 修法：根高度按 --ui-h（= innerHeight / zoom 的布局像素）补偿。
  const css = read('style.css');
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
  assert.ok(/html,\s*body\s*\{[^}]*height:\s*var\(--ui-h/.test(body),
    'html, body 的高度必须走 var(--ui-h, …) 补偿，否则缩放后会比视口高');
  assert.ok(!/#app\s*\{[^}]*height:\s*[\d.]+vh/.test(body),
    '#app 又用回 vh 了 —— 缩放时会比视口高，焦点滚动会把整页顶上去');

  const prefs = read('ui-prefs.js');
  assert.ok(prefs.includes('--ui-h'), 'ui-prefs.js 不再写 --ui-h，补偿链断了');
  assert.ok(/applyViewportHeight/.test(prefs), 'zoom 变化时不再重算根高度');
});

test('侧栏里的纵向元素不许自己长高（按钮和列表抢剩余空间 → 按钮撑成一整块）', () => {
  const css = read('style.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = /\.new-session\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'style.css 里找不到 .new-session 规则');
  assert.ok(!/flex:\s*1\b|flex-grow:\s*[1-9]/.test(rule[1]),
    '.new-session 又在和 .session-list 抢剩余空间了 —— 会话少时按钮会撑成一大块');
});

test('缩放优先交给宿主原生（WebView2），拿不到才回落 CSS', () => {
  // 引擎级缩放没有 vh 的坑、文字按真实字号渲染；CSS zoom 是兜底。
  // 关键是**回落路径必须存在**：宿主说不行时界面不能两头空（缩放了但没人执行）。
  const app = read('app.js');
  assert.ok(app.includes("api('/desktop/zoom')"), '不再问宿主能不能原生缩放');
  assert.ok(app.includes('setNativeZoomHook(requestNativeZoom)'), '没把原生缩放挂到 ui-prefs 上');
  assert.ok(app.includes('setNativeZoomHook(null)'), '缺回落路径：宿主失灵时界面会两头空');
  assert.ok(app.includes('probeNativeZoom()'), '开机没有探测原生缩放能力');
  assert.ok(app.includes('fallbackToCssZoom'), '没有回落函数');
  assert.ok(read('index.html').includes('id="uiZoomMode"'), '界面面板没有显示当前缩放方式');
});

test('流式光标只许有一个，回合结束要收掉（否则每轮留一个还在闪）', () => {
  // 用户实测：「会话里面会出现多个闪烁的蓝色光标，会话结束还在闪」——
  // 旧实现只往块里插入 `<span class="cursor-blink">`，**从来没人删它**。
  const app = read('app.js');
  assert.ok(app.includes('function clearStreamCursor'), '没有清光标的函数');
  assert.match(app, /function endTurn\(\)[\s\S]*?clearStreamCursor\(\)[\s\S]*?state\.turn = null/,
    '回合结束时没收掉光标 → 它会在页面上一直闪');
  assert.match(app, /clearStreamCursor\(\);\s*\/\/ 先清旧的[\s\S]{0,200}cursor-blink/,
    '每次重渲染前没清旧的 → 每段文本都会留一个');
});

test('会话必须归入工作区，agent 才会在你看的目录里干活', () => {
  // 内核只让「已归入工作区」的会话把 shell 起在工作区目录里；没归入的只有默认
  // 沙箱 `<data>/workspace/db-<会话id>`。桌面界面以前从不调这个接口 → 用户在文件
  // 面板里明明看着自己的项目，agent 却说「工作区是空的、外面被沙箱拦了」。
  const app = read('app.js');
  assert.ok(app.includes('/workspace`'), '没有把会话归入工作区的调用');
  assert.ok(app.includes("method: 'PATCH'"), '归入工作区应该用 PATCH');
  assert.ok(app.includes('folderId'), 'PATCH 体里必须带 folderId');
  assert.ok(app.includes('bindSessionWorkspace(currentWorkspace)'), '选工作区时没把会话搬过去');
  assert.match(app, /state\.sessionId = id;\s*\n\s*await bindSessionWorkspace/,
    '新建的会话没归入面板当前的工作区');
  assert.ok(app.includes('syncWorkspaceToSession'), '切会话时没把面板切到那个会话的工作区');
  assert.ok(read('index.html').includes('id="wsAgentHint"'),
    '界面没告诉用户 agent 到底在哪个目录干活');
});

test('沙箱/插件/定时/知识库/市场/用量：六个面板都接上了内核 API', () => {
  // 内核有 14 个路由模块，桌面界面以前只调了 5 个。这几个面板补齐了
  // 用户会用到的：沙箱（放行）、助理（子代理）、附件上传等。
  const app = read('app.js');
  const html = read('index.html');
  const wired = {
    guard: ['/guard/events', '/guard/allowlist', 'loadGuard'],
    plugins: ['/plugins', 'loadPlugins'],
    scheduled: ['/scheduled/tasks', 'loadScheduled'],
    knowledge: ['/knowledge', 'loadKnowledge'],
    marketplace: ['/marketplace', 'loadMarketplace'],
    usage: ['/usage', 'loadUsage'],
  };
  for (const [pane, needles] of Object.entries(wired)) {
    assert.ok(html.includes(`data-pane="${pane}"`), `缺 ${pane} 面板/导航`);
    for (const n of needles) assert.ok(app.includes(n), `${pane} 面板没接上 ${n}`);
  }
});

test('附件上传：走 /api/upload，消息里只留路径（不塞 base64）', () => {
  // 一张手机照片 base64 后 ~7MB，进受控 textarea 会卡死主线程、进上下文烧 token。
  // 内核约定 path-only：上传到工作区，消息里放 markdown 路径引用。
  const app = read('app.js');
  assert.ok(app.includes("fetch(API + '/upload'"), '没有走 /api/upload 上传');
  assert.ok(app.includes('attachmentMarkdown'), '没有把附件拼成 path-only 引用');
  assert.ok(/!\[\$\{a\.name\}\]\(\$\{a\.path\}\)/.test(app), '图片附件应是 ![name](path) 形式（只留路径）');
  assert.ok(!/data:image\/[a-z]+;base64/.test(app), 'app.js 不该内联 base64 图片数据');
  assert.ok(read('index.html').includes('id="fileInput"'), '缺附件选择输入');
});

test('新面板的控件用组件库、图标用图标库，不手写', () => {
  // 约束：组件库/图标库有的就不手写。抽查几个高频面板。
  const html = read('index.html');
  const paneOf = (name) => {
    // 定位「面板 <section>」而不是同名的导航 <button>
    const i = html.indexOf(`<section class="settings-pane" data-pane="${name}"`);
    return html.slice(i, html.indexOf('</section>', i));
  };
  for (const name of ['scheduled', 'marketplace', 'usage', 'guard']) {
    const seg = paneOf(name);
    assert.ok(/<wa-button/.test(seg), `${name} 面板应使用 <wa-button> 而非手写 <button class=btn>`);
    assert.ok(/library="om"/.test(seg), `${name} 面板的图标应来自图标库（library="om"）`);
  }
});

/* ── 聊天里的图片 ──────────────────────────────────────────────────────
 * 内核会把 agent 生成的图/浏览器截图的路径发进 `toolEnd.images`，前端以前只加
 * 了一行「生成图片」标题、**不建 <img>**，markdown 也没有图片规则 —— 用户看到的
 * 是 `![生成图](C:\...)` 这种字面文本。这里真的执行 renderMarkdown 来钉规则。
 */
function renderMarkdownFn() {
  const src = read('app.js');
  const start = src.indexOf('function renderMarkdown(src) {');
  const end = src.indexOf('/* ── syntax highlighting');
  assert.ok(start >= 0 && end > start, '抠不出 renderMarkdown（函数被改名或挪走了？）');
  // 只跑这一个函数：等价物（转义 / 高亮 / 图片地址）用桩传进去。
  return new Function(
    'esc', 'highlight', 'rawImageUrl',
    `${src.slice(start, end)}; return renderMarkdown;`,
  )(
    (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
    () => '',
    (p) => `RAW(${p})`,
  );
}

test('renderMarkdown：![]() 渲染成 <img>，且排在链接规则之前', () => {
  const md = renderMarkdownFn();
  const img = md('![生成图](shots/a.png)');
  assert.ok(img.includes('<img class="md-image"'), `图片没渲染成 <img>：${img}`);
  assert.ok(img.includes('RAW(shots/a.png)'), `图片地址没走 rawImageUrl：${img}`);
  assert.ok(!img.includes('<a href'), '`![alt](路径)` 必须优先当图片，不能掉进链接规则');
  // 普通链接不受影响
  const link = md('[文档](https://example.com/x)');
  assert.ok(link.includes('<a href="https://example.com/x"'), `普通链接被图片规则吃掉了：${link}`);
});

test('toolEnd 带 images 时真的建 <img>（不是只加个标题）', () => {
  const app = read('app.js');
  // 只钉行为：toolEnd 必须把 f.images 交给 appendImages，而 appendImages 必须真的建 <img>。
  // （别钉"代码写在 toolEnd 里"——那只是实现位置，换个抽法就假红。）
  const i = app.indexOf("case 'toolEnd'");
  assert.ok(i > 0, "找不到 case 'toolEnd'");
  const block = app.slice(i, app.indexOf("case 'subagentStart'", i));
  assert.ok(/appendImages\([^)]*f\.images/.test(block), 'toolEnd 没有把 f.images 交给 appendImages');
  const fn = app.slice(app.indexOf('function appendImages('));
  assert.ok(fn.slice(0, 900).includes("createElement('img')"), 'appendImages 没有真的建 <img>');
  assert.ok(fn.slice(0, 900).includes('rawImageUrl('), 'appendImages 没有把路径转成可读地址');
  // 子代理的 ToolEnd 也走同一条路
  assert.ok(/appendImages\(b\.body, f\.images\)/.test(app), '子代理的图片没有渲染');
});

test('子代理的 5 种帧 + fallback 都有分支（以前全落 default，界面完全静默）', () => {
  const app = read('app.js');
  for (const t of ['subagentStart', 'subagentDelta', 'subagentToolStart',
                   'subagentToolEnd', 'subagentEnd', 'fallback']) {
    assert.ok(new RegExp(`case '${t}':`).test(app), `没有处理 ${t} 帧 —— 它会静默落进 default`);
  }
  const fn = app.slice(app.indexOf('function subagentFrame('));
  for (const t of ['subagentDelta', 'subagentToolStart', 'subagentToolEnd', 'subagentEnd']) {
    assert.ok(fn.slice(0, 2200).includes(t), `subagentFrame 没有处理 ${t}`);
  }
});

test('记忆页：读 mtime（后端字段）、能改能删、有整理入口', () => {
  const app = read('app.js');
  const i = app.indexOf('function memoryRow(');
  assert.ok(i > 0, '找不到 memoryRow');
  const fn = app.slice(i, i + 4000);
  // 后端 system_api.py 给的是 {"name","kind","kindLabel","size","mtime","preview"}。
  // 以前前端读 f.modified → 时间列永远是空的。
  assert.ok(fn.includes('fmtTime(f.mtime)'), '记忆页没有用后端给的 mtime（时间列会一直空）');
  assert.ok(!/f\.modified/.test(fn), '还有残留的 f.modified');
  assert.ok(/encodeURI\(name\),\s*\{\s*\n?\s*method: 'PUT'/.test(fn), '没有保存正文（PUT）');
  assert.ok(fn.includes("method: 'DELETE'"), '没有删除');
  assert.ok(app.includes('/system/memory/organize'), '没有「整理记忆」入口');
  assert.ok(app.includes('memoryRow('), '列表没有用 memoryRow 渲染');
});

/* ── 界面缩放「落在谁的窗口上」（2026-10-08 用户实测到的 bug）───────────────
 * PC 上双开时，第二个实例复用后端、自己开窗口。`POST /api/desktop/zoom` 由
 * **后端那个进程**处理，而它只能操作自己窗口的 WebView2 —— 它回 `ok:true`，
 * 缩的却是另一个窗口。老代码收到 ok:true 就把 CSS zoom 撤掉，于是：
 * **百分数照变、窗口纹丝不动**（用户原话「缩放失效了，数值变了但窗口没动」）。
 * 这里真跑这几个函数，钉住「量过本窗口视口才算接管」。
 */
function zoomFns() {
  const src = read('app.js');
  const start = src.indexOf('function zoomBridge(');
  const end = src.indexOf('function fallbackToCssZoom(');
  assert.ok(start >= 0 && end > start, '抠不出缩放这段（函数被改名或挪走了？）');
  const body = src.slice(start, end);
  return function make(env) {
    return new Function(
      'api', 'setZoomMode', 'fallbackToCssZoom', 'UiPrefs', 'window', 'setTimeout',
      `${body}; return { probeNativeZoom, requestNativeZoom, pushNativeZoom, nativeZoomTookThisWindow };`,
    )(env.api, env.setZoomMode, env.fallbackToCssZoom, env.UiPrefs, env.window, setTimeout);
  };
}

function zoomEnv(opts) {
  const o = opts || {};
  const env = {
    calls: [], posts: [], bridgeCalls: [], trust: [], fallbacks: [], modes: [],
    window: { innerWidth: 1200 },
  };
  const setWidth = (z) => { if (o.moves) env.window.innerWidth = Math.round(1200 / z); };
  env.api = async (path, req) => {
    const factor = req && req.body ? JSON.parse(req.body).factor : null;
    env.calls.push({ path, factor });
    if (factor != null) { env.posts.push({ path, factor }); setWidth(factor); }
    return { ok: true, handle: true, ready: true, applied: factor == null ? 1.728 : factor };
  };
  if (o.bridge) {
    env.window.__OPENMINIS_DESKTOP__ = true;
    env.window.addEventListener = () => {};
    env.window.pywebview = {
      api: {
        set_zoom: async (z) => { env.bridgeCalls.push(z); setWidth(z); return { ok: true, applied: z }; },
        zoom_capability: async () => {
          env.bridgeCaps = (env.bridgeCaps || 0) + 1;
          return { handle: true, ready: true, applied: 1.728 };
        },
      },
    };
  }
  env.setZoomMode = (m, why) => env.modes.push([m, why]);
  env.fallbackToCssZoom = (why) => env.fallbacks.push(why);
  env.UiPrefs = {
    trusted: !!o.trusted,
    isNativeTrusted() { return this.trusted; },
    trustNativeZoom(ok) { env.trust.push(ok); this.trusted = !!ok; return !!ok; },
    setNativeZoomHook(fn) { env.hook = fn; this.trusted = false; },
    applyAll() { env.applyAlls = (env.applyAlls || 0) + 1; return {}; },
  };
  return env;
}

test('缩放：宿主回 ok 也要自己量 —— 缩的是「别人的窗口」时必须回落 CSS', async () => {
  const env = zoomEnv({ moves: false });
  const fns = zoomFns()(env);
  assert.equal(fns.requestNativeZoom(1.2), false, '还没证实就不许声称接管（CSS 会被撤掉、画面不动）');
  await new Promise((r) => setTimeout(r, 300));
  assert.deepEqual(env.posts, [{ path: '/desktop/zoom', factor: 1.2 }], '请求该发出去');
  assert.deepEqual(env.trust, [], '本窗口视口没变，就不许标成已接管');
  assert.equal(env.fallbacks.length, 1, '必须回落 CSS，否则用户看到的就是「缩放失效」');
  assert.match(env.fallbacks[0], /applied/, '回落理由要带上宿主报的值，便于定位');
});

test('缩放：本窗口确实被原生接管 → 标信任，之后走快路径不再等', async () => {
  const env = zoomEnv({ moves: true });
  const fns = zoomFns()(env);
  assert.equal(fns.requestNativeZoom(1.2), false, '第一次仍要等量完');
  await new Promise((r) => setTimeout(r, 300));
  assert.deepEqual(env.trust, [true]);
  assert.equal(env.fallbacks.length, 0, '本窗口生效了就不该回落');
  assert.equal(fns.requestNativeZoom(1.44), true, '已信任 → 同步放行，不再垫 CSS（不闪）');
});

test('缩放：回到 100% 时视口本来就不变，不能误判成「没落到本窗口」', async () => {
  const env = zoomEnv({ moves: false });
  const fns = zoomFns()(env);
  fns.requestNativeZoom(1);
  await new Promise((r) => setTimeout(r, 300));
  assert.deepEqual(env.trust, [true], '100% 没有可观测差异，判成功即可');
  assert.equal(env.fallbacks.length, 0);
});

test('缩放：桌面窗口优先走**本窗口**的宿主桥，一个 HTTP 都不发', async () => {
  // 双开的根因就是 HTTP 那条路由后端进程处理、只能作用它自己的窗口。
  // 桥（window.pywebview.api）在**本进程**里，缩的必然是本窗口。
  const env = zoomEnv({ moves: true, bridge: true });
  const fns = zoomFns()(env);
  await fns.probeNativeZoom(0);
  assert.equal(env.bridgeCaps, 1, '有桥就该问桥要能力（HTTP 是后端进程的视角）');
  assert.deepEqual(env.calls, [], '有桥时不该发任何 HTTP 请求');
  await fns.pushNativeZoom(1.2, true);
  assert.deepEqual(env.bridgeCalls, [1.2], '缩放该走本窗口的桥');
  assert.deepEqual(env.calls, [], '有桥时仍然不该有 HTTP —— 那条路会落到别的窗口上');
  assert.deepEqual(env.trust, [true], '本窗口视口真的变了 → 标信任');
  assert.equal(env.fallbacks.length, 0);
});

test('缩放：没有桥（浏览器 / --upstream-ui）时才退回 HTTP', async () => {
  const env = zoomEnv({ moves: true, bridge: false });
  env.window.__OPENMINIS_DESKTOP__ = false;
  const fns = zoomFns()(env);
  await fns.probeNativeZoom(0);
  assert.equal(env.calls.length, 1, '没有桥才用 HTTP 探能力');
  await fns.pushNativeZoom(1.2, true);
  assert.deepEqual(env.posts, [{ path: '/desktop/zoom', factor: 1.2 }], '没有桥才走 HTTP 缩放');
  assert.deepEqual(env.trust, [true]);
});

test('缩放：桥存在但没落到本窗口时，照样回落 CSS（不因为"有桥"就盲目信任）', async () => {
  const env = zoomEnv({ moves: false, bridge: true });
  const fns = zoomFns()(env);
  await fns.pushNativeZoom(1.2, true);
  assert.deepEqual(env.bridgeCalls, [1.2]);
  assert.deepEqual(env.trust, [], '桥回了 ok 但本窗口视口没变 → 不许标信任');
  assert.equal(env.fallbacks.length, 1, '必须回落 CSS，否则用户看到的就是"缩放失效"');
});
