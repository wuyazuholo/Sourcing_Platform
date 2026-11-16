<script setup>
import { METRICS } from './researchView.js'
defineProps({ comparison: { type: Object, required: true }, metric: { type: String, required: true }, truncated: Boolean })
const emit = defineEmits(['update:metric'])
</script>
<template>
  <section class="comparison" aria-label="候选对比">
    <header><h3>候选对比</h3><label>比较指标 <select :value="metric" @change="emit('update:metric', $event.target.value)"><option v-for="(value, key) in METRICS" :key="key" :value="key">{{ value.label }}</option></select></label></header>
    <p>{{ comparison.reason }}。{{ truncated ? '结果已截断，不能据此作全表排名。' : '比较范围为本次保存的匹配记录。' }}</p>
    <div class="table-wrap" tabindex="0" aria-label="可横向滚动的候选对比表"><table><thead><tr><th>指标</th><th v-for="candidate in comparison.rows" :key="candidate.supplier_id">{{ candidate.supplier_name }}<span v-if="comparison.winnerIds.includes(String(candidate.supplier_id))">该指标领先{{ comparison.winnerIds.length > 1 ? '（并列）' : '' }}</span></th></tr></thead><tbody><tr v-for="(value, key) in METRICS" :key="key"><th>{{ value.label }}</th><td v-for="candidate in comparison.rows" :key="candidate.supplier_id" :class="{ emphasized: key === metric && comparison.winnerIds.includes(String(candidate.supplier_id)) }">{{ candidate[key] ?? '未记录' }}</td></tr></tbody></table></div>
  </section>
</template>
<style scoped>
.comparison { background:white; border:1px solid #dbe4f0; padding:20px; border-radius:12px; margin:18px 0; }.comparison header { display:flex; align-items:center; justify-content:space-between; gap:16px; }h3 { margin:0; font-size:17px; }label,p { font-size:13px; }p { color:#64748b; line-height:1.8; }select { padding:8px; border:1px solid #cbd5e1; border-radius:6px; margin-left:8px; background:white; }.table-wrap { width:100%; overflow-x:auto; }table { width:100%; min-width:480px; border-collapse:collapse; table-layout:fixed; font-size:13px; }th,td { text-align:left; padding:14px 12px; border:1px solid #e2e8f0; overflow-wrap:anywhere; }thead th { background:#f8fafc; }th:first-child { width:100px; }th span { display:block; color:#2563eb; font-weight:400; margin-top:8px; font-size:12px; }.emphasized { background:#eff6ff; color:#1d4ed8; font-weight:600; }@media(max-width:650px) { .comparison { padding:14px; }.comparison header { align-items:flex-start; flex-direction:column; } }
</style>
