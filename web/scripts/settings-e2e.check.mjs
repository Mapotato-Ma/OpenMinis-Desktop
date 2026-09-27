/* 对**真实内核**验四条「全量替换 PUT」规则：模块组装载荷 → 真 PUT → 查真实状态。
 *
 *   python desktop_main.py --no-window --port 8790     （另开一个终端起服务）
 *   node web/scripts/settings-e2e.check.mjs            （或 npm run check:e2e）
 *
 * 不进 CI：它需要一个活着的内核，而且会真的改服务端设置（跑完会自己清干净）。
 * 它替代的是 docs/DESKTOP.md 里那段「在浏览器里人肉点一遍」的验证记录 ——
 * 这些规则每条错了都是「配置看着配好了、其实没生效」这种静默故障，
 * 值得有个能红的断言盯着。
 */
import { readFileSync } from 'node:fs';

const SRC = readFileSync(new URL('../desktop/settings-model.js', import.meta.url), 'utf8');
const mod = { exports: {} };
new Function('module', 'window', SRC)(mod, undefined);
const M = mod.exports;

const BASE = process.env.OPENMINIS_BASE || 'http://localhost:8790';
const TEST_ID = 'e2e-provider';
let fails = 0;

const j = async (r) => { try { return await r.json(); } catch { return null; } };
const getSettings = () => fetch(`${BASE}/api/settings`).then(j);
const put = async (body) => {
  const r = await fetch(`${BASE}/api/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: await j(r) };
};
function check(name, cond, extra = '') {
  if (!cond) fails += 1;
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`);
}
const chatSlot = (s) => (s.modelSlots || []).find((x) => x.slot === 'chat') || {};

const live = await getSettings();
if (!live || !('providers' in live)) {
  console.error(`连不上 ${BASE} —— 先起服务：python desktop_main.py --no-window --port 8790`);
  process.exit(2);
}
const before = live.providers.length;
console.log(`起点：${before} 个服务商，activeProviderId=${live.activeProviderId}\n`);

try {
  // 规则 5：加服务商 → 对话槽自动接上（v0.1.1 修的那个 bug，不能丢）
  let s = M.load(live, null);
  s = M.reduce(s, {
    type: 'provider/add',
    id: TEST_ID,
    provider: { type: 'anthropic', baseUrl: 'https://api.anthropic.com', label: 'E2E' },
    meta: { label: 'E2E', defaultModel: 'claude-e2e' },
  });
  s = M.reduce(s, { type: 'provider/setKey', id: TEST_ID, key: 'sk-e2e-not-a-real-key' });
  let r = await put(M.toPayload(s).body);
  check('规则5 添加服务商被接受', r.status === 200, `status=${r.status}`);
  let live2 = await getSettings();
  check('规则5 activeProviderId 指向新实例', live2.activeProviderId === TEST_ID, `=${live2.activeProviderId}`);
  check('规则1 完整列表已落库', live2.providers.length === before + 1, `providers=${live2.providers.length}`);

  // 规则 2：不带 apiKey 再存一次 —— 留空 = 保持已存密文
  s = M.load(live2, null);
  r = await put(M.toPayload(s).body);
  live2 = await getSettings();
  const mine = live2.providers.find((p) => p.id === TEST_ID) || {};
  check('规则2 不带密钥的保存被接受', r.status === 200, `status=${r.status}`);
  check('规则2 已存密钥没被抹掉', mine.hasKey === true, `hasKey=${mine.hasKey}`);

  // 规则 4：删服务商 → 模块同时清掉指向它的槽位 → 服务端必须接受
  s = M.load(live2, null);
  s = M.reduce(s, { type: 'provider/remove', id: TEST_ID });
  const payload = M.toPayload(s).body;
  check('规则4 载荷里 chat 槽已置 null', payload.modelSlots.chat === null, String(payload.modelSlots.chat));
  r = await put(payload);
  check('规则4 删除被接受（没有触发整单拒绝）', r.status === 200,
    `status=${r.status}${r.body && r.body.detail ? ' ' + JSON.stringify(r.body.detail).slice(0, 140) : ''}`);
  live2 = await getSettings();
  check('规则4 服务商已删净', live2.providers.length === before, `providers=${live2.providers.length}`);
  check('规则4 没有留下悬空的对话槽', chatSlot(live2).instanceId !== TEST_ID, `chat=${chatSlot(live2).instanceId}`);
} finally {
  // 收尾：把这次验证加的东西清干净，别把环境留脏
  const now = await getSettings();
  const ids = (now.providers || []).filter((p) => p.id === TEST_ID).map((p) => p.id);
  for (const id of ids) {
    const st = M.reduce(M.load(now, null), { type: 'provider/remove', id });
    await put(M.toPayload(st).body);
  }
  const end = await getSettings();
  console.log(`\n收尾：providers=${end.providers.length}（起点 ${before}）`);
}

console.log(fails ? `${fails} 项失败` : '全部通过');
process.exit(fails ? 1 : 0);
