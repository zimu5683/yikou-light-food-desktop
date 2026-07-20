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

```bash
./scripts/build_macos.sh
```

GitHub Actions 的 macOS runner 也会自动构建 `.app`。首次启动可能需要在“系统设置 → 隐私与安全性”允许应用运行。

## 数据与安全

配置保存在用户配置目录，Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。

## 发布与更新

发布新功能前，请先修改 `app/__init__.py` 中的 `__version__`，然后创建并推送版本标签：

```powershell
git tag v1.1.0
git push origin main --tags
```

推送 `vX.Y.Z` 标签会触发 Windows 工作流构建并发布 `yikou-light-food.exe` 及其 SHA-256 校验文件。工作流会验证标签与应用内版本一致。应用启动时会在后台检查 GitHub Release；打包版发现新版本后可校验、下载并自动安装，源码运行模式只提示用户前往 Release 页面下载，避免误覆盖 Python 解释器。
