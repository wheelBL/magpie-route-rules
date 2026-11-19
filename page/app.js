// 路由规则学习全景页：读插件自身 options 的 learnedView（伴生每 5 分钟刷新），
// 每 30 秒自动重读。全部数据来自 options.learned / options.learnedView，
// 不发任何网络请求（页面沙盒 connect-src 'none'）。
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]))
}

function render(o) {
  const lv = (o && o.learnedView) || null
  const meta = document.getElementById('meta')
  meta.textContent = lv
    ? '数据时间 ' + esc(lv.generatedAt) + ' · 伴生 route-learn 每 5 分钟刷新 · 本页每 30 秒重读'
    : '尚无 learnedView —— 请确认 tools/route-learn.py ≥ 0.3.15 且学习计划任务已运行过一轮'

  // 插件效果
  const imp = document.getElementById('impact')
  imp.textContent = ''
  const im = lv && lv.impact
  if (!im) {
    const p = document.createElement('p')
    p.className = 'muted'
    p.textContent = '统计中（伴生每小时重算 7 天窗口，首轮运行后显示）'
    imp.appendChild(p)
  } else {
    const big = document.createElement('div')
    const items = [
      ['可用度', im.availability + '%', '估计无插件 ' + (im.availabilityNoPlugin != null ? im.availabilityNoPlugin + '%' : '—')],
      ['自动挽救', im.saves + ' 次', '透明切换后成功（预算/流/链）'],
      ['请求总数', im.requests + ' (' + im.windowDays + '天)', '排除伴生自身流量'],
      ['首字节中位', im.medianTtftS != null ? im.medianTtftS + 's' : '—', '耗时中位 ' + (im.medianDurationS != null ? im.medianDurationS + 's' : '—')],
    ]
    for (const [k, v, sub] of items) {
      const b = document.createElement('div')
      b.style.display = 'inline-block'
      b.style.margin = '8px 18px 8px 0'
      b.style.verticalAlign = 'top'
      const t = document.createElement('div')
      t.className = 'muted'
      t.style.fontSize = '12px'
      t.textContent = k
      const n = document.createElement('div')
      n.style.fontSize = '22px'
      n.style.fontWeight = '600'
      n.textContent = v
      const s = document.createElement('div')
      s.className = 'muted'
      s.style.fontSize = '11px'
      s.textContent = sub
      b.append(t, n, s)
      big.appendChild(b)
    }
    imp.appendChild(big)
    const detail = im.savesDetail || {}
    const keys = Object.keys(detail)
    if (keys.length) {
      const p = document.createElement('p')
      p.className = 'muted'
      p.textContent = '切换明细：' + keys.map((k) => k + ' ×' + detail[k]).join('，')
      imp.appendChild(p)
    }
  }

  // 性能画像
  const tbody = document.querySelector('#perf')
  tbody.textContent = ''
  const rows = (lv && lv.perfSummary) || []
  if (!rows.length) {
    const tr = document.createElement('tr')
    const td = document.createElement('td')
    td.colSpan = 6
    td.className = 'muted'
    td.textContent = '暂无采样（伴生扫描尚未积累）'
    tr.appendChild(td)
    tbody.appendChild(tr)
  }
  for (const e of rows) {
    const tr = document.createElement('tr')
    const ttfS = e.ttfS != null ? e.ttfS : (e.ttfMs != null ? Math.round(e.ttfMs / 100) / 10 : null)
    const cells = [
      [esc(e.model) + (e.degraded ? ' ⚠ ' + esc(e.degraded) + ' 降级' : ''), ''],
      [e.score != null ? e.score : '采样不足', 'num'],
      [e.tps != null ? e.tps : '—', 'num'],
      [ttfS != null ? ttfS + 's' : '—', 'num'],
      [e.samples != null ? e.samples : 0, 'num'],
      [e.consecFails != null ? e.consecFails : 0, 'num'],
    ]
    for (const [text, cls] of cells) {
      const td = document.createElement('td')
      if (cls) td.className = cls
      td.textContent = text
      tr.appendChild(td)
    }
    tbody.appendChild(tr)
  }

  // 反思洞察
  const ins = document.getElementById('insights')
  ins.textContent = ''
  const list = (lv && lv.insights) || []
  if (!list.length) {
    const p = document.createElement('p')
    p.className = 'muted'
    p.textContent = '当前状态健康，无待关注事项'
    ins.appendChild(p)
  }
  for (const i of list) {
    const p = document.createElement('p')
    const badge = document.createElement('span')
    badge.className = 'badge ' + esc(i.level)
    badge.textContent = i.level
    p.appendChild(badge)
    p.appendChild(document.createTextNode(i.text))
    ins.appendChild(p)
  }

  // 图片能力
  const imgs = document.getElementById('images')
  imgs.textContent = ''
  const iv = (lv && lv.imagesView) || {}
  const keys = Object.keys(iv)
  if (!keys.length) {
    const p = document.createElement('p')
    p.className = 'muted'
    p.textContent = '无标记（候选按未知能力对待，允许试探；探针会主动确认）'
    imgs.appendChild(p)
  }
  for (const m of keys) {
    const p = document.createElement('p')
    const name = document.createElement('span')
    name.textContent = m + ': '
    const st = document.createElement('span')
    const state = iv[m].state
    st.className = state === 'good' ? 'good' : state === 'bad' ? 'bad' : ''
    st.textContent = state === 'good' ? '可用' : state === 'bad' ? '不可用' : String(state)
    const age = document.createElement('span')
    age.className = 'muted'
    age.textContent = '（' + iv[m].ageMin + ' 分钟前标记）'
    p.append(name, st, age)
    imgs.appendChild(p)
  }

  // 动作
  const acts = document.getElementById('actions')
  acts.textContent = ''
  const av = (lv && lv.actionsView) || []
  if (!av.length) {
    const p = document.createElement('p')
    p.className = 'muted'
    p.textContent = '无执行中动作'
    acts.appendChild(p)
  }
  for (const a of av) {
    const p = document.createElement('p')
    p.textContent = a.model + ': ' + a.kind + '（剩 ' + a.leftMin + ' 分钟，by=' + (a.by || '') + '）'
    acts.appendChild(p)
  }

  // 深度反思
  const rf = document.getElementById('reflect')
  rf.textContent = ''
  const lrc = lv && lv.lastReflect
  if (!lrc) {
    const p = document.createElement('p')
    p.className = 'muted'
    p.textContent = '尚未运行 AI 深度反思（tools/route-learn.py --reflect）'
    rf.appendChild(p)
  } else {
    const m = document.createElement('p')
    m.className = 'meta'
    m.textContent = (lrc.model || '') + ' · ' + (lrc.at ? new Date(lrc.at).toLocaleString() : '')
    const pre = document.createElement('pre')
    pre.textContent = lrc.text || ''
    rf.append(m, pre)
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
