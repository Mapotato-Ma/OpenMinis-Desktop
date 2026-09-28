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
