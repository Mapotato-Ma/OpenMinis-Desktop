# Lucide (vendored)

- 版本：1.48.0 · 许可：ISC（见同目录 `LICENSE`）
- 来源：npm `lucide-static@1.48.0` 的 `icons/` 子集
- 生成：`node web/scripts/vendor-lucide.mjs <包根目录>` —— **不要手改**，
  要加图标就改脚本里的 ICONS 再重跑。
- 这里只有我们真正用到的 41 个（整包 2118 个 / 约 1MB）；
  41 个合计 19KB。
- 这些 SVG 都是 24×24、stroke="currentColor"，颜色跟 CSS 走、尺寸跟 font-size 走。
