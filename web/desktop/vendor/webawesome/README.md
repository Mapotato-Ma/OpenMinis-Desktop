# Web Awesome (vendored)

- 版本：3.14.0 · 许可：MIT（见同目录 `LICENSE.md`）
- 来源：npm `@awesome.me/webawesome@3.14.0` 的 dist-cdn 子集
- 生成：`node web/scripts/vendor-webawesome.mjs <包根目录>` —— **不要手改这个目录**，
  要加组件就改脚本里的 ENTRIES 再重跑（它按 ES module 的 import 图取闭包）。
- 只带了我们用到的组件与它依赖的 chunks；整包有 2366 个文件 / 17MB，这里是
  93 个文件 / 511KB。
