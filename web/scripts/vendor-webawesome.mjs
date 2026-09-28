/* 把 Web Awesome 的**最小子集** vendor 进桌面界面。
 *
 *   node web/scripts/vendor-webawesome.mjs /tmp/wa/package
 *
 * 为什么不用整包：官方 dist 有 2366 个文件 / 17MB，而这个界面只用到几个组件。
 * 桌面界面的约束是「无构建步骤」（见 docs/adr/0001），所以不能用打包器做 tree-shaking
 * —— 那就自己走一遍 ES module 的 import 图，只把闭包里的文件抄进来。
 *
 * 产出的目录必须能**离线**工作：exe 里不带 CDN，界面加载的是本地文件。
 */
import { mkdirSync, readFileSync, writeFileSync, copyFileSync, existsSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';

const SRC = process.argv[2];
if (!SRC || !existsSync(SRC)) {
  console.error('用法: node web/scripts/vendor-webawesome.mjs <解压后的包根目录>');
  process.exit(2);
}

const OUT = path.resolve(new URL('../desktop/vendor/webawesome/', import.meta.url).pathname);
const VERSION = JSON.parse(readFileSync(path.join(SRC, 'package.json'), 'utf8')).version;

/** 我们真正用到的组件 —— 加组件就在这里加一行。 */
const ENTRIES = [
  'components/select/select.js',
  'components/option/option.js',
  'components/tooltip/tooltip.js',
];

const CDN = path.join(SRC, 'dist-cdn');

/** 抽出一个 ES module 里的相对 import 目标。 */
function importsOf(code) {
  const out = [];
  const re = /(?:^|\n)\s*(?:import|export)[\s\S]*?from\s*['"]([^'"]+)['"]|(?:^|\n)\s*import\s*['"]([^'"]+)['"]/g;
  let m;
  while ((m = re.exec(code))) out.push(m[1] || m[2]);
  return out.filter((s) => s.startsWith('.'));
}

const seen = new Set();
const queue = [...ENTRIES];
while (queue.length) {
  const rel = queue.shift();                 // 一律是相对 dist-cdn 的路径
  if (seen.has(rel)) continue;
  seen.add(rel);
  const abs = path.resolve(CDN, rel);
  if (!existsSync(abs)) {
    console.error(`✗ 找不到 ${rel}（组件名写错了？）`);
    process.exit(1);
  }
  for (const dep of importsOf(readFileSync(abs, 'utf8'))) {
    queue.push(path.relative(CDN, path.resolve(path.dirname(abs), dep)));
  }
}

// 样式：组件样式内嵌在 chunks 里，但主题与 layers 是独立 CSS，要一起带上。
// 样式整棵带上：webawesome.css 里是一串 @import，少一个就是一个 404。
// 它们加起来才几十 KB，比维护一份「哪些 CSS 是必需的」清单便宜。
const styleFiles = [];
(function walkStyles(dir) {
  for (const e of readdirSync(path.join(CDN, dir), { withFileTypes: true })) {
    const rel = `${dir}/${e.name}`;
    if (e.isDirectory()) walkStyles(rel);
    else if (e.name.endsWith('.css')) styleFiles.push(rel);
  }
})('styles');

rmSync(OUT, { recursive: true, force: true });
let bytes = 0;
const copy = (rel) => {
  const from = path.join(CDN, rel);
  if (!existsSync(from)) return false;
  const to = path.join(OUT, rel);
  mkdirSync(path.dirname(to), { recursive: true });
  copyFileSync(from, to);
  bytes += readFileSync(from).length;
  return true;
};

for (const rel of seen) copy(rel);
for (const rel of styleFiles) copy(rel);

copyFileSync(path.join(SRC, 'LICENSE.md'), path.join(OUT, 'LICENSE.md'));
writeFileSync(path.join(OUT, 'README.md'), `# Web Awesome (vendored)

- 版本：${VERSION} · 许可：MIT（见同目录 \`LICENSE.md\`）
- 来源：npm \`@awesome.me/webawesome@${VERSION}\` 的 dist-cdn 子集
- 生成：\`node web/scripts/vendor-webawesome.mjs <包根目录>\` —— **不要手改这个目录**，
  要加组件就改脚本里的 ENTRIES 再重跑（它按 ES module 的 import 图取闭包）。
- 只带了我们用到的组件与它依赖的 chunks；整包有 2366 个文件 / 17MB，这里是
  ${seen.size + styleFiles.length} 个文件 / ${Math.round(bytes / 1024)}KB。
`);

console.log(`✓ vendor 到 ${path.relative(process.cwd(), OUT)}`);
console.log(`  ${seen.size} 个模块 + ${styleFiles.length} 个样式，合计 ${Math.round(bytes / 1024)}KB`);
for (const rel of [...seen].sort()) console.log('   ', rel);
