import { onRequest } from '../route-rules.middleware.js'
const caps = { 'cannbot/deepseek-v4.1-flash': { minFailTok: 90000 } }
const opts = {
  rules: [{ type: 'images', target: 'cannbot/deepseek-v4.1-flash', fallbacks: ['cannbot/glm-5.3'] }],
  learned: { images: {}, payloadCaps: caps },
}
// 大输入 + 带图 → 应绕过 deepseek（minFailTok=90000 ≤ 估算 tokens）落到 glm
const big = { model: 'group/steady', messages: [{ role: 'user', content: [
  { type: 'text', text: 'x'.repeat(200000) },
  { type: 'image_url', image_url: { url: 'data:image/png;base64,x' } },
] }] }
const r1 = onRequest(big, { options: opts, state: {} })
console.log('big+img ->', r1 && r1.model)
if (!r1 || r1.model !== 'cannbot/glm-5.3') { console.error('FAIL: expected glm'); process.exit(1) }
// 小输入带图 → 正常首选 deepseek
const small = { model: 'group/steady', messages: [{ role: 'user', content: [
  { type: 'text', text: '这张图是什么颜色' },
  { type: 'image_url', image_url: { url: 'data:image/png;base64,x' } },
] }] }
const r2 = onRequest(small, { options: opts, state: {} })
console.log('small+img ->', r2 && r2.model)
if (!r2 || r2.model !== 'cannbot/deepseek-v4.1-flash') { console.error('FAIL: expected deepseek'); process.exit(1) }
// 无图大输入 → images 规则不触发，不改写
const noimg = { model: 'group/steady', messages: [{ role: 'user', content: 'x'.repeat(200000) }] }
const r3 = onRequest(noimg, { options: opts, state: {} })
console.log('big noimg ->', r3 === undefined || r3.model)
// 0.3.48：input 规则的候选 walk 同样按学习容量过滤（目标 minFailTok ≤ 估算 → 换 fallback）
const caps2 = { 'cannbot/deepseek-v4-pro': { minFailTok: 45000 }, 'cannbot/glm-5.3': { maxOkTok: 476249 } }
const opts2 = { rules: [{ type: 'input', at: 40000, target: 'cannbot/deepseek-v4-pro', fallbacks: ['cannbot/glm-5.3'] }], learned: { payloadCaps: caps2 } }
const big2 = { model: 'group/steady', messages: [{ role: 'user', content: [
  { type: 'text', text: 'x'.repeat(200000) },
  { type: 'image_url', image_url: { url: 'data:image/png;base64,x' } },
] }] }
const r4 = onRequest(big2, { options: opts2, state: {} }) // 全新 body：共享对象会被前次改写污染
console.log('big input-rule ->', r4 && r4.model)
if (!r4 || r4.model !== 'cannbot/glm-5.3') { console.error('FAIL: expected glm for input rule'); process.exit(1) }
// 0.3.48：无规则命中 + 所问模型有拒收证据（minFailTok ≤ 估算）→ 改写到学习中容量最大者
const caps3 = {
  'cannbot/qwen3.8-max': { minFailTok: 344064 },
  'cannbot/deepseek-v4.1-flash': { maxOkTok: 494735 },
  'cannbot/glm-5.3': { maxOkTok: 476249 },
}
const huge = { model: 'cannbot/qwen3.8-max', messages: [{ role: 'user', content: 'x'.repeat(700000) }] } // 估算 ÷2 → 350k tokens：≥344064 且 <476k，容量过滤后只剩 deepseek
const r5 = onRequest(huge, { options: { rules: [], learned: { payloadCaps: caps3 }, clamp: false }, state: {} })
console.log('rule-less rescue ->', r5 && r5.model)
if (!r5 || r5.model !== 'cannbot/deepseek-v4.1-flash') { console.error('FAIL: expected deepseek rescue'); process.exit(1) }
// 所问模型无拒收证据 → 不改写（fail-open）
const r6 = onRequest({ model: 'cannbot/glm-5.3', messages: [{ role: 'user', content: 'x'.repeat(700000) }] }, { options: { rules: [], learned: { payloadCaps: caps3 }, clamp: false }, state: {} })
if (r6 !== undefined) { console.error('FAIL: expected no rewrite without evidence'); process.exit(1) }
console.log('middleware payload filter: pass')

