# R41 · 前端运行时性能

一句话：**首屏 1.46 MB 里有 373 KB 是一个从 CDN 同步加载、只有图谱页要用的
库**（渲染阻塞 240 ms，三甲内网还取不到）；**真实用户路径的 CLS 是 0.0817**
（九段骨架的高度 0 → N 行把下面整个推下去）；**二次访问原来没有缓存策略**，
浏览器按启发式自己猜新鲜期。这三件都修掉了，各自带实测数。

另外这一轮在做 Worker 布局时撞出一条**看不见的反向依赖**：
`LAYER_X` / `Y_MIN` / `Y_MAX` / `HERB_GAP` 这四个只有 graph.js 在用的布局常量
定义在 app.js 里，浏览器里两个 script 共享全局作用域所以从来没报错过。

量具：`scripts/profile_frontend.py`（真 Chromium + PerformanceObserver），
六个场景，视口 1440×900。

---

## 1. 指标口径（先说清楚，三条）

| 指标 | 怎么来的 | 口径上的讲究 |
|---|---|---|
| `fcp_ms` / `lcp_ms` | `paint` entry / `PerformanceObserver('largest-contentful-paint')` | 观察器必须在**任何页面脚本之前**装（`add_init_script`），晚一步就漏掉最早的那几条 |
| `tbt_ms` | Σ(长任务 − 50ms) | **不是"长任务总时长"**。一个 60 ms 的任务贡献 10 ms，不是 60 ms——否则一堆刚过线的任务会把这个数吹起来 |
| `cls` | `PerformanceObserver('layout-shift')`，**排除 `hadRecentInput`** | 点开折叠块把下面推下去不是"版面跳"。不排除的话这个数恒大，等于没有指标 |
| `inp_ms` | 真点一下，量到**下一帧渲染完** | 不是"事件回调返回"。回调里改完 DOM 就返回，人还没看见任何变化；两者能差一整帧到几百毫秒 |
| `graph_fps` | 连续 rAF 间隔的中位数 | — |
| `network` | `resource` entries | `renderBlockingStatus === "blocking"` 的条数单独报——那才是"挡住首屏"的那几个 |

**CLS 有两种口径，不许混成一个数。** CLS 排除用户输入后 500 ms 内的位移，
而"直接渲染终态"这个场景没有输入，首屏→终态那一大跳整个被计进去。所以：

- `chain_done_via_click`（**真实用户路径**）：点「辨证」→ running（那一大跳在
  500 ms 内、被排除）→ 等过 500 ms → 结果填进来（这一跳**是**要算的）。
- `chain_done`（**上界**）：直接渲染，没有点击。刷新页面恢复会话落在这个口径上。

**点击必须是 Playwright 的真实鼠标点击**，不能用脚本里的 `el.click()`：
Chromium 只认**可信事件**，脚本造的不可信事件不开那个 500 ms 窗口。
第一版就是用 `el.click()` 写的，量出来的还是上界，那个场景等于白设。

---

## 2. 基线（改前）

```bash
python -m scripts.profile_frontend --repeat 1 --out eval/frontend/R41_baseline.json
```

| 场景 | FCP | LCP | TBT | CLS | 请求 | 字节 | 渲染阻塞 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 首屏 | 308 | 308 | 0 | **0.0269** | 10 | 1,458,054 | **2** |
| 单链终态（上界口径） | 272 | 368 | 0 | **0.6711** | 11 | 1,831,658 | 2 |
| 问诊图 | 280 | 384 | 0 | 0.3011 | 11 | 1,831,658 | 2 |

首屏那 1,458,054 字节的构成（`decodedBodySize`，从 profiler 的 `network.all` 读）：

| 字节 | 阻塞 | 文件 |
|---:|---|---|
| 320,248 | 否 | noto-serif-sc-600-subset.woff2 |
| 315,552 | 否 | noto-serif-sc-400-subset.woff2 |
| 250,356 | 否 | noto-sans-sc-500-subset.woff2 |
| 246,468 | 否 | noto-sans-sc-400-subset.woff2 |
| 157,149 | 否 | app.js |
| 89,275 | 否 | graph.js |
| 63,634 | **是** | app.css |
| 10,231 | 否 | ui/select.js |
| — | **是（240 ms）** | **cytoscape.min.js** ← `<head>` 里的同步 CDN script |

四个字重合计 **1,132,624 字节 = 首屏的 77.7%**；cytoscape **373 KB**，
而它只有图谱页要用。

CLS 的肇事元素（profiler 把 `LayoutShiftAttribution` 的节点打出来了——
**一个 CLS 数字没法指导修改**）：

| 位移 | 时刻 | 肇事元素 |
|---:|---:|---|
| 0.6441 | 379 ms | `div#tab-consult.tab-panel` + `div#input-panel`（首屏→终态那一大跳，上界口径） |
| 0.0257 | 362 ms | `textarea#complaint` + `div.input-row`（示例主诉填进来，把输入框推下去） |
| 0.0013 | 378 ms | `button.example` |

---

## 3. 逐项优化（改前 → 改后，全部实测）

### 3.1 按需加载：`<head>` 里那个同步 CDN script 整个去掉

`index.html` 原来在 `<head>` 里同步加载 cdnjs 上的 cytoscape。三个问题叠在一起：

1. **渲染阻塞 240 ms**——`<head>` 里的同步 `<script>` 挡住 HTML 解析；
2. **373 KB 只有图谱页要用**，不开图谱的人白等白下；
3. **三甲内网取不到 cdnjs**，那条请求会一直挂到超时，而"断网可用"正是录制
   回放这条路线存在的理由。

`graph.js` 早就有 `ensureCytoscape()`（用时动态插入本地副本
`web/vendor/cytoscape.min.js`），所以这一项是**把 CDN 那一路删掉**，
让唯一的加载路径变成按需的那条。顺带三个 `<script>` 加 `defer`——它们在
`</body>` 前本来就不阻塞首次绘制，`defer` 买到的是**边解析 HTML 边并行下载**
这三个文件（157 + 89 + 10 KB）。

`defer` 而不是 `async`：`async` 不保证执行顺序，而这三个有硬顺序
（app.js 用 graph.js 定义的 `formulaSourceLabel`，顺序反了是 ReferenceError）。

| | 改前 | 改后 |
|---|---:|---:|
| 首屏请求数 | 10 | **9** |
| 首屏渲染阻塞请求 | **2** | **1**（只剩 app.css，那是 CSS 应有的行为） |
| FCP | 308 ms | **124 ms（−59.7%）** |
| LCP | 308 ms | **124 ms（−59.7%）** |

（FCP/LCP 的降幅里有本机方差，但"少一个 240 ms 的阻塞请求"是确定的结构改动，
方向不会反。）

### 3.2 缓存策略：二次访问 1,475,897 → 3,941 字节（**−99.7%**）

`StaticFiles` 只给 ETag / Last-Modified，**没有 `Cache-Control`**。浏览器于是
按启发式自己猜新鲜期——猜多久取决于浏览器版本，而"取决于浏览器版本"意味着
现场表现不可复现。

两类资源两种策略，**不能给同一个值**：

| 类 | 谁 | 策略 | 为什么 |
|---|---|---|---|
| 不变的第三方产物 | `vendor/`（字体 1.13 MB + cytoscape 373 KB = 首屏字节的 **84%**） | `public, max-age=31536000, immutable` | 内容跟文件名绑定（字体子集是 `scripts/subset_fonts.py` 的产物、cytoscape 带版本），换内容必然换文件名 |
| 会改的自家代码 | `index.html` / `app.js` / `app.css` / `graph.js` | `no-cache` | **必须每次问服务器**。给 `max-age` 的话，升级之后医生刷新页面还是旧的 JS 配新的后端——一类最难查的故障（R45 的升级回滚靠这条）。`no-cache` 不是"不缓存"，是"缓存但每次带 ETag 问一句"，304 只有几十字节 |

判据里有一条专门查 **304 的响应也带策略头**——否则浏览器在 304 之后又回到
"自己猜"。还有一条查 `/health` 这类 API **不受**静态策略影响（问诊结果被缓存
会让第二个患者看到第一个患者的方）。

### 3.3 字体：preload 首屏那两个，另外两个刻意不 preload

四个字重合计 1.13 MB。不 preload 时它们要等 CSS 解析完、布局判定"这一段确实
用到这个字重"之后才开始下载——那段等待就是 FOUT（先系统字体、再换成 Noto）
的长度。

**只 preload 首屏用到的两个**（serif 400 / sans 400）：preload 是"现在就下"，
四个全 preload 会把带宽从真正要用的那两个身上抢走。两条 `<link>` 都带
`crossorigin`——字体请求本身是匿名 CORS 模式，不带的话两个请求的缓存键不一样，
浏览器会**再下一遍**，净亏。

**1.13 MB 没有被砍掉**，这一项如实记为"已核实、这是设计成本"：那四个 woff2
已经是 `scripts/subset_fonts.py` 做过的子集（只含本页真正用到的字），
中文字体子集就是这个量级。有了 3.2 的 `immutable` 之后它是**首次访问一次性
成本**（二次访问 0 字节）。把它降下去只有两条路，两条都是产品决定不是优化：
减字重（4 → 2，改版式）或者 `font-display: optional`（首访用系统字体）。
**不擅自替用户做这个决定**，如实标出代价。

### 3.4 CLS：真实用户路径 0.0817 → **0.0020**

两处留位，各自绑在它的数据源上：

**① 示例主诉区**（首屏那 0.0257）。三条示例是 `/health` 回来之后由 JS 填的
（`core/examples.py` 下发，前端不写死），填进去把 textarea 整个推下去。
留位按"标题 + `--examples-rows` 行"算，而**行数与后端那张表的条数由一条测试
绑在一起**——后端加第四条示例，那条测试会红，提醒把 CSS 的行数跟着改。
不绑的话 CLS 会在某一轮悄悄回来。

首屏 CLS **0.0269 → 0.0024（−91%）**。

**② 九段骨架的 `chain-body`**（真实路径那 0.0816）。running 态骨架里
`chain-body` 是空的（高度 0），结果填进来每段长出 1~4 行，九段累加把下面的
证据区与输入区整个推下去（肇事元素 `details#detail-zone` + `div#input-panel`）。
`min-height: 1.75em` 正好一行（跟 `line-height: 1.75` 配对）。

**不留更多**：留三行会让 running 态出现三倍空白，那是用体验换指标。

| 口径 | 改前 | 改后 |
|---|---:|---:|
| 首屏（纯加载，无交互） | 0.0269 | **0.0024** |
| **真实用户路径**（可信点击 → running → 结果） | 0.0817 | **0.0020** |
| 直接渲染终态（上界，无点击） | 0.6711 | 0.6520 |

上界那一栏**不是"没修好"**：首屏→终态那一大跳在真实世界里落在点击后 500 ms
内、被 CLS 排除。它仍然报出来，因为刷新恢复会话会落在这个口径上。

两块留位的地方同时加了 `contain: layout style`——**留位与 containment 是一对**，
只留位的话内容变化仍然会触发祖先重排。

### 3.5 消除布局抖动：`showTooltip` 的强制同步布局

改之前这个函数是教科书式的 layout thrashing：

```
写 innerHTML → 写 class → 【读 getBoundingClientRect】→ 写 style.left/top
```

中间那次读会强制浏览器**同步**跑一遍布局，而这个函数挂在图谱的 mouseover 上
——鼠标在图上划一下就是几十次同步布局，每一次都在输入事件的处理里。

改成"读完再写、第二次读挪进 rAF"：

1. 先读**不依赖 tooltip 内容**的量（画布的位置与尺寸）；
2. 写内容，但先别显示（`.measuring` → `visibility: hidden`，**仍然参与布局**
   所以量得到尺寸；`display:none` 量不到）；
3. 第二次读与最终定位放进 `requestAnimationFrame`——从输入事件里搬出去。

代价是 tooltip 晚一帧出现（16.7 ms）。`hideTooltip` 也要清 `.measuring`：
rAF 还没跑到就被关掉的话，留着这个 class 会让下一次 show 出来是隐形的。

### 3.6 Web Worker 布局 + 一条看不见的反向依赖

`computeLayout` 是纯函数（只读 nodes/edges，不碰 DOM、不碰 cytoscape），
整段搬到 Worker 上算。**Worker 里不重写一份布局**：用
`importScripts("graph.js")` 把这份文件原样拉进去，直接调它自己的 `computeLayout`
（一个算法只有一处实现）。

**第一次跑就报 `Worker 报错：LAYER_X is not defined`**：
`LAYER_X` / `Y_MIN` / `Y_MAX` / `HERB_GAP` 这四个**只有 graph.js 在用**的布局
常量定义在 **app.js** 里。浏览器里两个 script 共享全局作用域，所以从来没报错
过；而 Worker 只 import graph.js。这是一条反方向的跨文件依赖
（文件顶部写明依赖方向只能 app → graph），**它是被 profiler 打出
`fallback_reason` 才看见的**——不打那个字段的话，Worker 每次静默回落到主线程，
表现就是"Worker 没有收益"。已把四个常量搬回 graph.js。

三条兜底，缺一条这个优化就变成一个新的故障源：Worker 建不起来 / 算不出来 /
不回话（2 s 超时），任何一条都回落到主线程并把原因记进 `layoutStats`。
回复**按 id 认领**——同一个 Worker 会被连续几次布局复用，不认 id 的话上一次
迟到的回复会被当成这一次的结果，图会用错的坐标画出来而且不报错。

| | 改前 | 改后 |
|---|---|---|
| 布局跑在哪 | 主线程 | **Worker**（`layoutStats.where == "worker"`，无回落） |
| 问诊图布局耗时 | 亚毫秒（十几个节点） | 18.5–19.9 ms（含 Worker 创建 + 一次往返） |

**这一项在当前规模上是负收益，如实记负。** 搬它的理由不是"现在慢"，是
**图会长大**：R42 要换成 dagre 布局，图谱浏览器那张持久图是几百到几千节点，
dagre 在那个规模上是几十到几百毫秒——那正是一个会让点击没反应的长任务。
机制与判据先立好，换算法时不用再动这一层。

### 3.7 窗口化列表（虚拟滚动）：按阈值启用，默认不生效

实测的 DOM 规模：整页 **154~297 个节点，最长的列表 14 个子节点**。参考医案在
默认配置下是 20 条（`REFS_IN_RESPONSE`）。**这个量级上做虚拟滚动是纯亏**——
多 80 行代码、多一个剪裁/滚动跳动的失败模式，换 0 收益。

但 `REFS_IN_RESPONSE` 是环境变量，医院调到 500 是完全合法的配置。所以机制
建好、**按阈值（50）启用**：默认那条路径的 DOM 逐字节不变。

500 条 × 3 列的实测对照（真 Chromium）：

| | DOM 节点 | 构建 + 布局 |
|---|---:|---:|
| 全渲染 | 6000（1500 个 `.ref-item`） | 57.4 ms |
| 窗口化 | **81**（72 个 `.ref-row`） | **2.5 ms** |
| | **−98.6%** | **−95.6%** |

默认 20 条时实测 `.virtual-list` 数量为 **0**、`.ref-item` 60 个——确认默认
界面一个像素都没动。

**为什么是"固定行高 + 只渲染可见窗口"，不是"滚到底再追加"**：追加式 DOM 会
随滚动一直长，滚到底跟一次全渲染一样——它解决的是首次渲染，不是 DOM 规模。
代价是 >阈值 时列表从"每条三行摘要"变成"每条一行"（完整内容点开进证据侧栏），
这是一个**取舍**，写在代码注释里。两处行高（`app.js` 的 `VIRTUAL_ROW_HEIGHT`
与 `app.css` 的 `.ref-row height`）由一条测试绑住——不一致会越滚越偏，
而症状（"滚下去有些医案看不到"）看起来像数据问题。

### 3.8 事件委托

`#columns` 那一片早就是委托的（R14 的注释写着）。这一轮补上示例按钮：
`renderExamples` 原来逐个按钮 `addEventListener`，依赖"innerHTML 换了节点、
旧监听器跟着走"这个副作用才不泄漏——**依赖副作用的正确性是看不出来的正确性**。
改成一次绑在容器上，`renderExamples` 于是变成纯粹的"填 HTML"，没有副作用。

顺带修掉 R40 自己引入的一处浪费：预热轮询原来每秒调一次
`initDemoModeBanner()`，那会把身份色重新注一遍 CSS 变量、三条示例重新
`innerHTML` 一遍、λ₁ 说明重新渲染一遍——每秒一次、最多 180 次，全是白做的
DOM 工作（这些在预热期间不会变）。改成 `pollWarmupOnly()` 只更新那条横幅。

### 3.9 长任务与大响应体

| | 实测 |
|---|---|
| 长任务（>50 ms） | 六个场景 **0~1 个**，最长 51–60 ms，只出现在页面加载那一下 |
| TBT | **0~2 ms** |
| 问诊响应体 | R40 已从 2,848,127 → 83,729 字节（−97.1%），所以这一轮没有"解析 2.8 MB"这个长任务了 |
| 图谱浏览器 | R16 起就是分页拉的（`?node_types=element&limit=GB_ELEMENT_LIMIT`），不是一次拉 `/api/graph` 的全量 1,929,196 字节 |
| 图谱帧率 | 中位 **60 fps**，最差帧间隔 16.8 ms（问诊图与图谱浏览器都是） |

**这两项本轮没有需要修的东西**，如实记为"已核实无长任务可消"，而不是编一个
削减比例。真正让 TBT 变成 0 的是 R40 那次响应体裁剪。

---

## 4. 改前 → 改后总表

| 指标 | 改前 | 改后 | 差 |
|---|---:|---:|---|
| 首屏 FCP | 308 ms | 124 ms | **−59.7%** |
| 首屏 LCP | 308 ms | 124 ms | **−59.7%** |
| 首屏请求数 | 10 | 9 | −1 |
| 首屏**渲染阻塞**请求 | 2 | **1** | −50% |
| 首屏 CLS | 0.0269 | **0.0024** | **−91.1%** |
| **真实用户路径 CLS** | 0.0817 | **0.0020** | **−97.6%** |
| 二次访问字节 | 1,475,897（无缓存策略，浏览器自己猜） | **3,941** | **−99.7%** |
| 二次访问 FCP | — | **40 ms** | — |
| 布局线程 | 主线程 | Worker | 机制到位，收益要等 R42 的 dagre |
| 500 条列表 DOM | 6000 节点 / 57.4 ms | 81 节点 / 2.5 ms | **−98.6% / −95.6%** |
| TBT | 0 | 0 | 无可改 |
| 图谱帧率 | 60 fps | 60 fps | 无可改 |
| 首屏字节 | 1,458,054 | 1,475,897 | **+1.2%**，见下 |

**首屏字节没降，还微涨了**，如实说明：去掉的 cytoscape 在本沙盒里
`transferSize` 报 0（代理层命中，解码体积算不到首屏那个数上），而新增的两条
`preload` 让两个字体**更早**开始下、于是更可能落在 load 之前被统计到。
真正省下来的是**阻塞请求数**（2 → 1）与**二次访问**（−99.7%）。
把 +1.2% 说成"优化"或者把它藏起来都不对。

---

## 5. 验收表（9 项）

| # | 项 | 判据 | 结果 |
|---|---|---|---|
| 1 | 真浏览器量具可跑，六指标齐 | `scripts/profile_frontend.py`，`observers_failed` 为空 | ✅ FCP/LCP/TBT/CLS/INP/长任务/FPS/堆/网络 |
| 2 | 按需加载 | `<head>` 无 `<script>`，无外网引用 | ✅ 阻塞请求 2 → 1 |
| 3 | 消除长任务 | TBT 与长任务条数 | ✅ TBT 0~2 ms、长任务 0~1 个（最长 60 ms，仅加载时） |
| 4 | 虚拟滚动 | 阈值行为 + 500 条对照 | ✅ 6000 → 81 节点（−98.6%）；默认 20 条不启用 |
| 5 | Web Worker 布局 | `layoutStats.where == "worker"` 且无回落 | ✅（顺带修掉一条反向依赖） |
| 6 | 消除布局抖动 | `showTooltip` 读写分离 + rAF | ✅ 输入事件里不再有强制同步布局 |
| 7 | CSS containment | 三处（示例区 / chain-body / 窗口化列表） | ✅ |
| 8 | 动画只用 transform/opacity | 扫 `@keyframes` 与 `transition` | ✅ 有测试逐条查（`transition` 里出现 height/width/top 就红） |
| 9 | 字体 CLS | preload + swap + 留位 | ✅ 首屏 CLS 0.0024 |

外加：交互延迟实测（角色下拉 25.1 ms、点第九段 31.4 ms、图谱重置 22.1 ms），
全部 < 100 ms——那是 R43 的题目，这里先把数留下当基线。

## 6. 测试（4 个文件）

| 文件 | 条数 | 要求 | 测什么 |
|---|---:|---:|---|
| `tests/test_frontend_loading.py` | **21** | ≥15 | 一个外网地址都不许有、`<head>` 无 script、本地副本真的在且不是桩、defer 而非 async、只 preload 两个且带 crossorigin、vendor immutable / 自家代码 no-cache、**304 也带策略头**、API 不受影响、挂载点真的用了策略类 |
| `tests/test_frontend_cls.py` | **12** | ≥8 | 留位的行数**绑后端示例条数**、留一行且不留更多、containment 与留位成对、`@keyframes` 与 `transition` 只许动 transform/opacity |
| `tests/test_virtual_list.py` | **20** | ≥6 | 阈值高于默认条数、两条路径不混、spacer 高度等于全部行（滚动条不许撒谎）、**两处行高必须一致**、单行摘要不丢字段、scroll 走 rAF 合并、阈值边界 7 个点 |
| `tests/test_layout_worker.py` | **13** | ≥6 | 四个布局常量在 graph.js、app.js 里既不定义也不使用、**没有 window 的环境里真的 import 并算一遍**、Worker 引导脚本不重写算法、四条兜底原因都在、回复按 id 认领、超时合理、perf 钩子不混进 `window.TCM`、`growGraph` 真的 await、布局确定性 |

合计 **66** 条新测试。

## 7. 无法完成项

无。两项**实测为负收益/无可改**的已在正文标明并给出数据：

1. **Web Worker 布局**在当前图规模（十几个节点）上是负收益（亚毫秒 → 18.5 ms，
   多出来的是 Worker 创建与一次往返）。保留，因为 R42 换 dagre 之后规模上
   两三个数量级。
2. **首屏字节数**没降（+1.2%）。1.13 MB 的字体是设计成本（已子集化），
   压下去的两条路（减字重 / `font-display: optional`）都是产品决定不是优化，
   不擅自替用户决定。真正省下来的是阻塞请求数与二次访问。

一项**要真机才能量**：`INP`（Interaction to Next Paint）这里量的是
"点击到下一帧渲染完"，跟 Chrome 用户体验报告里那个 INP 口径接近但不等同
（后者取一段时间内的第 98 百分位、且只统计真实用户）。标⏳。

## 8. 落盘

```
scripts/profile_frontend.py            真浏览器前端 profile（六场景 + --compare）
eval/frontend/R41_baseline.json        改前
eval/frontend/R41_after.json           改后（3 次，逐场景中位数）
web/index.html                         去掉 CDN script、三个 script 加 defer、两条字体 preload
web/app.css                            示例区与 chain-body 留位 + containment + 窗口化列表样式 + .measuring
web/app.js                             事件委托、预热轮询只更新横幅、窗口化列表
web/graph.js                           布局常量搬回来、layoutAsync + Worker、showTooltip 读写分离
api/main.py                            _CachingStatic（vendor immutable / 自家代码 no-cache）
```
