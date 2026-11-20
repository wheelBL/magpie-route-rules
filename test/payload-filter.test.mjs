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
console.log('middleware payload filter: pass')

