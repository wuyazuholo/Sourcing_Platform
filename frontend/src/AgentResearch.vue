<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import CandidateDetails from './CandidateDetails.vue'
import CandidateComparison from './CandidateComparison.vue'
import { candidateDetail, compareCandidates, formatTime, inclusionReasons, querySignature, reportForm, suggestedTerms } from './researchView.js'
import { request } from './api.js'
import { currentPage, navigate, pageState, registerPageState } from './navigation.js'

const emit = defineEmits(['session-expired'])
const defaults = () => ({ hs: '8544', product: '', start: '2025-01-01', end: '2025-12-31', market: '', limit: 5, ranking: 'recent', engine: 'native', scanLimit: 200000, maxRecords: 5000 })
const form = ref(defaults()), job = ref(null), busy = ref(false), error = ref(''), service = ref(null)
const report = computed(() => job.value?.result)
const selected = ref([]), hidden = ref([]), activeId = ref(null), metric = ref('latest_date'), queryForm = ref(null)
const visibleCandidates = computed(() => (report.value?.candidates || []).filter(row => !hidden.value.includes(String(row.supplier_id))))
const detail = computed(() => candidateDetail(report.value, activeId.value))
const comparison = computed(() => selected.value.length >= 2 ? compareCandidates(report.value, selected.value, metric.value) : null)
const suggestions = computed(() => suggestedTerms(form.value.product))
const edited = computed(() => report.value && querySignature(form.value) !== querySignature(reportForm(report.value)))
watch(() => job.value?.id, () => { selected.value = []; hidden.value = []; activeId.value = null }, { flush: 'sync' })
const pending = computed(() => ['queued', 'running'].includes(job.value?.status))
const statusText = computed(() => ({ queued: '等待查询', running: '正在查询并生成证据', completed: '调查完成', failed: '调查未完成' }[job.value?.status] || ''))
let timer, sequence = 0, disposed = false
const capture = () => ({ form: form.value, job: job.value, selected: selected.value, hidden: hidden.value, metric: metric.value })
function hideCandidate(id) { hidden.value = [...hidden.value, String(id)]; selected.value = selected.value.filter(value => value !== String(id)); if (activeId.value === String(id)) activeId.value = null }
function toggleCandidate(id) { const value = String(id); selected.value = selected.value.includes(value) ? selected.value.filter(item => item !== value) : selected.value.length < 5 ? [...selected.value, value] : selected.value }
function adjustQuery(clear) { if (clear) form.value[clear] = ''; else form.value = reportForm(report.value); queryForm.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
function handleError(caught) {
  error.value = caught.message
  if ([401, 403].includes(caught.status)) emit('session-expired', caught.message)
}
async function checkService() {
  try { const value = await request('/agent/status'); if (!disposed) service.value = value }
  catch (caught) { if (!disposed) handleError(caught) }
}
async function poll(id, ticket) {
  try {
    const value = await request(`/agent/searches/${id}`)
    if (disposed || ticket !== sequence) return
    job.value = value
    error.value = ''
    if (['queued', 'running'].includes(value.status)) timer = setTimeout(() => poll(id, ticket), 1500)
  } catch (caught) { if (!disposed && ticket === sequence) handleError(caught) }
}
function retry() {
  if (job.value?.id) { clearTimeout(timer); error.value = ''; poll(job.value.id, ++sequence) }
  else checkService()
}
function restore(saved, route) {
  if (route.screen !== 'agent') return
  sequence++; clearTimeout(timer); error.value = ''; busy.value = false
  form.value = saved?.form || defaults()
  job.value = saved?.job || (route.job ? { id: route.job, status: 'queued' } : null)
  if (route.job && job.value?.id !== route.job) job.value = { id: route.job, status: 'queued' }
  const known = new Set((job.value?.result?.candidates || []).map(row => String(row.supplier_id)))
  hidden.value = (saved?.hidden || []).filter(id => known.has(id))
  selected.value = (saved?.selected || []).filter(id => known.has(id) && !hidden.value.includes(id)).slice(0, 5)
  metric.value = ['latest_date', 'buyer_count', 'active_months', 'record_count'].includes(saved?.metric) ? saved.metric : 'latest_date'
  if (job.value?.id && (!job.value.result || pending.value)) poll(job.value.id, sequence)
}
const unregister = registerPageState('agent', capture, restore)
async function submit() {
  if (busy.value || pending.value) return
  error.value = ''
  if (!form.value.hs.trim() && !form.value.product.trim()) { error.value = '请输入 HS 编码或商品描述'; return }
  if (form.value.start > form.value.end) { error.value = '开始日期不能晚于结束日期'; return }
  busy.value = true
  const ticket = ++sequence
  try {
    const body = { ...form.value, hs: form.value.hs.trim() || null, product: form.value.product.trim() || null, market: form.value.market.trim() || null }
    const value = await request('/agent/searches', { method: 'POST', body })
    if (disposed || ticket !== sequence) return
    job.value = value
    navigate({ screen: 'agent', job: value.id }, { replace: true, saved: true, skip: ['agent'] })
    poll(value.id, ticket)
  } catch (caught) { if (!disposed && ticket === sequence) handleError(caught) }
  finally { if (!disposed && ticket === sequence) busy.value = false }
}
onMounted(() => { restore(pageState('agent'), currentPage.value.route); checkService() })
onBeforeUnmount(() => { disposed = true; sequence++; clearTimeout(timer); unregister() })
</script>

<template>
  <section class="agent-research" aria-label="Agent 供应商调查">
    <div class="agent-intro">
      <div><h2>从真实交易中发现供应商</h2><p>使用墨西哥出口记录筛选候选，生成可追溯的交易证据报告。</p></div>
      <span class="tag" :class="service?.ready ? 'green' : ''">{{ service?.ready ? '调查服务已连接' : service?.message || '正在连接调查服务' }}</span>
    </div>
    <form ref="queryForm" class="agent-form" @submit.prevent="submit">
      <div class="agent-fields">
        <label>HS 编码<input v-model="form.hs" name="hs" placeholder="例如 8544" pattern="[0-9]{2,12}" maxlength="12"></label>
        <label>商品描述<input v-model="form.product" name="product" placeholder="可选，按源语言匹配原文" minlength="2" maxlength="160"></label>
        <label>开始日期<input v-model="form.start" name="start" type="date" required></label>
        <label>结束日期<input v-model="form.end" name="end" type="date" required></label>
        <label>目的国<input v-model="form.market" name="market" placeholder="可选，源库英文名称" minlength="2" maxlength="100"></label>
        <label>候选数量<input v-model.number="form.limit" name="limit" type="number" min="1" max="10" required></label>
        <label>候选排序<select v-model="form.ranking"><option value="recent">最近贸易日期</option><option value="customer_coverage">已知买方数</option><option value="activity">活跃月份数</option></select></label>
        <label>检索范围<select v-model.number="form.scanLimit"><option :value="50000">最近 5 万条源记录</option><option :value="200000">最近 20 万条源记录</option><option :value="2000000">最近 200 万条源记录</option><option :value="0">全历史条件查询（可能超时）</option></select></label>
      </div>
      <div v-if="suggestions.length" class="agent-suggestions"><p>中文不会自动翻译。可选择演示词表中的西语原文建议；仅填入商品描述，HS 和其他条件由你确认。</p><button v-for="suggestion in suggestions" :key="suggestion.text" type="button" @click="form.product = suggestion.text">{{ suggestion.label }}</button></div>
      <details class="agent-options"><summary>高级选项</summary><div class="agent-fields"><label>保存匹配记录上限<input v-model.number="form.maxRecords" type="number" min="1" max="50000" required></label><label>执行引擎<select v-model="form.engine"><option value="native">原生 Agent</option><option value="langgraph">LangGraph</option></select></label></div></details>
      <p class="agent-scope">数据范围为墨西哥出口表。{{ form.scanLimit ? '先按记录编号取所选窗口，再按条件筛选；不覆盖全表或完整市场。' : '请求全历史条件查询，仍受超时和保存上限约束。' }}贸易日期与货值按源字段展示；当前供货能力尚未核验。</p>
      <button type="submit" class="agent-primary" :disabled="busy || pending || service?.ready === false">{{ busy ? '正在提交…' : pending ? '调查进行中…' : '开始调查' }}</button>
      <p v-if="edited" class="agent-draft-note" role="status">上方条件已修改，当前仍显示上一次调查结果。点击“开始调查”后更新结果。</p>
    </form>

    <div v-if="error" role="alert" class="agent-error">{{ error }} <button type="button" @click="retry">重新连接</button></div>
    <div v-if="job" class="agent-progress" role="status" aria-live="polite"><strong>{{ statusText }}</strong><span v-if="pending">可以离开本页，返回后会继续获取结果。</span><span v-else-if="job.status === 'failed'">{{ job.error }}</span><span v-else>查询时间：{{ formatTime(job.updated_at) }}</span></div>

    <template v-if="report">
      <div class="agent-result-heading"><h2>调查结果</h2><span>{{ report.record_count }} 条已保存匹配 · {{ report.candidates.length }} 家候选</span></div>
      <div class="agent-applied"><strong>本次已提交条件</strong><span>HS：{{ report.requirements?.hs_prefix || '未限定' }}</span><span>商品：{{ report.requirements?.product_text || '未限定' }}</span><span>{{ report.requirements?.start_date }} — {{ report.requirements?.end_date }}</span><span>目的国：{{ report.requirements?.market_country || '未限定' }}</span><button type="button" @click="adjustQuery()">恢复本次条件</button></div>
      <p v-if="report.result_truncated" class="agent-warning" role="alert">匹配结果超过保存上限。排名和统计仅覆盖已保存部分，请缩小查询条件。</p>
      <p class="agent-scope">{{ report.full_history ? '全历史条件查询' : `最近至多 ${report.scan_limit.toLocaleString()} 条源记录内筛选` }} · 来源：{{ report.table }} · {{ formatTime(report.extracted_at) }}</p>
      <div v-if="!report.candidates.length" class="agent-empty"><p>在本次声明的检索范围内未找到匹配候选，不能据此认定源库没有相关企业。</p><p>可检查 HS、源语言商品描述和日期是否合适；调整条件后再点击“开始调查”。</p><button type="button" @click="adjustQuery()">修改本次条件</button><button v-if="report.requirements?.market_country" type="button" @click="adjustQuery('market')">清除目的国条件</button><button v-if="report.requirements?.product_text && report.requirements?.hs_prefix" type="button" @click="adjustQuery('product')">移除商品描述条件</button></div>
      <template v-else>
        <div class="agent-selection"><span>已选 {{ selected.length }} 家（选择 2—5 家对比）</span><button v-if="selected.length" type="button" @click="selected = []">清空选择</button><button v-if="hidden.length" type="button" @click="hidden = []">还原 {{ hidden.length }} 家暂不考虑的候选</button></div>
        <CandidateComparison v-if="comparison" v-model:metric="metric" :comparison="comparison" :truncated="report.result_truncated" />
        <p v-if="!visibleCandidates.length" class="agent-empty">本次候选均被标记为“暂不考虑”。点击“还原”可继续查看，原调查报告仍保留。</p>
        <div class="agent-candidates"><article v-for="candidate in visibleCandidates" :key="candidate.supplier_id" class="agent-candidate"><label class="agent-pick"><input type="checkbox" :checked="selected.includes(String(candidate.supplier_id))" :disabled="!selected.includes(String(candidate.supplier_id)) && selected.length >= 5" :aria-label="`选择 ${candidate.supplier_name} 进行对比`" @change="toggleCandidate(candidate.supplier_id)">加入对比</label><h3>{{ candidate.supplier_name }}</h3><p>{{ candidate.supplier_country || '国家未记录' }}</p><ul class="agent-reasons"><li v-for="reason in inclusionReasons(report, candidate).slice(0, 2)" :key="reason">{{ reason }}</li></ul><dl><div><dt>匹配记录</dt><dd>{{ candidate.record_count }}</dd></div><div><dt>已知买方</dt><dd>{{ candidate.buyer_count }}</dd></div><div><dt>活跃月份</dt><dd>{{ candidate.active_months }}</dd></div><div><dt>最近贸易日期</dt><dd>{{ candidate.latest_date }}</dd></div></dl><div class="agent-actions"><button type="button" @click="activeId = String(candidate.supplier_id)">查看详情</button><button type="button" @click="hideCandidate(candidate.supplier_id)">暂不考虑</button></div></article></div>
      </template>
      <CandidateDetails v-if="detail" :detail="detail" :reasons="inclusionReasons(report, detail.candidate)" :truncated="report.result_truncated" @close="activeId = null" />
      <details class="agent-report"><summary>完整调查报告与引用</summary><pre>{{ report.text }}</pre></details>
      <details class="agent-report"><summary>查看原始交易证据（{{ report.evidence.length }}）</summary><article v-for="evidence in report.evidence" :key="evidence.id" class="agent-evidence"><strong>{{ evidence.title }}</strong><p>{{ evidence.text }}</p><p class="agent-scope">{{ evidence.source.source_table }} · 记录 {{ evidence.source.record_id }} · 快照 {{ evidence.source.snapshot_sha256 }}</p></article></details>
      <p class="agent-scope">本次数据检索与报告采用本地确定性流程，不调用外部模型服务。快照 SHA-256：<code>{{ report.snapshot_sha256 }}</code></p>
    </template>
    <div v-else-if="!job" class="agent-empty">填写条件后开始调查。报告会保留检索范围、候选统计和原始交易引用。</div>
  </section>
</template>

<style scoped>
.agent-research { max-width: 1260px; margin: 0 auto; }
.agent-intro,.agent-result-heading { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:20px; }
.agent-intro h2,.agent-result-heading h2 { margin:0 0 8px; font-size:22px; }
.agent-intro p,.agent-candidate p { color:#64748b; margin:0; }
.agent-form { background:white; border:1px solid #e2e8f0; border-radius:14px; padding:24px; }
.agent-fields { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:18px; }
.agent-fields label { display:flex; flex-direction:column; gap:8px; font-size:13px; color:#334155; }
.agent-fields input,.agent-fields select { width:100%; box-sizing:border-box; min-height:42px; border:1px solid #cbd5e1; border-radius:7px; padding:9px; background:white; color:#0f172a; font:inherit; }
.agent-options { margin-top:18px; }.agent-options .agent-fields { margin-top:16px; }
summary { cursor:pointer; font-weight:600; font-size:14px; }
.agent-scope { color:#64748b; font-size:12px; line-height:1.8; overflow-wrap:anywhere; }
.agent-primary { background:#2563eb; color:white; border:0; border-radius:8px; padding:12px 24px; cursor:pointer; font:inherit; }
.agent-primary:disabled { opacity:.55; cursor:wait; }
.agent-progress { display:flex; flex-wrap:wrap; gap:14px; padding:18px; margin:20px 0; background:#eff6ff; border-radius:10px; font-size:14px; }
.agent-error,.agent-warning { padding:16px; border-radius:8px; margin:18px 0; background:#fff1f2; color:#9f1239; }
.agent-error button { margin-left:12px; cursor:pointer; }
.agent-result-heading { margin-top:30px; }.agent-result-heading span { color:#64748b; font-size:14px; }
.agent-candidates { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.agent-candidate { background:white; border:1px solid #e2e8f0; padding:22px; border-radius:12px; }
.agent-candidate h3 { margin:0 0 8px; font-size:17px; overflow-wrap:anywhere; }
.agent-candidate dl { display:grid; grid-template-columns:repeat(2,1fr); gap:16px; margin-bottom:0; }
.agent-candidate dt { font-size:12px; color:#64748b; }.agent-candidate dd { margin:6px 0 0; font-weight:600; }
.agent-report { border:1px solid #e2e8f0; border-radius:10px; padding:20px; margin-top:20px; background:white; }
.agent-report pre { white-space:pre-wrap; overflow-wrap:anywhere; font-family:inherit; font-size:13px; line-height:1.9; }
.agent-evidence { border-top:1px solid #e2e8f0; padding:16px 0; font-size:13px; }
.agent-empty { padding:48px 20px; text-align:center; color:#64748b; }
.agent-actions,.agent-selection,.agent-applied { display:flex; flex-wrap:wrap; align-items:center; gap:12px; }.agent-actions { margin-top:20px; padding-top:14px; border-top:1px solid #e2e8f0; }.agent-actions button,.agent-selection button,.agent-applied button,.agent-empty button,.agent-suggestions button { padding:8px 12px; border:1px solid #cbd5e1; background:white; color:#2563eb; border-radius:6px; cursor:pointer; font-size:13px; }.agent-empty button { margin:5px; }.agent-selection { font-size:13px; margin:20px 0 14px; }.agent-applied { background:#f8fafc; border-radius:8px; padding:14px; font-size:12px; color:#475569; }.agent-applied button { margin-left:auto; }.agent-pick { display:flex; align-items:center; gap:8px; color:#475569; font-size:12px; margin-bottom:16px; }.agent-reasons { padding-left:18px; color:#475569; font-size:12px; line-height:1.9; margin:14px 0; }.agent-suggestions { background:#eff6ff; border-radius:8px; padding:12px; margin-top:16px; font-size:12px; }.agent-suggestions p { margin:0 0 8px; color:#475569; }.agent-suggestions button { margin:3px; }.agent-draft-note { font-size:12px; color:#92400e; line-height:1.8; }
.agent-applied span { min-width:0; overflow-wrap:anywhere; max-width:100%; }
@media(max-width:1000px) { .agent-fields { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media(max-width:650px) { .agent-fields,.agent-candidates { grid-template-columns:1fr; }.agent-intro,.agent-result-heading { align-items:flex-start; flex-direction:column; }.agent-form { padding:18px; } }
</style>
