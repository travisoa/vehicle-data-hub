# Agent Instructions

本项目主要由 Claude Code 和 Codex 直接操作。收到「查询/下载某车型」这类需求时，按本文件的流程执行，不要重新发明流程。

## 项目定位

统一的车型数据下载与对标分析工具：

- **汽车之家**（`autohome_cc/`）：按车型名抓配置，导出 Excel 对比表到 `output/`（含「配置分析」差异透视 sheet）
- **工信部公告**（`miit_gonggao/`）：按商标/型号前缀查公告，下载参数页 PDF 到 `downloads/<车型>/`；`review_export.py` 可把参数页 PDF 转成公告参数 Excel
- **竞品对标**（`benchmark/`）：抓汽车之家口碑 + 懂车帝销量榜，生成对标分析 HTML 报告到 `output/`

这是通用工具，不要把它当成只服务少数硬编码车型的脚本；车型相关条件一律走 `data/vehicle_profiles.json` 档案或 CLI 参数。

## 收到车型需求时的执行流程

统一用项目虚拟环境运行：`.venv/bin/python main.py ...`（首次先 `./scripts/bootstrap.sh`）。

1. **判断用户要哪类内容**：
   - 要配置对比 / 参数表 / Excel → 汽车之家：`--source autohome`
   - 要公告页 / 公告参数 PDF / 工信部数据 → 工信部：`--source gonggao`
   - 都要或未说明 → `fetch <车型>` 默认两个来源都抓
   - 要公告参数表 / 公告参数 Excel → 先确认 `downloads/<车型>/` 有 PDF（没有先 fetch），再 `main.py review <车型>`
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
   - 如果这个车型以后还会查，把条件沉淀进 `data/vehicle_profiles.json`（见下文格式）
5. **汇报结果**：列出生成的 Excel 路径、PDF 目录、批次号，以及失败/跳过项。

常用命令：

```bash
.venv/bin/python main.py fetch <车型A> <车型B>          # 两个来源
.venv/bin/python main.py fetch <车型> --source gonggao  # 只下公告 PDF（默认最新批次）
.venv/bin/python main.py fetch <车型> --all-batches --source gonggao  # 全部批次
.venv/bin/python main.py autohome --models "..." [--browser-login --browser-fallback]
.venv/bin/python main.py gonggao query ... --download   # 公告查询 CLI 全参数可用
.venv/bin/python main.py gonggao jianmian sync          # 减免税目录抓取入库（增量，--force 重下并作废转换缓存）
.venv/bin/python main.py gonggao jianmian search <市场名> --resolve [--download] [--latest-batch]
.venv/bin/python main.py gonggao jianmian export [--keyword <关键词>] [--xlsx 路径]
.venv/bin/python main.py review <车型> [-o 输出.xlsx] [--no-catalog]   # 公告 PDF -> 公告参数 Excel
.venv/bin/python main.py report <本品> --vs <竞品1> <竞品2> [--focus all|sales|koubei] [--offline] [--pages N]
```

## 减免购置税目录（jianmian）

- 数据源：装备中心 col1691 正式发布文章（标题含《减免车辆购置税的新能源汽车车型目录》），附件 .doc 解析后入 `data/jianmian_catalog.sqlite`，附件缓存在 `downloads/jianmian/<文章ID>_公告第N批/`（目录名 ID 前缀是缓存键锚点，旧版纯ID目录会自动迁移）
- 附件按批次自动命名（购置税目录第28批.doc / 车船税目录第83批.doc / 公告第404批附件.doc），派生缓存同步改名；目录内 `manifest.json` 维护 URL名->本地名 映射。`--force` 会重新下载并删除 `.html`/`.lo.docx`/`.word.docx` 再解析，不要手工改目录/文件名绕过它；存量迁移用 `jianmian rename`
- 解析依赖 macOS `textutil`（.doc 转 HTML）；表格被压平成单段，行重建依赖型号/企业名锚点，详见 `miit_gonggao/jianmian.py` 模块注释
- 入库全部新能源车型表（乘用车/客车/货车/专用车，表头含 型号+续航/电池；通用名称仅乘用车表有），截至2026-06 购置税目录第1~30批全量覆盖
- `articles` 表带 `status/error/row_count`：目录附件解析 0 行记 `zero_rows`、异常记 `error`，都不算已同步、下次 sync 默认重试；解析异常时先 rollback 再标 `error`，不提交半截 DELETE。部分附件成功、部分 0 行时写入成功附件并保留其余旧行，整篇仍标 `zero_rows`。空嗅探先完整解析（避免标题靠后的真目录被标成 `ok`/0 行）；`.docx` 精确转换失败时按 `zero_rows` 重试，不能标成「无目录附件」（仅 textutil html 不算已转出）。docx 已转出但仍无目录标题的按非目录跳过，不把整篇钉在 `zero_rows`。只有明确的「目录」/「推荐」才在嗅探阶段跳过。全文都是非目录附件时标 `ok`+「无目录附件」。旧库（status 为 NULL）视为已同步
- 解析主路径：textutil 廉价嗅探附件是否目录 → 目录附件经 `convert_doc_to_docx` 转 .docx 按真实表格精确解析；转换器优先级 LibreOffice headless（`.lo.docx` 缓存，无窗口）> 本机 Microsoft Word（`.word.docx` 缓存，会调起 Word；在其沙盒容器 `~/Library/Group Containers/UBF8T346G9.Office/` 内转换、不弹授权框）；两者都没有时退回压平文本启发式重建。缓存命中不再调用任何转换器（`--force` 除外）；LibreOffice 失败/超时（`SOFFICE_TIMEOUT`=300s，大合刊附件可能超）自动回退 Word
- 2020~2022 年的《新能源汽车推广应用推荐车型目录》表头无"通用名称"列，不入库（警告跳过属预期）
- 典型用途：新车只知道市场名时 `jianmian search <市场名> --resolve` 反查公告型号与商标，再沉淀进车型档案

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
- 工信部/EIDC 全部 HTTP 走 `core.http_request`（统一节流 0.8~1.8s + 5xx/网络错误/响应截断（chunked `IncompleteRead`）退避重试，4xx 直接抛出），新增请求不要绕开它
- `query --download` 单条 PDF 重试后仍失败只跳过并在结尾汇总，不中断整批。退出码区分三档：**0** 全部成功 / **1** 全失败（一份 PDF 都没拿到，含查询结果为空）/ **2** 部分成功（已拿到 PDF，另有失败或非 PDF 条目）。`jianmian search --download` 同一套语义（`core.download_exit_code`）；`main.py fetch` 把 2 当成功，只在结尾单独列出「部分成功」车型，不计入失败
- 优先 `requests + BeautifulSoup` / 标准库；只有确认必要才用 Playwright
- 工信部 PDF 接口的 `NECaptchaValidate` 沿用现有随机 token 方式，不要改动
- 缺失字段不要抛异常中断，记录日志后继续

## 输出规则

- 汽车之家 Excel → `output/汽车之家_<车型>配置表_<YYYYMMDD>.xlsx`（多车型取首车+总数：`汽车之家_<车型>等3个车型配置表_20260728.xlsx`；同名自动追加 `_2` 序号；命名逻辑在 `autohome_cc/utils/cleaners.py` 的 `build_export_filename`）（sheet：说明/车型信息/配置分析/详细配置表/颜色明细；配置分析按动力总成分组（能源×驱动×电池电量×座位数）每组一列，保留整合后的全量参数，组内多值合并总结，差异单元格高亮）
- 公告参数表 → `output/公告参数_<车型>_<批次>.xlsx`（多批次取区间如 `第394-406批`；关键参数 sheet 无「是否已评审」列，备注列写真实来源：公告参数页/减免车辆购置税目录/未显示未解析）；对标报告 → `output/对标报告_*.html`（销量标注懂车帝榜单具体月份，如「2026年05月销量」）
- 减免税目录导出 → `output/jianmian_catalog.xlsx`（单文件增量覆盖）；`output/jianmian_by_category/`（6 个固定分类 xlsx 覆盖更新）
- 口碑/销量原始数据缓存 → `downloads/benchmark/<车型>.json`（`report --offline` 依赖它；sales 字段为 `{dongchedi/autohome: {latest/half_year/year: {count,rank,label,scope}}}` 双源三维度结构；汽车之家 `scope=brand` 表示品牌检索兜底命中）
- 工信部 PDF → `downloads/<车型>/`；查询快照 `downloads/query_*.json`，下载索引 `downloads/manifest_*.json`
- PDF 只认 `%PDF` 魔数；不是 PDF 时保留 `.html` 供人工检查。非 PDF 属预期情形，只有**全部**条目都没拿到 PDF 才算失败（退出码 1），部分非 PDF 退出码为 2
- 不要删除既有 `output/`、`downloads/` 内容，除非用户明确要求

## 开发规则

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
