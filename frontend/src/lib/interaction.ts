/**
 * 交互层的纯逻辑：把"能不能点、点了会不会重复、结果是什么档次"从组件里抽出来。
 *
 * 为什么单独成模块：这些判断是**安全相关**的（重复上传、把未知结果当成功），
 * 但它们又只依赖输入数据，完全可以脱离 DOM 测试。组件只负责渲染，判定逻辑
 * 在这里；真正的权威判定仍然在后端。
 */

export interface CanResolveResult {
  allowed: boolean
  /** 不满足时的原因（直接显示给用户，不要用泛泛的"操作不可用"）。 */
  reason: string
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
