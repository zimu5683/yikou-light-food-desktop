/**
 * 交互逻辑测试（node:test，无 DOM）。
 *
 * A31 要求的"双击不重复调用危险动作 / 未知不显示绿色"都落在这些纯函数上：
 * 组件只渲染结果。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { canUpload, singleFlight } from './interaction.ts'

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
