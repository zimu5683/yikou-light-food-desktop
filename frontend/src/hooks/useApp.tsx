/**
 * 全局应用状态：桥接事件 → React store 的唯一入口。
 *
 * 职责（对应 app/bridge.py 事件协议）：
 * - 握手并装载初始状态（config / passwords / version）
 * - log、status、task:*、update:*、decision 事件分发
 * - 表单动作、任务动作、文件对话框、窗口控制
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { toast } from 'sonner'
import {
  api,
  connectBridge,
  isApiReady,
  onBridgeEvent,
  pullBridgeEvents,
  type AppState,
  type CaptchaRequest,
  type DecisionRequest,
  type OrderFormPayload,
  type SssFormPayload,
  type StatusState,
  type BridgeEvent,
  type UpdateAvailable,
} from '@/lib/bridge'

import {
  AppContext,
  UpdateAvailableContext,
  type AppStateBundle,
  type LogRow,
  type TaskMode,
  type UpdateProgress,
} from './appContext'

export function AppProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [mocked, setMocked] = useState(false)
  const [version, setVersion] = useState('')
  const [status, setStatus] = useState<StatusState>('ready')
  const [frozen, setFrozen] = useState(false)
  const [config, setConfig] = useState<AppState['config'] | null>(null)
  const [passwords, setPasswords] = useState({ order: '', sss: '' })
  const [logs, setLogs] = useState<LogRow[]>([])
  const [decision, setDecision] = useState<DecisionRequest | null>(null)
  const [captcha, setCaptcha] = useState<CaptchaRequest | null>(null)
  const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null)
  const [updateAvailable, setAvailableState] = useState<UpdateAvailable | null>(null)
  const [mode, setMode] = useState<TaskMode>('order')
  const logId = useRef(0)

  const appendLog = useCallback((entry: Omit<LogRow, 'id'>) => {
    setLogs((prev) => [...prev.slice(-1999), { ...entry, id: (logId.current += 1) }])
  }, [])

  // ---- 桥接握手 ----
  useEffect(() => {
    let active = true
    connectBridge().then(({ state, mocked }) => {
      if (!active) return
      setReady(true)
      setMocked(mocked)
      setVersion(state.version)
      setStatus(state.status)
      setFrozen(state.frozen)
      setConfig(state.config)
      setPasswords(state.passwords)
      // 验收回传：自动化验收依赖本通道（evaluate_js 在新 WebKitGTK 上不可信）
      if (!mocked) {
        api()
          .frontend_report({ kind: 'ready', version: state.version, status: state.status })
          .catch(() => {})
      }
      // 旧版行为：启动 700ms 后静默检查更新
      if (!mocked) {
        setTimeout(() => {
          api().check_updates(false).catch(() => {})
        }, 700)
      }
    })
    return () => {
      active = false
    }
  }, [])

  // ---- 事件应用 ----
  const applyEvent = useCallback(
    (event: BridgeEvent) => {
      switch (event.event) {
        case 'log': {
          // 旧版用“-------”文本行分隔订单；新界面行间已有虚线裁切线，直接略去
          if (/^\s*-{3,}\s*$/.test(event.payload.msg)) break
          appendLog(event.payload)
          break
        }
        case 'events:dropped': {
          const critical = (event.payload.critical_dropped_count ?? 0) > 0
          appendLog({
            ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
            level: critical ? 'ERROR' : 'WARN',
            msg: event.payload.message,
          })
          if (critical) toast.error(event.payload.message, { duration: 10000 })
          break
        }
        case 'status':
          setStatus(event.payload.state)
          break
        case 'task:done':
          if (event.payload.stopped) {
            toast.info('任务已停止')
          } else if (event.payload.partial) {
            toast.error(event.payload.message, { duration: 8000 })
          } else {
            toast.success(event.payload.message)
          }
          break
        case 'task:error':
          toast.error(event.payload.message, { duration: 8000 })
          break
        case 'task:browser_missing':
          toast.error('未检测到可用浏览器', { duration: 8000 })
          api().open_external('https://www.microsoft.com/edge/download').catch(() => {})
          break
        case 'update:available':
          setAvailableState(event.payload)
          break
        case 'update:latest':
          toast.info(`当前已是最新版本（${event.payload.current}）。`)
          break
        case 'update:error':
          toast.error(`检查更新失败：${event.payload.message}`)
          break
        case 'update:progress':
          setUpdateProgress((prev) => ({
            stage: prev?.stage ?? '下载更新…',
            downloaded: event.payload.downloaded,
            total: event.payload.total,
          }))
          break
        case 'update:stage':
          setUpdateProgress((prev) => ({
            stage: event.payload.stage,
            downloaded: prev?.downloaded ?? 0,
            total: prev?.total ?? null,
          }))
          break
        case 'update:install_error':
          setUpdateProgress(null)
          toast.error(`更新失败：${event.payload.message}`, { duration: 8000 })
          break
        case 'update:installed':
          setUpdateProgress(null)
          toast.success('更新完成，程序即将关闭并自动重启。')
          break
        case 'decision':
          setDecision(event.payload)
          break
        case 'captcha':
          setCaptcha(event.payload)
          break
      }
    },
    [appendLog],
  )

  // ---- 事件分发：onBridgeEvent 监听 + 定时轮询双通道 ----
  useEffect(() => {
    const off = onBridgeEvent(applyEvent)
    return off
  }, [applyEvent])

  // 轮询通道：drain_events 是 Python→JS 的可靠方向（evaluate_js 不可信）
  useEffect(() => {
    if (!ready || mocked) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      try {
        // pullBridgeEvents 内部维护 last_sequence/ACK 与 event_id 去重：
        // 只有成功 dispatch 后才推进 cursor，断线/刷新后可从断点重放。
        await pullBridgeEvents()
      } catch {
        /* 超时或窗口关闭：下一轮继续 */
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 150)
      }
    }
    timer = window.setTimeout(poll, 120)
    return () => {
      stopped = true
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [ready, mocked, applyEvent])

  // ---- 动作 ----
  const startOrder = useCallback(async (payload: OrderFormPayload) => {
    const result = await api().start_order(payload)
    if (result.ok) return null
    if (result.reason === 'busy') {
      toast.error(result.message ?? '已有任务正在运行，请先停止')
      return {}
    }
    return result.fields ?? {}
  }, [])

  const startSss = useCallback(async (payload: SssFormPayload) => {
    const result = await api().start_sss(payload)
    if (result.ok) return null
    if (result.reason === 'busy') {
      toast.error(result.message ?? '已有任务正在运行，请先停止')
      return {}
    }
    return result.fields ?? {}
  }, [])

  const stopTask = useCallback(async () => {
    await api().stop_task().catch(() => {})
  }, [])

  const chooseExcel = useCallback(
    (mode: 'order' | 'sss') => api().choose_excel(mode),
    [],
  )

  const newTemplate = useCallback(
    (mode: 'order' | 'sss') => api().new_template(mode),
    [],
  )

  const checkBrowser = useCallback(() => {
    api().check_browser().catch(() => {})
  }, [])

  const clearPassword = useCallback(async (mode: 'order' | 'sss') => {
    await api().clear_password(mode).catch(() => {})
    toast.success(mode === 'sss' ? '已清除本机保存的闪时送密码' : '已清除本机保存的密码')
  }, [])

  const checkUpdates = useCallback((manual: boolean) => {
    api().check_updates(manual).catch(() => {})
  }, [])

  const installUpdate = useCallback(async () => {
    const result = await api().install_update().catch(() => ({ ok: false }))
    return 'ok' in result && result.ok
  }, [])

  const openExternal = useCallback((url: string) => {
    api().open_external(url).catch(() => {})
  }, [])

  const requestClose = useCallback(() => {
    api().request_close().catch(() => {})
  }, [])

  const setSplitRatio = useCallback((ratio: number) => {
    if (isApiReady()) api().set_split_ratio(ratio).catch(() => {})
  }, [])

  const clearLogs = useCallback(() => setLogs([]), [])

  const resolveDecision = useCallback((id: string, choice: string) => {
    setDecision(null)
    api().resolve_decision(id, choice).catch(() => {})
  }, [])

  const resolveCaptcha = useCallback((id: string, code: string) => {
    setCaptcha(null)
    api().resolve_captcha(id, code).catch(() => {})
  }, [])

  const value = useMemo<AppStateBundle>(
    () => ({
      ready,
      mocked,
      version,
      status,
      frozen,
      config,
      passwords,
      logs,
      decision,
      captcha,
      updateProgress,
      workerAlive: status === 'running' || status === 'stopping',
      mode,
      setMode,
      startOrder,
      startSss,
      stopTask,
      chooseExcel,
      newTemplate,
      checkBrowser,
      clearPassword,
      checkUpdates,
      installUpdate,
      openExternal,
      requestClose,
      setSplitRatio,
      clearLogs,
      resolveDecision,
      resolveCaptcha,
    }),
    [ready, mocked, version, status, frozen, config, passwords, logs, decision,
      updateProgress, mode, captcha, startOrder, startSss, stopTask, chooseExcel,
      newTemplate, checkBrowser, clearPassword, checkUpdates, installUpdate,
      openExternal, requestClose, setSplitRatio, clearLogs, resolveDecision, resolveCaptcha],
  )

  const updateAvailableValue = useMemo(
    () => ({
      available: updateAvailable,
      setAvailable: setAvailableState,
    }),
    [updateAvailable],
  )

  return (
    <AppContext.Provider value={value}>
      <UpdateAvailableContext.Provider value={updateAvailableValue}>
        {children}
      </UpdateAvailableContext.Provider>
    </AppContext.Provider>
  )
}
