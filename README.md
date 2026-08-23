<div align="center">

# Vehicle Data Hub

> 车型数据一站式采集：汽车之家配置对比、工信部公告参数页、减免税目录反查、口碑销量竞品对标。

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
downloads/&lt;车型A&gt;/406_&lt;车辆型号&gt;_*.pdf
downloads/manifest_20260823_*.json

全部任务完成。</code></pre>

</td>
<td width="50%" valign="top">

<pre><code>$ main.py review &lt;车型&gt;

解析 downloads/&lt;车型&gt;/*.pdf … 12 份
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

对应命令：`main.py gonggao query ...`、`main.py gonggao jianmian ...` 与 `main.py fetch --source gonggao`

<table>
<colgroup><col width="1%"><col></colgroup>
<thead>
<tr><th align="left" nowrap width="1%">能力</th><th align="left">说明</th></tr>
</thead>
<tbody>
<tr><td nowrap width="1%"><strong>公告查询</strong></td><td>按商标、企业、车辆型号、名称与批次组合筛选；支持型号前缀的<strong>包含与排除</strong>（<code>--model-prefix</code> / <code>--exclude-model-prefix</code>）及任意字段后置筛选（<code>--row-filter</code>）</td></tr>
<tr><td nowrap width="1%"><strong>参数页下载</strong></td><td><code>--download</code> 下载参数页 PDF 至 <code>downloads/&lt;车型&gt;/</code>；按 <code>%PDF</code> 头校验，非 PDF 时保留 <code>.html</code> 供人工核查；单条失败仅跳过并在结尾汇总，<strong>不中断整批</strong></td></tr>
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
<tr><td nowrap width="1%"><strong>坐标级解析</strong></td><td>公告参数页为单页固定版式，按<strong>词坐标</strong>精确解析，覆盖底部「发动机 / 其他 / 轴荷 / VIN / 底盘」多栏交错区</td></tr>
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
| 公告参数评审表 | **review** | `review <车型>`（需 `downloads/<车型>/` 已有 PDF） |
| 竞品对标 / 口碑 / 销量 | **report** | `report <本品> --vs <竞品1> <竞品2>` |
| 仅知市场名，无法检索公告 | **jianmian** | `gonggao jianmian search <市场名> --download` |
| 已知商标或型号，临时查询 | **gonggao query** | `gonggao query --trademark "<商标>" --model-prefix <型号前缀> --latest-batch` |

---

## 快速开始

### 运行环境

Python 3.11+。**大部分功能为纯 Python 实现，macOS 与 Linux 均可运行**；仅减免税目录的附件同步链路
依赖 macOS 自带的 `textutil`。具体边界如下：

| 功能 | macOS | Linux / Windows |
| --- | :---: | --- |
| `fetch` / `autohome` / `gonggao query` / `review` / `report` / `profiles` | ✅ | ✅ 纯 Python，无平台依赖 |
| `gonggao jianmian search` / `export` | ✅ | ✅ 读取既有 SQLite，无平台依赖 |
| `gonggao jianmian sync` / `rename` | ✅ | ⚠️ 需要 `textutil` 嗅探附件类型，缺失时会中止 |

因此在 Linux 上部署时，随包分发一份已同步好的 `data/jianmian_catalog.sqlite`，即可完整使用目录反查
与 `review` 的目录补参数能力；目录的增量同步仍需在 macOS 侧执行。

`.doc` 转 `.docx` 的转换器本身跨平台：`find_soffice()` 优先通过 `shutil.which("soffice")` 定位
LibreOffice，Linux 发行版安装后即在 `PATH` 中；Microsoft Word 的 AppleScript 通道为 macOS 专有。
CI 在 ubuntu 上执行离线单测，验证的即是这部分平台无关逻辑。

### 安装

```bash
./scripts/bootstrap.sh
```

依赖分三层，按需安装：

| 依赖层 | 内容 | 适用场景 |
| --- | --- | --- |
| `requirements.txt` | requests / bs4 / openpyxl / pdfplumber 等 | 默认安装 |
| `requirements-browser.txt` | Playwright（含 Chromium） | 仅 `--browser-login` / `--browser-fallback` 需要，通过 `INSTALL_PLAYWRIGHT=1 ./scripts/bootstrap.sh` 安装 |
| `requirements-dev.txt` | pytest / ruff | 开发与测试，通过 `INSTALL_DEV=1 ./scripts/bootstrap.sh` 安装 |

可选：安装 LibreOffice（macOS 为 `brew install --cask libreoffice`，Linux 使用发行版包管理器）以启用
无窗口的目录附件转换；未安装时回退至 Microsoft Word 通道或启发式解析。

### 验证安装

```bash
.venv/bin/python -m pytest             # 解析边界与导出链路（离线，依赖 tests/fixtures）
.venv/bin/python scripts/smoke_test.py # 离线 Excel 链路；附件缓存存在时附带深度解析校验
.venv/bin/python -m ruff check .       # lint
.venv/bin/python main.py profiles      # 列出统一车型档案
```

### 典型用法

```bash
# 1. 两个来源同时抓取：配置 Excel 输出至 output/，公告 PDF 输出至 downloads/<车型>/
.venv/bin/python main.py fetch <车型A> <车型B>

# 2. 公告 PDF 转公告参数 Excel（自动补充减免税目录参数）
.venv/bin/python main.py review <车型>

# 3. 生成口碑与销量竞品对标 HTML 报告
.venv/bin/python main.py report <本品> --vs <竞品1> <竞品2>
```

`fetch` 的工信部查询默认仅保留**最新公告批次**（等价于 `gonggao query --latest-batch`），
使用 `--all-batches` 保留全部批次。未命中档案的车型，工信部侧自动转为按市场名走减免税目录反查，
成功后提示将建议的 `model_prefixes` 沉淀至档案。输出目录可按来源分别指定：
`--autohome-output-dir`（Excel，默认 `output/`）与 `--gonggao-output-dir`（PDF，默认 `downloads/`）。

---

## 命令速查

`main.py` 提供六个顶层命令：

| 命令 | 说明 |
| --- | --- |
| `fetch <车型>...` | 按统一车型档案同时抓取两个来源 |
| `autohome ...` | 汽车之家配置抓取完整 CLI（`--models`、`--capture-file`、`--cookies`、`--browser-login` 等） |
| `gonggao ...` | 工信部公告查询完整 CLI（含 `query`、`profiles`、`jianmian` 子命令） |
| `review <车型/PDF>...` | 公告参数页 PDF 转公告参数 Excel（[详解](#公告参数表review)） |
| `report <车型> --vs ...` | 口碑与销量竞品对标 HTML 报告（[详解](#竞品对标报告report)） |
| `profiles` | 列出统一车型档案 |

两个来源的全部参数均在各自子命令下可用：

```bash
.venv/bin/python main.py gonggao query --trademark "<商标>" --model-prefix <型号前缀> --latest-batch --download
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

**`gonggao jianmian`**：`sync`（`--max-pages` / `--max-articles` / `--force` 重新解析）、
`search <关键词>`（`--resolve` 经公告接口反查 / `--download` 反查后下载，隐含 `--resolve`）、
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
.venv/bin/python main.py gonggao jianmian export --by-category      # 按车辆类别分 6 个文件导出至 output/jianmian_by_category/
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
`zero_rows`，异常标记为 `error`，两者均不计为已同步，下次 `sync` 默认重试。解析异常，
以及已入库文章重试时解析出 0 行，均不会清除该文章已入库的历史行。

附件文件与缓存目录按批次自动命名（原始 cms 哈希名与文章 ID 不具可读性）：目录形如
`downloads/jianmian/12168_公告第404批/`，文件形如 `购置税目录第28批.doc`、`车船税目录第83批.doc`、
`公告第404批附件.doc`、`推荐目录2022年第5批.doc`，派生缓存（`.html` / `.lo.docx` / `.word.docx`）同步改名。
各文章目录下的 `manifest.json` 维护 URL 名到本地名的映射，确保 `--force` 时不会重复下载；
旧版纯 ID 目录在 sync 时自动迁移，存量可通过 `main.py gonggao jianmian rename` 一次性迁移
（同时更新库内 attachment 字段）。

> 2020~2022 年的《新能源汽车推广应用推荐车型目录》表头无「通用名称」列，不予入库。同步过程中的相应警告属预期行为。

### 公告参数表（review）

将 `fetch` 或 `gonggao query --download` 下载的公告参数页 PDF 转换为公告参数 Excel，
行结构对齐《公告参数评审模板》（行为参数项、列为各配置型号，附「备注」列标注真实来源）：

```bash
.venv/bin/python main.py review <车型>                          # 解析 downloads/<车型>/*.pdf
.venv/bin/python main.py review downloads/<车型>/某配置.pdf -o output/评审.xlsx
.venv/bin/python main.py review <车型> --no-catalog             # 不从减免税目录补充参数
```

- 公告参数页为单页固定版式，按词坐标精确解析，覆盖底部「发动机 / 其他 / 轴荷 / VIN / 底盘」多栏交错区
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
| `output/jianmian_catalog.xlsx` | 减免税目录单文件导出 |
| `output/jianmian_by_category/` | 减免税目录分类导出（6 个固定 xlsx） |
| `downloads/<车型>/*.pdf` | 工信部公告参数页 PDF；接口未返回 PDF 时保留同名 `.html` 供人工核查 |
| `downloads/benchmark/<车型>.json` | 口碑与销量原始数据缓存（`report --offline` 复用） |
| `downloads/query_*.json` / `manifest_*.json` | 公告查询快照与下载索引 |
| `downloads/jianmian/<文章ID>_公告第N批/` | 减免税目录附件及派生缓存 |
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
2. `--cookie-file ./cookies.txt`：导入浏览器导出的 cookies（支持 header 与 Netscape 两种格式）
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
- [data/vehicle_profiles.json](data/vehicle_profiles.json)：统一车型档案（含现有车型样例）
- `miit_gonggao/jianmian.py` 模块注释：目录附件 .doc 转 .docx 的转换与表格解析细则
- `autohome_cc/crawler/search.py`：汽车之家搜索接口的地址、参数与响应解析（接口变化仅需修改此处）
- [.github/workflows/ci.yml](.github/workflows/ci.yml)：CI 配置
