// 路由规则学习页（重设计版）：读 options.learnedView 渲染。
// 数据经 magpie bridge 获取，页面沙盒内无任何网络请求。
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]))
}

function el(tag, cls, text) {
  const e = document.createElement(tag)
  if (cls) e.className = cls
  if (text != null) e.textContent = text
  return e
}

function render(o) {
  const lv = (o && o.learnedView) || null
  document.getElementById('meta').textContent = lv
    ? '数据时间 ' + lv.generatedAt + ' · 伴生每 5 分钟刷新 · 本页每 30 秒重读'
    : '尚无 learnedView —— 请确认 tools/route-learn.py ≥ 0.3.15 且学习任务已运行'

  // ── 效果 KPI ──
  const impBox = document.getElementById('impact')
  impBox.textContent = ''
  const im = lv && lv.impact
  if (!im) {
    impBox.appendChild(el('p', 'empty', '统计中（伴生每小时重算，首轮运行后显示）'))
  } else {
    document.getElementById('impactSub').textContent = im.windowDays + ' 天滚动 · 排除伴生流量'
    const enp = im.estimatedNoPlugin
    const availDelta = enp && enp.availabilityPct != null && im.availability != null
      ? ['无插件预估 ' + enp.availabilityPct + '% ', Math.max(0, Math.round((im.availability - enp.availabilityPct) * 10) / 10)]
      : ['7 天 HTTP 成功率', null]
    const cards = [
      { cls: 'good', label: '可用度', value: im.availability != null ? im.availability : '—', unit: '%',
        delta: availDelta },
      { cls: 'warn', label: '切换保障', value: im.saves != null ? im.saves : '—', unit: ' 次',
        delta: ['占 ' + (im.switchGuaranteedPct != null ? im.switchGuaranteedPct + '%' : '—') + ' · 预算/流切换保障完整交付', null] },
      { cls: '', label: '请求总数', value: im.requests != null ? im.requests : '—', unit: ' (' + im.windowDays + 'd)',
        delta: ['失败 ' + Math.max(0, Math.round((im.requests || 0) * (1 - (im.availability || 100) / 100))) + ' 次', null] },
      { cls: '', label: '首字节中位', value: im.medianTtftS != null ? im.medianTtftS : '—', unit: 's',
        delta: ['总耗时中位 ' + (im.medianDurationS != null ? im.medianDurationS + 's' : '—'), null] },
    ]
    for (const c of cards) {
      const card = el('div', 'kpi ' + (c.cls || ''))
      card.appendChild(el('div', 'label', c.label))
      const v = el('div', 'value')
      v.textContent = String(c.value)
      if (c.unit) {
        const u = el('small', null, c.unit)
        u.textContent = ' ' + c.unit
        v.appendChild(u)
      }
      card.appendChild(v)
      if (c.delta) {
        const d = el('div', 'delta')
        if (c.delta[1] != null && c.delta[1] > 0) {
          d.append(c.delta[0] + ' ')
          const b = el('b', null, '+' + c.delta[1])
          d.appendChild(b)
        } else {
          d.textContent = c.delta[0]
        }
        card.appendChild(d)
      }
      impBox.appendChild(card)
    }

    // 对比区：装前历史（真实）+ 无插件预估，统一为一行式对比条
    const cmp = el('div', 'cmp')
    const ba = im.beforeAfter
    if (ba) {
      cmp.appendChild(el('div', 'cmp-title', '装插件前后（真实历史 · 边界 ' + ba.boundary + ' 自动检测）'))
      const r1 = el('div', 'cmp-row')
      r1.append(el('span', 'cmp-k', '错误率'), el('span', 'cmp-v', ba.pre.errorRatePct + '% → ' + ba.post.errorRatePct + '%'))
      cmp.appendChild(r1)
      const r2 = el('div', 'cmp-row')
      r2.append(el('span', 'cmp-k', '空回复率'), el('span', 'cmp-v', ba.pre.emptyRatePct + '% → ' + ba.post.emptyRatePct + '%'))
      cmp.appendChild(r2)
      const rc = el('div', 'cmp-cap', '装前 ' + ba.pre.requests + ' · 装后 ' + ba.post.requests + ' 请求')
      cmp.appendChild(rc)
    }
    if (enp) {
      cmp.appendChild(el('div', 'cmp-title', '若现在移除插件（预估 · 当前预算与路由不变）'))
      const r3 = el('div', 'cmp-row')
      r3.append(el('span', 'cmp-k', '可用度'), el('span', 'cmp-v', enp.availabilityPct + '%（' + enp.wouldFail + ' 个切换请求预计掐流/超限失败）'))
      cmp.appendChild(r3)
      cmp.appendChild(el('div', 'cmp-cap', '依据：预算超上限→推理烧尽→掐流（87db83cd 实证）；装前健康是 4000 预算时代，条件不同'))
    }
    if (cmp.childNodes.length) impBox.appendChild(cmp)
  }

  // ── 模型优先级 ──
  const perf = document.getElementById('perf')
  perf.textContent = ''
  const rows = (lv && lv.perfSummary) || []
  if (!rows.length) {
    perf.appendChild(el('p', 'empty', '暂无采样（伴生扫描尚未积累）'))
  }
  const maxScore = Math.max(1, ...rows.map((e) => e.score || 0))
  rows.forEach((e, i) => {
    const m = el('div', 'model' + (i === 0 ? ' first' : ''))
    m.appendChild(el('div', 'rank', String(i + 1)))
    const nameBox = el('div', 'name')
    nameBox.appendChild(el('span', 'nm', e.model))
    if (e.degraded) {
      nameBox.appendChild(el('span', 'deg', '⚠ ' + e.degraded + ' 降级'))
    }
    m.appendChild(nameBox)
    const bar = el('div', 'bar')
    const fill = el('i')
    fill.style.width = Math.round(100 * (e.score || 0) / maxScore) + '%'
    bar.appendChild(fill)
    m.appendChild(bar)
    const s1 = el('div', 'stat')
    s1.appendChild(el('b', null, e.score != null ? e.score : '—'))
    s1.appendChild(el('span', null, '评分'))
    m.appendChild(s1)
    const s1b = el('div', 'stat')
    s1b.appendChild(el('b', null, e.tps != null ? e.tps : '—'))
    s1b.appendChild(el('span', null, 'tok/s'))
    m.appendChild(s1b)
    const s2 = el('div', 'stat')
    s2.appendChild(el('b', null, e.ttfS != null ? e.ttfS + 's' : (e.ttfMs != null ? (Math.round(e.ttfMs / 100) / 10) + 's' : '—')))
    s2.appendChild(el('span', null, '首字节'))
    m.appendChild(s2)
    const s3 = el('div', 'stat')
    s3.appendChild(el('b', null, String(e.samples ?? 0)))
    s3.appendChild(el('span', null, '采样'))
    m.appendChild(s3)
    perf.appendChild(m)
  })

  // ── 洞察 ──
  const ins = document.getElementById('insights')
  ins.textContent = ''
  const list = (lv && lv.insights) || []
  if (!list.length) {
    ins.appendChild(el('p', 'empty', '当前状态健康，无待关注事项'))
  }
  for (const i of list) {
    const d = el('div', 'insight ' + esc(i.level))
    const lv2 = el('span', 'lv', i.level)
    d.appendChild(lv2)
    d.appendChild(document.createTextNode(i.text))
    ins.appendChild(d)
  }

  // ── 图片能力 ──
  const imgs = document.getElementById('images')
  imgs.textContent = ''
  const iv = (lv && lv.imagesView) || {}
  const ikeys = Object.keys(iv)
  if (!ikeys.length) {
    imgs.appendChild(el('p', 'empty', '无标记（候选按未知能力对待，探针会主动确认）'))
  }
  for (const k of ikeys) {
    const r = el('div', 'row')
    const st = iv[k].state
    r.appendChild(el('span', 'dot ' + (st === 'good' ? 'ok' : st === 'bad' ? 'bad' : 'none')))
    r.appendChild(el('span', 'nm', k))
    r.appendChild(el('span', 'tag', st === 'good' ? '可用' : st === 'bad' ? '不可用' : String(st)))
    r.appendChild(el('span', 'ago', iv[k].ageMin + ' 分钟前'))
    imgs.appendChild(r)
  }

  // ── 动作 ──
  const acts = document.getElementById('actions')
  acts.textContent = ''
  const av = (lv && lv.actionsView) || []
  if (!av.length) {
    acts.appendChild(el('p', 'empty', '无执行中动作'))
  }
  for (const a of av) {
    const r = el('div', 'row')
    r.appendChild(el('span', 'dot ' + (a.kind === 'pause' ? 'bad' : 'warn')))
    r.appendChild(el('span', 'nm', a.model))
    r.appendChild(el('span', 'tag', a.kind))
    r.appendChild(el('span', 'ago', '剩 ' + a.leftMin + ' 分钟 · ' + (a.by || '')))
    acts.appendChild(r)
  }

  // ── AI 反思 ──
  const rf = document.getElementById('reflect')
  rf.textContent = ''
  const lrc = lv && lv.lastReflect
  if (!lrc) {
    rf.appendChild(el('p', 'empty', '尚未运行（自动每日一次，或手动 tools/route-learn.py --reflect）'))
  } else {
    const box = el('div', 'reflect')
    const head = el('div', 'head')
    const left = el('span', null, lrc.model || '')
    const right = el('span', null, lrc.at ? new Date(lrc.at).toLocaleString() : '')
    head.append(left, right)
    box.appendChild(head)
    box.appendChild(el('pre', null, lrc.text || ''))
    rf.appendChild(box)
  }
}

async function main() {
  await magpie.ready
  const paint = async () => {
    try { render(await magpie.options.get()) } catch { /* keep last paint */ }
  }
  await paint()
  setInterval(paint, 30000)
}

magpie.ready.then(main)
