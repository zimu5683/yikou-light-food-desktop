/**
 * 交互逻辑测试（node:test，无 DOM）。
 *
 * A31 要求的"双击不重复调用危险动作 / 未知不显示绿色 / 过期证据不能处置"
 * 都落在这些纯函数上：组件只渲染结果。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  ABSENT_CLASSIFICATION,
  canResolve,
  canUpload,
  evidenceFresh,
  singleFlight,
  uncertainGroups,
} from './interaction.ts'

// bridge.ts 在模块顶层写 window.__bridge（供 pywebview 侧注入），Node 里需要垫一层。
globalThis.window = {
  localStorage: {
    getItem: () => null,
    setItem: () => {},
  },
  __bridge: { dispatch: () => {} },
} as unknown as Window & typeof globalThis

const { classifyOutcome, conflictNotice, operationIsActive } = await import('./bridge.ts')

// ---------- 单飞：双击不重复触发危险动作 ----------

test('singleFlight 只执行一次，重复调用复用同一个 Promise', async () => {
  let calls = 0
  let release: (value: string) => void = () => {}
  const action = singleFlight(
    () => new Promise<string>((resolve) => {
      calls += 1
      release = resolve
    }),
  )

  const first = action()
  const second = action()
  assert.equal(calls, 1, '第二次点击必须复用同一个在途请求')
  assert.equal(first, second)

  release('done')
  assert.equal(await first, 'done')
})

test('singleFlight 完成后可以再次触发（不是永久锁死）', async () => {
  let calls = 0
  const action = singleFlight(async () => {
    calls += 1
    return calls
  })
  assert.equal(await action(), 1)
  assert.equal(await action(), 2)
})

test('singleFlight 失败后也释放，允许重试', async () => {
  let calls = 0
  const action = singleFlight(async () => {
    calls += 1
    if (calls === 1) throw new Error('boom')
    return 'ok'
  })
  await assert.rejects(() => action())
  assert.equal(await action(), 'ok')
})

// ---------- 证据新鲜度 ----------

test('evidenceFresh 默认按 600 秒判定', () => {
  const created = new Date('2026-10-07T10:00:00').getTime()
  assert.equal(evidenceFresh({ results: [], created_at: '2026-10-07T10:00:00' },
                             created + 599_000), true)
  assert.equal(evidenceFresh({ results: [], created_at: '2026-10-07T10:00:00' },
                             created + 601_000), false)
})

test('evidenceFresh 对缺失/非法时间一律视为过期', () => {
  assert.equal(evidenceFresh(null), false)
  assert.equal(evidenceFresh({ results: [] }), false)
  assert.equal(evidenceFresh({ results: [], created_at: '不是时间' }), false)
})

// ---------- 处置按钮：只有"站内确认没有"才能解除阻断 ----------

const freshEvidence = (classifications: Record<string, string>) => ({
  results: Object.entries(classifications).map(([journal_id, classification]) => ({
    journal_id,
    classification,
  })),
  created_at: new Date().toISOString(),
  ttl_seconds: 600,
})

test('canResolve 拒绝空选择与过短备注', () => {
  assert.equal(canResolve({
    decision: 'station_absent', selected: [], evidence: null, note: '已确认没有',
  }).allowed, false)
  assert.equal(canResolve({
    decision: 'station_absent', selected: ['a'], evidence: freshEvidence({ a: ABSENT_CLASSIFICATION }),
    note: '嗯',
  }).allowed, false)
})

test('canResolve 允许 keep（保持阻断不写记录）', () => {
  assert.equal(canResolve({
    decision: 'keep', selected: ['a'], evidence: null, note: '',
  }).allowed, true)
})

test('canResolve 拒绝没有证据或证据过期的情形', () => {
  assert.equal(canResolve({
    decision: 'station_absent', selected: ['a'], evidence: null, note: '已确认没有',
  }).reason.includes('只读核对'), true)

  const stale = {
    results: [{ journal_id: 'a', classification: ABSENT_CLASSIFICATION }],
    created_at: new Date(Date.now() - 3600_000).toISOString(),
    ttl_seconds: 600,
  }
  const got = canResolve({
    decision: 'station_absent', selected: ['a'], evidence: stale, note: '已确认没有',
  })
  assert.equal(got.allowed, false)
  assert.match(got.reason, /过期/)
})

test('canResolve 拒绝"查询失败"与"找到相似订单"', () => {
  const failed = canResolve({
    decision: 'station_absent', selected: ['a'],
    evidence: freshEvidence({ a: 'scan_failed' }), note: '我已确认没有',
  })
  assert.equal(failed.allowed, false)
  assert.match(failed.reason, /查询失败或找到相似订单/)

  const otherDay = canResolve({
    decision: 'station_absent', selected: ['a'],
    evidence: freshEvidence({ a: 'station_found_other_day' }), note: '我已确认没有',
  })
  assert.equal(otherDay.allowed, false)
})

test('canResolve 拒绝不在核对结果里的记录（核对后新增）', () => {
  const got = canResolve({
    decision: 'station_absent', selected: ['a', 'new-one'],
    evidence: freshEvidence({ a: ABSENT_CLASSIFICATION }), note: '我已确认没有',
  })
  assert.equal(got.allowed, false)
  assert.match(got.reason, /不在这次核对结果里/)
})

test('canResolve 允许全部为 station_missing 的正常解除', () => {
  const got = canResolve({
    decision: 'station_absent', selected: ['a', 'b'],
    evidence: freshEvidence({ a: ABSENT_CLASSIFICATION, b: ABSENT_CLASSIFICATION }),
    note: '已逐条打电话确认站内没有',
  })
  assert.equal(got.allowed, true, got.reason)
})

// ---------- 上传按钮 ----------

test('canUpload 要求令牌，且未知结果时禁止直接重传', () => {
  assert.equal(canUpload({ enabled: true, operationActive: false }).allowed, false)
  assert.equal(canUpload({
    enabled: true, operationActive: true, previewId: 'pv-1',
  }).allowed, false)
  const unknown = canUpload({
    enabled: true, operationActive: false, previewId: 'pv-1',
    previewOk: false, outcomeKind: 'unknown',
  })
  assert.equal(unknown.allowed, false)
  assert.match(unknown.reason, /恢复/)
  assert.equal(canUpload({
    enabled: true, operationActive: false, previewId: 'pv-1', previewOk: true,
  }).allowed, true)
})

// ---------- 结果分档：未知/阻断不得显示成"成功" ----------

test('classifyOutcome 把未知结果判为 unknown（不是失败也不是成功）', () => {
  assert.equal(classifyOutcome({ status: 'uncertain', ok: false }).kind, 'unknown')
  assert.equal(
    classifyOutcome({ status: 'blocked_by_uncertain', ok: false }).kind, 'unknown')
  assert.equal(
    classifyOutcome({
      status: 'failed', ok: false,
      executionSummary: { rows_unknown: true, proven_no_write: false },
    }).kind,
    'unknown',
  )
})

test('classifyOutcome 区分阻断与未执行', () => {
  assert.equal(classifyOutcome({ status: 'blocked', ok: false }).kind, 'blocked')
  assert.equal(classifyOutcome({ status: 'stale_batch', ok: false }).kind, 'blocked')
  assert.equal(classifyOutcome({ code: 'operation_conflict', ok: false }).kind, 'rejected')
  assert.equal(classifyOutcome({ code: 'preview_expired', ok: false }).kind, 'rejected')
})

test('classifyOutcome 对成功与部分完成给出不同档次', () => {
  assert.equal(classifyOutcome({ status: 'success', ok: true }).kind, 'success')
  assert.equal(classifyOutcome({ status: 'noop', ok: true }).kind, 'success')
  assert.equal(classifyOutcome({ status: 'partial', ok: false }).kind, 'partial')
  assert.notEqual(classifyOutcome({ status: 'partial', ok: false }).tone, 'ok')
})

test('uncertainPending 会把"成功"降级成未知（还有未结案记录）', () => {
  const got = classifyOutcome({ status: 'success', ok: true, uncertainPending: 2 })
  assert.equal(got.kind, 'unknown')
})

// ---------- 操作状态 ----------

test('operationIsActive 只看后端的 active 字段', () => {
  assert.equal(operationIsActive(null), false)
  assert.equal(operationIsActive({ ok: true, active: false, operation: null, last: null }), false)
  assert.equal(operationIsActive({
    ok: true, active: true, last: null,
    operation: {
      operation_id: 'op-1', mode: 'order', title: '订单处理', status: 'running',
      active: true, phase: '登录', reason: '', next_action: '', summary: {},
      started_at: '', finished_at: '',
    },
  }), true)
})

test('conflictNotice 说清"谁在跑"', () => {
  const notice = conflictNotice({
    ok: true, active: true, last: null,
    operation: {
      operation_id: 'op-1', mode: 'wps_upload', title: '云文档上传', status: 'running',
      active: true, phase: 'applying', reason: '', next_action: '', summary: {},
      started_at: '', finished_at: '',
    },
  })
  assert.match(notice, /云文档上传/)
  assert.match(notice, /applying/)
})

// ---------- 未决记录分组 ----------

test('uncertainGroups 兼容没有 groups 的旧返回', () => {
  const got = uncertainGroups({ records: [{ journal_id: 'a' }] })
  assert.equal(got.current.length, 1)
  assert.equal(got.otherScope.length, 0)
  assert.equal(got.history.length, 0)
})

test('uncertainGroups 保留其它范围与历史记录', () => {
  const got = uncertainGroups({
    groups: { current: [{ journal_id: 'a' }], other_scope: [{ journal_id: 'b' }],
              history: [{ journal_id: 'c' }] },
  })
  assert.deepEqual(got.current.map((item) => (item as { journal_id: string }).journal_id), ['a'])
  assert.equal(got.otherScope.length, 1)
  assert.equal(got.history.length, 1)
})
