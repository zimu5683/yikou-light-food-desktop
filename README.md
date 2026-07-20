# 一口轻食桌面程序

这是一个使用 Tkinter + Playwright + openpyxl 的订单处理桌面程序。账号密码不会写入源码；密码通过 Windows Credential Manager 或 macOS Keychain（`keyring`）保存。

## 开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python run.py
```

也可以在 Visual Studio 中打开仓库目录，将 `run.py` 设为启动文件并使用 Python 调试器。

## Windows 构建

```powershell
.\scripts\build_windows.ps1
```

生成的程序位于 `dist/yikou-light-food.exe`。首次运行可点击“安装 / 检查浏览器”，或执行 `yikou-light-food.exe --install-browser`。需要联网下载 Playwright Chromium；若系统存在 Edge 或 Chrome，运行时会优先使用系统浏览器。浏览器文件安装在当前用户的 Playwright 缓存目录中，不会写入程序目录。

## macOS 构建

普通用户可在 GitHub [Releases](https://github.com/zimu5683/yikou-light-food-desktop/releases/latest) 页面下载 `yikou-light-food-macos.zip`。解压后将 `yikou-light-food.app` 拖入“应用程序”目录即可运行。当前下载包适用于 Apple 芯片（M1/M2/M3/M4 等）Mac；首次打开若被 macOS 拦截，请右键应用选择“打开”，或前往“系统设置 → 隐私与安全性”允许运行。

开发者也可以在 macOS 上从源码构建：

```bash
./scripts/build_macos.sh
```

推送版本标签后，GitHub Actions 会构建 `.app`，打包为 `yikou-light-food-macos.zip`，并自动附加到对应的 GitHub Release 下载页面。

## 数据与安全

配置保存在用户配置目录，Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。

## 发布与更新

发布新功能前，请先修改 `app/__init__.py` 中的 `__version__`，然后创建并推送版本标签：

```powershell
git tag v1.1.0
git push origin main --tags
```

推送 `vX.Y.Z` 标签会触发 Windows 和 macOS 工作流，分别发布 `yikou-light-food.exe`、`yikou-light-food-macos.zip` 及其 SHA-256 校验文件。工作流会验证标签与应用内版本一致。应用启动时会在后台检查 GitHub Release；Windows 打包版可校验、下载并自动安装，macOS 用户收到提示后从 Release 页面下载新版 ZIP，源码运行模式也只提示前往 Release 页面。
