/* ==========================================================================
   settings-model.js — 设置页的状态与规则（深模块）

   为什么单独一个模块：内核的 ``/api/settings`` 是**全量替换 PUT** —— 你发过去的
   列表就是新状态。这带来四条必须遵守的规则：

     1. 每次提交都要发**完整**的服务商列表，不是只发被改的那个；
     2. ``apiKey`` 留空 = 「保持已存的密文」（服务端从不把密钥发回来，客户端也
        没法原样带回去，所以客户端永远不需要搬运密文）；
     3. 槽位要么完整（instanceId + model），要么显式 ``null``；
     4. 删服务商时必须同时清掉指向它的槽位绑定，否则服务端以「modelSlots 指向
        未配置的厂商」**整单拒绝**（症状：删掉一个没用的服务商，结果保存失败）。

   另外两条从真实事故里长出来的：
     · 「恢复推荐」写回的是推荐列表，但**不该**因此亮出「未保存」—— 脏判定必须
       拿草稿和服务端当前值比，而不是「用户点过没」；
     · 添加服务商时若「对话」槽还是空的，自动把它设为当前 —— 「加了服务商」和
       「对话能跑」在内核里是两件事，界面必须把前者接到后者上。

   以前这些规则散在 app.js 的 28 个变更点上，靠一份全局可变草稿传递，
   已知的两次线上问题都出在这里。现在它们都住在这个文件里。

   接口只有四个（都是纯函数，不碰 DOM、不碰网络）：
     load(server, prev)   → state     服务端载荷 → 新状态（带 baseline 用于脏判定）
     reduce(state, action)→ state     所有改动走这里；返回新对象，不原地改
     toPayload(state)     → {body, dirty}
     view(state)          → 渲染层要的投影（把内部形状翻译成视图形状）

   测试：web/scripts/settings-model.check.mjs（node --test，无需浏览器）。
   ========================================================================== */

/* 为什么整块包在 IIFE 里：桌面界面是**多个 classic script 共享同一个全局作用域**
   （没有构建步骤，也就没有模块系统）。顶层再声明一个 const/function 就会和
   app.js 撞名 —— 撞了不是覆盖，而是 SyntaxError，整个 app.js 直接不执行，
   页面看起来"渲染出来了但什么都点不动"。所以这里只往外放一个 window.SettingsModel。 */
(function (global) {
  'use strict';

  /* 内核会无条件补进工具列表的三个 —— 界面上不能关，也不参与「全不选」。 */
  const LOCKED_TOOL_IDS = ['skill_use', 'send', 'subagent_delegate'];

  const SLOT_CHAT = 'chat';

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function normSlot(v) {
    const instanceId = (v && v.instanceId) || '';
    const model = (v && v.model) || '';
    return instanceId ? { instanceId, model } : { instanceId: '', model: '' };
  }

  function defaultModelFor(server, type) {
    const meta = ((server && server.providerTypes) || []).find((t) => t.type === type) || {};
    return meta.defaultModel || '';
  }

  /** 服务端载荷 → 初始状态。``prev`` 保留未保存的编辑（重新加载时不该丢草稿）。 */
  function load(server, prev) {
    const src = server || {};
    const providers = (src.providers || []).map((p) => ({
      id: p.id,
      type: p.type,
      label: p.label || '',
      baseUrl: p.baseUrl || '',
      model: p.model || '',
      hasKey: !!p.hasKey,
      engine: p.engine,
    }));

    const slots = {};
    for (const s of src.modelSlots || []) slots[s.slot] = normSlot(s);

    const tools = {};
    for (const i of src.identities || []) {
      // enabledTools 是三态：null=没覆盖（回落推荐）、[...]=显式覆盖、[]=一个都不给。
      // 这里把 null 归到「推荐」，界面才会显示它实际生效的工具集合；[] 保持为空。
      tools[i.id] = ((i.enabledTools != null ? i.enabledTools : i.recommendedTools) || []).slice();
    }

    const state = {
      server: src,
      providers,
      /** 用户刚敲进去、还没保存的密钥：{providerId: 'plaintext'}。从不进 view()。 */
      keys: (prev && prev.keys) ? clone(prev.keys) : {},
      slots,
      agent: Object.assign({}, src.agent || {}),
      identities: {
        activeId: src.activeIdentityId || 'assistant',
        selected: ((src.identities || [])[0] || {}).id || null,
        tools,
      },
      baseline: {
        providers: clone(providers),
        slots: clone(slots),
        agent: Object.assign({}, src.agent || {}),
        activeIdentityId: src.activeIdentityId || 'assistant',
        tools: clone(tools),
      },
    };

    if (prev) {
      // 保留未保存的编辑：把上一次的草稿叠回来（密钥、槽位、工具、agent）。
      if (prev.slots) Object.assign(state.slots, clone(prev.slots));
      if (prev.agent) state.agent = Object.assign({}, state.agent, clone(prev.agent));
      if (prev.identities) {
        state.identities.activeId = prev.identities.activeId || state.identities.activeId;
        state.identities.selected = prev.identities.selected || state.identities.selected;
        const pick = (obj, merged) => {
          const out = Object.assign({}, merged);
          for (const id of Object.keys(obj || {})) {
            if (id in out) out[id] = obj[id].slice();
          }
          return out;
        };
        state.identities.tools = pick(prev.identities.tools, state.identities.tools);
      }
    }
    return state;
  }

  /* ── 动作 ────────────────────────────────────────────────────────────────
     命名是 ``域/动词``，这样看调用点就知道它在改哪一块。
     ─────────────────────────────────────────────────────────────────────── */
  function reduce(state, action) {
    const a = action || {};
    const next = Object.assign({}, state, {
      providers: state.providers.slice(),
      keys: Object.assign({}, state.keys),
      slots: Object.assign({}, state.slots),
      agent: Object.assign({}, state.agent),
      identities: Object.assign({}, state.identities, {
        tools: Object.assign({}, state.identities.tools),
      }),
    });

    switch (a.type) {
      case 'provider/add': {
        const p = a.provider || {};
        const meta = a.meta || {};
        const created = {
          id: p.id || a.id || '',
          type: p.type,
          label: p.label || meta.label || '',
          baseUrl: p.baseUrl || '',
          model: p.model || meta.defaultModel || '',
          hasKey: false,
          engine: p.engine,
        };
        next.providers.push(created);
        // 规则 5：对话槽空着就把它接上 —— 否则"配好了但会话说没有配置模型服务"
        if (!next.slots[SLOT_CHAT] || !next.slots[SLOT_CHAT].instanceId) {
          next.slots[SLOT_CHAT] = { instanceId: created.id, model: created.model };
        }
        return next;
      }

      case 'provider/patch': {
        next.providers = next.providers.map((p) => (
          p.id === a.id ? Object.assign({}, p, a.patch || {}) : p
        ));
        return next;
      }

      case 'provider/setKey': {
        // 空串 = 保持已存的密文（规则 2），所以不落进 keys。
        const v = a.key == null ? '' : String(a.key);
        if (v) next.keys[a.id] = v;
        else delete next.keys[a.id];
        return next;
      }

      case 'provider/remove': {
        next.providers = next.providers.filter((p) => p.id !== a.id);
        delete next.keys[a.id];
        // 规则 4：指向它的槽位必须一起清掉，否则服务端整单拒绝
        for (const slot of Object.keys(next.slots)) {
          if (next.slots[slot] && next.slots[slot].instanceId === a.id) {
            next.slots[slot] = { instanceId: '', model: '' };
          }
        }
        return next;
      }

      case 'slot/set': {
        const p = next.providers.find((x) => x.id === (a.instanceId || ''));
        // 给了 model 就原样采用（允许显式空串 —— 用户就是在清空它）；只换绑服务商
        // 时按该实例的模型 / 该类型默认值回落。
        const model = ('model' in a)
          ? (a.model || '')
          : (p ? (p.model || defaultModelFor(state.server, p.type)) : '');
        next.slots[a.slot] = p ? { instanceId: p.id, model } : { instanceId: '', model: '' };
        return next;
      }

      case 'identity/select':
        next.identities.selected = a.id || null;
        return next;

      case 'identity/setActive':
        next.identities.activeId = a.id || next.identities.activeId;
        return next;

      case 'identity/setTools': {
        const ids = (a.tools || []).slice();
        // 锁定工具永远在列表里（内核也会补，但界面必须说实话）
        for (const id of LOCKED_TOOL_IDS) if (!ids.includes(id)) ids.push(id);
        next.identities.tools[a.id] = ids;
        return next;
      }

      case 'identity/setAllTools': {
        const ids = allToolIds(state.server).filter((t) => !LOCKED_TOOL_IDS.includes(t));
        const chosen = a.mode === 'all' ? ids.concat(LOCKED_TOOL_IDS)
          : a.mode === 'recommended' ? recommendedToolIds(state.server, a.id)
            : LOCKED_TOOL_IDS.slice();
        // 'recommended' 原样写回推荐列表（不加锁定工具）：否则「恢复推荐」会与
        // 服务端当前值不同，凭空空亮出「未保存」—— 而它本该是一个无操作。
        next.identities.tools[a.id] = Array.from(new Set(chosen));
        return next;
      }

      case 'agent/patch':
        next.agent = Object.assign({}, next.agent, a.patch || {});
        return next;

      case 'reset':
        return load(state.server, null);

      default:
        return next;
    }
  }

  function allToolIds(server) {
    return ((server && server.toolCatalog) || []).map((t) => t.id);
  }

  function recommendedToolIds(server, id) {
    const ident = ((server && server.identities) || []).find((i) => i.id === id) || {};
    return (ident.recommendedTools || []).slice();
  }

  /* ── 出站：草稿 → PUT body + 脏判定 ──────────────────────────────────────
     四条规则都在这一个函数里兑现。脏判定拿草稿和 baseline 比 —— 这是「点了恢复
     推荐不该亮未保存」的关键（旧写法靠手工 markDirty/clearDirty 记账）。
     ─────────────────────────────────────────────────────────────────────── */
  function toPayload(state) {
    const b = state.baseline;

    const providers = state.providers.map((p) => {
      const out = {
        id: p.id, type: p.type, label: p.label || '',
        baseUrl: p.baseUrl || '', model: p.model || '',
      };
      // 规则 2：只在用户真的敲了密钥时带上它；留空表示「保持已存密文」
      if (state.keys[p.id]) out.apiKey = state.keys[p.id];
      return out;
    });

    const modelSlots = {};
    for (const slot of Object.keys(state.slots)) {
      const v = normSlot(state.slots[slot]);
      // 规则 3：要么完整，要么显式 null
      modelSlots[slot] = (v.instanceId && v.model) ? { instanceId: v.instanceId, model: v.model } : null;
    }

    const dirtyIdentities = (state.server.identities || [])
      .map((i) => i.id)
      .filter((id) => !sameSet(state.identities.tools[id] || [], b.tools[id] || []));

    const body = { providers, modelSlots };
    const agentDirty = JSON.stringify(state.agent) !== JSON.stringify(b.agent);
    if (agentDirty) body.agent = state.agent;
    if (dirtyIdentities.length) {
      // 规则：只发真正改过的身份 —— 把「恢复推荐」写成一堆与推荐值相同的覆盖
      // 会让存储状态和「没有覆盖」分叉，下次也说不清谁对。
      body.identityEdits = dirtyIdentities.map((id) => ({
        id, enabledTools: (state.identities.tools[id] || []).slice(),
      }));
    }
    if (state.identities.activeId !== b.activeIdentityId) body.activeIdentityId = state.identities.activeId;

    const modelsDirty = !sameProviders(state.providers, b.providers) || !sameSlots(state.slots, b.slots);
    const dirty = {
      models: modelsDirty,
      agent: agentDirty,
      identity: dirtyIdentities.length > 0 || state.identities.activeId !== b.activeIdentityId,
      /** 哪些身份的工具列表真的改过 —— 界面用它给单张卡片打「未保存」。 */
      identities: dirtyIdentities,
      any: false,
    };
    dirty.any = dirty.models || dirty.agent || dirty.identity;
    return { body, dirty };
  }

  function sameProviders(a, b) {
    if (a.length !== b.length) return false;
    return a.every((p, i) => {
      const q = b[i];
      return q && p.id === q.id && p.type === q.type && (p.label || '') === (q.label || '')
        && (p.baseUrl || '') === (q.baseUrl || '') && (p.model || '') === (q.model || '');
    });
  }

  function sameSlots(a, b) {
    const ka = Object.keys(a), kb = Object.keys(b || {});
    if (ka.length !== kb.length) return false;
    return ka.every((k) => {
      const x = normSlot(a[k]), y = normSlot((b || {})[k]);
      return x.instanceId === y.instanceId && x.model === y.model;
    });
  }

  function sameSet(a, b) {
    if (a.length !== b.length) return false;
    const s = new Set(b);
    return a.every((x) => s.has(x));
  }

  /* ── 视图投影：渲染层读这个，不去碰内部形状 ─────────────────────────────── */
  function view(state) {
    const slots = ((state.server.modelSlots) || []).map((s) => {
      const cur = normSlot(state.slots[s.slot]);
      return {
        slot: s.slot,
        label: s.label,
        instanceId: cur.instanceId,
        model: cur.model,
        provider: state.providers.find((p) => p.id === cur.instanceId) || null,
      };
    });
    const chat = slots.find((s) => s.slot === SLOT_CHAT) || { instanceId: '', model: '' };
    return {
      providers: state.providers,
      slots,
      agent: state.agent,
      identities: ((state.server.identities) || []).map((i) => ({
        id: i.id, name: i.name, emoji: i.emoji, description: i.description,
        builtin: i.builtin, disabled: i.disabled,
        recommendedTools: (i.recommendedTools || []).slice(),
        tools: (state.identities.tools[i.id] || []).slice(),
        active: i.id === state.identities.activeId,
        selected: i.id === state.identities.selected,
      })),
      toolCatalog: (state.server.toolCatalog) || [],
      lockedToolIds: LOCKED_TOOL_IDS.slice(),
      selectedIdentityId: state.identities.selected,
      chat: { instanceId: chat.instanceId || '', model: chat.model || '' },
      chatProvider: state.providers.find((p) => p.id === (chat.instanceId || '')) || null,
      newKeys: Object.keys(state.keys),
    };
  }

  /* 供渲染层判断「这个 id 是不是锁定工具」。 */
  function isLockedTool(id) {
    return LOCKED_TOOL_IDS.includes(id);
  }

  function identityTools(state, id) {
    return (state.identities.tools[id] || []).slice();
  }
  const api = { load, reduce, toPayload, view, isLockedTool, identityTools, LOCKED_TOOL_IDS, SLOT_CHAT };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;  // node 测试
  if (global) global.SettingsModel = api;                                     // 浏览器
})(typeof window !== 'undefined' ? window : null);
