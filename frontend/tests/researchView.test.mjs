import test from 'node:test'
import assert from 'node:assert/strict'
import { candidateDetail, compareCandidates, inclusionReasons, querySignature, reportForm, suggestedTerms } from '../src/researchView.js'

const report = { requirements: { hs_prefix: '8544', start_date: '2025-01-01', end_date: '2025-12-31' }, candidates: [
  { supplier_id: '9007199254740993', supplier_name: 'A', record_count: 9, buyer_count: 2, active_months: 3, latest_date: '2025-10-01' },
  { supplier_id: '9007199254740995', supplier_name: 'B', record_count: 7, buyer_count: 2, active_months: 2, latest_date: '2025-11-01' },
], candidate_profiles: [{ supplier_id: '9007199254740993', record_count: 9, missing_buyer_ids: 2 }], evidence: [{ source: { supplier_id: '9007199254740993' } }, { source: { supplier_id: '9007199254740995' } }] }
const ids = report.candidates.map(row => row.supplier_id)

test('comparison preserves BIGINT IDs and explains tied buyers versus recency', () => {
  assert.deepEqual(compareCandidates(report, ids, 'buyer_count').winnerIds, ids)
  assert.match(compareCandidates(report, ids, 'buyer_count').reason, /并列/)
  assert.deepEqual(compareCandidates(report, ids, 'latest_date').winnerIds, [ids[1]])
})
test('unknown, repeated, stale or oversized selections cannot become comparison winners', () => {
  for (const selected of [[], [ids[0]], [ids[0], ids[0]], [ids[0], 'unknown'], [...ids, '1', '2', '3', '4']]) assert.throws(() => compareCandidates(report, selected, 'record_count'))
  assert.throws(() => compareCandidates(report, ids, 'invented_score'))
})
test('missing buyer IDs are not turned into a zero-customer advantage', () => {
  const unknown = { ...report, candidates: report.candidates.map(row => ({ ...row, buyer_count: 0 })) }
  assert.deepEqual(compareCandidates(unknown, ids, 'buyer_count').winnerIds, [])
  const absent = { ...report, candidates: [report.candidates[0], { ...report.candidates[1], record_count: null }] }
  assert.deepEqual(compareCandidates(absent, ids, 'record_count').winnerIds, [])
})
test('details use only the selected candidate profile and evidence, including legacy reports', () => {
  const detail = candidateDetail(report, ids[0])
  assert.equal(detail.profile.record_count, 9)
  assert.equal(detail.evidence.length, 1)
  assert.equal(candidateDetail(report, 'unknown'), null)
  assert.equal(candidateDetail({ ...report, candidate_profiles: undefined }, ids[0]).profile, null)
})
test('inclusion explanations quote confirmed filters and observed records without invented scores', () => {
  const reasons = inclusionReasons(report, report.candidates[0]).join(' ')
  assert.match(reasons, /HS 8544/)
  assert.match(reasons, /9 条/)
  assert.doesNotMatch(reasons, /认证|评分|优质|全市场/)
})
test('submitted query can be restored and changes are detected independently of result data', () => {
  const form = reportForm(report)
  assert.equal(form.hs, '8544')
  assert.notEqual(querySignature(form), querySignature({ ...form, hs: '3926' }))
  assert.equal(querySignature(form), querySignature({ ...form, hs: ' 8544 ' }))
})
test('demo translation suggestions are explicit and do not modify the original query', () => {
  assert.equal(suggestedTerms('绝缘电缆')[0].text, 'cable')
  assert.deepEqual(suggestedTerms('unknown product'), [])
  assert.equal(report.requirements.hs_prefix, '8544')
})
