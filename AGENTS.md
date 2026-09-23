# Agent Instructions

本项目主要由 Claude Code 和 Codex 直接操作。收到「查询/下载某车型」这类需求时，按本文件的流程执行，不要重新发明流程。

## 项目定位

统一的车型数据下载与对标分析工具：

- **汽车之家**（`autohome_cc/`）：按车型名抓配置，导出 Excel 对比表到 `output/`（含「配置分析」差异透视 sheet）
- **工信部公告**（`miit_gonggao/`）：按商标/型号前缀查公告，下载参数页 PDF 和详情页原图到 `downloads/announcement_site/<品牌>/<车型>/<批次>/`；`review_export.py` 可把参数页 PDF 转成公告参数 Excel

## 与 Website 项目的边界

数据流单向：本项目 → Website，不存在反向依赖。判断一处改动是否越界，只问一句：**它读写的资产，唯一写入方是谁？**

本项目独占写入：`data/jianmian_catalog.sqlite`、`data/vehicle_profiles.json`、
`data/announcement_site.sqlite`（公告、PDF 路径/哈希/解析字段、采集轮次及逐型号状态、正式公告跟踪）、
`downloads/announcement_site/`（含 `_snapshots/`、`分类公告/`、`_revisions/`）、
`downloads/announcement_batches/`、`var/runs/`、采集报告及 `output/`。
Website 独占写入其派生站点库、前后端、站点构建/部署产物；只读消费上游业务库和 PDF。
2026-09-12 已把 Website 的采集实现与完整业务库统一迁入本项目；Website 旧脚本为兼容转发，
旧数据库/运行目录为符号链接，不能再建立独立副本。详见 `README.md` 的“公告批量采集”章节。
`main.py gonggao collect` 是批量入口：目录/型号名单与固定产品清单共享下载、解析、入库和状态收尾；
`collection_tracking` 管理近期事件，`collection_report` 生成采集报告，`collection_status` 管理持久收录统计。
历史 CLI 查询的 `manifest_*.json` 保持原始证据，不改写成网站采集轮次。
覆盖率统计和修订归档现在可读取本项目权威业务库；归档仍默认预览，`--apply` 须用户确认。
`core.build_announcement_download_dir` 是所有下载入口共用的目录结构契约。
- **竞品对标**（`benchmark/`）：抓汽车之家口碑 + 懂车帝销量榜，生成对标分析 HTML 报告到 `output/`

这是通用工具，不要把它当成只服务少数硬编码车型的脚本；车型相关条件一律走 `data/vehicle_profiles.json` 档案或 CLI 参数。

## 收到车型需求时的执行流程

统一用项目虚拟环境运行：`.venv/bin/python main.py ...`（首次先 `./scripts/bootstrap.sh`）。

**若任务是网站批量收录、历史补采、重试或提速，先按下面的「批量采集复用约束」执行；**
下列 `fetch/query --download` 流程用于车型查询和独立文件交付，不替代网站业务库登记。

1. **判断用户要哪类内容**：
   - 要配置对比 / 参数表 / Excel → 汽车之家：`--source autohome`
   - 要公告页 / 公告参数 PDF / 工信部数据 → 工信部：`--source gonggao`
   - 都要或未说明 → `fetch <车型>` 默认两个来源都抓
   - 要公告参数表 / 公告参数 Excel → 先确认 `downloads/announcement_site/<品牌>/<车型>/` 有 PDF（没有先 fetch），再 `main.py review <车型>`
   - 要竞品对标 / 口碑分析 / 槽点亮点 / 销量对比 → `main.py report <本品> --vs <竞品...>`（断网或复现用 `--offline`，需要 `downloads/benchmark/` 已有缓存）。按需聚焦板块：`--focus all`（默认综合，顶部含「综合研判」结论层）/ `sales`（仅销量对标）/ `koubei`（仅口碑对标）；不带 `--vs` 生成单车画像。销量为懂车帝全榜+汽车之家级别榜双源、各含最新月/近半年/近12个月三维度并标注月份范围；懂车帝对英文命名品牌（中文名↔英文名）按型号标识跨语言匹配，汽车之家级别榜未进 Top20 时按口碑品牌 ID 品牌检索兜底
2. **先查档案**：`main.py profiles` 看车型是否已有统一档案（含别名匹配）。
3. **命中档案** → 直接 `main.py fetch <车型名>`。
4. **未命中档案**：直接 `main.py fetch <车型>` 也能跑——汽车之家按输入名搜索；
   工信部侧 fetch 会**自动走减免购置税目录按市场名反查**（公告商标常与品牌不一致，例如由代工方持有商标），
   反查成功即下载**最新批次** PDF 并在输出里给出"建议 model_prefixes"（`--all-batches` 保留历史批次）。需要手动控制时：
    - `main.py gonggao jianmian search <市场名> --resolve [--download] [--latest-batch]`（库里没有就先 `main.py gonggao jianmian sync`）
    - 已知商标或型号时用透传 CLI 一次性查询：
     `main.py gonggao query --trademark "某某牌" --model-prefix XXX --latest-batch --download --vehicle-folder <车型>`
     （`--model-code`/`--company`/`--model-prefix` 之一存在时可不带 `--trademark`）
    - 要查变更扩展公示时用 `main.py gonggao changes --model-code <完整公告型号>`；公示只用于提前了解，
      不下载、不入库、不作为任何采集入口。公示批次与正式公告批次不能假定相同；
      正式发布之后用 `main.py gonggao collect --republished-from-status` 刷新已有参数页
   - 如果这个车型以后还会查，用 `main.py profiles add <市场名>` 把条件写进 `data/vehicle_profiles.json`，
     不要手抄终端里的「建议 model_prefixes」。先 `--dry-run` 预览；命令只写机器能确定的字段，
     商标不唯一、`clmc` 不一致、`exclude_model_prefixes` 一律留空并在终端点名，这些需要人工补
5. **汇报结果**：列出生成的 Excel 路径、PDF 目录、批次号，以及失败/跳过项。

常用命令：

```bash
.venv/bin/python main.py fetch <车型A> <车型B>          # 两个来源
.venv/bin/python main.py fetch <车型> --source gonggao  # 公告 PDF + 原图（默认最新批次）
.venv/bin/python main.py fetch <车型> --all-batches --source gonggao  # 全部批次
.venv/bin/python main.py autohome --models "..." [--browser-login --browser-fallback]
.venv/bin/python main.py gonggao query ... --download   # 公告查询 CLI 全参数可用
.venv/bin/python main.py gonggao query --model-code <型号> --images-only --vehicle-folder <车型>  # 独立补原图
.venv/bin/python main.py gonggao changes --model-code <完整公告型号>  # 变更扩展公示只读查询（不下载）
.venv/bin/python main.py gonggao collect --republished-from-status   # 正式发布重发 -> 刷新已有参数页
.venv/bin/python main.py gonggao collect --republished-batch 409     # 同上，按批次缓存现算
.venv/bin/python main.py gonggao collect --republished-batch 409 --dry-run  # 只出清单快照，不查接口不下载
.venv/bin/python main.py gonggao jianmian sync          # 减免税目录抓取入库（增量，--force 重下并作废转换缓存）
.venv/bin/python main.py gonggao jianmian search <市场名> --resolve [--download] [--latest-batch]
.venv/bin/python main.py gonggao jianmian export [--keyword <关键词>] [--xlsx 路径]
.venv/bin/python main.py profiles                       # 列出统一车型档案
.venv/bin/python main.py profiles add <市场名> [--dry-run] [--overwrite] [-f 名单.txt]  # 反查并写入档案
.venv/bin/python main.py review <车型> [-o 输出.xlsx] [--no-catalog]   # 公告 PDF -> 公告参数 Excel
.venv/bin/python main.py report <本品> --vs <竞品1> <竞品2> [--focus all|sales|koubei] [--offline] [--pages N]
```

## 批量采集复用约束

- 开始网站采集任务前，先读 `README.md` 的“公告批量采集”章节，检查
  `main.py gonggao collect --help`、所选模式及既有轮次/清单；不得未查入口就编写下载脚本。
- 查询收录量、缺口或历史变化，先用 `main.py gonggao status`（可加 `--json`/`--history`）
  或读 `var/reports/collection-status/latest.json`，再判断是否需要显式 `--refresh`。
  默认查看仅比较输入元数据和规则哈希，显示 `stale` 不自行重算；不要直接临时重扫或另建统计台账。
  历代 `snapshots/<generation>/{summary.json,candidates.json,report.md}` 保留；刷新失败读
  `last-error.json`，旧 latest 不能当作本次结果。批次收尾统一更新，不在每条 PDF 后全量计算。
  产品候选、目录型号六桶、事件和公开库占位分开使用；过期完整缓存只作历史基线，窗口日期及
  PDF 文件校验边界见 README。独立文件导出不回写业务库，不改变收录状态。
- 完整批次减少先恢复合格缓存；确需调整来源基线使用 `status --refresh --rebase-cache`，
  同时给出当前 `--expect-generation`、精确 `--removed-batches` 和 `--rebase-reason`。
  保留历史与范围变化证据，不删除 latest 或让普通刷新绕过保护；自动收尾不使用基线调整选项。
  已知接口缺表作为带日期的来源说明，真正枚举/校验/无证据缺失单独告警，不按批次号硬编码豁免。
- 网站批量采集统一使用 `main.py gonggao collect`；近期事件沿用
  `python -m miit_gonggao.collection_tracking`。不得因日期、批次、车型范围、补采或提速
  新建独立批量下载入口，也不得复制已有下载循环。
- 公示（变更扩展公示、新产品公示）只用于提前了解，不作为任何采集入口：不登记事件、不下载、
  不进业务库。已有参数页被官方变更后要刷新时，用 `collect --republished-from-status`
  （读已落盘统计记录）或 `--republished-batch <批次>`（按批次枚举缓存现算），
  两者的重发判据都是同一产品 ID 在更高正式批次再次出现，但本地侧门槛不同：
  `--republished-from-status` 只收 PDF 已解析的产品，`--republished-batch` 只要求有有效 PDF，
  所以已下载但解析失败的重发只有后者会选中，前者把它留在统计的 `pdf_unparsed` 候选里。
  正式接口批次低于记录批次时该型号记 `awaiting_effective` 并跳过，绝不用更旧的一版覆盖本地已有参数页。
  `--limit` 在该模式下按候选产品计数，去重后型号数可能更少；`--dry-run` 的统计与快照都按本轮清单输出。
- 批次枚举与按型号查询是两个官方接口，口径不一定一致：缓存把产品列进更高批次，不等于查询
  接口认那一批是当前有效版本。`--dry-run --verify` 按型号只读核实清单，分出 `confirmed`
  （真重发）/`stale_record`（接口批次更低）/`missing`（查不到精确型号）/`failed`（未核实），
  结果并入同一份清单快照。实际采集时 `batch_is_at_least` 已逐条核实，所以 `--verify` 只在
  `--dry-run` 下可用。
- 事件消费端只认 `kind='formal'`。旧库里遗留的 `new_notice`/`change_notice` 行原样保留在
  `tracking_events`，但不进重试计划（`plan` 把它们单列为 `legacy_notice`），Website 构库也会跳过
  并记入 `tracking_non_formal_skipped`。不得删除源库历史来"解决"这个问题。
- 能力不足时，先定位缺少的参数、候选选择或恢复能力，在既有 `collection*.py` 中扩展。
  可按职责拆分内部策略模块，但必须接入原入口，复用 `store_announcement`、锁、解析、
  状态收尾和权威业务库；内部拆分不构成新增一套采集器的理由。
- 禁止用临时 Python、Shell、Notebook、后台进程或另一个库绕开上述约束。
  一次性辅助代码可只读统计、估算、生成名单或核验结果，不能发起 PDF 下载或另写采集状态。
- 每轮变化放入上游 `var/runs/<本轮>/` 的清单、参数和日志；通用代码不带任务日期/批次硬编码。
  恢复固定清单使用原清单/哈希及 `--resume-run`，提速使用现有模式支持的节流参数。
  不得删改清单校验、并发锁或失败保护来完成运行；跨轮按最好结局避免重复下载。
- 固定清单 `nev_complete_manifest_v1` 及旧 scope 均仅接受新能源；
  `automotive_complete_manifest_v1` 仅在第 408、409 批允许非新能源/未知整车，其他批次仍须新能源。
  需要新能源证据但产品名未明确时，仅允许通过 `--catalog-db` 的精确同型号具体能源补证。
  通用新能源标签不能单独放行；忽略其后，具体能源须唯一且属于新能源，并与产品名相容。
  缺失、普通混动或冲突证据均拒绝；不信任清单自报能源，不改写官方产品名或放宽整车判定。
- Website 兼容脚本仅转发，不增加业务逻辑；业务库、PDF、下载日志保持上游唯一活动资产。
  发布数据只读消费；本轮是否下载及收录范围仍以用户命令为准。
- 修改采集能力时，交付说明应列出复用入口、实际能力缺口、修改模块、数据落点及验证结果。
  旧架构设想、历史报告和记忆不能作为新增独立下载入口的依据；当前规则见本节和采集文档。

## 减免购置税目录（jianmian）

- 数据源：装备中心 col1691 正式发布文章（标题含《减免车辆购置税的新能源汽车车型目录》），附件 .doc 解析后入 `data/jianmian_catalog.sqlite`，附件缓存在 `downloads/jianmian/<文章ID>_公告第N批/`（目录名 ID 前缀是缓存键锚点，旧版纯ID目录会自动迁移）
- 附件按批次自动命名（购置税目录第28批.doc / 车船税目录第83批.doc / 公告第404批附件.doc），派生缓存同步改名；目录内 `manifest.json` 维护 URL名->本地名 映射。`--force` 会重新下载并删除 `.html`/`.lo.docx`/`.word.docx` 再解析，不要手工改目录/文件名绕过它；存量迁移用 `jianmian rename`
- 解析依赖 macOS `textutil`（.doc 转 HTML）；表格被压平成单段，行重建依赖型号/企业名锚点，详见 `miit_gonggao/jianmian.py` 模块注释
- 入库全部新能源车型表（乘用车/客车/货车/专用车，表头含 型号+续航/电池；通用名称仅乘用车表有），截至2026-06 购置税目录第1~30批全量覆盖
- `articles` 表带 `status/error/row_count`：目录附件解析 0 行记 `zero_rows`、异常记 `error`，都不算已同步、下次 sync 默认重试；解析异常时先 rollback 再标 `error`，不提交半截 DELETE。部分附件成功、部分 0 行时写入成功附件并保留其余旧行，整篇仍标 `zero_rows`。空嗅探先完整解析（避免标题靠后的真目录被标成 `ok`/0 行）；`.docx` 精确转换失败时按 `zero_rows` 重试，不能标成「无目录附件」（仅 textutil html 不算已转出）。docx 已转出但仍无目录标题的按非目录跳过，不把整篇钉在 `zero_rows`。只有明确的「目录」/「推荐」才在嗅探阶段跳过。全文都是非目录附件时标 `ok`+「无目录附件」。旧库（status 为 NULL）视为已同步
- 解析主路径：textutil 廉价嗅探附件是否目录 → 目录附件经 `convert_doc_to_docx` 转 .docx 按真实表格精确解析；转换器优先级 LibreOffice headless（`.lo.docx` 缓存，无窗口）> 本机 Microsoft Word（`.word.docx` 缓存，会调起 Word；在其沙盒容器 `~/Library/Group Containers/UBF8T346G9.Office/` 内转换、不弹授权框）；两者都没有时退回压平文本启发式重建。缓存命中不再调用任何转换器（`--force` 除外）；LibreOffice 失败/超时（`SOFFICE_TIMEOUT`=300s，大合刊附件可能超）自动回退 Word
- 2020~2022 年的《新能源汽车推广应用推荐车型目录》表头无"通用名称"列，不入库（警告跳过属预期）
- 典型用途：新车只知道市场名时 `jianmian search <市场名> --resolve` 反查公告型号与商标，再沉淀进车型档案

### 目录未覆盖车型（`scripts/announcement_catalog_gap.py`）

反过来问「公告发布了、但没进新能源目录的车型有哪些」，输出格式与
`jianmian export --by-category` 前 19 列完全对齐，可直接与目录表拼接：

```bash
.venv/bin/python scripts/announcement_catalog_gap.py --batch 409         # 单批
.venv/bin/python scripts/announcement_catalog_gap.py --batch 173-409     # 全部可查批次
```

- 公告参数查询接口**拒绝只带 `pc` 的空条件查询**（`respCode=500`「请输入查询条件」），
  因此按企业名称关键词并集枚举整批，再用型号两字母前缀反查交叉校验完备性；
  接口同样**不支持第 173 批以下**（`不支持对小于173批以下的数据查询.`），脚本自动跳过
- 每批枚举结果缓存到 `downloads/announcement_batches/batch<N>.json`，重复导出不再打接口；
  单批失败不中断其余批次，`--refresh` 强制重抓
- 类别按 本项目 `scripts/announcement_catalog_gap.py:derive_category` 生成导出分组。
  该分组不代替汽车整车收录判断；网站范围统一由 `miit_gonggao.vehicle_classification` 判断
- 公告接口只返回 11 个键，续驶里程/油耗/排量/整备质量/电池等目录字段在参数页 PDF 里，
  导出时**留空而不是编造**
- Excel 按车辆型号去重（跨批次同型号取最新批次，另记出现批次数与最早批次），
  逐产品 ID 明细另出 CSV——全量批次的产品记录数会超过 Excel 单表 104 万行上限
- 缓存 v4 只复用「完整 + 通过所需交叉校验 + 7 天内」的结果；部分失败另存 `.partial.json`
  并向调用方返回失败，批次在上游没有数据表则另存 `.absent.json` 待下次重新探测。
  这两类文件都是证据，不得当作成功全集参与导出

### 跨平台运行（macOS / Windows / Linux）

三平台均可运行，全部功能无平台专有依赖。`jianmian sync` / `rename` 需要 LibreOffice 把目录附件
的老式 `.doc` 转成 `.docx`；其余命令纯 Python。已落地的适配约定，改动时不要破坏：

1. **`.docx` 是主链路**。原生 `.docx` 附件由 `convert_doc_to_docx` 直接返回自身，不送进转换器空转；
   `.doc` 先复用 `.lo.docx` / `.word.docx` 缓存，未命中才调 LibreOffice。
2. **`find_soffice()` 三平台探测**：先 `PATH`，再 `SOFFICE_PATHS` 里的默认安装位置。Windows 版
   LibreOffice 装完不写入 `PATH`，删掉这些候选路径会让 Windows 直接不可用。
3. **转换产物必须过 `is_valid_docx()`**（非空 + 含 `word/document.xml`）才落缓存，避免把垃圾
   产物缓存下来导致后续解析恒为 0 行。
4. **`SOFFICE_TIMEOUT` 可配**（环境变量，默认 300s）。无 Word 兜底的平台遇到大合刊附件需要调大。
5. **`textutil` 是 macOS 可选加速，不是必需品**。`doc_to_html` 在缺失时返回 `None`（不得改回
   `raise SystemExit`），调用方改用 `peek_catalog_info_docx` 嗅探。已有 `.html` 缓存在任何平台都复用。
6. **Word AppleScript 通道有 `sys.platform != "darwin"` 守卫**，其他平台不得调用 `osascript`。
7. **性能差异要如实告知**：macOS 靠 textutil 在转换前筛掉非目录附件（缓存中占 32% 个数、51% 体积），
   其他平台需转换后才能判定，首次全量同步的转换量约为两倍。这是一次性成本——转换有缓存，
   判定为非目录的文章标 `ok` 进入 `known`，增量同步无差异。不要为了抹平它退回纯文本提取而丢失表格边界。
8. 保持 `manifest.json` 映射、派生缓存同步重命名、`--force` 缓存失效、`zero_rows` / `error` 重试、
   事务回滚与部分成功保留旧行等既有语义。

## 统一车型档案

`data/vehicle_profiles.json`，每条同时描述两个来源。该文件与 `data/jianmian_catalog.sqlite` 均为
本地业务数据，已在 `.gitignore` 中排除，不要提交；新环境从 `data/vehicle_profiles.example.json` 复制后填写。

```json
{
  "name": "示例车型",
  "aliases": ["example model", "EX1"],
  "autohome": { "models": ["示例车型"] },
  "gonggao": {
    "trademark": "示例牌",
    "filters": { "clmc": "多用途乘用车" },
    "model_prefixes": ["ABC6520M"],
    "exclude_model_prefixes": ["ABC6520AP"]
  }
}
```

- `autohome.models`：车系名导出全部在售配置；带年款的具体版本名只匹配最相关一个
- `gonggao` 字段承载公告查询条件；旧版平铺格式（`trademark` 直接放在顶层、无 `gonggao` 子对象）仍兼容
- 一次性筛选优先用 CLI（`--model-prefix`、`--row-filter`、`--latest-batch`），不要为临时需求改档案
- 注意：工信部公告查询主要覆盖新能源推广目录口径，普通油电 HEV（非插电）可能查不到

## 登录受限时（汽车之家）

命中权限/验证页时，直接告知用户并给出选项，不要静默重试：

1. `--browser-login`：调起浏览器扫码登录（需先 `INSTALL_PLAYWRIGHT=1 ./scripts/bootstrap.sh` 装 Chromium），登录态在 `.browser/autohome`
2. `--cookie-file ./cookies.txt`：用户导出 cookies（header 或 Netscape 格式均可；Netscape 的 `#HttpOnly_` 行会按 cookie 解析）
3. `--browser-fallback`：requests 失败时浏览器渲染兜底

## 抓取与网络规则

- 两个来源都保持低频、串行、温和，不加激进并发
- 经用户明确要求可给 `gonggao collect`（含目录、清单文件与正式发布重发各模式）传
  `--min-interval/--max-interval`，与固定清单共用同一实现：只覆盖本进程的
  `core.REQUEST_MIN_INTERVAL`，退出即恢复，不改 `core.py` 默认值；查询或下载失败会立即降回
  默认间隔并在结尾说明，本轮实际节奏记入 `ingestion_runs.selector_json`。默认不提速，必须显式传参
- 例外：`scripts/announcement_catalog_gap.py` 的全量批次枚举是一次性只读大作业（约 2.9 万次请求），经项目所有者授权可用 `--fast`（0.2~0.4 秒）或 `--min-interval/--max-interval` 提速。它只覆盖该进程内的 `core.REQUEST_MIN_INTERVAL`，不改 `core.py` 默认值，PDF 下载等其他功能不受影响；仍然串行（同等平均 QPS 下比并发对服务端更平滑），且连续 3 次请求异常会自动降回保守节流。默认不提速，必须显式传参
- 工信部/EIDC 全部 HTTP 走 `core.http_request`（统一节流 0.8~1.8s + 5xx/网络错误/响应截断（chunked `IncompleteRead`）退避重试，4xx 直接抛出），新增请求不要绕开它
- `query --download` 单条 PDF 重试后仍失败只跳过并在结尾汇总，不中断整批。退出码区分三档：**0** 全部成功 / **1** 全失败（一份 PDF 都没拿到，含查询结果为空）/ **2** 部分成功（已拿到 PDF，另有 PDF 或图片异常）。`jianmian search --download` 同一套语义（`core.download_exit_code`）；`main.py fetch` 把 2 当成功，只在结尾单独列出「部分成功」车型，不计入失败
- 优先 `requests + BeautifulSoup` / 标准库；只有确认必要才用 Playwright
- 工信部 PDF 接口的 `NECaptchaValidate` 沿用现有随机 token 方式，不要改动
- 缺失字段不要抛异常中断，记录日志后继续

## 输出规则

- 汽车之家 Excel → `output/汽车之家_<车型>配置表_<YYYYMMDD>.xlsx`（多车型取首车+总数：`汽车之家_<车型>等3个车型配置表_20260728.xlsx`；同名自动追加 `_2` 序号；命名逻辑在 `autohome_cc/utils/cleaners.py` 的 `build_export_filename`）（sheet：说明/车型信息/配置分析/详细配置表/颜色明细；配置分析按动力总成分组（能源×驱动×电池电量×座位数）每组一列，保留整合后的全量参数，组内多值合并总结，差异单元格高亮）
- 公告参数表 → `output/公告参数_<车型>_<批次>.xlsx`（多批次取区间如 `第394-406批`；关键参数 sheet 无「是否已评审」列，备注列写真实来源：公告参数页/减免车辆购置税目录/未显示未解析）；对标报告 → `output/对标报告_*.html`（销量标注懂车帝榜单具体月份，如「2026年05月销量」）
- 减免税目录导出 → `jianmian export --by-category` 产出 `output/jianmian_by_category/`：
  `全量.xlsx`（全部行）+ 6 个分类 xlsx，两者列结构相同。不带 `--by-category` 时只写单文件
  `output/jianmian_catalog.xlsx`，两条路互斥。导出只反映目录库自身，不含公告 PDF 的采集状态——
  采集覆盖率由本项目 `collection_coverage` 单独统计，不混入目录原始导出
- 导出一律整表重写覆盖，没有增量合并；`--keyword` 会把输出文件整体换成匹配子集，别拿带 keyword 的产物当全量表
- 口碑/销量原始数据缓存 → `downloads/benchmark/<车型>.json`（`report --offline` 依赖它；sales 字段为 `{dongchedi/autohome: {latest/half_year/year: {count,rank,label,scope}}}` 双源三维度结构；汽车之家 `scope=brand` 表示品牌检索兜底命中）
- 工信部 PDF → `downloads/announcement_site/<品牌>/<车型>/第<批次>批/`，目录统一由
  `core.build_announcement_download_dir` 生成。CLI 查询快照/下载索引仍在 `_snapshots/`，
  每条保留 `status/error`。批量下载结果统一写 `data/announcement_site.sqlite`，
  固定产品清单的逐产品日志另存 `var/runs/<本轮>/results.jsonl`；两类历史来源均保留，不互相覆盖。
  PDF 分类及历史修订分别由 `collection_classify`、`collection_revisions` 读取同一业务库处理。
- 变更扩展公示查询快照 → `downloads/announcement_site/_snapshots/change_notice_*.json`，其中同时保存公示文章、发布日期、公示批次、命中型号和详情链接；公示只读，没有下载索引
- 正式发布重发清单快照 → `downloads/announcement_site/_snapshots/republished_seed_*.json`，保存来源（统计记录代号或批次）、过期批次标记、候选产品 ID 及其记录批次与本地批次
- 公示 `notice_batch` 以文章标题为准；表格原始批次保留在 `raw_notice_batch`，混合列保留在
  `batch_or_chassis_id`，不得把底盘 ID 当作公告批次。解析仅跳过明确整行合计，缺列、未知合并行
  和缺型号均报告表格行号并停止登记，不静默丢弃产品行。
- PDF 只认 `%PDF` 魔数；不是 PDF 时保留 `.html` 供人工检查。非 PDF 属预期情形，只有**全部**条目都没拿到 PDF 才算失败（退出码 1），部分非 PDF 退出码为 2
- PDF 下载成功后默认同时获取详情页原图；复用 `core.download_param_page` 和 `images.download_product_images`，批量暂存发布仍走 `store_announcement`。图片及逐图索引保存在同批次 `images/<型号>_<产品ID>/`；会话、校验、错误、重试与补图接口契约见 [README 公告原图](README.md#公告原图)。只查询和预览不补图；既有 PDF 跳过路径只对上次图片获取失败的产品重取图片，不重下 PDF，不借此扩大历史采集范围。只要 PDF 时用 `--no-images`。
- 不要删除既有 `output/`、`downloads/` 内容，除非用户明确要求

## 开发规则

- README 集中维护现行结构、命令与数据契约；完成的审查、迁移和设计记录由 Git 追溯，
  一次性产物留在 `var/reports/`。新增能力更新原章节，不另写重复方案或长期历史审查页。
  合并/清理文档时保留有效约束与恢复信息，并修复本项目和 Website 的所有引用。

- `autohome_cc/`、`miit_gonggao/` 包内导入统一用 `autohome_cc.xxx` / `miit_gonggao.xxx` 前缀，不要写成顶层导入
- 解析逻辑与抓取逻辑保持分离，方便扩字段
- 汽车之家搜索接口的地址、参数与响应解析集中在 `autohome_cc/crawler/search.py`
  （`SEARCH_API_URL` / `build_search_params` / `parse_series_hits`），`benchmark/collector.py`
  复用同一套解析、各自保留网络层；接口结构变化只改这一处。搜品牌名时接口返回品牌 ID，
  靠 `SeriesHit.is_series` 区分，不要拿它当车系 ID
- 依赖分层：`requirements.txt` 基础 / `requirements-browser.txt` Playwright（可选）/ `requirements-dev.txt` pytest+ruff；新依赖放对层
- 行为变化时同步更新 `README.md` 和本文件
- 代码改动后至少跑：
  - `.venv/bin/python -m pytest`（解析边界 + 目录库状态 + Excel 导出，离线 fixtures）
  - `.venv/bin/python -m ruff check .`
  - `.venv/bin/python scripts/smoke_test.py`（离线 Excel 链路；有附件缓存时附带深度解析校验）
  - `.venv/bin/python main.py profiles`
  - 一次轻量在线 smoke（gonggao 查询不带 `--download`，除非下载行为有改动）

```bash
.venv/bin/python main.py gonggao query "<车型>" --latest-batch --output-dir /tmp/hub-smoke-a
.venv/bin/python main.py gonggao query --trademark "<商标>" --model-prefix <型号前缀> --latest-batch --output-dir /tmp/hub-smoke-b
```
