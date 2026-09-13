# 交接文档：把本地排单表同步到 WPS 云端

> 写给接手这个任务的 AI。请先完整读完本文再动代码。
> 最后更新：2026-09-12 · 项目：`/home/zimu/文档/yikou-light-food-desktop`

---

## 0. 一句话目标

用户的桌面程序（Python + pywebview + React）每天处理完外卖订单后，会写出一张本地 Excel《排单.xlsx》。
现在要让程序**把这张表的内容自动写进 WPS 云端的 6 张排单汇总表**（协作者在维护的共享文档）。

---

## 1. 用户的核心需求（原话整理，按优先级）

### 1.1 数据同步规则（已实现并验收通过）

对每个本地子表 → 对应的云端表：

1. **按【名字 + 电话】匹配客户**（两者都对上才算同一个人）；
2. **旧客户**：把「目标日期」那一格写 `1`；**总餐次写成与本地「餐次」列相同的值**
   - ⚠️ 是**绝对值**，不是累加！（用户明确："12 餐肯定是多的，他只下了 6 餐"）
   - 绝对值写法天然幂等：重复运行不会翻倍
3. **新客户**：追加到表尾，填：名字 / 地址 / 电话 / 类型 / 餐种 / 总餐次 / 目标日期格=`1`
   - **其它日期格留空**（用户原话："没有写 0 的必要，公式会自动跳过空格"）
   - **备注列不写**
4. **已出餐 / 剩余餐不碰**（云端有公式，会自己重算）

### 1.2 目标日期怎么定（已实现）

网站每天 21:00 截止（有时 20:00），用户总在晚上跑程序：

| 运行时刻 | 写入哪一列 |
|---|---|
| **20:00 ~ 次日 10:00** | **运行日 + 1 天** |
| 其它时刻 | 运行日 |

- 日期列**只比对「月.日」，忽略星期文字** —— 实测协作者把 `9.16` 写成「9.16周一」（实际是周三），认星期会找错列。
- 窗口起止小时可在配置里调（`wps_target_hour_start` / `wps_target_hour_end`）。

### 1.3 通讯记号（已实现）

云端表右侧（**备注列右边第 2 列**）有一个「协作者通讯记号」：

- 用户每次上传后要写一个**周几数字**：**周日=1、周一=2、周二=3 … 周六=7**
- 位置：`备注列 + 2`（实测 6 张表一致）
- **测试模式下一律不写**

### 1.4 新客户行的格式（**当前半成品，最需要你接手**）

用户要求：

| 项 | 要求 |
|---|---|
| 字体、字号、对齐 | **跟表格内原有数据行一致**（不要写死，要从表里"学"） |
| **经济餐底色** | **不改任何底色**（用户原话："我不需要填写绿色的底色，这个不用管"） |
| **豪华餐底色** | **整行金黄** `#FFFFC000`（与「总餐次」列同色） |
| 边框 | 做不到就算了（接口不支持） |

**用户指认的样板行**：东湖中餐的**第 99 行「哒哒」**，其颜色分布是：

```
A~J 列（名字…餐种）= 白色 #FFFFFFFF
K~M 列（总餐次/已出餐/剩余餐）= 金黄 #FFFFC000
N 列（备注）= 白色 #FFFFFFFF
```

⚠️ **重点坑**：这张表的数据行底色**并不统一** —— 实测 3~99 行里
**73 行白底、24 行绿底（#FF92D050）**。我曾用"第一行"当样板，结果把 25 个新行
全涂成了绿色，用户很不满（"你颜色涂的什么玩意儿，我至始至终都只用到白黄两种颜色"）。

**正确做法**：统计多数派底色 → 取多数派里最靠近表尾的行当样板 → 逐列照抄它的底色。

---

## 2. 表映射关系

| 本地子表 | 云端正式表 | file_id |
|---|---|---|
| 东湖中餐 | 东湖午餐9月.xlsx | `fr2FrpFVMrM8poHaSHFK1xAsmSSBMspye` |
| 衣锦中餐 | **校门口**午餐9月.xlsx | `amqzgcXVMrMWopCdyxkJrxnqzcQ8UbaJG` |
| 医学院中餐 | 医学院午餐9月.xlsx | `qvuPzdurK1MyKwL2weiZ1xF8RzdijFdaJ` |
| 东湖晚餐 | 东湖晚餐9月.xlsx | `noJa93Xq9xM62hfVqj6UrxEmgLjAHmqka` |
| 衣锦晚餐 | **校门口**晚餐9月.xlsx | `mAc5hDw1q1MV33kW8JE7xxnnW1KEdYc1V` |
| 医学院晚餐 | 医学院晚餐9月.xlsx | `pPpnGwh9ERxMRByaRp2EHrxUgCDjP4kZpA`（见 config.py） |

**注意**：本地叫「衣锦」，云端文件叫「校门口」，但云端表标题写的是「衣 锦 汇 总」。
用户已确认这个对应关系。

云端表都在**「我的设备」的 drive_id = `757726038`**（客户端显示为「自动上传文档/其他设备」）。

### 云端表结构

```
第 1 行：合并标题（如「农 大 东 湖 周 餐 汇 总」）
第 2 行：表头 = 名字 | 地址 | 电话 | <每列一个日期> | 类型 | 餐种 | 总餐次 | 已出餐 | 剩余餐 | 备注
第 3 行起：数据
```

- **日期列数量差异巨大**：东湖中餐只有 4 个日期列；衣锦中餐有 **121 个**（含大量历史日期 + 中间的空列）。
- **列位置会变**：协作者每天在「类型」列前插入 1~2 列，所以**所有列都必须按表头文字查找，不能按列号写死**。
- **工作列表头不统一**：东湖午餐/医学院午餐写「总餐次/已出餐/剩余餐」，其余 4 张写「总餐数/出餐/剩余」。

---

## 3. 接口能力与限制（全部实测，别浪费时间重测）

### 3.1 用什么接口

金山官方 CLI **`kdocs-cli` v2.5.29**，已放进项目 `vendor/kdocs-cli/kdocs-cli`
（官方 CDN 下载，sha256 与官方 `checksums.txt` 一致，六平台包齐全）。

- 个人账号可用，**不需要企业资质**
- 授权：`kdocs-cli auth login`（浏览器确认一次，token 存系统密钥链约 1 年）
- 调用方式：`kdocs-cli sheet <action> '<json>'`，大参数走 `--file <json路径>`（避免 argv 上限）

### 3.2 单次调用限制（**最容易踩的坑**）

| 限制 | 数值 | 错误码/现象 |
|---|---|---|
| 单次写入单元格数 | **100** | `400001 rangeData length N exceeds limit 100` |
| 单次读取格数 | **50000**（工具层） | `400001 range 选区过大（N 行 × M 列 = X 格）` |
| **每日调用总量** | 约 150~200 次后触发 | `429001 今日调用次数已达上限` |
| 短时频繁触发 | — | `429002 频繁触发调用限制` |
| 恢复时间 | **每天 08:00** | 返回 `reset_at`（时区口径不稳，别用它算时刻） |

⚠️ **额度是最大的工程约束**：我（上一个 AI）在调试中**两次把当日额度打满**。
所以代码里必须避免"逐行读格式"这类操作（97 行 = 97 次调用）。
**优选做法：一次批量读，本地统计。**

### 3.3 格式能力

| 能力 | 是否支持 | 说明 |
|---|---|---|
| 字体名 / 字号 / 加粗 / 斜体 / 颜色 | ✅ | `opType: "format"` + `xf.font`，字号单位是 twip（pt × 20） |
| 水平/垂直对齐 | ✅ | `xf.alcH` / `xf.alcV`（居中=2/1） |
| 背景色 | ✅ | `xf.fill.back.value` = ARGB 整数 |
| 自动换行 | ✅ | `xf.wrap` |
| **边框** | ❌ | 试过 `bd`/`border`/`borders`/数组 4 种写法，回读 `hasBorder` 全 False |
| 读格式 | ⚠️ 部分 | `get-range-data` 返回 `fonts`/`alignment`/`cell_background_color`/`hasBorder`；**空单元格不返回**（所以目标行的空列读不到底色 → 必须照抄模板行） |

**接口对无效字段静默忽略**（返回 `code:0` 但没生效）—— 写完必须回读校验。

### 3.4 其它可用能力

- **排序**：`sheet range-sort`，参数 `range`（如 `A3:L42`）、`key`（列字母或 1-based 列号）、
  `order`（asc/desc）、`key2`/`key3`（三级排序）、`header`（首行是否表头）。
  **已实测有效**（C=30,A=10,D=40,B=20 按 B 升序 → A,B,C,D）。
  ⚠️ 用户提过"你也没排序"——**排序功能尚未接进程序**，如果用户要，用这个接口。
- 插入行/列：`insert-rows-cols`
- 列宽行高：`set-range-width-height`（twip）、`auto-fit`
- 合并：`merge-range`
- 条件格式、数据校验：`create-conditional-format-rules`、`create-data-validations`
- 追加整行：`add-row`

---

## 4. 代码结构

```
app/wps_cloud.py            核心（约 1100 行）：CLI 封装 / 表头解析 / 匹配 / 计划 / 执行 / 格式
app/config.py               AppConfig 里的 wps_* 字段 + 表映射常量
app/bridge.py               wps_status / wps_preview / wps_upload / wps_authorize /
                            wps_check_copies / save_wps_config
frontend/src/components/CloudForm.tsx    「云文档同步」页签 UI
frontend/src/lib/bridge.ts  前端类型
scripts/reset_wps_field.py  把"试验田"重置回基线存档（开发用）
scripts/fetch_kdocs_cli.py  构建时按平台下载并校验官方 CLI
tests/test_wps_cloud.py     单元测试（离线，mock 掉 subprocess）
design/WPS-CLOUD-SYNC-PLAN.md   完整实施记录（含历次踩坑）
```

### 关键数据结构

```python
CloudOrder(sheet, name, address, phone, meal_type, meal_kind, meals, row)
    # meal_type = 类型（中餐/晚餐）；meal_kind = 餐种（经济/豪华）；meals = 餐次

Change(kind, name, phone, row, delta, target_col, total_before, total_after,
       target_ok, address, meal_type, meal_kind, detail)
    # kind = "existing" | "new"；needs_write = (not target_ok) or total 变化

SheetPlan(sheet, file_id, drive_id, target_date, target_col, target_header,
          weekday_number, changes, warnings, append_row, columns,
          format_rows, format_fills)
```

---

## 5. ⚠️ 当前代码状态：**有 14 个测试失败，是刚才改格式功能时留下的**

**失败原因非常单一**：测试替身 `FakeCli.read_grid()` 没有跟上新签名
（真实实现新增了 `with_format` 关键字参数）：

```
TypeError: FakeCli.read_grid() got an unexpected keyword argument 'with_format'
```

**修复方法**：在 `tests/test_wps_cloud.py` 里给 `FakeCli.read_grid`（以及
`TemplateCli` / `BatchCli` 的覆盖版本）加上 `*, with_format: bool = False` 参数，
并在 `with_format=True` 时返回 `{(r, c): {"text": ..., "fill": ...}}` 结构。

**生产代码本身是能跑的**（失败全在测试替身）。但**格式功能还没在真机上验证通过**，
因为改完就撞上了当日额度耗尽。

### 我停手时的最后一步改动

`app/wps_cloud.py` 的 `build_plan()` 里，格式样板的选择已改成：

1. **一次**批量读「姓名列 ~ 备注列」（`read_grid(..., with_format=True)`），
   范围从第 3 行到数据末尾；
2. 统计这些行**姓名列的底色**，取**多数派**（东湖中餐应为白 `#FFFFFFFF`）；
3. 从多数派里取**最靠近表尾**的一行当样板；
4. 把样板行的**逐列底色**缓存到 `plan.format_fills`；
5. `apply_plan()` 对每个新客户行调用 `cli.copy_row_format(...)`，
   **逐列照抄** `format_fills` 的底色（豪华餐则整行金黄）。

**待验证点**：
- 多数派统计是否真的选中白底行；
- 照抄后新行的颜色是否与第 99 行完全一致（A~J 白、K~M 黄、N 白）；
- 调用次数是否够省（目标：读 1 次 + 每行格式 1 次）。

---

## 6. 三套表格配置：正式表 / 试验田 / 基线存档

用户的要求：

> **正式表 = 验收标准**（今天的工作成果已做好）；
> **试验田 = 程序写入目标**（可以随便试错）；
> **达不到正式表的预期，就把试验田滚回基线存档**。

| 角色 | 是什么 | 当前 file_id |
|---|---|---|
| 正式表 | `DEFAULT_WPS_PRODUCTION_TABLES` in `app/config.py` | 见第 2 节 |
| 试验田 | `DEFAULT_WPS_TABLES` in `app/config.py` | 见下 |
| 基线存档 | `基准-<子表名>.xlsx`（**只读，永不改动**） | `design/WPS基线存档.json` |

**当前试验田（截至 2026-09-12 14:5x）**：

| 子表 | file_id |
|---|---|
| 东湖中餐 | `H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG` |
| 衣锦中餐 | `qFBgqf13GxM7vPUbTSJmxxsrgopD4DnpA` |
| 医学院中餐 | `p9P2p2NFfxMZjLXGZ1fyxxrFTBf9s81Kn` |
| 东湖晚餐 | `RBLtXB8x3rMcQhCp6zp11xGBN7Wey2xCD` |
| 衣锦晚餐 | `afnJ5h5Di1M3U9VwX3rvxx9jpTn8EUw9o` |
| 医学院晚餐 | `rxYTF8Juk9MBbhkbfjE9Bx1dQ3vGeZ3zr` |

**重要**：
- 用户明确说**晚餐 3 张暂时不用管**（程序对它们会正确跳过："未找到目标日期列"）。
- 用户要求**先只做东湖中餐**，验证通过后再做另外两张中餐表。
- 重置命令：`python scripts/reset_wps_field.py --apply --sheets 东湖中餐`
  （它会从 `基准-*.xlsx` 复制出**新的**试验田并更新 `config.py`；
  同时会清掉用户配置里过期的 `wps_tables`，否则配置会覆盖代码默认值 —— 这是个坑）

**⚠️ 尚未切正式表**：用户说"还有功能要加"，所以正式表还没启用。
等用户验收满意后，用 `bridge.restore_wps_production_tables()` 一键切回。

---

## 7. 部署方式

程序是 PyInstaller 单文件，运行目录：

```
/home/zimu/下载/yikou-light-food-linux-x64(1)/yikou-light-food
```

**构建 + 部署流程**（务必按顺序）：

```bash
cd /home/zimu/文档/yikou-light-food-desktop
# 1) 先改完代码（构建会把 config.py 的默认值打进包，改一半就构建会打进旧值！）
# 2) 构建
PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/python -m PyInstaller --clean --noconfirm yikou-light-food.spec
# 3) 验证产物
./dist/yikou-light-food --wps-check          # 应打印 6 个写入目标 + kdocs-cli 路径 + 授权状态
# 4) 部署（程序若在运行需先关闭，否则用户重启才生效）
TARGET="/home/zimu/下载/yikou-light-food-linux-x64(1)"
mv "$TARGET/yikou-light-food" "$TARGET/yikou-light-food.old_$(date +%H%M%S)"
cp dist/yikou-light-food "$TARGET/yikou-light-food" && chmod +x "$TARGET/yikou-light-food"
```

**自检开关**（`app/main.py`）：`--version` / `--self-check` / `--wps-check`

---

## 8. 已知踩过的坑（**请全部避免**）

| # | 坑 | 教训 |
|---|---|---|
| 1 | 一次写 175 个格子 → 接口上限 100，整张表失败 | 必须分批（`WRITE_BATCH_CELLS = 80`） |
| 2 | 按 285 行 × 201 列读 → 超 5 万格上限，预览/上传直接失败 | 用 `scan_bounds()` 按"行×列 ≤ 45000"自适应压缩行数 |
| 3 | **逐行读格式**（97 行 = 97 次调用）→ 当日额度打满 | 一次批量读 + 本地统计 |
| 4 | 用"第一行"当格式样板 → 25 个新行全被涂成绿色 | 统计多数派 + 取最靠近表尾的多数派行 |
| 5 | "读回目标行原底色再写回" → 空列读不到颜色，被涂成杂色 | 改为**照抄模板行的逐列底色** |
| 6 | 用户配置里存了 `wps_tables`，覆盖代码默认值 → 重置后程序仍写旧副本 | 重置脚本要一并清掉配置里的过期键 |
| 7 | 构建跑到一半才改 config.py → 打进包的是旧 ID | **先改完代码再构建**，构建后必须 `--wps-check` 核对 |
| 8 | 列号 0-based / 1-based 混淆（我踩了 3 次） | 用 `read_row()` / `find_column()`（都返回 1-based） |
| 9 | 回读失败被误报成"校验不一致" | 网络失败要单独报 `verify_unreadable`，并仍记账本避免重复写 |
| 10 | 用 `AppConfig()` 而不是 `AppConfig.load()` 调试 → 读不到用户配置 | 调试脚本注意 |

---

## 9. 建议的下一步（优先级从高到低）

1. **修 14 个失败的测试**（给测试替身加 `with_format` 参数）—— 半小时内能搞定；
2. **等额度恢复（每天 08:00）后真机验证格式功能**：
   - 只跑东湖中餐：`bridge` 的 `wps_upload()` 或直接调 `build_plan` + `apply_plan`；
   - 核对：新行 A~J 白、K~M 黄、N 白，字体 = 原数据的字体（Microsoft YaHei 10）、居中；
   - 豪华行（第 108/120/123 行的「陈章依/刘姵怡/灵」）整行金黄；
   - 核对完成后与正式表逐人比对（人员 / 日期格 / 总餐次）。
3. **东湖中餐通过后再做衣锦中餐和医学院中餐**（用户明确要求这个顺序）；
4. **如果用户要排序**：用 `cli.sort_range()`（已封装，未接入流程）；
5. **最后再切正式表**（`restore_wps_production_tables()`）。

---

## 10. 用户沟通风格提醒

- 用户是**实际使用者**，不是程序员：解释要具体（"张这一行现在是 6"），少用术语；
- 用户**明确讨厌猜测**：不确定就先问，别硬做（我在需求确认上反复过很多次，是对的）；
- 用户对**数据正确性极敏感**：任何写入都要能说清"改了哪几格、为什么"；
- 改动前**先说方案**，用户同意再动手；
- 云端有大量我留下的测试文件，清单在 `design/云端测试文件清理清单.md`（用户还没删）。

---

## 11. 交接时的即时状态（重要，接手先看这里）

### 额度：耗尽中

```
code 429001  今日调用次数已达上限，将于 2026-09-13 08:00:00 恢复
```

**在 2026-09-13 08:00 之前，所有云端操作（读/写/搜索/取链接）都会失败。**
这不是代码问题。建议接手后先等额度恢复，再跑真机验证。

### 代码：生产逻辑可用，测试有 14 个失败

- `app/wps_cloud.py` / `app/bridge.py` 的**生产逻辑是完整的**；
- 14 个失败全在 `tests/test_wps_cloud.py` 的**测试替身**签名不匹配（见第 5 节），
  生产代码本身不报错；
- **格式功能的最新改动（多数派选样板 + 一次批量读）尚未真机验证**。

### 试验田：东湖中餐被重置过多次

`H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG` 是**最后一次重置出的干净副本**（97 人、无 9.12 数据）。
但因为额度耗尽，**没能完成"写入 + 验证"这一步**。
接手后请先确认它的实际内容（`get-sheets-info` + 读姓名列），必要时再重置一次。

### 打包版程序：**旧版本**

运行目录里的 `yikou-light-food` 是 **14:51 构建**的版本 ——
包含"分批写入、自适应读取、格式雏形"，但**不包含**最后的多数派样板修改。
源码比打包版新。接手后若要给用户测，需重新构建部署（见第 7 节）。

### 用户当前的情绪与期望

- 用户对格式涂错**明确不满**（"你颜色涂的什么玩意儿"），已要求先暂停；
- 用户的期望：**先只把东湖中餐做对**，颜色严格是「白 + 黄」两种表现，
  和表内第 99 行「哒哒」一致；
- 用户还说"你也没排序" —— 排序能力接口支持但**未接入程序**，需要时再做；
- 用户原话："把我目前的需求，接口的限制，遇到的困难什么的总结出来，
  我到时候用别的 ai 进行修改操作" → **本文就是给接手方的说明书**。

---

## 12. 复核记录（2026-09-13 22:4x，由上一个 AI 复查本轮改动）

### 发现并修复的两个问题

**① 打包版比源码旧一整天（严重）**

| 文件 | 时间 |
|---|---|
| `app/config.py` | 09-13 21:55 |
| `app/bridge.py` | 09-13 22:00 |
| `app/wps_cloud.py` | 09-13 22:32 |
| `frontend/dist/index.html` | 09-13 22:33 |
| `dist/yikou-light-food` | **09-12 17:47** |
| 部署目录的程序 | **09-12 17:47（同一个文件）** |

**后果**：本轮新增的「按地址组插入行」「格式批量合并」等逻辑**根本没打进程序**，
用户拿旧版去真机验证会得到错误结论。
**已处理**：重新构建并部署（09-13 22:48），`--self-check` / `--wps-check` 均通过。

> ⚠️ 教训：**每次改完代码必须重新构建部署**，并用 `--wps-check` 核对产物。
> 判断方法：`stat -c '%y' app/wps_cloud.py dist/yikou-light-food` 比时间。

**② `writing_test_copies` 显示判断失效**

`wps_status()` 原来用 `cfg.wps_tables`（可能是正式表 ID）判断"是否在写测试副本"，
而测试模式下**实际生效目标是 `cfg.wps_test_tables`**，导致正在写副本时被误报成
"正在写正式表"。
**已修复**：改用 `self._wps_effective_tables()` 的实际生效目标判断，
并新增 `status["effective_targets"]` 字段（列出真实写入的 file_id）。

### 复核通过的项

| 检查 | 结果 |
|---|---|
| `pytest tests/` | **338 passed** / 4 skipped |
| `ruff check app/ tests/ scripts/` | 全部通过 |
| 前端 `npm run build` | 通过 |
| 测试替身是否跟上 `with_format` 签名 | ✅ 已跟上 |
| 是否残留已删函数 `copy_row_format` 的引用 | ✅ 无残留 |
| 前端调用的 bridge 方法是否都存在 | ✅ 4 个都在 |
| 格式实现（`build_format_ops`）逻辑 | ✅ 经济行逐列照抄模板底色、豪华行整行金黄、相邻列/行合并省调用 |
| 插入行测试覆盖 | ✅ 有 `test_new_customers_insert_after_address_group`、`test_insert_failure_rolls_back_inserted_rows` 等 |
| 接口健康（status / preview / check_copies） | ✅ 全部正常，预览 14 人已完成、零变更 |

### 当前状态

- **写入目标 = 测试副本**（仅东湖中餐 `sT4PQKKmo1MtL7AxhNk1BxaXzcx9oLnVf`）
- **测试模式 = 开**，正式表受保护
- 部署版已是最新（09-13 22:48）
- 待办：真机验证「插入行」在云端的实际行为（见 `WPS真机验证清单-20260913.md`）
