<div align="center">

# Vehicle Data Hub

> 车型数据一站式采集：汽车之家配置对比、工信部公告参数页、减免税目录反查、口碑销量竞品对标。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Platform: macOS | Linux](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-lightgrey.svg)](#运行环境)
[![CI](https://github.com/travisoa/vehicle-data-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/travisoa/vehicle-data-hub/actions/workflows/ci.yml)
[![Sources](https://img.shields.io/badge/数据源-汽车之家%20%2F%20工信部%20%2F%20懂车帝-2EAD33.svg)](#功能特性)

<br>

竞品分析所需的配置表、准入参数与销量口碑分属三个来源，口径、反爬与格式各不相同。<br>
工信部《公告》按产品商标与车辆型号登记，与对外宣传的市场名经常不一致。<br>
公告参数页为逐份 PDF，转入评审模板需人工誊写，且部分参数仅存在于减免税目录。

[初衷](#初衷) · [运行效果](#运行效果) · [功能特性](#功能特性) · [快速开始](#快速开始) · [命令速查](#命令速查) · [使用详解](#使用详解) · [车型档案](#统一车型档案) · [输出产物](#输出产物) · [参考文档](#参考文档)

</div>

---

## 初衷

### 数据来源分散，采集成本高于分析成本

> **同一车型的配置、公告与销量口碑分属三个来源，采集口径互不相通。**

配置数据在汽车之家，准入参数在工信部《公告》，销量与口碑分布于懂车帝与车主评价。三个来源各有独立的
接口口径、访问限制与数据格式，单次竞品分析的主要工时消耗在数据归集环节，而非差异研判。

本工具将该链路收敛为单条命令：输入车型名，配置 Excel、公告参数页 PDF、对标报告 HTML
一次生成；车型的查询条件沉淀至[统一车型档案](#统一车型档案)，供后续复用。

### 市场名与公告商标不一致

> **《公告》按产品商标与车辆型号登记，与市场名无固定对应关系。**

部分车型的公告商标由代工方或集团母公司持有，与市场品牌并不一致；车辆型号则为无语义的字母数字组合。仅凭市场名无法直接检索到对应记录。

本工具将工信部《减免车辆购置税的新能源汽车车型目录》各批次附件解析入本地 SQLite，
以通用名称反查公告型号与商标（详见[减免税目录反查](#减免税目录反查jianmian)），
使仅知市场名的车型亦可定位到对应的公告参数页。

---

## 运行效果

以下为产物结构示意。命令、文件命名规则与工作表结构为实际行为，终端输出文案有所精简。

<table width="100%" border="1" cellpadding="12" cellspacing="0">
<tr>
<th width="50%" align="center">单条命令覆盖两个来源<br><sub>配置 Excel 与公告 PDF 同时落盘</sub></th>
<th width="50%" align="center">公告 PDF 转评审 Excel<br><sub>按批次命名，目录参数自动补齐</sub></th>
</tr>
<tr>
<td width="50%" valign="top">

<pre><code>$ main.py fetch &lt;车型A&gt; &lt;车型B&gt;

=== [汽车之家] &lt;车型A&gt;, &lt;车型B&gt; ===
output/汽车之家_&lt;车型A&gt;等2个车型配置表_20260823.xlsx

=== [工信部公告] &lt;车型A&gt; ===
downloads/announcement_site/&lt;品牌&gt;/&lt;车型A&gt;/第406批/&lt;品牌&gt;_&lt;车辆型号&gt;_406_*.pdf
downloads/announcement_site/_snapshots/manifest_20260823_*.json

全部任务完成。</code></pre>

</td>
<td width="50%" valign="top">

<pre><code>$ main.py review &lt;车型&gt;

解析 downloads/announcement_site/&lt;品牌&gt;/&lt;车型&gt;/**/*.pdf … 12 份
目录补参数（购置税目录优先）… 命中 12
output/公告参数_&lt;车型&gt;_第394-406批.xlsx
  ├─ 关键参数（行=参数项，列=各配置）
  └─ 数据来源（备注列标注真实出处）</code></pre>

</td>
</tr>
</table>

<table width="100%" border="1" cellpadding="12" cellspacing="0">
<tr>
<th width="50%" align="center">配置表按动力总成分组透视<br><sub>非原始配置表的逐列转录</sub></th>
<th width="50%" align="center">对标报告为自包含 HTML<br><sub>无外部依赖，可直接分发</sub></th>
</tr>
<tr>
<td width="50%" valign="top">

<pre><code>汽车之家_&lt;车型&gt;配置表_20260728.xlsx
├─ 说明
├─ 车型信息
├─ 配置分析          ← 核心
│   ├─ 车型配置强度（标配/选配/独有计数）
│   ├─ 整合参数明细（每个动力总成分组一列）
│   │    「增程四驱60度电池6座」
│   │    「纯电两驱100度电池7座」
│   └─ 车型独有配置
├─ 详细配置表（保留全部车型列）
└─ 颜色明细</code></pre>

</td>
<td width="50%" valign="top">

<pre><code>$ main.py report &lt;本品&gt; --vs &lt;竞品1&gt; &lt;竞品2&gt;

output/对标报告_&lt;本品&gt;_20260823_160241.html
├─ 综合研判（销量位次/口碑分差/结论）
├─ 核心指标卡片
├─ 双源销量对比（懂车帝 + 汽车之家）
│    最新月 / 近半年 / 近12个月
├─ 口碑分维度评分对比条形图
├─ 亮点 / 槽点 + 用户原声
└─ 本品改进建议</code></pre>

</td>
</tr>
</table>

---

## 功能特性

### 汽车之家配置抓取

对应命令：`main.py autohome ...` 与 `main.py fetch --source autohome`

<table>
<colgroup><col width="1%"><col></colgroup>
<thead>
<tr><th align="left" nowrap width="1%">能力</th><th align="left">说明</th></tr>
</thead>
<tbody>
<tr><td nowrap width="1%"><strong>按名抓取</strong></td><td>输入车系名导出<strong>全部在售配置</strong>；带年款的具体版本名仅匹配最相关的一个（<code>autohome_cc/crawler/search.py</code>）</td></tr>
<tr><td nowrap width="1%"><strong>配置分析</strong></td><td>按<strong>动力总成分组</strong>（能源×驱动×电池电量×座位数）透视，组内多值合并归纳——数值型输出 <code>2995/3200</code>，配置型标注「区分高低配」；独有配置标绿，组间差异标黄</td></tr>
<tr><td nowrap width="1%"><strong>多车合表</strong></td><td>多车型一次导出至同一工作簿，命名取首车加总数；同日重复导出自动追加 <code>_2</code>、<code>_3</code>，<strong>不覆盖</strong>既有文件</td></tr>
<tr><td nowrap width="1%"><strong>登录受限兜底</strong></td><td>命中权限或验证页时提供三种方式：<code>--browser-login</code> 扫码登录、<code>--cookie-file</code> 导入 cookies、<code>--browser-fallback</code> 浏览器渲染兜底（详见<a href="#登录受限时">登录受限时</a>）</td></tr>
<tr><td nowrap width="1%"><strong>离线复现</strong></td><td><code>--capture-file</code> 直接解析本地抓包 HTML，无需联网即可验证导出链路</td></tr>
</tbody>
</table>

### 工信部公告与减免税目录

对应命令：`main.py gonggao query ...`、`main.py gonggao changes ...`（只读）、
`main.py gonggao jianmian ...` 与 `main.py fetch --source gonggao`

<table>
<colgroup><col width="1%"><col></colgroup>
<thead>
<tr><th align="left" nowrap width="1%">能力</th><th align="left">说明</th></tr>
</thead>
<tbody>
<tr><td nowrap width="1%"><strong>公告查询</strong></td><td>按商标、企业、车辆型号、名称与批次组合筛选；支持型号前缀的<strong>包含与排除</strong>（<code>--model-prefix</code> / <code>--exclude-model-prefix</code>）及任意字段后置筛选（<code>--row-filter</code>）</td></tr>
<tr><td nowrap width="1%"><strong>变更扩展公示</strong></td><td><code>gonggao changes</code> 按公示企业、商标、产品名称或产品型号<strong>只读</strong>查询工信部变更扩展清单，供提前了解尚未正式发布的变更。公示不下载、不入库、不作为任何采集入口；公示批次与正式公告批次不强制相同。</td></tr>
<tr><td nowrap width="1%"><strong>正式发布重发刷新</strong></td><td><code>gonggao collect --republished-from-status</code>（或 <code>--republished-batch &lt;批次&gt;</code>）按<strong>正式公告</strong>的重新发布记录刷新已有参数页：同一产品 ID 在更高正式批次再次出现即为变更扩展或勘误重发。前者读已落盘的收录统计记录、不重算，只收 PDF 已解析的产品；后者按批次枚举缓存现算，只要求本地有有效 PDF，因此已下载但解析失败的重发只有后者会选中。两者都排除底盘等非整车。</td></tr>
<tr><td nowrap width="1%"><strong>参数页下载</strong></td><td><code>--download</code> 下载参数页 PDF 至 <code>downloads/announcement_site/&lt;品牌&gt;/&lt;车型&gt;/第&lt;批次&gt;批/</code>；查询快照与下载索引存入 <code>_snapshots/</code>；按 <code>%PDF</code> 头校验，非 PDF 时保留 <code>.html</code> 供人工核查；单条失败仅跳过并在结尾汇总，<strong>不中断整批</strong></td></tr>
<tr><td nowrap width="1%"><strong>动态修订归档</strong></td><td>公告网站的参数页 PDF 由查询接口动态生成。同一产品 ID 后续返回的字段或批次发生变化时，当前版本留在主目录，旧快照归入 <code>downloads/announcement_site/_revisions/</code> 并在 <code>manifest.json</code> 记录哈希、PDF 生成时间、身份字段及差异；历史快照不参与普通车型查询和分类索引。</td></tr>
<tr><td nowrap width="1%"><strong>目录反查</strong></td><td><code>jianmian search &lt;市场名&gt;</code> 以通用名称反查公告型号与商标；<code>--resolve</code> 经公告接口确认，<code>--download</code> 直接下载参数页</td></tr>
<tr><td nowrap width="1%"><strong>目录入库</strong></td><td><code>jianmian sync</code> 增量抓取全部批次附件并解析入 SQLite（覆盖乘用车、客车、货车、专用车全类别）；<code>articles</code> 表记录 <code>status/error/row_count</code>，<code>zero_rows</code> 与 <code>error</code> 状态下次自动重试</td></tr>
<tr><td nowrap width="1%"><strong>.doc 精确解析</strong></td><td>老式二进制 .doc 先转 .docx，按<strong>真实表格结构</strong>解析（含纵向合并），避免转换工具压平表格导致单元格边界丢失；按 LibreOffice headless、Microsoft Word AppleScript、启发式重建三级降级，各级结果均有缓存</td></tr>
<tr><td nowrap width="1%"><strong>目录导出</strong></td><td><code>jianmian export</code> 导出 Excel，支持关键词过滤与<strong>按车辆类别分 6 个文件</strong>输出（<code>--by-category</code>）</td></tr>
</tbody>
</table>

### 公告参数表转换

对应命令：`main.py review ...`（[使用详解](#公告参数表review)）

<table>
<colgroup><col width="1%"><col></colgroup>
<thead>
<tr><th align="left" nowrap width="1%">能力</th><th align="left">说明</th></tr>
</thead>
<tbody>
<tr><td nowrap width="1%"><strong>模板对齐</strong></td><td>PDF 转 Excel，行结构对齐《公告参数评审模板》：<strong>行为参数项、列为各配置型号</strong>，附「备注」列标注真实来源</td></tr>
<tr><td nowrap width="1%"><strong>坐标级解析</strong></td><td>按<strong>词坐标</strong>解析整车参数页的底部多栏交错区，以及改装车参数页的横向底盘表和全宽说明</td></tr>
<tr><td nowrap width="1%"><strong>目录补参数</strong></td><td>公告页未显示的字段（通用名称、续驶里程、燃料消耗量、电池质量、储电量）按公告型号自动从目录库补齐（购置税目录优先），并以<strong>浅黄底色</strong>标注</td></tr>
<tr><td nowrap width="1%"><strong>批次命名</strong></td><td>默认输出 <code>公告参数_&lt;车型&gt;_&lt;批次&gt;.xlsx</code>；跨批次时取区间（与 PDF 顺序无关）如 <code>第394-406批</code>；同名自动追加序号</td></tr>
<tr><td nowrap width="1%"><strong>来源可追溯</strong></td><td>「备注」列填写「公告参数页」「减免车辆购置税目录」等实际出处；未显示或未命中的字段明确标注为未解析或未命中，避免空值歧义</td></tr>
</tbody>
</table>

### 竞品对标分析

对应命令：`main.py report ...`（[使用详解](#竞品对标报告report)）

<table>
<colgroup><col width="1%"><col></colgroup>
<thead>
<tr><th align="left" nowrap width="1%">能力</th><th align="left">说明</th></tr>
</thead>
<tbody>
<tr><td nowrap width="1%"><strong>综合研判</strong></td><td>由销量位次、口碑分差、优势与短板维度、价格定位合成要点与结论，置于报告顶部</td></tr>
<tr><td nowrap width="1%"><strong>双源三维度销量</strong></td><td>懂车帝全市场榜与汽车之家细分级别榜并行，各按「最新月 / 近半年 / 近12个月」呈现销量与排名，表头标注<strong>具体月份区间</strong>；三个维度均由数据源原生支持，<strong>非本地累加</strong></td></tr>
<tr><td nowrap width="1%"><strong>口碑深度</strong></td><td>分维度评分对比条形图、官方结构化亮点与槽点总结、「不满意」评价<strong>关键词统计</strong>及用户原声</td></tr>
<tr><td nowrap width="1%"><strong>改进建议</strong></td><td>由本品槽点<strong>频次</strong>与相对竞品的<strong>维度分差</strong>两路生成</td></tr>
<tr><td nowrap width="1%"><strong>板块可裁剪</strong></td><td><code>--focus all</code> 综合（默认）、<code>sales</code> 仅销量、<code>koubei</code> 仅口碑；未指定 <code>--vs</code> 时生成<strong>单车画像</strong></td></tr>
<tr><td nowrap width="1%"><strong>自包含产物</strong></td><td>HTML 无外部依赖，可直接分发；原始响应缓存于 <code>downloads/benchmark/</code>，支持 <code>--offline</code> 离线复现</td></tr>
</tbody>
</table>

### 命令选择

统一使用项目虚拟环境运行：`.venv/bin/python main.py ...`（下表省略该前缀）。

| 目标产物 | 对应命令 | 示例 |
| --- | --- | --- |
| 配置对比表 / 参数 Excel | **汽车之家** | `fetch <车型A> --source autohome` |
| 公告页 / 准入参数 PDF | **工信部公告** | `fetch <车型A> --source gonggao` |
| 两者均需，或来源未定 | **两个来源** | `fetch <车型A> <车型B>` |
| 公告参数评审表 | **review** | `review <车型>`（需 `downloads/announcement_site/<品牌>/<车型>/` 已有 PDF） |
| 竞品对标 / 口碑 / 销量 | **report** | `report <本品> --vs <竞品1> <竞品2>` |
| 仅知市场名，无法检索公告 | **jianmian** | `gonggao jianmian search <市场名> --download` |
| 已知商标或型号，临时查询 | **gonggao query** | `gonggao query --trademark "<商标>" --model-prefix <型号前缀> --latest-batch` |

---

## 快速开始

### 运行环境

Python 3.11+，**macOS / Windows / Linux 三平台均可运行**。全部功能都不依赖任何平台专有工具，
唯一的外部依赖是减免税目录同步时用来转换 `.doc` 附件的 LibreOffice：

| 功能 | 额外依赖 |
| --- | --- |
| `fetch` / `autohome` / `gonggao query` / `review` / `report` / `profiles` | 无，纯 Python |
| `gonggao jianmian search` / `export` | 无，读取本地 SQLite |
| `gonggao jianmian sync` / `rename` | **LibreOffice**（把目录附件的老式 `.doc` 转成 `.docx`） |

`find_soffice()` 先查 `PATH`，再探测三平台的默认安装位置——Windows 版 LibreOffice 装完
**默认不写入 PATH**，由代码自动补上 `C:\Program Files\LibreOffice\program\soffice.exe`。

macOS 上还有两条额外的加速与兜底通道，其他平台没有也不影响正确性：

- **`textutil` 廉价嗅探**（macOS 自带）：在昂贵的 `.docx` 转换之前先花几十毫秒判断附件是不是目录，
  把公告附件直接筛掉。其他平台没有它，改为转换后用 `peek_catalog_info_docx` 判定——结论一致，
  但非目录附件也要经过一次转换。实测缓存中 32% 的附件（按体积 51%）属于这一类，
  因此**首次全量同步的转换工作量约为 macOS 的两倍**。转换结果有缓存、判定为非目录的文章会标记为
  已同步，所以这是一次性成本，后续增量同步没有差异。
- **Microsoft Word AppleScript 通道**：LibreOffice 转换失败或超时后的兜底，仅 macOS 可用。
  其他平台只有 LibreOffice 一条路，遇到几十 MB 的多目录合刊附件可能触发默认 300 秒超时，
  可通过环境变量调大：`SOFFICE_TIMEOUT=900`。

CI 在 ubuntu 上执行离线单测，验证的即是这部分平台无关逻辑。

### 安装

```bash
./scripts/bootstrap.sh
```

Windows 使用 PowerShell 版本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

依赖分三层，按需安装：

| 依赖层 | 内容 | 适用场景 |
| --- | --- | --- |
| `requirements.txt` | requests / bs4 / openpyxl / pdfplumber 等 | 默认安装 |
| `requirements-browser.txt` | Playwright（含 Chromium） | 仅 `--browser-login` / `--browser-fallback` 需要，通过 `INSTALL_PLAYWRIGHT=1 ./scripts/bootstrap.sh` 安装 |
| `requirements-dev.txt` | pytest / ruff | 开发与测试，通过 `INSTALL_DEV=1 ./scripts/bootstrap.sh` 安装 |

目录同步需要 LibreOffice：macOS `brew install --cask libreoffice`、Windows `winget install TheDocumentFoundation.LibreOffice`、Linux 用发行版包管理器。
其余功能不需要它。macOS 上未安装时可回退至 Microsoft Word 通道。

### 验证安装

```bash
.venv/bin/python -m pytest             # 解析边界与导出链路（离线，依赖 tests/fixtures）
.venv/bin/python scripts/smoke_test.py # 离线 Excel 链路；附件缓存存在时附带深度解析校验
.venv/bin/python -m ruff check .       # lint
.venv/bin/python main.py profiles      # 列出统一车型档案
```

### 典型用法

```bash
# 1. 两个来源同时抓取：配置 Excel 输出至 output/，公告 PDF 输出至 downloads/announcement_site/<品牌>/<车型>/<批次>/
.venv/bin/python main.py fetch <车型A> <车型B>

# 2. 公告 PDF 转公告参数 Excel（自动补充减免税目录参数）
.venv/bin/python main.py review <车型>

# 3. 生成口碑与销量竞品对标 HTML 报告
.venv/bin/python main.py report <本品> --vs <竞品1> <竞品2>
```

`fetch` 的工信部查询默认仅保留**最新公告批次**（档案命中走 `gonggao query --latest-batch`，
未命中档案的减免目录反查同样带 `--latest-batch`），
使用 `--all-batches` 保留全部批次。未命中档案的车型，工信部侧自动转为按市场名走减免税目录反查，
成功后提示将建议的 `model_prefixes` 沉淀至档案。输出目录可按来源分别指定：
`--autohome-output-dir`（Excel，默认 `output/`）与 `--gonggao-output-dir`（PDF，默认 `downloads/announcement_site/`）。

`--download` 的退出码分三档，便于脚本区分「白跑一趟」和「拿到了但需人工检查」：

| 退出码 | 含义 |
| :---: | --- |
| `0` | 全部成功，每条都是 PDF |
| `1` | 全失败——一份 PDF 都没拿到（含查询结果为空） |
| `2` | 部分成功——已拿到 PDF，另有条目下载失败或返回的不是 PDF |

接口未返回 PDF 属预期情形（保留同名 `.html` 供人工检查），因此不会让整批算作失败。
`main.py fetch` 据此把退出码 2 视为成功，仅在结尾单独列出「部分成功」的车型。

---

## 命令速查

`main.py` 提供六个顶层命令：

| 命令 | 说明 |
| --- | --- |
| `fetch <车型>...` | 按统一车型档案同时抓取两个来源 |
| `autohome ...` | 汽车之家配置抓取完整 CLI（`--models`、`--capture-file`、`--cookies`、`--browser-login` 等） |
| `gonggao ...` | 工信部公告查询完整 CLI（含 `collect`、`query`、`changes`、`profiles`、`jianmian` 子命令） |
| `review <车型/PDF>...` | 公告参数页 PDF 转公告参数 Excel（[详解](#公告参数表review)） |
| `report <车型> --vs ...` | 口碑与销量竞品对标 HTML 报告（[详解](#竞品对标报告report)） |
| `profiles` | 列出统一车型档案 |
| `profiles add <市场名>...` | 按市场名反查公告条件并写入档案（`-f` 名单文件可批量，`--dry-run` 预览） |

网站批量收录、补采及重试先使用 `main.py gonggao collect`，
按 [公告采集契约](#公告批量采集) 选择模式；不要为新批次或提速另建下载脚本。
能力不足时扩展原入口的参数或内部策略。下面的 `query --download` 用于独立查询下载，
不能代替网站采集库登记；`changes` 只读查询公示，任何情况下都不作为采集入口。

两个来源的全部参数均在各自子命令下可用：

```bash
.venv/bin/python main.py gonggao query --trademark "<商标>" --model-prefix <型号前缀> --latest-batch --download
.venv/bin/python main.py gonggao changes --model-code <完整公告型号>
.venv/bin/python main.py autohome --models "<车型名>" --browser-login --browser-fallback
.venv/bin/python main.py autohome --capture-file /path/to/autohome_config.html
```

<details>
<summary><strong>常用参数明细</strong>（点击展开）</summary>

**`gonggao query`**：`--trademark` 产品商标 / `--company` 企业 / `--model-code` 型号模糊匹配 /
`--vehicle-name` 车辆名称 / `--pc` 批次 / `--model-prefix` 型号前缀（可重复）/
`--exclude-model-prefix` 排除前缀（可重复）/ `--row-filter FIELD=VALUE` 任意字段后置筛选（可重复）/
`--latest-batch` 仅保留最高批次 / `--all-pages` 拉取全部分页 / `--download` 下载 PDF /
`--detail-html` 同时保存技术参数 HTML / `--vehicle-folder` 指定下载目录名 / `--limit` 条数限制

**`gonggao changes`**（只读，不下载）：`--company` 公示企业 / `--trademark` 公示商标 /
`--product-name` 公示产品名称 / `--model-code` 公示产品型号 /
`--limit` 限制返回数量 / `--all` 显式允许无条件遍历整批 /
`--notice-url` 指定新的工信部变更扩展公示文章。默认使用项目已验证的官方公示入口；
工信部发布新批次且文章随机 URL 变化时，应把该批次文章 URL 通过 `--notice-url` 传入。
公示只用于提前了解，正式发布后的刷新一律走 `gonggao collect --republished-from-status`。

**`gonggao jianmian`**：`sync`（`--max-pages` / `--max-articles` / `--force` 重新下载并作废转换缓存）、
`search <关键词>`（`--resolve` 经公告接口反查 / `--download` 反查后下载，隐含 `--resolve` / `--latest-batch` 只下最高批次）、
`export`（`--keyword` 过滤 / `--by-category` 分 6 类导出）、`rename`（存量附件按批次改名迁移）

**`review`**：`-o` 输出路径 / `--title` 报表标题中的车型名 / `--db` 目录库路径 /
`--no-catalog` 不补充目录参数 / `--download-dir` 公告 PDF 根目录

**`report`**：`--vs` 竞品车型（可多个）/ `--focus all|sales|koubei` /
`--pages N` 口碑抓取页数（默认 5，每页约 10 条）/ `--offline` 使用缓存离线生成 / `-o` 输出路径

**`autohome`**：`--models` 逗号分隔车型名 / `--capture-file` 本地抓包文件（可重复）/ `--cookies` /
`--cookie-file` / `--browser-login` / `--browser-fallback` / `--browser-profile-dir` /
`--browser-channel` / `--output-dir`

</details>

---

## 公告批量采集

公告批量收录、补采、恢复和提速统一使用本项目 `main.py gonggao collect`；近期事件由
`miit_gonggao.collection_tracking` 处理。PDF、解析参数、下载台账和日志均在上游，
Website 只读构建站点派生库。强制复用约束见 [AGENTS.md](AGENTS.md#批量采集复用约束)。

### 按任务选择

| 任务 | 复用方式 | 边界 |
| --- | --- | --- |
| 目录或型号名单采集 | `gonggao collect` 的目录参数或 `-f` | 会查询同型号所有正式批次；不是固定批次下载 |
| 已核验汽车整车产品 ID | `gonggao collect --manifest ...` | 只处理冻结清单；新能源 scope 仅收新能源，通用 scope 的非新能源/未知仅 408/409 批 |
| 近期正式公告 | `collection_tracking` 的登记、`plan`、`collect` | `plan` 只读；`collect` 会下载，受当前授权约束 |
| 正式发布重发刷新 | `gonggao collect --republished-from-status` 或 `--republished-batch` | 只刷新本地已有有效参数页且被更高正式批次重发的整车；公示不作为来源；`--dry-run` 只出清单不下载 |
| 中断恢复 | 固定清单原文、哈希、目录与 `--resume-run` | 不能另建脚本或新台账掩盖未完成轮次 |
| 提速 | `gonggao collect` 各模式与固定清单共用 `--min-interval/--max-interval` | 不改全局默认，不绕过错误降速及互斥锁 |
| 收录数量、缺口与历史变化 | `gonggao status`、`--json`、`--history` | 默认读取持久统计；需要重新计算时显式 `--refresh` |
| 估算、筛选与核验 | 先读持久统计及候选清单，再核验必要的源记录 | 不另建统计台账，不附带 PDF 下载 |
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
# 读取最新收录统计，不重新统计或下载
.venv/bin/python main.py gonggao status
.venv/bin/python main.py gonggao status --json
# 读取最近十次统计变化；重新计算使用单独的 --refresh
.venv/bin/python main.py gonggao status --history
.venv/bin/python main.py gonggao status --refresh
# 最近一轮或指定轮次报告
.venv/bin/python -m miit_gonggao.collection_report --run 24
# 近期正式公告跟踪，只读生成计划
.venv/bin/python -m miit_gonggao.collection_tracking plan --limit 400
```

固定清单模式加 `--apply` 才实际下载；恢复已有轮次还须 `--resume-run <ID>`，
保持清单原文、SHA256 和运行目录一致。有效 PDF 校验路径、魔数、字节数、哈希后跳过，
解析问题保留记录。经用户明确要求可传 `--min-interval/--max-interval`，仅改变本轮节流；
下载异常恢复默认间隔，连续三次下载失败停止。该模式不会重新按型号搜索或增加其他批次。
固定清单使用 schema_version=1。`nev_complete_manifest_v1`（及兼容旧 scope）仅接受新能源；
`automotive_complete_manifest_v1` 在第 408、409 批接受燃油、普通混动与动力未知整车，其他批次仍须新能源。
各 scope 均执行整车判定，不依赖清单自报能源；恢复时 scope 与清单哈希必须一致。
需要核验新能源范围时优先按官方产品名判断；产品名只写混合动力等无法确认类型时，只读查询
`--catalog-db`（默认 `data/jianmian_catalog.sqlite`）中精确同型号的具体能源证据。
通用“新能源/新能源汽车”标签不单独证明类型；具体能源冲突、普通混动或无证据均拒绝。
不采用清单自报的 `catalog_energy`，不改写官方产品名；整车范围、清单哈希和数量校验仍须通过。

目录模式、固定清单模式与近期事件模式保留各自的候选选择逻辑，统一复用
`collection.store_announcement` 的下载、参数解析和发布事务，以及数据库锁和逐型号状态收尾。
`collection_manifest.py` 是固定清单策略模块，不再另建独立下载脚本。
`scripts/announcement_catalog_gap.py` 只负责官方批次枚举和缓存，不下载 PDF。

`gonggao query --download` 继续支持临时查询与文件下载，保留原 CLI 输出/退出码契约；
其 `manifest_*.json` 是原始查询下载证据。网站批量采集应使用 `gonggao collect`，
不要用临时查询下载代替业务库登记。

### 下载记录与数据目录

| 资产 | 上游路径 |
| --- | --- |
| 权威采集业务库 | `data/announcement_site.sqlite` |
| PDF 原件与非 PDF 源响应 | `downloads/announcement_site/` |
| CLI 查询/下载索引与源快照 | `downloads/announcement_site/_snapshots/` |
| 固定清单、断点、逐产品结果 | `var/runs/<本轮>/` |
| 采集报告 | `var/reports/` |
| 持久收录统计与候选清单 | `var/reports/collection-status/` |

日常查询“已收录多少、还缺多少”先执行 `main.py gonggao status`，或读取
`var/reports/collection-status/latest.json`。默认只读最新汇总，并比较业务库、目录库
（含 WAL/journal）、批次缓存文件元数据及统计规则代码哈希；不扫描业务表、解析批次缓存或
遍历 PDF。输入变化显示 `stale`，没有快照显示 `missing`，均不会自行重算。
`--json` 输出状态及汇总，`--history` 只读最近十代变化；明确需要更新时使用 `--refresh`。

每代记录位于 `snapshots/<generation>/`，包含 `summary.json`、`candidates.json`、
`report.md`。文件全部生成后才原子更新 `latest.json`；刷新失败保留旧最新记录，并写
`last-error.json`，不能把旧汇总当成已更新。候选按产品 ID 保留，去重型号数另列；
统计范围为汽车整车的新能源历史记录，以及第 408、409 批非新能源和动力未知候选。
动力未知独立列示，不计入已确认能源类别；候选不扩大本轮下载授权。
目录型号六桶、事件状态及 Website 公开库占位各有口径，不能与产品缺口相加或互相替代。

目录/型号名单、固定产品清单、近期事件采集在正常或中断收尾提交后更新一次统计。
近期事件登记及仅更新已解决事件的操作、`main.py gonggao jianmian sync` 的目录库变更、
批次枚举命令的缓存写入，也在本次操作收尾统一更新；输入未变化时复用已有快照。
不在每条 PDF 下载后重扫全库。独立 `review_export`/`main.py review` 仅导出文件，
不回写业务库，不改变收录状态。

过期的完整批次缓存保留为历史基线，并显示来源有效期；它不证明官方当前全集，
部分/缺失缓存也不等于零缺口。事件窗口以汇总 `as_of_date` 为准，日期变化单独提示，
不触发日常查询重算。查看统计不检查 PDF 文件变化；手动修改 PDF 而未改变库或缓存后，
应显式 `--refresh` 重新核验路径、大小和文件头，不能将默认查看视为逐文件 SHA-256 校验。

完整批次减少时，自动刷新和普通 `--refresh` 保留上一基线，并指出减少的批次。
先恢复该批合格缓存；枚举跳过交叉验证造成的降级，可用默认 `--verify-every 1` 重建相应批次。
确需调整统计来源基线时，使用专门入口，不删除 `latest.json`：

```bash
.venv/bin/python main.py gonggao status --refresh --rebase-cache \
  --expect-generation <当前generation> --removed-batches <减少的批次，逗号分隔> \
  --rebase-reason '说明调整来源范围的原因'
```

旧代标识、减少批次和原因必须一致；空全集、进行中的采集及计算期间输入变化仍会阻断。
新快照保留历史链，并在 `cache_rebase`、报告和 `--history` 中记录基线变化，不能将该差额当作下载量。
自动采集不使用此选项；正常收尾仍按原规则更新。

缓存诊断按批次保存于 `summary.inputs.batch_caches.cache_diagnostics`。
已验证的上游接口缺表作为 `notices` 中的来源限制，保留日期和依据，不算成功的空批次；
部分枚举失败、缓存校验失败和没有来源证据的缓存缺失分别告警，列出批次与原因。
较旧且已被成功缓存替代的失败证据只保留审计记录；较新失败仍提示当前使用旧完整缓存。
旧版未分类记录保守提示，显式刷新后采用新分类；默认查看仍只读取已保存的诊断。

业务库保留 `ingestion_runs`、`run_models`、`documents`、`announcement_fields`，以及
公告、车型、商标、批次和正式公告跟踪表。判断历史下载结果须跨轮取最好结局；
`completed_at` 只表示轮次结束，不能据此宣称全部成功。
原 CLI 索引继续保留来源差异，不伪造为业务库的采集轮次。

Website 的旧采集脚本仅转发，旧业务库与已迁移运行目录是上游资产的符号链接。
新任务直接使用本项目入口和路径，不能另建活动库或在兼容脚本内增加下载逻辑。
原 CLI 查询索引保留原始来源，不伪造为批量采集轮次。业务库、PDF、`var/` 不纳入 Git。
迁移清单保留在 `var/reports/collection-upstream-migration-20260912.json`，
原始备份在 Website 的 `var/migrations/collection-upstream-20260912/`；不能覆盖后续新增记录。

修订归档使用 `python -m miit_gonggao.collection_revisions`，默认预览，明确授权后才加 `--apply`。

### 公告批次枚举缓存

`python scripts/announcement_catalog_gap.py --batch <批次> --fetch-only` 用于完整枚举。
缓存 v4 仅复用 7 天内的完整结果；关键词或校验失败保存 `.partial.json` 并以失败结束，
缺失批次保存 `.absent.json` 并在下一次重新探测，均不覆盖已有成功缓存。
近期正式公告跟踪由本项目 `miit_gonggao.collection_tracking` 写入上游权威业务库；Website 只读构建派生库。
公示表格按实际表头定位企业、产品名称、型号，兼容新产品及变更扩展列顺序；
无法识别时停止，不把错位或不完整结果当作有效清单，行错误指出表格行号。
明确的整行合计可跳过，未知合并行仍阻断登记。`notice_batch` 取文章批次，
原始表格批次/混合列分别保留为 `raw_notice_batch` / `batch_or_chassis_id`，不把底盘 ID 当批次。

## 使用详解

### 减免税目录反查（jianmian）

新车型通常仅有市场名（通用名称），而公告商标可能与品牌不一致（例如由代工方持有商标时，公告商标与市场品牌完全不同）。
`jianmian` 将工信部《减免车辆购置税的新能源汽车车型目录》各批次附件解析入本地 SQLite
（`data/jianmian_catalog.sqlite`，含通用名称、车辆型号、企业、续航、电池能量等字段，
覆盖乘用车、客车、货车、专用车全部类别；通用名称字段仅乘用车表具备），支持反查与导出：

```bash
.venv/bin/python main.py gonggao jianmian sync                      # 增量抓取全部批次（约240篇文章，首次耗时较长）
.venv/bin/python main.py gonggao jianmian search <市场名>            # 检索目录库
.venv/bin/python main.py gonggao jianmian search <市场名> --resolve  # 经公告接口反查商标与批次
.venv/bin/python main.py gonggao jianmian search <市场名> --download # 反查后直接下载公告参数页 PDF
.venv/bin/python main.py gonggao jianmian export --keyword <关键词>      # 导出 Excel 至 output/jianmian_catalog.xlsx
.venv/bin/python main.py gonggao jianmian export --by-category      # 全量.xlsx + 6 个分类文件导出至 output/jianmian_by_category/
```

**关于 .doc 转 .docx**：目录附件为老式二进制 .doc，`textutil` 会将表格压平并丢失单元格边界，
因此入库前先转为 .docx，按真实表格结构精确解析（含纵向合并）。转换器按优先级自动选择，
各级结果均有缓存，同一附件仅转换一次：

1. **LibreOffice headless**（`soffice --headless --convert-to docx`，无窗口、无授权弹窗）→ 缓存为 `<附件>.lo.docx`
2. **本机 Microsoft Word**（经 AppleScript 调用，转换在 Word 沙盒容器内完成、不弹授权框，但会启动 Word 应用；macOS 专有）→ 缓存为 `<附件>.word.docx`
3. 两者均不可用时，回退至 `textutil` 压平文本的启发式行重建

LibreOffice 转换失败或超时（默认 300s，数十 MB 的多目录合刊附件可能超时）后自动回退至 Word。
实测两者解析行数一致；个别经 Word 转换后解析为 0 行的附件，LibreOffice 反而能够精确解析。

购置税目录第 1~30 批已全量覆盖（历史附件均有 `.word.docx` 缓存，不会再触发转换）。

`sync` 在 `articles` 表中记录每篇文章的 `status/error/row_count`：目录附件解析出 0 行标记为
`zero_rows`，异常标记为 `error`，两者均不计为已同步，下次 `sync` 默认重试。解析异常会先回滚
再标状态，避免半截 DELETE 清掉旧行；部分附件成功时写入成功附件并保留其余旧行。空嗅探先完整解析；`.docx` 精确转换失败时按 `zero_rows` 重试（仅 textutil html 不算已转出），docx 已转出但仍无目录标题的按非目录跳过。
`--force` 会重新下载并删除 `.html` / `.lo.docx` / `.word.docx` 后再解析。

附件文件与缓存目录按批次自动命名（原始 cms 哈希名与文章 ID 不具可读性）：目录形如
`downloads/jianmian/12168_公告第404批/`，文件形如 `购置税目录第28批.doc`、`车船税目录第83批.doc`、
`公告第404批附件.doc`、`推荐目录2022年第5批.doc`，派生缓存（`.html` / `.lo.docx` / `.word.docx`）同步改名。
各文章目录下的 `manifest.json` 维护 URL 名到本地名的映射（`--force` 仍按映射写回同一本地名）；
旧版纯 ID 目录在 sync 时自动迁移，存量可通过 `main.py gonggao jianmian rename` 一次性迁移
（同时更新库内 attachment 字段）。

> 2020~2022 年的《新能源汽车推广应用推荐车型目录》表头无「通用名称」列，不予入库。同步过程中的相应警告属预期行为。

### 公告参数表（review）

将 `fetch` 或 `gonggao query --download` 下载的公告参数页 PDF 转换为公告参数 Excel，
行结构对齐《公告参数评审模板》（行为参数项、列为各配置型号，附「备注」列标注真实来源）：

```bash
.venv/bin/python main.py review <车型>                          # 解析 downloads/announcement_site/<品牌>/<车型>/**/*.pdf
.venv/bin/python main.py review downloads/announcement_site/<品牌>/<车型>/某配置.pdf -o output/评审.xlsx
.venv/bin/python main.py review <车型> --no-catalog             # 不从减免税目录补充参数
```

- 整车参数页按词坐标解析，覆盖底部「发动机 / 其他 / 轴荷 / VIN / 底盘」多栏交错区
- 改装车参数页独立解析横向底盘引用表、VIN 和全宽「其他」说明，兼容「底盘 ID」表头被拆成多个词；缺少必要表头时报告解析错误。公告页未显示燃料种类时保留缺失，不从油耗或运输介质推断能源
- 公告页未显示的目录参数（通用名称、续驶里程、燃料消耗量、电池质量、储电量）按公告型号自动从
  `data/jianmian_catalog.sqlite` 补齐（购置税目录优先），并以浅黄底色标注
- 默认输出命名为 `output/公告参数_<车型>_<批次>.xlsx`，如 `公告参数_<车型>_第406批.xlsx`；
  PDF 跨多个批次时取区间（与 PDF 顺序无关），如 `公告参数_<车型>_第394-406批.xlsx`；同名文件存在时自动追加序号，不覆盖
- 「备注」列按参数实际来源填写，如「公告参数页」「减免车辆购置税目录」；未显示或未命中的字段明确标注为未解析或未命中

### 竞品对标报告（report）

抓取汽车之家车主口碑（评分、官方结构化亮点与槽点总结、满意及不满意原声）与懂车帝月销量榜，
生成自包含 HTML 对标分析报告（无外部依赖，可直接分发）：

```bash
.venv/bin/python main.py report <本品> --vs <竞品1> <竞品2>    # 综合对标（默认全部板块）
.venv/bin/python main.py report <本品> --vs <竞品> --focus sales    # 仅销量对标
.venv/bin/python main.py report <本品> --vs <竞品> --focus koubei   # 仅口碑对标
.venv/bin/python main.py report <本品>                          # 单车画像（无竞品）
.venv/bin/python main.py report <本品> --pages 10               # 增加口碑抓取页数（每页约 10 条）
.venv/bin/python main.py report <本品> --offline                # 使用 downloads/benchmark/ 缓存离线生成
```

**报告板块可按需裁剪**（`--focus`）：`all` 综合（默认）、`sales` 仅销量对标、`koubei` 仅口碑对标；
未指定 `--vs` 时生成单车画像报告。

报告包含：**综合研判**（由销量位次、口碑分差、优势与短板维度、价格定位合成要点与结论）、
核心指标卡片（综合评分、口碑数、指导价、具体月份销量、新车 PPH）、关键指标对比表、
**双源销量对比表**、口碑分维度评分对比条形图、各车亮点与槽点（官方结构化总结及「不满意」评价关键词统计）、
用户原声、本品改进建议（由槽点频次与相对竞品的维度分差两路生成）。
`--focus sales` 时省略口碑深度板块与改进建议，`--focus koubei` 时省略双源销量表。

**销量双源三维度**：同时抓取懂车帝（全市场榜）与汽车之家（细分级别榜），各按「最新月份 / 近半年 / 近12个月」
三个维度呈现销量与排名，表头标注具体月份范围（如「2025年12月~2026年05月」）。三个维度均由数据源原生
支持（懂车帝 `month=500/1000`、汽车之家 `date=YYYY-MM_YYYY-MM` 区间），非本地累加。汽车之家优先取所在
级别榜 Top20（口碑级别名自动映射，如「MPV」映射为「全部MPV」），未进入 Top20 时按口碑中的品牌 ID 走
品牌检索兜底；仍未上榜的维度留空，由两个来源互补覆盖。品牌检索兜底命中时，报告中的汽车之家排名标注为
「品牌筛选」内名次。原始响应缓存于 `downloads/benchmark/<车型>.json`，支持离线复现。请保持低频访问。

---

## 统一车型档案

默认档案位于 `data/vehicle_profiles.json`。该文件承载各车型的公告商标与型号前缀，属于使用方的
业务数据，**不纳入版本管理**；仓库中提供脱敏模板 [`data/vehicle_profiles.example.json`](data/vehicle_profiles.example.json)。
新环境按下列方式初始化后自行填写即可：

```bash
cp data/vehicle_profiles.example.json data/vehicle_profiles.json
```

档案缺失时 `load_mappings` 返回空列表，各命令仍可按输入名直接检索，不会中断。
每个条目同时描述两个来源的查询条件：

```json
{
  "name": "示例车型",
  "aliases": ["示例车型", "example model", "EX1"],
  "autohome": {
    "models": ["示例车型"]
  },
  "gonggao": {
    "trademark": "示例牌",
    "filters": { "clmc": "多用途乘用车" },
    "model_prefixes": ["ABC6520M"],
    "exclude_model_prefixes": ["ABC6520AP", "ABC6530AB", "ABC649"]
  }
}
```

- `autohome.models`：汽车之家搜索使用的车系或车型名。车系名导出全部在售配置，带年款的具体版本名仅匹配最相关的一个
- `gonggao.*`：工信部公告查询条件（商标、筛选项、型号前缀）

新增条目不必手写。`profiles add` 会按市场名走减免税目录反查，把公告商标、`clmc`、`model_prefixes` 直接写入档案：

```bash
.venv/bin/python main.py profiles add <市场名> --dry-run   # 先预览将写入的条目
.venv/bin/python main.py profiles add <市场名A> <市场名B>   # 确认后落盘
.venv/bin/python main.py profiles add -f names.txt         # 批量，按行读取，# 开头为注释
```

写入遵循「拿不准就留空」：公告返回多个商标、或各条记录 `clmc` 不一致时该字段留空并在终端点名；
`exclude_model_prefixes` 需要人判断哪些前缀属于同名异车，一律不猜。`autohome.models` 暂用市场名兜底，
汽车之家站内名称不同的车型需人工改写。已存在同名或同别名档案时跳过（`--overwrite` 覆盖），
且跳过前不会发起任何目录检索与公告请求。

- `fetch` 时未命中档案的车型：汽车之家按输入名直接搜索；工信部公告自动转为减免税目录反查并下载（目录库为空时提示先执行 `jianmian sync`）
- 旧版平铺格式（`trademark` 位于顶层、无 `gonggao` 子对象）保持兼容
- 一次性筛选优先使用 CLI 参数（`--model-prefix`、`--row-filter`、`--latest-batch`），不建议为临时需求修改档案

> 工信部公告查询主要覆盖新能源推广目录口径，普通油电混动（非插电）车型可能无法检索到。

---

## 输出产物

| 路径 | 内容 |
| --- | --- |
| `output/汽车之家_<车型>配置表_<日期>.xlsx` | 汽车之家配置对比表，工作表结构见下 |
| `output/公告参数_<车型>_<批次>.xlsx` | 公告参数表（关键参数 / 数据来源） |
| `output/对标报告_*.html` | 竞品对标分析报告（自包含） |
| `output/jianmian_catalog.xlsx` | 减免税目录单文件导出（不带 `--by-category` 时的输出；配 `--keyword` 会整体换成匹配子集） |
| `output/jianmian_by_category/全量.xlsx` | 减免税目录全量表（列结构与分类文件相同） |
| `output/jianmian_by_category/<类别>.xlsx` | 减免税目录分类导出（6 个固定 xlsx） |
| `downloads/announcement_site/<品牌>/<车型>/第<批次>批/*.pdf` | 工信部公告参数页 PDF；接口未返回 PDF 时保留同名 `.html` 供人工核查 |
| `downloads/benchmark/<车型>.json` | 口碑与销量原始数据缓存（`report --offline` 复用） |
| `downloads/announcement_site/_snapshots/query_*.json` / `manifest_*.json` | `gonggao query --download` 的查询快照与下载索引；索引含成功与失败两类条目（`status` 为 `ok`/`not_pdf`/`download_failed`）。网站批量采集用本项目 `gonggao collect`，统一登记 `data/announcement_site.sqlite`，保留独立的来源证据 |
| `downloads/announcement_site/_revisions/` | 公告网站动态页面变更前的 PDF 快照；`manifest.json` 记录原始哈希、当前哈希、PDF 生成时间与字段差异，不参与主库分类或普通查询 |
| `downloads/jianmian/<文章ID>_公告第N批/` | 减免税目录附件及派生缓存 |
| `data/announcement_site.sqlite` | 公告采集、文档、解析参数与正式公告跟踪的权威业务库 |
| `var/runs/` / `var/reports/` | 上游采集清单、进度、逐产品日志和报告 |
| `data/jianmian_catalog.sqlite` | 减免税目录车型库（本地数据，不纳入版本管理） |
| `logs/` | 汽车之家抓取运行日志 |

**汽车之家配置表的五个工作表**（说明 / 车型信息 / **配置分析** / 详细配置表 / 颜色明细）：

- 多车型合表时命名取首车加总数，如 `汽车之家_<车型>等3个车型配置表_20260728.xlsx`；同日重复导出自动追加 `_2`、`_3` 序号，不覆盖既有文件
- 「配置分析」按**动力总成分组**（能源×驱动×电池电量×座位数，如「增程四驱60度电池6座」「纯电两驱100度电池7座」，
  取自能源类型、驱动方式、电池能量、座位数参数）组织，含三部分：**车型配置强度**（按分组排列，标配、选配、独有配置计数）、
  **整合参数明细**（保留全量参数，每个分组一列，组内多值在单元格内合并归纳——数值型如 `2995/3200`，
  配置型标注「区分高低配」；某组独有配置标绿，组间差异标黄）、**车型独有配置**
- 「详细配置表」保留全部车型列（无底色，非空单元格带表格线），用于查看单车明细时筛选

> 除非明确需要，不建议删除既有 `output/` 与 `downloads/` 内容——两者是离线复现与增量缓存的基础。

---

## 项目结构

```text
.
├── main.py                    # 统一 CLI：fetch / autohome / gonggao / review / report / profiles
├── data/
│   └── vehicle_profiles.json  # 统一车型档案
├── autohome_cc/               # 汽车之家配置抓取
│   ├── app.py                 # AutohomeScraperApp 主流程
│   ├── config.py
│   ├── crawler/               # http_client / browser_client / search（搜索接口收敛于此）
│   ├── parser/                # compare_parser / spec_parser
│   ├── exporter/              # excel_writer
│   └── utils/                 # cleaners（含 build_export_filename）/ logger
├── miit_gonggao/
│   ├── core.py                # 公告查询、参数页 PDF 下载、统一节流与重试
│   ├── jianmian.py            # 减免购置税/车船税目录抓取、解析、SQLite 与 Excel
│   └── review_export.py       # 公告参数页 PDF 转公告参数 Excel
├── benchmark/                 # 竞品对标
│   ├── collector.py           # 口碑与销量采集
│   ├── analyzer.py            # 槽点亮点分析、改进建议
│   ├── report.py              # HTML 报告渲染
│   └── cli.py
├── tests/                     # pytest：解析边界、目录库状态、Excel 导出（fixtures 离线可复现）
│   └── fixtures/
├── scripts/
│   ├── bootstrap.sh
│   └── smoke_test.py
├── .github/workflows/ci.yml   # CI：ruff 与 pytest（ubuntu，离线单测）
├── pyproject.toml             # ruff / pytest 配置
└── requirements.txt           # 另有 requirements-browser.txt / requirements-dev.txt
```

---

## 登录受限时

Autohome 返回权限或验证页时，有三种处理方式：

1. `--browser-login`：调起浏览器扫码登录（需先执行 `INSTALL_PLAYWRIGHT=1 ./scripts/bootstrap.sh` 安装 Chromium），登录态保存至 `.browser/autohome`
2. `--cookie-file ./cookies.txt`：导入浏览器导出的 cookies（支持 header 与 Netscape 两种格式；Netscape 的 `#HttpOnly_` 行会按 cookie 解析）
3. `--browser-fallback`：requests 请求失败时由浏览器渲染页面兜底

工信部公告接口无需登录，但仍应保持低频串行访问。

---

## 开发与测试

抓取与网络访问遵循低频、串行、温和的原则。工信部与 EIDC 的全部 HTTP 请求统一经 `core.http_request`
（内置 0.8~1.8s 节流，对 5xx、网络错误与响应截断执行退避重试，4xx 直接抛出），新增请求不应绕开该入口。
优先使用 `requests` 与 `BeautifulSoup`，确认必要时才引入 Playwright。

代码改动后至少执行：

```bash
.venv/bin/python -m pytest              # 解析边界、目录库状态与 Excel 导出（离线 fixtures）
.venv/bin/python -m ruff check .
.venv/bin/python scripts/smoke_test.py  # 离线 Excel 链路；存在附件缓存时附带深度解析校验
.venv/bin/python main.py profiles
```

另需执行一次轻量在线 smoke（不带 `--download`，除非下载行为有改动）：

```bash
.venv/bin/python main.py gonggao query "<车型>" --latest-batch --output-dir /tmp/hub-smoke
```

行为发生变化时，同步更新 `README.md` 与 [AGENTS.md](AGENTS.md)。

---

## 参考文档

- [AGENTS.md](AGENTS.md)：Agent 执行流程、抓取与输出规则、开发约定
- [data/vehicle_profiles.example.json](data/vehicle_profiles.example.json)：统一车型档案的脱敏模板（真实档案为本地数据，不纳入版本管理）
- `miit_gonggao/jianmian.py` 模块注释：目录附件 .doc 转 .docx 的转换与表格解析细则
- `autohome_cc/crawler/search.py`：汽车之家搜索接口的地址、参数与响应解析（接口变化仅需修改此处）
- [.github/workflows/ci.yml](.github/workflows/ci.yml)：CI 配置

---

## 许可

本项目以 [MIT License](LICENSE) 发布。

许可覆盖的是本仓库的代码，**不包括**通过本工具抓取到的任何数据。汽车之家、工信部装备工业发展中心、
懂车帝等来源的数据，其权利归属与使用条件由各来源方规定。使用者需自行遵守目标站点的服务条款与
`robots.txt`，控制访问频率，并对由此产生的合规责任负责。本工具按「原样」提供，不附带任何担保。

<div align="center">

MIT License © [travisoa](https://github.com/travisoa/)

</div>
