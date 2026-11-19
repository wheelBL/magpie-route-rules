import { test } from 'node:test'
import assert from 'node:assert/strict'
import { onRequest, onEvent, onResponse } from '../route-rules.middleware.js'

const base = { model: 'group/steady', messages: [{ role: 'user', content: 'hi' }], max_tokens: 100 }
const body = (over = {}) => ({ ...base, ...over })
const ctx = (options = {}, state = {}) => ({ options, state, status: 200 })
const imgPart = { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } }

const opts = (over = {}) => ({
  rules: [
    { type: 'images', target: 'cannbot/deepseek-v4.1-flash' },
    { type: 'input', at: 100000, target: 'cannbot/deepseek-v4-pro' },
    { type: 'output', at: 4096, target: 'cannbot/glm-5.3' },
  ],
  limits: { 'cannbot/deepseek-v4.1-flash': 4096 },
  clamp: true,
  ...over,
})

test('images 规则把带图请求路由到指定模型', () => {
  const state = {}
  const out = onRequest(body({ messages: [{ role: 'user', content: [{ type: 'text', text: '看图' }, imgPart] }] }), ctx(opts(), state))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
  assert.equal(state.asked, 'group/steady')
})

test('无规则命中时不改写并返回 undefined', () => {
  assert.equal(onRequest(body(), ctx(opts())), undefined)
})

test('input 规则按估算输入长度路由（ASCII 4 字符/token）', () => {
  const long = 'a'.repeat(400000) // ~100k tokens
  const out = onRequest(body({ messages: [{ role: 'user', content: long }] }), ctx(opts()))
  assert.equal(out.model, 'cannbot/deepseek-v4-pro')
})

test('input 规则对 CJK 按 1.1 字符/token 估算', () => {
  const long = '汉'.repeat(110000) // ~121k tokens
  const out = onRequest(body({ messages: [{ role: 'user', content: long }] }), ctx(opts()))
  assert.equal(out.model, 'cannbot/deepseek-v4-pro')
})

test('output 规则按请求预算路由', () => {
  const out = onRequest(body({ max_tokens: 5000 }), ctx(opts()))
  assert.equal(out.model, 'cannbot/glm-5.3')
})

test('rules 按序首中：带图优先于长度', () => {
  const long = 'a'.repeat(400000)
  const out = onRequest(body({ messages: [{ role: 'user', content: [{ type: 'text', text: long }, imgPart] }] }), ctx(opts()))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('limits 钳制：重写后的目标模型也会被收敛', () => {
  const o = { rules: [{ type: 'output', at: 4096, target: 'cannbot/deepseek-v4.1-flash' }], limits: { 'cannbot/deepseek-v4.1-flash': 4096 }, clamp: true }
  const out = onRequest(body({ max_tokens: 100000 }), ctx(o))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
  assert.equal(out.max_tokens, 4096)
})

test('clamp=false 时不收敛', () => {
  const o = { rules: [], limits: { 'group/steady': 4096 }, clamp: false }
  assert.equal(onRequest(body({ max_tokens: 100000 }), ctx(o)), undefined)
})

test('clamp 直接作用于所问模型（无规则）', () => {
  const o = { rules: [], limits: { 'group/steady': 4096 }, clamp: true }
  const out = onRequest(body({ max_tokens: 100000 }), ctx(o))
  assert.equal(out.model, 'group/steady')
  assert.equal(out.max_tokens, 4096)
})

test('scope 限制作用的模型范围', () => {
  const o = opts({ scope: ['cannbot/glm-5.3'] })
  assert.equal(onRequest(body(), ctx(o)), undefined, 'group/steady 不在 scope → 不处理')
  const state = {}
  const out = onRequest(body({ model: 'cannbot/glm-5.3', messages: [{ role: 'user', content: [imgPart] }] }), ctx(o, state))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash', 'scope 内 → 规则生效')
})

test('scope 支持 /正则/', () => {
  const o = opts({ scope: ['/^group\\//'] })
  const state = {}
  const out = onRequest(body({ messages: [{ role: 'user', content: [imgPart] }] }), ctx(o, state))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('onEvent/onResponse 把回复里的模型名还原为所问名称', () => {
  const state = {}
  onRequest(body({ messages: [{ role: 'user', content: [imgPart] }] }), ctx(opts(), state))
  const ev = { type: 'message_start', message: { model: 'cannbot/deepseek-v4-1-flash-261017' } }
  const fixed = onEvent(ev, ctx(opts(), state))
  assert.equal(fixed.message.model, 'group/steady')
  const resp = onResponse({ model: 'cannbot/deepseek-v4-1-flash-261017', usage: {} }, ctx(opts(), state))
  assert.equal(resp.model, 'group/steady')
})

test('未改写过的请求不做名字还原', () => {
  const state = {}
  onRequest(body(), ctx(opts(), state))
  const ev = { type: 'message_start', message: { model: 'whatever' } }
  assert.equal(onEvent(ev, ctx(opts(), state)), undefined)
})

test('坏 options / 非 JSON 对象 body 一律 fail-open', () => {
  assert.equal(onRequest(body(), ctx(null)), undefined)
  assert.equal(onRequest(body(), ctx({ rules: 'oops' })), undefined)
  assert.equal(onRequest(null, ctx(opts())), undefined)
  assert.equal(onRequest([1], ctx(opts())), undefined)
})

test('缺 model 的请求不处理', () => {
  const b = { messages: [{ role: 'user', content: 'hi' }] }
  assert.equal(onRequest(b, ctx(opts())), undefined)
})

test('规则级 scope：不同组可以路由到不同目标', () => {
  const o = { rules: [
    { type: 'images', target: 'provider-a/vision', scope: ['group/alpha'] },
    { type: 'images', target: 'provider-b/vision', scope: ['group/beta'] },
  ], clamp: false }
  const outA = onRequest(body({ model: 'group/alpha', messages: [{ role: 'user', content: [imgPart] }] }), ctx(o))
  assert.equal(outA.model, 'provider-a/vision')
  const outB = onRequest(body({ model: 'group/beta', messages: [{ role: 'user', content: [imgPart] }] }), ctx(o))
  assert.equal(outB.model, 'provider-b/vision')
  const outC = onRequest(body({ model: 'group/gamma', messages: [{ role: 'user', content: [imgPart] }] }), ctx(o))
  assert.equal(outC, undefined, '两条规则 scope 都不覆盖 → 不处理')
})

test('规则级 scope 用正则覆盖一族组', () => {
  const o = { rules: [{ type: 'images', target: 'provider-a/vision', scope: ['/^group\\/vision-/'] }], clamp: false }
  const out = onRequest(body({ model: 'group/vision-eu', messages: [{ role: 'user', content: [imgPart] }] }), ctx(o))
  assert.equal(out.model, 'provider-a/vision')
})

test('无 limits 字段时钳制跳过', () => {
  const o = opts({ limits: undefined })
  const out = onRequest(body({ max_tokens: 100000 }), ctx(o))
  assert.equal(out.max_tokens, 100000, '无规则命中、无钳制 → 原样')
})

// ── 自学习：learned.images 驱动候选选择 ─────────────────────────────────────

const imgOpts = (over = {}) => opts({
  rules: [{ type: 'images', target: 'cannbot/deepseek-v4.1-flash', fallbacks: ['cannbot/glm-5.3', 'cannbot/qwen3.8-max'] }],
  learningTtlMs: 21600000,
  ...over,
})
const imgBody = () => body({ messages: [{ role: 'user', content: [imgPart] }] })

test('学习：首选模型被标 bad → 跳到 fallback', () => {
  const now = Date.now()
  const learned = { images: { 'cannbot/deepseek-v4.1-flash': { state: 'bad', at: now } } }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/glm-5.3')
})

test('学习：所有候选都 bad → 回落首选（fail-open）', () => {
  const now = Date.now()
  const learned = { images: {
    'cannbot/deepseek-v4.1-flash': { state: 'bad', at: now },
    'cannbot/glm-5.3': { state: 'bad', at: now },
    'cannbot/qwen3.8-max': { state: 'bad', at: now },
  } }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('学习：TTL 过期的 bad 视为 unknown，恢复首选试探', () => {
  const learned = { images: { 'cannbot/deepseek-v4.1-flash': { state: 'bad', at: Date.now() - 7 * 3600e3 } } }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('学习：标 good 的模型优先于未知候选', () => {
  const learned = { images: { 'cannbot/qwen3.8-max': { state: 'good', at: Date.now() } } }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/qwen3.8-max')
})

test('学习：bad 的候选排到末位而不是被丢弃（前面全失效时仍可兜底）', () => {
  const now = Date.now()
  const learned = { images: { 'cannbot/glm-5.3': { state: 'bad', at: now } } }
  // 首选被 fallback 链跳过：deepseek bad？不 —— deepseek unknown，先走 deepseek
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash', '首选 unknown 优先于 fallback bad')
})

test('学习：非图片规则不受 learned 影响', () => {
  const now = Date.now()
  const o = opts({
    rules: [{ type: 'output', at: 4096, target: 'cannbot/deepseek-v4.1-flash', fallbacks: ['cannbot/glm-5.3'] }],
    learned: { images: { 'cannbot/deepseek-v4.1-flash': { state: 'bad', at: now } } },
  })
  const out = onRequest(body({ max_tokens: 5000 }), ctx(o))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('学习：fallbacks 之外的候补不会凭空出现', () => {
  const now = Date.now()
  const learned = { images: {
    'cannbot/deepseek-v4.1-flash': { state: 'bad', at: now },
    'cannbot/glm-5.3': { state: 'bad', at: now },
    'cannbot/qwen3.8-max': { state: 'bad', at: now },
    'cannbot/deepseek-v4-pro': { state: 'good', at: now },
  } }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash', 'good 的 deepseek-v4-pro 不在候选链 → 不使用')
})

// ── 失败动作学习：pause / clampOutput ────────────────────────────────────────

test('失败动作：pause 的模型退出候选（熔断）', () => {
  const now = Date.now()
  const learned = {
    images: {},
    actions: [{ model: 'cannbot/deepseek-v4.1-flash', kind: 'pause', ttlMs: 600000, at: now, by: 'ai' }],
  }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/glm-5.3', 'deepseek 被熔断 → 首个存活候选')
})

test('失败动作：pause 也作用于非图片规则', () => {
  const now = Date.now()
  const learned = {
    actions: [{ model: 'cannbot/deepseek-v4.1-flash', kind: 'pause', ttlMs: 600000, at: now, by: 'ai' }],
  }
  const o = opts({ rules: [{ type: 'output', at: 4096, target: 'cannbot/deepseek-v4.1-flash', fallbacks: ['cannbot/glm-5.3'] }], learned })
  const out = onRequest(body({ max_tokens: 5000 }), ctx(o))
  assert.equal(out.model, 'cannbot/glm-5.3')
})

test('失败动作：全部候选 pause 时 fail-open 回落首选', () => {
  const now = Date.now()
  const learned = {
    actions: ['cannbot/deepseek-v4.1-flash', 'cannbot/glm-5.3', 'cannbot/qwen3.8-max'].map((model) => ({
      model, kind: 'pause', ttlMs: 600000, at: now, by: 'ai',
    })),
  }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash')
})

test('失败动作：过期 pause 不生效', () => {
  const learned = {
    actions: [{ model: 'cannbot/deepseek-v4.1-flash', kind: 'pause', ttlMs: 600000, at: Date.now() - 700000, by: 'ai' }],
  }
  const out = onRequest(imgBody(), ctx(imgOpts({ learned })))
  assert.equal(out.model, 'cannbot/deepseek-v4.1-flash', 'pause 过期 → 正常选中首选')
})

test('失败动作：clampOutput 对最终模型收敛预算', () => {
  const now = Date.now()
  const learned = {
    actions: [{ model: 'cannbot/glm-5.3', kind: 'clampOutput', cap: 8192, ttlMs: 3600000, at: now, by: 'ai' }],
  }
  const o = imgOpts({ rules: [{ type: 'images', target: 'cannbot/glm-5.3' }], learned })
  const out = onRequest(body({ messages: [{ role: 'user', content: [imgPart] }], max_tokens: 100000 }), ctx(o))
  assert.equal(out.model, 'cannbot/glm-5.3')
  assert.equal(out.max_tokens, 8192)
})

test('失败动作：多个 clampOutput 取最小；未知 kind 忽略', () => {
  const now = Date.now()
  const learned = {
    actions: [
      { model: 'cannbot/glm-5.3', kind: 'clampOutput', cap: 8192, ttlMs: 3600000, at: now, by: 'rule' },
      { model: 'cannbot/glm-5.3', kind: 'clampOutput', cap: 4096, ttlMs: 3600000, at: now, by: 'rule' },
      { model: 'cannbot/glm-5.3', kind: 'unknownKind', ttlMs: 3600000, at: now, by: 'ai' },
    ],
  }
  const o = imgOpts({ rules: [{ type: 'images', target: 'cannbot/glm-5.3' }], learned })
  const out = onRequest(body({ messages: [{ role: 'user', content: [imgPart] }], max_tokens: 100000 }), ctx(o))
  assert.equal(out.max_tokens, 4096)
})
