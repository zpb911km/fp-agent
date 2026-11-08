/* ═══════════════════════════════════════════════════════════
   FP WebUI — 工具卡元信息（后端驱动）

   动机：工具卡参数区原本只有两档 —— 手写渲染器 / 裸 JSON。
   裸 JSON 把 OpenAI schema 的 name/type/required/desc 全丢了，
   而这些信息后端本就有（tools/__init__.definitions()）。

   现在：启动时拉 /api/ui/schema，缓存 ui.tool_cards，参数区
   有元信息就画结构化摘要，没有就退回 JSON —— 新增工具的卡片
   展示只需后端（插件）提供元信息，前端源码零改动。

   与 renderers.js 的分工：
     · renderers = 手写定制（某个工具需要特殊可视化时才写）
     · toolmeta  = 数据驱动的通用结构化展示（绝大多数工具）
     · JSON 兜底 = 两者都没有时的最后防线，绝不丢数据
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { authFetch } from './http.js';

/** @type {Map<string, {name:string, desc:string, params:Array<{name:string, required:boolean, type:string, desc:string}>}>} */
const cards = new Map();

/** 元信息是否已加载（未加载时参数区直接走 JSON，避免闪动） */
let loaded = false;

/**
 * 拉取后端 UI 描述并缓存工具卡元信息。
 * 失败静默降级：loaded 保持 false，所有工具退回 JSON 兜底。
 * @returns {Promise<boolean>} 是否加载成功
 */
export async function fetchToolMeta() {
  try {
    const r = await authFetch('/api/ui/schema');
    if (!r.ok) return false;
    const data = await r.json();
    const list = (data.ui && data.ui.tool_cards) || [];
    cards.clear();
    for (const c of list) {
      if (c && typeof c.name === 'string' && c.name) cards.set(c.name, c);
    }
    loaded = true;
    return true;
  } catch (e) {
    console.warn('[ToolMeta] 获取工具卡元信息失败:', e);
    return false;
  }
}

/**
 * 取某工具的后端元信息（无则 null → 调用方走 JSON 兜底）。
 * @param {string} name 工具名
 */
export function getToolCard(name) {
  if (!loaded || !name) return null;
  return cards.get(name) || null;
}

/** 是否已加载（供测试 / 调试对账） */
export function isLoaded() {
  return loaded;
}

/** 已加载的工具卡数量（供 e2e 断言"后端下发了多少"） */
export function toolCardCount() {
  return cards.size;
}

/** 已加载的工具名清单（供测试遍历取元信息，避免测试重复发请求 —— 重复 fetch 会撞上 token 已被 401 分支清除的情况） */
export function toolCardNames() {
  return Array.from(cards.keys());
}
