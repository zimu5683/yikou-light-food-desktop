/**
 * 应用根组件：自绘标题栏 + 左右分栏（<980px 上下堆叠）+ 对话框与 Toast。
 * 分栏比例持久化到 AppConfig（0.30–0.55，与旧版 ResponsiveSplitPane 一致）。
 */
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Toaster } from '@/components/ui/sonner'
import { DecisionDialog, UpdateAvailableDialog, UpdateProgressDialog } from '@/components/dialogs'
import { LogConsole } from '@/components/LogConsole'
import { TaskPanel } from '@/components/TaskPanel'
import { TitleBar } from '@/components/TitleBar'
import { useApp } from '@/hooks/useApp'
import { cn } from '@/lib/utils'

const STACK_BREAKPOINT = 980
const RATIO_MIN = 0.3
const RATIO_MAX = 0.55

export default function App() {
  const { config, setSplitRatio } = useApp()
  const [stacked, setStacked] = useState(false)
  const [ratio, setRatio] = useState(config?.split_ratio ?? 0.38)
  const dividerRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  // 视口宽度 → 堆叠切换（对应旧版 layout_mode 980 断点）
  useEffect(() => {
    const onResize = () => setStacked(window.innerWidth < STACK_BREAKPOINT)
    onResize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onDividerDown = useCallback((e: ReactPointerEvent) => {
    dragging.current = true
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }, [])

  const onDividerMove = useCallback(
    (e: ReactPointerEvent) => {
      if (!dragging.current || !containerRef.current || stacked) return
      const rect = containerRef.current.getBoundingClientRect()
      const next = Math.min(RATIO_MAX, Math.max(RATIO_MIN, (e.clientX - rect.left) / rect.width))
      setRatio(next)
    },
    [stacked],
  )

  const onDividerUp = useCallback(() => {
    if (!dragging.current) return
    dragging.current = false
    setSplitRatio(ratio)
  }, [ratio, setSplitRatio])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <TitleBar />
      <main ref={containerRef} className={cn('flex min-h-0 flex-1', stacked ? 'flex-col' : 'flex-row')}>
        <div
          className={cn(
            'flex min-h-0 flex-col',
            stacked ? 'max-h-[55%] flex-none border-b' : 'shrink-0 border-r',
          )}
          style={stacked ? undefined : { width: `${Math.round(ratio * 100)}%`, minWidth: 340 }}
        >
          <TaskPanel />
        </div>

        {!stacked && (
          <div
            ref={dividerRef}
            role="separator"
            aria-orientation="vertical"
            onPointerDown={onDividerDown}
            onPointerMove={onDividerMove}
            onPointerUp={onDividerUp}
            className="group relative z-10 w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50"
          >
            <div className="absolute inset-y-0 -left-1.5 -right-1.5" />
          </div>
        )}

        <LogConsole />
      </main>

      {/* 对话框与通知 */}
      <DecisionDialog />
      <UpdateAvailableDialog />
      <UpdateProgressDialog />
      <Toaster position="bottom-right" richColors closeButton />
    </div>
  )
}
