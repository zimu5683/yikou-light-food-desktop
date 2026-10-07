/**
 * 交互层的纯逻辑：把"能不能点、点了会不会重复、结果是什么档次"从组件里抽出来。
 *
 * 为什么单独成模块：这些判断是**安全相关**的（重复上传、用过期证据解除阻断、
 * 把未知结果当成功），但它们又只依赖输入数据，完全可以脱离 DOM 测试。
 * 组件只负责渲染，判定逻辑在这里；真正的权威判定仍然在后端。
 */

export type DecisionKind = 'station_present' | 'station_absent' | 'keep'

export interface ReviewEvidenceItem {
  journal_id: string
  classification: string
}

export interface ReviewEvidence {
  results: ReviewEvidenceItem[]
  created_at?: string
  ttl_seconds?: number
}

/** 站内确认没有：只有这一种分类允许支撑「解除阻断」。 */
export const ABSENT_CLASSIFICATION = 'station_missing'

/** 证据是否还在有效期内（默认与后端一致：600 秒）。 */
export function evidenceFresh(
  evidence: ReviewEvidence | null,
  now: number = Date.now(),
): boolean {
  if (!evidence || !evidence.created_at) return false
  const created = Date.parse(evidence.created_at)
  if (Number.isNaN(created)) return false
  const ttlMs = (evidence.ttl_seconds ?? 600) * 1000
  return now - created <= ttlMs
}

export interface CanResolveInput {
  decision: DecisionKind
  selected: string[]
  evidence: ReviewEvidence | null
  note: string
  structuredChecked?: boolean
  now?: number
}

export interface CanResolveResult {
  allowed: boolean
  /** 不满足时的原因（直接显示给用户，不要用泛泛的"操作不可用"）。 */
  reason: string
}

/**
 * 处置按钮的可用条件（与后端校验一一对应，避免"能点但一定被拒"）。
 *
 * 「确认站内没有」最严：必须选了记录、证据新鲜、且**所选记录全部**是
 * ``station_missing``（读取失败或找到相似订单都不算）。这一条是前端与后端
 * 共同的语言，任何一侧放松都会让"读取失败"被当成"站内没有"。
 */
export function canResolve(input: CanResolveInput): CanResolveResult {
  const { decision, selected, evidence, note, structuredChecked } = input
  const now = input.now ?? Date.now()
  if (selected.length === 0) {
    return { allowed: false, reason: '请先勾选要处置的未决记录' }
  }
  if (decision === 'keep') {
    return { allowed: true, reason: '' }
  }
  if (note.trim().length < 4) {
    return { allowed: false, reason: '人工备注至少 4 个字符（写清凭什么这样判定）' }
  }
  if (decision === 'station_present') {
    return { allowed: true, reason: '' }
  }
  if (decision === 'station_absent' && structuredChecked === false) {
    // 仅保留接口：当前 UI 不需要结构核对（那是 WPS 恢复的口径）。
  }
  if (!evidence) {
    return { allowed: false, reason: '还没有只读核对证据，请先做「只读核对」' }
  }
  if (!evidenceFresh(evidence, now)) {
    return { allowed: false, reason: '核对证据已过期，请重新做「只读核对」' }
  }
  const byId = new Map(evidence.results.map((item) => [item.journal_id, item]))
  const missing = selected.filter((id) => !byId.has(id))
  if (missing.length > 0) {
    return {
      allowed: false,
      reason: `有 ${missing.length} 条所选记录不在这次核对结果里，请重新核对`,
    }
  }
  const notAbsent = selected
    .map((id) => byId.get(id)?.classification)
    .filter((value) => value !== ABSENT_CLASSIFICATION)
  if (notAbsent.length > 0) {
    return {
      allowed: false,
      reason:
        '所选记录里有的不是「站内确认没有」：查询失败或找到相似订单都不能当成没下单',
    }
  }
  return { allowed: true, reason: '' }
}

/**
 * 单飞：同一个危险动作在完成前不会被第二次触发（双击/连点）。
 *
 * 返回的包装函数会把并发调用直接短路成同一个 Promise，避免"点两下上传两次"。
 * 后端仍然有权威互斥 —— 这里只是不让用户制造出那次冲突。
 */
export function singleFlight<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
): (...args: A) => Promise<R> {
  let inflight: Promise<R> | null = null
  return (...args: A): Promise<R> => {
    if (inflight) return inflight
    let started: Promise<R>
    try {
      started = action(...args)
    } catch (error) {
      return Promise.reject(error)
    }
    inflight = started.finally(() => {
      inflight = null
    })
    return inflight
  }
}

/** 上传按钮的可用条件（令牌存在 + 上次结果不是"未知"）。 */
export function canUpload(input: {
  previewId?: string
  previewOk?: boolean
  enabled: boolean
  operationActive: boolean
  outcomeKind?: string
}): CanResolveResult {
  if (!input.enabled) return { allowed: false, reason: '云文档同步未开启' }
  if (input.operationActive) {
    return { allowed: false, reason: '当前有操作正在进行，等它结束后再上传' }
  }
  if (!input.previewId) {
    return { allowed: false, reason: '请先预览（上传必须带预览令牌）' }
  }
  if (input.previewOk === false && input.outcomeKind === 'unknown') {
    return {
      allowed: false,
      reason: '上一次上传结果未知，请先在「云同步恢复」里处置，不要直接重传',
    }
  }
  return { allowed: true, reason: '' }
}

/** 从 Bridge 返回的未决记录状态里挑出前端要展示的三组（缺失时退化为空）。 */
export function uncertainGroups(state: {
  records?: unknown
  groups?: { current?: unknown; other_scope?: unknown; history?: unknown }
} | null): { current: unknown[]; otherScope: unknown[]; history: unknown[] } {
  const groups = state?.groups ?? {}
  return {
    current: Array.isArray(groups.current)
      ? groups.current
      : Array.isArray(state?.records)
        ? state.records
        : [],
    otherScope: Array.isArray(groups.other_scope) ? groups.other_scope : [],
    history: Array.isArray(groups.history) ? groups.history : [],
  }
}
