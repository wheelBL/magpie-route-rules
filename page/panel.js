// 用量页面板：模型优先级 top3 + 待关注洞察数（数据同 learnedView）
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]))
}

async function main() {
  await magpie.ready
  const paint = async () => {
    let o = null
    try { o = await magpie.options.get() } catch { return }
    const lv = (o && o.learnedView) || null
    const tbody = document.querySelector('#perf')
    tbody.textContent = ''
    const rows = ((lv && lv.perfSummary) || []).slice(0, 3)
    if (!rows.length) {
      const tr = document.createElement('tr')
      const td = document.createElement('td')
      td.className = 'muted'
      td.textContent = '暂无采样'
      tr.appendChild(td)
      tbody.appendChild(tr)
    }
    for (const e of rows) {
      const tr = document.createElement('tr')
      const name = document.createElement('td')
      name.textContent = e.model + (e.degraded ? ' ⚠' : '')
      const score = document.createElement('td')
      score.className = 'num'
      score.textContent = e.score != null ? e.score + ' 分 · ' + (e.tps != null ? e.tps + ' tok/s' : '') : '采样不足'
      tr.append(name, score)
      tbody.appendChild(tr)
    }
    const ins = ((lv && lv.insights) || []).filter((i) => i.level === 'warn')
    const meta = document.getElementById('meta')
    meta.textContent = lv
      ? (ins.length ? ins.length + ' 条待关注警告 · ' : '状态健康 · ') + '更新于 ' + esc(lv.generatedAt)
      : 'route-learn ≥ 0.3.15 运行一轮后显示'
  }
  await paint()
  setInterval(paint, 30000)
}

magpie.ready.then(main)
