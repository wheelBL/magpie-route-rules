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
    const enp = im.estimatedNoPlugin
    const ba = im.beforeAfter
    // 副标题承载分母性质信息（请求量/边界）；效果区只留有优势故事的卡
    const subParts = [im.windowDays + ' 天滚动 · 排除伴生流量']
    if (im.requests != null) subParts.push(im.requests + ' 请求')
    if (ba && ba.boundary) subParts.push('装插件边界 ' + ba.boundary + '（自动检测）')
    document.getElementById('impactSub').textContent = subParts.join(' · ')
    // 每张卡自带前后故事（delta 行）；卡网格即全部——无外挂对比组件
    const cards = [
      { cls: 'good', label: '可用度', value: im.availability != null ? im.availability : '—', unit: '%',
        delta: enp && enp.availabilityPct != null && im.availability != null
          ? { text: '若移除预估 ' + enp.availabilityPct + '%', gain: Math.max(0, Math.round((im.availability - enp.availabilityPct) * 10) / 10) }
          : { text: '7 天 HTTP 成功率' } },
      { cls: 'warn', label: '切换保障', value: im.saves != null ? im.saves : '—', unit: ' 次',
        delta: { text: '占 ' + (im.switchGuaranteedPct != null ? im.switchGuaranteedPct + '%' : '—') + ' · 透明切换保障交付' } },
      { cls: ba && ba.post.errorRatePct != null ? 'good' : '', label: '错误率',
        value: ba ? ba.post.errorRatePct : '—', unit: '%',
        delta: ba ? { text: '装前 ' + ba.pre.errorRatePct + '%' } : { text: '—' } },
      { cls: ba ? 'warn' : '', label: '空回复率',
        value: ba ? ba.post.emptyRatePct : '—', unit: '%',
        delta: ba ? { text: '装前 ' + ba.pre.emptyRatePct + '% · 口径待拆解' } : { text: '—' } },
    ]
    for (const c of cards) {
      const card = el('div', 'kpi ' + (c.cls || ''))
      card.appendChild(el('div', 'label', c.label))
      const v = el('div', 'value')
      v.textContent = String(c.value)
      if (c.unit) v.appendChild(document.createTextNode(' ' + c.unit))
      card.appendChild(v)
      if (c.delta) {
        const d = el('div', 'delta')
        d.textContent = c.delta.text
        if (c.delta.gain != null && c.delta.gain > 0) {
          d.appendChild(document.createTextNode(' '))
          const b = el('b', null, '+' + c.delta.gain)
          d.appendChild(b)
        }
        card.appendChild(d)
      }
      impBox.appendChild(card)
    }
    // 依据注脚（优势的因果说明）；请求量/边界已在副标题，时延中位归模型表
    if (enp) {
      impBox.appendChild(el('div', 'stat-note',
        '若移除依据：切换请求预算超模型上限 → 推理烧尽 → 掐流（87db83cd 实证：重试 5 次全败）'))
    }
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
