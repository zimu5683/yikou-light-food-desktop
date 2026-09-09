/**
 * AppProvider 与消费者共享的 context/类型/selector。
 *
 * 单独成文件，让 useApp.tsx 只导出 React 组件，避免 Fast Refresh 因
 * “同一文件同时导出组件和 hook/常量”而失效。
 */
import { createContext, useContext } from 'react'
import {
  type AppState,
  type CaptchaRequest,
  type DecisionRequest,
  type LogEntry,
  type OrderFormPayload,
  type SssFormPayload,
  type StatusState,
  type UpdateAvailable,
} from '@/lib/bridge'

export type TaskMode = 'order' | 'sss'

export interface LogRow extends LogEntry {
  id: number
}

export interface UpdateProgress {
  stage: string
  downloaded: number
  total: number | null
}

export type FieldErrors = Record<string, { message: string } | undefined>

export interface AppStateBundle {
  ready: boolean
  mocked: boolean
  version: string
  status: StatusState
  frozen: boolean
  config: AppState['config'] | null
  passwords: { order: string; sss: string }
  logs: LogRow[]
  decision: DecisionRequest | null
  captcha: CaptchaRequest | null
  updateProgress: UpdateProgress | null
  workerAlive: boolean
  mode: TaskMode
  setMode: (mode: TaskMode) => void
  startOrder: (payload: OrderFormPayload) => Promise<FieldErrors | null>
  startSss: (payload: SssFormPayload) => Promise<FieldErrors | null>
  stopTask: () => Promise<void>
  chooseExcel: (mode: 'order' | 'sss') => Promise<{ path: string; error: string }>
  newTemplate: (mode: 'order' | 'sss') => Promise<{ path: string; error: string }>
  checkBrowser: () => void
  clearPassword: (mode: 'order' | 'sss') => Promise<void>
  checkUpdates: (manual: boolean) => void
  installUpdate: () => Promise<boolean>
  openExternal: (url: string) => void
  requestClose: () => void
  setSplitRatio: (ratio: number) => void
  clearLogs: () => void
  resolveDecision: (id: string, choice: string) => void
  resolveCaptcha: (id: string, code: string) => void
}

export const AppContext = createContext<AppStateBundle | null>(null)
export const UpdateAvailableContext = createContext<{
  available: UpdateAvailable | null
  setAvailable: (v: UpdateAvailable | null) => void
} | null>(null)

const STATUS_LABELS: Record<StatusState, string> = {
  ready: '就绪',
  running: '处理中',
  stopping: '正在停止',
  success: '处理完成',
  partial: '部分完成',
  stopped: '已停止',
  error: '处理失败',
  updating: '检查更新',
}

export function statusLabel(state: StatusState): string {
  return STATUS_LABELS[state] ?? state
}

export function useUpdateAvailable() {
  const ctx = useContext(UpdateAvailableContext)
  if (!ctx) throw new Error('useUpdateAvailable must be used within AppProvider')
  return ctx
}

export function useApp(): AppStateBundle {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp must be used within AppProvider')
  return ctx
}
