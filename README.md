# 一口轻食桌面程序

这是一个使用 Tkinter + Playwright + openpyxl 的订单处理桌面程序。账号密码不会写入源码；密码通过 Windows Credential Manager、macOS Keychain 或 Linux SecretService（GNOME Keyring/KWallet，`keyring`）保存；系统没有可用密钥环时退化为每次运行手动输入。

此项目是本人自用，代码功能不完善，还有许多需要改进的地方，项目公开，大家也可以以我项目为基础开发出更完整功能的项目。

程序包含两个任务模式：

- **订单处理**：登录管理后台，读取最新订单并写入排单 Excel；
- **闪时送下单**：从独立的《闪时送.xlsx》读取订单（午餐/晚餐两表），在闪时送平台逐单创建预约单。

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

## Linux 构建

普通用户可在 GitHub [Releases](https://github.com/zimu5683/yikou-light-food-desktop/releases/latest) 页面下载 `yikou-light-food-linux-x64.tar.gz`（x86_64 发行版，基于 glibc 2.35 构建，Ubuntu 22.04/Debian 12/Fedora 36 及更新版本可直接运行）。解压后执行：

```bash
tar -xzf yikou-light-food-linux-x64.tar.gz
chmod +x yikou-light-food
./yikou-light-food
```

Linux 版同样具备更新检查：发现新版本后会提示前往 GitHub Release 页面下载新的 tar.gz，按上面的步骤覆盖解压即可。

浏览器方面建议安装系统版 Microsoft Edge（`microsoft-edge-stable`）或 Google Chrome（`google-chrome-stable`），程序会自动识别；也可点击“安装 / 检查浏览器”下载 Playwright Chromium（需要系统已具备常见运行库，缺失时可参照 Playwright 文档安装依赖）。

开发者也可以在 Linux 上从源码构建：

```bash
./scripts/build_linux.sh
```

产物为仓库根目录的 `yikou-light-food-linux-<架构>.tar.gz` 及其 SHA-256 校验文件。推送版本标签后，GitHub Actions 会构建并自动附加到对应的 GitHub Release 下载页面。

## 数据与安全

配置、定位器配置与失败快照保存在用户配置目录（Windows：`%APPDATA%\yikou-light-food`），Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。

## 页面定位与网站改版适配

程序定位页面元素采用“候选链”策略：每一步按顺序尝试多个定位方式，第一个命中即用。候选按稳定性从高到低排列：

1. **URL 路由直达**（`goto` + `wait_url`）：直接打开目标页面路由，完全不依赖页面文字；
2. **DOM 结构与 ARIA 角色**（`css` / `role`）：依赖 Element UI 的渲染结构，不随文案变化；
3. **显示文字**（`text` / `text_re`）：最后兜底，支持正则与中英文多候选。

定位器配置在首次运行时自动生成到用户配置目录的 `locators.json`。网站改版导致定位失败时，程序会提示失败的步骤，并把**页面截图、HTML 快照和当前网址**保存到用户配置目录的 `logs/`；对照快照修改 `locators.json` 即可适配，无需改代码或重新打包。删除 `locators.json` 并重启程序会重新生成默认配置。

## 闪时送下单

切到「闪时送下单」页签后填写：闪时送网址、账号、密码、订单 Excel 文件，以及下单时的「商品名称」与「常用地址」默认值。

- 订单 Excel 格式：`午餐`、`晚餐`两个工作表，第 1 行表头、第 2 行占位，从第 3 行开始为 A=姓名、B=门牌号、C=电话、D=送达时间（D 列暂不使用，送达时间由程序按规则计算：午餐 11:00 / 晚餐 17:00，当天 16 点后顺延次日）。
- **登录需手动完成**：闪时送登录页有验证码，程序只自动填写账号密码，随后请在浏览器中手动完成验证码并点击登录；检测到登录成功后自动开始逐单下单。
- 闪时送平台的定位器独立保存在用户配置目录的 `sss_locators.json`，改版失败时同样会保存截图/HTML/网址到 `logs/`，修改该文件即可适配。
- 闪时送密码使用独立凭据名 `yikou-light-food-sss`，与管理后台账号密码互不覆盖。

- 候选字段：`css`（CSS 选择器）、`role` + `name`/`name_re`（ARIA 角色）、`placeholder`（输入框占位文字）、`text`（文字，子串匹配）、`text_re`（文字正则）、`has_text`/`has_text_re`（对结果按内含文字过滤）、`index`（取第 N 个匹配）
- 步骤字段：`goto`（URL 模板，`{base}` 为站点根）、`wait_url`（跳转后 URL 校验，Playwright glob）、`action`（`click`/`dblclick`）、`wait_networkidle`、`confirm: "table"`（等待订单表格渲染）

## 发布与更新

发布新功能前，请先修改 `app/__init__.py` 中的 `__version__`，然后创建并推送版本标签：

```powershell
git tag v1.1.0
git push origin main --tags
```

推送 `vX.Y.Z` 标签会触发 Windows、macOS 和 Linux 工作流，分别发布 `yikou-light-food.exe`、`yikou-light-food-macos.zip`、`yikou-light-food-linux-x64.tar.gz` 及其 SHA-256 校验文件。工作流会验证标签与应用内版本一致。应用启动时会在后台检查 GitHub Release；Windows 打包版可校验、下载并自动安装，macOS 与 Linux 用户收到提示后从 Release 页面下载新版安装包，源码运行模式也只提示前往 Release 页面。

更新包下载后会用官方 SHA-256 校验，校验文件优先从 GitHub 直接拉取；GitHub 直连不可达时改用 `latest.json` 内嵌的同一官方哈希（发布工作流会同时写入两处）。注意：若所使用的下载镜像被完全控制，内嵌回退在理论上可被绕过，彻底方案是为 exe 做代码签名。
