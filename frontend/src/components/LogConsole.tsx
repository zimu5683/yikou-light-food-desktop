/**
 * 运行日志控制台 = 一张正在打印的小票（Wheat Press 记忆点）：
 * 锯齿顶边 + 等宽时间戳/级别 + 虚线裁切线 + 命中价签黄高亮 + 页脚印章小字。
 * 功能：即输即滤（保留命中高亮与无命中提示）、复制、清空、自动滚动。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, Copy, Eraser } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { statusLabel, useApp } from '@/hooks/useApp'
import { cn } from '@/lib/utils'

const LEVEL_CLASS: Record<string, string> = {
  OK: 'text-success',
  INFO: 'text-muted-foreground',
  WARN: 'text-warning',
  ERROR: 'text-destructive',
}

function statusBadgeMeta(status: string): { label: string; live: boolean } {
  return { label: statusLabel(status as never), live: status === 'running' }
}

export function LogConsole() {
  const { logs, status, clearLogs } = useApp()
  const [filter, setFilter] = useState('')
  const [autoscroll, setAutoscroll] = useState(true)
  const paperRef = useRef<HTMLDivElement>(null)

  const query = filter.trim().toLowerCase()
  const filtered = useMemo(
    () => (query ? logs.filter((row) => row.msg.toLowerCase().includes(query)) : logs),
    [logs, query],
  )

  useEffect(() => {
    if (autoscroll && paperRef.current) {
      paperRef.current.scrollTop = paperRef.current.scrollHeight
    }
  }, [filtered, autoscroll])

  async function copyAll() {
    const text = (query ? filtered : logs)
      .map((r) => `${r.ts} ${r.level} ${formatLogMsg(r.msg)}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      // 反馈由按钮自身短暂变化呈现
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }

  const { label: badgeLabel, live } = statusBadgeMeta(status)

  return (
    <section className="flex min-h-0 flex-1 flex-col px-5 pb-4 pt-4">
      <div className="flex items-center gap-2.5">
        <h2 className="font-serif text-base font-semibold tracking-[1px]">运行日志</h2>
        <span className="inline-flex h-6 items-center gap-1.5 rounded-[2px] border bg-card px-2 text-xs font-medium">
          <span
            className={cn(
              'size-[7px] rounded-[1px] bg-primary',
              live && 'led-breathe',
            )}
          />
          {badgeLabel}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤日志…"
            className="h-7 w-44 rounded-[4px] border-border bg-card text-xs"
          />
          <ToolButton onClick={copyAll} label="复制日志">
            <Copy className="size-3.5" />
            复制
          </ToolButton>
          <ToolButton onClick={clearLogs} label="清空日志">
            <Eraser className="size-3.5" />
            清空
          </ToolButton>
          <ToolButton
            onClick={() => setAutoscroll((v) => !v)}
            active={autoscroll}
            label="自动滚动"
          >
            <ArrowDownToLine className="size-3.5" />
            自动滚动
          </ToolButton>
        </div>
      </div>

      <div className="receipt mt-3.5 flex min-h-0 flex-1 flex-col">
        <div className="receipt-tear" />
        <div
          ref={paperRef}
          className="receipt-paper min-h-0 flex-1 select-text overflow-y-auto border-x bg-card py-2.5 font-mono text-xs"
        >
          {filtered.length === 0 && (
            <p className="px-4 py-6 text-center text-[11px] text-ink-faint">
              {logs.length === 0
                ? '等待任务启动，日志将实时打印在这里。'
                : `未找到包含“${filter.trim()}”的日志。`}
            </p>
          )}
          {filtered.map((row) => {
            const hit = Boolean(query) && row.msg.toLowerCase().includes(query)
            return (
              <div key={row.id} className="receipt-row flex gap-2.5 px-4 leading-[1.75]">
                <span className="shrink-0 text-ink-faint">{row.ts}</span>
                <span className={cn('w-[38px] shrink-0 font-semibold', LEVEL_CLASS[row.level])}>
                  {row.level}
                </span>
                <span
                  className={cn(
                    'min-w-0 break-all text-foreground',
                    hit && 'rounded-[2px] bg-hit px-0.5',
                  )}
                >
                  {isOrderSummary(row.msg) ? <OrderSummaryText msg={row.msg} /> : row.msg}
                </span>
              </div>
            )
          })}
          {logs.length > 0 && (
            <p className="mt-2.5 text-center font-serif text-[11px] tracking-[2px] text-ink-faint">
              — 一 口 轻 食 · 一 单 一 味 —
            </p>
          )}
        </div>
        <div className="h-0.5 shrink-0 border-x bg-card" />
      </div>
    </section>
  )
}

/** 订单摘要行（W8 | 李 | 电话 | 地址 | 餐品）→ 每字段一行 */
function isOrderSummary(msg: string): boolean {
  // automation 的 _format_order_summary 用全角“｜”连接字段
  return /^W\d+\s*[｜|]/.test(msg)
}

function formatLogMsg(msg: string): string {
  if (!isOrderSummary(msg)) return msg
  return msg
    .split(/[｜|]/)
    .map((part) => part.trim())
    .join('\n')
}

function OrderSummaryText({ msg }: { msg: string }) {
  const parts = msg.split(/[｜|]/).map((part) => part.trim())
  return (
    <span className="block">
      {parts.map((part, i) => (
        <span key={i} className={cn('block', i === 0 && 'font-semibold text-primary-strong')}>
          {part}
        </span>
      ))}
    </span>
  )
}

function ToolButton({
  onClick,
  active,
  label,
  children,
}: {
  onClick: () => void
  active?: boolean
  label: string
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        'inline-flex h-7 items-center gap-1 rounded-[4px] border px-2 text-xs transition-colors',
        active
          ? 'border-primary bg-primary-soft text-primary-strong'
          : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-foreground',
      )}
    >
      {children}
    </button>
  )
}
