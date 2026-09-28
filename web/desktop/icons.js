/* 本地图标库：把 vendored 的 Lucide 图标注册给 <wa-icon>。
 *
 * 用法：<wa-icon library="om" name="settings"></wa-icon>
 *
 * 为什么这个文件必须是 module：registerIconLibrary 是组件库的 ESM 导出，
 * classic script 没法 import 它。（ui-check 里那条「我们自己的代码只能是
 * classic script」的规则对这个文件开了白名单，并且把白名单长度钉死在 1 ——
 * 它是例外，不是新惯例。）
 *
 * 图标本身在 web/desktop/vendor/lucide/（36 个，16KB）—— 那是 vendor-lucide.mjs
 * 的产物，别手改；要加图标就改脚本里的 ICONS 再重跑。
 */
import { registerIconLibrary } from './vendor/webawesome/components/icon/library.js';

registerIconLibrary('om', {
  // 只可能是本地文件：桌面应用打包后是离线的，不碰 CDN。
  resolver: (name) => `./vendor/lucide/${name}.svg`,
  // Lucide 的 SVG 已经是 24×24 + stroke="currentColor" + stroke-width 2，
  // 颜色跟 CSS 走、尺寸跟 font-size 走，不需要再改。
  mutator: (svg) => svg,
});
