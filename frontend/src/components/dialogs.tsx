/**
 * 对话框体系：把旧版 15 种 messagebox/filedialog 映射为 Dialog/Toast。
 * - 决策弹窗（decision 事件）：订单定位失败/下单失败 retry-skip-stop、
 *   Excel 占用 retry-cancel、关闭保护 stop_and_close-keep-cancel
 * - 更新流程：发现新版本（确认安装/打开 Release）、下载进度（不可关闭）
 * - 浏览器缺失：引导打开 Edge/Chrome 下载页
 */
import { useState } from 'react'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Progress } from '@/components/ui/progress'
import { useApp, useUpdateAvailable } from '@/hooks/useApp'
import { cn } from '@/lib/utils'

const CHOICE_STYLES: Record<string, string> = {
  primary: 'bg-primary text-primary-foreground hover:bg-primary-strong',
  neutral: 'border-border bg-card text-foreground hover:bg-secondary',
  danger: 'bg-destructive text-white hover:bg-destructive/90',
}

/** 决策弹窗：worker 阻塞等待用户选择（对应旧 askyesnocancel/askretrycancel） */
export function DecisionDialog() {
  const { decision, resolveDecision } = useApp()
  if (!decision) return null
  return (
    <AlertDialog open onOpenChange={(open) => { if (!open) resolveDecision(decision.id, 'cancel') }}>
      <AlertDialogContent className="max-w-md rounded-lg">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-serif">{decision.title}</AlertDialogTitle>
          <AlertDialogDescription className="whitespace-pre-wrap">
            {decision.message}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="flex-row justify-end gap-2">
          {decision.choices.map((choice) => (
            <Button
              key={choice.value}
              className={cn('h-8 min-w-20 rounded-[6px] text-xs', CHOICE_STYLES[choice.style])}
              onClick={() => resolveDecision(decision.id, choice.value)}
            >
              {choice.label}
            </Button>
          ))}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** 闪时送图形验证码（纯接口模式）：在应用内显示验证码图片，不启动浏览器 */
export function CaptchaDialog() {
  const { captcha, resolveCaptcha } = useApp()
  const [code, setCode] = useState('')
  if (!captcha) return null
  const current = captcha

  function submit() {
    resolveCaptcha(current.id, code.trim())
    setCode('')
  }

  function cancel() {
    resolveCaptcha(current.id, '')
    setCode('')
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) cancel() }}>
      <DialogContent className="max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">闪时送登录验证</DialogTitle>
          <DialogDescription>
            请输入图片中的验证码。验证码用于纯接口登录，不会弹出浏览器。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col items-center gap-3 py-2">
          <img
            src={`data:image/png;base64,${current.image}`}
            alt="验证码"
            className="h-24 rounded border border-border bg-card object-contain"
          />
          <input
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder="请输入验证码"
            className="h-9 w-full rounded-[4px] border border-border bg-secondary px-3 text-center font-mono text-base tracking-[0.3em] outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
          />
        </div>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={cancel}>
            取消
          </Button>
          <Button
            className="h-8 rounded-[6px] text-xs"
            onClick={submit}
            disabled={code.trim().length < 4}
          >
            确定
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** 发现新版本（对应旧版 askyesno「发现新版本」） */
export function UpdateAvailableDialog() {
  const { available, setAvailable } = useUpdateAvailable()
  const { installUpdate, openExternal, frozen } = useApp()
  const [installing, setInstalling] = useState(false)
  if (!available) return null

  async function onInstall() {
    setInstalling(true)
    const ok = await installUpdate()
    if (!ok) setInstalling(false)
    // 成功后由 update:progress 事件接管界面
  }

  function onOpenPage() {
    if (!available) return
    // Release 页地址由 tag 拼出（open_external 校验 https）
    openExternal(`https://github.com/zimu5683/yikou-light-food-desktop/releases/tag/${available.tag}`)
    setAvailable(null)
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) setAvailable(null) }}>
      <DialogContent className="max-w-md rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">发现新版本 {available.tag}</DialogTitle>
          <DialogDescription className="max-h-40 overflow-y-auto whitespace-pre-wrap">
            {available.body}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            variant="ghost"
            className="h-8 text-xs"
            onClick={() => setAvailable(null)}
          >
            暂不更新
          </Button>
          {frozen && available.can_auto_install ? (
            <Button className="h-8 rounded-[6px] text-xs" disabled={installing} onClick={onInstall}>
              {installing ? '准备下载…' : '立即下载并安装'}
            </Button>
          ) : (
            <Button className="h-8 rounded-[6px] text-xs" onClick={onOpenPage}>
              打开 Release 页面
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** 更新下载进度（模态、不可关闭，对应旧版禁止关闭的进度窗） */
export function UpdateProgressDialog() {
  const { updateProgress } = useApp()
  if (!updateProgress) return null
  const percent =
    updateProgress.total && updateProgress.total > 0
      ? Math.min((updateProgress.downloaded * 100) / updateProgress.total, 100)
      : null
  const mb = (n: number) => `${(n / 1024 / 1024).toFixed(1)} MB`
  return (
    <Dialog open>
      <DialogContent
        className="max-w-sm rounded-lg [&>button]:hidden"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="font-serif">正在更新</DialogTitle>
          <DialogDescription>
            {updateProgress.stage}
            {updateProgress.downloaded > 0 && (
              <>
                ：已下载 {mb(updateProgress.downloaded)}
                {updateProgress.total ? ` / ${mb(updateProgress.total)}（${percent!.toFixed(0)}%）` : ''}
              </>
            )}
          </DialogDescription>
        </DialogHeader>
        {percent !== null ? (
          <Progress value={percent} className="h-2" />
        ) : (
          <Progress value={100} className="h-2 animate-pulse" />
        )}
        <p className="text-[11px] text-muted-foreground">
          下载完成后程序会自动关闭、替换并重新启动。
        </p>
      </DialogContent>
    </Dialog>
  )
}
