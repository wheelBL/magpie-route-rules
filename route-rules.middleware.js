// Request-aware routing rules as a magpie gateway middleware.
//
// Runs inside magpie's gateway on every agent request, before provider
// routing: it can send the model an agent asks for as another one by rules
// (images / input length / output budget), and optionally clamp the output
// budget to per-model ceilings.
//
// Self-learning (0.2.0): image-request capability is learned from real
// outcomes. The middleware itself is stateless (gateway hooks are pooled and
// fail open); the learned map arrives through `options.learned.images`
// (modelId → {state: 'bad'|'good', at}) and is maintained by a companion
// (route-learn) that watches magpie's usage log: a model whose image request
// fails (406 / multimodal-flavoured errors) is marked bad for a TTL and is
// skipped for later image requests, so rules stay declarative while the
// actual targets adapt to the environment. Since 0.3.44 a probe or a failed
// image request can also prove a model has no native vision (magpie described
// the image for it, or refused with "can't see images"): that mark carries
// via:'describe' and is re-verified weekly instead of on every TTL.
//
// Failure-action learning (0.3.0): the companion also writes
// `options.learned.actions` — one record per failure signature:
//   { model, kind: 'pause'|'clampOutput'|'ignore', cap?, ttlMs, at, by }
// The middleware enforces the fresh ones: a `pause` on a model takes it out
// of the candidate walk (circuit breaker), a `clampOutput` caps the output
// budget on top of the static limits. `ignore` records a handled signature so
// the AI classifier is not consulted twice.
//
// Options (set with: magpie plugin options magpie-route-rules '<json>'):
//   rules  [{ type: 'images'|'input'|'output', at?: N,
//             target: modelId, fallbacks?: [modelId...] }]
//          evaluated in order; the first match picks the first healthy
//          candidate (for `images`, learned health decides; otherwise the
//          first listed) and rewrites body.model.
//   limits { modelId: maxOutputTokens } — optional per-model ceilings.
//   clamp  true (default) — apply limits to max_tokens/max_completion_tokens.
//   scope  [modelId | /regex/] — optional; only asked models matching the
//          list are processed at all. Default: every request.
//   learned { images: { modelId: {state, at, via?} } } — read-only here;
//          written by the route-learn companion. via:'describe' = the model
//          only saw images through the describe fallback (no native vision):
//          treated as incapable even past the TTL.
//   learned.payloadCaps { modelId: {minFailTok?, maxOkTok?} } — 413-learned
//          capacity evidence: every rule's walk skips candidates known to
//          refuse inputs this size, and with no rule at all the request is
//          rewritten to the learned highest-capacity model (0.3.48).
//   learningTtlMs 6h — a 'bad' mark older than this is treated as unknown.
//
// The asked model's name is restored in replies (onEvent/onResponse), so an
// agent that reads its model's name back sees what it asked for.
//
// Pure JS, no imports: middleware runs in the gateway's own JS engine and may
// only bundle relative files. Every hook fails open.

function textOf(value) {
  return typeof value === 'string' ? value : ''
}

// Estimated prompt tokens: ASCII ~4 chars/token, CJK ~1.1 chars/token.
// Image parts carry no countable text and are ignored on purpose.
function analyze(body) {
  let hasImage = false
  let ascii = 0
  let wide = 0
  const count = (value) => {
    if (typeof value !== 'string' || !value) return
    const wide2 = value.replace(/[\x00-\x7f]/g, '')
    wide += wide2.length
    ascii += value.length - wide2.length
  }
  count(textOf(body.system))
  const messages = Array.isArray(body.messages) ? body.messages : []
  for (const message of messages) {
    const content = message ? message.content : undefined
    if (typeof content === 'string') { count(content); continue }
    if (!Array.isArray(content)) continue
    for (const part of content) {
      if (!part) continue
      if (part.type === 'text') count(part.text)
      else if (part.type === 'image_url' || part.type === 'image' || part.type === 'input_image') hasImage = true
      else if (typeof part === 'string') count(part)
    }
  }
  if (Array.isArray(body.tools)) count(JSON.stringify(body.tools))
  const estInputTokens = Math.ceil(ascii / 4 + wide * 1.1)
  return { hasImage, estInputTokens }
}

function scopeMatch(asked, scope) {
  if (!Array.isArray(scope) || scope.length === 0) return true
  for (const entry of scope) {
    if (typeof entry !== 'string' || !entry) continue
    const re = /^\/(.+)\/([a-z]*)$/.exec(entry)
    if (re) {
      if (new RegExp(re[1], re[2].replace('g', '')).test(asked)) return true
    } else if (entry === asked) return true
  }
  return false
}

function ruleMatches(rule, info, requested) {
  if (!rule || typeof rule.target !== 'string' || !rule.target) return false
  if (rule.type === 'images') return info.hasImage
  if (rule.type === 'input') return Number.isFinite(rule.at) && rule.at > 0 && info.estInputTokens >= rule.at
  if (rule.type === 'output') return Number.isFinite(rule.at) && rule.at > 0 && requested != null && requested >= rule.at
  return false
}

function rulesFrom(options) {
  return options && Array.isArray(options.rules) ? options.rules.filter((r) => r && typeof r === 'object') : []
}

function firstRule(rules, info, requested, asked) {
  for (const rule of rules) {
    if (!ruleMatches(rule, info, requested)) continue
    // per-rule scope overrides the global one: lets different groups/models
    // route to different targets with separate rules.
    if (Array.isArray(rule.scope) && rule.scope.length && !scopeMatch(asked, rule.scope)) continue
    return rule
  }
  return null
}

function candidatesOf(rule) {
  const list = []
  if (rule && typeof rule.target === 'string' && rule.target) list.push(rule.target)
  if (rule && Array.isArray(rule.fallbacks)) {
    for (const t of rule.fallbacks) {
      if (typeof t === 'string' && t && !list.includes(t)) list.push(t)
    }
  }
  return list
}

// Learned image capability: 'capable' | 'incapable' | 'unknown'.
function imageHealth(modelId, learnedImages, now, ttl) {
  const entry = learnedImages ? learnedImages[modelId] : null
  if (!entry || entry.state == null) return 'unknown'
  if (entry.state === 'good') return 'capable'
  // via 'describe': the model was caught seeing images only through magpie's
  // describe fallback (the Image recognition pool) — natively vision-less.
  // That is a catalog-level property, not a transient failure, so the mark
  // outlives the TTL instead of decaying to 'unknown' and putting a blind
  // model ahead of candidates that at least might see. route-learn re-verifies
  // it weekly (its own image_bad_window does the same).
  if (entry.via === 'describe') return 'incapable'
  return (now - (entry.at || 0)) < ttl ? 'incapable' : 'unknown'
}

// Learned failure actions (see 0.3.0 note above).
function freshActions(options, now) {
  const list = options && options.learned && Array.isArray(options.learned.actions) ? options.learned.actions : []
  const out = []
  for (const action of list) {
    if (!action || typeof action.model !== 'string' || !action.model) continue
    const ttl = Number.isFinite(action.ttlMs) && action.ttlMs > 0 ? action.ttlMs : 600000
    if ((now - (action.at || 0)) >= ttl) continue
    out.push(action)
  }
  return out
}

function modelPaused(modelId, actions) {
  return actions.some((a) => a.model === modelId && a.kind === 'pause')
}

function learnedClampCap(modelId, actions) {
  let cap = null
  for (const a of actions) {
    if (a.model !== modelId || a.kind !== 'clampOutput') continue
    if (Number.isFinite(a.cap) && a.cap > 0) cap = cap == null ? a.cap : Math.min(cap, a.cap)
  }
  return cap
}

// Prefer learned-capable, then unknown (probe it), then incapable (last resort).
function pickImageTarget(candidates, learnedImages, now, ttl, payloadCaps, estTok) {
  // Payload 前置过滤（学习式）：已知拒收此输入量的候选先剔除；
  // 全部被剔除时回退原列表（fail-open，让上游自己说话）。
  let pool = candidates
  if (payloadCaps && estTok > 0) {
    const pass = candidates.filter((m) => {
      const pc = payloadCaps[m]
      return !(pc && Number.isFinite(pc.minFailTok) && estTok >= pc.minFailTok)
    })
    if (pass.length) pool = pass
  }
  const scored = pool.map((m) => ({ m, h: imageHealth(m, learnedImages, now, ttl) }))
  for (const tier of ['capable', 'unknown', 'incapable']) {
    for (const s of scored) {
      if (s.h === tier) return s.m
    }
  }
  return null
}

// Learned payload pre-filter: a candidate with 413-learned evidence it refuses
// inputs this size is skipped. Every rule's walk applies it since 0.3.48
// (images had it first); when all are filtered out the order stands
// (fail-open — let the upstream speak for itself).
function payloadPass(candidates, payloadCaps, estTok) {
  if (!payloadCaps || !estTok || estTok <= 0) return candidates
  const pass = candidates.filter((m) => {
    const pc = payloadCaps[m]
    return !(pc && Number.isFinite(pc.minFailTok) && estTok >= pc.minFailTok)
  })
  return pass.length ? pass : candidates
}

// Degradation migration (0.3.59): the first score-sorted perfTop entry that is
// not the asked model, not paused, and has learned capacity for this input.
// perfTop arrives from the companion (trusted metrics only, currently not
// paused at write time); pause is re-checked live here because actions can
// change between companion cycles.
function pickDegradationTarget(perfTop, asked, actions, payloadCaps, estTok) {
  for (const entry of perfTop) {
    const m = entry && entry.model
    if (!m || m === asked) continue
    if (modelPaused(m, actions)) continue
    const pc = payloadCaps ? payloadCaps[m] : null
    if (pc && Number.isFinite(pc.minFailTok) && estTok >= pc.minFailTok) continue
    return m
  }
  return null
}


// Rough input-token estimate (chars/2, images ≈ 1k) — only used to compare
// against learned 413 caps; a wrong estimate just falls through fail-open.
function estimateInputTokens(body) {
  let chars = 0
  for (const msg of (body.messages || [])) {
    const c = msg && msg.content
    if (typeof c === 'string') chars += c.length
    else if (Array.isArray(c)) {
      for (const part of c) {
        if (part && typeof part.text === 'string') chars += part.text.length
        else if (part && (part.image_url || part.image || part.input_image)) chars += 2000
      }
    }
  }
  return Math.ceil(chars / 2)
}

function clampTokens(body, keys, cap) {
  for (const k of keys) body[k] = cap
}

function askedName(body) {
  return typeof body.model === 'string' && body.model ? body.model : null
}

function restoreName(value, asked) {
  if (!asked || !value || typeof value !== 'object') return undefined
  // SSE message_start names the model on ev.message; the rest on ev.model /
  // ev.response.model / ev.modelVersion (Gemini).
  if (value.message && typeof value.message.model === 'string' && value.message.model !== asked) {
    value.message.model = asked
    return value
  }
  if (typeof value.model === 'string' && value.model !== asked) {
    value.model = asked
    return value
  }
  if (value.response && typeof value.response.model === 'string' && value.response.model !== asked) {
    value.response.model = asked
    return value
  }
  if (typeof value.modelVersion === 'string' && value.modelVersion !== asked) {
    value.modelVersion = asked
    return value
  }
  return undefined
}

export function onRequest(body, ctx) {
  try {
    if (!body || typeof body !== 'object' || Array.isArray(body)) return
    const asked = askedName(body)
    if (!asked) return
    const options = ctx && typeof ctx.options === 'object' && ctx.options ? ctx.options : {}
    if (!scopeMatch(asked, options.scope)) return
    const info = analyze(body)
    const keys = ['max_tokens', 'max_completion_tokens'].filter((k) => Number.isFinite(body[k]))
    const requested = keys.length ? Math.max.apply(null, keys.map((k) => body[k])) : null

    const rule = firstRule(rulesFrom(options), info, requested, asked)
    let changed = false
    if (rule) {
      const now = Date.now()
      const actions = freshActions(options, now)
      let candidates = candidatesOf(rule)
      // Circuit breaker: learned-paused models leave the walk; if everything
      // is paused the walk stays intact (fail-open, try the first again).
      const alive = candidates.filter((m) => !modelPaused(m, actions))
      if (alive.length) candidates = alive
      let target = null
      if (info.hasImage) {
        const ttl = Number.isFinite(options.learningTtlMs) && options.learningTtlMs > 0
          ? options.learningTtlMs : 21600000
        const learnedImages = options.learned && options.learned.images ? options.learned.images : {}
        const payloadCaps = (options.learned && options.learned.payloadCaps) || null
        target = pickImageTarget(candidates, learnedImages, now, ttl, payloadCaps, estimateInputTokens(body))
      } else if (candidates.length) {
        const payloadCaps = (options.learned && options.learned.payloadCaps) || null
        target = payloadPass(candidates, payloadCaps, estimateInputTokens(body))[0]
      }
      if (target && target !== asked) {
        ctx.state.asked = asked
        body.model = target
        changed = true
      }
      // Learned failure actions on the final model, on top of static limits.
      if (requested != null) {
        const learnedCap = learnedClampCap(target || asked, actions)
        if (learnedCap != null && requested > learnedCap) {
          clampTokens(body, keys, learnedCap)
          changed = true
        }
      }
    } else {
      // No rule matched — two learned, evidence-driven rewrites for plain
      // requests (both fail open, both mask the asked name in replies):
      //
      // 1) Degradation migration (0.3.59): the asked model carries a fresh
      //    learned pause (real upstream failure or the joint breaker) — a
      //    session pinned to it would just die. Migrate to the best healthy
      //    model from the companion's perfTop (score-sorted, trusted metrics
      //    only). Deterministic argmax = sticky while conditions hold, so the
      //    prefix cache survives the outage window; when the pause expires or
      //    a probe resumes the model, rewrites stop and traffic returns.
      //    Group ids never pause, so group requests keep magpie's own
      //    failover untouched.
      // 2) Payload rescue (0.3.48): the asked model has 413-learned evidence
      //    it refuses inputs this size — rewrite to the learned highest-
      //    capacity model instead of letting the same 413 repeat.
      const payloadCaps = (options.learned && options.learned.payloadCaps) || null
      const estTok = payloadCaps ? estimateInputTokens(body) : 0
      const actions = freshActions(options, Date.now())
      const perfTop = options.learned && Array.isArray(options.learned.perfTop) ? options.learned.perfTop : []
      let migrated = false
      if (modelPaused(asked, actions) && perfTop.length) {
        const target = pickDegradationTarget(perfTop, asked, actions, payloadCaps, estTok)
        if (target) {
          ctx.state.asked = asked
          body.model = target
          changed = true
          migrated = true
        }
      }
      if (!migrated) {
        const askedPc = payloadCaps ? payloadCaps[asked] : null
        if (askedPc && Number.isFinite(askedPc.minFailTok) && estTok >= askedPc.minFailTok) {
          let best = null
          let bestCap = -Infinity
          for (const m of Object.keys(payloadCaps)) {
            if (m === asked) continue
            const pc = payloadCaps[m]
            if (!pc || !Number.isFinite(pc.maxOkTok) || pc.maxOkTok < estTok) continue
            if (modelPaused(m, actions)) continue
            if (pc.maxOkTok > bestCap) { best = m; bestCap = pc.maxOkTok }
          }
          if (best && best !== asked) {
            ctx.state.asked = asked
            body.model = best
            changed = true
          }
        }
      }
    }

    if (options.clamp !== false && options.limits && typeof options.limits === 'object' && requested != null && requested > 0) {
      const finalModel = typeof body.model === 'string' ? body.model : asked
      const cap = options.limits[finalModel]
      if (Number.isFinite(cap) && cap > 0 && requested > cap) {
        clampTokens(body, keys, cap)
        changed = true
      }
    }

    if (!changed) return
    if (ctx.state.asked === undefined) ctx.state.asked = asked
    return body
  } catch {
    return undefined
  }
}

export function onEvent(ev, ctx) {
  try {
    if (!ctx || !ctx.state || !ctx.state.asked) return
    return restoreName(ev, ctx.state.asked)
  } catch {
    return undefined
  }
}

export function onResponse(body, ctx) {
  try {
    if (!ctx || !ctx.state || !ctx.state.asked) return
    if (ctx.status != null && ctx.status >= 400) return
    return restoreName(body, ctx.state.asked)
  } catch {
    return undefined
  }
}
