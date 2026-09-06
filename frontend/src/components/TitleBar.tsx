/**
 * 自绘标题栏：印章 Logo + 宋体产品名 + 版本徽章 + 主题切换 + 窗口控制。
 * `.pywebview-drag-region` 供 pywebview frameless 拖拽。
 */
import { useEffect, useState, type ReactNode } from 'react'
import { Minus, Moon, Square, Sun, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useApp } from '@/hooks/useApp'
import { api, isApiReady } from '@/lib/bridge'
import { applyTheme, initialTheme, type Theme } from '@/lib/theme'

function windowAction(action: 'minimize' | 'toggle_maximize') {
  if (isApiReady()) api().window_action(action).catch(() => {})
}

export function TitleBar() {
  const { version, mocked, requestClose } = useApp()
  const [theme, setTheme] = useState<Theme>(initialTheme)

  // 启动时把持久化的主题真正应用上去（仅保存状态不会切换 .dark 类）。
  useEffect(() => {
    applyTheme(theme)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggleTheme() {
    const next: Theme = theme === 'light' ? 'dark' : 'light'
    setTheme(next)
    applyTheme(next)
  }

  return (
    <header className="flex h-11 shrink-0 select-none items-center gap-2.5 border-b px-4">
      <div
        className="pywebview-drag-region -my-11 flex h-11 flex-1 items-center gap-2.5"
        onMouseDown={(e) => {
          if (e.button === 0 && isApiReady()) {
            api().begin_window_drag(e.screenX, e.screenY).catch(() => {})
          }
        }}
      >
        <div className="flex h-6 w-6 items-center justify-center rounded-[3px] bg-primary font-serif text-[13px] font-bold text-primary-foreground">
          轻
        </div>
        <span className="font-serif text-base font-semibold tracking-[1px]">一口轻食</span>
        <span className="text-xs text-muted-foreground">订单自动处理台</span>
        <Badge
          variant="outline"
          className="tabular ml-1 rounded-[3px] font-mono text-[11px] font-normal"
        >
          v{version || '…'}
        </Badge>
        {mocked && <span className="text-xs text-warning">浏览器 mock 模式</span>}
      </div>
      <div className="flex items-center gap-1.5">
        <Button
          variant="ghost"
          size="sm"
          className="h-6 rounded-[4px] px-2 text-xs text-muted-foreground hover:text-foreground"
          onClick={toggleTheme}
        >
          {theme === 'light' ? <Moon className="size-3.5" /> : <Sun className="size-3.5" />}
          {theme === 'light' ? '深色' : '浅色'}
        </Button>
        <div className="mx-1 h-4 w-px bg-border" />
        <WindowButton label="最小化" onClick={() => windowAction('minimize')}>
          <Minus className="size-3.5" />
        </WindowButton>
        <WindowButton label="最大化" onClick={() => windowAction('toggle_maximize')}>
          <Square className="size-3" />
        </WindowButton>
        <WindowButton label="关闭" danger onClick={requestClose}>
          <X className="size-4" />
        </WindowButton>
      </div>
    </header>
  )
}

function WindowButton({
  label,
  onClick,
  danger,
  children,
}: {
  label: string
  onClick: () => void
  danger?: boolean
  children: ReactNode
}) {
  return (
    <button
      aria-label={label}
      title={label}
      onClick={onClick}
      className={`grid h-7 w-8 place-items-center rounded-[4px] text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground ${
        danger ? 'hover:bg-destructive hover:text-white' : ''
      }`}
    >
      {children}
    </button>
  )
}
