# 一口轻食桌面程序

这是一个使用 pywebview（React + TypeScript + Tailwind 前端）+ Playwright + openpyxl 的订单处理桌面程序。账号密码不会写入源码；密码通过 Windows Credential Manager、macOS Keychain 或 Linux SecretService（GNOME Keyring/KWallet，`keyring`）保存；系统没有可用密钥环时退化为每次运行手动输入。

此项目是本人自用，代码功能不完善，还有许多需要改进的地方，项目公开，大家也可以以我项目为基础开发出更完整功能的项目。

程序包含三个任务模式：

- **订单处理**：登录管理后台，读取最新订单并写入排单 Excel；
- **云文档同步**：把本地排单表的内容增量写入 WPS 云端的排单表（见下文）；
- **闪时送下单**：从独立的《闪时送.xlsx》读取订单（午餐/晚餐两表），在闪时送平台逐单创建预约单。

默认使用**纯接口模式**：不启动浏览器，直接调用平台 HTTP 接口完成登录、读单和下单。
界面里可关闭“纯接口模式”开关，回退到原来的 Playwright 浏览器模式作为备用。

## 开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/fetch_browser.py    # 抓取并精简内置 Chromium 到 vendor/browser/
python run.py
```

`fetch_browser.py` 只做一次即可；它优先复用本机 Playwright 缓存，缺失时才下载。开发态会依次在仓库根 `browser/` 和 `vendor/browser/` 中查找内置 Chromium，也可以用环境变量 `YIKOU_BROWSER_DIR` 指向别处。

也可以在 Visual Studio 中打开仓库目录，将 `run.py` 设为启动文件并使用 Python 调试器。

## Windows 构建

```powershell
.\scripts\build_windows.ps1
```

构建产物有两份：`dist/yikou-light-food.exe`（单文件可执行程序）和 `dist/yikou-light-food-windows-x64.zip`（**首次安装请分发这个**）。zip 内是 exe 与内置 Chromium 目录：

```
yikou-light-food.exe
browser/
  browser.json
  chromium-<revision>/chrome-win64/chrome.exe
```

解压后**必须保持 `browser/` 与 exe 同级**，自动化会固定调用这份内置 Chromium，不再探测系统 Edge/Chrome，也不会在运行时下载浏览器。可执行 `yikou-light-food.exe --check-browser` 做自检，它会打印内置 Chromium 的路径与版本。

增量更新只替换 exe，`browser/` 目录保持不动，因此日常小版本升级仍然只下载差分补丁。

## macOS 构建

普通用户可在 GitHub [Releases](https://github.com/zimu5683/yikou-light-food-desktop/releases/latest) 页面下载 `yikou-light-food-macos.zip`。解压后将 `yikou-light-food.app` 拖入“应用程序”目录即可运行。当前下载包适用于 Apple 芯片（M1/M2/M3/M4 等）Mac；首次打开若被 macOS 拦截，请右键应用选择“打开”，或前往“系统设置 → 隐私与安全性”允许运行。

开发者也可以在 macOS 上从源码构建：

```bash
./scripts/build_macos.sh
```

推送版本标签后，GitHub Actions 会构建 `.app`，打包为 `yikou-light-food-macos.zip`，并自动附加到对应的 GitHub Release 下载页面。

## Linux 构建

普通用户可在 GitHub [Releases](https://github.com/zimu5683/yikou-light-food-desktop/releases/latest) 页面下载 `yikou-light-food-linux-x64.tar.gz`（x86_64 发行版，基于 glibc 2.35 构建）。Linux 版本使用系统 GTK 3 + WebKitGTK 4.0/4.1 作为 pywebview 渲染内核；Ubuntu/Debian 通常需要先安装对应运行库。从 v3.0.2 起，Linux 打包不再捆绑 GTK/GLib/C++ 运行库，运行时直接使用当前系统的对应库，避免在新版发行版上因捆绑旧库导致 WebKit 启动失败：

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

部分较旧发行版将最后一个包命名为 `gir1.2-webkit2-4.0`。程序启动时会检查前端文件和图形后端，缺少依赖会显示明确错误，而不是打开空白窗口。满足这些依赖后，Ubuntu 22.04、Debian 12 及更新版本可按下列方式运行：

```bash
tar -xzf yikou-light-food-linux-x64.tar.gz
chmod +x yikou-light-food
./yikou-light-food
```

解压后当前目录下会同时得到可执行文件与内置的 `browser/` 目录，两者必须放在一起。自检命令：`./yikou-light-food --check-browser`。

Linux 打包版同样支持自动更新：启动时会后台检查 GitHub Release，发现新版本后可以直接下载、校验并自动替换重启（需程序所在目录可写，失败时仍会引导前往 Release 页面手动下载）。

浏览器方面无需在系统里安装 Edge/Chrome：发行包已经内置 Chromium，自动化固定使用它。若内置 Chromium 因缺少系统运行库而无法启动，可参照 Playwright 文档安装 Chromium 的依赖项。

开发者也可以在 Linux 上从源码构建：

```bash
./scripts/build_linux.sh
```

产物为仓库根目录的 `yikou-light-food-linux-<架构>.tar.gz` 及其 SHA-256 校验文件。推送版本标签后，GitHub Actions 会构建并自动附加到对应的 GitHub Release 下载页面。

## 云文档同步（WPS 云端排单表）

「云文档同步」页签把本地《排单.xlsx》的内容**增量写入**协作者维护的 WPS 云端排单表，
不需要整表覆盖，因此云端的公式、自定义排序、字体和列宽都不会被破坏。

### 它做什么

对本地排单表的每个子表（东湖/衣锦/医学院 × 中餐/晚餐）：

1. 在云端表里按内容定位**目标日期列**（只比对「月.日」，忽略星期文字）；
   **只在「电话列 ~ 类型列」之间的日期区里找** —— 备注右侧协作者写的
   「9.14 周一」是标记，不会被当成日期列，也不会被写进任何数字；
2. 按【名字 + 电话】找到客户所在行；
3. 旧客户：把**目标日期格写 1**，**总餐次写成与本地「餐次」一致**；
4. 新客户：
   1. 把这一批新客户**统一插到第 3 行与第 4 行之间**（插入的行会自动继承上方格式）；
   2. 填名字/地址/电话/类型/餐种，并按模板行刷底色（经济餐照抄模板、豪华餐整行金黄）；
   3. 按**列B 的地址顺序重排整张表**（见下），新客户落在自己地址组里；
   4. 再写目标日期格 = 1、总餐次、`已出餐 = SUM(日期列)`、`剩余餐 = 总餐次 − 已出餐`；
5. 可选：在备注列右侧第 3 列写协作者的**通讯记号**（周日 1、周一 2 … 周六 7）。
   备注+2 那格是协作者自己的日期标记，程序只读不写。

**总餐次写的是绝对值而不是累加值**，所以同一天重复上传不会翻倍——
云端已经是目标状态时，程序一个格子都不会写。

### 地址顺序（列B 的排列规则）

新增客户时程序会把整张表按列B 重排。顺序在「云文档同步」页签的**地址排序**里配置：
一行一个地址，从上到下就是顺序；**留空 = 按地址升序排列**（医学院用这种，
`医2号` 会排在 `医10号` 前面）。

| 子表 | 默认顺序 |
|---|---|
| 东湖中餐 / 东湖晚餐 | 小、大西、A1~A6、b1~b12、C1~C12、D1~D12 |
| 衣锦中餐 / 衣锦晚餐 | 外卖柜、校门口 |
| 医学院中餐 / 医学院晚餐 | （留空）按地址升序 |

- 清单里没有的地址（例如「学三」「碳汇楼」）**统一排到表格最后面**，并在预览里列出来；
- 匹配地址时忽略大小写和空格（本地写「B5」会按清单落成「b5」）；
- 同一个地址组里，**原有客户在前、本次新增的在后**；
- 排序只覆盖「第 3 行 ~ 最后一个有姓名的行」，表尾的合计/说明不会被移动；
- 排序用的是**云端原地排序**（保留格式与公式）+ 一列临时排序键，排完即删；
- 排序完成后程序会**回读列A~C 重新定位每个人的新行号**再写数据，绝不按预测行号硬写。


### 目标日期怎么算

网站 21:00 截止、程序通常在晚上运行，因此：

| 运行时刻 | 写入云端哪一列 |
|---|---|
| 20:00 ~ 次日 10:00 | **运行日 + 1 天** |
| 其它时刻 | 运行日 |

窗口起止小时可在配置里调整（`wps_target_hour_start` / `wps_target_hour_end`）。

### 首次使用

1. 「云文档同步」页签 → **去授权**，在浏览器里用你的 WPS 账号确认一次；
   token 由 CLI 存进系统密钥链，约一年内无需重复授权；
2. 保持**测试模式**（默认开启），点「预览」确认要改的内容；
3. 确认无误后点「确认上传」，去云端核对结果；
4. 核对通过后关闭测试模式，正式启用。

**测试模式**下每张正式表都会被替换为对应的测试副本（`vendor` 之外，云端名称以「测试-」开头），
所以测试期间绝不会碰正式排单表；测试模式也不写协作者通讯记号。

### 安全边界

- 上传前必须先「预览」，确认按钮在预览成功前不可点；
- 找不到目标日期列 → 不写、不建列，只提示（通常是协作者还没加当天的列）；
- 写入后逐格回读校验（含"这个行号上确实是这个人"），校验不通过则报告并不更新本地账本；
- 排序失败 → 把插进去的新行整块删掉，云端恢复原状；
  **排序成功之后**的一步失败则不再删行（新行已散落到各地址组，删行会删错人），
  只报告并提示重新上传——重复执行是安全的；
- 本地账本在用户配置目录的 `wps_sync_state.json`，只用于留痕与状态展示；
- 任何失败都只写日志，**不会影响本地排单任务**。

### 组件来源

云文档读写依赖金山官方 CLI `kdocs-cli`（无 PyPI 包，只能随包分发）。
构建脚本会调用 `scripts/fetch_kdocs_cli.py` 按平台下载并用官方 `checksums.txt`
校验 sha256；运行时按「打包内置 → 仓库 vendor → 程序同目录 → PATH」顺序查找，
也可以在页签里手动指定路径。详情见 `vendor/kdocs-cli/README.md`。

## 数据与安全

配置、定位器配置与失败快照保存在用户配置目录（Windows：`%APPDATA%\yikou-light-food`），Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。

## 上下文自适应压缩（诊断产物预算）

失败快照（`logs/` 下的截图 + HTML + 网址三元组）与 `update.log` 都是「只增不减」的，
长期使用会越堆越多。程序内置一套**预算 + 逼近触发 + 自适应压缩**机制，把这两类
产物的总占用稳定在 **100 MiB** 以内。

预算**只统计这两类受管产物**：`webview/`（WebKit 的 localStorage 与缓存）、
`config.json`、`wps_sync_state.json` 等既不在处置范围内，也不计入预算——否则一笔
压不下去的占用会让程序误判“超标”，删掉本可保留的证据却永远达不成目标。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `budget_bytes` | 100 MiB | 受管产物的硬预算 |
| `trigger_ratio` | 80% | 占用达到 80 MiB（逼近阈值）才开始整理 |
| `target_ratio` | 60% | 整理后尽量降到 60 MiB 以下，留出滞回余量 |
| `fresh_keep` | 5 | 最近 5 组现场**永不压缩**，随时可直接打开 |
| `archive_keep` | 24 | `logs/archive/` 内保留的归档数量上限 |
| `log_file_bytes` | 8 MiB | 单个日志文件超过该大小就轮转 |

整理动作严格按**信息损失从小到大**分档执行：

1. **轮转超大日志**（gzip 后截断）—— 无损；
2. **淘汰超出 `archive_keep` 的既有归档** —— 有损，只动最旧的；
3. **把证据组压成 `logs/archive/<时间戳>_snapshot.tar.gz`** —— 无损；
4. **继续淘汰最旧的归档** —— 有损；
5. **直接清理最旧的候选证据组** —— 有损兜底。

关键在于**有损档位只在真的突破 100 MiB 时才启动**：压缩不要钱，丢数据要命。
实测 140 MiB 的历史现场会被压到 81 MiB，而**一条现场都不会丢**（40 组全部保留：
5 组明文最新现场 + 24 组归档 + 其余按策略留存）。第 3 档由新到旧归档、第 5 档由
旧到新清理，两端合起来保证**越新的现场越先被保住**。

安全边界：归档先写 `.tmp`、重新打开逐成员核对**解压后大小**、`os.replace` 原子
落位，**校验通过后**才删原文件；不跟随符号链接，不触碰 `logs/` 与 `update.log`
之外的任何文件（`config.json`、`webview/` 的 localStorage 等一律不动）；任何失败
只写日志，绝不影响排单/下单主流程。

程序启动时会在后台自动整理一次（失败或异常都不影响启动）。手动查看与整理：

```bash
python -m app.main --compact-artifacts --dry-run   # 只看计划，不动文件
python -m app.main --compact-artifacts             # 真正执行
```

## 页面定位与网站改版适配

程序定位页面元素采用“候选链”策略：每一步按顺序尝试多个定位方式，第一个命中即用。候选按稳定性从高到低排列：

1. **URL 路由直达**（`goto` + `wait_url`）：直接打开目标页面路由，完全不依赖页面文字；
2. **DOM 结构与 ARIA 角色**（`css` / `role`）：依赖 Element UI 的渲染结构，不随文案变化；
3. **显示文字**（`text` / `text_re`）：最后兜底，支持正则与中英文多候选。

定位器配置在首次运行时自动生成到用户配置目录的 `locators.json`。网站改版导致定位失败时，程序会提示失败的步骤，并把**页面截图、HTML 快照和当前网址**保存到用户配置目录的 `logs/`；对照快照修改 `locators.json` 即可适配，无需改代码或重新打包。删除 `locators.json` 并重启程序会重新生成默认配置。

## 闪时送下单

切到「闪时送下单」页签后填写：闪时送网址、账号、密码、订单 Excel 文件（云端模式下是**留档文件**，可留空），以及下单时的「商品名称」与「常用地址」默认值。

### 名单来源

- **云端当天名单（默认）**：每次「开始下单」（含干跑/预检）之前，程序从 WPS 云端的
  《东湖午餐9月.xlsx》（内部名「东湖中餐」）与《东湖晚餐9月.xlsx》（「东湖晚餐」）里，
  取出**当天那一列**标了 `1` 的人，姓名/地址/电话直接进内存用于下单（不再从 Excel 读单）。
  - 「当天」的口径：**运行时刻 20:00 之后 → 识别次日**；其余时刻（含次日 00:00~10:00 的清晨）
    → 识别运行日。例：9.15 20:00 ~ 9.16 10:00 之间运行，识别的都是 `9.16`，下 9.16 的中午/晚上的单。
  - **地址是「大西」或「小」的人不下单**（含「小西」「带空格」等写法，忽略空格与大小写），其余地址照常下单。
  - 某张表**没有当天日期列**（例如周末不做晚餐）→ 那一餐不下单、另一餐照常，日志写明原因。
  - **下单前核对日期**：识别到的日期必须等于本次实际使用的送达日期（午餐 11:00 / 晚餐 17:00，
    当天 16 点后顺延次日）；不一致（例如 16:00~20:00 之间运行）→ **拒绝下单**，不登录、不提交。
  - 名单会**留档**写进《闪时送.xlsx》的 `午餐`/`晚餐` 两表（先清空 A:C 第 3 行起，电话按文本写入），
    并在 **E1** 写云端该日期列的表头原文（形如 `9.16 周三`）。留档失败只告警，不影响下单。
  - 云端读不到（未授权 / 缺 `kdocs-cli` / 当日额度用尽 / 表头异常）或要下单的人数据不完整
    （缺姓名、地址，或电话不是 11 位）→ **拒绝下单**并说明原因，提示改用「本地 Excel」人工下单。
    报错会**点名是哪个单元格、现在是什么值**（如「东湖中餐 第 106 行 刘卓雅：C106 现在是「0」，
    不是 11 位手机号」），照着去云端改即可；改完仍报错时先确认改的是云文档本身、且已保存同步
    （共享文档只有查看权限时改动不会生效）。
  - 每次下单固定 **4 次只读调用**（每张表 2 次），与云同步共用金山接口的每日额度（约 150~200 次，次日 08:00 恢复）。
  - 页签上的「读取云端当天名单」按钮可以只读取 + 留档（不下单），用于下单前先核对人数与日期。
- **本地 Excel**：旧行为，直接读《闪时送.xlsx》的名单（人工准备名单时的兜底），不做云端读取。
  - Excel 格式：`午餐`、`晚餐`两个工作表，第 1 行表头、第 2 行占位，从第 3 行开始为 A=姓名、B=门牌号、C=电话、D=送达时间（D 列暂不使用，送达时间由程序按规则计算；云端模式留档时 E1 = 当天日期）。
- 只读自检（不写云端、不写本地文件）：`python -m app.main --sss-import-check` 会打印
  `kdocs-cli` 路径/授权状态、生效目标表、当天日期、两张表当天列标 1 的人数与地址过滤后的人数。
- **登录需手动完成**：闪时送登录有图形验证码。纯接口模式下程序会在应用内弹出验证码小窗，输入后自动完成登录并逐单下单，不再弹出浏览器；浏览器备用模式下仍是在浏览器中手动输入验证码。
- 闪时送平台的定位器独立保存在用户配置目录的 `sss_locators.json`，改版失败时同样会保存截图/HTML/网址到 `logs/`，修改该文件即可适配。
- 下单默认采用 **at-least-once + 对账确认**：平台抓包报文未发现客户端幂等字段，因此不会向未知 schema 强塞字段。程序在下单前后查询站内订单、校验完整订单指纹和批次时间窗口，网络异常或超时不会自动重发 POST；如果平台后续确认支持幂等字段，可在配置中设置 `sss_idempotency_field` 启用稳定 `client_request_id`。
- 闪时送密码使用独立凭据名 `yikou-light-food-sss`，与管理后台账号密码互不覆盖。

- 候选字段：`css`（CSS 选择器）、`role` + `name`/`name_re`（ARIA 角色）、`placeholder`（输入框占位文字）、`text`（文字，子串匹配）、`text_re`（文字正则）、`has_text`/`has_text_re`（对结果按内含文字过滤）、`index`（取第 N 个匹配）
- 步骤字段：`goto`（URL 模板，`{base}` 为站点根）、`wait_url`（跳转后 URL 校验，Playwright glob）、`action`（`click`/`dblclick`）、`wait_networkidle`、`confirm: "table"`（等待订单表格渲染）

## 发布与更新

发布新功能前，请先修改 `app/__init__.py` 中的 `__version__`，然后创建并推送版本标签：

```powershell
git tag v1.1.0
git push origin main --tags
```

推送 `vX.Y.Z` 标签会触发 Windows、macOS 和 Linux 工作流，分别发布 `yikou-light-food.exe`、`yikou-light-food-macos.zip`、`yikou-light-food-linux-x64.tar.gz` 及其 SHA-256 校验文件。工作流会验证标签与应用内版本一致。应用启动时会在后台检查 GitHub Release；Windows、Linux 与 macOS 打包版均可校验、下载并自动安装（macOS 自用模式允许未签名更新，但仍强制校验签名清单与 SHA-256），源码运行模式只提示前往 Release 页面。Linux 自动更新会把新版 tar.gz 解压到程序目录下的 `.yikou-light-food.update-<pid>/` 暂存目录，待本进程退出后由后台脚本原子替换可执行文件并重启；macOS 会替换整个 `.app` bundle 并重启；安装目录不可写时回退为提示手动下载。

更新真实性不再依赖 SHA-256 或镜像：发布工作流用 Ed25519 私钥对 `latest.json` 生成 `latest.json.sig`，客户端只使用内置公钥（`app/updater.py` 中的 `UPDATE_MANIFEST_PUBLIC_KEY`）验证通过的清单。SHA-256 仅用于完整性校验；镜像只负责传输字节流，不能成为信任根。更新器严格拒绝降级、同版本覆盖、非 SemVer 版本、平台/架构不匹配和超过大小限制的资源。

发布仓库需要配置 GitHub Actions Secret `UPDATE_SIGNING_KEY`（Ed25519 PKCS#8 PEM 私钥内容）；可用 `python scripts/generate_update_signing_key.py` 生成并妥善备份，再执行：

```bash
gh secret set UPDATE_SIGNING_KEY < ~/.config/yikou-light-food/update-signing-key.pem
```

未配置时 `publish-manifest` 任务会失败，不会发布未签名清单。Windows/macOS 打包版还会分别校验 Authenticode 发布者与 codesign/Team ID/公证；这些公开信任锚在发布时由仓库变量 `YIKOU_WINDOWS_AUTHENTICODE_PUBLISHER` / `YIKOU_MACOS_TEAM_ID` 写入随包分发的 `app/update_trust.json`。本项目当前为个人自用，`app/update_trust.json` 中显式设置 `"allow_unsigned_update": true`：没有 Authenticode/Team ID 时跳过 OS 发布者签名校验，但 **Ed25519 清单签名、SHA-256、版本/平台/架构校验仍然强制**。如果未来要公开发布，配置真实证书并把该开关改为 `false` 即可恢复 fail-closed。新清单通过 `requires_platform_metadata` 强制每个平台资源声明 `platform`/`architecture`，不再允许“缺少字段就放行”。Linux 更新包只允许单个 `yikou-light-food` 文件，拒绝夹带额外文件或 setuid 位。替换前会运行新产物的 `--self-check`；替换后新 GUI 必须写入启动健康标记，超时未写入会自动恢复上一版并重启。更新过程会写入用户目录的 `update.log`，便于排查“已下载但仍是旧版本”一类问题。Windows 与 Linux 有上一版可对照时，发布工作流会生成 bsdiff 差分补丁：更新器按本地文件的 SHA-256 匹配基线，命中则只下载补丁还原出新版，未命中自动回退全量下载。

提交前建议额外运行一次工作区卫生检查（含未跟踪文件）：

```bash
python scripts/check_workspace_hygiene.py --working-tree
```
