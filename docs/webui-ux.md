# WebUI UX 设计（Fungi）

> 2026-09-06 合并自原 `docs/brainstorm-ux.md`、`docs/plan-ux.md`、`docs/mobile-webui-design.md`（内容原样保留，按 桌面美化脑暴 → 桌面美化计划 → 移动端设计 顺序）。本文档是设计与实施记录。
>
> **2026-09-11 现状对照**（正文照旧，下列数字/清单当时快照已过期）：前端文件在**仓库根 `web/`**；
> 体积已长到 `style.css` 36KB、`app.js` 48KB（另有 `common.js` 38KB、`m.js` 54KB）；服务端 WebUI
> 端点见下面「移动端」一节更新后的清单（多了 `/resume` `/save` `/new` `/configure` `/comm-send`
> `/pickfile` `/spawn-pending`）；二维码缺 `segno` 已自愈（spec §20）；好友视图与留言邮箱的后续修订
> 见 spec §15 / §18 / §19。文中「280 tests / 385 passed」等计数也已过期：当前全量 458 passed。

# Brainstorm: WebUI UX 美化（结合 Gasp-Design 组件库）

> 2026-09-05。素材：`useful/基于LLM/Skill/Gasp-Design`（37 个自包含 GSAP 3.12 + Three.js 0.174 动效组件）。
> 目标对象：`web/index.html` + `web/style.css`（26KB）+ `web/app.js`（46KB）。

## Problem

WebUI 功能完整（会话流、工具卡片、consent 卡、好友旁观、agent 气泡、双主题），但视觉层是
"能用"级别：入场动画只有一条 CSS `msgIn` 关键帧，状态变化（consent 切换、文件落地、任务
委派）没有反馈动效，主题切换是整页 0.4s 淡淡的 crossfade，空状态/加载态是纯文字。用户要求
结合 Gasp-Design 彻头彻尾美化 UX。

## Context

- **现有动效资产**：`msgIn` 入场、agent 气泡 `breath` 呼吸、按钮 scale 微动、主题 crossfade。
  其余全是静态。
- **技术约束**：无构建工具（纯 HTML/JS/CSS）；`marked` 已走 jsdelivr CDN——但 WebUI 常在
  **无外网的 LAN joiner 机器**上打开，GSAP 必须落 `web/vendor/` 本地（gsap.min.js ~70KB +
  Flip ~10KB，比 Three.js 600KB 便宜两个量级）。
- **主题**：CSS 变量驱动（浅粉 `#ec4899` / 暗青 `#2dd4bf`），`html.theme-anim` 交叉渐变机制
  已有，必须保留变量体系不动。
- **性能**：聊天流是高频重绘区（流式 markdown），动画必须在 `prefers-reduced-motion` 下
  可关、不得给每条消息常驻 rAF。
- **红线**：UX 违背直觉零容忍——动效不得拖慢操作（过渡 ≤400ms，阻塞交互的动画禁止）；
  263 个 Python 测试不测 JS，但 DOM 结构大改会碰 `app.js` 渲染函数，需手动回归。

## Options Considered

### Option A: 微动效点缀（现有皮肤 + GSAP 交互层）

- **How**：DOM/CSS 不动，新增 `web/motion.js`（单文件，gsap 本地 vendored），只在关键点
  挂 GSAP：消息入场编排、send 按钮液态反馈、consent 卡翻转、agent 气泡进度环、数字计数器。
- **Pros**：一天内完成；每个效果独立可回退；零 DOM 结构风险。
- **Cons**：天花板低——信息层级、排版、空状态、质感原样保留，称不上"彻头彻尾"。
- **Risk**：几乎为零。最坏情况：动效显得贴皮（CSS keyframes 与 GSAP 双轨并存，动效语言不统一）。

### Option B: 设计语言重做 + 统一 GSAP 动效系统（推荐）

- **How**：三层重做，一 commit 一层可单独回退：
  1. **Token 层**（style.css 重构为变量体系）：完整 type scale（12/13/14.5/17/22/28）、
     spacing 4px 栅格、radius/shadow/easing token（`--ease-out-expo` 等与 GSAP 同曲线）、
     表面质感（surface 上再叠极淡 accent 渐变 + 内描边）。DOM 不动，纯 CSS。
  2. **动效层**（motion.js，GSAP 统一引擎，替换散落的 CSS keyframes）：
     - 消息编排：用户消息右入、助手消息左入 + 流式期间 caret 光晕；工具卡片展开用弹性曲线。
     - **consent 卡 = Gasp `flip-drag-reorder` 的 Flip 技法**：允许/询问切换时卡片 3D 翻转
       落章（"已放行" 印章缩放盖下）；asks banner 新卡从横幅弹性垂落。
     - 文件落地：`floating-orbs` 孢子粒子从卡片飘向好友头像（真菌身份梗）。
     - 会话列表：过滤/置顶重排用 **Flip** 布局过渡；活动项左侧 light-trail 指示条。
     - 主题切换：`day-night-cycle` 的日月轨迹 + 背景色相插值，替代现 0.4s crossfade
       （GSAP 驱动 `--bg` 等 4 个核心变量插值，其余变量仍走 CSS transition）。
     - 发送按钮 `liquid-button`；思考指示器 `elastic-wave`（替代三点跳动）；
       token/会话计数 `number-counter`。
     - agent 气泡：idle 时缓慢轨道漂移，运行中叠 `scroll-progress-ring` 式进度环。
  3. **状态层**：空状态（无会话/无好友）配 `floating-orbs` 微景 + 一句引导；加载/重连态
       顶部细光带（`light-trail`）。
- **Pros**：动效语言统一（单一 GSAP ticker、统一 easing token）；真菌身份贯穿（孢子/菌丝
  光带）；三层各自可回退；不引 Three.js，体积可控。
- **Cons**：动量最大——style.css 全量重构 + app.js 渲染函数加钩子约 6-8 处；需要两轮
  真机/浏览器回归。
- **Risk**：流式重绘与 GSAP tween 打架（消息 append 频繁）→ 对策：只对"新节点首帧"做
  enter 动画，流式更新走纯 CSS；`reduced-motion` 一刀切跳过。

### Option C: 沉浸 3D 层（Three.js 点云 + 着色器）

- **How**：背景 `pointcloud` 孢子场、主题切换 `shader-distortion`、agent modal `disassembly-3d`。
- **Pros**：观感冲击最强，演示效果拉满。
- **Cons**：Three.js ~600KB 本地化；日常聊天工具里是性能税与注意力税；低配 joiner 机器
  风扇起飞；对"违背直觉零容忍"红线最危险。
- **Risk**：高。作为日常生产力 UI 是负资产。
- **判定**：不作为基础方案。Option B 完成后若想要一个"哇"点，可单加主题切换的
  `pixelation-transition`（2 行内联 shader 成本）——单列 open question。

## Recommended: Option B

理由：A 达不到"彻头彻尾"的要求；C 违背 LAN 生产力工具的性能直觉；B 用 GSAP 一个引擎
吃到 Gasp-Design 的编排精华（Flip/light-trail/orbs/day-night），体积 +80KB，三层分离
每层可独立回退（git revert 单 commit 即可，符合"不满意随时回退"）。

### Implementation outline

1. `web/vendor/gsap.min.js` + `web/vendor/Flip.min.js` 本地化（jsdelivr 拉取一次落盘）。
2. commit 1：`style.css` token 重构（纯 CSS，零 JS 改动）→ 浏览器全页面回归。
3. commit 2：`motion.js` 消息编排 + 输入区 + 主题 day-night → 手动回归流式聊天。
4. commit 3：consent 卡 Flip 落章 + asks banner 垂落 + 孢子落地粒子。
5. commit 4：会话列表 Flip + light-trail + 空状态微景 + agent 气泡进度环。
6. 每步 `prefers-reduced-motion` 全量旁路；`app.js` 钩子以 `window.fungiMotion?.xxx` 可选
   调用（motion.js 缺失时 UI 完全正常——天然回退开关）。

## Open Questions

1. 主题切换是否要加 `pixelation-transition` 彩蛋（Option C 的唯一残留）？默认不加。
2. 磁吸光标 / 鼠标聚光灯这类桌面级炫技默认不做（生产力工具注意力成本），同意吗？
3. 字体是否引入本地化中文字体文件（体现 type scale），还是继续系统字体栈只调字号层级？
   默认后者（零体积成本）。


---

# Implementation Plan: WebUI UX 美化（方案 B：设计语言重做 + 统一 GSAP 动效系统）

> Design: `docs/brainstorm-ux.md`（2026-09-05 用户批准 Option B）。
> 已拍板：不加 pixelation 彩蛋；不做磁吸光标；字体走系统栈。

## Component Map

```
NEW:
- web/vendor/gsap.min.js          （3.12.x core，本地化，jsdelivr 拉取一次落盘）
- web/vendor/Flip.min.js          （Flip 插件，会话/好友列表重排）
- web/motion.js                   （动效模块，唯一 GSAP 入口；可整文件删除）
- docs/plan-ux.md                 （本文件）

MODIFIED:
- web/index.html                  （<head> 加 vendor+motion 两个 <script>，其余不动）
- web/style.css                   （commit 1 重构 token；commit 2-4 配合微调）
- web/app.js                      （8 处钩子，全部 window.fungiMotion?.x?.() 可选调用）

DELETED: (none)
```

## Interface Contract（动效模块唯一接口）

```js
window.fungiMotion = {
  reduced: Boolean,          // prefers-reduced-motion 命中时 true，其余全短路
  msgIn(el, kind),           // kind: 'user'|'assistant'|'tool'|'error'|'ask'
  askCardIn(el),             // asks banner / 内联 pending 卡垂落
  askResolved(card, ok),     // 3D 翻转落章（ok=允许→印章"已放行"）
  spores(fromEl),            // 孢子粒子（文件落地）
  listFlip(container, fn),   // Flip 包装：fn() 内做 DOM 变更
  themeTo(t),                // day-night 插值核心变量；app.js 仍负责 data-theme
  ring(el, pct),             // agent 气泡进度环
  waveOn(el)/waveOff(el),    // 思考波
  counter(el, to)            // 数字递增
};
```

app.js 侧**只允许** `window.fungiMotion?.msgIn?.(...)` 形式调用——motion.js 缺失时 UI
完全静态可用（天然回退开关 + 每层 revert 不留死引用）。

## Tasks

### Task 0: vendor 本地化
**Files:** `web/vendor/gsap.min.js`、`web/vendor/Flip.min.js`
**Acceptance:** 文件存在且浏览器加载 `gsap.version` 正常；直连失败走 7897 代理拉取。
**Depends on:** none

### Task 1: Token 层重构（commit 1，纯 CSS 零 JS）
**Files:** `web/style.css`
**What:** `:root`/dark 补齐 type scale（12/13/14.5/17/22/28）、spacing 4px 栅格、
`--ease-out-expo` 等 easing token（与 GSAP 曲线一致）、shadow/surface 质感（淡 accent
渐变 + 内描边）；全选择器换算到 token。**现有变量名一个不删**（只增不改语义）。
**Acceptance:** 浏览器实测浅/深主题全部 surface（会话/好友/聊天流/三张 modal/横幅）；
截图给用户过目 → **检查点：停等确认**。
**Depends on:** none

### Task 2: 动效引擎 + 输入区 + 主题（commit 2）
**Files:** `web/motion.js`（新）、`web/index.html`、`web/app.js`（`addDiv:13`、
`renderTurnLive:582`、`applyTheme:1030`、send 按钮 wiring 处）、`web/style.css`（微调）
**What:** motion.js 骨架 + reduced-motion 守卫；`msgIn` 编排（用户右入/助手左入，流式
节点只动首帧）；liquid 发送钮；`elastic-wave` 思考指示器替换现三点/状态文字；主题
day-night（GSAP 插值 `--bg/--surface/--text/--accent` 四核心变量，其余走现有
`theme-anim` CSS transition 兜底）。
**Acceptance:** 流式聊天无卡顿（DevTools Performance 无长任务峰值）；删 motion.js 刷新
后 UI 正常；主题切换往返 5 次无残留中间态。
**Depends on:** Task 0, 1

### Task 3: consent/ask 卡与孢子（commit 3）
**Files:** `web/motion.js`、`web/app.js`（`buildActiveAskCard:686`、`answerPendingAsk:823`、
`buildPendingAskCard:773`、好友视图 transfer 落地处）
**What:** asks banner 新卡垂落；应答翻转落章；transfer 落盘成功 → `spores()` 从卡片飘向
好友行。
**Acceptance:** `FUNGI_SELFTEST=1` 全链路 + 浏览器实测 allow/ask/no 三路径动画完整、
连点不炸（动画期间按钮仍可点，无队列堆积）。
**Depends on:** Task 2
> **v0.1.1 后记**：应答时的孢子粒子已移除（用户裁决——允许/拒绝只保留翻转落章）；
`spores()` 仅保留给好友视图 transfer 落盘成功使用。

### Task 4: 列表 + 托盘 + 空状态（commit 4）
**Files:** `web/motion.js`、`web/app.js`（`renderSessionList:220`、`renderFriendList:927`、
`agentBubble:299`、空状态节点）、`web/style.css`
**What:** 会话/好友列表 Flip 重排 + light-trail 活动指示条；空状态 floating-orbs 微景；
agent 气泡轨道漂移 + 进度环。
**Acceptance:** 过滤输入实时重排无跳动；好友进出房间列表平滑；reduced-motion 下全部静止。
**Depends on:** Task 2

## Execution Strategy

- 顺序执行 0→1→2→3→4（同文件串行，无并行空间）。
- **检查点：Task 1 完成后停**，截图给用户确认 token 层观感（最激进的一步，此时回退成本最低）；
  2→4 连续执行，每 commit 后浏览器截图随交付汇报。
- 每 commit 前跑 `python -m pytest -q`（Python 侧应零影响，跑全量兜底）+ 手动浏览器回归。

## Global Constraints

1. 动效只动 `transform/opacity`（Flip 一次性读布局除外）；时长 ≤380ms；ambient 循环必须
   可暂停且 reduced-motion 全关。
2. 不阻塞输入：动画期间按钮可点、无动画队列堆积（GSAP `overwrite:'auto'`）。
3. CSS 变量名向后兼容：只增不删；`html.theme-anim` 机制保留为 fallback。
4. 不引构建工具、不引 Three.js、不改任何 Python 文件。
5. 一任务一 commit；浏览器验证按 fungi-webui-verify 配方（in-process harness + hub start，
   注意 proxy/JS 陷阱）。


---

# 移动端 WebUI 设计（2026-09-06）

目标：手机扫码进入专用移动网页端；电脑端继续走「打开 WebUI」。扫码/点按钮天然分流。

## 决策（已与用户确认）

| 分叉 | 决策 |
|---|---|
| 二维码入口 | GUI 新页面「手机端」，位置：加入房间之下、模型配置之上。不放在桌面 WebUI 里（用户不想先进 WebUI） |
| 局域网安全 | token 门禁：非 loopback 请求必须带 `?t=<token>`，否则 403。本机访问不受影响 |
| 功能范围 | 全功能：聊天 + 会话抽屉 + ask/consent 卡片 + 好友视图 + agent tray + 文件上传，分切片交付 |
| 实现方式 | 独立 `web/m.html` + `web/m.css` + `web/m.js`，复用现有后端 API；不改造桌面 app.js |

## URL 与 token

- 服务端首次启动时生成 token 并持久化到 `~/.fungi/webui_token`（重启不变，手机书签长期有效）。
- WebUI server 绑定从 `127.0.0.1` 改为 `0.0.0.0`（hub 先例）。
- 二维码内容：`http://<LAN-IP>:<port>/m?t=<token>`。LAN IP 复用 `__main__.py` 的 `_lan_ip()` 思路（UDP connect 探测，失败回落 127.0.0.1）。
- `m.js` 首次从 URL query 取 `t` 存 localStorage，之后所有 fetch/`/events` 请求自动附带 `?t=`；收到 403 → 清 token 显示「请重新扫码」遮罩。
- 新端点 `GET /lan` → `{ip, port, token, url}`（本机 loopback 才返回 token；非 loopback 请求不回显 token）。

### 鉴权规则

- 请求源 IP 是 loopback（127.0.0.1/::1）→ 放行（桌面 WebUI、GUI 自检零改动）。
- 非 loopback → 校验 `t` 参数（GET query / POST 由前端一律拼 query）；不匹配 403。
- 静态文件同样受门禁：`m.html` 本身就要 token 才能拿到（token 在 URL 里，无额外交互）。

## 服务端改动（fungi/server.py）

1. `make_webui_server` 绑 `0.0.0.0`；token 懒生成 + 持久化。
2. `do_GET`/`do_POST` 入口统一过 `_authorized(self)` 门禁。
3. 静态路由加 `/m`、`/m.css`、`/m.js`（沿用 `_send_static`）。
4. `GET /lan` 端点。
5. 现有 API 全部不动（2026-09-11 实况——GET：`/` `/m` `/app.js /common.js /style.css /motion.js /m.css /m.js` `/vendor/*` `/model` `/config-status` `/lan` `/asks` `/sessions` `/session` `/peers` `/consent-mode` `/comm-log` `/mail` `/events` `/spawn-pending`；POST：`/chat` `/retry` `/stop` `/resume` `/answer` `/mail/read` `/configure` `/save` `/new` `/consent-mode` `/upload` `/pickfile` `/comm-send`）。
   - 2026-09-25 追加（spec §66，只增不改）：`GET /model` 多回一个 `models`（下拉列表那份可选列表）；
     新增 `POST /model`（切模型 + 自动探一次调用）。`/configure` 的 Model 框从「覆盖」改成「添加」。
     缓存里的老页面只读 `d.model`，两处都还是它要的形状，照旧能用。
   - 2026-09-25 再追加（spec §66.7 自绘下拉 / §68 每个模型记自己的端点与密钥）：头部那个模型列表是
     **自绘**的下拉（原生 `<select>` 的弹出层由系统画，吃不到 token 也没有开合过程 —— 用户当天报告
     「风格和原来的不搭，没有动画效果」）；面板 `position:fixed` 挂在 `body` 上（留在头部会被
     `#title-wrap{overflow:hidden}` 裁掉），开合动画在 `motion.js` 的 `menuIn/menuOut`
     （`reduced` 时模块整体折叠 = 瞬时开合）。`GET /model` 再多回一个 `endpoint`
     （**密钥永不上页面**），`POST /model` 起连 url+key 一起切。旧的 `{model, models}` 形状仍是子集，
     缓存页面照旧能用。

## GUI：手机端页（当时写在 `fungi/gui.py`，2026-09-10 起是 `fungi/gui/mobile.py`）

- `MobilePage(QWidget)`，`addSubInterface` 顺序插在 join_page 与 cfg_page 之间（图标 `FluentIcon.QRCODE` 若可用，否则 CAMERA/SCAN 兜底）。
- 进入页面（或点「生成二维码」）时若 WebUI 未启动则懒启动（`room.open_webui(open_browser=False)`），随后：
  - segno（纯 Python、零依赖）生成 PNG bytes → QPixmap → 居中显示（白底留 quiet zone，深色前景）；
  - 下方显示可复制的 URL 文本 + 「刷新 IP」按钮（换网络后重测 LAN IP）；
  - 未发起/未加入房间时显示提示文案「先发起或加入房间」。
- 依赖：`segno` 加进 pyproject（GUI 运行时依赖；exe 打包随之带入）。
- 页面提示一行防火墙说明：连不上时检查 Windows 防火墙入站规则（Public 配置文件常拦 Python）。

## 移动端页面设计（m.html/m.css/m.js）

### 布局与手势（用户规格）

- 首页 = 当前会话聊天页。**右划（手指从左边缘向右）→ 会话列表抽屉**：抽屉宽度 = 视口 2/3，停在右侧露出 1/3 主页面；**左滑收回**。抽屉上方为主页面半透明暗罩，点击暗罩也收回。
- 跟手拖拽 + GSAP 松手惯性回弹（速度阈值 + 位移阈值判定开/合）；CSS `will-change: transform`，仅 transform/opacity 动画。
- gasp-design 细节：抽屉列表项 stagger 入场、发送按钮按压微缩、流式尾部光标呼吸、新会话按钮弹性、ask 卡片入场翻动。

### 功能切片

1. **MVP**：消息流式渲染（复用桌面的事件契约：text/reasoning/tool/done/status/error）、会话抽屉（新建/切换/删除/重命名暂缓——重命名先不做，桌面独有）、停止（⏹ 替换发送键）、`/events` 断线重连、token 处理。
2. **ask 卡片**：`pollPendingAsks` 同款 3s 轮询 + 卡片重挂契约（Map 注册 + 重绘后重挂，见 skill://fungi-webui-card-contract）——移动端同样必须遵守，否则卡片被流式重绘吞掉。
3. **好友视图**：抽屉里好友分组 + `/comm-log` 只读会话 + consent 滑条（allow/ask）。
4. **agent tray + 上传**：子代理气泡列表 + 点开模态；`/upload` 走 `<input type=file>`。
5. 明确不做：config 弹窗（API key 在 GUI 配）、文件浏览按钮复用上传、Alt+R 重试改成长按消息重试（后续再议，首版不做）。

### 视觉

- 沿用桌面主题变量（浅色默认/深色切换，localStorage 同键 `fungi-theme`），viewport 基础字号 16px 防 iOS 缩放；触控目标 ≥ 44px；`100dvh` 布局 + `env(safe-area-inset-*)`。
- 输入区固定底部；键盘弹出用 `visualViewport` 监听补偿（iOS Safari 必踩）。

## 测试与验证

- server：`tests/test_room.py` 风格新增用例——token 门禁（伪造非 loopback：单元级测 `_authorized`）、`/lan`、`/m` 静态路由；现有 localhost 测试必须保持全绿。
- GUI：offscreen 冒烟（MobilePage 可实例化、按钮存在）。
- 端到端：browser-act 开 `http://127.0.0.1:<port>/m?t=...` 断言抽屉手势、流式渲染、卡片跨重绘存活；真机手机扫码验证（防火墙放行后）。
- 一切片一 commit。

## 风险

- Windows 防火墙拦入站 → GUI 页提示 + 文档记录；真机联调时实测。
- iOS Safari 长按选择/橡皮筋滚动与手势冲突 → `touch-action` 与 `user-select` 按区域控制。
- `_TURN_TAPES` 内存上限未实测（既有已知项），移动端长时间挂后台重连会加重它——不在本切片解决，记录观察。

## 实施结果（2026-09-06，HEAD f6cf578+）

全部切片已交付：25b7a37（server 门禁+lan+m 路由）、5ea6c57（GUI MobilePage+segno）、2eeeac7（磁带 bug 修复）、f6cf578（移动端前端）、后续好友/agent tray commit。

- **好友视图**：抽屉内「好友」分组（/peers 5s 轮询）；点开 = 只读 comm-log + 顶栏「←」返回 + 权限分段控件（允许/询问，POST /consent-mode）。
- **agent tray**：agent_spawn/status/event → 输入区上方横向气泡（状态色点）；点开底部模态看 goal/status/最近 30 条事件。
- **移动端文件入口改为上传**：桌面「选择文件」调 `/pickfile`（服务端 tkinter 对话框选**电脑**文件），手机触发会在电脑上弹窗，语义不成立——移动端不调它，改用 `<input type="file">` + `POST /upload`，把**手机上的**文件传上本机 server 再取其落盘路径（`web/m.html` 聊天输入区与好友输入区各一个，逻辑在 `web/m.js`；`tests/test_webui_upload.py` 覆盖上传端点）。跨主机传文件仍走 hub transfer（agent 工具），不经过 WebUI。
- **验证中抓到并修复的既有 bug**（2eeeac7）：磁带 60s grace-pop 按会话 id 无世代 pop——上一回合 done 后 60s 内同会话开新回合，新回合运行中磁带被弹掉，刷新重连拿到裸 done 静默丢失直播视图（桌面同样中招）。修复：pop 按磁带对象身份校验；grace 提为 `_TAPE_GRACE_S` 供测试。
- **前端坑**：scroll-bottom 按钮必须放在 `#messages` 外层（全量重绘 innerHTML='' 会销毁它，桌面靠 `if (!b) return` 掩盖成功能缺失而非崩溃；移动端曾因此 TypeError 吞掉 reattach）。
- **`#messages` 一次只有一个主人（2026-09-10 真机：好友对话"像被刷新掉了"）**：好友视图打开时，**任何会话侧的重绘都必须早退**——`renderMessages` 入口 `if (friendView) return;`（`reloadSessionFromServer`/`switchSession` 都经它落笔），`handleTurnEvent` 的 `done` 分支与 `recoverAfterDrop` 也只刷新会话列表不落笔。此前用「会话回合跑完」触发 `reloadSessionFromServer()` 会把好友对话整屏换成会话（复现：回合中途点进好友视图，done 到了以后 `#messages` 从 44 个 friend 节点变成 2 个会话节点），而 comm-log 载荷没变 → `lastFriendPayload` 让好友视图再也画不回来，只有刷新页面才恢复。配套三条硬约束：① `renderFriendChat` **先判空后清屏**（空载荷不得擦掉已有画面）② 载荷没变不重绘 ③ 重绘抛错必须 `lastFriendPayload = null` 解缓存，否则一次异常就把视图冻死到刷新为止（手机端 m.js 是同一套代码，同样三条）。
- **好友会话渲染的是一张时间轴（2026-09-10 顺序根治，用户点名「卡片没按时间顺序渲染」）**：载荷里的东西各有各的顺序（transcript 列表、hub 事件按 ts、mail 按 ts、asks 累计桶），**不许串接**。规则：`renderTranscript` 里 ask 卡按 `call_id` 锚在触发它的工具调用处（旧记录退化为「问题原文与调用参数逐字相等」的精确匹配 → 再退化到存储顺序）；带 `ts` 的行（事件/邮件/带 ts 的卡片 ask）用 `insertByTs` 插到第一个 `data-ts` 更大的兄弟节点前；没有 `ts` 的行（旧转录、旧 ask）保持到达顺序——旧 ask 的工具调用已不在转录里 ⇒ 属于更早的回合 ⇒ **前置**，不甩末尾。数据侧：`asks` 记录带 `call_id`+`ts`，comm 转录消息落盘时由 `merge_comm_history` 给新行打 `ts`（旧行保留原 ts）。
- **验证**：280 tests 全绿（+7）；门控 harness + browser-act 实测 token 门禁（LAN 无 token 403 / 带 token 200 / loopback 免检）、抽屉合成触摸手势开合、流式回合、done 落点、中途刷新重连接续、agent 气泡+模态。真机手机扫码待用户实测（Windows 防火墙可能需放行 Python 入站）。

## 真机首扫反馈修复（2026-09-06 晚）

手机扫码后弹「链接已失效」遮罩。根因**不是 token 不一致**（GUI 二维码与 `~/.fungi/webui_token` 同源，房间 Token 与 WebUI t= 是两回事，GUI 已加说明文字），而是：`m.html` 引用的 `/m.css`、`/m.js`、`/vendor/*` 是写死路径无法带 token，手机（非 loopback）全部 403 → CSS 丢失使遮罩失去 `display:none` 直接露出、JS 丢失页面死掉。修复：静态壳资源（页面/css/js/vendor）豁免门禁——壳里没有数据，无 token 打开 `/m` 时 m.js 正常运行并显示有样式的重扫码遮罩；数据端点保持全门禁；`/vendor/..` 穿越仍 403。同时：marked 从 jsdelivr CDN vendor 化到 `/vendor/marked.min.js`（国内手机网络 CDN 不可达会让 m.js 首行 ReferenceError 全页死掉，桌面 index.html 一并改本地）；遮罩改为默认可见、有 token 才隐藏（JS 挂掉也显示有意义提示）；连接被手机 reset 的 10054 噪音 traceback 由 `WebUIServer.handle_error` 吞掉。

## 手机端第二道门：防火墙按「程序」放行（2026-09-12）

用户报告「exe 扫码进不去移动 WebUI」，源码版却能进。根因不在前端也不在服务端：Windows 防火墙的入站例外
**按程序**给，`python.exe` 当年被放行过、新解压的 `Fungi.exe` 没有 → 手机的包被静默丢弃（桌面零提示、
手机一直转圈）。二维码地址本身没问题（`http://<lan_ip>:<port>/m?t=<token>`，WebUI 绑 `0.0.0.0`）。

落地：手机端页自检「当前 exe 有没有入站放行」，没有就显示一行说明 + 「放行防火墙（手机才能连）」按钮，
点它走 UAC 调 `New-NetFirewallRule`（幂等：先删同名规则再加），随后自动复检。实现与坑（PowerShell 探测
1.8~2.6s 必须异步、程序可能有多条规则所以按数字判断、缓存策略、GUI 测试不许 shell out）见 `docs/spec.md` §32。
规则按**程序路径**匹配：换目录/换版本的 exe 要重新放行。

## 视频模型门控（2026-09-06 深夜；同夜改为统一就绪检查）

用户诉求："不要在要用的时候才下载"、"维持正常运行的都要一并检查"。VidSense 已 vendored 进仓库（顶层 `vidsense/`，上游 Offblink/VidSense 仍是开发上游，更新手动拷），三层落地：

1. **`fungi/tools/video.py` — 单一事实来源 `_video_ready()`**：一次查齐全部运行组件——Python 库（huggingface_hub/torch/transformers/faster_whisper/opencv，`find_spec` 零导入成本）、ffmpeg+ffprobe（`shutil.which`）、两个 HF 模型缓存（`_model_cached`：`HF_HUB_CACHE` 或 `~/.cache/huggingface/hub` 的 `models--<org>--<name>/snapshots/<rev>/`，≥50MB 阈值防半截）。`_HEALABLE = {huggingface_hub, CLIP, whisper}` 标记下载按钮可自愈的项。`tool_video` 缺任何组件即拒绝：可自愈缺失给 pip/下载脚本指引；torch 等 "不可自愈缺失" 提示手动装 VidSense 依赖。绝不现场下载。
2. **`scripts/download_video_models.py`**：`HF_ENDPOINT` 默认 hf-mirror.com；`list_repo_files` 选唯一 torch 权重再 `snapshot_download(allow_patterns=...)`（CLIP 仓库另有 tf/flax 权重 ~1.8GB 必须筛）；try-import 守卫缺 huggingface_hub 打印 pip 指令 exit 1。
3. **GUI `ConfigPage`**：状态行显示全部 8 项 ✓/✗（进场 + showEvent 自动检查）+「下载缺失模型」按钮——**仅当存在可自愈缺失（依赖/模型）时启用**；torch/ffmpeg 等缺失只提示手动装。点击 = 阶段链：缺 huggingface_hub → 先 `pip install huggingface_hub` → 再跑下载脚本；QTimer 1s 轮询（不用 Signal 传参），每步成功自动接下一步、完成后自动复检。**坑：`_check_video_models` 会覆写状态行——`_start_next_dl_step` 必须先 check 再写进度文案**。冻结 exe：`_python_cmd()` 落 `shutil.which("python")`。子进程 CREATE_NEW_CONSOLE（exe --noconsole 也会弹控制台）。

（2026-09-12 用户定调：这一整块在设置页里从顶层「VidSense」标题**挪进设置页末段**，成为小标题「视频理解」；
`ConfigPage` 里就是把它从「来信提醒」后面移到末段，其余状态行/下载按钮原样。
2026-09-15 再定调：这个末段的标题是**「拓展」**，不是「实验性」——判据是“这个能力有没有独立项目”：VidSense 与 GhostWorld 都能单独跑，所以进拓展；「实验性」留给日记与桌面控制。末段之下是 GhostWorld 角色控制与视频理解。）

测试：`tests/test_video_tool.py`（fixture 打桩 `_video_ready` 为 `_ALL_READY`；可自愈缺失指引用例；torch/ffmpeg 缺失 → 手动安装提示用例；`_model_cached` 布局/阈值单测）+ `tests/test_gui.py`（`_ready(**overrides)` 工厂：全就绪→禁用；CLIP 缺→启用；仅 hub 缺→启用（可自愈）；仅 torch/ffmpeg 缺→禁用+手动提示；缺依赖先 pip 后脚本的阶段链；exe 解释器回退）。

## background 工具与 Bg 气泡（2026-09-06 深夜）

`background` 工具：一条命令丢后台直跑（工作线程 + bash abort，无子代理、零 LLM 调用），`dispatched (id=...)` 即回，输出经 `[background report]` 回传再激活。前端右上角气泡显示 **Bg**（spawn 仍显示 L2/L3），点开看命令与输出。

气泡生命周期补丁：后台任务完成时的 `agent_status` 原本发往已关闭的派发回合流（被 suppress），前端永远收不到 done——`/resume` 回合现在重发各项最终状态；/stop 杀掉的残跑气泡由前端在 `done (aborted)` 时统一置为 stopped 并移除（桌面与手机端同构）。
