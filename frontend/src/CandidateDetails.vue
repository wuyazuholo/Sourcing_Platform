<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
const props = defineProps({ detail: { type: Object, required: true }, reasons: { type: Array, required: true }, truncated: Boolean })
const emit = defineEmits(['close'])
const dialog = ref(null)
const maximumMonth = computed(() => Math.max(1, ...(props.detail.profile?.monthly_activity || []).map(row => row.record_count)))
onMounted(() => dialog.value.showModal())
onBeforeUnmount(() => { if (dialog.value?.open) dialog.value.close() })
</script>

<template>
  <dialog ref="dialog" aria-labelledby="candidate-title" class="candidate-dialog" @close="emit('close')">
    <header><div><p>候选详情 · {{ detail.candidate.supplier_country || '国家未记录' }}</p><h2 id="candidate-title">{{ detail.candidate.supplier_name }}</h2></div><button type="button" aria-label="关闭候选详情" @click="dialog.close()">关闭 ✕</button></header>
    <p class="scope">{{ truncated ? '统计仅覆盖本次保存的部分匹配记录。' : '统计仅覆盖本次已保存的匹配记录。' }}贸易活动不能直接证明当前供货能力或资质。</p>
    <section><h3>入选依据</h3><ul><li v-for="reason in reasons" :key="reason">{{ reason }}</li></ul></section>
    <template v-if="detail.profile">
      <p v-if="detail.profile.missing_buyer_ids" class="notice">{{ detail.profile.missing_buyer_ids }} 条匹配记录缺少买方 ID；“已知买方数”可能低估实际买方数量。</p>
      <section><h3>月份活动</h3><div v-for="month in detail.profile.monthly_activity" :key="month.month" class="month"><span>{{ month.month }}</span><div class="track"><div :style="{ width: `${month.record_count / maximumMonth * 100}%` }"></div></div><strong>{{ month.record_count }} 条</strong></div></section>
      <div class="detail-columns">
        <section><h3>主要已知买方（至多 5 家）</h3><p v-if="!detail.profile.buyers.length" class="scope">暂无可统计的买方标识，不能认定没有买方。</p><ul v-else class="data-list"><li v-for="buyer in detail.profile.buyers" :key="buyer.buyer_id"><span>{{ buyer.buyer_name || '名称未记录' }}</span><strong>{{ buyer.record_count }} 条</strong></li></ul></section>
        <section><h3>商品 HS 分布（至多 5 项）</h3><ul class="data-list"><li v-for="product in detail.profile.products" :key="product.hs_code || 'unknown'"><span>{{ product.hs_code || '编码未记录' }}</span><strong>{{ product.record_count }} 条</strong></li></ul></section>
      </div>
    </template>
    <p v-else class="notice">这个历史任务尚未保存详细画像。已有候选与原始引用仍可查看；新调查会自动生成完整画像。</p>
    <section><h3>交易证据（每家至多展示 3 条）</h3><article v-for="evidence in detail.evidence" :key="evidence.id" class="evidence"><strong>{{ evidence.source.shipment_date }} · {{ evidence.source.hs_code || 'HS 未记录' }}</strong><p>{{ evidence.source.goods_description || '商品原文未记录' }}</p><p>买方：{{ evidence.source.buyer_name || '名称未记录' }} · {{ evidence.source.buyer_country || '国家未记录' }}</p><p v-if="evidence.source.cif_usd != null">源字段货值（CIF / USD）：{{ evidence.source.cif_usd.toLocaleString('zh-CN') }}</p><details><summary>来源与快照</summary><p class="scope">{{ evidence.source.source_table }} · 记录 {{ evidence.source.record_id }}<br>SHA-256：{{ evidence.source.snapshot_sha256 }}</p></details></article></section>
  </dialog>
</template>

<style scoped>
.candidate-dialog { box-sizing:border-box; width:min(900px,calc(100vw - 32px)); max-height:88vh; overflow:auto; padding:28px; border:1px solid #dbe4f0; border-radius:16px; color:#172b4d; box-shadow:0 20px 80px #0f172a33; }
.candidate-dialog::backdrop { background:#0f172a88; }header { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; }header p { margin:0 0 8px; color:#64748b; font-size:13px; }h2 { font-size:22px; margin:0; overflow-wrap:anywhere; }h3 { font-size:15px; margin:22px 0 12px; }button { white-space:nowrap; padding:8px 12px; border:1px solid #cbd5e1; background:white; border-radius:6px; cursor:pointer; }
li,p { font-size:13px; line-height:1.9; overflow-wrap:anywhere; }.scope { color:#64748b; font-size:12px; }.notice { background:#fff7ed; padding:12px; border-radius:8px; color:#9a3412; }.detail-columns { display:grid; grid-template-columns:1fr 1fr; gap:28px; }
.data-list { list-style:none; padding:0; }.data-list li { display:flex; justify-content:space-between; align-items:baseline; gap:16px; padding:10px 0; border-bottom:1px solid #e2e8f0; }.data-list strong { white-space:nowrap; }
.month { display:grid; grid-template-columns:68px 1fr 65px; gap:12px; align-items:center; margin:12px 0; font-size:12px; }.track { height:10px; background:#eff6ff; border-radius:8px; overflow:hidden; }.track div { height:100%; background:#3b82f6; border-radius:8px; }.evidence { border-top:1px solid #e2e8f0; padding:14px 0; font-size:13px; }.evidence p { margin:6px 0; }summary { cursor:pointer; color:#2563eb; }
@media(max-width:650px) { .candidate-dialog { padding:18px; }.detail-columns { grid-template-columns:1fr; gap:0; }h2 { font-size:18px; } }
</style>
