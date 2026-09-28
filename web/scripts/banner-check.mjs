import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

// 同 settings-model.check.mjs：用 new Function 按「浏览器加载 classic script」装载
// （node:vm 造出来的对象属于另一个 realm，deepStrictEqual 会说"结构一样但不等"）。
const SRC = readFileSync(new URL('../desktop/app.js', import.meta.url), 'utf8');

test('横幅渲染只调用服务端结论，不再自己判 model / key', () => {
  // 静态检查：renderModelsHealth 里不许再出现自己拼的「还缺」判断，
  // 它必须读 /api/desktop/chat-readiness。
  assert.ok(SRC.includes('chat-readiness'), 'renderModelsHealth 应当调用服务端的 readiness');
  const body = SRC.slice(SRC.indexOf('async function renderModelsHealth'),
                         SRC.indexOf('function drawHealth'));
  assert.ok(!body.includes('pendingKeys'), '不该再自己判断密钥有没有填');
  assert.ok(!body.includes("missing.push"), '不该再自己拼「还缺什么」');
  assert.ok(body.includes('SettingsModel.readinessBanner'), '应当把结论交给模块翻译');
});

test('横幅画的是 spec 给的四种字段', () => {
  const body = SRC.slice(SRC.indexOf('function drawHealth'), SRC.indexOf('/* ── provider probe state'));
  for (const field of ['spec.title', 'spec.sub', 'spec.hint', 'spec.note', 'spec.showPickFirst']) {
    assert.ok(body.includes(field), `drawHealth 没有画 ${field}`);
  }
});
