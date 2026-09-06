# 前端重写进度快照（2026-09-06 · M7/M8 完成，v3.0.1 已发布）

## 当前状态：M0–M5 全部完成，运行层稳定，M6 大部分完成

## 已完成并验证

- **M0 环境**：Node 24 / pnpm / pywebview（venv 内 **5.4**，requirements 写 `>=5.4,<6`）；skill 在 `.zcode/skills/`。
- **M1 设计**：「全麦早市 Wheat Press」token 冻结于 `design/DESIGN-WEB.md` §5；mock 与验收截图在 `design/mocks/preview/`。
- **M2 桥接层**：`app/bridge.py`（js_api + 13 种事件 + 阻塞决策异步化 + request_close 选择处理 + update:installed 后自毁）。
- **M3 前端工程**：`frontend/`（React18+TS+Tailwind4+shadcn 15 组件）；singlefile 产物；`vite.config.ts` base './'。
- **M4/M5 完整业务 UI**（本次完成）：
  - `src/components/TitleBar.tsx` 自绘标题栏（印章/宋体/版本徽章/主题切换/窗口控制/pywebview-drag-region）
  - `src/components/TaskPanel.tsx` 双模式表单（下划线 tab、字段三态、日期选择器仅今天/过去、Stepper 1–9999、Switch、吸底操作条 dock、更多菜单、停止/清除密码确认对话框）
  - `src/components/LogConsole.tsx` 小票日志控制台（锯齿边、结构化条目、即输即滤+命中高亮、复制/清空/自动滚动、状态徽章 7 态、页脚印章）
  - `src/components/dialogs.tsx` 决策弹窗/发现新版本/下载进度（不可关）/Toaster
  - `src/hooks/useApp.tsx` 事件分发与动作中心（含验收回传 frontend_report）
- **运行层根因链（最终结论）**：
  1. 裸绝对路径会被 pywebview 改写到内部 HTTP 服务，GNOME 代理环境下页面空白 → **必须用 `Path.as_uri()`**（另一 AI 修复，已验证）。
  2. WebKitGTK 2.52 渲染沙箱在受限父进程（CI/Xvfb/Agent Shell）下静默杀死渲染进程 → `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1`（旧变量 FORCE_SANDBOX 已失效）。
  3. DMABUF 黑屏 → `WEBKIT_DISABLE_DMABUF_RENDERER=1`。三者均已写入 `webview_app.py`。
  4. `evaluate_js` 在此栈上不可信 → **自动化验收必须走 `frontend_report` 回传通道**（`useApp.tsx` 握手后自动回传）。
- **M6 打包/CI（另一 AI 完成）**：spec 打包 frontend、三平台构建脚本加 pnpm build、release.yml 加 Node/pnpm、README 依赖说明、`tests/test_webview_app.py`。

## 验收数据

- `frontend_report` 回传通道：4/4 稳定（真实窗口、frameless、file://）
- 真实应用像素验收截图：`design/mocks/preview/acceptance-real-light.png`（真实配置/更新检查日志/状态徽章实时流转）
- Playwright 状态矩阵：`/tmp/ui_shots/`（浅/深/闪时送/堆叠，5 张）
- pytest 103 passed 4 skipped；ruff 全绿；compileall OK

## 用户实测反馈修复（2026-09-06 第二轮）

1. **窗口无法拖动**：pywebview 5.4 GTK 在 frameless+easy_drag=False 时不注册任何拖拽处理器（源码 171-177 行），`pywebview-drag-region` CSS 类在 GTK 后端无效 → 新增 `bridge.begin_window_drag(x,y)`：前端 mousedown 传 screenX/Y，Python 经 `BrowserView.instances[uid].window.begin_move_drag()` 交还窗口管理器。注意 GTK 时间戳是 32 位毫秒，系统纪元毫秒需 `& 0xFFFFFFFF`。
2. **日志成对丢行**：`evaluate_js`（阻塞+信号量实现）在 WebKitGTK 并发/连发时结果投递被静默吞掉 → **整体改回旧 Tkinter 验证过的「事件队列+前端轮询」模式**：`bridge._emit_event` 入队（cap 500），前端每 150ms `drain_events()` 拉取。压测 25 条连发全部到达（队列清零）。decision/update/status 也全走该通道（decision 若用 evaluate 推送，丢了会让 worker 永久阻塞）。
3. **Stepper 无法编辑**：受控输入把空值立刻钳回 1，Backspace 失效 → 改为草稿态（输入时允许任意数字/清空，失焦或 Enter 提交钳位）。
4. **闪时送安全测试**：`start_sss` 增加 `dry_run` 透传（payload.dry_run → config.sss_dry_run，只组装报文不真实提交）。注意：SSS 登录需要人工在最小化浏览器窗口输入图形验证码（平台安全设计），自动化无法替代，测试时需用户配合输入一次。
5. pywebview `window.gui` 在 5.x/6.x 都是平台模块；GTK 渲染器实例要用 `BrowserView.instances[window.uid]` 获取。

## 用户实测反馈修复（2026-09-06 第三轮）

1. 删除日志行「接口读取外送订单 N 笔（新单在前）」（automation.py:704，用户要求不保留）。
2. 日志文字可鼠标选中：body 全局 select-none 的例外——小票纸与输入框加 select-text。
3. 订单摘要多行排版：`W8|李|电话|地址|餐品` 前端渲染为每字段一行，订单编号（首行）全麦棕加粗；判定规则 `/^W\d+\s*\|/`（"W6 处理失败：…" 等非摘要行不受影响）；"复制日志"输出跟随多行格式。automation.py 的 `_format_order_summary` 未动（业务模块零改动原则）。
4. 订单处理浏览器窗口最小化：automation.py 新增 `_minimize_window`（与 sss.py 同一 CDP 方案，best-effort），在 `browser.new_page()` 后调用，登录全程最小化。

## 用户实测反馈修复（2026-09-06 第四轮）

- **订单多行排版没生效的根因**：automation 的 `_format_order_summary` 用**全角"｜"（U+FF5C）**连接字段，前端正则只匹配半角"|"，判定永远不命中 → 正则改为 `/^W\d+\s*[｜|]/`、split 改 `/[｜|]/`。教训：对接业务模块的文本输出前，先确认实际字符（截图里全角竖线视觉上与半角几乎一样）。
- 浏览器最小化失败不再静默：`_minimize_window` 失败时输出「浏览器最小化失败（不影响任务）：原因」到日志，便于远程诊断。
- 坑：bash 无引号 heredoc 会展开 `${...}`（曾把 JS 模板字符串打穿），含模板字符串的补丁必须用 Edit 工具或引号 heredoc。

## M7/M8 完成记录

- **v3.0.1 已通过 CI 发布**：Windows/Linux/macOS 三平台产物 + v2.2.0→3.0.1 增量补丁 + latest.json 清单全部就位，老用户自动更新已可收到。
- CI 修了两处（commit 94fc576 / 9b60de7）：Linux 构建改用系统 Python + apt GTK 绑定（setup-python 无法 import gi）；file URI 断言用 url2pathname 兼容 Windows 盘符。上传补丁时遇到过一次瞬时网络错误，重跑 failed jobs 后成功。
- 备注：仓库还有旧解压目录 `/home/zimu/下载/yikou-light-food-linux-x64/`（9 月 1 日的旧二进制），用户实际运行的程序以其启动路径为准。

- M7：功能对照验收清单见 `design/M7-ACCEPTANCE.md`（26 项；真实订单流程/日志完整性由用户两轮实测确认）。
- M8：已删除 `app/gui.py`、`app/design_system.py`、`tests/test_design_system.py`；`main.py` 移除 --legacy；`DESIGN.md` 归档至 `design/legacy/`；release.yml 冒烟清单换成 bridge/webview_app/excel_templates；spec 与 requirements 移除 Pillow/Tkinter 残留；版本号 **3.0.1**。

## 剩余事项（M7/M8）

1. M7 功能对照验收：按探索报告 23 项对照表逐项人工/自动化核对（重点：真实任务跑一轮订单处理 + 闪时送、更新下载全流程、决策弹窗、关闭保护）。
2. M8 清理（需用户验收后确认）：删 `app/gui.py`、`app/design_system.py`、`tests/test_design_system.py`；移除 `main.py --legacy`；DESIGN.md 归档 design/legacy；版本号 3.0.0；release.yml 冒烟 import 清单换新模块。
3. 已知行为简化（记录在案）：浏览器缺失不再弹 Edge/Chrome 询问，直接打开 Edge 下载页 + Toast；更新失败不再弹窗询问打开 Release（Toast 提示 + 可从更多菜单重新检查）。
4. 所有改动**未提交 commit**——建议在 M7 验收通过后由用户确认提交。
