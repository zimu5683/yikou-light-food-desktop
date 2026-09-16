/**
 * 运行日志控制台 = 一张正在打印的小票（Wheat Press 记忆点）：
 * 锯齿顶边 + 等宽时间戳/级别 + 虚线裁切线 + 命中价签黄高亮 + 页脚印章小字。
 * 功能：即输即滤（保留命中高亮与无命中提示）、复制、清空、自动滚动。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, Copy, Eraser } from 'lucide-react'
import { Input } from '@/components/ui/input'
import type { AddressInputRequest } from '@/lib/bridge'
import { statusLabel, useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { formatLogMsg, isOrderSummary, splitOrderSummary } from '@/lib/format'

const LEVEL_CLASS: Record<string, string> = {
  OK: 'text-success',
  INFO: 'text-muted-foreground',
  WARN: 'text-warning',
  ERROR: 'text-destructive',
}

/**
 * 状态徽章文案与「是否在跑」。
 *
 * 留在组件里而不搬进 `@/lib/format`：它依赖 `statusLabel`，而 `statusLabel` 所在的
 * `@/hooks/appContext` 用了 `@/` 别名导入，Node 的测试运行器解析不了 —— 搬过去会让
 * `format.ts` 无法被测试直接 import。真正的文案映射本来就在 appContext 里。
 */
function statusBadgeMeta(status: string): { label: string; live: boolean } {
  return { label: statusLabel(status as never), live: status === 'running' }
}

export function LogConsole() {
  const { logs, status, clearLogs, addressInput, resolveAddressInput } = useApp()
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
  }, [filtered, autoscroll, addressInput])

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
          {filtered.length === 0 && !addressInput && (
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
          {addressInput && (
            <InlineAddressInput
              key={addressInput.id}
              request={addressInput}
              onResolve={resolveAddressInput}
            />
          )}
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

/** 日志区内的待确认地址输入框：不弹窗，直接在原地址下面换地址 */
function InlineAddressInput({
  request,
  onResolve,
}: {
  request: AddressInputRequest
  onResolve: (id: string, entries: Record<string, string>) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const filled = request.items.some((item) => (values[item.raw_address] ?? '').trim().length > 0)

  function update(raw: string, value: string) {
    setValues((prev) => ({ ...prev, [raw]: value }))
  }

  function submit() {
    const entries: Record<string, string> = {}
    for (const item of request.items) {
      const value = (values[item.raw_address] ?? '').trim()
      if (value) entries[item.raw_address] = value
    }
    onResolve(request.id, entries)
  }

  function skip() {
    onResolve(request.id, {})
  }

  return (
    <div className="mx-4 my-2.5 rounded-[4px] border border-dashed border-primary bg-primary-soft/40 p-3">
      <div className="font-serif text-xs font-semibold text-primary-strong">
        待确认地址 · 请直接输入最终地址
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
        输入后点「应用并排序」，系统会自动改表并整理；留空则保持原待确认流程。
      </p>
      <div className="mt-2.5 space-y-2.5">
        {request.items.map((item) => (
          <div key={item.raw_address} className="rounded-[3px] border border-border bg-card p-2.5">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
              <span className="font-semibold text-foreground">{item.order_numbers.join('、')}</span>
              <span className="text-ink-faint">{item.reason || '无法自动识别'}</span>
            </div>
            <div className="mt-0.5 break-all text-[11px] text-ink-faint">{item.raw_address}</div>
            {item.suggested_point ? (
              <div className="mt-0.5 text-[11px] text-ink-faint">规则建议：{item.suggested_point}</div>
            ) : null}
            <input
              autoFocus={request.items.indexOf(item) === 0}
              value={values[item.raw_address] ?? ''}
              onChange={(e) => update(item.raw_address, e.target.value)}
              placeholder="在这里输入地址，如 D2 / 学三 / 教5"
              className="mt-1.5 h-9 w-full rounded-[4px] border border-border bg-secondary px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
            />
          </div>
        ))}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          onClick={skip}
          className="h-8 rounded-[4px] border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
        >
          暂不处理
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!filled}
          className="h-8 rounded-[4px] bg-primary px-3 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          应用并排序
        </button>
      </div>
    </div>
  )
}

/** 订单摘要行（W8 | 李 | 电话 | 地址 | 餐品）→ 每字段一行 */
function OrderSummaryText({ msg }: { msg: string }) {
  const parts = splitOrderSummary(msg)
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
