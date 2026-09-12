# 公告采集与下载记录

公告 PDF、下载/解析实现和完整采集业务库统一由本项目维护。Website 只读这些资产，
负责构建公开站点库、API、前端和部署。

## 统一入口

这是公告批量采集的现行运行契约；两项目 AGENTS.md 提供强制约束，README 只做入口导航。
历史架构设想和迁移记录不定义新的采集入口。批次、范围、提速或恢复需求改变时，
先复用参数与清单，能力缺口在既有入口下扩展，不另建独立下载脚本。

### 按任务选择

| 任务 | 复用方式 | 边界 |
| --- | --- | --- |
| 目录或型号名单采集 | `gonggao collect` 的目录参数或 `-f` | 会查询同型号所有正式批次；不是固定批次下载 |
| 已核验新能源整车产品 ID | `gonggao collect --manifest ...` | 只处理清单产品；当前不接受普通燃油/混动产品 |
| 近期公告/公示 | `collection_tracking` 的登记、`plan`、`collect` | `plan` 只读；`collect` 会下载，受当前授权约束 |
| 中断恢复 | 固定清单原文、哈希、目录与 `--resume-run` | 不能另建脚本或新台账掩盖未完成轮次 |
| 提速 | 固定清单已有 `--min-interval/--max-interval` | 不改全局默认，不绕过错误降速及互斥锁 |
| 估算、筛选与核验 | 只读业务库、已有缓存，生成本轮清单/报告 | 辅助代码不下载 PDF、不写新的采集状态 |
| 批次产品枚举 | `scripts/announcement_catalog_gap.py` | 保存官方清单；不增加 PDF 下载功能 |

Website 收录范围由其 `docs/collection-boundary.md` 定义：新能源整车可收录历史正式公告，
非新能源仅第 408、409 批；范围待核验不冒充符合。网站公开范围与本轮下载授权分别核实。
已有模式不能精确表达本轮范围时，先扩展既有参数/内部选择策略，不能扩大下载范围凑合执行。

### 能力扩展规则

先说明现有入口和具体缺口，再修改 `collection*.py`；必要时拆分内部模块，仍由既有入口调度。
继续复用 `collection.store_announcement`、数据库锁、状态收尾、解析模块和上游活动库。
不得另写独立下载循环，也不得以临时脚本、Shell 循环、Notebook 或后台进程规避。
新增日期或批次只形成 `var/runs/<本轮>/` 下的数据清单与运行参数，不形成另一份下载程序。
代码改动的验证覆盖本次能力缺口和已有恢复/去重边界；最终报告列明入口、run ID、
产品/型号数量、成功/异常/剩余数量及记录位置。

### 常用命令

在 vehicle-data-hub 根目录执行：

```bash
# 目录批量采集（直接执行下载；沿用原 seed 参数）
.venv/bin/python main.py gonggao collect --catalog-batch 32 --all-categories --limit 500
# 已审核的精确型号名单
.venv/bin/python main.py gonggao collect -f /绝对路径/型号名单.txt
# 已审核的新能源整车产品 ID 清单；默认只校验
.venv/bin/python main.py gonggao collect \
  --manifest var/runs/<本轮>/manifest.json \
  --manifest-sha256 <已核验SHA256> --run-dir var/runs/<本轮>
# 最近一轮或指定轮次报告
.venv/bin/python -m miit_gonggao.collection_report --run 24
# 近期公示/公告跟踪，只读生成计划
.venv/bin/python -m miit_gonggao.collection_tracking plan --limit 400
```

固定清单模式加 `--apply` 才实际下载；恢复已有轮次还须 `--resume-run <ID>`，
保持清单原文、SHA256 和运行目录一致。有效 PDF 校验路径、魔数、字节数、哈希后跳过，
解析问题保留记录。经用户明确要求可传 `--min-interval/--max-interval`，仅改变本轮节流；
下载异常恢复默认间隔，连续三次下载失败停止。该模式不会重新按型号搜索或增加其他批次。

目录模式、固定清单模式与近期事件模式保留各自的候选选择逻辑，统一复用
`collection.store_announcement` 的下载、参数解析和发布事务，以及数据库锁和逐型号状态收尾。
`collection_manifest.py` 是固定清单策略模块，不再另建独立下载脚本。
`scripts/announcement_catalog_gap.py` 只负责官方批次枚举和缓存，不下载 PDF。

`gonggao query/changes --download` 继续支持临时查询与文件下载，保留原 CLI 输出/退出码契约；
其 `manifest_*.json` 是原始查询下载证据。网站批量采集应使用 `gonggao collect`，
不要用临时查询下载代替业务库登记。

## 数据与历史

| 资产 | 上游路径 |
| --- | --- |
| 权威采集业务库 | `data/announcement_site.sqlite` |
| PDF 原件与非 PDF 源响应 | `downloads/announcement_site/` |
| CLI 查询/下载索引与源快照 | `downloads/announcement_site/_snapshots/` |
| 固定清单、断点、逐产品结果 | `var/runs/<本轮>/` |
| 采集报告 | `var/reports/` |

业务库保留 `ingestion_runs`、`run_models`、`documents`、`announcement_fields`，以及
公告、车型、商标、批次和公示跟踪表。判断历史下载结果须跨轮取最好结局；
`completed_at` 只表示轮次结束，不能据此宣称全部成功。
原 CLI 索引继续保留来源差异，不伪造为业务库的采集轮次。

2026-09-12 从 Website 完整迁移 10 张表：24 轮采集、41,309 条公告/文档记录、
41,297 条参数记录、41,261 条逐型号记录、19,604 条事件、474 条事件尝试。
31 个清单/日志/报告文件同步迁入；PDF 本体原已在上游，本次下载及搬动数量均为 0。
迁移前后库 SHA256 均为
`0e673b3321e60a4043df71bbf5ee35ba20f3cd53e56cb3e9ea61806d28869c75`，
逐表计数、文档引用摘要一致，`quick_check=ok`，所有文档路径和字节数核对通过。
这属于迁移时的核验结果，不是未来运行后的固定总量。

完整证据在 `var/reports/collection-upstream-migration-20260912.json`。
Website 原始库及日志备份在 `Website/var/migrations/collection-upstream-20260912/`。
Website 旧业务库、已迁移的运行目录和报告路径均为兼容符号链接，指向本项目唯一活动资产。
后续新增日志与报告直接在上游查询。原始选择器中的旧绝对路径保留，旧清单可以继续核验/恢复。

业务库和 `var/` 不纳入 Git。查询业务库使用只读连接；修订归档仍默认预览，
显式授权后才可运行 `python -m miit_gonggao.collection_revisions --apply`。
