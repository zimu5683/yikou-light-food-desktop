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

生成的程序位于 `dist/一口轻食/`。首次运行点击“安装/检查浏览器”，或执行 `一口轻食.exe --install-browser`。需要联网下载 Playwright Chromium；若系统存在 Edge，运行时会优先使用 Edge。

## macOS 构建

```bash
./scripts/build_macos.sh
```

GitHub Actions 的 macOS runner 也会自动构建 `.app`。首次启动可能需要在“系统设置 → 隐私与安全性”允许应用运行。

## 数据与安全

配置保存在用户配置目录，Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。
