/* 把 Lucide 的**用到的那些**图标 vendor 进桌面界面。
 *
 *   node web/scripts/vendor-lucide.mjs /tmp/lucide/package
 *
 * 和 Web Awesome 那次同样的理由：整包 icons/ 有 2118 个 SVG（约 1MB），
 * 我们只用到二十来个。桌面界面没有构建步骤（ADR 0001），所以不做全局安装，
 * 只把点名的文件抄进 web/desktop/vendor/lucide/。
 *
 * Lucide 的 SVG 用 stroke="currentColor"、24×24、stroke-width 2 —— 也就是说
 * 颜色跟着 CSS 走，深浅主题都不用管；尺寸由 font-size 决定（wa-icon 用 1em）。
 *
 * 许可：ISC（见 vendor 目录里的 LICENSE），无需署名，但保留许可文件是基本礼貌。
 */
import { mkdirSync, readFileSync, writeFileSync, copyFileSync, existsSync, rmSync } from 'node:fs';
import path from 'node:path';

const SRC = process.argv[2];
if (!SRC || !existsSync(SRC)) {
  console.error('用法: node web/scripts/vendor-lucide.mjs <解压后的 lucide-static 包根目录>');
  process.exit(2);
}

const OUT = path.resolve(new URL('../desktop/vendor/lucide/', import.meta.url).pathname);
const VERSION = JSON.parse(readFileSync(path.join(SRC, 'package.json'), 'utf8')).version;

/** 语义 → Lucide 图标名。左边是我们在界面里的用途，右边是 Lucide 的文件名。 */
const ICONS = {
  // 品牌与身份
  diamond: '品牌标记 ◈ / 人格',
  sparkles: '助手头像',
  bot: 'Agent 面板 / 身份头像占位',
  user: '用户头像 / 人格',
  puzzle: '身份与工具',
  // 导航与操作
  settings: '设置 / 模型服务 / json 文件',
  zap: '技能 / MCP 工具',
  brain: '记忆',
  info: '关于',
  'refresh-cw': '刷新',
  'rotate-cw': '重载会话',
  x: '关闭 / 删除 / 失败',
  check: '完成 / 选中',
  'circle-check': '就绪',
  'triangle-alert': '警告 / 未就绪',
  'circle-x': '失败',
  'chevron-right': '折叠箭头',
  'arrow-up': '发送',
  square: '停止生成',
  'sun-moon': '主题：跟随系统',
  sun: '主题：浅色',
  moon: '主题：深色',
  // 工具与文件
  terminal: 'shell / 终端',
  'file-text': '文件 / 默认文件类型',
  'file-pen': '写文件',
  'pen-line': '编辑文件',
  'folder-tree': '列目录',
  search: '搜索文件 / 网页搜索',
  globe: '网页抓取',
  image: '读图',
  palette: '生成图片 / html·css 文件',
  send: '发消息',
  users: '子 agent',
  wrench: '未知工具',
  trash: '删除',
  copy: '复制代码',
  folder: '目录',
  files: '活动栏：文件',
  'messages-square': '活动栏：会话',
  'git-compare': '活动栏：变更（diff）',
  'panel-left': '活动栏：收起侧栏',
  file: '默认文件',
  'file-code': '代码文件',
  braces: 'json / yaml 等配置',
  box: '子事项/资源兜底',
  // —— 新面板：助理 / 沙箱 / 插件 / 定时任务 / 知识库 / 市场 / 附件 / 用量 ——
  'plus': '新建（助理、工作区、定时任务…）',
  'pencil': '编辑',
  'save': '保存',
  'play': '运行 / 立即执行',
  'pause': '暂停',
  'cpu': '模型 / 引擎',
  'shield': '沙箱',
  'shield-check': '已放行',
  'shield-alert': '被沙箱拦下',
  'lock': '锁定 / 需要密码',
  'unlock': '放行',
  'key-round': '放行白名单 / 访问密码',
  'history': '拦截历史',
  'ban': '拒绝',
  'plug': '插件',
  'plug-zap': '已启用的插件',
  'power': '启用 / 停用',
  'package': '安装包 / 技能包',
  'clock': '定时任务',
  'calendar-clock': '计划时间',
  'timer': '间隔',
  'repeat': '重复',
  'book-open': '知识库',
  'library': '知识条目',
  'store': '技能市场',
  'download': '安装 / 下载',
  'star': '推荐 / 收藏',
  'paperclip': '添加附件',
  'upload': '上传',
  'chart-column': '用量统计',
  'coins': 'token 消耗',
  'hash': '会话 / 编号',
  'wallpaper': '背景图',
  'monitor': '界面显示',
  'layout-grid': '布局',
  'chevron-down': '展开',
  'ellipsis': '更多操作',
  'external-link': '在浏览器打开',
  'filter': '筛选',
  'loader-circle': '进行中',
  'list-checks': '清单 / 明细',
  'user-round': '助理（成员）',
  'rocket': '快速开始',
  'trash-2': '删除',
};

rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

const missing = [];
let bytes = 0;
for (const name of Object.keys(ICONS)) {
  const from = path.join(SRC, 'icons', `${name}.svg`);
  if (!existsSync(from)) {
    missing.push(name);
    continue;
  }
  copyFileSync(from, path.join(OUT, `${name}.svg`));
  bytes += readFileSync(from).length;
}
copyFileSync(path.join(SRC, 'LICENSE'), path.join(OUT, 'LICENSE'));
writeFileSync(path.join(OUT, 'README.md'), `# Lucide (vendored)

- 版本：${VERSION} · 许可：ISC（见同目录 \`LICENSE\`）
- 来源：npm \`lucide-static@${VERSION}\` 的 \`icons/\` 子集
- 生成：\`node web/scripts/vendor-lucide.mjs <包根目录>\` —— **不要手改**，
  要加图标就改脚本里的 ICONS 再重跑。
- 这里只有我们真正用到的 ${Object.keys(ICONS).length} 个（整包 2118 个 / 约 1MB）；
  ${Object.keys(ICONS).length} 个合计 ${Math.round(bytes / 1024)}KB。
- 这些 SVG 都是 24×24、stroke="currentColor"，颜色跟 CSS 走、尺寸跟 font-size 走。
`);

if (missing.length) {
  console.error(`✗ 这些名字在 Lucide ${VERSION} 里不存在：${missing.join(', ')}`);
  process.exit(1);
}
console.log(`✓ vendor 了 ${Object.keys(ICONS).length} 个图标，合计 ${Math.round(bytes / 1024)}KB → ${path.relative(process.cwd(), OUT)}`);
