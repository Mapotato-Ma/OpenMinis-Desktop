/* 设置状态模块的回归检查 —— 把「全量替换 PUT」的四条规则钉死。
 *
 *   node --test web/scripts/settings-model.check.mjs
 *   （或 npm run check:settings，见 web/package.json）
 *
 * 这个文件存在的理由：这些规则以前散在 app.js 的 28 个变更点里，只能靠人肉在
 * 浏览器里点 —— 而它们每一条错了都是「配置看着配好了，其实没生效」这种静默故障。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// 按「浏览器把 classic script 塞进页面全局」的方式装载它 —— 这同时验证了
// 「桌面界面没有构建步骤」这条取舍：模块就是一个能被 <script> 直接吃的文件。
// （用 new Function 而不是 node:vm：vm 里造出来的对象属于另一个 realm，
//   原型不同会让 deepStrictEqual 把"结构一样"报成不等。）
const SRC = readFileSync(new URL('../desktop/settings-model.js', import.meta.url), 'utf8');
const mod = { exports: {} };
new Function('module', 'window', SRC)(mod, undefined);
const M = mod.exports;

/** 一份贴近内核 /api/settings 的载荷。 */
function serverPayload() {
  return {
    providers: [
      { id: 'p1', type: 'openai-compatible', label: '甲', baseUrl: 'https://a/v1', model: 'm-a', hasKey: true },
      { id: 'p2', type: 'anthropic', label: '乙', baseUrl: 'https://b', model: 'm-b', hasKey: false },
    ],
    modelSlots: [
      { slot: 'chat', label: '对话', instanceId: 'p1', model: 'm-a' },
      { slot: 'compaction', label: '压缩', instanceId: '', model: '' },
    ],
    providerTypes: [
      { type: 'openai-compatible', label: 'OpenAI 兼容', defaultModel: 'gpt-4o-mini' },
      { type: 'anthropic', label: 'Anthropic', defaultModel: 'claude-x' },
    ],
    identities: [
      { id: 'assistant', name: '助手', recommendedTools: ['skill_use', 'send', 'read'], enabledTools: ['skill_use', 'send', 'read'] },
      { id: 'writer', name: '写作', recommendedTools: ['skill_use', 'send'], enabledTools: ['skill_use', 'send'] },
    ],
    toolCatalog: [{ id: 'skill_use' }, { id: 'send' }, { id: 'read' }, { id: 'search' }, { id: 'subagent_delegate' }],
    activeIdentityId: 'assistant',
    agent: { subagentEnabled: true, maxTurns: 12 },
  };
}

const fresh = () => M.load(serverPayload());

test('刚加载时什么都不算脏，且载荷里不含密钥', () => {
  const { body, dirty } = M.toPayload(fresh());
  assert.equal(dirty.any, false);
  assert.equal(dirty.models, false);
  assert.ok(!('identityEdits' in body), '没改过的身份不该出现在 identityEdits 里');
  assert.ok(!('agent' in body), '没改过的 agent 不该出现在载荷里');
  assert.ok(body.providers.every((p) => !('apiKey' in p)), 'apiKey 不该凭空出现');
});

test('规则 1：每次都发完整服务商列表（全量替换）', () => {
  const { body } = M.toPayload(fresh());
  assert.deepEqual(body.providers.map((p) => p.id), ['p1', 'p2']);
  assert.equal(body.providers[0].baseUrl, 'https://a/v1');
});

test('规则 2：只在真的敲了密钥时才带 apiKey；留空 = 保持已存密文', () => {
  let s = M.reduce(fresh(), { type: 'provider/setKey', id: 'p2', key: 'sk-typed' });
  let { body, dirty } = M.toPayload(s);
  assert.equal(body.providers.find((p) => p.id === 'p2').apiKey, 'sk-typed');
  assert.ok(!('apiKey' in body.providers.find((p) => p.id === 'p1')), '没敲过的那个不该带上');
  assert.equal(dirty.models, false, '填密钥不算"改了服务商配置"以外的脏 —— 但它确实该亮未保存');

  // 敲了又清空 → 回到"保持不动"
  s = M.reduce(s, { type: 'provider/setKey', id: 'p2', key: '' });
  ({ body } = M.toPayload(s));
  assert.ok(!('apiKey' in body.providers.find((p) => p.id === 'p2')));
});

test('规则 3：槽位要么完整，要么显式 null', () => {
  const s = M.reduce(fresh(), { type: 'slot/set', slot: 'compaction', instanceId: 'p2', model: 'm-b' });
  const { body } = M.toPayload(s);
  assert.deepEqual(body.modelSlots.compaction, { instanceId: 'p2', model: 'm-b' });
  // 清空之后必须发 null，不能漏掉这个键 —— 否则服务端会保留旧绑定
  const cleared = M.toPayload(M.reduce(s, { type: 'slot/set', slot: 'compaction', instanceId: '' }));
  assert.ok('compaction' in cleared.body.modelSlots);
  assert.equal(cleared.body.modelSlots.compaction, null);
});

test('规则 4：删服务商必须同时清掉指向它的槽位（否则服务端整单拒绝）', () => {
  const s = M.reduce(fresh(), { type: 'provider/remove', id: 'p1' });
  const { body } = M.toPayload(s);
  assert.deepEqual(body.providers.map((p) => p.id), ['p2']);
  assert.equal(body.modelSlots.chat, null, 'chat 槽指向被删的 p1，必须清成 null');
  assert.deepEqual(s.slots.chat, { instanceId: '', model: '' });
});

test('规则 5：添加服务商时，空着的对话槽自动接上（v0.1.1 那个 bug）', () => {
  // 全新安装的样子：一个服务商都没有，对话槽也是空的
  const bare = M.load({
    ...serverPayload(),
    providers: [],
    modelSlots: [{ slot: 'chat', label: '对话', instanceId: '', model: '' }],
  });
  const s = M.reduce(bare, {
    type: 'provider/add',
    id: 'p3',
    provider: { type: 'anthropic', baseUrl: 'https://c' },
    meta: { label: 'Anthropic', defaultModel: 'claude-x' },
  });
  assert.equal(s.slots.chat.instanceId, 'p3');
  assert.equal(s.slots.chat.model, 'claude-x', 'model 回落到该类型的默认值');

  // 对话槽已经有绑定时不许抢
  const s2 = M.reduce(fresh(), {
    type: 'provider/add', id: 'p4', provider: { type: 'anthropic' }, meta: { defaultModel: 'claude-x' },
  });
  assert.equal(s2.slots.chat.instanceId, 'p1');
});

test('槽位选服务商时 model 自动回落到该服务商的模型', () => {
  const s = M.reduce(fresh(), { type: 'slot/set', slot: 'compaction', instanceId: 'p2' });
  assert.deepEqual(s.slots.compaction, { instanceId: 'p2', model: 'm-b' });
});

test('「恢复推荐」不该亮未保存，也不该写出覆盖', () => {
  const s = M.reduce(fresh(), { type: 'identity/setAllTools', id: 'assistant', mode: 'recommended' });
  const { body, dirty } = M.toPayload(s);
  assert.equal(dirty.identity, false);
  assert.ok(!('identityEdits' in body));
});

test('真改了工具 → identityEdits 只含改过的那个身份，且锁定工具被补回', () => {
  const s = M.reduce(fresh(), { type: 'identity/setTools', id: 'writer', tools: ['read'] });
  const { body, dirty } = M.toPayload(s);
  assert.equal(dirty.identity, true);
  assert.equal(body.identityEdits.length, 1);
  assert.equal(body.identityEdits[0].id, 'writer');
  // 内核会无条件补这三个，界面不能声称"一个工具都没有"
  for (const id of ['skill_use', 'send', 'subagent_delegate']) {
    assert.ok(body.identityEdits[0].enabledTools.includes(id), `${id} 必须留着`);
  }
});

test('「全不选」也保留锁定工具', () => {
  const s = M.reduce(fresh(), { type: 'identity/setAllTools', id: 'assistant', mode: 'none' });
  assert.deepEqual(M.identityTools(s, 'assistant').sort(), ['send', 'skill_use', 'subagent_delegate']);
});

test('enabledTools 为 null（没覆盖）时回落到推荐列表，显式 [] 保持为空', () => {
  const p = serverPayload();
  p.identities[0].enabledTools = null;
  p.identities[1].enabledTools = [];
  const s = M.load(p);
  assert.deepEqual(M.identityTools(s, 'assistant').sort(), ['read', 'send', 'skill_use']);
  assert.deepEqual(M.identityTools(s, 'writer'), []);
  assert.equal(M.toPayload(s).dirty.any, false, '归一化之后不该凭空算成脏');
});

test('agent 与身份切换各自独立成脏，互不牵连', () => {
  let s = M.reduce(fresh(), { type: 'agent/patch', patch: { maxTurns: 30 } });
  let r = M.toPayload(s);
  assert.equal(r.dirty.agent, true);
  assert.equal(r.dirty.models, false);
  assert.equal(r.dirty.identity, false);
  assert.equal(r.body.agent.maxTurns, 30);

  s = M.reduce(fresh(), { type: 'identity/setActive', id: 'writer' });
  r = M.toPayload(s);
  assert.equal(r.dirty.identity, true);
  assert.equal(r.body.activeIdentityId, 'writer');
  assert.equal(r.dirty.models, false);
});

test('reduce 不原地改传入的 state（渲染层靠引用比较决定重画）', () => {
  const s = fresh();
  const before = JSON.stringify(s);
  M.reduce(s, { type: 'provider/remove', id: 'p1' });
  M.reduce(s, { type: 'identity/setTools', id: 'writer', tools: [] });
  assert.equal(JSON.stringify(s), before);
});

test('view() 把内部形状翻译成渲染层要的投影', () => {
  const v = M.view(fresh());
  assert.equal(v.providers.length, 2);
  assert.equal(v.chat.instanceId, 'p1');
  assert.equal(v.chatProvider.id, 'p1');
  assert.equal(v.slots.find((s) => s.slot === 'chat').label, '对话');
  assert.equal(v.identities.find((i) => i.id === 'assistant').active, true);
  assert.equal(v.selectedIdentityId, 'assistant');
  assert.deepEqual(v.lockedToolIds, ['skill_use', 'send', 'subagent_delegate']);
});

test('view() 不泄露用户刚敲的密钥明文', () => {
  const s = M.reduce(fresh(), { type: 'provider/setKey', id: 'p1', key: 'sk-secret-value' });
  const dumped = JSON.stringify(M.view(s));
  assert.ok(!dumped.includes('sk-secret-value'), '密钥不能进渲染层');
  assert.ok(!dumped.includes('sk-secret'), '密钥不能进渲染层');
});

test('reset 丢弃草稿回到服务端状态', () => {
  let s = M.reduce(fresh(), { type: 'provider/remove', id: 'p1' });
  assert.equal(M.toPayload(s).dirty.models, true);
  s = M.reduce(s, { type: 'reset' });
  assert.equal(M.toPayload(s).dirty.any, false);
});

test('load(server, prev) 保留未保存的编辑（重新加载不丢草稿）', () => {
  const s1 = M.reduce(fresh(), { type: 'provider/setKey', id: 'p2', key: 'sk-keep' });
  const s2 = M.load(serverPayload(), s1);
  assert.equal(M.toPayload(s2).body.providers.find((p) => p.id === 'p2').apiKey, 'sk-keep');
});
