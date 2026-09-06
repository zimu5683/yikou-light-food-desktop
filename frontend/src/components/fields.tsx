/**
 * 表单基础件：Field（三态输入容器）、Stepper、DateField（仅今天/过去）。
 * 三态语义对齐旧版 FormField：neutral（聚焦高亮）/ valid / invalid。
 */
import { useMemo, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from 'react'
import { CalendarIcon, Minus, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

export interface FieldState {
  state?: 'neutral' | 'valid' | 'invalid'
  message?: string
}

export function Field({
  label,
  htmlFor,
  error,
  okMessage,
  helper,
  children,
  className,
}: {
  label: string
  htmlFor?: string
  error?: string
  okMessage?: string
  /** 常驻 helper（聚焦时显示） */
  helper?: string
  children: ReactNode
  className?: string
}) {
  const [focused, setFocused] = useState(false)
  const showHelper = Boolean(error || okMessage || (focused && helper))
  return (
    <div className={cn('mb-3', className)}>
      <label htmlFor={htmlFor} className="mb-1 block text-xs font-medium">
        {label}
      </label>
      <div onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}>
        {children}
      </div>
      {showHelper && (
        <p className={cn('mt-1 text-[11px]', error ? 'text-destructive' : 'text-muted-foreground')}>
          {error ?? okMessage ?? helper}
        </p>
      )}
    </div>
  )
}

export function TextInput({
  state,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { state?: FieldState['state'] }) {
  return (
    <Input
      className={cn(
        'h-[34px] rounded-[4px] border-transparent bg-secondary text-[13px] transition-colors focus-visible:bg-card',
        state === 'valid' &&
          'border-success/50 bg-success/5 focus-visible:border-success focus-visible:ring-success/20',
        state === 'invalid' && 'border-destructive bg-destructive/5',
        className,
      )}
      {...props}
    />
  )
}

export function GhostButton({
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button
      variant="outline"
      className={cn(
        'h-[34px] shrink-0 rounded-[4px] border-border bg-card px-3 text-xs text-foreground hover:border-primary hover:bg-card hover:text-primary-strong',
        className,
      )}
      {...props}
    />
  )
}

/** 数字步进器：可留空（留空表示“全部”），左右 − ＋，中间等宽数字 */
export function Stepper({
  value,
  onChange,
  min = 1,
  max = 9999,
  invalid,
  allowEmpty = true,
}: {
  value: number | null
  onChange: (v: number | null) => void
  min?: number
  max?: number
  invalid?: boolean
  allowEmpty?: boolean
}) {
  // 草稿态：输入时允许任意内容（含清空），失焦或按按钮时才提交并钳位。
  const [draft, setDraft] = useState<string | null>(null)
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.trunc(v) || min))
  const commit = (raw: string) => {
    const parsed = Number.parseInt(raw, 10)
    if ((raw.trim() === '' || Number.isNaN(parsed)) && allowEmpty) {
      onChange(null)
    } else {
      onChange(clamp(Number.isNaN(parsed) ? min : parsed))
    }
    setDraft(null)
  }
  const step = (delta: number) => {
    const base = draft !== null ? Number.parseInt(draft, 10) : value
    if (delta < 0 && (base === null || base === undefined || Number.isNaN(base))) {
      onChange(null)
      setDraft(null)
      return
    }
    const numeric = base && !Number.isNaN(base) ? base : min
    let next: number | null
    if (base === null || base === undefined || Number.isNaN(base)) {
      // 留空时按 +：回到最小值（1），而不是跳到 2。
      next = delta > 0 ? min : null
    } else if (delta < 0 && allowEmpty && numeric <= min) {
      next = null
    } else {
      next = clamp(numeric + delta)
    }
    onChange(next)
    setDraft(null)
  }
  return (
    <div
      className={cn(
        'inline-flex items-center overflow-hidden rounded-[4px] border bg-card',
        invalid && 'border-destructive',
      )}
    >
      <button
        type="button"
        aria-label="减少"
        className="h-[34px] w-[30px] text-[15px] text-muted-foreground hover:bg-secondary hover:text-foreground"
        onClick={() => step(-1)}
      >
        <Minus className="mx-auto size-3.5" />
      </button>
      <input
        aria-label="待处理订单数"
        inputMode="numeric"
        className="tabular h-[34px] w-11 border-x bg-transparent text-center font-mono text-[13px] outline-none"
        value={draft ?? (value === null ? '' : String(value))}
        onChange={(e) => {
          const raw = e.target.value.replace(/[^0-9]/g, '').slice(0, 4)
          setDraft(raw)
          if (raw !== '') onChange(clamp(Number.parseInt(raw, 10)))
          else if (allowEmpty) onChange(null)
        }}
        onBlur={() => {
          if (draft !== null) commit(draft)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            if (draft !== null) commit(draft)
            ;(e.target as HTMLInputElement).blur()
          }
        }}
      />
      <button
        type="button"
        aria-label="增加"
        className="h-[34px] w-[30px] text-[15px] text-muted-foreground hover:bg-secondary hover:text-foreground"
        onClick={() => step(1)}
      >
        <Plus className="mx-auto size-3.5" />
      </button>
    </div>
  )
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'] as const

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  )
}

function formatISO(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** 日期选择：只允许今天或过去日期（与旧版 _DatePickerPopup 语义一致），可清空 */
export function DateField({
  value,
  onChange,
  invalid,
}: {
  value: string
  onChange: (iso: string) => void
  invalid?: boolean
}) {
  const today = useMemo(() => {
    const now = new Date()
    return new Date(now.getFullYear(), now.getMonth(), now.getDate())
  }, [])
  const [viewMonth, setViewMonth] = useState<Date>(() => {
    const parsed = value ? new Date(value) : today
    return Number.isNaN(parsed.getTime()) ? today : startOfMonth(parsed)
  })
  const [open, setOpen] = useState(false)

  const selected = useMemo(() => {
    const parsed = value ? new Date(value) : null
    return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null
  }, [value])

  const cells = useMemo(() => {
    const first = startOfMonth(viewMonth)
    const startWeekday = first.getDay()
    const daysInMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 0).getDate()
    const list: (Date | null)[] = Array.from({ length: startWeekday }, () => null)
    for (let day = 1; day <= daysInMonth; day += 1) {
      list.push(new Date(viewMonth.getFullYear(), viewMonth.getMonth(), day))
    }
    return list
  }, [viewMonth])

  const canGoNext = startOfMonth(viewMonth) < startOfMonth(today)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className="flex flex-1 gap-1.5">
        <PopoverTrigger asChild>
          <button
            type="button"
            className={cn(
              'flex h-[34px] flex-1 items-center rounded-[4px] border border-transparent bg-secondary px-2.5 text-left text-[13px] transition-colors hover:bg-secondary/80',
              invalid && 'border-destructive bg-destructive/5',
              open && 'border-primary bg-card',
            )}
          >
            <CalendarIcon className="mr-2 size-3.5 text-muted-foreground" />
            <span className={cn('tabular', !value && 'text-ink-faint')}>
              {value || '留空默认今天'}
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-[264px] rounded-lg border bg-card p-3" align="start">
          <div className="mb-2 flex items-center justify-between">
            <button
              type="button"
              aria-label="上一月"
              className="grid size-7 place-items-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground"
              onClick={() => setViewMonth(new Date(viewMonth.getFullYear(), viewMonth.getMonth() - 1, 1))}
            >
              ‹
            </button>
            <span className="text-[13px] font-medium">
              {viewMonth.getFullYear()} 年 {viewMonth.getMonth() + 1} 月
            </span>
            <button
              type="button"
              aria-label="下一月"
              disabled={!canGoNext}
              className="grid size-7 place-items-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-30"
              onClick={() => setViewMonth(new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 1))}
            >
              ›
            </button>
          </div>
          <div className="grid grid-cols-7 gap-y-1 text-center">
            {WEEKDAYS.map((d) => (
              <span key={d} className="py-1 text-[11px] text-muted-foreground">
                {d}
              </span>
            ))}
            {cells.map((date, i) => {
              if (!date) return <span key={`empty-${i}`} />
              const future = date > today
              const isSelected = selected ? sameDay(date, selected) : false
              return (
                <button
                  key={date.toISOString()}
                  type="button"
                  disabled={future}
                  onClick={() => {
                    onChange(formatISO(date))
                    setOpen(false)
                  }}
                  className={cn(
                    'tabular mx-auto grid size-7 place-items-center rounded-[4px] font-mono text-xs',
                    future && 'text-ink-faint/40',
                    !future && !isSelected && 'hover:bg-secondary',
                    isSelected && 'bg-primary font-semibold text-primary-foreground',
                  )}
                >
                  {date.getDate()}
                </button>
              )
            })}
          </div>
          <div className="mt-2 flex justify-between border-t pt-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs text-muted-foreground"
              onClick={() => {
                onChange('')
                setOpen(false)
              }}
            >
              清空
            </Button>
            <span className="self-center text-[11px] text-muted-foreground">
              仅可选择今天或过去日期
            </span>
          </div>
        </PopoverContent>
      </div>
    </Popover>
  )
}
