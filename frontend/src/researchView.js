export const METRICS = {
  latest_date: { label: '最近贸易日期', reason: '最近贸易日期最晚' },
  buyer_count: { label: '已知买方数', reason: '已知买方数最多' },
  active_months: { label: '活跃月份数', reason: '活跃月份数最多' },
  record_count: { label: '匹配记录数', reason: '匹配记录数最多' },
}

export function compareCandidates(report, ids, metric) {
  if (!METRICS[metric] || ids.length < 2 || ids.length > 5 || new Set(ids.map(String)).size !== ids.length) throw new Error('请选择 2—5 家不同候选及有效指标')
  const rows = ids.map(id => report.candidates.find(candidate => String(candidate.supplier_id) === String(id)))
  if (rows.some(candidate => !candidate)) throw new Error('只能比较本次调查中的候选')
  const values = rows.map(row => row[metric])
  const available = metric === 'latest_date'
    ? values.every(value => /^\d{4}-\d{2}-\d{2}$/.test(value || ''))
    : values.every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)
  if (!available) return { rows, winnerIds: [], reason: '该指标有缺失，无法据此比较', label: METRICS[metric].label }
  if (metric === 'buyer_count' && values.every(value => value === 0)) return { rows, winnerIds: [], reason: '缺少可比较的买方标识，不能认定没有买方', label: METRICS[metric].label }
  const best = values.reduce((a, b) => a > b ? a : b)
  const winnerIds = rows.filter(row => row[metric] === best).map(row => String(row.supplier_id))
  return { rows, winnerIds, label: METRICS[metric].label, reason: `${METRICS[metric].reason}${winnerIds.length > 1 ? '（并列）' : ''}；仅比较本次所选候选` }
}

export function candidateDetail(report, id) {
  const candidate = report?.candidates?.find(row => String(row.supplier_id) === String(id))
  if (!candidate) return null
  return {
    candidate,
    profile: report.candidate_profiles?.find(row => String(row.supplier_id) === String(id)) || null,
    evidence: (report.evidence || []).filter(row => String(row.source.supplier_id) === String(id)),
  }
}

export function inclusionReasons(report, candidate) {
  const req = report.requirements || {}
  const reasons = []
  if (req.hs_prefix) reasons.push(`符合 HS ${req.hs_prefix} 前缀条件`)
  if (req.product_text) reasons.push(`符合商品原文“${req.product_text}”条件`)
  if (req.market_country) reasons.push(`符合目的国 ${req.market_country} 条件`)
  reasons.push(`查询期间内保存 ${candidate.record_count} 条匹配记录`)
  reasons.push(`最近贸易日期 ${candidate.latest_date}`)
  return reasons
}

export function reportForm(report) {
  const req = report.requirements || {}
  return { hs: req.hs_prefix || '', product: req.product_text || '', start: req.start_date || '', end: req.end_date || '', market: req.market_country || '', limit: req.limit || 5, ranking: req.ranking || 'recent', engine: report.engine || 'native', scanLimit: report.scan_limit ?? 200000, maxRecords: report.max_records ?? 5000 }
}

export function querySignature(form) {
  return JSON.stringify(['hs', 'product', 'start', 'end', 'market', 'limit', 'ranking', 'engine', 'scanLimit', 'maxRecords'].map(key => typeof form[key] === 'string' ? form[key].trim() : form[key]))
}

// Hand-written demo vocabulary; selecting a term edits the draft, never auto-submits or changes HS.
const TERMS = [
  { terms: ['电缆', '线缆'], text: 'cable', label: '电缆 → cable（西语）' },
  { terms: ['电线', '导线'], text: 'conductor', label: '导线 → conductor（西语）' },
  { terms: ['塑料'], text: 'plástico', label: '塑料 → plástico（西语）' },
  { terms: ['汽车零件', '汽车配件'], text: 'partes automotrices', label: '汽车零件 → partes automotrices（西语）' },
]
export function suggestedTerms(text) { return TERMS.filter(entry => entry.terms.some(term => (text || '').includes(term))) }

export function formatTime(value) {
  const date = new Date(value)
  return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date) : '暂无信息'
}
