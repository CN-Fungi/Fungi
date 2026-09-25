# Spec: Fungi

> 定位：以 YESIR 为基座的 LAN 多主机 Agent 协作网络。server 发起房间，client 直连 server，client 间流量由 server relay。存储统一在 server 主机。核心洞见：(1) 通讯 Agent 之间自主交流仅限 `public/`，其他目录需征求同意；(2) 用户仅与本机 Agent 交流，跨主机事务交由通讯 Agent 处理。

> 2026-09-03 评审定案：无 Redis（见 docs/architecture.md 的 Brainstorm 修订记录）；托盘栈 PyQt6；consent 裁决者为目录属主 host 的用户。

## 1. 术语与实体

| 术语 | 定义 |
|---|---|
| host | 一台运行 Fungi 进程的主机，用户起名，房间内唯一 |
| server | 发起 LAN 的 host，承载 hub（HTTP relay + 存储） |
| client | 直连 server 的 host |
| Agent | host 进程内的一个 Agent 分身 |
| 本机 Agent（local） | 专职与用户交互的 Agent，每 host 恰一个 |
| 通讯 Agent（comm） | 专职与一台远端 host 的对位通讯 Agent 交互的 Agent，每个远端 host 一个 |
| 对位（counterpart） | host A 上对接 host B 的通讯 Agent 与 host B 上对接 host A 的通讯 Agent 互为对位 |
| `public/` | server 存储上的公共目录，通讯 Agent 自由读写 |
| `homes/<host>/` | server 存储上各 host 的属地目录，非属主访问需属主用户 consent |

## 2. 拓扑与生命周期

- server 启动：生成/读取房间 token，起 hub HTTP 服务 + 本机 Agent；通讯 Agent 按名册动态增删。
- client 加入：HTTP join（name + token）→ server 建名册项并回发 host 列表；此后心跳保活。
- Agent 生成规则：每 host 维护「对端 host → 通讯 Agent」映射；名册变化（新 host join / 心跳超时剔除）时增删通讯 Agent，两端同步。
- 退出：client leave 或心跳超时被剔除；server 关停即房间解散。

## 3. 消息协议

JSON envelope，HTTP 承载：

```json
{"v": 1, "id": "uuid", "src": "alpha:comm-beta", "dst": "beta:comm-alpha",
 "type": "chat", "ts": 1730000000, "reply_to": null, "body": {}}
```

- type：`chat`（对话）、`task`（goal/reply_format/context 委派）、`result`（task 回执）、`ask`（同意/提问请求）、`answer`（对 ask 的回答，reply_to=ask_id）、`err`（另有 `transfer` §10、`mail` §14）。
- 可靠性：server 为每 Agent 维护内存 inbox，收端长轮询拉取后 ack；投递按消息 id 去重，语义 at-least-once。

## 4. Server（hub）职责

端点（Face 风格，token 鉴权）：

- `POST /api/join` `{name, token}` → `{host_id, peers, fs_base}`；`POST /api/leave`
- `POST /api/heartbeat` → 顺带返回待办通知（pull 模型，与 Face 一致）
- `POST /api/send` envelope → 投递（本地直投或 relay 转发，同一函数）
- `GET /api/poll?after=<cursor>` → 长轮询 inbox
- 存储代理：`/api/fs/ls|read|write|edit|glob|grep`、`/api/sessions...`（YESIR session 语义），全部经路径守卫

hub 内存态：名册、各 Agent inbox、pending-ask 注册表（ask_id → 投递状态，供 heartbeat 重放未决通知与去重）。跨 Agent 文件写锁用 hub 内存锁（LAN 规模无需分布式锁）。

存储布局（server `data/`）：`sessions/`（YESIR 兼容 JSON）、`public/`、`homes/<host>/`。

> **会话归属修订（2026-09-04）**：会话必须按 host 隔离——server 角色存 hub store
> `data/sessions/`；client 角色存**本机** `sessions/`（YESIR 默认目录），不再经
> `/api/save` 落到对面操作的磁盘上。此前共享单目录导致任一方的 WebUI 会话列表
> 列出对方全部对话（真机回归发现，用户判定为严重隐私问题）。hub 的
> `/api/sessions` 仅供 server 角色自身使用；另加 `POST /api/transfer/upload`
> （raw 字节流式上传，token 查询串鉴权，413=超 max_file_mb），让用户面 Agent
> 能发送**本机真实文件**（store 之外的路径）。

## 5. 同意流（消息面承载，无 Redis）

ask 是普通消息，不需要独立协调设施：

```
请求方 Agent 调 confirm / inquire
  → 发 ask envelope（to=目标 host:local）
  → PendingAsk 注册表登记，threading.Event 阻塞（复用 YESIR tools/ask.py 机制）
  → relay 投到目标 host 的本机 Agent → WebUI 卡片（asks 横幅，打开即见）
  → 用户打开 WebUI → 卡片（允许 / 禁止 / 自定义输入）
  → 本机 Agent 回 answer envelope（reply_to=ask_id，value=yes|no|自定义文本）
  → 请求方唤醒，返回 "USER: <value>" / "DENIED"
```

- 超时默认 600s（用户可能不在电脑前，比 YESIR 的 300s 长，可配）；超时返回 `"ERROR: 用户未回答"`。
- 断线补偿：本机 Agent 心跳时从 hub pending-ask 注册表重放未决卡片。
- 裁决者：ask 涉及 `homes/<owner>/` 时 to=属主 host 的 本机 Agent；本机属主操作 to=本机 本机 Agent（同进程直连，不走网络）。

## 6. Agent 规格

### 6.1 通讯 Agent

- 工具：`send_peer(text)`（只发 chat；task 由本机 Agent 的 `delegate` 发，见 §6.2）、`send_file(host, path, name, reason)`（§10）、`amail(host, subject, body)`（§14）、`read_file/write_file/edit_file/glob_files/grep_files`（路径守卫版，hub 侧 op 名是 `ls|read|write|edit|glob|grep`，见 §4）、`confirm(host, action, path, reason)`、`inquire(...)`，另挂 `todo`（§16）。
- 2026-09-10 移除 `spawn` / `background`：通讯 Agent 由对端驱动、身边没有用户监督，而 clone 没有 spawn 的再激活通道（子代理只能同步多跑一跳，`background` 的报告也无处落地）——两者都只放大这个不受控 Agent 的爆炸半径（`trilayer.build_clone_agent(subagents=False)`）。
- 路径守卫：`public/` 自由；`homes/<owner>/` 非属主需 consent（confirm 发往属主 host 的 本机 Agent）；`homes/<own>/` 与自身会话目录需自身用户 consent；`sessions/` 拒绝。
- 自主交流：对位通讯 Agent 之间 chat/task 自由往来，无需用户参与；涉及 `public/` 之外的文件操作才触发 consent。

### 6.2 本机 Agent

- 工具：YESIR 原生全套（shell/web/inquire…，其中 shell 除一次性 `bash` 外还有会话式 `bash_start`/`bash_send`/`bash_kill`，见 §31）+ `delegate(host, goal, reply_format)` + `peers()`。
- 用户仅与本机 Agent 对话（核心洞见 2）；delegate 内部把 task envelope 发给对应通讯 Agent 并阻塞等 result。

### 6.3 ask 汇聚

所有 ask（含通讯 Agent 的 inquire / confirm）统一为 ask envelope 落到目标 host 的本机 Agent → WebUI 卡片。本机 Agent 自己的 inquire 是同一机制的同进程特例（直连 PendingAsk，不过网络）。

## 7. WebUI 与托盘

- WebUI 默认关闭：进程启动即最小化到托盘（PyQt5 + qfluentwidgets，2026-09-05 起统一；
  运行时画图标、fluent 菜单、单实例）；托盘菜单「打开 WebUI / 打开数据目录 / 退出」，双击托盘打开 WebUI。
- 有未决 ask 时在 WebUI 顶部横幅展示卡片（asks banner）；用户点托盘 → 打开 WebUI。
- ask 卡片渲染于聊天流：允许 / 禁止 / 自定义输入框，对应 answer value `yes` / `no` / 自定义文本。
- 会话存储在 server；WebUI 经本机 Agent 代理读写（对用户透明）。

## 8. 安全

- 房间 token：join 与所有 API 必带，错误 token 403。
- 路径守卫在 server 端强制（不只靠 Agent 自觉）：resolve 后前缀校验，拒绝 `..` 与绝对路径逃逸。
- v1 不做传输加密（LAN 内网假设，明文 HTTP），文档明示。

## 9. 工程约束

- Python ≥ 3.13；标准库 + PyQt5 + PyQt-Fluent-Widgets（GUI 与托盘同一套绑定与组件）。
- pytest；真实 LLM 只做冒烟（ZAI_API_KEY 配方）。
- ruff 全套（lint + format），`scripts/check.ps1` 门禁与 YESIR 相同。
- 一任务一 commit（英文祈使句）；push 需用户发话。

## 10. 增补（2026-09-03 定稿）：好友列表、会话旁观、文件传输

- **消息类型**：新增 `transfer`（文件传输元数据：id/name/size/reason/from）。
- **通讯会话落盘**：hub 投递成功后镜像 chat/task/result/transfer envelope 到
  `data/comm/<hostA>__<hostB>.jsonl`（按 host 名排序，双向同文件，单写者 = relay）。
  `Clone.history` 仍只作 LLM 上下文。
- **chat 回复兜底（2026-09-10 废除，见 §20）**：chat 回合若 LLM 未调用 send_peer 且最终文本非空，回合结束钩子自动补发
  （防止 LLM 忘调工具导致回复静默丢失，2026-09-03 真机实测发现）；显式调用过则不重复。
- **好友列表**：`GET /api/peers`（hub）→ 本机 Agent 代理 `/peers` → WebUI 侧栏在线成员；
  点击进入只读会话视图（`GET /comm-log?host=` 渲染双方通讯 Agent 对话流），无输入框
  （核心洞见 2 不破）。
- **文件传输（C2，落对端本地盘）**：字节面 store-and-forward——`POST /api/transfer`
  服务端从 store 复制暂存（上限 `max_file_mb`，config.json，默认 200——**2026-09-18 上限取消，见 §48**），envelope 只传元数据；
  控制面复用 consent——接收方 通讯 Agent 向属主本机 Agent 发 ask（同意模式由滑块控制，见下），
  同意后经 `GET /api/transfer` 下载落盘 `<inbox_dir>/<来源host>/<文件名>`（config.json
  `inbox_dir`，默认 `<repo>/inbox`，重名加序号，basename 消毒）。落盘路径经 result envelope
  回执发送方。transfers 注册表在内存，server 重启丢失未拉取的暂存文件（v1 容忍，与 §3 一致）。
- **同意模式滑块（2026-09-04 修订）**：一次性「始终允许」废除——持久放行改为每个好友的
  可见可逆开关（WebUI 好友会话顶部滑块，左=允许，右=询问），存于 `~/.fungi/consent_rules.json`
  的 `modes`（host → allow|ask，默认 ask）；旧版 `always_allow` 地址列表自动迁移为 host 模式。
  判定键为 ask body 的 `from`（逻辑请求方）——传输回执的 envelope src 是接收方自己的
  通讯 Agent，按 src 键控会错挂到自家 host。inquire（通用提问）永不自动放行。
- **display-name 层（2026-09-04）**：wire 身份仍是 ASCII 安全的 host 名（envelope 地址、
  URL、文件名——主机名强校验的理由不变），昵称只走展示层。`Member.display` 随 join 携带、
  re-join 刷新（UI 改名无需重启）；`/api/peers` 与 join/heartbeat 的 `roster` 字段返回
  `[{name, display}]`；WebUI 侧栏/好友会话标题/旁观消息来源/通知标题显示昵称，无昵称回退
  显示 name。入口 `--display`，config.json `display` 可存；昵称做清洗（去控制字符、归一
  空白、截断 64 字符）但不受 ASCII 限制，中文/emoji 均可，且永不进入任何 wire 地址。

## 11. 增补（2026-09-04）：skill 系统

- **存储**：每 host 本地 `data/skills/<name>/SKILL.md`（目录式：SKILL.md 载 frontmatter `description`
  与 markdown 正文，旁置脚本等伴随文件经 `skills` 工具的 `path` 读取；旧版扁平 `<name>.md` 仍可读，
  同名目录优先）。name 即目录名，kebab-case ≤64 字符，正文上限 32k。每 host 一份，不随房间同步（v1）。
- **注入（每次初始化读列表）**：每个 agent 构建点（TriLayer `build_orchestrator` /
  `build_clone_agent` / `_run_task` 子代理）重新读盘，把「名称+描述」清单追加到 system
  prompt——本回合保存的 skill 下一回合即对全体 Agent 可见。WebUI 已存 session 的 system
  消息保留原有内容，仅尾部托管 skills 段（去旧附新，见 `agent.run`）。
- **工具与元技能**：`skills` 工具（list/read/save）；`writing-skills` 元技能在首次访问时自动
  播种到目录，写明格式与质量标准（description 写触发条件、步骤给精确命令/路径、记录坑与验证法）。
- **安全**：save 仅限用户面 agent（本机 Agent、WebUI 编排者）；通讯 Agent 及其 spawn 只读——
  自主跨 host 代理不得在本 host 持久化 prompt 影响（与 §5/§8 的 consent 思路一致）。

## 12. 增补（2026-09-05）：托盘栈统一、房主 Token 自定义与热更新、CI/CD

- **托盘栈修订（推翻 2026-09-03 的 PyQt6 定案）**：全局 qfluentwidgets 是 PyQt5 build，
  GUI（今 `fungi/gui/` 包，2026-09-10 由 gui.py 拆包）全程 PyQt5——托盘（`fungi/tray.py`）与 CLI 房间模式（`__main__.py`）、
  selftest 一并迁到 PyQt5，**全仓库单一 Qt 绑定**。托盘菜单用 qfluentwidgets
  `SystemTrayMenu`（与 GUI 同一套 fluent 组件；右键弹出，零新增依赖）。
  CLI 托盘行为不变（左键/双击开 WebUI）；GUI 托盘点击/双击回主界面、右键弹 fluent 菜单；
  另有**单实例 IPC** 兜底：GUI 监听 QLocalServer `FungiGuiIPC`，二次启动经 QLocalSocket
  发 `show`，运行实例 `show_and_raise` 后第二实例退出（洞见 v2 同款）。
- **房主 Token 自定义**：GUI「发起房间」页 Token 行是可编辑输入项（位于发起按钮上方，
  预填自动生成值）。字符集 `[A-Za-z0-9_-]`、1-64 位——token 进 URL 与加入命令，空格/中文非法。
  发起前修改：开房即用该值；留空则自动生成。
- **Token 运行中热更新**：房间运行中改完 Token 按回车（或移开焦点，`editingFinished`）
  即改写 `hub.token`；所有 API 鉴权逐请求读它，无需重启房间。语义：旧 token 立即失效
  （403）——**已加入的好友需用新 Token 重新加入**；UI 以 InfoBar 提示。CLI `--token`
  语义不变（仅开房时定值）。
- **CI/CD**：`.github/workflows/ci.yml`（push main / PR）与 `release.yml`（tag `v*`）均以
  pytest 为门禁（windows-latest + py3.13，Qt 测试在 runner 上 importorskip 跳过）；
  release 追加 `git archive` 源码 zip + GitHub Release（generate_release_notes）。
  ruff 仍是本地门禁（`scripts/check.ps1`），暂不进 CI——版本漂移待统一后钉版。
  **2026-09-10 修订**：ruff 已进 CI（`python -m ruff check`，只 check 不 format），
  版本从 pyproject 的 dev extra 读出（`ruff==0.13.0`）；playwright 已装、浏览器用例真跑
  （不再 skip）。

## 13. 增补（2026-09-06）：文件空间全景、会话目录统一、视频理解与移动端打磨

### 文件空间全景

fs 守卫仍是白名单三分区（`public/` 自由、`homes/<host>/` 属主、其余一律拒绝），
但 `data/` 的实际布局早已不止三个目录——**每台主机一份自己的 `data/`**，server
主机的那份额外承载共享空间：

| 目录（每主机 `<repo>/data/`） | 内容 | 写者 |
|---|---|---|
| `sessions/` | 本机 WebUI 会话（每会话一个 JSON，逐回合落盘） | 各自主机——server 角色=hub store 后端；client/单机=本地落盘，**绝不代理进 hub store**（2026-09-04 真机教训：共享一个 sessions/ 会让每台主机的 WebUI 列出所有主机的聊天） |
| `comm-sessions/` | 好友视图：通讯 Agent 的会话式转录（与 sessions 同构） | room 进程 |
| `comm/`（仅 server） | Agent 间信封流量镜像，`发送方__接收方.jsonl` | hub，每次投递一条 |
| `public/`、`homes/<host>/` | Agent 文件空间（守卫白名单内） | Agent |
| `transfers/` | send_file 暂存（store-and-forward，取走即删） | hub |
| `skills/<name>/` | 每主机技能沉淀（SKILL.md + 脚本） | 仅用户面 agent；通讯 Agent 只读 |
| `mail/<host>.jsonl`（仅 server，§14/§15） | 留言邮箱（append-only，每箱 500 封滚旧） | hub |
| `todos.json`（§16） | 主人的日历待办（GUI 日历与信使共写） | 用户与信使 |

仓库根另有 `inbox/`（send_file 收件，`<来源主机>/` 子目录）与 `config.json`（模型、
展示昵称）；用户级配置在 `~/.fungi/`（`webui_token`=WebUI 门禁、
`consent_rules.json`=好友同意模式开关）。以上全部不入库。

**会话目录统一**：单机模式原本写仓库根 `sessions/`，与房间模式的 `data/sessions/`
双轨——同一台机器的聊天史裂成两处。现 `session.py SESSIONS_DIR` 统一指向
`data/sessions/`，根目录旧会话已迁移，目录删除。

### 视频理解（`video` 工具）

- 零配置发现旁置的 VidSense checkout（config 显式 > `<安装根>/Skill/VidSense` 兄弟目录
  > 桌面备用路径）；子进程跑 VidSense **原生本地管线**（`--no-api`：ffmpeg/ffprobe 抽取、
  faster-whisper 转写、CLIP 镜头切分），Fungi 读取事件卡 JSON 后自己用 ffmpeg 按时间戳
  重抽关键帧——理解交给 Fungi 自己的视觉模型，无第二 API key，VidSense 仓库零改动。
- 可靠性细节：子进程 env 注入 `HF_ENDPOINT=hf-mirror.com`（防 GFW 下 transformers
  HEAD 校验卡死）；路径含 CJK 时先复制成 ASCII 临时副本再喂管线（本机 ffmpeg 解码
  中文路径输入会失败）；相对路径按 Fungi cwd 解析；错误路径透传子进程 stderr。

### 移动端打磨（真机 X5 反馈闭环）

- **右划抽屉任意位置可起**：48px 边缘 wedge 从未在真机触发且与系统边缘手势冲突，废除；
  touchstart 无卫语句（X5 吞手势后 drag 卡 truthy 的教训）；touchmove 垂直锁隔离滚动。
- **横向滚动优先**：touchstart 命中横向可滚动元素（长工具输出 `<pre>`、agent tray）时不启动
  抽屉拖动，pan-x 交还给内容本身（touch-action 沿祖先链取交集，`#chat-wrap/#messages`
  需 `pan-x pan-y`）。
- **空输入即重试**：发送键图标随输入框状态切换（↻ 重试 / ↑ 发送 / ■ 停止）；重试走桌面
  Alt+R 同一契约（POST /retry，server 剥离失败回合的合成尾）。
- **状态栏**：tool 事件显示 `⚙ <工具名>…`，tool_result 复位 Thinking——长工具不再挂着
  陈旧的 "Writing..."。
- **停止丢卡片修复**：流未收到 done 就终结（硬中断/断网）时，`recoverAfterDrop` 先按磁盘
  reconcile 再视情重连接续，live 卡片不再在下次渲染时凭空消失（桌面 app.js 同步）。


## 14. 增补（2026-09-09）：文字留言 + 信使开关

- **amail**：通讯 Agent 工具（`host/subject/body`），发 `type="mail"` envelope；hub.send 对 mail
  直接落 `data/mail/<host>.jsonl`（server 权威、append-only、每箱 500 封滚旧），**不进 relay**——
  收件两端零 agent 参与，离线容忍。WebUI（桌面+手机）「邮件」入口轮询 `GET /mail`（WebUI runtime
  端点，runtime 解析本机主机名）出未读红点，模态框阅读，`POST /mail/read` 标记已读。
- **信使（courier）**：config `courier: bool = True`，GUI 设置页开关，每个信封重读（免重启）。
  - 开（默认）：现状——本机 通讯 Agent 醒来转述对面留言、跑 receive_transfer 推卡。
  - 关：`Clone.on_direct` 钩子（chat/transfer 在入队前询问）→ room `_courier_direct`：
    chat 直接落会话视图 transcript（署名 `[<addr>]`）；transfer 由 room 合成同 id ask 进卡片
    管线，用户答 yes 后 room 直接 `download_transfer` 落盘并回 `{ok,saved}`——收方 agent 全程不醒。
- **前端 common.js**：fetch 封装（`initHttp` 支持 prefix/onUnauthorized）+ 工具卡片/consent 卡/
  确认弹窗/邮件 UI 抽到 `window.FungiCommon`，app.js/m.js 只留壳；server.py 静态白名单加 `/common.js`。
  注意 `[hidden]` 属性会被 CSS `display:flex` 覆盖——mail 列表/详情面板必须显式 `[hidden]{display:none}`。
- **好友视图可写（人类直发）**：`POST /comm-send {host, text|file}` → `RoomBase.comm_send_human`——
  envelope（chat/transfer，`body.from_human=true` + `sender_name`）从本机 通讯 Agent 地址直投
  `peer:comm-<host>`，**不经本机信使**；收端语义由对面的信使开关决定：开 → `Clone.render_input`
  署「来自 ○○ 的用户」走正常转述轮；关 → `_courier_direct` 直落对面 transcript/consent 卡
  （transfer 卡问句署「来自 ○○ 的用户」）。发送侧 transcript 以 `sender:"human", mine:true`
  追加自己的消息；两侧 transcript 写入均持 per-sid 锁（`RoomBase._comm_lock`，覆盖 turn end、
  courier-off 直写、卡片 verdict 三个线程）。人类发的文件发送侧免确认，接收侧确认管线不变（卡片注明落盘位置）。投递成功后 hub 暂存副本经
  `DELETE /api/transfer`（`Transfers.discard_for`，仅收件方可删）清除，`data/transfers/` 只承担中转暂存。

## 15. 增补（2026-09-10）：文字消息统一入留言邮箱 + 好友视图排序修复

- **统一存储**：好友视图人类直发文字不再写 comm transcript（SessionStore），而是发
  `type="mail"` envelope（`from: <host>:human`）——hub `deliver_pair` 同时落收发两端邮箱：
  收件方未读、发件方已读（`mine: true`），记录新增 `peer`（对话对面主机）。通讯 Agent 的
  留言工具同路径受益。协议 `parse_addr` 补 `mail` 角色——**修复潜在 bug：真实 hub 链路上
  `amail` 信封此前会被 `parse_addr("host:mail")` 拒绝**（旧测试全走 FakeTransport 未暴露）。
- **信使语义收敛**：文字/邮件投递与信使开关解耦（永远直达、零 agent）；信使只管 Agent 对话
  转述与 consent 卡片。`_courier_direct` 的 from_human chat 分支随之删除。
- **好友视图渲染序修复**：此前 transcript → events 两段拼接，事件行永远压在最新消息下面
  （人类消息"不在最下面"的根因）。现在 `/comm-log` 载荷增加 `mails`（按 `peer` 过滤、ts 排序），
  桌面/手机统一渲染 transcript → events → mails，人类消息恒在最底。
- **未读入口收敛（2026-09-10 二次修订）**：✉ 统一收件箱入口废弃；未读数改为好友列表逐人徽标
  （common.js `initMailUnread` 轮询 /mail 按 peer 计数），点开好友视图即整线标读
  （前端逐封 POST /mail/read）。人类发送者标签与代码块对比度提高。

## 16. 增补（2026-09-10）：信使的记忆、日历与 todo 工具

- **信使概念收窄**：信使只指「代收代复留言」的消息信使。文件传输从不经过 LLM
  （transfer 信封 → consent 卡片 → inbox/<src>/，与 courier 开关无关），不再是信使的一部分。
- **长期记忆**：`config.courier_memory`（GUI 信使页编辑，即时写盘）。通讯 Agent 每轮
  chat turn 重建 system prompt 时重读注入——`Clone.system_prompt` 支持 callable，
  `resolved_prompt()` 每轮求值（与 courier 开关同款 live 语义）。
- **四周日历待办**：`fungi/todos.py`，存储 `data/todos.json`（date → [items]，gitignore）。
  注入窗口 = overdue 30 天尾 + 今天 + 未来 21 天，按日排列随 prompt 注入。
- **todo 工具**：action add/list/remove，同一份存储。挂载三处：L1 Orchestrator
  （trilayer，parallel_tools 含 todo）、**本机 Agent 的 clone 工具集**——房间模式 WebUI
  回合走 `RoomRuntime.build_agent`（由 clone.tools 装配），不走 build_orchestrator，
  漏挂本地 clone 会导致 WebUI 侧看不到该工具（真机教训）——以及信使 comm clone。
  信使与用户 thus 共写一份日历。
- **日历条目属于用户（2026-09-10 真机教训）**：信使把主人 9/11 的约「remove 后 add 改写」
  了一遍——对主人就是一次静默取消，而当时的本意只是「这条之外再加一条」。现在 `add`
  只追加（同条不重复），`remove` 必须给出确切条目文本（整日清空只留在 GUI 日历）；
  `todos.RULES` 随上述三处 prompt 注入，写明**新信息另加一条**而非改写旧条，落定的约要
  主动记（含钟点）。
- **GUI 信使页**：长期记忆编辑 + 4 周圆形日历（点日期弹窗录入，一行一条）；
  今天 accent 实心、有待办淡橙底色。信使开关仍在设置页。

## 17. 增补（2026-09-10）：输入框回车即更新

用户定调：凡是「要点按钮才提交」的输入框，键盘上按回车就该等效提交（按钮保留，不是替换）。
- **单行 `LineEdit` → `returnPressed`**（不用 `editingFinished`：移开焦点也提交会变成
  「一切换焦点就开房／加入」）。发起房间页 主机名/昵称 → `_start`；加入房间页四个框
  （IP/Token/昵称/主机名）→ `_join`；设置页三个框 → `_save`（与按钮同语义：留空的框不覆盖
  已存配置，保存后清空三格）。`JoinPage._join` 入口新增 `not join_btn.isEnabled()` 早退——
  禁用态就是「扫描进行中」，否则第二次回车会再开一个发现线程并二次 emit `join_done`。
- **多行 `TextEdit` → `Ctrl+Enter`**：回车必须留给换行。信使页长期记忆 →
  `_save_courier_memory`；日历录入弹窗 → `accept`。`QShortcut("Ctrl+Return")` 挂在输入框上、
  `WidgetWithChildrenShortcut` 上下文——只在该框聚焦时生效，不做窗口级劫持（HostPage 的
  Ctrl+C 是窗口级旧例，新加的一律不跟）。保存按钮 tooltip 注明该键位。
- **已有同款先例**：发起房间页 Token 的 `editingFinished`（§12）——单行框回车即热更新。
- **开房/加入之后，那几格仍能「回车即更新」（2026-09-10 用户二次点名）**：`HostPage._apply_identity`
  （主机名/昵称；Token 早有热更）与 `JoinPage._enter`（加入页四格）在房间运行中提交**能改的那部分**：
  昵称 → `RoomBase.set_display()`（服务端直接 `hub.join` 刷 roster；客户端 `client.display` + re-join，
  `roster.join` 本就刷新 display，所以对面 5s 轮询 `/peers` 立刻看到新名字），Token → `RoomClient.set_token()`
  （赋值 + 心跳校验，失败还原）——房主换了 Token 后加入方靠它续上，否则请求全线 403。
  **wire 名与房主 IP 拒绝并还原字段**（地址、roster key、`data/` 文件名、对面 comm clone 都以它们为准，
  换它们等于换房间/换身份），InfoBar 说明原因，不静默失败。
  坑：`join_btn` 加入成功后**一直保持禁用**（职责已交给「离开房间」），所以实时提交的守卫只能看
  `room is None`，不能看按钮状态——否则整条实时路径静默失效（真机探针当场抓到）。

## 18. 增补（2026-09-10）：好友视图一张时间轴（把乱序根治掉）

现场两句：**「好友对话跑完一轮就没了」**（已由 §15 的 `#messages` 归属修复，见 `docs/webui-ux.md`
前端坑）与**「thinking 和工具调用的卡片没有按时间顺序渲染」**。后者是结构问题，不是渲染 bug：

- **根因一（串接）**：`renderFriendChat` 把三份各自有序的列表**接在一起**（transcript 列表 →
  hub 事件按 ts → mail 按 ts），而 ask 卡要靠「按存储顺序 shift」猜位置，猜不中就 append 到末尾。
  实测（真转录 17 条消息 / 3 条 ask）：两条 ask 被甩到最底部，而它们其实比画面里所有消息都早。
- **根因二（数据没有排序信息）**：`asks` 记录只有 `{id, questions, answers, status}`——既不知道
  是哪次工具调用触发的，也没有时间戳；转录消息同样没有时间戳。没有可排序的量。
- **改法**：`make_ask_tool` 改 `with_call_id=True`，记录带 `call_id` + `ts`；卡片 ask
  （`_record_card_verdict`）带 `ts`；comm 转录的消息落盘时由 `merge_comm_history` 给**新到**的行打
  `ts`（已有行保留原 ts）。前端 `renderTranscript`/`renderFriendChat` 合成**一张时间轴**：ask 按
  `call_id` 精确锚在它的工具调用处（旧记录退化为「问题原文与调用参数逐字相等」的精确匹配，再退化
  到存储顺序）；带 `ts` 的事件/邮件/卡片 ask 用 `insertByTs` 插到第一个更晚的兄弟节点前；没有 `ts`
  的行保持到达顺序（旧转录整块在前）。
- **旧数据**：2026-09-10 之前的 ask 记录没有 `ts`/`call_id`，无法回溯定位——它们的工具调用已不在
  转录里，属于比画面更早的回合，因此**前置**到最前（不再甩末尾）；旧转录消息没有 `ts`，与事件/邮件
  的合并从新数据开始生效。
- **顺带修数据丢失**：chat 分支原先 `msgs = list(messages)` 直接覆盖；clone 被 roster 回收后重建时
  历史为空，下一轮就把整段转录覆盖掉（磁盘一起丢）。现由 `merge_comm_history` 做并集：clone 仍带着
  的行沿用存储副本（保 ts），被遗忘的行前置携带，新增行追加；任务分支本就是追加。回归测试：
  `test_chat_turn_after_a_clone_rebuild_keeps_the_earlier_transcript`、
  `test_chat_turn_with_cumulative_history_does_not_duplicate`、`test_ask_record_carries_its_tool_call_id`。

## 19. 增补（2026-09-10 深夜）：消息时间标签、好友视图分侧、设置页预填、转录合并的身份判据

同一夜的四件现场事（用户报告为准）+ 一件事故善后：

- **每条消息悬停显示发送时间**（用户要求）：`common.js::whenLabel(ts)` 把 ts 说成人话——今天/昨天/前天，
  七天内用星期几，再远用 `YYYY-MM-DD`，时间一律 24 小时制。`markTs()` 顺手写进 `data-when`；
  `style.css`/`m.css` 用 `::after{content:attr(data-when)}`。**位置**：绝对定位在卡片**下方**、贴**发送方**
  那一侧（我方靠右、对面靠左），默认 `display:none`，`:hover` 才 `block`——不参与布局，行高与滚动高度
  都不变（第一版把时间写在气泡里，一悬停就把行撑高、整页跟着跳，用户当场退回）。伪元素不进
  `textContent`，行内容探测不受影响。会话转录原先没有逐行时间：
  `public_messages()` 在持久化边界给**首次落盘**的行打 `ts`（就地写，回合开始的那次 save 定住 user 行、
  结束那次 save 定住本轮新行——否则会把老行重打成回合结束时刻）。`ts` 是存储字段，不是协议字段：
  `llm._wire_messages()` 在出网前剥掉，避免凭空给 provider 造一个字段。
- **好友视图分侧**：我方（信使）的**思考 `<details>`、工具卡、错误行**原先是 `align-self:flex-start`，
  而我方正文靠右——同一轮的东西被拆到两边（用户：「因为这是我方的」）。共享渲染器把 `opts.side.agent`
  一并加到 reasoning/tool/error 行（含 live tape 的 reasoning/tool/result）。桌面此前只给我方正文加了
  `friend-mine`；手机端干脆**没有侧**，对面的行还落进 `.msg.user` 的强调色气泡里（看着像"我说的"）。
  现两端同一套 `side` 类：对面在左（素底），我方在右。移动端会话视图不受影响。
- **好友视图的实时思考自动展开**（2026-09-11 用户报告：「只显示一个 Thinking，点进去才展开；
  本机会话是流式时自动展开、思考结束收起」）：本机会话的实时渲染器 `app.js::renderTurnLive` 一直在
  `<details>` 上写 `det.open = !closed`，而好友视图的实时磁带走共享的 `common.js::renderLiveEvents`，
  它建 `<details>` 时**根本没设 `open`**——于是永远收起（`56a8e5a` 引入好友视图旁观时就没这一步）。
  现在共享渲染器同样按 `reasoning_end` 记 `closed`：正在写的那个 `open`，写完了收起，一轮里多个思考块
  各按自己的结束事件算（与 `renderTurnLive` 的语义逐条一致）。转录里的旧思考两边都仍是收起——只动实时。
  回归：`test_webui_friend.py::test_friend_live_thinking_opens_while_it_streams`（去掉修复即超时失败）。
- **设置页显示当前配置**（用户要求）：三个框不再是空的——接口地址/模型直接预填（可编辑），API Key
  只把掩码 `sk-12…a5f4` 放进**占位符**（真 key 依旧不上屏，空框 = 保持不变的老语义不变）；保存后
  `_load_fields()` 让框回到当前值（不是清空），进页(`showEvent`)也刷新一次，命令行/WebUI 改过模型能立刻看到。
- **转录合并按"身份"对齐，不按整字典**：`_comparable()` 原先把除 `ts` 外的整个字典拿来比。clone 的
  history 副本与存储副本并不逐字相同（`run_turn` 补写的 assistant 行不带 agent 那份 `reasoning`），
  于是一模一样的行被当新行 → 对齐错位 → 走了"clone 忘了这些行"的携带分支 → **信使的上一条回复在
  最新对话里出现两次，还被顶到它自己的提问前面**。现在只比 (role, content, tool_call_id, 工具调用名)，
  匹配上的行沿用存储副本（连带 reasoning 与 ts）。复现：对面一次发两条 + 信使两条都回 → 浏览器里
  DOM 出现两行相同 assistant；修后 0 重复。回归：`test_comm_history_merge_keeps_a_reply_with_its_question`。
- **事故善后（拆包写坏用户配置）**：`fungi/gui/` 拆包后页面改读 `fungi.config`，而 GUI 测试还打在
  `fungi.gui.load_config/save_config` 门面上，patch 失效 → 真 `save_config` 被调用，把测试串写进了用户
  的长期记忆（`courier_memory`）与 key。已从 `data/comm-sessions/comm-pc.json` 的 system prompt 与用户
  自己留存的配置副本取回旧值并写回。**结构性防复发**：`tests/conftest.py::_never_write_the_user_config`
  把 `config.CONFIG_PATH` 指向 tmp 副本（放在子目录里，避免被把 tmp_path 当数据目录的用例 glob 到），
  从此任何测试都不可能碰到仓库里的 `config.json`。

## 20. 增补（2026-09-10 深夜）：信使的双通道（汇报 ≠ 发给对面）+ 二维码依赖自愈

### 现场（对方机器上的 `comm-OwO.json`，22 行）
主人一句「你明天早上有空吗？」，两边信使**互相确认了 4 轮 8 行**，正文一轮比一轮长
（49→329→525 / 199→704→553 字），最后靠模型自己 `<<SILENT>>` 才停下（那条 3245 字 reasoning
全是在说服自己"别回了"）。根因不是渲染，而是**回合收尾文本的身份混了**：
`clone/comm.py::_chat_end` 的兜底把"没有调用 send_peer 的收尾文本"直接投给对面，
而模型把它写成了**给自家主人的汇报**（「**你**反问『啥远程控制？』」「另外有一点我想**单独跟你讲明白**」），
于是私密汇报出网、对面逐条作答，两边互相放大。同一份转录里还暴露出：`public/` 里一份无意翻到的
文档被当成"上次的活"，信使据此用 `inquire` 推给主人一道虚构选择题。

### 新契约（用户指令，2026-09-10）
- **`send_peer` 是唯一上网通道**：回合收尾文本＝给自家主人的汇报，只留在本机。实现上
  `Clone.run_turn` 的 chat 分支只把文本记进历史，`on_chat_end` 钩子与 `CommTools.peer_sends`
  一并删除（clean cutover，不留死插口）。
- 汇报要说清"我发了什么、对面回了什么、有什么要主人定"；不想汇报就裸 `<<SILENT>>`
  （存储侧 `_drop_silent` 仍会剥掉它，只留下 reasoning）。
- prompt 另加两条约束：**别翻与本轮无关的文件**（不许把无意翻到的文档当成任务依据；
  查不出来的就照实说）；**`inquire` 不得用来确认自己编出来的计划**。
- 好友视图把这类行标成 **信使汇报**：`renderTranscript` 在 `opts.report` 下给 assistant 行加
  `.report`，`style.css`/`m.css` 用 `::before{content:"信使汇报"}` 渲染（不进 `textContent`，
  行文本语义不变；会话视图不带这个类）。

### 二维码依赖（用户报告「缺少依赖 segno」）
`MobilePage.refresh()` 原先在 `ImportError` 分支**提前 return**：连"手机端地址"都不填，
页面等于废掉。现在**先算地址**（手机手输也能进，光标停在开头而不是滚到 token 尾巴）、提示可换行，
缺依赖时给**一键安装**（`python -m pip install segno`，独立控制台 + 1s 轮询，装完自动重绘；
`sys.frozen` 的打包版不给按钮、提示更新）。`release.yml` 加 `--collect-all segno`：
发布包不再可能缺这个纯 Python 小依赖。

## 21. 增补（2026-09-11）：GUI 的"记忆"不再被测试改坏；发起房间页也会记

现场：「为啥我每次点进加入房间就是新昵称和 tok_new？我记得有持久化的。」——记忆确实存在
（`HKCU\Software\Offblink\FungiGUI` 的 `last_token` / `last_nick` / `last_ip`），但被两类东西毁掉：

- **测试写进了用户的真 QSettings**：`tests/test_gui.py` 的 join 用例把自己的夹具值落盘（`tok-new`、
  `新昵称`、`pc-alpha`、`192.168.1.20` —— 注册表里逮到的就是这些），跑完再 `remove()` 那三个 key
  「别漏进用户设置」。**每跑一次全量测试，用户的 token/昵称/IP 就被删一次**。
  修法：`tests/conftest.py::_hermetic_gui_settings`（session 级）把两个页面指到 test-only 的 app 名
  （`FungiGUI-test`），跑完 clear；手工 remove 全部删掉（同 config.json 事故，同一类结构性防线）。
  注意：`QSettings.setDefaultFormat(IniFormat)` 在 Windows 上**不影响** `QSettings(org, app)`（实测仍走注册表），
  所以走"换 app 名"而不是"换格式"。
- **发起房间页根本没有持久化**：token 每次构造都 `secrets.token_urlsafe(12)` 现生成、`_leave()` 又换一个，
  昵称/主机名也不记。现在三者都从 QSettings 还原（`last_token` / `last_nick` / `last_host_name`），
  开房、热更新 token、实时改名时写回；**离开房间不再换 token**（下一个房间继续用，好友不必重输）。

顺带修了两条被午夜打翻的时间标签用例（正午锚点 + 星期几由时间戳推导，别写死）。

## 22. 增补（2026-09-11）：信使的第三件正事——替两边把「约」定下来

用户指令：**需要约会（广义）时，信使要帮助人类双方确定时间、地点和事件。** 广义＝见面、通话、
吃饭、拜访，任何需要定下来的计划。`COMM_SYSTEM_PROMPT`（`fungi/clone/comm.py`）新增一条：
钉死三件让计划成真的东西——**事件、时间、地点**；本机主人不知道的向对面信使要，给具体选项
而不是来回「你什么时候方便」；没有任何一方点头的细节不算数；落定的计划带钟点写进主人的日历
（`todo` 工具），只把真正该由主人回答的问题交给主人（沿用 §20 的 `inquire` 约束）。

与既有约束的关系：这条**不是**给信使新开一个聊天理由——`send_peer` 仍只在回复有必要时调用、
「别为确认而回复」照旧；它管的是「计划该被推着走完」，不是「多说两句」。GUI 帮助页（`fungi/gui/help.py`
的「信使」一条）同步补上这半句；README 场景①正是这类对话，故未改动。

## 23. 增补（2026-09-11）：好友视图的顺序——信使的转录被自己写坏的那次

现场：「好友视图的顺序依旧会乱，刷新也不行，整体呈现对话沉底、工具和思考上浮」。
渲染没错，**`merge_comm_history` 写坏的是文件本身**。

- 克隆的 history **不是转录的逐行镜像**：它只有对话（对面的行 + 信使的汇报文本），而转录里还有
  这一轮 Agent 自己写下的行——reasoning、tool_calls、tool 结果（`Agent.run` 就地往 `messages`
  里追加）。旧实现**按下标逐对比较** `carried_c[si] == stored_c[si]`：走到第一个「只有 store 有」
  的工具行就再也对不上，于是判定成「克隆把历史全忘了」，走 `keep + carried` 兜底——
  **把整段对话连同新的 `ts` 重新追加到末尾**。结果是 store 里已有的工具/思考行原地不动留在上面，
  全部对话（对面的消息、信使的汇报）沉到下面；每次 chat 回合都重演一遍，刷新也只是重画同一个文件。
  证据：用户机器上那份 `data/comm-sessions/comm-pc.json` 正是这个形状——前 18 行全是 rich 行（4 个
  回合的 reasoning/tool），后 12 行是**整段 lean 对话**（对面消息 + 汇报）且 `ts` 全等于最后一次
  merge 的时刻。
- **新语义**：store 是基底，只增不改。逐行在 store 里**向后找**（`while stored_c[j] != comp: j += 1`），
  跨过只有 store 才有的行；每个 store 行最多被认领一次；只有**最后一个被认领的 carried 行之后**
  的行才算新行，追加到末尾并盖 `ts`。store 丢了的行（history 被裁剪）不再被重新追加。
- 回归：`tests/test_room.py::test_comm_history_merge_keeps_the_tool_rows_above_the_dialogue_they_turn_belonged_to`
  （旧实现下第一个元素就是 `('assistant', None)`——工具行被抬到对话之前；新实现顺序不变）。
  既有三条 merge 用例（丢标记、跨回合回复、克隆遗忘）语义不变，全绿。
- **已写坏的文件不会自愈**：那份转录的顺序是坏 merge 烙进文件的，新代码只保证此后不再写坏。
  要修复旧转录需按其真实时间重排（需 pc 那台的 `data/comm/OwO__pc.jsonl` 镜像给出对面消息的真实 ts）。

## 24. 增补（2026-09-11）：信使能改自己写错的日历；汇报卡片上的反馈框

### 日历：`update`（用户可以删/改，别人不许乱动）
用户裁决：「之前我强调不要乱删日程，指的是不要无缘无故删除。但如果他自己写错了，还是可以修改的。」
- `todo` 工具新增 **`update`**（`date` + `old` → `text`）：**就地替换**一天里的一条，保持位置；
  `old` 不在那天就报 `no such item`（绝不凭空造条目），新文本与既有条目重名时只留一条。
- `todos.RULES` 随三处 prompt 注入，写明：**改自己的错是正当理由**（钟点/地点/措辞写错），
  用 `update` 而不是 `remove` + 重加（后者对用户就是一次静默取消，那是 2026-09-10 的教训）；
  **用户自己写下的条目不许动**，`remove` 仍需有理由相信用户想让它消失。
- 回归：`tests/test_todos.py::test_update_fixes_an_entry_in_place`。

### 反馈框：每条汇报卡片下面，主人 ↔ 自家信使
用户要求：「为了纠正信使偶尔会犯的小错误，每条信使汇报卡片上都提供一个反馈输入框。
提交的内容与对面没有关系，只是主人与信使之间的对话。」
- **每条 `.msg.assistant.report`（好友视图的我方汇报行）下带一个 `.report-feedback`**：
  一行输入 + 发送，`common.js::attachReportFeedback` 构建（桌面 app.js 与手机 m.js 共用，
  由 `opts.feedbackHost` 触发，两个客户端都传 `friendView`）。
- **只在本地唤醒自家信使**：`POST /comm-note {host, text}` → `RoomBase.comm_note_human` →
  `Clone.note()` 把一个 `from_owner` 的 chat 信封**直接塞进自己的 worker 队列**，
  从不过 `transport`：对面既收不到信封，hub 的 comm 镜像里也没有行（回归直接断言
  `commlog.read() == []` 且对面 agent 不醒）。渲染成 `[主人的反馈] …` 一行，走 chat 回合
  的全部好处：进 history、进转录（刷新后还在）、收尾文本仍是给主人的汇报。
- **与对面的关系**：`COMM_SYSTEM_PROMPT` 写明这是主人私下对你说的话——**不许转达对面、
  不许在对面的对话里提起**，只当纠错依据。
- **框里允许为空**：空提交**不发请求**（前端直接早退，不空转一次 LLM 回合），也不报错。
  服务端对空文本返回 `error: empty feedback`（防御性，正常 UI 到不了）。
- **成功与否看响应体**：`FC.postJSON` 返回的是 `Response`（既有调用都是"发了不管"），
  这里必须 `await res.json()` 再看 `ok`——HTTP 200 里带 `{"error": …}` 不能读成成功
  （实测踩到过：信使还没就绪时界面谎报"已发给信使"）。
- 回归：`tests/test_friend_send.py::test_courier_feedback_wakes_our_courier_and_never_the_peer`、
  `test_an_empty_note_never_wakes_the_courier`；
  `tests/test_webui_friend.py::test_every_report_row_offers_feedback_for_our_courier_only`
  （真浏览器：空框不发、填了才发、转录里出现主人的行）。

## 25. 增补（2026-09-11）：信使不再被提问卡住；来信的铃声与闪动；发文件的进度条

### 25.1 信使的 `inquire` 不再阻塞（否则它会替对面闭嘴 30 分钟）

用户提问：「目前的 inquire 是阻塞的吗？如果是的话，一旦用户没有及时回复 inquire，那信使是不是
就卡死不动，对面来信也不动了？这可不行。」

- **事实确认**：是阻塞的。`CommTools.inquire` → `_blocking_ask` → `PendingAsks.wait(timeout_s=1800)`，
  而一个 clone **只有一条 worker 线程**（`Clone._work` 队列 + `_work_loop`）：问题没人答，工人的这一轮
  就停在 `Event.wait` 上，最长 30 分钟。期间轮询线程照旧收信，但**对面每一条 chat 都排在队列里不动**
  （它还会连带卡住 `send_peer`，因为网络通道也长在同一个 worker 上）。
- **新语义**：信使的提问只发信封、立刻返回（工具结果 `ASKED (not blocking)…`），卡片照旧等人。
  主人答复后 `RoomBase._send_answer` **把 `questions` 一起带回**（同意类裁决没有 `questions`，只有
  `question`），`Clone.dispatch` 发现**没人在 wait**（`pending.resolve()` 返回 False）时走
  `Clone._answer_turn`：把答复变成**一轮新的 chat**，渲染成 `[主人的答复] 问: … 答: …`。
  问题必须跟着答复走——那一轮的 tool 调用不在 clone 的 history 里（history 只存对话文本），
  只给一句「周六有空」信使不知道在答什么。
- **边界**：`confirm` / `send_file` / `receive_transfer` 的等待**保持阻塞**。它们的返回值就是裁决本身，
  工具要在这一轮里拿到答案才能继续；`_answer_turn` 只认 `questions`，所以迟到的同意裁决**不会**
  被误当成一轮对话（回归在 `test_friend_send.py::test_a_consent_verdict_stays_a_bare_value`）。
- **为什么不是复用 `background`**：`TriLayer.build_clone_agent` 的 docstring 已经写明
  「a clone has no re-activation channel … its background reports had nowhere to land」，comm clone 走
  `subagents=False`（连 spawn/background 都没有）；而 background 的复活链路是 **WebUI 会话级**的
  （`server.py::_PENDING_SPAWNS` + 浏览器 `/spawn-pending` → `/resume`），信使既没有会话 id 也没有浏览器。
  这一次补的正是 clone 侧的复活通道——**答复信封自己就是唤醒**，与上一轮的「反馈框」（`Clone.note`）
  同一条路：不过 transport、直接进 worker 队列。
- prompt 与 schema 同步（`COMM_SYSTEM_PROMPT` 的 inquire 一条 + `_SCHEMA_INQUIRE`）：
  「不阻塞、答复晚些作为 `[主人的答复]` 到达、别等、别再问第二遍」。
- 回归：`tests/test_comm_clone.py::test_inquire_never_blocks_the_courier_and_the_answer_arrives_as_a_turn`
  （一轮提问未答 → 这一轮照常收尾 → 期间对面的 chat 被照常服务并回话 → 答复到达后再起一轮，
  且带问题原文）、`test_an_answer_without_questions_is_not_a_turn`。

### 25.2 来信提醒：托盘图标闪动 + 铃声（可关）

用户要求：「对于来信，当用户不在好友视图时（和显示未读一个判断条件），需要响铃和托盘图标闪动。」

- **条件与未读徽标同源**：房间自己的邮箱轮询把 `unread` 记在 `RoomBase.last_unread`
  （`_mail_watch_loop` / `_mail_poll_once`），GUI 每秒读它——不额外打一份到 hub。
  WebUI 里「点开好友视图即整线标已读」，那正是停铃的开关（与 `initMailUnread` 的 `byPeer` 同一个字段）。
- **宽限期 10 秒**（`fungi/gui/app.py::RING_GRACE_S`）：好友视图要 ~8 秒才把这一条标成已读
  （5s `/comm-log` 轮询 + 3s 邮箱轮询），立刻响铃就会为「你正看着的那条」响。闪动**不等**宽限
  ——它就是未读提示本身，误报的代价只是一枚图标。
- **铃声资产**：`assets/ringtones/*.wav`（7 个：叮咚/风铃/蜂鸣/警示/通知/钢琴/合成器），
  由 `scripts/make_ringtones.py` 按用户 Get It 应用的合成配方生成（44.1kHz 单声道，仓库里不带 numpy）。
  播放是 QtMultimedia 的 `QSoundEffect`（**一声**，见 §26.2；第十二轮起不再是循环）；缺多媒体插件退
  `winsound`；都没有就静音——没有声卡不能拖垮 GUI。exe 打包加了 `--add-data "assets;assets"`。
- **设置页「来信提醒」**：铃声开关（默认开，写 `config.ring`）+ 铃声选择下拉（换一个即保存并试听一次）
  + 「试听」按钮（听当前选中的那一首，见 §26.2）。
  **关掉不显示铃声选择**（用户明确要求），未读的图标闪动照旧。
- **托盘**：未读时图标在两版之间闪（`tray.make_icon(badge=True)` 的红点版），未读清零即停。
  （当时菜单里还有一项「停止铃声」，同一天就按用户要求撤掉了，见 §28。）
- 回归：`tests/test_gui.py`（宽限期、关铃仍闪、开关收起下拉；第十二轮另加三条，见 §26.2）
  + `test_tray_icon_flashes_while_mail_is_unread`。

### 25.3 发文件的进度条（模态，完成自动关闭；手机两步）

用户要求：「对于文件传输，我希望添加进度条。进度条本身是模态框（风格一致），进度完成后自动关闭。
手机端的上传文件需要多一步——首先上传到服务器端，再从服务器上传到目标主机。对于不同的进行步骤，
进度条应进行说明。」

- **字节只在服务器上流动**（页面看不到它们），所以进度只能由服务器数：**浏览器自己铸一个 job id**
  → `POST /comm-send {host, file, job}` → `GET /transfer-progress?id=` 轮询 →
  `fungi/xfer.py::TransferJobs`（`running` / `done` / `error`，跑完 300 秒后清）。
  不带 `job` 的调用（测试、命令行、旧页面）**行为与返回体完全不变**（`{ok, kind, name}`）。
- **进度钩子**：`upload_transfer(path, name, to_host, progress=)` 从 room 穿到
  `HubClient`（256 KiB 一块地数）与 `LocalTransport`（`read` 闭包，同一根进度条）。
- **手机多一跳，就多一条进度**：① 浏览器 → 电脑（XHR 的 `upload.onprogress`，只有客户端能测）
  ② 电脑 → 对方（服务器的 job）。手机聊天框的文件上传也走同一个模态（单步）。
  桌面发文件是单步——文件本来就在这台电脑上。
- **模态**（`web/common.js::initTransfer`，桌面与手机共用；`#xfer-overlay` + `.xf-step`）：
  每一跳一行「标签 + 进度条 + 字节/百分比说明」，完成时全行 100% 并标绿，等 900ms 自动关闭；
  失败则把原因写在那一行并**留在屏幕上**（关掉它不会取消传输：按钮只是收起卡片）。
  job id 存在 `#xfer-overlay.dataset.job` 上（控制台与浏览器测试的唯一把手）。
- 回归：`tests/test_webui_transfer.py`（真浏览器 4 条：条与说明的渲染、桌面上传全流程并落在 hub、
  手机两跳并把文件落进 inbox、失败留在屏幕上）+ `tests/test_friend_send.py` 的 job 状态两条。

## 26. 增补（2026-09-11）：铃声只响一次且能试听；汇报的「评价」框；点托盘图标进 WebUI

用户原话：「铃声只响一次，但是图标保持闪动（即不变）。选中的铃声也要可以试听（现在不行）。
将反馈输入框里面的提示改为"评价一下"，"主人的反馈"改成"评价"（太尬了）」，
外加「点击图标跳转webUI（现在是启动器）」。

### 26.1 铃声一声：`Ringer` 从循环改成一次性播放

- `fungi/gui/ring.py::Ringer.start()` 过去用 `QSoundEffect.Infinite`（`LOOP_FOREVER`）/ winsound 的
  `SND_LOOP` 循环，一直响到被读掉。现在两个后端都只播一次（loop count 1 / 不带 `SND_LOOP`），
  `LOOP_FOREVER` 随之删除；`preview()` 与来信铃从此是同一种播放。
- **`ringing` 的语义是本节要害**：它从 `start()` 到 `stop()` 一直是 True，**不是**「此刻有声音」。
  `app.py::_poll_unread` 每秒调 `_start_ring`，拿它做幂等判据（`if self._ringer.ringing: return`）；
  若它在 WAV 放完就变回 False，同一首会被每秒重播一遍。
- **图标不受牵连**：闪动由 `RoomBase.last_unread` 驱动（`_poll_unread` → `_Tray.set_alert`），
  与铃声各自独立——铃声停了图标照闪，直到那条被读掉。
- **托盘不再有铃声项**：一声就完，没有可停的东西——同一天就按用户要求撤掉了「停止铃声」（§28）。
- 回归：`test_a_tone_asks_both_backends_for_a_single_play`（Qt 的 loop count == 1、winsound 的 flags
  不含 `SND_LOOP`；改回循环即红）、`test_the_unread_poll_rings_once_not_once_per_second`
  （五次轮询只发一次播放请求）。

### 26.2 选中的铃声也能试听：铃声那一行多了「试听」按钮

- 过去只接了 `tone_combo.currentIndexChanged`——**换到别的项才响**，想听当前那一首没有入口
  （这正是用户说的「现在不行」）。
- `ConfigPage.preview_btn = PushButton("试听")`，`_row("铃声选择", self.tone_combo, self.preview_btn)`
  放在行尾（`_row` 的第三个参数就是行尾控件）；`clicked` → `_preview_selected()` →
  `_preview_tone(currentIndex())`：听当前音色并顺手写盘。槽不带参数——`clicked` 传的是 checked(bool)，
  不是索引。
- 不引新依赖：复用 `ring.Ringer.preview()`（懒建的 `self._preview`）。
- 回归：`test_the_audition_button_plays_the_tone_that_is_already_selected`（不动下拉框、点按钮，
  试听记录里就是当前音色，且配置一致）。

### 26.3 汇报框的措辞：`[主人的反馈]` → `[评价]`，placeholder → 「评价一下」

- **行内标签**：`fungi/clone/base.py::render_input` 的 `from_owner` 分支渲染成 `[评价] {text}`。
- **prompt 必须同字面量**：`fungi/clone/comm.py::COMM_SYSTEM_PROMPT` 里那条同步改写
  （「it reaches you as a `[评价]` message … from the 评价 box under the report」）——
  信使靠这个标记认人，代码与 prompt 差一个字它就认不出这是主人的话。
- **输入框提示**：`web/common.js::attachReportFeedback` 的 `placeholder` 改成「评价一下」。
- §24 是第十轮的历史记录（含用户当时原话），**不改写**；口径以本节为准（2026-09-11 起）。
- 回归（硬比对字面量，已同步）：
  `tests/test_friend_send.py::test_courier_feedback_wakes_our_courier_and_never_the_peer`、
  `tests/test_webui_friend.py::test_every_report_row_offers_feedback_for_our_courier_only`、
  `test_the_mobile_friend_view_offers_feedback_too`。
- 帮助页「信使」条目与 `docs/README-详细版.docx` 同步为「评价框」（`README.md` 不动——用户明令）。

### 26.4 点托盘图标 → 进 WebUI（原来是唤起启动器）

- `fungi/gui/trayicon.py::_Tray._on_activated` 的 `Trigger` / `DoubleClick` 从 `show_and_raise()`
  改为 `open_webui_from_tray()`（`FungiGui` 上那个 = `rooms[0].open_webui()`）。
- **启动器仍进得去**：菜单里的「显示主界面」保持 `show_and_raise`——用户说的只是「点图标」。
- 房间模式的托盘（`fungi/tray.py::TrayController`）本来就是「点击 → 开 WebUI」，未动
  （`tests/test_tray.py` 钉着）。
- 与未读的配合：响铃时点图标正好直接开好友视图，读掉即停闪。
- 回归：`tests/test_gui.py::test_tray_icon_click_opens_the_webui`
  （`FakeRoom` 记 `open_webui` 的调用次数）。

## 27. 修复（2026-09-11）：会话列表改名不生效，要刷新才看见

用户报告：「会话列表的重命名回车后不更新命名（虽然已经改了，但是刷新才显示）」。

- **症状与成因**：`/save` 真的写了盘（刷新后名字是新），但那一行**停在旧名字上**。会话行是
  **按 id 复用**的（`renderSessionList` 的 keyed reconciliation，为 FLIP 动画保留节点），所以一行的
  ✎/删除 handler 攥着的是**创建那一行时**那次 `/sessions` 返回的 session 对象；任何后续
  `loadSessions()`（`resumeIfPending` 3 s 轮询、每次 turn 之后、新建/删除会话）换掉的是
  `allSessions` 里的**新对象**，行的闭包却还是旧的。`finishRename` 于是把新名字写进**旧对象**，
  紧接着 `renderSessionList()` 的守卫发现「画出来的名字 ≠ 列表条目的名字」，又按旧条目把名字写了回去。
- **修法**（`web/app.js`）：新增 `sessionById(id)`（查**当前** `allSessions` 条目），
  `startRename` / `finishRename` / 删除确认一律按 `row.dataset.sid` 现查现用，改名写回的是当前条目
  ——画出来与列表一致，守卫自然不再回滚。
- **顺带修掉的第二个缺陷**：回车把输入框换回 `<span>` 会让它 blur，blur 处理器于是**第二次**
  调用 `finishRename`，`row.replaceChild` 抛未捕获的 `NotFoundError`（浏览器控制台可见），且
  向 `/save` 多发一次请求。现在输入框带 `done` 标志（一次改名只收尾一次），并按
  `inp.parentNode === row` 判断还该不该换回。
- 手机端（`web/m.js`）不受影响：它每次渲染**重建**所有行，闭包永远属于当前那一代。
- 回归：`tests/test_webui_sessions.py::test_renaming_a_session_updates_the_list_not_only_the_file`
  （真浏览器：先 `await loadSessions()` 造出陈旧闭包的条件，再改名，断言**画面**、**内存里的列表**、
  **`/sessions` 返回**三者一致且无未捕获错误）。改前红：画面停在 `(new session)`。

## 28. 调整（2026-09-11）：撤掉托盘菜单的「停止铃声」

用户：「托盘菜单的停止铃声去掉，多余」。

- 铃声改成一声之后（§26.1），那一项只剩「掐掉还在响的尾音」这点用处——连它一起撤掉。菜单回到
  显示主界面 / 打开 WebUI / 退出（`fungi/gui/trayicon.py`），`set_alert` 只管闪动与提示语。
- **连带删净，不留半死的开关**：`FungiGui.stop_ring()` 与 `_silenced` 标志一起移除——`_silenced`
  只由它置位，留着就是一段永不触发的分支（`_poll_unread` 的两处条件随之简化）。
- 回归：`tests/test_gui.py::test_tray_icon_flashes_while_mail_is_unread`（断言菜单里没有「停止铃声」，
  闪动与提示语照旧）；原来那条「停止铃声只停这一条」的用例随行为删除。

## 29. 边界（2026-09-11）：exe 版没有本地视频理解

用户拿着一句外部说法来问真伪：「已知边界：exe 版上 video 工具不可用（PyInstaller 冻结环境里跑不了
vidsense 子进程的 Python 解释器）——需要视频理解请用源码方式运行」。**结论：属实，而且是结构性的**
（不是「装个 Python 就好了」）。

- **子进程需要一个解释器**：`fungi/tools/video.py` 用
  `[sys.executable, "-m", "vidsense.cli", str(work), "--no-api"]` 起 vendored 管线；冻结之后
  `sys.executable` 就是 `Fungi.exe`，那行会变成「再起一个 Fungi」。`video.py` 里没有任何 frozen 分支；
  `fungi/gui/config.py::_python_cmd()` 的注释早就写明 *frozen exe has none*。
- **更早一步就断了**：`_video_ready()` 在**当前解释器**里 `find_spec` torch / transformers /
  faster-whisper / opencv。exe 的构建环境（`.github/workflows/release.yml` 的
  `pip install pytest pillow segno PyQt5 PyQt-Fluent-Widgets pyinstaller`）里没有 torch，
  而它是 GB 级、不可能塞进 60 MB 的包；冻结进程也**看不见系统 Python 的 site-packages**，
  所以「自己再装一套依赖」救不回来。`--collect-all vidsense` 只打包**包文件**，不带解释器。
- **处置**（用户 2026-09-11 拍板「按 1 办」）：不折腾打包形态，**写清楚 + 界面上说明白**——
  `ConfigPage._check_video_models` 在 `sys.frozen` 时直接写「本地视频理解只在源码方式下可用
  （exe 里没有 Python 解释器，跑不了 vidsense 子进程）：需要它就用 python start.py 跑源码」，
  并**隐藏**那个点了也没用的「下载缺失模型」按钮（`_HEALABLE` 那套自愈链在冻结态无从生效）。
- 回归：`tests/test_gui.py::test_config_page_frozen_exe_points_video_at_the_source_run`。

## 30. 修复（2026-09-11）：exe 原地更新在「运行中的 exe 已不在盘上」时不再全盘失败；图标回到任务栏

### 30.1 `update_exe`：没有东西可以让位时就直接装

用户报告（pc 那台，对话框原文）：

> 更新失败: [WinError 2] 系统找不到指定的文件。: '…\Desktop\release\Fungi\Fungi.exe' → '…\Fungi.exe.old'

- **读到的病因**：`WinError 2`（ERROR_FILE_NOT_FOUND）出在换装第一步 `exe.rename(old_exe)` 上——
  `sys.executable` 指向的 `Fungi.exe` **当时已经不在盘上**（文件夹被挪过/被改名、上次换装半途死掉只剩
  `.old`、或被安全软件/云同步动了）。下载与解压都成功了，卡的是「备份旧映象」这一步。
- **修法**：`exe` 不在盘上就**跳过备份直接装**（新文件 move 进去），这既是修复也是那种半残状态的恢复；
  `_internal` 的搬入条件从「旧安装里有」改成「新包里就有」（旧安装缺 runtime 时不再装出个跑不起来的
  exe）。**整段换装进 try**：任何 `OSError` 都把旧安装**整体**放回去（先清掉这次留下的半成品，再还原
  `_internal.old` / `Fungi.exe.old`），不再有「exe 已让位、_internal 还原失败」那种半坏状态。
- 回归：`tests/test_update.py::test_update_exe_recovers_when_the_running_exe_is_gone`、
  `test_update_exe_installs_the_runtime_even_without_one_on_disk`（两条在改前都是那条 WinError 2）。
  **（2026-09-12 追记：整块更新功能已按用户裁决移除，见 §34——本节连同这两条用例都是历史记录。）**
- **注意**：修复只有到**下一个 release** 才到得了 exe 用户手里——那台机器先手动解压新版 zip 覆盖一次。

### 30.2 任务栏图标：找回被误删的 `--icon`，并给进程一个身份

用户报告：「程序图标是蘑菇，托盘也是蘑菇，但是任务栏不是」。

- **一条被误删的旗标**：`7e44559` 给 exe 加过 `--icon assets/fungi.ico`，**`0066456` 重写 workflow 时把它
  丢了**（`assets/fungi.ico` 自此成了没人引用的死资源）→ exe 里没有蘑菇资源，Explorer 与任务栏只能落到
  PyInstaller 的默认图标。已在 `release.yml` 复原（并写明是复原，别再丢）。
- **进程身份**：源码运行（`python start.py`）时任务栏是按 `python.exe` 分组的，用的是它的图标；现在
  `run_gui()` 在**创建任何窗口之前**调用
  `SetCurrentProcessExplicitAppUserModelID("Offblink.Fungi")`，并 `app.setWindowIcon(assets/fungi.ico)`
  （缺资源时退化为运行时绘制的那枚 `make_icon()`），任务栏按钮因此拿到自家图标与身份。
- 实测：AUMID 读回 `Offblink.Fungi`（hr=0）；app/window 图标非空、64×64（`assets/fungi.ico` 只有一档
  64×64，够用；要更锐的高分屏大图标得再加尺寸）。⚠️ 本机 `QScreen.grabWindow(0)` 抓屏全黑、PIL 抓屏
  跑不起来，**任务栏的视觉确认得在你机器上看一眼**（源码跑一次就能看到；exe 要等下一个 release）。

## 31. 增补（2026-09-11）：会话式 bash（`bash_start`/`bash_send`/`bash_kill`）与「不上 PTY」的裁决

### 31.1 工具语义

- **三个工具**（`fungi/tools/shell.py`，与一次性 `bash` 同文件）：`bash_start(command, cwd, stdin_arg, should_abort)`
  起一条长驻命令，回 `id=<8hex>` + 首屏（最多等 1.0s）；`stdin_arg="nul"`（默认）时交互程序立刻读 EOF 自退，
  `"pipe"` 才可喂输入。`bash_send(id, text)` 写一行（`text + "\n"`），返回**自上次读取以来**的全部输出——
  不是「本次发送之后」：两次调用之间到达的提示符/报错正是要看的东西。`bash_kill(id)` 杀整棵进程树
  （Windows `taskkill /F /T`，POSIX `killpg`）。
- **分层**：三件套在 `TOOLS` 里，但**不在** `L3_TOOL_NAMES`——L3 工具子代理只拿一次性 `bash`（REPL 驱动
  属于用户面 Agent 的能力）。
- **生命周期**：会话属于发起它的回合。`should_abort` 经 `WeakMethod` 持 `Agent._aborted`，Agent 被 GC 即视为
  回合结束、会话成孤儿；另有 `BASH_SESSION_IDLE = 600s` 空闲上限与 `/stop`。守护线程 `bash-session-reaper`
  每 2s 收一次。**已退出**的会话不立刻摘除：缓冲与退出码留着做 post-mortem（`npm create vite` 的失败证据
  曾被 reaper 在 2s 内抹掉），直到空闲上限。
- **输出**：两条 daemon 线程各按 `read1(65536)` 喂增量 utf-8 解码器（Windows 管道没有 select），
  合并后统一走 `_truncate`（8000 字符头尾）。

### 31.2 能力边界：三根管道，不是 PTY

- 能跑：`python -i`、`npm` 一类脚手架提示、dev server / watch——任何「按行读写」的程序。
- 跑不了：要真 tty 的全屏/ANSI 程序（vim/top/psql）——`isatty()` 为假、无回显、无 terminfo。
- 与 eval 型内核的区别：没有跨调用的语言状态；`python -i` 的状态活在那个会话进程里，会话一杀就没了。

### 31.3 要不要升级成 ConPTY：2026-09-11 实测后裁决「不做」

用户提问「你觉得有必要完善 pty 吗？」。本机（Win11 + Python 3.13.7 + pywinpty 3.0.5）实测对照，
同一条 `print(1+1)`：

| | 现状（PIPE） | ConPTY |
|---|---|---|
| 起会话首屏 | `>>>`：56 字符 / 0 转义 | 23 字符，全是握手查询 `ESC[1t ESC[c ESC[?1004h ESC[?9001h` |
| 首行结果 | 6 字符 `2\r\n>>>` | 18,626 字符 / 667 个 ESC |
| 下一行 | 18 字符 | 175,381 字符 / 6,306 个 ESC |
| 换行 | `\n` 有效 | **`\n` 无效，必须 `\r`**（CRLF 输出再翻倍） |
| 输入回显 | 无 | 有，另带 OSC 标题序列 |
| `getpass` 密码 | 答不进去 | ✅ `pw: ` → `PW=secret123` |
| 全屏 VT 程序 | 不适用 | 绝对定位重绘流；没有屏幕模型就重建不出画面 |

- **唯一真收益**是 tty-gated 的密码提示（管道喂不进去，ConPTY 可以）。**代价**：现有 `bash_send` 的 `\n`
  全部失效；输出被重绘流灌满（`_truncate` 前得先 strip ANSI）；固定 2s 的 `SEND_READ_WAIT` 要换成
  「静默即停」；`.bat` 包装与行纪律要重做；`pywinpty`（cp313 wheel 存在）要进基础依赖并被 PyInstaller
  收 native 扩展。而按 31.1，`bash_start` 是**唯一**有产线证据的路径，改造风险直接落在它身上。
- **需求侧证据**：全库 `data/sessions/` 里 `bash_start` 只出现在一个会话（`20260910-090126.json`：
  1 起 + 8 喂，全是 `python -i -q` 里试 pyfiglet/cowsay），全部管道兼容，**零次因缺 tty 失败**。
- **结论**：不做。判据＝出现真实需求（要在 Fungi 里回答 ssh/mysql/git 的凭据提示，或驱动 TUI 类程序）
  再议；且那时的形状是**另开一条通道 + 最小 ANSI 屏幕模型**，不是给现有会话加个 `pty=true` 开关。
  纯凭据类需求更省的解法是让它非交互（`SSH_ASKPASS` / `credential.helper` / `mysql_config_editor`）。

### 31.4 子进程一律带 `WSL_UTF8=1`（2026-09-11 修）

`wsl.exe` 不管控制台代码页，**自己的输出一律 UTF-16LE**（发行版列表、诊断），于是 `wsl -l -v` 经
`chcp 65001` 包装回来是 `W\x00S\x00L\x002\x00` 这种乱码。`shell._child_env()` 给所有 bash 子进程注入
`WSL_UTF8=1`，wsl 改用 UTF-8，与本模块的 utf-8 解码对齐。回归：
`tests/test_tools.py::test_bash_children_see_utf8_wsl_env`、
`tests/test_bash_session.py::test_session_children_see_utf8_wsl_env`（改前两条皆红：`Environment variable WSL_UTF8 not defined`）。

## 32. 增补（2026-09-12）：手机端二维码进不去——防火墙按「程序」放行，页面自检 + 一键放行

用户报告：「exe 扫二维码进不去移动 WebUI」（源码版能进）。真凶不是代码：

- Windows 防火墙的入站例外是**按程序**的。本机取证：`python.exe`/`pythonw.exe` 有 8 条 Public 配置的
  Allow 规则（当年跑源码时点过「允许访问」），而 `Fungi.exe` **一条都没有**；WLAN 是 Public 配置且
  `DefaultInboundAction` 未配置 ⇒ 默认阻断入站。手机发出的包被**静默丢弃**——桌面侧零报错，手机侧一直转圈。
- 二维码侧无问题：地址是 `http://<lan_ip>:<webui 端口>/m?t=<token>`（端口取自真实监听端口，WebUI 绑
  `0.0.0.0`）；v0.4.1 包里 `_internal/web/*`、`_internal/segno` 都在。
- 现场修复：给该 `Fungi.exe` 加一条入站 Allow（TCP / Private,Public），手机立刻能进。**这条规则是
  按程序路径匹配的**：换目录、换版本的 exe 需要重新放行（同目录覆盖更新则沿用）。

### 32.1 落地：手机端页自检 + 一键放行（`fungi/gui/firewall.py` + `fungi/gui/mobile.py`）

- **检测**：`firewall.check_command()` 跑 `powershell -EncodedCommand`（UTF-16LE base64 → CJK 路径与引号
  全免疫），数「Enabled + Inbound + Allow 且程序等于 `sys.executable` 真实路径」的规则条数；
  `parse_count` 只读**最后一行**并按数字判断——程序可能同时拥有多条规则（python.exe 就是 2 条），
  这正是第一版按 `== "1"` 判断踩到的坑。结果缓存：True 永久有效，「无规则」30s 过期（用户可能刚点了放行）。
- **异步**：探测是一个 PowerShell 进程（本机实测 1.8~2.6s），所以走 `Popen` + `QTimer` 500ms 轮询，
  不阻塞 GUI；没有房间时既不探测也不显示。
- **提示与放行**：判定为「无规则」时，页面在二维码下方显示
  「手机连不上多半是这个原因：Windows 防火墙还没有放行 `Fungi.exe` 的入站连接（源码版早就放行过
  python.exe，打包版通常没人放行）」+「放行防火墙（手机才能连）」按钮。按钮经
  `ShellExecuteW("runas")`（UAC）执行同一份 `-EncodedCommand` 脚本：先删同名规则再
  `New-NetFirewallRule -Direction Inbound -Action Allow -Protocol TCP -Profile Private,Public`（幂等），
  成功后清缓存并在 2s 后复检；UAC 被取消（返回 5）则 InfoBar 如实说明，不装作成功。
- 页面顶部提示文案改口：不再说「公用网络常拦 Python 入站」（源码视角），改为「看下面的防火墙提示」。
- 回归：`tests/test_gui.py::test_firewall_probe_parses_the_rule_count`、
  `test_firewall_allow_rule_targets_this_program_only`、
  `test_mobile_page_offers_the_firewall_fix_when_this_program_is_blocked`；该文件另有 autouse fixture 把
  `firewall.start_check` 打桩成 `None`（真判定依机器而变且 PowerShell 慢，GUI 测试不该 shell out）。
- 验证：真机探测 `python.exe`→True（2 条规则）、今天手工加的 `Fungi.exe`→True、`notepad.exe`→False；
  真实平台截图确认「无规则」态显示提示+按钮、「有规则」态两者皆隐。

### 32.2 调整（2026-09-12，用户反馈）：状态常显 + 未放行时自动发起 UAC

用户两句话：「首先我没看到手机端有所谓的自检+放行」「其次为啥你不直接 uac 呢」。查下来两条都成立：

- **为什么看不见**：提示行与按钮**只在判定为「被挡」时才显示**。而他本机那条路径早就被放行过——
  两条 enabled+inbound+allow 规则（上一轮手工加的 `Fungi exe (mobile WebUI)`，以及应用自己建的
  `Fungi mobile WebUI (Fungi.exe)`，后者只可能由「一键放行」生成）→ 探测返回 True → 整页安静 →
  功能看起来根本不存在。同理，没起房间时也不显示（没有可连的东西）。
- **改法**：`_show_firewall` 分三态——`True`：常显一行「Windows 防火墙已放行 <程序名> 的入站连接，手机可以直接连。」
  （`False` 才藏起来的设计被判定为把功能藏没了）；`False`：显示原因 + 按钮，并**由应用自己发起一次 UAC**
  （`_fw_prompted`，每页生命周期只自动一次；用户取消后不再自动纠缠，按钮留着手动重试）；
  `None`（没房间 / 非 Windows / 探测失败）照旧什么都不显示，不许瞎报状态。
- **UAC 的边界**：它**无法静默**——`ShellExecuteW("runas")` 必弹系统确认框。所以「直接 UAC」省掉的只是
  **那一次点击**，不是那次确认；自动弹窗只发生在「房间运行中 + 探测判定被挡 + 打开手机端页」这一刻。
- 顶部提示不再指着「下面的防火墙提示」，只说扫码与刷新——状态行自己会说话。
- **探测的脆弱点（本轮踩到）**：规则匹配是整串比较（PowerShell `-eq`，大小写不敏感但对**斜杠方向敏感**），
  所以只能喂 `os.path.realpath(sys.executable)`（原生反斜杠）。用正斜杠路径手工查询会得到 0 条、误判「未放行」。
- 回归：`tests/test_gui.py::test_mobile_page_asks_windows_for_the_rule_by_itself_only_once` + 放宽后的
  `test_mobile_page_offers_the_firewall_fix_when_this_program_is_blocked`（已放行态改为断言状态行可见）。
  GUI 测试的 autouse fixture 现在把 `firewall.request_allow` 也打桩，否则「被挡」态一进页面就会真弹 UAC。

## 33. 修复（2026-09-12）：关窗其实没停进托盘，进程直接退了（房间被杀）

用户报告：「压根没有最小化到托盘的能力，无论何种情况下，关闭启动器就关闭了应用」。

- **病因**：`FungiGui.closeEvent` 在「停到托盘」分支里把关闭事件 `ignore()` 之后**继续往下走**，
  先无条件 `self._tray.hide()`（把回窗口的唯一入口也抹了），再 `super().closeEvent(event)` —— Qt 的
  默认实现会 **accept** 关闭事件，于是「最后一个窗口已关闭」触发 `quitOnLastWindowClosed=True`，
  **整个进程退出**：房间随之消失、WebUI 也断。真机探针（真实平台，构造带假房间的 `FungiGui`：
  `show()` → `close()` → 逐时刻读 `isVisible()`，并用「`exec_()` 会不会自己返回」判断进程是否退出）：

  | 时刻 | 修前 | 修后 |
  |---|---|---|
  | 房间启动 | win ✓ tray ✓ | win ✓ tray ✓ |
  | 关窗 | win ✗ **tray ✗** | win ✗ tray ✓ |
  | 关窗 2s 后 | **`exec_()` 自己返回（进程退出）** | 进程仍在、房间未停 |
  | 托盘菜单「显示主界面」 | 无处可点 | 窗口回来（`show_and_raise`） |

- **修法**（`fungi/gui/app.py`）：有房间时 `event.ignore()` + `self.hide()` + `self._tray.show()` 之后
  **直接 return**，不再调 `super().closeEvent`、也不再 hide 托盘；没有房间时那条「hide 托盘 → 正常关闭」
  的路径保持不变（此时进程确实无事可做）。
- 回归：`tests/test_gui.py::test_close_parks_room_to_tray`（关窗后托盘仍可见）、
  `test_close_parks_instead_of_closing_the_window`（新：直接发一个 `QCloseEvent` 给 `closeEvent`，
  断言事件**没被 accept** —— 吃下它就等于关窗口、进而退出应用）。两条在改前**都红**
  （`isAccepted() is True`、tray 不可见），改后绿。
- 口径提醒：托盘图标的生命周期是「有房间才有」（`update_tray` 在创建/加入房间时调用）——与是否打开过
  WebUI 无关；关窗行为由 `rooms()` 是否非空决定。要真正退出仍是托盘菜单「退出」或页面上的「离开房间」。

## 34. 调整（2026-09-12）：整块「软件更新」功能移除（用户裁决）

用户报告：「更新失败：`<urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。>`」，
随后裁决「算了，去掉整个更新功能」。

- **为什么这条路终归不可靠**：更新包要从 `github.com` 的资产主机下载（302 到
  `release-assets.githubusercontent.com`）。本机实测：**直连 3/3 被重置**（WinError 10054），只有经本地
  代理才通。而「自动找代理」本身有陷阱——`urllib` 对**显式** `ProxyHandler` 也照样做 `proxy_bypass`
  判断，于是游离的 `NO_PROXY` 能把兜底代理一起屏蔽；何况不同主机的代理端口各不相同
  （用户原话：「有的主机都不是 7897」）。与其堆兜底，不如去掉。
- **处置**：删除 `fungi/update.py`（下载 / 原地换装 / relaunch / `git pull` / 版本检查）、设置页的
  「软件更新」一节及其线程与信号、`fungi/gui/__init__.py` 里对它的 re-export、`tests/test_update.py`、
  `tests/test_gui.py` 的两条更新用例。**保留** `local_version()`——迁到 `fungi/config.py`
  （`fungi --version` 与「版本只住 pyproject」的单一真源不变），回归 `tests/test_config.py`。
- **代价（明确记录）**：exe 用户从此**手动**更新——到 Releases 页下载 `fungi-vX.Y.Z-windows-x64.zip`
  覆盖解压；源码用户仍是 `git pull`（本就不自动装依赖）。要重造可参考 `v0.5.0` tag 的历史实现。
- **顺带消失**：`.old` 残留清扫（`cleanup_old_install`）——旧安装里若还留着 `Fungi.exe.old` /
  `_internal.old`，手工删掉即可。

## 35. 增补（2026-09-13）：本机 Agent 能看屏与动手（`screen` 工具）

需求线：**给 Agent 加控制能力，而 Agent 背后是 VLM**。可行性与参数已在原型
`C:/tmp/pcbridge/pcbridge.py` 上真机实测完（原型**不在**本仓库）；本节只落四个决策——**能力本体 /
授权 / 在场 / 帧生命周期**，实现见 `fungi/tools/screen.py`。

### 35.1 能力本体：模型只回答「点哪个」，坐标由程序给

- **一条实测把形状定死**：让模型直接输出像素坐标，误差 **15–68px**（18px 的目标等于随机）；同一目标
  改成「从候选编号里选」，**3/3 命中，难目标 1px**。二值化喂模型是**负收益**（误差 21–296px、延迟 ×3）；
  把二值化交给算法（轮廓切框）是正收益（真实桌面切出 94 个候选、75 个自带文本）。
- 由此定下**坐标来源优先级**：a11y rect（**0 误差**）> OCR 文本框 > 视觉轮廓编号（SoM）> 两级放大让模型
  在放大图上给坐标（0.8–5.6px）。
- **写进 schema 的强制项**：`click` / `type` / `scroll` **没有 x/y 参数**。目标只能用 `target`（`targets`
  动作返回的编号）或 `name`（控件名 / 可见文本）表达，坐标由程序从 a11y rect 或 OCR 文本框取出。
  「别信模型的绝对坐标」因此是**结构约束**，不是提示词里的请求。
- 工具面（单工具多动作，工具名 `screen`）：`shot` / `windows` / `targets` / `click` / `type` / `key` / `scroll`。
  - `shot` 返回 `ImageRead`（复用 `agent._tool_content()` 的多模态通道，模型直接看见）；`targets` 把候选编号
    画回图上再附像素——模型看到的就是它要选的那张图。
  - 每个动作返回**动作后的新帧**：observe → act → observe 由既有回合结构提供，不新增引擎。
  - 窗口身份一律 **hwnd**：`windows` 给编号 + hwnd + 标题 + 进程名，注入动作必须带 hwnd。
    绝不按标题模糊匹配（实测两个窗口的标题都含 `demo.txt`）。
  - 文本输入**一律粘贴**（a11y 聚焦 → 剪贴板 → Ctrl+V → 回读校验）：不键入、不按输入法分情况。
    **⚠️ 该条已被 §37 取代（用户 2026-09-14 第四次点名）**：输入只有逐字一种方式，粘贴路线已从工具里删净。
  - 键盘走自己的 VK 表 + 粘滞修饰键语义；任何收尾路径（含异常/拒绝）都必须 `release_all_keys()`，
    否则主机的 Ctrl 会卡住。
  - 滚动用 `ScrollItemPattern.ScrollIntoView`（**按目标滚**：不碰滚轮、不占焦点、不动物理光标，也不会滚错
    窗格）；只有无 a11y 时才退 PgUp/PgDn。
  - **不做**：双击（拆成单动作后是时序耦合——两次点击只有落在系统双击时间窗内才等价，比单动作更易偶发
    失败；打开用 `shell_open`/`explorer_select`，重命名用 F2，展开用 `ExpandCollapse`）、滚轮、画图/拖拽。
- **动作后必校验，不校验不动手**（硬路径，写在代码里）：`click`/`type`/`key`/`scroll` 之后读程序侧真值——
  a11y 值 / 焦点元素 / 窗口状态 / 帧差——并把结论写进动作结果。连续 **3 次**对不上（同一 hwnd 同一目标）
  就升级为 `ask` 问人，而不是继续试。
- **「目标不存在」的判断留在程序侧**：`targets` 只报程序侧证据（a11y + 可选 OCR），查不到就返回
  `no_target` + 候选清单（让模型改名/换编号/换路径），**绝不**"猜个坐标点了试试"。让模型判空会把 token
  预算烧光并返回空 content。

### 35.2 授权：开关即许可（2026-09-13 用户裁决修订）

- **默认关**：`config.json` 的 `pc_control`（默认 `false`），设置页「实验性」一节有开关。关 = 工具**不进**本机
  Agent 的工具面：模型连看都看不到它，不是"调用了才拒绝"。
- **打开开关就是全部许可**：用户原话「其实我不想 fungi 操作还经我允许，直接操作即可。实验性开关意味着我已经
  允许了」。因此**没有**逐次确认卡，也**没有** 10 分钟授权窗口与「不可逆动作单独确认」——那些本来就在用卡片
  打断每一次动作，等于把"我已经同意过的事"再问一遍。**本段取代本文件早先写下的确认卡设计。**
- **判断留在程序的结构性护栏里**（不依赖用户点卡片，全部是代码里的硬路径）：
  1. **不点没把握的东西**：整块画布（无 pattern 且覆盖 ≥75% 窗口）不点、中心点不在目标窗口内不点、
     编号/标签过期（窗口移动过）不点、被别的窗口挡住取不到像素不点 —— 如实报错并给出下一步。
  2. **动作后必校验**：a11y 值 / 焦点 / 窗口状态 / 帧差；同一个目标连续 **3 次**没效果就改为**问人**
     （这是求助，不是许可）。
  3. **不弹任何本机通知**：托盘通知会盖在被操控的屏幕上方、并抢走焦点，正是这条能力最怕的干扰
     （2026-09-13 用户实测反馈，见 §35.14）——所以每个会话第一次动手时的「Fungi 正在控制桌面」提示
     **已删除**，`screen` 工具与托盘之间不再有任何接线。
  4. **关掉开关 = 立刻收回**：设置页关掉即 `disarm()`——释放所有按着的键、清掉标签与帧缓存。
  5. **房间退出 / 进程退出必 disarm**：防主机 Ctrl 卡死（这是只有人才能恢复的故障）。
- **硬边界**：UAC 安全桌面与提权窗口注入不进（**UIPI**）——凡是要提权的动作必须用户自己点。
- 给用户的提醒（也写在设置页）：这条能力等价于把键盘鼠标交给 Agent，所以开关放在显眼处、关掉立即生效。

### 35.3 在场：只挂本机 Agent

- 挂载点只有两处，都是"用户就在这台机器前"的场合：`trilayer.build_orchestrator`（单机 CLI / `--web`）与
  `clone/local.build_local_clone`（房间模式的 WebUI 回合，即 `room.py` 每轮重建的本机 Agent）。
- **通讯 Agent 永不挂**（它由对端驱动、身边没有用户监督——与 §6.1 砍掉 `spawn`/`background` 同一条理由）。
- **子代理不继承**：`child_extra_tools` 不含 `screen`，L3 白名单更不含。动手的是本机 Agent 本人，
  不是它派出去的子代理。
- 房间模式的挂载点每个回合重读 `load_config()`（与日记开关同一口径），所以设置页一开一关**下一轮**即生效。

### 35.4 帧生命周期：只留当前 + 上一帧

- 会话内只保留**当前帧 + 上一帧**（`deque(maxlen=2)`）：像素是稀缺资源（`data/sessions/*.json` 里已有
  base64 图片的先例，那是每回合一次性落盘的证据，不是可累积的内存）。
- 帧只用于**动作前后差分**与"这一步真的改变了吗"；不用于记忆——要复看就重新 `shot`。
- 给模型的图按长边 ≤1568 缩放后编码（与 `files.py` 的 `IMAGE_MAX_DIM` 同一口径），帧里另存**原分辨率**图
  供差分；`shot` 的结果文本注明尺寸与字节数。帧只存内存，进程退出即消失。

### 35.5 依赖与打包

- a11y = UI Automation，经 `comtypes`（`comtypes.client.GetModule("UIAutomationCore.dll")`）；它是**基础依赖**
  （exe 的依赖是 workflow 里显式列的 `pip install ...`，不是 `pip install .`，所以 `release.yml` 的 pip 行
  必须同步）。缺 `comtypes` 时工具返回明确的 ERROR（照 `video` 工具的做法），不静默降级。
- OCR 是**可选**能力（`rapidocr-onnxruntime`，重依赖，照 `video` extra 的做法不进基础依赖）：没装时
  `no_target` 的返回里写明原因。
- 打包：`_pyinstaller_hooks_contrib` 自带 `hook-comtypes.client.py`（本机 site-packages 已确认存在），
  而 comtypes 在冻结态把生成的类型库模块**只建在内存里**（`comtypes.gen.__path__` 落在压缩包里时不再
  往磁盘写）——所以 exe 侧除了 pip 行加 `comtypes` 之外不需要额外 `--collect-all`。

### 35.6 本切片到哪为止（边界如实记录）

- 已覆盖：a11y 可达的控件（现代桌面程序绝大多数）+ 只读看屏 + 粘贴 + 键表 + 按目标滚动 + 校验 + 授权。
- **未覆盖**：纯自绘界面（canvas/游戏/CAD）——a11y 里只有窗口外框。这类目标的降级链（OCR 文本框 →
  视觉轮廓编号 → 两级放大）留待后续切片，需要真机量一组命中率；原型已证明可行（OCR + 两点标定点中纯像素
  按钮；`canvas = 截图 × (0.667, 0.670) − (7.7, 32.0)`，**截图坐标 ≠ 屏幕坐标 ≠ 应用坐标**，不标定会偏
  ~40 物理像素，表现为"点了没反应"）。
- 也不做：真实屏幕画面外发（发给模型的图只来自用户已打开的 `pc_control` 与本次调用——这是能力的本意；
  与"是否拿真实桌面做测量实验"是两个问题，后者未决）。

### 35.7 窗口身份之外：最小化、托盘，以及「截图坐标 = a11y 坐标 = 输入坐标」（真机增补）

- **状态要报出来，不能靠"可见"猜**：`window_state` = `normal | minimized | hidden | untitled`。
  最小化窗口对 `IsWindowVisible` 仍然是"可见"、标题也在，只有 `IsIconic` 能分辨；它的 `GetWindowRect`
  是图标槽（本机实测 `(-48000, -48000, -47644, -47941)`），真实几何得走
  `GetWindowPlacement().rcNormalPosition`（实测恢复态 `780x690`）。
- **列表分两档**：默认只列"看得见或能还原"的（`normal` + `minimized`）；`windows include="all"` 才加进
  隐藏的（托盘常驻应用）与无标题的系统窗口（任务栏 `Shell_TrayWnd`），并跳过 <40px 的桥接/驱动窗口
  （本机 59 行 → 37 行，多出来的是 plumbing）。
- **不在屏幕上的窗口不许测量**：`targets` / `shot(hwnd)` 对 `minimized`/`hidden` 直接拒绝并指向
  `action=restore`；`restore` 是唯一的"叫回来"动作（`SW_RESTORE` 对隐藏窗口同样有效），所有注入动作之前
  也会自动做一次，并在结果里回显 `restored from minimized/hidden`。实测：隐藏窗口 `restore` 后回到
  `normal`；隐藏期间 a11y 树退化（6 个元素 vs 34 个），这正是"先叫回来再测量"的另一个理由。
- **托盘图标本身可点**：任务栏 `Shell_TrayWnd`（标题为空、常显）的 a11y 树里就有
  `显示隐藏的图标` / `网络 …` / `音量 …` / `电源 …`，以及应用按钮（`Everything 已固定`、
  `智能终端 - 1 个运行窗口`）——`windows include="all"` → `targets(hwnd=<taskbar>)` → 按名字点。
  溢出区的图标要先点 `显示隐藏的图标`，再扫它弹出的浮出窗口。
- **进程必须先把 DPI 意识定下来，再去读任何几何**：`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`
  晚于第一次读坐标就会两套空间混用——本机实测同一个窗口 `GetWindowRect` 给 `(300,200)`、UIA 给
  `(450,300)`（150% 缩放）。所以 `window_rect` / `normal_rect` / `_focused` / 截图 / `_scan` 都先
  `_ensure_dpi()`；这条不修，点击就会系统性偏 ~1/3 个窗口。
- **DPI-unaware 目标程序的截图有讲究**：它的 `PrintWindow` 位图是"1:1 内容塞进缩放过的画布"——实测
  58% 黑边、每个控件框按 1.5× 错位，而且随窗口历史不稳定；"不激活置顶"（`SetWindowPos` `HWND_TOP`
  + `SWP_NOACTIVATE`）被 shell 忽略（`WindowFromPoint` 仍返回覆盖它的窗口）。因此：unaware 窗口一律走
  **屏幕裁剪**（1–5% 黑边、与 a11y 坐标逐像素对齐）；它若被别的窗口挡着，就**如实拒绝**
  （`capture_problem`）并指向 `restore`——给一张会误导选号的图比报错更坏。
- DPI-aware 的窗口（如 Win11 Notepad 的 XAML 面）照旧走 `PrintWindow`：能截不在前台的窗口、不抢焦点。

### 35.8 修复与加固（2026-09-13 晚，来自用户真机实测反馈）

用户拿一台真实应用（微信 4.x）试了这条链路，报回四件事，四件都落到了代码里：

- **卡片答案是一个列表**：WebUI 把答过的卡片按「每问一条」回传，所以单个「允许」到达时是 `["允许"]`。
  原判定拿原值跟字符串元组比 → **用户点了允许却被当成拒绝**（`REFUSED ... (answer: "['允许']")`）。
  修法：`_answer_text()` 归一（列表/字符串同一条路），`_allowance()` 只认明确的允许词、明确拒绝词优先，
  认不出来按拒绝处理（安全默认）。回归 `tests/test_screen.py` 四条（含列表形状）。
- **自绘界面（微信 4.x）根本没有可点的控件**：整个界面只有 `MMUIRenderSubWindowHW` 一个元素、无任何
  pattern，而原来的 `targets` **只在 a11y 完全空**时才退 OCR——有一个容器元素就永远轮不到 OCR。修法：
  a11y 里**没有可交互候选**时就把 OCR 文本框追加进列表（spec §35.1 的优先级顺序），返回里说明这些是
  「图上的文字」。实测（探针窗上把 a11y 强制打空）：2.1s 出 11 个文本框，`按下`/`初始文本` 定位准确；
  中文密集列表会粘连误读（`项项`），所以只作「按可见文本找目标」，不作读取内容的手段。
- **两个"点错地方"的护栏**（用户那次点击落到了桌面文件上）：`target_problem()` 在注入前拒绝
  (a) 中心点**不在目标窗口矩形内**的候选（真点下去只会打到盖在下面的东西），以及
  (b) **无 pattern 且覆盖 ≥75% 窗口面积**的"整块画布"（自绘界面恰好只暴露这一个元素，它的中心不是控件
  而是猜）。要动它请用 `name=<可见文字>`（此时走 OCR）或键盘。
- **发出去的那一下要再问一次**（**已被 §35.2「开关即许可」取代，2026-09-13**：`typed_windows` 已随确认卡一起
  从代码里删除，`type` 之后按 Enter 不再有任何卡片）：`type` 之后在**同一窗口**按 Enter（提交/发送）属于不可逆动作，
  单独确认（`_session.typed_windows` 记住"这个窗口刚被粘过字"）；Tab 不算提交，不打扰。
- 顺带修的顺序问题：**先抬起窗口再测量**。原顺序里，被挡住的 DPI-unaware 窗口在做 name→OCR 解析时取不到
  像素（`grab_window` 返回 None）→ 名字永远解析不出来。现在 `click`/`type`/`scroll` 在授权通过后先
  `set_foreground`，再做最终解析；"快速失败"的预检只在像素可信（窗口 normal 且 `capture_problem` 为空）时进行。

### 35.9 补回第三条候选来源：程序侧二值化切框（用户点名的缺口）

用户问「不是我们起初的二值化丢了？」——是的，第一版只接了 a11y 与 OCR：a11y 只剩一个容器元素时 `targets`
把那个容器当候选，而**唯一能给无文字图标定位的那一层不在**。现在补回，与原型同序同分工：

- `_visual_boxes()`：**在程序里**做自适应阈值（`BoxBlur` 求局部均值 → 至少暗 8 级算墨）→ 闭运算
  （`MaxFilter`/`MinFilter`）→ 连通域（行程编码 + 并查集，纯 Python）→ 按尺寸滤掉边框/背景 → 合并邻近框。
  **不引入 OpenCV**（原型用 cv2，但那是 40MB 依赖；Pillow 的 C 实现 + 并查集够用，实测 780x690 窗口 ~0.3s）。
- **二值化图永不进模型**：模型只拿到编号清单 + 画着编号的**彩色**帧——原型实测把阈值图喂模型是负收益
  （误差 21–296px、延迟 ×3）。
- 触发条件是「**客户区**里没有可点候选」（`client_rect` + `inside_client`）：窗口自己的最小化/关闭按钮在标题栏
  里，不算数——第一版正是被它们骗过去，切图那层永远不跑（画布实测：修前 6 个候选全是标题栏，修后
  `#8 [shape] '(no text)'`）。
- 边框那条**闭合环**的包围盒是整个帧：必须先按尺寸/占比滤掉再合并，否则它把真实候选全吞掉（画布实测 0 个框）。
- 真机验收（Tk 画布 = 零 a11y，两个纯像素控件，其中一个**无文字图标**）：`targets` 给出 `#7 [text] '确定'`
  （OCR）与 `#8 [shape] '(no text)'`（二值化），点 #8 → 画布自己的日志 `{"kind":"hit","target":"icon"}`。

### 35.10 语义落回程序：模型给图标起名，程序记住（2026-09-13 用户裁决「前者就好」）

分工写清楚：**选哪个编号、那个形状是什么，是模型的事**（它在看图）；**把它固定下来是程序的事**。
新动作 `label(hwnd, target=N, label="发送")`：

- 标签绑定的是**矩形**而不是编号（编号只在一次 listing 内有效，矩形只要窗口不动就有效），存在会话里；
  `disarm()` 一并清空——授权期结束，那段语义也结束。
- `name=` 解析顺序：a11y 精确名 → **标签** → a11y 子串 → OCR 文本。真实控件永远压过标签。
- 窗口移动/改尺寸后标签失效并**如实报错**（与编号同一套判据：绑定时的窗口矩形对不上就拒绝），让它重跑
  `targets` 再标一次。
- 匹配不到时 `no_target` 带上「你在此窗口贴过的标签」，避免它反复给同一个形状重命名。
- 下一次 `targets` 会把标签显示回候选上（`#8 [shape] '发送'`），而不是又变成 `(no text)`。
- 真机验收：画布上 `label(target=8, label="发送")` → 再 `targets` 显示 `'发送'` → `click(name="发送")` →
  画布日志 `hit target=icon`。
- **本轮明确不做**：程序侧图标模板库（采集/匹配图标位图）——那是另一件事，量级完全不同。

### 35.11 自绘界面（QQ / 微信）能攻到哪一步（2026-09-13 实测）

用户问「我们有无攻破的可能？」——分三种界面，答案不同：

- **QQ 是 Electron（`Chrome_WidgetWin_1`）**：窗口里的 a11y 只有 **7 个无名无类**、铺满客户区的 ScrollItem 空壳。
- **外部强制唤醒 a11y：失败（实测）**。`oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT, IID_IAccessible)`
  返回 `hr=0`（调用成功）、拿到指针，随后重建 UIA 树**仍是 7 个无名元素**；把它抬到前台也只有 9 个、依旧无名。
  Electron 只在应用自己开启时才暴露真实树（`app.setAccessibilitySupportEnabled(true)`，或启动时带
  `--force-renderer-accessibility`）——所以这是一条**可试的正路**：用那个旗标重启应用。
- **CDP：现在没有，可造**。QQ 进程**没有任何监听端口**（netstat 实测）→ 现成调试端口不存在；要它得用
  `--remote-debugging-port=9222` 重启（Electron 认这个旗标），之后就能用 DOM 驱动，**完全不靠像素**。
- **微信不是 Electron 而是 Qt**（本机窗口类 `Qt51514QWindowIcon` / `Qt51514WxTrayIconMessageWindowClass`）：
  主界面是 `MMUIRenderSubWindowHW` 自绘面。Qt 应用要么自己实现 a11y，要么外面没有开关能把它打开 → 只剩
  像素（OCR + 二值化）或它自己的协议/存储。
- **像素路线可用，但有前提**：本机对 QQ 窗口 `targets` 实测得到 **29 个 OCR 文本框 + 15 个程序切出的形状**
  （左栏图标、头像、按钮）。"点得动"取决于两件事：① 坐标来自程序（已解决）；② **窗口必须在前台且未被遮挡
  ——Chromium 会丢弃被遮挡/不可见窗口的输入**，这正是"注入了但应用不处理"的另一半原因。工具现在每次注入前
  都先 `set_foreground`，并把"没抬起来"写进结果。
- **键盘路线最稳**（聊天类应用的推荐姿势）：`Ctrl+F` 搜索联系人 → 粘贴名字 → 回车（或按编号点中会话）
  → 粘贴正文 → 回车发送。全程只有"选定会话"那一步需要看屏。
- **不做**：为某个应用写死的坐标脚本、图标模板库。要更干净的路，就在启动参数上做文章（上面的旗标），而不是
  在像素上堆补丁。

### 35.12 「命令唤醒」为什么没用、点托盘为什么有用（2026-09-13 实测）

用户问：「为啥我点任务栏的微信 / 托盘的 QQ 就响应，fungi 用命令唤醒的窗口就不响应？」——量出来的结论是：
**唤醒方式确实有问题，但不是"没抬到前台"，而是"应用自己不知道它被打开了"。**

- `ShowWindow(SW_RESTORE)` 单独：窗口变 `normal`/可见，但**不是前台**（`GetForegroundWindow()` 仍是别人）。
- 再加 `SetForegroundWindow`：返回 1，窗口**确实是前台**（实测 `GetForegroundWindow() == hwnd`），`SwitchToThisWindow`
  同样有效。**所以"没唤醒"这个解释不成立。**
- 此时对 QQ 注入：单次 `SendInput`、批量 `SendInput`、甚至**直接 `PostMessage` 的 WM_LBUTTONDOWN/UP**（绕开输入
  队列、也绕开任何低级钩子）→ 整屏像素差 **0.000%**；`ctrl+f` 也一样；四个 QQ 窗口逐个试，**没有一个是活的**。
- 对照实验：普通 Win32 窗口（探针）**隐藏 → `restore` → 按编号点击 → 应用自己的日志确认收到**（0→1）。
  也就是说唤醒方式与注入通道本身没问题，问题在被注入的那个应用。
- 排除 UIPI：QQ、微信、我们这个进程**都是 ELEVATED**（同级），不是完整性级别把输入挡了。
- 排除"窗口选错"：四个 QQ 窗口逐个唤醒到前台再试，全都不理。
- 强制 a11y 激活也无效：`oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT, IID_IAccessible)` 返回 `hr=0`
  并拿到指针，重建 UIA 树仍是 **7 个无名元素**。
- **走 shell 才真的打开**：`targets(hwnd=Shell_TrayWnd)` → 点 `显示隐藏的图标` → 溢出面板
  （`TopLevelWindowForOverflowXamlIsland`）出现 → 点里面的 ` QQ: 3754901636…` → QQ 窗口 `hidden → normal`。
- **但它的 a11y 不能指望**：同一条路径下，一次读到 **20 个带名元素**（`窗口控制区域`、`天气:晴,点击查看详情`），
  另一次点开后 5 秒仍是 **0 个带名控件**（同一个 hwnd、同样是 `normal` 且前台）。所以：shell 路径解决的是
  「应用有没有真的打开」，**读写要靠像素层**（OCR + 二值化切框，实测 29 个文本框 + 15 个形状）。`restore`
  的结果里带上「named controls: N」，就是让模型知道自己处在哪个体制里。

**机制**：`ShowWindow`/`SetForegroundWindow` 改的是**操作系统**对窗口的账；应用自己仍以为它在托盘里——渲染面没接上、
输入路径不处理、a11y 也不打开。而点任务栏/托盘的图标**不是点在应用上**：那一下落在 **explorer**（任务栏宿主）上，
explorer 随后投递应用自己的 tray / 激活回调，应用于是跑它自己的「打开主窗口」流程。**这就是"人点就行、命令不行"的全部原因。**

**工具的修法**（`restore`，spec §35.7 的延伸）：

- **条件前移**（2026-09-13 用户要求："对于微信 QQ 这种，直接采用新方法"）：`via="auto"` 先看窗口再动手，
  满足任一条就**直接走 shell**，不再先等 OS 唤醒失败：① 窗口不在屏幕上（`minimized`/`hidden`，这就是应用的托盘
  状态）；② 它的 a11y **没有任何可寻址的东西**（无名字也无类名，QQ 的标志性 7 个空壳）；③ 它属于自绘家族
  （`Chrome_WidgetWin*` / `Qt5*` / `Qt6*` / `MMUIRender*`）**且刚被我们唤醒过**——第三条规定了"刚叫回来的
  Chromium/Qt 窗口"才走 shell，健康在屏的 Chromium 应用（如 Edge，有带名控件）不会被多余地点一下任务栏。
  走完 shell 若窗口仍不在屏幕上，再回落到 OS 唤醒。
- `via="window"` 只走 OS 唤醒；`via="shell"` 强制走 shell。
  先按「窗口标题 / 进程名」在任务栏找它的按钮，找不到就点 `显示隐藏的图标` 打开溢出面板，再按名字点它的托盘图标，
  然后关掉面板。
- 判据用**带名字的 a11y 元素数**（`_named_a11y`），比帧差可靠：实测隐藏态 0 → 真正醒来的 QQ 是 10–20。
- 真机验收：把 QQ 隐藏 → `restore(via="auto")` → 回到 `normal` 且 `named controls: 10`（修前同样是"可见"，
  但带名控件是 0，等于给了一个死窗口）。
- **只有一个唤醒入口**（2026-09-13 用户追问「你跑了新旧两种方法，不应该直接新方法吗」后重构）：
  `wake_window(hwnd, via)` 是 `restore` 与**所有注入动作**共用的唯一入口。`auto` 时先读窗口再决定——需要 shell
  的（不在屏幕上 / a11y 无可寻址 / 自绘且刚被唤醒）**直接走 shell，不再先跑一次 `ShowWindow`**：先跑 OS 唤醒
  只会把一个死窗口弄成"可见"，而且会破坏判据本身（窗口已经 `normal` 了，`shell_reason` 的第一条就失效）。
  只有 shell 走完窗口仍不在屏幕上，才回落到 `ShowWindow` 兜底。实测（QQ 隐藏态）：
  `→ normal (was hidden, named controls: 10)` + `shell wake first: it is hidden…` +
  `shell wake: clicked its tray icon ' QQ: …'`，**OS 唤醒一次都没用**。

### 35.13 双击回到工具面；「活着」与「看起来活着」的判据（2026-09-13 用户裁决 + 真机实测）

**双击回来了**（用户裁决）：「双击是除命令以外，当应用不在任务栏或托盘时的另一种快捷的打开方式」。`shell_open` 只管
"已经知道路径"的东西；画在桌面上、列表里、或属于**既不在任务栏也不在托盘**的应用的图标，只有双击能打开。而且它是
唯一"开新进程"的打开方式——新进程天然是活的，正好绕开 §35.12 那套僵尸窗口。

- 动作面：`click` / `double_click` 共用一个实现，差别只有两处。一是鼠标压放对 ×2（间隔 60ms，落在系统双击时间内，
  两次之间不移动指针），二是**验证**。
- **双击的"有效果"往往是"新窗口出现"**：双击桌面图标几乎不动被点窗口的画面（实测桌面帧差 0.00%），只在别处冒出一个
  新窗口。所以双击额外对比注入前后的窗口列表，出现新窗口即 `verify: opened 'xxx' → verified` 并把新窗口名字写进结果。
- 真机验收：`double_click(hwnd=桌面, name="Code")` → `(injected) verify: opened 'Code - 文件资源管理器' → verified`，
  `list_windows` 里确实多出 `CabinetWClass 'Code - 文件资源管理器'`；随后 `alt+f4` 关掉的是**我们自己开的**那一个。

**这一轮真机操作抓出三个真 bug**：

1. `list_windows(include_hidden=True)` 把**所有最小化窗口**当"图标大小的杂项"扔掉了：最小化窗口的 `GetWindowRect`
   是图标槽（-48000,-48000），而"跳过 <40px"的判断跑在"用 `GetWindowPlacement` 还原几何"**之前**。症状：`restore`
   对最小化窗口报 `the window is gone`，可 `list_windows(include_hidden=False)` 里明明有它（真机 5 个窗口全中）。
   修：先还原几何，再过滤尺寸。
2. `click_at` 的注入自检写成 `3 * clicks`，而实际事件数是 `1 + 2*clicks`（双击 = 5）→ 双击**明明成功却报
   `INJECTION FAILED`**，连带 `verified` 恒假。修：`1 + 2*clicks`。
3. `type` 在**粘贴已经完成之后**崩（引用了早已删除的 `_session.typed_windows`）→ 副作用发生了、工具却报错。修：删掉
   该行，并留回归用例 `test_type_pastes_verifies_and_survives_its_own_side_effect`。

**shell 唤醒是异步的，0.8 秒不足以判死刑**：微信（重 Qt 应用）点完托盘图标要 >0.8s 才起来。原实现 0.8s 就判"没醒"，
发 ESC 关面板并回落 `ShowWindow` → 得到一个"看着 `normal`、输入全被吞"的窗口（用户现场观察：「第一次开了两次，
第二次确实是死的」）。改：点完**轮询等窗口自己变 `normal`**（最多 `SHELL_WAKE_S`=4s）再判；没醒才 ESC 并重试一次；
真回落到 `ShowWindow` 时，结果里**明说**"应用没走自己的唤醒路径，这个窗口可能不理输入"。

**被"显示桌面"最小化的窗口，`ShowWindow` 放不回来**：`win+d` 之后窗口由 shell 托管；实测对 5 个最小化窗口调
`ShowWindow(SW_RESTORE)` 全部无效（仍是 `minimized`），而**点它的任务栏按钮**（shell 自己的路）把它们全部放回
`normal`——微信那条正是 `clicked its shell entry '微信 - 1 个运行窗口'`。修：`wake_window` 在 OS 唤醒无效时补一次
shell 路。

**自绘应用（微信）里"输入框"不可寻址——这是本轮摸到的边界**：

- `targets` 给出 36 个 OCR 文本框 + 10 个形状，**没有一个盖住消息输入框**：空框没有文字，轮廓对比度不足，二值化切不出
  来；a11y 里也没有（Qt + `MMUIRender` 自绘）。
- 所以"往微信里输入"只有两条路：① 焦点本来就在输入框（打开会话后通常如此，实测两次都粘进去了）；② 焦点不在时，
  工具**没有任何程序侧目标可点**。此时 `type` 的验证会说真话（`unverified: nothing changed on screen`）——
  **不要在这之后按回车**。
- 反面教材：`ctrl+alt+w` 是微信用自己的**开关**，不是"打开"——窗口已经开着时按它会把窗口关掉；随后 `type` 自己把窗口
  唤回来，但那条路是 `ShowWindow`（僵尸路径），粘贴进不去。
- **发消息前必须核对收件人**（本轮救了一次）：微信用自己的路径重开时会回到**它自己上次选中的会话**。实测唤醒后输入框
  里是「你好」，而标题是「方外·小希」——直接回车就发错人。规矩：**先读标题（a11y 名或 OCR 文本）确认收件人 → 再粘贴 →
  再看一眼输入框 → 最后回车**。
- 微信的草稿**按会话存**（切走再切回，草稿仍在原会话），别把"别的会话里有草稿"当成"我的输入没生效"。

**聊天类应用实录（工具驱动，非手写注入）**：QQ → 九佾、微信 → Blinvo，两封信都是这么发出去的
（`windows` → `restore` → `targets` → 点会话行 / 读标题 → `type` → 再次核对 → `key enter` → 截图确认气泡）。

### 35.14 不弹通知：托盘提示盖在被操控的屏幕上（2026-09-13 用户实测反馈）

用户原话：「操控 screen 不要弹通知，会影响操作」。托盘 toast 是**浮在最上层的窗口**，正好落在正在被截图、被点击的
那块屏幕上，还会抢焦点——唤醒/粘贴/回车这一串动作里它既是画面噪声（帧差被它污染）也是输入干扰（焦点被它拿走）。

- **删掉的是通知，不是护栏**：`screen.set_notifier` / `_hint` / `announce_once` 与 `Session.announced` 全部移除；
  `disarm()` 也不再"说一声"，**只做收回动作**（释放所有按着的键、清标签与帧缓存）——收回本身不需要通知，
  它是靠副作用自证的。`disarm()` 的 `reason` 参数随之删除（没人再读它）。
- **接线一并拆掉**：`gui/app.py` 的 `_wire_screen_notifications` 与 `__main__.py` 里 `if cfg.pc_control: screen.set_notifier(tray.notify)`
  都不再存在——`screen` 工具与托盘之间现在**没有任何通道**，未来想加通知要重新走一次这条路。
- **授权语义不变**（§35.2 的"开关即许可"原样成立）：没有卡片这件事本来就不靠通知来补，通知只是"本机可见提示"，
  现在连它也去掉；设置页开关的文案同步改成"打开开关即表示你同意它直接动手；"（去掉"不再逐次弹卡片询问"，
  分号后换行），InfoBar 里那句"动手前会先问你"也跟着改成"能看屏并直接动手"（旧文案与本段设计矛盾）。
- 托盘**其它**通知（"Fungi 已启动"、来信铃声/闪动）不受影响：它们不发生在桌控动作的链路上。

### 35.15 「触手可及」：打开窗口走应用自己的门口（2026-09-13 用户裁决 + 真机实测）

用户原话：「需要引入一种『触手可及』的概念：在桌面、任务栏或托盘的话，采用新方法打开」。**触手可及 = 应用自己的
入口点在屏幕上**——任务栏按钮、托盘图标、桌面图标。有一个就走它（`at_hand`），因为那一下点击落在 explorer 上，
应用会跑自己的"打开"路径（§35.12 的实测根因）；一个都没有才回落到 `ShowWindow`，并把"这窗口可能不理输入"说出来。

**找入口（surface 发现，全部实测）**

| 表面 | 怎么找到 | 实测 |
|---|---|---|
| 任务栏 / 托盘图标条 | `Shell_TrayWnd` | hwnd=65786；按钮名 `<AppDisplayName> - N 个运行窗口`，cls `Taskbar.TaskListButtonAutomationPeer`；托盘图标 cls `SystemTray.*` |
| 托盘溢出区 | 点「显示隐藏的图标」后出现的 `TopLevelWindowForOverflowXamlIsland` / `NotifyIconOverflowWindow` | 托盘类应用（QQ/微信）的图标多在这里，不在可见条上 |
| 桌面图标 | `Progman`（或持有 `SHELLDLL_DefView` 的 `WorkerW`）→ `SHELLDLL_DefView` → `SysListView32` | Progman 0x10148；a11y 行 = 每个图标一行（'QQ' (117,5,233,96)、'微信'、'学习'…），最上面一行是容器 `'桌面'` (0,0,2240,1400) |

**「触手可及」的三个面，各有各的方法（用户 2026-09-14 澄清）**：任务栏 = **单击**；托盘 = **点箭头展开隐藏后单击**；桌面 = **先 `win+d` 把桌面叫出来，再做桌控（双击图标）**。触手可及整体属于「桌控」范畴。顺序：任务栏/托盘 → 桌面。前两者点击是**激活已运行的窗口**，桌面图标是双击、**没跑时会启动应用**，
所以放最后。桌面走 `click_at(clicks=2)`（单击只选中）；双击后若原窗口没醒但冒出了新窗口，结果里报
`clicked its 桌面图标 'QQ' (double-click) and it opened 'X' instead`（与 §35.13 同一证据规则）。

**四条护栏（都是实测出来的）**

1. **前台窗口不点**：点已在最前的窗口的任务栏按钮会把它**最小化**（Windows 的开关语义）→ 直接返回
   `it is already on screen and in front — nothing to open`。
2. **桌面图标先 hit-test**：桌面被任何窗口盖住时图标也一起被盖住，盲双击会打在盖住它的窗口上（本机实测：
   浏览器最大化时 `WindowFromPoint` 在图标位置返回浏览器渲染宿主，桌面可见时才返回 `SysListView32`）。
   所以双击前要求 `GetAncestor(WindowFromPoint(中心), GA_ROOT) == 桌面表面`；被盖住时**明说**
   `its desktop icon 'QQ' is covered by another window`，而不是硬点。
3. **容器不是入口**：两个表面都把自己的容器列成一行（桌面 `'桌面'` 2240x1400、任务栏整条），点它什么都不开 →
   入口必须小于所在表面面积的 1/4（`_entry_sized`）。
4. **固定按钮不是入口**：`已固定`/`Pinned` 形态只在应用**没在跑**时出现，点了是"启动"，不是打开我们手里那个窗口。

**匹配（handoff 第 5 条的缺口在这里补掉）**：行名与窗口名对同一应用的叫法不同，两个方向都要试，且都按**整词**匹配：

- 行 **包含** 窗口标题或进程名 —— 桌面 'QQ'、托盘 ' QQ: 3754901636'、'Python - 1 个运行窗口'（窗口标题 `Fungi`）；
- 行里的**应用名**被窗口标题包含 —— 修前从来不匹配的 Explorer 情形：按钮叫 `文件资源管理器 - 1 个运行窗口`，
  窗口叫 `Fungi - 文件资源管理器`（handoff 第 5 条正是这个，现已命中）；
- **零宽字符要洗掉**：Edge 的标题实测是 `Fungi - 个人 - Microsoft\u200b Edge`（两词之间一个 U+200B），
  与按钮 `Microsoft Edge - 1 个运行窗口` 没有任何朴素子串关系 → `_norm()` 去掉 U+200B/200C/200D/2060/FEFF 再比；
- **必须整词**：桌面上一个叫 `OS` 的图标，用朴素子串会命中 `Microsoft Edge`（micros**os**oft）和
  `Task Host Window`（h**os**t）——给两个毫不相干的应用各找一个假入口；改成词边界后本机 40 个窗口零误配。

**真机验收（2026-09-13）**：最小化的 Explorer 窗口（`Fungi - 文件资源管理器`，handoff 里 `shell_wake` 唯一没命中的那个）
→ `at_hand` 给出 `任务栏按钮 '文件资源管理器 - 1 个运行窗口'` → `shell_wake` 单击一次 →
`clicked its 任务栏按钮 '文件资源管理器 - 1 个运行窗口' and it came up`，`window_state` minimized → normal，
可用控件 27 个（随后已把它最小化还原）。

**已知缺口（如实记录）**：入口名与应用任何窗口标题**完全无公共词**时仍找不到入口 —— 实测 WindowsTerminal：
按钮 `智能终端 - 2 个运行窗口`，窗口标题 `π : …` / `C:\WINDOWS\py.exe`，`at_hand` 返回 None，回落 `ShowWindow`
并照旧声明僵尸风险。（任务栏行的 UIA 属性里没有 AutomationId / HelpText 可用：本机 comtypes 代理上逐个读都是
AttributeError，拿不到 AUMID 这类能对齐 exe 的身份。）

**三种打开方式，各自真机跑过**

| 表面 | 方式 | 实测 |
|---|---|---|
| 任务栏按钮 | **单击** | 最小化 Explorer 窗口 → `clicked its 任务栏按钮 '文件资源管理器 - 1 个运行窗口' and it came up`（minimized → normal，27 个可用控件） |
| 桌面图标 | **双击**（单击只选中） | §35.13 那轮已在真机双击桌面图标打开过 `Code - 文件资源管理器`；本轮加的是护栏（hit-test + 容器过滤） |
| 托盘图标 | **点箭头展开隐藏 → 单击图标** | OneDrive（窗口藏在托盘）→ `clicked its 托盘图标 'OneDrive - 个人' and it came up`（0.5s，hidden → normal）；干净起点连测 3/3 成功 |

**托盘那条的三个实测事实**（都写进了代码）

- **飞窗不是"点了才有"**：`TopLevelWindowForOverflowXamlIsland` 在点箭头之前就存在（hidden）——实测点后 0.12s 变 `normal`、
  0.30s 才有 12 个图标行。所以判"开着"看 **state**，不看存在；`_close_tray_flyout()` 也只在真开着时才发 ESC
  （否则那一下 ESC 会打到当前有焦点的窗口上）。
- **箭头是开关**：把飞窗留在开着状态后，下一次点箭头是**关掉**它（本机实测踩到：搜索因此落空）。所以只在飞窗确实关着时才点它。
- **点完托盘图标，飞窗不会自己关**（实测飞窗仍开着、应用窗口已经起来），而且**不能顺手关**：应用的面板/窗口多在"窗外点一下"时
  light-dismiss —— 实测点箭头去收飞窗，把刚起来的 OneDrive 面板一起点没了。所以成功路径**不碰任何东西**，只把
  `(the notification flyout is still open)` 写进结果。
- **点托盘图标可能起的是"另一个窗口"**：OneDrive 的托盘图标起的是它的 `Activity Center`，而被持有的那个窗口（`OneDrive - 个人`，
  194x56 的气球宿主）始终是 hidden。所以判定改成"注入前后比可见窗口列表"，与 §35.13 双击同一证据规则：
  `clicked its 托盘图标 'OneDrive - 个人' and it opened 'Activity Center' instead`。

### 35.16 被盖住的矩形不许点；桌面/任务栏永不"抬起"（2026-09-14 用户追问「为啥它没看见桌面的微信快捷方式」）

**先说结论：它看见了，只是拒绝点。** 用户机器桌面上有 `微信` 快捷方式，`at_hand` 对微信窗口（运行中且藏在托盘）一直匹配得到它
——2026-09-13 那次全窗口实测里有 `'微信' proc=Weixin.exe state=hidden desktop='微信'`。用户看到的是**拒绝**：双击前那一步
hit-test 发现图标位置现在归别的窗口。

**实测（都在这台机器上）**

- 桌面 a11y 里 `微信` 是第 10 行：`[Invoke+Select+ScrollItem] '微信' rect=(117,147,233,238) centre=(175,192)`——**矩形一直在**。
- 但同一时刻 `WindowFromPoint(175,192)` 返回 `CASCADIA_HOSTING_WINDOW_CLASS 'π : Remove GUI popup…'`（用户自己的终端窗口）
  → 双击会打在终端上。**"矩形存在"不等于"可以点"**。
- **抬起桌面没用**：`set_foreground(Progman)` 返回 True、前台确实变成 `Program Manager`，但终端照样盖着那个图标
  （`WindowFromPoint` 仍是终端）——它只偷走用户焦点，一个图标都没露出来。
- 因此 `_is_shell_surface()`（桌面 + 任务栏）**永不 `set_foreground`**：`wake_window(desktop)` 实测返回 `[]` 且前台**不变**
  （`0x5A0938` 保持），而以前会抢焦点。

**落进代码的两条硬路径**

1. **`target_problem()` 增加"被盖住"拒绝**（`covering_window(point)` = `GetAncestor(WindowFromPoint(p), GA_ROOT)`）：
   点必须落在调用者指定的那个窗口上。动作路径本来就会先 `set_foreground`，所以普通窗口不受影响；桌面/全屏面则如实报
   `is covered: (175,192) belongs to 'π : Remove GUI popup…' (0x5A0938), so the click would go there instead of to 0x10148.`
   ——把"谁盖着它"写进结果，模型可以自己去点那个窗口的最小化按钮。`WindowFromPoint` 返回 0 也拒绝（屏幕上那里没有可点的窗口）。
2. **`at_hand` 的桌面分支**（§35.15 已有）用同一个 `covering_window` 判定，被盖住时返回 None 并给出同一句话。

**被盖住怎么办：先 `win+d`（用户 2026-09-14 裁决订正）**。「对于桌面这类关键词，肯定是要 win+d 的」——`win+d` 是 shell 自己的
"把桌面叫出来"路径，与 §35.12「点应用自己的图标才是它的打开路径」同一条道理。所以顺序是：**任务是关于桌面的 → 先 `win+d` 露桌面 →
再 `double_click` 图标**；`targets(桌面)`/被盖住的拒绝只是"现在看不见"的如实反馈，含义是"先把桌面叫出来"，不是"这条路不行"。
两处配套事实：`win+d` 是**开关**（再按一次恢复窗口）；而被 `win+d` 最小化的窗口 `ShowWindow` 放不回来、要点它的任务栏按钮（§35.12 实测）。

**同一天的真实回合并暴露了更大的洞（2026-09-14，用户贴出的一轮「打开微信」）**：微信**根本没在跑**（`windows include="all"`
里一个 `Weixin.exe` 都没有），于是 `at_hand` 那条路整条不适用——它挂在"手里有一个窗口"上。那一轮模型的做法是：
`windows` → `dir`/`where` 找 exe → **bash `start` 起 Weixin.exe**。桌面的 `微信` 快捷方式一直在 `windows` 列表里
（`hwnd=65864 0x10148 explorer.exe 'Program Manager'`），但**没有任何一句话告诉它那是桌面**：列表只解释了任务栏那一行。
能力其实早就存在——`targets(hwnd=65864)` 实测直接列出 35 个图标（`#2 'PakePlus'` … `#10 '微信'` 带 `Invoke+Select+ScrollItem`），
`double_click(name="微信")` 就是入口。缺的是**可发现性**，所以补两处（都是给模型看的文字，不是新机制）：

1. `windows` 报告在任务栏那行之后加一句桌面行：`the desktop is the 0x10148 row above: targets(hwnd=65864) lists the
   desktop icons (one row per icon), and double_click(hwnd=<it>, name=<icon text>) opens that one — this is how an
   application with no window, no taskbar button and no tray icon is started.`（+ 被盖住会被拒并说明原因）
2. 工具描述里把"没在跑的应用"写明：**别去磁盘上找 exe**，桌面图标就是它自己的入口；被盖住时先用 `key ['win','d']` 露桌面
   （再按一次就收回来），或者去处理盖住它的那个窗口。

## 36. 增补（2026-09-14 用户点名）：鼠标平滑移动，不是瞬移

用户原话：「将鼠标瞬移改成平滑移动」。原来是**一次** `MOUSEEVENTF_MOVE|ABSOLUTE` 直接落到目标——应用看到的是从原位置
一步跳过去，任何**跟踪指针**而不是只读位置的东西都看不到过程：hover 高亮与 tooltip 不会出现、画布/拖拽类界面拿不到中间坐标、
点错了也看不出"它从哪来"。

- **`move_to(x, y)`**：`MOVE_STEPS`=14 个中间点、总 `MOVE_DURATION_S`=0.22s、`smoothstep` 缓入缓出（两端无突变），
  **最后一点就是目标本身**（`eased==1.0` 时算出来正好是 `nx,ny`，所以不需要补一枪）。仍走 **`SendInput`** 而不是
  `SetCursorPos`：整个工具一条注入通道，应用看到的是一个连续的、和点击同源的手势。指针已经在目标上时不旅行（只补 1 个
  pin 事件）。
- **注入自检跟着改**：原来是 `1 + 2*clicks`，现在是 **`moves + 2*clicks`**（`move_to` 的返回值就是它注入的移动事件数）。
  这个数仍然是用来抓"结构体大小写错 → SendInput 静默返回 0"的。
- **双击不受影响**：移动只发生在两次压放**之前**，两次压放之间坐标不变（§35.13 的"一个手势"判据照旧）。
- **到达校验（写这一版时抓到的真问题）**：越界坐标会被系统 clamp——实测 `move_to(-185, 345)` 结束后指针在 **x=0**（当时
  差一点就在那里按下左键）。现在 `click_at` 在压放**之前**读 `cursor_pos()` 核对是否到达，`MOVE_TOLERANCE_PX`=2
  （实测往返误差：常规点 0px，屏幕角落 −1px）；**没到达就不按**，直接返回 False（调用方按既有口径报 `INJECTION FAILED`）。
- **代价**：每次点击 +约 0.22s。实测 `shell_wake`（含 `at_hand` 0.40s）合计 **0.70s**，仍在 `SHELL_WAKE_S`=4s 与
  `SETTLE_S`=0.3s 的量级内。
- **真机验收（2026-09-14）**：后台线程采样 `GetCursorPos`——`move_to` 一次移动期间采到 **244 次、15 个不同位置**
  （瞬移只会是 1–2 个），耗时 **0.225s**，落点与目标**逐像素相等**；端到端点击最小化的 Explorer 窗口 →
  `clicked its 任务栏按钮 '文件资源管理器 - 1 个运行窗口' and it came up`（minimized → normal），随后已把窗口还原。

**为什么不用 pyautogui（2026-09-14 用户问过，裁决：不换）**：pyautogui 确实有现成的 `moveTo(x, y, duration=, tween=)`，
本机也装着 `0.9.54`，但**它从来不是 Fungi 的依赖**（`screen.py` 第一版 `dbb88cd` 就写了这句注释，自建 VK 表与
`SendInput` 都是同一个口径；用户记得的 pyautogui 是上游原型 **Ophio**，`Ophio/repo/app/screen_capture.py` 里
`pyautogui.moveTo(..., duration=0.1)` 与 `duration=0`（瞬移）各一条）。本机实测对照：

| | 本实现 | `pyautogui.moveTo(duration=0.22)` |
|---|---|---|
| 中间点数 | 14（采样到 15 个位置） | 4（`num_steps = int(duration/MINIMUM_SLEEP 0.05)`；要 14 步得 `duration≥0.7`） |
| 单次耗时 | 0.225s | 0.378s（每次公开调用后固定 `PAUSE=0.1`） |
| 注入通道 | `SendInput`（与点击/按键同一条） | 移动 `SetCursorPos`、按键过时的 `mouse_event`（源码注释自述） |
| 自检 / 到达校验 | 有 | 无（越界自己 clamp 到边缘照点） |
| 旁路 | 无 | 指针在屏幕角时 `moveTo` 抛 `FailSafeException`（实测），须全局关掉 |
| 依赖 | 0 | +`pytweening`/`pyscreeze`/`pygetwindow`/`mouseinfo`/`pymsgbox`/`pyperclip`，且要同步 `release.yml` 的 pip 行（§35.5） |

留一手的可选改进（**未做**，等哪天嫌慢再说）：现在不管远近都 0.22s/14 步，短跳也花 0.22s；可按距离给时长
（`distance/6000 px·s⁻¹`，下限 0.06s）并把步数按 ~16ms 一点重算，短跳降到 ~0.06s。

## 37. `type`：输入只有一种方式——逐字（用户 2026-09-14 第四次点名）

用户拿出自己当年的 Typer（`Desktop/Vibe Coding/useful/Typer/源文件/Typer.pyw`）说「当初我的复制效果其实想做成这样的」。
Typer 的做法：`pynput.keyboard.Controller().type(char)` **一个字符一次**，中间 `char_delay`（可调小数秒），
**全程没有剪贴板代码**（grep 复制/粘贴/剪贴板 零命中）。Fungi 的 `type` 原本只有一种落字方式：把文本塞进剪贴板再 ctrl+V——
原子、快，也是当时自绘输入框唯一实测可靠的路。

**现在的裁决把这条抹掉了**（用户原话：「**不要提供全文粘贴这种方式**，除非他自己全选，然后快捷键粘贴，当我没说。
**我们要做输入，就只有一种方式，逐字复制**」）。所以：

- `type` **只有逐字**：`style` 参数从工具面删除，模块里剪贴板那一半（`snapshot_clipboard` / `set_clipboard_text` /
  `restore_clipboard` / `clipboard_text` / `ClipSnapshot` / `CF_*` 与 GlobalAlloc 原型）**整块删净**——不留半死的开关。
  传 `style="paste"` 会得到 `ERROR: there is no 'paste' route — input is typed one character at a time`。
  人类自己全选 + Ctrl+V 不关我们的事（用户："当我没说"）。
- **为什么这次必须删**（2026-09-14 本机实测的现场）：另一轮 Agent 交出的报告里写着「用 screen 的 type **一次性粘贴全文**（664 字）」——
  内容确实落进了 `新建文本文档 (2).txt`（1980 B = 652×3 + 12×2、UTF-8 无 BOM、五段齐，我逐项核对过），
  但**方式**是你点名的那个：把整篇文本从剪贴板一次性灌进编辑器。逐字才是"输入"。
- **实现**：每个字符一对 `KEYEVENTF_UNICODE` 的 down/up（`wVk=0`、`wScan=UTF-16 码元`）。这样**绕开键盘布局与输入法**——
  中文是"这个字"本身，不是要 IME 翻译的键码。`\n`/`\r` 发 Enter、`\t` 发 Tab（Typer 也是这么映射的：U+000A 不是
  应用读到的"换行"）。星平面字符（emoji）按**代理对**发两个码元（Windows 一次只吃一个）。
- **节奏**：字符之间 `char_delay`（默认 `TYPE_CHAR_DELAY_S` = **0.15s**，最后一个字之后不停顿）。
  原先 0.03s（33 字/秒）是**看不见**的——那一段看起来和粘贴一样，正是这条裁决要消灭的东西；用户口径
  「一般地，一秒 5 到 10 个字就差不多了」，实测 0.15s 的间隔 = **147 ms/字 = 6.8 字/秒**（纯注入成本只有 ~1 ms/字、
  850 字/秒，所以节奏完全由这个旋钮决定），落在带子中间。`_held` 完全不参与：一个字符不是"会被按住的键"。
- **慢下来就得能停**：`type_text(..., should_abort=)` 在**每个字符之前**问一次，停止键在**打字过程中**就生效
  （延迟 ≤ 一个字符），结果是 `stopped after N of M character(s): the turn was aborted` + 已落字数——按 0.15s 算，一段 640 字的
  前赤壁赋要 96 秒，不打断的话这一回合就锁死了。
- **判定（strike）不再把"画面变了"算成失败（2026-09-14 实测修正）**：自绘控件回读不到值（§40），那里唯一的证据就是帧差；
  修前连打三次**好**字也升级成问人（实测 `ESCALATED: 3 failed attempts at type into 0x1b90ea6`，而每个字都落了）。
  现在 `changed > DIFF_THRESHOLD` 同样清 strike，升级那行还带上「已落 N/M 字」（与本节"副作用已经发生要说实话"同源）。
- **半路失败要说实话**：`type_text()` 返回 `(已输入字数, 错误)`；某个字被 SendInput 拒绝就停在那里，把"已经落了几个字"
  写进结果（沿用 §35.13 那条"副作用已经发生就别装作没发生"的口径）。

**真机验收（2026-09-14）**：在**自己进程**里开一个窗口 + `EDIT` 控件（不动用户的任何窗口），聚焦后由后台线程逐字输入
`"你好，世界 😀 abc"`，主线程泵消息并按 10ms 采样 `WM_GETTEXTLENGTH`：

```
type_text → (11, None)       控件里的文本与期望逐字符相同（含 emoji 代理对）
增长时间线（要求 150ms/字）: +0.03s 1字 → +0.16s 2字 → +0.32s 3字 → … → +1.38s 11字
相邻字符间隔: [159, 152, 152, 149, 152, 151, 149, 159] ms   （第 6→8 字那一跳就是 emoji 的两个码元）
```

**同一天的真机闭环（Notepad++）**：桌面文档图标 `double_click` 打开（Scintilla 自绘，画面就是工具面）→ `type`
31 字 → `typed: 31/31`、`6.8 字/秒`、`the clipboard is never touched` → `ctrl+s` → **磁盘上的文件逐字等于打进去的那句**。
停一次给它看：`threading.Timer(1.5, …)` 置位 → `typed: 3/37 characters · stopped after 3 of 37 character(s)`。

**仍未测的**：逐字在**微信/QQ**（自绘输入框）里能不能落地——粘贴那条路是当年在那里唯一验过的，现在它没了，
所以"往微信里打字"这条能力**待验**（§35.11 的整套流程要按逐字重跑一遍才敢说还行）。

## 38. 血缘盘点（2026-09-14）：五个老项目与 Fungi 的真实对应

用户点名把五个老项目写进《详细版》的血缘一节（`docs/README-详细版.docx`），并说「你如果不肯定，可以分别确定一下
fungi 中的哪部分能力和这些项目有重合，再写文档」。逐一读源码后（五个并行只读调研 + 复核），结论里**有两条是误归因**，
一并记在这里，免得下次再查一遍：

| 老项目 | 真实关系 | 证据 |
|---|---|---|
| **Get It**（日程提醒 App） | **同源** | `assets/ringtones/` 七个 WAV 由 `scripts/make_ringtones.py` 按 `Get It/get_it_pyqt/源文件/ringtone.py` 的配方移植（同振荡器/包络/音符序列）；`fungi/gui/ring.py` 开头就写着「the seven tones the user's Get It app plays」 |
| **Ophio**（LAN 屏幕共享 + 手机远程操控） | **两部分同源** | ① 按键词汇表——`screen.py` 的 `_KEY_VK` 注释自认「the Ophio key_map's names and aliases」；② 粘滞修饰键/组合键语义（`_MODIFIER_VK` + `_held` + `release_all_keys`）。其余（a11y/OCR/切框/hwnd 身份/动作后校验）Ophio 全树 grep 零命中，是 Fungi 自研；坐标口径相反（Ophio 由手指给归一化坐标） |
| **Typer**（逐字输入器） | **同源** | §37 就是这件事：`type_text()` 的骨架（一字符一次注入、`char_delay`、末字后不停顿、`\n`→Enter、不碰剪贴板）来自它；Fungi 把 pynput 换成自建 `SendInput` + `KEYEVENTF_UNICODE` |
| **Huh**（PyQt6 截窗翻译工具） | **误归因**：上一代解法，且被有意换掉 | Huh 走 pygetwindow：标题子串认窗口 + 用户从列表里挑 + pyautogui 区域截图；Fungi 的窗口身份是 hwnd+pid+类名+状态，`screen.py` 里那句「never a title match」正是对它的否决。Fungi 全树/全文档零处提到 Huh |
| **二值化**（`其他/二值化`） | **误归因**：同名不同物 | 那个项目是 **Floyd–Steinberg 抖动**（固定阈值 128 + 误差扩散），全文 grep `findContours/connected/contour/bbox/morph/Otsu/adaptiveThreshold` **零命中**，没有切框能力。Fungi `_visual_boxes` 的真祖先是探针原型 **`C:/tmp/pcbridge/pcbridge.py`**（cv2.adaptiveThreshold(21,8) / MORPH_CLOSE(9)×2 / findContours，尺寸 8..900、IoU 0.3 —— 与 Fungi 的 `INK_CONTRAST=8`/`INK_CLOSE=9`/`MIN_SIDE,MAX_SIDE`/`_overlaps(iou=0.3)` 逐项对应） |

所以桌面控制这条线的**真正主心骨**（a11y → OCR → 程序侧切框 → 候选编号 → 动作后校验）没有先行项目可认：它是为
「Agent 用 VLM 看屏」专门写的，`pcbridge.py` 是动手前的一次实测（§35.8 记着它不在仓库里），不是血亲。
《详细版》里这段就是按这张表写的（含「上一代解法」「同名不同物」两条如实标注）。

## 39. 两条规则：文件走 shell（大），窗口走桌控（小）

用户裁决（2026-09-14）：「文件的增删改，指的是对于任意地方，是一个**大规则**。而触手可及和自绘应用文件的打开是一个**小规则**，
他们之间的联系在于一个强烈建议使用 shell，另一个强烈使用桌控。」

- **两个范畴（用户 2026-09-14 二次澄清）**：**触手可及整体属于「桌控」范畴**（三个面各有其法：任务栏单击 / 托盘展开后单击 / 桌面 `win+d` 后桌控，见 §35.15/§35.16）；**文件的增删改属于「shell」范畴，且对全局成立**。下面两条就是这两个范畴的规则。
- **大规则（shell 范畴，任意地方）**：文件/目录的**创建、删除、重命名、移动**一律走 shell —— `mkdir` / `ren` / `move` / `del`（或文件工具），
  **本机、共享空间、对端机器，任何地方都一样**。不走 GUI：不点那一行、不按 F2、不拖拽、不用 screen 工具。理由：GUI 路径每一步都要
  先把画面解析成矩形，**解析到相邻那一行就是数据事故**（§35.8 那次点击落到桌面文件上就是它的来源）；shell 那条路没有"解析"这一步，
  名字就是名字。
- **它住在哪（这才是"任意地方"的关键）**：这是**全局提示词**，不是 screen 工具的描述。实现是一份共享常量
  `fungi/agent.py::FILE_OPS_RULE`，追加到**每一个会做文件操作的提示词**：`SYSTEM_PROMPT`（L1/编排器）、`clone/local.py`（本机 Agent）、
  `clone/comm.py`（信使，两个分支都追加）、`trilayer.py` 的 `L2_SYSTEM` / `L3_SYSTEM`（子代理）。房间回合与 clone 回合的提示词是复制
  本机/克隆的，自动带上。
- **与既有工具规矩的分工（不打架）**：文件**内容**是 `read` / `write` / `edit` 的地盘（原有规矩「NEVER `bash cat` / `echo >`」照旧）；
  文件**是否存在**没有工具，所以归 shell。
- **一个如实标注的反面（本机实测 2026-09-14）**：`del` / `Remove-Item` **不进回收站**——看 shell 回收站命名空间的条目数，`cmd /c del`
  一个自建临时文件后**条目数不变（11 → 11）**；而

  ```
  powershell -NoProfile -Command "Add-Type -AssemblyName Microsoft.VisualBasic;
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('<path>','OnlyErrorDialogs','SendToRecycleBin')"
  ```

  → **11 → 12**（退出码 0；目录用 `DeleteDirectory`）。所以动**用户的东西**要用后者或先问人——许可覆盖"可以动手"，不覆盖"可以不留下痕迹"
  （§35.2 的口径）。
- **小规则（桌控范围内）**：打开窗口/应用 → **触手可及**（任务栏按钮 / 托盘图标 / 桌面图标，§35.15）；关于桌面的任务 → 先 `win+d`
  把桌面叫出来（§35.16 订正），再动图标；自绘应用里**没有程序路径**的东西（微信/QQ 的消息输入框之类）→ 才动像素（OCR + 切框）。
- **联系**：一条强烈建议用 **shell**，另一条强烈建议用**桌控**；判据是「**有没有程序路径**」——有（文件；有命令行/API 的东西）就走 shell，
  没有（窗口的打开动作、自绘界面里的控件）才动像素。§35.1 里「重命名用 F2」由大规则取代：工具面仍接受 `key ["f2"]`，但推荐路径变了。
- **本节并列写法已被 §41 次序化（2026-09-14 用户第三次澄清）**：触手可及（**含打开文件**）> CRUD > 自绘 > 其余，
  「有没有程序路径」不再是第一判据——它曾经把「打开一个文件」整条推给 shell。

## 40. 自绘界面：事先判断，直接走桌控

用户裁决（2026-09-14）：「对于特殊的自绘界面，事先判断并也采用桌控」。

- **判据（两条实测信号，任一成立）**，落成代码里的 `self_drawn_reason(hwnd, pairs)`：
  1. 客户区里**没有一个**"有名字或类名、且带 pattern"的控件 —— a11y 只剩标题栏那几个窗口按钮
     （微信 4.x 实测：整个界面只有一个 `MMUIRenderSubWindowHW` 元素、无 pattern）；
  2. 类名属于自绘家族（`SELF_DRAWN_CLASSES` = Chromium/Electron、Qt5/Qt6、MMUIRender，与 `shell_reason` 醒人时用的同一张表）
     **且**客户区没有可寻址控件 —— 这是 QQ 的签名（7 个匿名 ScrollItem 空壳）。
  **只按类名不算**：Edge 也是 Chromium，但它有具名控件，判成自绘等于把更好的来源扔掉。
- **"事先"体现在哪**：`targets` 一进来就先跑这个判断，成立就直接铺像素层（OCR 文本 + 程序侧切框形状），
  并且**在列表标题里先说出来**，而不是让模型从"没有名字"里猜：

  ```
  TARGETS in hwnd=0x… '微信' (self-drawn: Qt5152QWindowIcon is a self-drawn family and nothing
  inside its client area is addressable — the picture is the tool face here; a11y has nothing
  clickable here; 29 text boxes read off the picture by OCR …; 15 shapes … — pick them by
  number) — pick one by number
  ```

- **与 §39 两张范畴的关系**：自绘界面属于"**没有程序路径**"那一侧 → 归**桌控**（像素）；文件那侧有程序路径 → 归 **shell**。
  桌控的"触手可及"管的是**打开**（三个面各一种方式），这条管的是**打开之后怎么在自绘界面里动手**。
- 真机证据（§35.11）：微信 4.x = Qt + `MMUIRender` 自绘，OCR 出 36 个文本框 + 10 个形状；QQ = Chromium/Electron，7 个匿名空壳。
  二值化图本身永远不进模型（§35.9 = 喂阈值图给模型是负收益：误差 21-296px、延迟 ×3）。

## 41. 决策链（2026-09-14 第三次澄清）：触手可及 > CRUD > 自绘 > 其余

用户原话：「文件的CRUD和触手可及优先，如果属于触手可及的范畴，一律采用桌控，包括打开文件，任务栏和托盘等。
如果属于CRUD，一律采用SHELL（不包括打开操作）。如果不是前两者，则判断是不是自绘界面，如果是，也采用桌控；
最后if else，才使用Shell。」——这是一个**次序**，不是 §39 那种并列清单。

| 次序 | 判据 | 归谁 | 做法 |
|---|---|---|---|
| 1 | **触手可及**：屏幕上有一个入口点——桌面图标、已经开着的窗口里列出的文件行、任务栏按钮、托盘图标 | **桌控**（`screen`） | 任务栏单击 / 托盘展开后单击 / 桌面 `win+d` 后双击（§35.15、§35.16）。**打开文件也在这一条** |
| 2 | **CRUD**：创建/删除/重命名/移动文件或目录 | **shell** | `mkdir` / `ren` / `move` / `del`；内容归 read/write/edit。**打开不在其中** |
| 3 | **自绘界面**：没有可寻址控件（QQ/微信/Chromium 壳） | **桌控** | 事先判断、铺像素层（§40） |
| 4 | 其余 | **shell** | 打开一个"屏幕上什么都没有"的文件 → `start "" "<path>"`（§42 之后它不再困住回合） |

**为什么这一轮必须写次序（2026-09-14 的真实回合，用户贴出 `data/sessions/20260914-113234.json`）**：任务
「去桌面新建文本文档，在里面输入前赤壁赋全文。请使用 screen 工具。」模型 6 步：`bash` 探桌面路径 → `screen key win+d`
（先被拒"needs hwnd"，补 `windows` 后成功，**桌面已经露出来、帧已附上**）→ `write` 建 `前赤壁赋.txt`（内容只有 1 字节 `" "`）
→ **`bash start "" notepad "<path>"`** → 这一调用挂住（§42），用户按停止（`ERROR: cancelled by user`），会话 11:32:34 → 11:35:44 结束。
它自己的 reasoning 里其实**先想过**桌控那条路（右键桌面 → 新建 → 文本文档），是被规则劝退的。两处文字各推了一把：

- **§39 的判据**「有没有程序路径——有（**文件**…）就走 shell」把"文件"整类判给 shell，于是"打开一个文件"跟着走；
  而 CRUD 的清单（创建/删除/重命名/移动）里从来没有"打开"。次序写清楚（第 1 行在第 2 行之前）就没有这个缝。
- **screen 工具描述**把 `double_click` 写成 "the open gesture for an icon that **has no path you know**"（`fungi/tools/screen.py` 的 `SCHEMA`）。
  这一轮模型**知道**路径（文件是它自己写的），按字面就该排除 `double_click`——它照字面做了。已改成
  「at hand on screen 的东西的打开手势：桌面图标、文档图标、已经开着的窗口里列出的文件」。
- 同类事故 §35.16 记过一次（微信没跑 → `bash start Weixin.exe`）：那次补的是「Program Manager 就是桌面」，治的是**窗口**；这次是**文档**。

**残留分支要说清**：既不在屏幕上、也不是 CRUD 的打开（深层目录里的文件）落在次序 4，用 `start "" "<path>"`
——它挑的是"双击会挑的那个应用"；本机 `.txt` 关联实测是 `txtfilelegacy` → **Notepad++**，所以 `start "" notepad <path>`
这种写法挑的是**另一个**应用。这一支现在能用，是因为 §42 把管道那条坑堵了。

**真机验收（2026-09-14，本机）**：桌面放一个 `fungi-chain-probe-<hex>.txt` → `targets(hwnd=Progman)` 直接读出
`#29 [Invoke+Select+ScrollItem] 'fungi-chain-probe-….txt'`（**文档图标在 a11y 里是有名、可寻址的一行**，不必靠 OCR）
→ `key ['win','d']` → `double_click(name='….txt')` → `verify: opened 'C:\…\fungi-chain-probe-….txt - Notepad++' → verified`。
即「打开文件走桌控」这条在真机上成立（此前 §35.13 只验过**应用**图标）。顺带两个老事实再次命中：`win+d` 是**开关**
（工具自报 `frame changed 0.00% → unverified` 就是"这一下没换状态"，隔一次再按才生效），而被它最小化的窗口要靠
**点任务栏按钮**回来（`click '文件资源管理器 - 1 个运行窗口'` → `frame changed 0.47% → verified`，`ShowWindow` 不管用）。
**出口是有代价的，这一点也量了（2026-09-14）**：任务栏那一格里**组里有 2 个窗口时**，点按钮打开的是**缩略图飞窗**而不是恢复——
它的 a11y 是空的（自绘，OCR 只读出行标题），真正吃点是**缩略图图片**那一行（标题文字点了 `frame changed 0.00% → unverified`），
Escape 也不关它（`send_keys(["escape"])` 无效；`XamlExplorerHostIslandWindow` 一直占着前台）；
`restore(hwnd=…)` 这条路能救回来（实测 `RESTORE hwnd=0x8F0D18 '…' → normal (was minimized, named controls: 27)`，
它自己的报告里两次"clicked its 任务栏按钮 … but it stayed hidden"）。所以**关于桌面的任务用 `win+d` 之前先想好怎么还**。

**落地的文字**：`fungi/agent.py::FILE_OPS_RULE`（次序 + "Opening a file is not a file operation"），追加到五个提示词
（§39 已记装配点）；screen 工具描述的 `double_click` 与"at hand 优先"两处；回归用例
`tests/test_local_clone.py::test_the_file_ops_rule_reaches_every_prompt_that_can_touch_a_filesystem`（三个 key 逐一在六个提示词里找）。

## 42. bash 不再被交出去的句柄困住：等的是进程，不是管道 EOF

**症状（本机实测 2026-09-14）**：`tool_bash` 把命令写进 `.bat`、`cmd.exe /c` 起、stdout/stderr 接 `PIPE`，每 0.2s
`communicate(timeout=0.2)` 轮询，上限 `BASH_TIMEOUT = 600`（`fungi/tools/shell.py`）。`start "" notepad <path>` 起的
GUI 子进程**继承了 cmd 的 stdout/stderr 写端**：cmd 自己**立刻退出**，但写端还开在 Notepad 手里 → `communicate()`
永远等不到 EOF → 循环转到 600s 或用户按停止（`ERROR: cancelled by user`）。**是句柄问题，不是"在等 Notepad 关掉"。**

| 形状（与工具同形的 Popen） | 实测 |
|---|---|
| `stdout=PIPE` + `communicate()`，bat 里 `start "" notepad <tmp>` | cmd 已退出（`poll()==0`）却**阻塞 >8s**；杀掉那个 Notepad 才释放 |
| 同上，`start /b cmd /c "ping -n 20 127.0.0.1 >nul"` | 同样阻塞（**无声**——这就是回归用例的复现命令） |
| 输出改文件 + `proc.wait()` | **0.11s 返回** |

**改法**：输出不走管道，改两个 `O_TEMPORARY` 临时文件（`os.open(..., O_TEMPORARY)` = `FILE_FLAG_DELETE_ON_CLOSE`）；
等待从 `communicate()` 改成 `proc.wait(timeout=0.2)` —— **等进程，不等 EOF**。为什么必须 `O_TEMPORARY`：句柄随 `start`
交给被启动的应用，它可能握几小时，那时 `unlink` 是共享冲突（POSIX 命名的分支由 `finally` 里的 `unlink` 兜底）。
实测语义：最后一个句柄关闭前文件存在（哪怕只有被启动的应用还握着），关掉即消失；`lseek(0)` 读得回子进程写的内容。

**代价与边界（如实）**：命令的输出在**我们启动的那个进程退出时**定格——`start` 之后的输出不再等（正是 `start` 该有的语义）；
`BASH_TIMEOUT`、`should_abort` 的 0.2s 响应不变（`test_bash_timeout` 仍走超时分支）；`bash_start` 那套会话机（`PIPE` + 读线程）没动。

**验收**：`tests/test_tools.py::test_bash_returns_when_the_command_hands_its_handles_to_a_child`——`BASH_TIMEOUT` 调到 6s、
跑无声复现那条命令，断言不出现 `Timed out` 且 5s 内返回。**修前跑它红**（实测 `AssertionError: 'ERROR: Timed out after 6s'`）；
修后 0.11s 返回。

## 43. GhostWorld 角色控制（2026-09-15）：一条回环通道，两半接线

**接的是什么**：GhostWorld（`useful/GhostWorld`，同一台机器上的第一人称小世界）把一条**回环通道**暴露给外部程序：
`ghostworld-send '<json>'` 一条命令一个 ack；`ghostworld-wait --follow` 阻塞着等，玩家说话才推一行。
协议规范在游戏仓库的 `docs/PROTOCOL-agent-channel.md`（线协议 / 发现文件 / 事件对象 / 游标 / 退出码）。

**只调 CLI，不 import 游戏**（`fungi/tools/ghostworld.py`，`subprocess`）：游戏内部怎么改都不关这边的事；
这边也碰不到游戏的世界（游戏侧红线：只有它的帧循环线程能改 WorldState，通道线程只投队列）。
`ghostworld_dir` 指向游戏检出目录（空则用安装出来的 console script）。
（填哪个目录不用猜：游戏那侧跑一次 `ghostworld --where`（或 `python -m metaverse.launch --where`）会直接打印这一行，
并把装到哪儿、运行时写哪儿、哪不可写一并列出来。）

**两半，缺一不可**：

| 一半 | 形状 | 为什么 |
|---|---|---|
| `ghostworld` 工具 | `BoundTool`：一轮一次调用、一条命令一个 ack | Agent 动手靠它——先 `pos`/`look` 看清，再 `say`/`goto`/`pickup` |
| 监视器 | `arm()/disarm()` + 常驻子进程 `ghostworld-wait --follow`，每行玩家发言变成一条**本地回合** | Agent 是**被叫醒的**，不是去轮询的（对齐 `_mail_watch_loop` 的既有形状） |

**玩家的话长得像 `[GhostWorld] 玩家（player）说：…`**，走 `Clone.note(text, source="GhostWorld")`：`note()` 原先
只有"主人对报告的反馈"一种来源（`from_owner`），现在多了 `from_channel` 分支（`clone/base.py::render_input`），
所以 Agent 不会把玩家读成主人；`note()` 从不交给 transport，对端信使也听不到。

**开关即许可（照抄 §35.2）**：`config.json` 的 `ghostworld`（默认 **关**）。关 = 工具**不进**工具面、监视器不 arm、
没有子进程；工具**调用时**再查一次（`enabled()`），关掉立刻生效。设置页**「拓展」**一节有开关（`gui/config.py`，
关掉顺手 `disarm()`），help 页有一节讲它。

**为什么是「拓展」不是「实验性」**（用户 2026-09-15 定调）：VidSense 与 GhostWorld 都已经是**能独立存在的项目**，
Fungi 只是把它们接上；「实验性」留给还在长、随时会改的东西（日记、桌面控制）。设置页因此分成两组：
实验性 = 日记 / 桌面控制，拓展 = 视频理解 / GhostWorld。

**监视器自己管子进程**：子进程退出码 **2** = 游戏没在跑 → **30s** 后再看，不是 5s 空转；游戏关掉又起来 → 子进程
自然重启；**不会丢事件**——游标（游戏侧 `metaverse/.wait_cursor.json`，按 `seq`）存在游戏那边，重连从上次位置续上，
游戏重启后 token 变了就归零，缓冲区里没读过的事件一次交出。只有 `kind=wake`（玩家发言）叫醒 Agent，
observation（`see`/`goto_done`/`position`…）不叫醒，要用时用工具读。`room.stop()` 与 `atexit` 都 disarm。

**听见就应（2026-09-15 用户要求）**：玩家的话不是进"信箱"等当前回合跑完——那等于让角色在长回合（走路、
连着几个工具轮）里装聋。监视器收到发言先 `Clone.interrupt_turn(2.0)`，**和 WebUI 的 `/stop` 是同一套 abort**
（`_turn_abort` 事件 → `TriLayer.should_abort`；被切断的回合已产出的内容留在 `history` 里，新回合仍知道刚才在干什么），
然后把这条发言排成**自己的回合**；两秒宽限让一串连珠炮不至于把答案反复推倒重来。

**`snapshot` 是眼睛（2026-09-15 用户要求）**：这条命令本来就把第一人称画面渲成 PNG（游戏侧 `metaverse/snapshot.py`，
从角色自己的位置与朝向前视）并把路径回在 ack 里（`snapshot_done.local`）；这边把那张图读回来包成 `ImageRead`
（`attach_snapshot`），Agent 循环会把它升级成多模态消息——**视觉模型真的看见**建筑/天空/地面/玩家，而不是只拿到一串坐标。
文件读不回来就退回原样 ack（路径还在里面）。工具描述里也写明了这个用途：想"看一眼"就用它。

**真机（2026-09-15 本机实测，无 LLM）**：headless 游戏 + GhostWorld 仓库自带的 `headless_player.py`（联调夹具：起服务、连一个会说活的玩家——
无头游戏里没人说话，它就把这件事做掉），直接调用本模块：

- `send_command({"cmd": "pos"})` → `{"type": "position", "x": 7.5, "y": 1.5, "facing": 1.57, "map": "smoke.json"}`；`look` → 完整 perception；
- `arm()` 之后 **0.4s** 拿到玩家的真实发言（两轮实测：`--say` 那句原样到达）（`seq=1, kind=wake, from=player`，且该事件**发表于它连上之前**——没丢）；
- `disarm()` → 监视器与子进程都消失。

**验收**：`tests/test_ghostworld.py`（24 例，全用假 CLI／假子进程，不碰真游戏、不起真进程）+
`tests/test_gui.py::test_the_ghostworld_switch_sits_under_experimental_and_disarms_when_off`。
**真机 LLM 回合（用户 2026-09-15 实测）**：玩家在游戏里说了一句，Agent 被叫醒后连着 `pos` → `look` → `say` → `track` 四个调用全部成功——
「叫醒 + 命令往返」这条线在真模型下走通了。（当时文本变乱码：是游戏侧 CLI 的 stdout 用了控制台代码页，已在 GhostWorld 修掉，不是这边的契约问题。）

## 44. 三种安装形状与「窗口」这一课（2026-09-16）

**同一个游戏，三种装法，通道文件在哪**（`ghostworld.py` 的 `packed_cli`/`_cli`/`_channel_file`）：

| 装法 | 怎么起通道 CLI | 通道发现文件 |
|---|---|---|
| 源码检出 | `python -m metaverse.cli_channel <verb>`（`ghostworld_dir` 指向检出根） | `<检出>/metaverse/.channel.json` |
| pip 安装 | console script `ghostworld-<verb>`（`ghostworld_dir` 可留空） | 装到哪就写在哪旁边 |
| 解压出来的 exe 发行包 | `GhostWorldCLI.exe <verb>`：先看 `ghostworld_dir` 那一层，再看 PATH | `%LOCALAPPDATA%\GhostWorld\.channel.json` |

发行包写不了自己那一层（可能只读、也随时会被清掉），所以通道与地图落在用户数据目录；`ghostworld_dir` 填的就是
**解压出来那一层**（`GhostWorld.exe` 与 `GhostWorldCLI.exe` 的旁边）。选哪条不用猜：游戏那侧跑一次
`ghostworld --where`，会把「装在哪 / 运行时写哪 / 哪不可写」直接打印出来。

**控制台窗口这一课（2026-09-16 用户报「发送信息后弹出 cli」）**：Fungi 的 exe 是 `--noconsole`，自己**没有**控制台；
Windows 会给它启动的**任何控制台程序**（`GhostWorldCLI.exe` 就是）分配一个**新的控制台窗口**。修法 =
`_run_cli` 与 `_spawn` 两处 spawn 都带 `getattr(subprocess, "CREATE_NO_WINDOW", 0)`。真机 A/B（父进程自己也没有控制台，
按窗口类名 `ConsoleWindowClass` 比 spawn 前后的窗口集合）：旧写法 +**2** 个可见控制台窗口，新写法 **0** 个，
follower 照常连上、`pos`/`say` 拿真 ack。（在 bash 里跑 python 永远量不到这种弹窗——子进程会继承 bash 的控制台。）

**硬杀游戏留下的 stale `.channel.json`**：`game_is_up()` 只看文件在不在（这是不连线就能拿到的唯一证据），
所以硬杀之后监视器每 **30s** 起一个注定退 **2** 的子进程——事件不会丢（游标在游戏那边），窗口也不再弹，
但这是纯空转。彻底修法要确认「这通道真有人在听」（查文件里的 `pid` 是否活着，或对 `port` 做一次短连接）。

## 45. 运行日志（2026-09-16 用户点名）：打包版没有终端，所以留一个文件

**用户原话**：「给 fungi 加个测试日志，方便 release 测试连接不上的问题」。Fungi 的 exe 是 `--noconsole`：
既没有终端可以回翻，`sys.stdout`/`sys.stderr` 也是 `None`——一次「连不上」的测试**什么都不留下**，
而报告最需要的恰恰是「试了哪个地址、谁试的、对方回了什么」。

**文件在哪**（`fungi/runlog.py`）：`logs/fungi-YYYYMMDD.log`，就在 `config.json` 与 `data/` 旁边（exe 的目录）；
按天追加、保留 **14 天**（启动时清掉过期文件）。程序目录不可写（比如解压进 Program Files）时，
退到 `%LOCALAPPDATA%\Fungi\logs`。`logs/` 进 `.gitignore`；文件是追加打开的，
所以测试**可以在程序还开着的时候**直接打开它看。

**谁写**：只有入口调 `setup()`——`__main__.main()` 与 `gui/app.py::run_gui()`（exe 从 `start.py` 直达后者），
其它模块只调 `runlog.note` / `problem` / `warn_once`。导入 Fungi 或跑测试**不写任何文件**（模块默认挂 `NullHandler`）。

**横幅**（每次运行一段）：版本 / python / 平台 / frozen / 根目录 / argv / config 路径与存在性 /
模型与 endpoint / **api_key 有没有（绝不写它本身）** / ghostworld 开关与目录。

**日志里不放凭据**：api_key 只写「有没有」；房间 token 一律不进文件——连「拿错 token 的敲门」那行也只记路径、不记 query（GET 的 query 就是 token），因为这个文件会被贴进公开仓库的 issue。

**最值钱的一条**：`sys.excepthook` + `threading.excepthook` 的 traceback。窗口版 exe 崩掉是**无声**的，
而 PyQt5 在 `qFatal` 之前会走 `sys.excepthook`，所以打包版的崩溃现在也留痕。

**「连不上」清单**（每条出站尝试一行）：

| 段 | 记什么 |
|---|---|
| 模型 | 失败原因（endpoint + HTTP / 连接失败 / 流中断 / 没有 finish 信号）；**首次连通**写一行 `model reachable` ——「模型没答」与「模型没连上」从此分得开 |
| 房间 | hub 每次请求失败（按 key **60s 节流**）+ 恢复一行 `is answering again`；房主侧 `hub listening on 0.0.0.0:<port>`；**拿错 token 的敲门**（`used the wrong token`，按来客节流） |
| 找房 | 扫的网段与端口区间、找到的 `ip:port`、没找到时三种可能（房主没起 / token 不同 / 不在同一网段），以及每个有响应但不是 hub 的端口 |
| WebUI / 手机 | `WebUI listening on 0.0.0.0:<port>`（端口从 8899 往上找，不是固定的）、手机页 URL、**Windows 防火墙判定**（未放行＝手机连不上最常见的一因） |
| GhostWorld | 每次 CLI 调用（命令、cwd、退出码、**stderr 尾**）、follower 起停与退出码、通道文件在不在、玩家发言、follower 自己合并进来的输出 |

**打包版的三个静默点一并修掉**（同源：`sys.stdout is None`）：`runlog.say()` 让「给用户看的一句话」在没有控制台时进日志；
`config._warn_once` 不再对 `None` 的 stderr 执行 `print`；`ConsoleSink` 在没有 stdout 时把 `tool`/`tool_result`/`error`
写进日志——以前这三行**直接消失**，连「Agent 到底调没调工具」都无从查起。

**音量**：`warn_once(key, interval)` 按 key 节流（默认 60s；GhostWorld 空转 600s），连上后 `forget(key)` 让下一次失败立刻可写。
一个闲着但开着开关的进程一天几十行，不是每 30s 一行。

**用户怎么拿到它**：房间托盘与 GUI 托盘菜单都有「打开日志」；帮助页最后一节「连不上、不听话的时候」带「打开日志目录」按钮。

**验收**：`tests/test_runlog.py`（12 例）钉住一天一个文件、每键节流、横幅不写 key、traceback 落地、无 stdout 时 sink 仍留痕、
14 天清理、只读目录回落。**真机探针**（真 hub + 真 RoomServer/RoomClient + 真 GhostWorld CLI，无 LLM）跑出的日志里逐条对上了
`hub listening on 0.0.0.0:53994`、`used the wrong token`、`WebUI listening on 0.0.0.0:53999`、
`hub http://127.0.0.1:9 request failed: … [WinError 10061]`（连打 5 次只 1 行）、
`channel --send ghostworld-send {"cmd": "pos"} (cwd=.) -> exit 2 stderr: … channel unreachable on port 60740`。
## 46. 增补（2026-09-17 用户点名）：`drag`——按住左键把东西搬过去

**用户原话**：「我想给 fungi 的桌控功能添加一个左键拖动能力：主要适用于需要将非文本内容输入文本框时的情况，
或者拖动窗口、图标等。」

脑暴时我提了一条「源与落点都是文件系统表面就拒绝」的护栏（理由是 §39 大规则写着「不点那一行、不按 F2、
不拖拽」），用户裁决：**「我什么时候说不允许拖拽了，就问你增删改查这几个和拖拽有重合吗？不要设限。」**
结论：**不设任何策略性拒绝**。§39 那条大规则管的是「文件的**移动/改名**」，而它和拖动唯一的真实重合是
「把文件落进一个文件系统表面」（那才是移动/复制）——这件事由落点归属如实报出来即可（见 §46.3 第二条），
不是工具该拦的事。

**本节推翻 §35.1 里「不做：…画图/拖拽」那半句**（双击当初也是这样被 §35.13 请回来的）。请回来的理由不是
「拖动好玩」，而是它补了一类别的动作都补不了的东西：**drop 不需要焦点**。§35.13 实测微信/QQ 的输入框
既没有 a11y 可寻址控件、OCR 与二值化也切不出来（空框没字、对比度不足），所以「往自绘输入框里送东西」过去
只有「焦点本来就在」这一条路；把东西**丢到窗口上**则绕开整个焦点问题。再加上「非文本内容」（文件、图片）
本来就不能靠 `type` 送进去（输入只有逐字一种方式，剪贴板永不动）。

### 46.1 形状：两端都由程序来读，模型只给相对位移

`click`/`type`/`scroll` 的先例（§35.1）照旧：**绝对坐标仍然不进 schema**。拖动因此是「一个程序读出来的抓取点
＋ 一个程序读出来的落点锚点 ＋ 模型给的相对位移」：

| 参数 | 含义 |
|---|---|
| `target` / `name` | 抓取点：源窗口里的候选（编号来自 `targets`，或可见文本） |
| `from="titlebar"` | 抓取点改取**窗口自己的标题栏**（`SM_CYCAPTION` 与窗口矩形算出来的；重定位窗口用） |
| `to_hwnd` | 落点所在窗口（默认 = `hwnd`） |
| `to_target` / `to_name` | 落点锚点：目标窗口里的候选 |
| **省略 `to_hwnd`** | **同窗口内搬运**：落点 = 抓取点本身（拖选择、拖滑块、拖窗口） |
| `dx` / `dy` | 落点的**相对**位移（物理像素），永远加在锚点上，从不解释成屏幕位置 |

三种锚点对应三种拖动：`to_target`（丢到一个控件上，如输入框）／给了 `to_hwnd` 没给目标（**丢到这个窗口上**，
落点 = 它客户区中心，也就是「文件丢进某个应用」）／什么都不给（**在同一个窗口里搬**，此时只有
`drop - grab` 这个差值有意义）。返回值里写明用了哪一种，例如
`to a carry inside the same window shifted by (+200,+40)`。

### 46.2 手势：按下 → 一段路径 → 悬停 → 松手

`drag_to()`（`fungi/tools/screen.py`）：滑到抓取点 → **核对真的到了**（`MOVE_TOLERANCE_PX`）→ 按下 → 停
`DRAG_HOLD_S`=0.08s（让应用收到按下）→ smoothstep 插值走 `DRAG_MIN_STEPS`..`DRAG_MAX_STEPS` 个点
（约每 12px 一个点、每点 12ms；太密的点流应用侧会合并，原型里标定过）→ 在落点悬停 `DRAG_DWELL_S`=0.25s
（drop 目标是靠悬停高亮的）→ 松手。与 §36 的平滑移动同源：**唯一一个「中间过程」被应用读到的动作**。

三件事会让它提前结束，三件都按人放弃拖动的方式收尾——**先发 Escape 再松键**（shell/OLE 的拖放循环里
Escape = 把东西还回去；不先松键是因为松键就已经落下去了）：

1. **停止键**（`should_abort`，每个点之前问一次，与 `type` 的每字一问同契约）；
2. **指针没到落点**（`_norm_point` 会把屏幕外的坐标 clamp 掉，实测 2026-09-14：x=-185 落到 0），
   宁可取消也不松在别处；
3. **`pre_release` 复核不过**：松手前再读一次「这个点现在归谁」，不是当初那个窗口就取消。

### 46.3 护栏（点级，不是策略级）

- **按下之前**：抓取点必须属于 `hwnd`（候选走 `target_problem`，标题栏走 `point_problem`），落点必须属于
  `to_hwnd`（`point_problem`：在窗口矩形内 + 该窗口就是这个点上的窗口）。任一条不过就**一个事件都不发**。
- **落点被别的窗口盖住**＝拒绝并报出盖住它的是谁（要过去的话有 `route="taskbar"`，见 §46.7）。拖动里「指针底下那个窗口」才是接收方，而两个窗口不能同时
  在最前面，所以重叠摆放时会出现「源能按、落点被盖」——这时先挪开一个，或把落点瞄到目标窗口露出来的部分。
  实测（2026-09-17）：探针窗口的客户区中心被最大化的资源管理器盖住时，工具报
  `stopped: the point (855, 835) belongs to 'dragtest - 文件资源管理器' (0x6A07DE), not to 0xEA09EA`，
  并在半路发 Escape 取消。**这不是策略拒绝，是「你让我丢到 B，但那个点是 A」。**
- **松手之前**再复核一次（同一函数）：拖动**经过任务栏**时 shell 会在悬停时切窗口，这条兜住它。
- **落到一个文件系统表面**会移动/复制文件——那是用户自己的事（§39 大规则管的是「别用像素路径做这件事」，
  这里如实报出落点归属即可），工具不加拒绝。
- **卡住的左键**：只有人能清的故障，和卡住的 Ctrl 同级。按下/松手都记账（`_held_buttons`），松手后问
  `GetAsyncKeyState`，系统说还按着就再抬一次；`_run` 的 `finally` 与 `disarm()` 都会抬。`_button_event`
  只在 SendInput 真的返回成功时才把按钮从账上划掉，所以「被吞掉的松手」不会被当成松好了。

### 46.4 真机实测（2026-09-17，2240x1400 @150%，自建靶子 `C:/tmp/drag_probe.py`）

靶子把**应用自己收到的**东西写进 `C:/tmp/drag_probe.jsonl`：`WM_LBUTTONDOWN` / `WM_MOUSEMOVE`（按下时
`SetCapture`）/ `WM_LBUTTONUP`（附 `EM_GETSEL`）、以及 `WM_DROPFILES`（只有真的跑了 OLE 拖放才会有）。
驱动脚本 `C:/tmp/drag_drive*.py` 把工具当模型用（`screen.bound(cfg, sink)`）。

| 形状 | 工具自述 | 应用自证 |
|---|---|---|
| 同窗口搬运（`target`+`dx/dy`） | `path: 25/25 points in 0.40s … button released (verified against the system)` | `down` @client(450,451) → **25 个 `move`**（缓入缓出的间距肉眼可读）→ `up` @client(650,530)，`moves_this_gesture: 25`；位移正好 +200/+79 逻辑 px = +300/+120 物理 px |
| 拖选择（Edit 内 `dx=+200`） | `verify: destination pixels changed 0.40% → verified` | 靶子轮询 `EM_GETSEL`：`[38,38] → [38,49] → [38,59]`（选区真的被拖出来了） |
| 重定位窗口（`from="titlebar"`） | `verify: the window moved (360,240,1710,1215) → (180,330,1530,1305) → verified` | 窗口矩形正好移动 (-180,+90)；抓取点 (1035,257) = 标题栏中线（`SM_CYCAPTION`=34 → 上沿 +17） |
| **文件丢进另一个应用**（资源管理器行 → 探针窗口） | `path: 48/48 … verify: destination pixels changed 0.26% → verified` | **`{"ev": "dropfiles", "count": 1, "names": ["C:\\tmp\\dragtest\\drag_source.txt"]}`** |

最后一行是脑暴时列的最大风险（「SendInput 造的拖动能不能启动 shell/OLE 的拖放循环，本机没测过」）的答案：
**能**。合成输入喂出的是完整的 OLE 拖放，落点应用拿到的是真实文件路径。

本轮真机还量到三条：

- **Win11 资源管理器的文件行不在本工具走的 a11y 树里**（`targets` 只列出标签页/地址栏/工具栏那些有名字的控件，
  文件列表一个都不出现），而且它不判自绘（窗口有带名字的 chrome 控件），所以**图片层也不进 listing**。
  那一行只能用 `name=`（走 OCR 兜底）够到，而 OCR 把 `drag_source.txt` 读成 **`drag_source. txt`**（多一个空格）
  ——所以按名字找文件行要用**片段**（`name="drag_source"`）。这条与 §35.11「OCR 的名字只是近似」是同一件事。
- **驱赶别的进程的窗口会失败**：`set_foreground` 对资源管理器返回假，结果里如实写
  `could not raise the source window to the foreground!`（按下仍然成功，因为那一行本来就露着）。这是既有行为，
  不是拖动引入的。
- **DPI-unaware 靶子自己的日志是 1.5 倍缩放的**（进程没声明 DPI 意识，Windows 给它缩放后的坐标），
  但**位移**对得上，事件条数也对得上——量靶子日志要用差值，别直接用绝对值。

### 46.5 边界（如实记录）

- **微信那次真机还没做**（用户选的：「微信等你到场那次再做」）：把文件拖进微信输入框、**绝不回车**。
  现在能做的是探针级的证明（上面第三行）。
- 拖动**不做**修饰键（Ctrl 复制 / Shift 移动）：用户没要，且按住修饰键贯穿整段手势会多一条收尾路径；
  要做的话 `_key_event` 已经能把键按住不放，加个 `keys` 参数即可。
- 拖动**不做**自由路径/贝塞尔/画图：原型里那套（`drag_path`/`bezier_points`）留在 bridge，不进仓库。
- 面板/跨虚拟桌面：落点不在屏幕上就会被 `point_problem` 或「没到落点」拦下，不会盲丢。

### 46.6 验收

- `tests/test_screen.py` 增 15 例：手势顺序（按下在路径前、松手在最后、落点精确）+ 自检计数、停止键
  （Escape 在松手之前 + 不留按下的键）、松手前复核不过就取消、落点被盖住时**一个事件都不发**、
  `_run` 的 finally 兜住「按下后抛异常」、`disarm` 抬起还按着的键、同窗口 dx/dy 的落点、
  跨窗口用两份 listing（这正是 `Session` 从「一份候选」改成「按 hwnd 各一份」的原因）、
  窗口矩形变化才是重定位的验证、自绘窗口整块表面**可以**当落点（与 `click` 的整块画布拒绝相对照）；
  以及 route 的四例（悬停到落点可达才继续 / 等不到就 Escape 取消 / 没有任务栏按钮就拒绝且不发事件 /
  `via_entry` 只认任务栏按钮不认托盘图标）和两个真机 bug 的回归例（**最小化窗口的图标槽不许当落点**、
  **窗口回来后要重新读落点**）。
- 门禁：`python -m ruff check .` 干净；`PYTHONIOENCODING=utf-8 python -m pytest tests -q` → 见 §46.8。

### 46.8 门禁与改动面

- `python -m ruff check .` 干净；`python -m ruff format --check .` 干净（没有整棵树重排，避免 §46 之外跟着漂）。
- `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **631 passed**（2:50）。
- 只有三个文件动过：`fungi/tools/screen.py`（+838）、`tests/test_screen.py`（+534）、`docs/spec.md`（+156）。
- 两个文件是**字节级**改的，改完确认过 CRLF（仓库 `core.autocrlf=true`；中途有一次被写成了 LF，已改回，`git diff --stat`
  在 CRLF/LF 之间无差别，但那行 warning 不值得留着）。

### 46.7 用户的两条 hint（2026-09-17 现场）：先拖到任务栏按钮上等它弹出来 / 或者 Ctrl+C 再 Ctrl+V

**用户原话**：「你刚才的操作给了我一定的启发：如果某一个窗口占满屏幕，解决方法是将文件拖拽到任务栏对应窗口的位置，
等待一会自动弹出该窗口，再拖进去。或者一种对于输入框的等价操作是：对这个文件 ctrl+c，再在输入框 ctrl+v——这是我们的
老本行，而我只是提出一种 insight 或者 hint，你可以将它写进 prompt。」

**第二条（Ctrl+C → Ctrl+V）写进 prompt 就够了**：工具面本来就能做（`click` 选中那一行或点进输入框 → `key ["ctrl","c"]`
→ 点输入框/会话 → `key ["ctrl","v"]`），所以它进的是 §46.1 那段描述。三条如实标注的代价（也都写进了描述）：剪贴板
会被占用并覆盖掉用户原来的内容；目标框得是接受粘贴的那种；发消息前仍然要先核对收件人（§35.13）。**这不改变 §37**：
「输入只有逐字一种方式」管的是**文本**——文本仍然逐字，而文件/图片的复制粘贴是另一回事（本来也没有逐字可言）。
`type` 自己的那句「剪贴板永不被触碰」照旧成立，它说的是 `type` 这条路由。

**这条也真机量过（2026-09-17）**：`click` 选中资源管理器那一行（`verify: focus matched`）→ `key ["ctrl","c"]` →
剪贴板格式从 `[13,16,1,7]`（纯文本系：CF_UNICODETEXT / CF_LOCALE / CF_TEXT / CF_OEMTEXT）变成含 **`15`（CF_HDROP）**
的 13 种格式 → **文件真的上了剪贴板**。再 `key ["ctrl","v"]` 打进一个 Win32 `Edit`：**文本**能进去（读回验证过），
**文件**进不去（裸 `Edit` 不接 CF_HDROP，什么都没发生）——所以「等价操作」的适用面是**接受粘贴/附件的那种输入框**
（聊天框正是），描述里那句「目标框得是接受粘贴的那种」就是这么来的。
脚手架顺手量到两件事：实验前剪贴板里是 `'终端 CLI'`，做完按文本还原成功（原格式 1/7/13/16 全是文本系，所以这次是完整的）；
**非文本格式（位图等）还原不了——走这条路线时要如实说**。

**第一条（拖到任务栏按钮上等它弹出来）工具面做不到，得加机制**：一条直线路径 + 一个落点表达不了「先悬停在按钮上、
等窗口起来、再继续」。于是加了 `route="taskbar"`：

- 把东西抬到目标窗口**自己的任务栏按钮**上按住不动，等最多 `DRAG_VIA_WAIT_S`=3s（每 0.15s 读一次）直到「落点现在
  真的能落」；等到之后**重新读一次落点**（从任务栏回来可能是「还原」，客户区中心会变），再从按钮拖进去、悬停、松手。
- 按钮从哪来：复用 §35.15 的「触手可及」解析，收窄成 `via_entry()`——只有**任务栏按钮**算，托盘图标不算（那个面板
  要点一下才开，而拖拽期间按键按不了）。没有任务栏按钮（只活在托盘里的应用）就如实拒绝，并且**一个事件都不发**。
- **带 `route` 时故意不先唤醒目的地**：没有 route 的拖动会先走 `_guarded_input(to_hwnd)`（`ShowWindow`/shell 那条路，
  §35.12），而 route 存在的意义正是走**应用自己的路**（shell 的悬停激活），所以那条预唤醒被跳过。
- 落点被盖住从此**不再只有「拒绝」一条**：换落点、搬开盖着它的窗口、或者走 route，三选一（§46.3 第一条）。

**真机实测（2026-09-17；源 = 资源管理器里的 `drag_source.txt`，目的地 = 探针窗口，两条路都跑通）**：

| 情形 | 工具自述 | 应用自证 |
|---|---|---|
| 目的地被最大化窗口盖住 | `via: its 任务栏按钮 'Python 3.13 (64-bit) - 1 个运行窗口' at (1187,1364) — hovered 0.45s until the drop point came within reach` | `{"ev": "dropfiles", "count": 1, "names": ["C:\\tmp\\dragtest\\drag_source.txt"]}` |
| **目的地最小化**（用户点名的场景） | `hovered 0.45s` · `verify: the destination came back on screen (was minimized, now normal) → verified` | 同上（路径一字不差） |

**这轮抓出两个只有真机能抓到的 bug（都已修 + 都加了指名回归用例）**：

1. **最小化窗口的几何是谎话**：`client_rect` 返回 0×0、`window_rect` 返回图标槽 `(-48000,-48000,…)`，而 `WindowFromPoint`
   在图标槽那个点上**居然答了那个窗口**——于是「落点可达」成立，东西被释放到图标槽上（指针被 clamp 到屏幕角落、
   动作自己取消，第一次实测的结果就是 `stopped: the pointer is at (2239,1399), not at the drop point (-48000,-48000)`）。
   修：等待条件**先问窗口状态**，只有 `normal` 才算可达（几何一个人说的话不算，§35.1 的老口径）。
2. **`wait_s` 写成默认参数会绑死在 import 时刻**：门禁里 monkeypatch 模块常量无效——正是 §35 那套「超时必须在调用时
   读模块常量」的老坑（`blocking_ask` 有一条同样的注释）。修：`wait_s=None` → 函数体内取 `DRAG_VIA_WAIT_S`。
3. 顺带：探针窗口是被我自己的终端盖住的那次也说明了同一件事——**agent 自己的终端就是屏幕上最碍事的那个窗口**，
   验证前先把它挪到角落（脚手架，不是被测对象）。

## 47. 修复（2026-09-18 用户真机反馈）：大文件发不出去——「进度条从 0 反复、最终 Failed to fetch」

**用户原话**：「我发现 fungi 在传送大文件的时候，会出现下载一点点，然后进度条重新从 0 开始，反复很多次，最终 fail to
fetch 的情况。」三个现象：**从 0 重来**、**反复多次**、**最后是 fetch 失败**（不是「文件太大」这种话）。全部按事实复现了。

### 47.1 复现：两个角色、三种异常，一个共同点

两只真房间（alpha 宿主 + beta 加入）、真浏览器、真 `Xfer` 模态，把 hub 上限压到 2 MiB 好让 5 MiB 的文件「太大」：

| 谁在发 | 服务端发生了什么 | 页面看到 |
|---|---|---|
| 宿主房间（自己的 hub，进程内） | `Transfers.stage_from` 抛 `ValueError: file too large` → `Hub.upload_transfer` → `LocalTransport`（只捕 `OSError`）→ `comm_send_human` → `do_POST`，**一路没人接** | `Failed to fetch` |
| 客户端房间（hub 在对面，走 HTTP） | hub 先答 413 就不再读 body，客户端往死掉的 socket 里接着发 → `ConnectionAbortedError: WinError 10053` 逃出 `HubClient.upload_transfer` | `Failed to fetch` |
| 同一件事的另一半 | `data/transfers/<id>__<name>` 被写到上限（默认 200 MiB）**再删掉**——用户当天看到的就是这个目录一直在动 | — |

**「从 0 重来」的来源量到了**：一次 `Xfer.sendOne` 在服务端**到过 2–3 次**（探针按 job id 数），**job id 一模一样**——
是 Chromium 在「连接被关掉、一个字节响应都没收到」时**静默重发同一个 POST**。重发到了 `/comm-send` 就是
`xfer_jobs.start(job, …)`，`done` 归零 → 轮询把 0 写回进度条 → **用户看见条子回到 0**。所以「反复很多次」不是用户手抖，
是浏览器自己在重试；重到 6–7 次后它放弃，页面显示的就是最后那个 `Failed to fetch`。

**还有一个把线索吃掉的地方**：`WebUIServer.handle_error` 会静默掉 `ConnectionAbortedError`/`ConnectionResetError`
（手机浏览器天天开一堆推测连接又 reset，这是对的）——于是客户端角色那次失败**连日志都没有**，是探针在
`BaseServer.handle_error` 上挂了一层才看见的。

### 47.2 修复：拒绝要当成一句话说出来，而不是把连接掐掉

1. **hub 端按声明尺寸先拒**（`hub/app.py::_Handler._transfer_upload`）：`Content-Length` 一读出来就问
   `transfers.refusal()`，超了**先答 413、再排空 body**（顺序重要：先答，发送方一个 RTT 内就读到并停手，排空只见 EOF）。
   `refusal()/too_large_message()` 是新的唯一措辞：`file too large: 16 MB (limit 2 MB)`——两个数都写出来，
   用户才知道该动哪个旋钮（`/upload` 的 413 也从光秃秃的 `file too large` 改成同一句）。**一个字节都不再落盘**。
2. **进程内那条路不抛异常**（`clone/base.py::LocalTransport.upload_transfer`）：先 `refusal(total)` → 返回 `{"error": …}`；
   `ValueError` 仍兜一层（文件在传输中长大）。这一条就是用户那台（自己开 hub）的病根。
3. **客户端会听早到的回复**（`hub/client.py::HubClient.upload_transfer`）：`endheaders()` 之后先 `_answered(conn, 50ms)`
   瞄一眼，body 里每 256 KiB 再瞄一次（`select` 0 超时）；看见回复就停止发送去 `getresponse()`。
   没有这一步，被拒的 16 MiB 会**一个不少地灌进排空循环**（实测 sent=16777216），有了它是 **sent=0 B、0.01s**。
   发送途中断链（`OSError`/`HTTPException`）也变成 `{"error": …}`，不再掀翻请求线程。
4. **兜底：任何路由都必须给出回答**（`server.py::YesSirHandler._answer`）：`do_GET/do_POST/do_DELETE` 改成
   薄壳 + `_get_routes/_post_routes/_delete_routes`，异常 → 500 `{"error": "<类型>: <消息>"}` 并写 `runlog.problem`，
   读完就走的读者只关连接。**「连接没了」在浏览器里和「网络坏了」是同一件事**，这一层让下一类 bug 变成页面上的一句话，
   而不是又一个 `Failed to fetch`。

### 47.3 验收

- 新用例两条，都先跑红：`tests/test_webui_transfer.py::test_a_file_over_the_cap_is_refused_by_name_from_either_role`
  （两个角色各发一次超限文件，断言模态里是 `file too large`、job 记录 `state=error`、`transfers/` 里一个文件都没有；还原成修复前它以
  `note: 'Failed to fetch'` 失败）和 `tests/test_webui_gate.py::test_a_route_that_raises_still_answers`
  （还原后 `http.client.RemoteDisconnected` → 修复后 500 + JSON）。
- 真机数字：5 MiB / 2 MiB 上限 → `file too large: 5 MB (limit 2 MB)`，服务端**到达 1 次**（不再重发，条子不再归零），
  未处理异常 0；客户端角色 8 MiB → 同样一句、1 次到达；16 MiB / 2 MiB 上限 → **sent=0 B**、0.01s、`transfers/` 连目录都不建。
- 上限内的传输不受影响：1 MiB 文件仍然 100% + `已发出，等待对方接收`，commlog 有 `[file]` 那行。
- 改这份 `.md` 照旧**字节级替换**：先把文件归一成 LF 再转回 CRLF，别在已经带 CRLF 的文本上再替换一次换行——混进 3 行 `CR+CR+LF` 就够让 `git diff` 把**整个文件**记成改动（实测 1956 增 / 1908 删，看着像全重写，其实只差那 3 行）。
- 门禁：`ruff check --fix fungi tests` / `ruff format --check fungi tests` / `ruff check fungi tests` 全干净；`PYTHONIOENCODING=utf-8 python -m pytest -q` → **633 passed**（3:05，比 §46.8 的 631 多两条新用例）。
- 改动面：`fungi/hub/app.py`、`fungi/hub/client.py`、`fungi/clone/base.py`、`fungi/server.py` + 两个测试文件；`docs/spec.md` 只追加本节（CRLF 保持，`git diff --stat` 是 56 行新增，没有整树漂移）。

**边界（如实记）**：上限本身在 2026-09-18 被用户裁决取消（§48）；这一节留下的两件事仍然成立、也仍然必要：
「每个请求都要有回答」（`_answer`）与「客户端会听早到的回复」（`_answered`）——现在它们守的是**另一种**提前拒绝
（暂存盘写不进去，hub 答 507）。大文件该怎么慢还怎么慢（发送是同步的、`/comm-send` 要等到字节全部进 hub 才回），这次没动它。

## 48. 用户裁决（2026-09-18）：文件传输**不设上限**

**用户原话**：「我认为不应该设置上限。」——那条 `max_file_mb`（默认 200 MB）取消，不是调大。

**为什么原来会有上限**：不是产品判断，是实现的形状——手机那条 `/upload` 把**整个 body 读进内存**
（`self.rfile.read(length)`）再拆 multipart，一个 3 GB 的视频就是 3 GB（还要再复制一份）在 16 GB 的机器上；上限是那道
保护。所以**取消上限必须先换掉这条实现**，否则「没有上限」只是把失败从「413」挪到「MemoryError」。

**换掉了什么**（一条都不留）：

- `web/common.js::upload`：`xhr.send(file)` —— **裸字节**，文件名走 `X-Fungi-Filename`（percent-encoded）。
  `/upload` 服务端按 64 KiB 流式写进 inbox，**不再拆 multipart**（`_extract_upload` 整块删掉）。
  顺带修好一件旧账：multipart 的 filename 走 latin-1，中文名会被搅坏；header 里 percent-encode 之后就没事了。
- `config.max_file_mb` 字段删除；`config.json` 里还留着这个键时**只在日志/控制台说一次**「已废止」，不静默吞掉
  （有人把它设小是为了护盘，不能让他以为还生效）。
- `hub/app.py::Transfers` 无 cap：`stage_from` 仍按 256 KiB 分块写盘，唯一的界限是磁盘空间；写不进去是
  `OSError`，HTTP 层答 **507 + `staging failed: …` 并排空 body**（发送方正在发 body，只有排空它才读得到这句）。
- `LocalTransport` / `HubClient` 仍然一句 `{"error": …}` 就返回，绝不把异常掀出请求线程（§47 的老账）。
- `_answered()` 的 peek 与 `_answer` 兜底**留着**：它们现在守的是上面那种提前拒绝（磁盘满），不再是尺寸上限。

**顺手抓到并修掉一个和上限无关的旧 bug**（量出来的，不是看出来的）：接收方下载完要 `DELETE /api/transfer` 丢掉 hub 上的
暂存副本，但**客户端把 token 放在 JSON body 里，而 hub 的这个路由从 query 里读 token**——于是**每一次** discard 都被
403 掉，两个调用点又都用 `contextlib.suppress(Exception)` 包着，静默无声。后果：**每成功送出一份文件，hub 盘上都留着
一整份**（300 MB 就 300 MB，重启才清）。修法是把 `do_DELETE` 改成和 `do_POST` 一样从 body 读 token（客户端是唯一调用者）。

**验收（真机数字，两角色各一次 + 手机那条）**：

| 场景 | 结果 |
|---|---|
| 宿主角色发 **300 MiB** | 入 hub 0.3s · 送达 2.0s · **sha256 一致** · 送完 hub 暂存 **0** |
| 客户端角色发 **300 MiB**（走 HTTP 到 hub） | 入 hub 1.2s · 送达 1.7s · **sha256 一致** · 暂存 **0** |
| 手机上传 **300 MiB**（裸字节，客户端从盘上分块发） | 0.9s 落盘 · **Python 堆峰值 0.3 MiB** · sha256 一致 |

- 新用例：`tests/test_room.py::test_a_delivered_transfer_lands_whole_and_drops_the_staged_copy`（两个方向都真发一次，
  哈希一致 + 暂存清空）、`tests/test_hub_app.py::test_transfer_discard_actually_drops_the_staged_copy`（路由契约：只有
  收件人 + 带 token 才能丢）、`tests/test_webui_upload.py::test_a_big_upload_is_streamed_not_buffered`
  （24 MiB body，Python 堆峰值必须 < 1/4；把缓冲改回去实测 **25,217,863 B** 复现红）、
  `tests/test_webui_transfer.py::test_a_big_file_goes_through_from_either_role`（4 MiB 两角色都 100% 且整份落 hub）。
- 门禁：`ruff check --fix fungi tests` / `ruff format --check fungi tests` / `ruff check fungi tests` 干净；
  `PYTHONIOENCODING=utf-8 python -m pytest -q` → **634 passed**（2:54，§47 那次是 633）。
- `web/common.js` 的 build marker 跟着改成 `web-raw-upload`（页面自报版本，线上遇到的旧页面一眼能认）；
  旧页面往新 `/upload` 发 multipart 时服务端答的是「reload the page」，不是一句 header 名。

**边界（如实记，这是「无上限」的代价）**：字节是**先落 hub 暂存、后问收件人**的，所以房间成员能写到**房主**的盘上，
唯一的门是房间 token（§8 的 LAN/熟人假设）。以前 200 MB 那道墙顺手拦住了这件事，现在没有墙了：真正兜底的是磁盘空间，
失败会说出来（507 / `cannot transfer …: [Errno 28]`），不会再变成一句 `Failed to fetch`。接收方落盘
`inbox/<来源host>/` 同样不设限。

## 49. 收件端落地（2026-09-19 用户真机反馈）：WinRAR 说「不可预料的压缩文件末端」

**现场**：`C:\Users\37549\Desktop\AndroidStudio.rar`（1,286,738,079 B）从本机（`OwO`，房间宿主）发给另一台 `pc`，
对方 WinRAR 报「不可预料的压缩文件末端」。他跟进了一句，把最容易走偏的解释堵死：**「不是路径问题，我后来换了个可以访问的路径也是这样」**。

### 49.1 对照试验（本机，先把「是不是传坏了字节」问死）

| 环节 | 大小（B） | 校验 | 判定 |
|---|---|---|---|
| 源文件（`UnRAR t` 退出码 0） | 1,286,738,079 | sha256 `f19cbc27…94d1` | 源本身完好 |
| 他这次留下的 3 份 hub 暂存副本 | 1,286,738,079 ×3 | md5 三份全等于源 | 发送端 → hub **逐字节相同（3/3）** |
| 对照·localhost（真代码路径，上传 4.9s / 下载 4.3s） | 1,286,738,079 | sha256 与源一致 | **相同** |
| 对照·走真实网卡（`192.168.0.104`） | 1,286,738,079 | sha256 一致，落地那份 `UnRAR t` 退出码 0 | **相同** |
| 对照·下载中途杀掉 hub 进程 | **407,568,384（31.7%）** | 前 407,568,384 字节与源逐字节相同 | 终名上留下**截断文件**，`UnRAR t` 退出码 3 |

结论一分为二：**传完的传输一个字节都不差**；**没传完的那次会在收件人盘上留下一个名字正常、内容是源文件前缀的截断文件**——
这正是 WinRAR 那句话的来源。而整条链路上**没有任何长度或校验检查**，所以发现它的只有 WinRAR。

### 49.2 他机器上到底发生了什么（都是他自己的证据）

`data/comm/OwO__pc.jsonl`：

```
08:37:31 发送 → 08:37:32 pc 回 [WinError 5] 拒绝访问。: 'C:\Program Files (x86)\Fungi\Fungi\inbox'
08:38:03 发送 → 08:38:03 pc 回同一句
08:41:03 发送 → 没有回音
08:45:20 hub 停止（他关掉了房间）
```

- 前两次死在收件端：`pc` 的 Fungi 装在 `C:\Program Files (x86)`，`inbox` 建不出来（`Path.mkdir` 报的是第一个失败的父目录，
  所以路径里没有 `OwO`）。换到可写路径之后这个错就没了——但那不是事故的根因。
- 第三次 08:41:03 发出，**4 分 17 秒后房间被关**，收件端的下载还在跑：套接字复位（本机复现到的是
  `ConnectionResetError [WinError 10054]`），半截文件留在 `inbox/OwO/AndroidStudio.rar`。
- 三份暂存都还在 hub 上（**成功送达才会 discard**）→ 三次都没有送完。`pc` 不在本机，它盘上那份没法直接哈希，
  这一句是推断，其余都是实测。

**还有一半原因是发送端说的**：`web/common.js` 的弹窗在**上传到 hub** 就 `finish('已发出，等待对方接收')`，
而这台机器同时是房间宿主，1.28 GB 走回环只要 5 秒——进度条瞬间 100%，真正搬去 `pc` 的那一份在收件端 clone 里悄悄跑，
**发送端既没有进度、失败也只在会话里留一行**。于是「看起来已经完成 → 关应用 → 收件人打开半截文件」。

### 49.3 改动（四条）

1. **原子落地 + 长度校验**（新 `fungi/landing.py::atomic_landing`）：字节先写 `<name>.part`，写完比对发送端声明的字节数
   （HTTP 的 `Content-Length`，本机直连是暂存记录里的 `size`），一致才 `replace` 成真名；任何失败（截断、断连、磁盘满）
   都删掉 part 文件。**终名下永远不会出现不是「发送端那份」的东西**。两条传输路径都走它
   （`HubClient.download_transfer` 与 `LocalTransport.download_transfer`）。
2. **收件端的判决要回到发送端**：transfer envelope 带上浏览器铸的 `job` id，收件端把它**原样**带回来——两种回答形状都带
   （信使在：`result`；信使关：房间 `_send_answer` 的 `answer`，`value` 之外并列一个 `job`，§43 起的两种形状都覆盖）。
   发送端的 comm clone 在 `dispatch` 里认出它，交给 `TransferJobs.deliver()`；失败同时写一行 `runlog.problem`
   （以前**只有收件端知道**为什么没落成，发送端的日志里一个字都没有）。
3. **弹窗等的是对方**（状态机：`running/upload → sent/deliver → done|error`，多一个 `phase` 让页面知道该画哪一步）：
   桌面两步「① 上传到房间 ② 对方接收」，手机三步「① 上传到电脑 ② 上传到房间 ③ 对方接收」。
   `done` 只由收件端的判决给出（带落点路径），失败留在屏幕上带原因；**页面自己的 await 不跟着送到**——
   它在上传结束时返回（会话照旧刷新），盯判决的泵是**分离的**，否则调用方会卡到对方点卡片、或者永远卡住。
4. **inbox 目录可写**（`config.writable_dir`，与日志目录同一套规则）：配置里写了 `inbox_dir` 就照用；
   否则先试 exe 旁边的 `inbox`，写不进去就退到 `%LOCALAPPDATA%\\Fungi\\inbox`，并在日志里说一次。
   **没有用自动 UAC**，三个理由：UAC 不能静默（每台新机器、每个新用户都要点一次）、把整个 GUI 提到管理员会让
   「从资源管理器拖文件进来」失效且 `data/`、`config.json` 变成管理员所有（之后非提权启动又写不动）、而且**根本不需要**——
   按用户的数据本来就该放 `%LOCALAPPDATA%`（日志 §45 已经这么做）。

### 49.4 边界（如实记）

- **校验的是长度，不是哈希**。协议不动（用户 2026-09-18 的裁决），所以没有握手级别的 checksum：长度能抓住的唯一
  故障是截断，而 TCP 不会翻转字节——这正是实测里唯一出现过的那种坏法（31.7% 前缀）。
- **发送端的应用关掉之后，收件人的失败没人能告诉它**：判决要过 hub。所以弹窗的最后一步在没人回答时会停在
  「对方仍在接收，结果会出现在会话里」，不会假装成功，也不会永远转圈。
- `urlopen(timeout=120)` 仍然是每次 socket 操作的 120 秒：链路停顿超过它就会放弃——现在放弃是**安全**的（part 文件被删掉、
  错误照原话回到发送端），重传不会再和上一次的残骸混在一起。
- 半截文件不会再出现，但**「送出即失败」依然可能**：房间 token 就是唯一的门（§8 的 LAN/熟人假设不变）。

### 49.5 验收

- 新用例 `tests/test_landing.py`（8 例）：完整落地不留 part · 短交付不落地（报 `written/expected`）·
  半路断连保住上一份好文件 · 无 `Content-Length` 也落地 · HTTP 半路断连不留东西（假 hub 先承诺 1 MiB 再掐线）·
  exe 旁边可写时用它 · 只读时退到 `%LOCALAPPDATA%` · 显式 `inbox_dir` 原样使用。
- `tests/test_friend_send.py`：job 上传完停在 `sent/deliver`（不再 `done`）· 收件端判决收尾（成功带落点、失败带原因）·
  两种回答形状都认 · 代理发的传输不带 job id · 信使关掉时 `answer` 也带 job id。
- `tests/test_webui_transfer.py`（真浏览器）：桌面两步、手机三步、**收件人点「同意」之后**才出现「对方已收到」并自动关闭 ·
  拒绝时留在屏幕上并写明原因 · 上传失败画在**出错的那一步**上 · 4 MiB 两角色都整份落到对方盘上（逐字节比对），
  送完 hub 暂存清空。
- 门禁：`python -m ruff check .` 干净；`PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **652 passed**（3:12，§48 那次是 634）。


### 49.6 用户真机现场（2026-09-19 晚）：「对方接受是何意味、为什么进度条卡死、对面为什么是 .part」

三句话、三个答案：前两句是设计，第三句是我这边的缺陷。

- **`.part` 是对的**：字节先落 `<名字>.<staged id 前 8 位>.part`，写完且长度对得上才改名成真名（49.3 第 1 条）。
  **名字里带 staged id** 是这一晚补的：他连发了两次同一份文件，两次落地会去写同一个 `.part`，而 Windows 上
  先完成的那次改名还会被另一个打开着的句柄挡掉（后落地的那份于是收到一句莫名的错误）。各写各的 part 之后，
  并发、重试、崩溃残留互不干扰。
- **「对方接收」那一步等的是收件端自己那台机器的人点同意**（落地前收件人必须点头，从 §10 起就是这样），
  点了之后 hub 才开始把字节推过去。发送端**看不到**「等同意」与「正在收」的区别 —— 只能看到「还没落地」。
- **卡死是真缺陷**：第二步当时背后什么都没有。实测他此刻的状态：`pc`（192.168.0.113）两条 ESTABLISHED、
  宿主 NIC **6.8 MB/s**（两份在共享带宽，各约 3.4 MB/s）→ 1.29 GB 要 6 分钟以上，而界面一动不动。

补的是第三条那条腿 —— **hub 自己记账，页面画它**：

- `Transfers` 的记录多一个 `sent`（每 1 MiB 报一次，`Transfers.moved`）；下载路由从 `shutil.copyfileobj`
  换成计数循环，字节的搬运方式没变，只是多记账。
- 新路由 `GET /api/transfer/progress?id&host&token` → `{sent, total}`，**只有这单 transfer 的 src 与 dst 能问**
  （第三个 host、未知 id 都是 404）。这不是协议变更：envelope 一个字段没动，房间 token 仍是那扇门。
- 房间把 job 的 `tid` 与 hub 的计数合并进 `/transfer-progress`：第二步于是显示
  `0 B / 1.29 GB · 0%`（对方还没点头）→ 真字节（正在收）→ 判决。没有计数时才回落到「等待对方接收…」。
- 用例：`test_hub_app.py::test_transfer_progress_counts_what_the_hub_handed_over`（计数到齐 + 第三方 404）、
  `test_room.py::test_the_senders_job_shows_what_the_hub_handed_the_peer`（点同意前 `delivered == 0`、
  `delivery_total == 文件大小`，点完变 `done` 且落盘逐字节相同）、
  `test_webui_transfer.py` 桌面与手机两条：**收件人点同意之前**，最后一步的 note 必须已经是字节形态。

## 50. 传输提速（2026-09-19 用户裁决 Option 2）：Range + 多流 + 断点续传

**先说清楚这一节改的是哪一段**：一次「发送」是三段腿（§49.1），只有第二段（hub → 收件端）真的过网络，
而三段的速度差着五十倍（本机回环 268 MB/s vs 2.4 GHz WLAN 5.05 MB/s）。所以这里动的是**第二段**：
让它能并发几条 TCP 流、并且断了能接着传。**链路本身仍是那个上限**（`netsh wlan show interfaces` 实测
2.4 GHz · 802.11n · 40 Mbit/s），换 5 GHz 或网线才是唯一能把它压到 20 秒级的手段 —— 代码最多值 ×1.3。

### 50.1 依据：一次观测（受控复测见 50.7）

| 流数 | 合计 | 单流 |
|---|---|---|
| 1（正常发送一次） | 5.05 MB/s | 5.05 |
| 2（用户手滑发了两次） | **6.8 MB/s** | ~3.4 |

单流没吃满链路 → 加流有约 +35% 空间。这是**一次观测**，所以 50.7 那次受控复测是必做的。

### 50.2 hub 侧的 Range 契约（只读路由，协议没动）

`GET /api/transfer?id&host&token` 现在认 `Range`：

- `bytes=a-b` / `bytes=a-` → **206** + `Content-Range: bytes a-b/size` + `Content-Length: b-a+1`，只流这一段；
- 没有 `Range` → **200** 全量（老行为一个字节没变）；两种回答都带 `Accept-Ranges: bytes`；
- 起点越过末尾 → **416** + `Content-Range: bytes */size`，一个字节都不发；
- 多段、后缀式（`bytes=-500`）、别的单位、`a>b` → **忽略 Range，按 200 处理**（RFC 9110 允许服务器忽略它
  看不懂的 Range；猜一个才是把错字节发出去的路）。末字节超过文件末尾按 RFC 夹到 `size-1`。
- 授权不变：`fetchable()` 仍然只放行这单 transfer 的 dst。

### 50.3 记账改成「窗口的并集」（页面与路由形状都不用改）

`sent` 原来是一个单调总数，多流一来就废：窗口乱序到达，窗口重试会把同一段报第二遍。
于是 `Transfers.moved()` 换成 `deliver_window(tid, start, end)`，内部是 `landing.Spans`（区间的并集）：

- **单调由构造保证**（并集只增不减），重试不重复计数，`sent` 也不会超过 `total`（写满即封顶）；
- 整份下载（一个窗口 `[0, size)`）与多流下载落在同一个尺度上，`sent` 的含义与 §49.6 完全一致；
- 对外仍是 `GET /api/transfer/progress` → `{sent, total}`，仍然只有这单的 src/dst 能问。

### 50.4 收件端：一次落地、多个窗口、窗口内续传

`HubClient.download_transfer()` 先问 `/api/transfer/progress` 拿 `total`，把 `[0, size)` 切成最多 **4** 个窗口
（每个不小于 4 MiB —— 小文件自然退化成单流），窗口 0 在本线程、其余各一个线程，窗口 0 那次请求顺便回答
「这个 hub 会不会 Range」。

- **一个 part 文件、就地写**：各窗口 `seek` 到自己的偏移写进同一个 `<名字>.<staged id 前 8 位>.part`，
  收齐再改名 —— 没有把窗口拼起来的第二份拷贝（1 GB 就是 1 GB），名字规则与 §49 一模一样：
  断了仍然只留一个 part 文件，无论几条流。`atomic_landing`（§49）保留为 `Landing` 的单写者外壳，
  本机直连那条路（`LocalTransport`）仍走它。
- **窗口内重试就是第一层续传**：窗口断了，从**盘上真到的位置**（`Spans.end_of_run`）接着要，而不是从头再来；
  每个窗口 3 次机会、间隔 0.5s/1s。某窗口用尽机会 → 通知其余窗口停下，整份不落地。
- **落地判定没有放松**：`Landing.commit()` 要求 ① 长度等于声明值 ② **每个字节都有窗口认领** ——
  长度对但中间有洞也拒绝（洞是「某个窗口没跑」的痕迹，而最后一个窗口会把文件撑到正确的大小）。
  任何失败路径都删掉 part：§49 的铁律不变。

### 50.5 新旧混跑

- 新 hub + 旧收件端：旧端不问 Range，走 200 全量，行为与 v0.7.3 完全相同。
- 旧 hub + 新收件端：第一条窗口请求会收到 200 全量，**那个响应就是单流**（直接用它，不重复下载一遍）。
- 连 `/api/transfer/progress` 都没有的 hub（早于 §49.6）：收件端退回单流。

### 50.6 这次没做（如实记）

- **跨次续传**（收件端重启后接着传同一份）留二期：需要新的 staged id 与「hub 留着暂存」的双向约定，
  本版只做「窗口内」。
- **协议不动**（用户 2026-09-18 的裁决）：envelope 字段一个没改，新增的只有 hub 的 HTTP 只读路由。
- **没有加配置项**：4 条流是代码里的默认值（`hub/client.py::WINDOWS`）。50.7 的复测由测量脚本直接传
  `streams=`，于是不必为一次实验在产品面上多一个开关。
- 压缩、WebSocket、浏览器代下载：§49.1 已排除，没再碰。

### 50.7 验收

- 新用例：
  - `tests/test_hub_app.py`：窗口逐字节（状态码 + `Content-Range` + 长度 + 内容）· 开区间与按 RFC 夹末字节 ·
    越界 416 且不发一个字节 · 忽略看不懂的 Range（多段/后缀/别的单位/`a>b`）· 只有 src/dst 能要 Range ·
    **进度是窗口的并集**（乱序到达 + 重叠重试后仍等于文件大小、且重问不变）· 真 socket 上 12 MiB 三窗口
    整份落地、计数到齐。
  - `tests/test_hub_client.py`（新）：切窗规则（小文件退化成单流、切得精确且不越界）· 多窗口逐字节落地且不留 part ·
    **断掉的窗口从断点续传**（下一次请求的起点 = 上一次真正到达的字节）· 反复失败则一个字节都不落地 ·
    旧 hub（忽略 Range / 没有 progress 路由）仍然收到整份文件。
  - `tests/test_landing.py`：多窗口乱序各写自己那一段 → 落地逐字节相同 · **长度对但中间有洞也不落地** ·
    `Spans` 是并集不是累计（乱序、相接、重复上报、越窗重试）。
- 本机回环跑真实代码路径（`AndroidStudio.rar`，1,286,738,079 B，两轮 sha256 都等于源 `f19cbc27…94d1`，
  4 流那份 `UnRAR t` 退出码 0、落地目录不留 part）：1 流 5.42s / 237 MB/s，4 流 5.06s / 254 MB/s。
  **这组数字只证明实现没坏**：回环上磁盘才是瓶颈，多流的收益在回环上不存在。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **666 passed**（198s，§49 那次是 652）。
- **受控复测（1/2/4 流走真 WLAN）待做**：写这一节时 `pc`（192.168.0.113）不可达（ping 超时），
  两台都在线之后按 §50.1 补三行数字（脚本 `C:/tmp/scratch/fungi-xfer-speed/streams.py`：
  在 `pc` 上把大文件发往 `OwO` 把字节暂存在 pc 的 hub 上，本机戴着 `--id` 拉 1/2/4 次，
  同时差分本机网卡 `ReceivedBytes` 证明字节真的过了网卡）。

## 51. 手机上传（2026-09-19 用户追问）：一条腿的窗口化，以及「终名下半个文件」在这一层的修复

**触发**：用户问「从移动端上传到服务器的你也做分流吗」——没有，§50 只改了 hub→收件端那一段。这一节把手机这一段补上。
先把边界说清：Fungi 里手机**只有上传**一条腿（`fungi/server.py` 的 GET 路由表里没有任何 file-serving 路由，
`web/m.js` 里也没有 Blob/download 的影子），所以「手机端分流」在那时只可能是上传。

### 51.1 这一层原来有什么缺陷（同一个 §49 缺陷类，早了一跳）

`_handle_upload` 直接 `dest.open("wb")` **写终名**，只在「抛异常」和「长度不够」两条路上才 `unlink`。于是手机传一半、
电脑上的 Fungi 被杀掉（或断电），`inbox/` 里就留一个名字正常、内容是半截的文件 —— 与 §49 里 WinRAR 抱怨的同一件事，
只是发生在手机这一跳。现在换成 §49 的规矩：字节先写 `<名字>.<sid>.part`，**长度对 + 每个字节都有窗口认领**才 rename。

### 51.2 协议形状（三个形状，新旧都能跑）

| 请求 | 含义 |
|---|---|
| `POST /upload`（无 query） | 老行为：整个文件一个 body。**一个字节没变**（老页面、curl、脚本继续能用），落地改走 part + rename |
| `POST /upload?sid&offset&size` | 一个窗口：body 是 `[offset, offset+len)`；`sid` 由浏览器铸，同一份文件的所有窗口共用一个 |
| `GET /upload` | 能力探针：新 host 回 `{ok, parts:true, min_part}`；老 host 没有这条 GET → 页面回落到单 body |
| `GET /upload?sid=` | 这一单的现状 `{done, received, total, missing:[[lo,hi],…]}`；覆盖已满时顺手落地并回 `path` |

- 落地判据 = **长度 == 声明值 且 `[0,size)` 全覆盖**（`landing.Spans` 的并集）；同一窗口重发不重复计数，计数也不倒退。
- `sid` 会进文件名 → 只收 `[A-Za-z0-9_-]{1,64}`；**给了一个不合法的 sid 就 400，绝不悄悄退化成「那这个 body 就是整个文件」**。
- 名字在**落地那一刻**才挑（`_inbox_path` 的 `-1` 编号）：传输途中别人落了同名文件，不会互相覆盖。
- 会话 30 分钟没人碰就扫掉（连同 part）；进程被杀留下的 `.part` 会留在 inbox —— 名字带 sid，不冒充真名，
  也不会挡下一次上传（与 §49 的 part 命名同一套）。
- 写盘仍是 64 KiB 流式，§48 那条「24 MiB 上传堆峰值 < 6 MiB」的用例继续过。

### 51.3 手机页那一半（`web/common.js::Xfer.upload`）

- 切窗规则与 §50 一致：每窗口 ≥ 4 MiB、最多 4 条，小文件自适应退化成单流；页面 URL 上挂 `?parts=N` 可以钉住条数
  （**测量用的旋钮**：`?parts=1` 就是改造前的一条流）。
- 每条各自重试 3 次（0.5s/1s 退避；重试时这条从头再来，进度计数取峰值 → 只增不减）。
- **谁说了算**：全部窗口发完之后页面**问 host** ——`missing` 为空才算成，不空就只补那几段（最多 3 轮）。
  这样「socket 说发出去了」与「host 真收到了」之间那道缝（中途断连、body 被截）不用猜。
- 老 host（没有 `GET /upload`）→ 回落成一条 body，行为与改造前一致。
- 一个踩过的坑写在这里：**query 必须放进传给 `url()` 的 path 里**。页面的 token 前缀会自己追加 `?t=…` 或 `&t=…`，
  我自己再拼一个 `?sid=` 就变成 `…?t=TOKEN?sid=…` —— token 被吞进 sid，整条流变 403（浏览器测试抓到的）。

### 51.4 没做

- **手机下载**（电脑→手机）在这一节里仍然不存在：见 §52，那是另一条腿，用户当晚点名要开。
- 手机端并发条数**没有真机实测**（与 §50.7 同一件事）：手机上行是否单连接吃不满，得在手机上量 1/2/4 条。

### 51.5 验收

- `tests/test_webui_upload.py`（19 例，§48 那次是 11）：能力探针 · 乱序窗口逐字节落地且不留 part ·
  同一窗口重发不重复计数 · 缺一段时**终名下什么都没有**且 `missing` 精确 · 截断的窗口保留已到的字节并报出缺哪一段
  （单 body 那条仍然删掉不留）· 落名在 commit 时决定（`-1` 编号）· 越界/非法 sid/空 sid 一律 400 ·
  被扫掉的会话连 part 一起没、状态回 404 · 单 body 路径与 24 MiB 堆峰值不变。
- `tests/test_webui_transfer.py::test_mobile_upload_cuts_a_big_file_into_windows`（真 Chromium 真页面）：
  12 MiB 从手机页发出 → 观测到页面真的发了 3 个互不重叠、每 4 MiB 一段、首段从 0 起的 `?offset=` POST +
  一次能力探针，落盘逐字节相同、不留 part，页面把落点路径写进了输入框。

## 52. 手机下载（2026-09-19 用户点名）：PC → 手机这条腿，同样支持分流

**触发**：用户问「手机端下载服务器的文件呢？有分流吗」，然后拍板「我真的想开这条腿，都支持分流」。
这条腿在 Fungi 里原本**不存在**（不是慢，是没有）：手机页只能上传，看得到文件路径、拿不到字节。

### 52.1 路由与权限（门口就是 WebUI token）

`GET /download?path=<路径>`：

| 形状 | 回答 |
|---|---|
| `?meta=1` | `{ok, name, size}` —— 只报名字和大小，零字节，供下载器切窗用 |
| 无 `Range` | `200` 全量 + `Accept-Ranges: bytes` + `Content-Disposition: attachment; filename*=UTF-8''…` |
| `bytes=a-b` / `bytes=a-` | `206` + `Content-Range` + 该窗口的 `Content-Length` |
| 起点越过末尾 | `416` + `Content-Range: bytes */size`，不发字节 |
| 不是文件 / 不存在 | `404`（目录、`nope.bin`、越界相对路径都走这条） |

- **Range 契约与 hub 是同一份代码**：`server.py` 直接用 `fungi/hub/app.py::range_window` 与 `RangeNotSatisfiableError`
  —— 一个实现在两个服务器上，免得两处各写一遍 206/416 的边界（§50 定的那条）。
- 权限口径写明白：**WebUI token ＝ 主人的设备**（扫过房主二维码的那台手机；房间好友拿的是**房间 token**，
  它在这里开不了任何门）。这与 agent 自己的 `read` 工具是同一种身份 —— 那个工具对路径没有任何根限制，
  所以这条路由**不新增一类权限**，只是把「让 agent 读出来贴进对话」变成「直接取字节」。

### 52.2 手机那一半（`web/common.js::Xfer.download` + `m.js` 的可点路径）

- 先 `?meta=1` 问大小 → 按 §50/§51 同一套规则切窗（每窗 ≥ 4 MiB、最多 4 条）。
- 各窗口并发 `fetch` 带 `Range`，**某窗口断了从它真到的字节续**（`Range: bytes=(lo+got)-(hi-1)`）。
- **组装只能在页面里做**：手机在 `http://` 页面上**没有** File System Access（那是 secure context 才有的），
  所以窗口是按顺序拼成 Blob 后交给浏览器保存 —— 代价是内存。因此：
  - `total > 192 MiB` 或切不出 ≥ 2 条 → **交给浏览器自己的下载器**（一条连接、落盘流式、没有进度条）；
  - 拼装前校验「收到的字节数 == meta 里的 size」，不等就**不接受**（§49 那条铁律在最后一跳的客户端形态）。
- 落地形态：`URL.createObjectURL(blob)` + `<a download=名字>`（`Content-Disposition` 已经带了名字）。
- **可点路径**：手机端把消息/工具结果里的绝对路径（`C:\…`）变成 `.file-link`，点一下就是「取这个文件」。
  实现在 `common.js::linkifyPaths`，只走 **text 节点**（`TreeWalker`），绝不改 HTML 字符串 ——
  在标记上跑正则正是把标签改坏、或把链接塞进 `href` 的经典做法。桌面端不传 `fileLink`，行为不变（文件本来就在它手上）。
- 手机上的进度条复用同一个发送模态（一步「下载到手机」）；交给浏览器下载时不假装有进度，直接说明。

### 52.3 没做

- **不在服务端拼装**（不做 `/download?sid&offset` 那种多请求会话）：手机要的是**保存到手机**，
  拼在服务端再传一遍等于把字节走两趟。§50 的两份实现（服务端 Range + 客户端窗口）在这里是「Range 用服务端那份、组装用客户端这份」。
- **HTTPS**：上了 HTTPS，手机就能用 File System Access 在**盘上就地写**（内存只占一个窗口），
  这是这条腿的下一步；自签证书要手机手动信任，属于另一件事。
- 手机端 1/2/4 条的真机数字同样**待测**（见 §50.7 的口径）。

### 52.4 验收

- `tests/test_webui_upload.py`：`?meta=1` 只回名字与大小 · 整份下载带 `Accept-Ranges` 与
  `Content-Disposition` · Range 逐字节（含开区间与按 RFC 夹末尾）· 越界 416 且不发字节 · 看不懂的 Range 忽略当 200 ·
  目录/不存在/越界相对路径 404。
- `tests/test_webui_transfer.py`（真 Chromium）：
  `test_the_phone_pulls_a_file_from_the_pc_in_windows` —— 12 MiB 从手机页拉取，观测到 1 次 `meta=1` +
  3 个 `Range` 窗口（互不重叠、覆盖 `[0,size)`、每 4 MiB 一段），**浏览器另一个保存下来的文件与源逐字节相同**，
  建议文件名取自 `Content-Disposition`；`test_paths_in_the_transcript_are_taps` —— 路径变可点链接、
  已有的 `<a>` 不被改写、句读不吞进路径。

## 53. 文件传输助手（2026-09-19 用户点名）：两个设备共用一个「只搬文件、不说话」的会话

**用户的原话与动机**：「现在电脑上传还是太麻烦了，你得先给 Agent 发消息，等到显示到卡片才上传。能不能专门提供一个
默认的文件传输助手会话……电脑上传文件手机的文件传输助手显示新信息，反过来手机上传文件也专门在这个会话」。
即：手机要拿到电脑上的东西，不该先经过一轮对话；两边各放一个东西进同一个地方，另一台设备自己就看到。

### 53.1 是什么

一个**固定会话**（id `file-transfer`，标题「文件传输助手」），两个方向都往它里面落**行**：

| 方向 | 怎么产生这一行 |
|---|---|
| 手机 → 电脑 | 手机端点 📎 上传（§51）**落地成功之后**，服务端自动写一行：`手机上传：拾荒集.zip（6 B）` + 换行 + 落点绝对路径 |
| 电脑 → 手机 | 电脑端在这个会话里发一条（📎 走 `/pickfile` 在电脑上选文件 → 路径进输入框 → 发送）：一条消息就是一行，路径写在里面 |

- **这个会话不跑模型**：在这个会话里发消息 = 追加一行 + 立刻 `done`（同一套 NDJSON 形状，页面照旧自己重载），
  **零 token、零延迟，也不存在「模型把路径改写一下」的风险**。测试里 `build_agent` 被换成「一旦被调用就失败」，
  证明这条路上永远不会跑到模型。
- **手机端点行里的路径就是 §52 的取文件**（可点链接是手机端本来就有的行为，见 §52.2）。
- 会话列表里**置顶**，并且服务端给这一行标 `shuttle: true` —— 前端不重复写一份 id 常量，哪个会话是它由服务端说了算。
- 两个 shell 都**只在这个会话里**每 3 秒轮询一次（`document.hidden` 时不轮询；手机端好友视图握着 `#messages` 时不抢）：
  对面设备放下的东西自己就出现，**不需要任何人点刷新**。

### 53.2 边界（都在测试里钉住）

- **半截的不算**：上传只有 `done`（长度 + 覆盖都过）才落行；部分窗口到达时列表里 `msgCount` 仍是 0。
- **会话存储坏了不影响传文件**：落行是 best effort（`runlog.warn_once` 说一次），文件已经在盘上了 ——
  一次成功的上传不会因为会话写不进去而变成失败。
- **删掉它也没关系**：下次任何人打开 `/sessions` 或直接打开这个 id 时重新建（空会话）。
- 路径**独立成行**（`名字/大小` 一行 + 路径一行）：长路径在手机上不会被前半句挤掉，也是可点链接的边界。
- 权限沿用 §52：WebUI token 是门，房间 token 在这扇门上什么也打不开。

### 53.3 没做

- **电脑侧的「推给手机」没有一个专门的按钮**：现在是在这个会话里 `📎 选电脑上的文件 → 发送` 两步。
  真正的专用一键（选完直接落行、不进输入框）留给以后 —— 先把会话这一层立起来。
- **行里没有缩略图/预览**：手机想看内容得先取下来（取下来就是普通的 §52 下载）。
- **多设备**：这个会话是**房主本人**的两台设备之间用的（token 属于主人）；别人（房间好友）走的是 §47–§50 那套 transfer + 同意卡。

### 53.4 验收

- `tests/test_shuttle.py`（6 例）：会话存在且排在列表第一、带 `shuttle` 标记 · 在里面发一条**只落行、不跑回合**
  （事件只有 `sessionId` + `done`，内容与用户敲进去的一字不差且带 `ts`）· 手机上传落行（`手机上传：…（6 B）`+
  落点路径 + 路径独立成行）· 没落地就**不落行** · 会话写不进去时上传照样成功 · 其它 id 仍然 404。
- `tests/test_webui_transfer.py::test_the_phone_sees_a_file_the_computer_dropped`（真 Chromium 真页面）：
  会话列表第一行就是它 → 手机切进这个会话 → 电脑侧丢一条路径进去 → **12 秒内**手机页面上出现可点的
  `.file-link`（没有任何人点刷新）→ 手机再上传一个文件，同一会话里出现「手机上传」那一行。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **688 passed**（212s；§52 那次是 681）。
- **顺手修的既有用例**：`tests/test_webui_sessions.py` 的重命名用例原来假设「列表第一行就是我刚建的会话」——
  传输会话置顶之后这个假设不成立了，改成按 id 找自己那一行（它本来测的就是重命名的陈旧闭包，不是列表顺序）。

### 53.5 一处顺手修的测试卫生

`/upload` 落地会往会话里写行以后，原来那批上传用例会把行写进**开发者自己的** `data/sessions/`（测试里第一次跑出
`file-transfer.json` 才发现）。`tests/test_webui_upload.py` 的 fixture 现在把 `session.SESSIONS_DIR` 指到 tmp，
那份被写出来的文件也删掉了。

## 54. 文件传输助手要和别的会话分开（2026-09-19 用户裁决，同日）：样式、图标、改不动也删不掉

**用户的原话与动机**：这个会话该和别的会话区分开，**至少在样式上**；**无法重命名或删除**；**始终置顶的图标**。
一句话：它不是一个能被误操作弄坏的聊天，它是两台设备之间的那条通道。

### 54.1 四条改动

1. **看得出来的不同**：列表里它前面有图标（`📁`）、底色与左边条**常显**（别的行只在 hover/active 时才亮）、
   右侧的 meta 位写「只搬文件」（而不是建会话的日期）、hover 有 tooltip 说明它是什么。
   样式挂在 `.session-row.shuttle` 上，由服务端 `/sessions` 里的 `shuttle: true` 驱动 —— 前端不复制一份 id 常量。
2. **没有 ✎ / ✕ 两个按钮**：两个 shell 的行构建里对它就**不渲染**这两个按钮。
   （桌面端的列表是「复用行 + 就地改」的，所以那半条路径也要照顾到：复用时会重写 meta，
   不判 `shuttle` 的话它的「只搬文件」会**在列表重绘的瞬间被打回日期** —— 这一处是测试抓出来的，不是看代码看出来的。）
3. **服务端也跟着拒绝**（UI 藏按钮是建议，不是保证）：
   - `DELETE /session?id=file-transfer` → **400**，并且回一句中文原因（「文件传输助手不能删除（它是两台设备之间的通道）」）；
   - `POST /save` 带任何别的标题 → 标题仍然是「文件传输助手」（行照常保存，只是名字改不动）。
4. **置顶不变**（§53 就有的那一条），并且 `GET /session?id=file-transfer` 也会确保它存在。

### 54.2 没做 / 待定

- **没有「清空记录」的入口**：它的行只会越积越多，而删掉会话文件它又会被重建 —— 于是现在没有清空的办法。
  要清空就得给一个专门的入口（例如列表里的「清空」或一条 `/file-transfer/clear` 路由），
  **这条留给用户定**：是保留全部记录（当传输日志看）还是给个清空键。
- 没有置顶以外的排序能力（拖动排序之类）——不在这一版。

### 54.3 验收

- `tests/test_shuttle.py`（8 例，比 §53 多 2）：新增「改名改不动」（`POST /save` 带别的标题之后，
  列表里仍是「文件传输助手」，而行一条没丢）与「删不掉」（`DELETE` 回 400 且行还在）。
- `tests/test_webui_transfer.py`（手机页）：列表第一行就是这个会话、带着图标、`.session-row-act` 计数为 **0**、
  meta 是「只搬文件」，而普通会话行仍然是 2 个按钮。
- `tests/test_webui_sessions.py`（桌面页）：同样的四条断言，再加一条**真的去打服务端**的：
  `DELETE /session?id=file-transfer` 回 400、会话仍在、错误话术里带「不能删除」。
  另外那条既有的重命名用例：它原来「等有任何一行」就等于「新会话已出现」，传输会话置顶之后这个假设不成立，
  改成等 `allSessions` 里真的出现 `(new session)`。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **691 passed**（209s；§53 那次是 688）。

## 55. 轮询不该让页面抖（2026-09-19 用户现场报告）：只追加新增行，没新行一个 DOM 都不碰

**用户的原话**：「我发现你这个会刷新导致页面出现频繁变化（有无滚动条等）移动端也是，会话列表频繁刷新重载」。

### 55.1 根因：是 §53 那个轮询走了「整表重画」，不是老代码

`reloadSessionFromServer()` 是现成的、给「回合结束/切换会话」用的重载，它做两件事：

1. `S.clearMsgs()` + 整表重画 —— 滚动条忽隐忽现、选区与图片被重建，一眼就是「页面在刷新」；
2. 顺手 `loadSessions()` —— **移动端的 `renderSessionList` 是先删掉所有行再逐行重建 + GSAP 淡入**，
   所以看起来就是「会话列表频繁刷新重载」。

§53 的轮询每 3 秒调它一次 → 用户看到的抖动。**移动端本来就有的两个 3 秒轮询不背这个锅**：
`pollPendingAsks` 只在真有新卡片时挂卡片、`resumeIfPending` 只在有后台任务时才动（都读过代码确认）。

### 55.2 修法（三层）

1. **服务端给一个游标**：`GET /session?id=X&after=N` → 只回第 N 行之后的行 + `total`；不带 `after` 时仍是整份
   （切换会话、回合结束那些路径行为一个字节没变）。
2. **两个 shell 的轮询改成 `pollShuttle()`**：只取新增行、只**追加**这几行（复用同一个 `renderTranscript` 渲染器，
   asks 传空），并且**永不调用 `loadSessions()`**。
3. **没有新行就直接 return** —— 不重绘、不碰列表、连一个 DOM 属性都不写。

游标就是渲染器手里那份 `rawMessages.length`，所以「整份载入」（切换会话）之后游标自然对齐，不需要额外状态。

### 55.3 现在保证什么（都写进了用例）

- **空转 4.5 秒**（> 一个轮询周期）：`#messages` 上的 MutationObserver 记到 **0** 次变更；
  `/sessions` 请求 **0** 次（会话列表在这条路径上永不重载）。
- **有新行**：只多一行（`rows + 1`），而且**原来那一行还是同一个 DOM 节点**（打 `data-probe` 验身份 ——
  重画会把节点换掉，追加不会）。
- 于是「有无滚动条」的抖动消失：容器高度只在真的多了一行时才变，且是往下长。

### 55.4 没做

- **没有改成推送**：`/events` 那条流是回合制的（磁带 + 直播），要让「追加一行」也走它，得给会话加一层
  发布订阅 —— 值得做，但不是这次修抖动的最小改动。
- 3 秒仍是周期：它现在是一个**几乎什么都不做**的轮询（一次小 GET，通常零 DOM 变更）。

### 55.5 验收

- `tests/test_shuttle.py::test_after_returns_only_what_is_new`：`after=1` 只回第 2 行、`after=2` 回空、
  `after=99` 回空且 `total` 仍是 2；不带 `after` 的整份形状不变。
- `tests/test_webui_transfer.py::test_the_transfer_session_only_appends_what_is_new`（真 Chromium）：
  进入传输会话 → 安静 4.5 秒断言 **0 变更 / 0 次 `/sessions`** → 电脑侧丢一条 → 新行出现、
  老行节点身份不变、总行数只 +1、`/sessions` 仍是 0。
- 这类用例的**共享状态陷阱**（写下来免得下次再踩）：传输会话是**模块级 room** 里的同一个会话，
  前面的用例会往里丢过行 —— 断言必须相对「进入时屏幕上已有几行」，不能假设空会话（第一版就是这么 flaky 的）。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **693 passed**（§54 那次是 691）。

## 56. 传输要有卡片、通道不要和「当前会话」撞衫（2026-09-19 用户现场反馈）

**用户的原话**：「你这个电脑到手机的也太鸡肋了吧，怎么还是链接形式呢？我的卡片呢？」
以及「文件传输助手的样式和选中会话的样式重合了，不太好」。

### 56.1 一行传输 = 一张卡片（不再是「正文里的一段路径」）

- **行变成记录，不是散文**：`shuttle_post(..., file=...)` 往那一行加 `file` 字段
  （`{name, size, path, direction}`）。`content` 里那句人话照旧写（日志、以及任何不经过 WebUI 读转录的东西都还看得懂）。
- **两个方向都自动带卡**：
  - 手机上传落地 → `direction: "phone"`（名字/大小/落点都是落盘那一刻的真值）；
  - 电脑侧发的消息里如果**点名了一个真实存在的文件** → `direction: "computer"`
    （`file_in()`：扫绝对路径、去掉尾部标点、`is_file()` 才算；**不存在的路径不是卡片，是打错字**，保持普通消息）。
    于是电脑侧的动作仍然是「📎 选文件 → 发送」两步，而手机端收到的是一张卡。
- **卡片长什么样**（`common.js::buildFileCard`，两个 shell 共用一套结构、各自配色）：
  图标 + 文件名 + 大小 · 一行「手机上传 / 电脑发送」 · 等宽小字的完整路径 · **手机端多一个「下载到手机」按钮**
  （直接调用 §52 那套窗口下载，路径不再是需要用户在段落里找的链接）。
- 卡片里的路径**不再二次变成链接**（`linkifyPaths` 跳过 `.file-card`）：一张卡上只有一个动作，别给两个入口。

### 56.2 通道和「当前会话」不再撞衫

**问题**：原来两套样式都是「accent 底色 + 左边条」，于是分不出「这是那条通道」还是「你现在在这」——
用户一眼就看出来了。

**现在的分工**（两种视觉语言，互不借用）：

| | 语言 |
|---|---|
| 聊天会话被选中 | **左边条 + accent 底色**（原样不动） |
| 文件传输助手 | **描边盒子 + 文件夹图标**，底色是中性的 `--surface2`；被选中时描边变 accent + 一圈光晕，**仍然没有左边条** |

**为什么两个 shell 要分别断言**：这条契约在移动端真的坏过 ——
`.session-row.active` 在 `m.css` 里写在后面，和我的 `.session-row.shuttle` 同特异度，后写的赢，
于是「选中的通道」在手机上又套上了 chat 的底色 + 左边条（**桌面端顺序相反，看起来是对的**）。
修法是让这三条规则带上 `.active` 变成更高特异度（`.session-row.shuttle,.session-row.shuttle.active{...}`），
**不靠顺序**。用例断言的是**计算样式**（`::before` 的 `content` 为 `none`、底色是 `rgb(245,246,252)`），
因为这事眼睛看不出来（浅色主题的 accent 是粉色，和「选中的粉」太像）。

### 56.3 验收

- `tests/test_shuttle.py`（11 例）：新增两条 —— 手机上传那行的 `file` 字段逐字段相等 ·
  「发一条点名真文件的路径」→ `direction: "computer"` + 真大小；**点名一个不存在的路径** → 那行没有 `file` 字段。
- `tests/test_webui_transfer.py::test_a_sent_path_arrives_as_a_card_that_pulls`（真 Chromium 端到端）：
  电脑侧 POST 一条带路径的消息（就是桌面页那条路）→ 手机进入传输会话 → 卡片出现（名字 / `40.0 KB` /
  「电脑发送」/ 路径 / 有按钮 / 卡片里 0 个 `.file-link`）→ **点按钮，浏览器另一存下来的文件与源逐字节相同**，
  文件名取自 `Content-Disposition`。
- 样式契约：手机端与桌面端各一条计算样式断言（见 56.2）。
- **共享状态的坑第三次咬人，记在这儿**：这条卡片用例第一版取的是「转录里第一张卡片」，
  而同一个模块级 room 里前面的用例（手机上传 `photo.bin`）也会落卡片 —— 整套跑就取到了别人的卡。
  改成**按名字找自己那张**。断言里凡是「某类元素里的第几个」都要先问一句：前面的用例会不会留下同样的东西。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **696 passed**（§55 那次是 693）。

## 57. 两个小毛病，同一个病根（2026-09-19 用户现场反馈）：CSS 写在谁前面、状态行谁来收尾

**用户的原话**：「电脑端上传后会显示 thinking，而且移动和电脑端都靠右 —— 不应该是自己在左边吗」。

修之前先取证（页面里读计算样式、读状态行，不靠看截图猜），两条都复现了，而且各自有一个具体病根：

### 57.1 卡片靠右：`.msg` 定义在我的规则**后面**，于是我的规则根本没生效

桌面端卡片实测 `align-self: auto`、宽度 791px（= `.msg` 的 `max-width:82%`）——
说明我写的 `.msg` 那一套（`align-self:flex-start`、`max-width:520px`、自己的 padding）**一条都没生效**：
`.msg` 与 `.file-card` 同特异度（都是 1 个类），而 `.msg` 在 `style.css` 里写在后面，冲突的属性它赢。

- 于是卡片**被拉满、贴到右边**（他说的「靠右」），而**手机端看不出这个问题**（`m.css` 里我的规则恰好写在
  `.msg` 后面，所以是好的）—— 跟 §56.2 那次一模一样的坑，只是发生在另一个文件。
- 修法同样是**不靠顺序**：卡片相关规则全部升成 `.msg.file-card`（两个类）→ 无论谁在前都赢。
  桌面端顺带补上 `align-self:flex-start`（非 `.user` 的行都要自己声明，容器是 `align-items:normal`）。

### 57.2 状态行停在 "Thinking..."：桌面端的回合收尾从来不重置它

`send()` 把 `status` 写成 `Thinking...`，而**桌面端 `done` 分支只做重载、不重置状态行**（手机端那半有这行）。
普通回合里后续事件会把它覆盖成 `Writing...`，看不太出来；**传输会话一条词都不写（§53：不跑模型）**，
于是它就永久停在 `Thinking...`。

修法：桌面端 `done` 分支补上与手机端同义的一行
（`t.aborted ? 'Aborted.' : (failed ? 'Turn failed.' : '')`）—— 顺带把普通会话也修好了。

### 57.3 验收

- `tests/test_webui_sessions.py::test_the_desktop_shows_the_card_left_and_stops_saying_thinking`（真 Chromium）：
  电脑侧发一条带真文件的路径 → 卡片出现 → 计算样式断言 `align-self:flex-start`、**`max-width:520px`**
  （这条才是「规则真的生效了」的证据：它被 `.msg` 盖掉时正是 82%）、边框 1px、距容器左边缘 < 40px；
  再在这个会话里发一句 → 等回合结束 → **`#status` 必须是空串**。
- `tests/test_webui_transfer.py`（卡片那条）顺带断言手机端卡片的 `align-self` 与边框。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **697 passed**（§56 那次是 696）＋
  本轮全量跑挂过一次 `tests/test_llm.py::test_stream_chat_abort_mid_stream_carries_partial`
  的 `WinError 10053`（本机已知的 socket flake：单跑 2/2 绿，且这一版没碰 `fungi/llm.py`）。

## 58. 名字里有空格就不是文件了（2026-09-19 用户现场反馈）：「我的卡片呢？怎么是信息？」

**用户的原话**：「？我的卡片呢？电脑转手机的卡片呢？怎么是信息？」—— 他发的是一行路径，页面上却是一条**普通消息**，不是卡片。

取证（他那一行就在 `data/sessions/file-transfer.json` 里，`file` 字段是 `None`）：

```
他发的那一行：C:/Users/37549/Pictures/Screenshots/屏幕录制 2026-09-17 090847.mp4
这个文件真实存在（6,376,178 B）
file_in() → None      ← 没有 file 字段，页面就照字段画成一条消息（§56）
```

### 58.1 病根：`\s` 既可能是路径的边界，也可能是名字的一部分

`FILE_PATH_RE` 的字符类是 `[^\s…]+` —— **到第一个空格就断**。于是从 `…/屏幕录制 2026-09-17 090847.mp4`
里切出来的是 `…/屏幕录制`，那个东西不是文件 → `file_in()` 返回 `None` → 普通消息。
而本仓库的用例**清一色是无空格的名字**（`handover.bin`、`gift.bin`、`C:\tmp\a.zip`），所以全绿：
这是**测试数据不真实**造成的系统性盲区，不是手滑。

同一个正则的第二份拷贝在 `web/common.js`（手机端的可点路径）：含空格的路径会被链成**半截**，点下去 404。
而**他手机上的录屏/截图文件名恰好全都带空格**（「屏幕录制 2026-09-17 090847.mp4」）——
这条腿上最常走的名字，正是最坏的那一种。

### 58.2 修法：谁说的都不算，`is_file()` 算

空格是合法文件名字符，所以**没有任何正则能说出一条路径在哪里结束**——只有磁盘知道。于是按「最字面优先」
依次试候选（`server.py::_path_candidates`）：

1. **整行就是那条路径**（§53 这条腿的正常形状：一行 = 一个地址）；只认盘符开头的绝对路径，
   免得把一句 `README.md` 当成相对路径去 stat（那才是真的会认错）。
2. 句子里扫出来的窄 run —— 原来的行为，原样保留。
3. 允许空格的宽 run，**一次还一个词**（`rsplit(" ", 1)`）：`给你 <path> 收` 就是在「还到 `…090847.mp4`」时命中的。

每一步的判据只有一个：`Path(candidate).is_file()`。命中即卡片；都不命中仍是普通消息 ——
**打错字不是传输**（§53 定的语义，没动）。

手机侧没有文件系统可问，所以它**只敢认一种形状**：这一行**整行**就是路径（`common.js::LINE_PATH_RE`，
允许空格，且这一行只有一个盘符起点 —— 一行里两个路径是句子，交给窄扫描）。半截链接比没有链接更坏：
现在它要么给出完整路径，要么不给。

### 58.3 验收

- **修前先复现**（`git stash` 掉这版源码再跑，两条都红）：`test_a_file_name_with_blanks_is_still_a_card`
  修前 `KeyError: 'file'`（正是他看到的「是信息不是卡片」）；`test_paths_in_the_transcript_are_taps`
  修前把那条路径链成 `C:\tmp\屏幕录制`（半截链接）。
- `tests/test_shuttle.py::test_a_file_name_with_blanks_is_still_a_card`：整行是带空格的路径 → 卡 ·
  句子里带 → 卡 · 差一个字的错名 → 没有 `file` 字段。
- `tests/test_webui_transfer.py::test_paths_in_the_transcript_are_taps`（真 Chromium）顺带断言：
  带空格的整行链成**完整**路径；一行两个路径仍链成两条。
- 他那一行原文的真机核对（本机跑，不进用例）：
  `file_in('C:/Users/37549/Pictures/Screenshots/屏幕录制 2026-09-17 090847.mp4', 'computer')`
  → `{'name': '屏幕录制 2026-09-17 090847.mp4', 'size': 6376178, …, 'direction': 'computer'}`。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **698 passed**（§57 那次是 697）。

### 58.4 没做 / 待定

- **已经存在的那一行不会自己变成卡片**：`file` 是写入那一刻算的，老行补不回来。
  要么重新发一次（反正那一次也没真传过去），要么在读取时对没有 `file` 的行补判一次 ——
  后者等于让卡片依赖「此刻文件还在不在」，与 §56「行是记录」的语义有出入，**留给用户定**。

## 59. 卡片该站在哪一边（2026-09-19 用户裁决）：自己在右、对方在左，手机端也别再回填路径

**用户的两句话**：先是 §57 那一轮「移动和电脑端都靠右 —— 不应该是自己在左边吗」，
然后本轮「你这不对啊，怎么还是统一靠左？」，外加「移动端发送完文件后不用自动填入路径（因为文件已经上传了）」。

两句合起来读才清楚：他不接受**一律同侧**。§57 只把卡片从「被 `.msg` 拉满、贴右边」修成 `align-self:flex-start`，
于是自己的卡和对方的卡都靠左 —— 换了一种糊法。**要的是分侧，不是换边。**

### 59.1 判据：卡片站在「发它那台设备」那边，每个 shell 各算各的自己

行里本来就有 `file.direction`（§56：`computer` / `phone`），缺的只是**这个 shell 是谁**：

- 桌面页（`app.js` 的 `renderOpts`）→ `my: 'computer'`；
- 手机页（`m.js` 的 `renderOpts`）→ `my: 'phone'`。

`common.js::buildFileCard` 据此给卡片加 `fc-mine` / `fc-peer`（不猜方向、也不按 shell 写死）：

| 看的人 | 电脑发送的卡 | 手机上传的卡 |
|---|---|---|
| 电脑（桌面页） | **右**（自己） | 左（对方） |
| 手机（手机页） | 左（对方） | **右**（自己） |

CSS 只加两条（`style.css` / `m.css`）：`.msg.file-card.fc-mine{align-self:flex-end}` ·
`.msg.file-card.fc-peer{align-self:flex-start}` —— 三个类，稳赢 `.msg` / `.msg.file-card`，
不靠「谁写在后面」（§57 那个坑的规矩）。自己那张卡的时间戳一起挪到右下
（`.msg.file-card.fc-mine[data-when]::after`，与 `.msg.user` 同一套语言）。
没传 `my` 的 shell 保持原样（基数规则仍是 `flex-start`）。

### 59.2 手机上传完不再回填路径

§51 当初是「上传完把落点路径塞进输入框，发给 agent 让它读那个文件」。用户现在点名不要：
**文件已经在电脑上了**，塞一条路径只是噪音 —— 那条路径卡片自己写着（`fc-path`）。
于是 `m.js` 去掉 `input.value = …` 与随之的 `autoGrow()`。
桌面端「📎 选文件 → 发送」两步**没动**：它填输入框是流程本身，不是回填。

### 59.3 验收

- 修前先复现（`git stash` 掉 `web/` 再跑，三条都红）：
  - `test_webui_sessions.py::test_the_desktop_sides_the_cards_and_stops_saying_thinking`：
    自己那张卡修前是 `flex-start`（应为 `flex-end` 且贴右边缘 < 40px）；同一页再来一行 `direction: phone`
    （服务端 `shuttle_post` 写的就是手机上传那一行），断言它 `flex-start` 且贴左 ——
    **两边不一样，这才是他报的那件事**。§57 的两条断言保留（`max-width:520px`、`1px` 边框）：
    它们证明规则真的生效，而不是被 `.msg` 盖掉。
  - `test_webui_transfer.py::test_the_phone_sees_a_file_the_computer_dropped`：手机页上自己上传的卡修前 `flex-start`。
  - `test_webui_transfer.py::test_mobile_upload_cuts_a_big_file_into_windows`：修前输入框里躺着落点路径
    （`…\inbox\big.bin`）；现在先等传输面板自己关掉（`#xfer-overlay` 去掉 `show`，`finish()` 后 900 ms）
    再断言输入框是空的 —— 「还没有回填」和「永远不会回填」是两件事，所以要等流程真的走完。
- 用例里等的是**自己那一行**（按文件名找卡），不是「有一张卡」：这几个浏览器模块共用一个页面，
  上一个用例的行还在屏上（同一个坑 §56 已踩过一次）。等「手机上传」这四个字更是假的，卡片本来就是这四个字。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **698 passed**（与 §58 同数：此刻没有新增用例，改的是既有的三条）。

### 59.4 没做 / 待定

- **不带文件的普通消息行也分侧了**（用户 2026-09-19「要分侧加字段」→ §60）：行里现在存 `direction`，
  由发消息的那个 shell 声明。原来这里写的是「等他真的在意再说」。
- 卡片在**好友视图**里不会出现（那边走 comm 消息，不读 `file`），所以 `my` 只作用于会话视图。

## 60. 普通消息行也分侧（2026-09-19 用户裁决「要分侧加字段」）：行里存下发送方

§59 只把**卡片**分了侧，他补了一句：传输助手里还有**没有文件的文字行**，那些行当时一律靠右（§59.4 如实记着）。
卡片自带 `file.direction`，文字行没有 —— 所以要**在行上存一个字段**。

### 60.1 行上存 `direction`，由发消息的 shell 声明

- 客户端在**发的时候声明**：`POST /chat` 的 body 多一个 `side`（`app.js` 的 `MY_SIDE = 'computer'`、
  `m.js` 的 `MY_SIDE = 'phone'`，与渲染用的 `my` 是同一个常量，所以不可能两边各说一套）。
- 服务端 `shuttle_post(..., side=)` 把它写进行里：`row["direction"]`。带文件的行仍取 `file.direction` ——
  也就是**同一个字段名、同一个含义**，不再造第二个名字。不认识的 `side` 一律当没声明：
  **猜一个错的边，比空着更坏**。
- 没声明的行保持原样（老页面、脚本、旧数据都不受影响）：还是 `.msg.user` 的默认（靠右）。
- 服务端这一层还顺手修了个不一致：**手机**发一条路径时，那张卡的 `direction` 以前被写死成 `computer`
  （`file_in(message, "computer")`），现在是「谁发的就是谁的」——他手机上打一条电脑上的路径，卡片会说「手机上传」。

### 60.2 渲染：一处算，两处用

`common.js::renderTranscript` 里算一次侧类（`mine` / `peer`），卡片与文字行共用：
`dir = m.direction || m.file.direction` → `dir === opts.my ? ' mine' : ' peer'`。
于是 `.fc-mine` / `.fc-peer` 那对名字并成 `mine` / `peer`（卡片 = `msg file-card mine`，文字行 = `msg user mine`），
CSS 里全是**三个类**（`.msg.file-card.mine` / `.msg.user.peer`），照旧不靠「谁写在后面」。
自己那一行的时间戳跟着走（`.msg.user.peer[data-when]::after`）。

### 60.3 验收

- 修前先红，分两层取证：
  - 只 `git stash` 掉 `web/`（服务端那半留着）：`test_the_phone_sees_a_file_the_computer_dropped` 里
    「电脑那头的一句话」仍是 `flex-end` —— 行里有了发送方、页面还不会用。
  - 连 `fungi/server.py` 一起 stash：`test_a_plain_row_keeps_the_side_that_typed_it` → `KeyError: 'direction'`；
    `test_a_message_naming_a_real_file_becomes_a_card` → 手机发的那条路径仍是 `computer`。
- `tests/test_shuttle.py::test_a_plain_row_keeps_the_side_that_typed_it`：`side="phone"` 的行记住 `phone` ·
  不声明的行没有这个字段 · `side="tablet"` 也不认。
- `tests/test_webui_sessions.py::test_the_desktop_sides_the_cards_and_stops_saying_thinking`（真 Chromium）：
  桌面自己打的那句靠右、`side="phone"` 那行靠左。
- `tests/test_webui_transfer.py::test_the_phone_sees_a_file_the_computer_dropped`（真 Chromium）：
  手机自己打的那句靠右、电脑那行靠左。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **699 passed / 271s**（§59 那次 698：本轮新增 `test_a_plain_row_keeps_the_side_that_typed_it`）。

### 60.4 仍然没做

- 好友视图那套 `friend-mine` / `friend-peer` **没有**并进 `mine` / `peer`：它两侧的语义（对面在左、信使在右）
  是另一回事，合并只为了少两个名字，得不偿失。

## 61. 会话提醒（2026-09-21 用户点名）：Agent 在等你 / 答完了 / 出错了 —— 复用铃声，每个会话一个红点

**用户原话**：「当Agent需要用户回复，比如inquire，或者回答完毕，或者出现错误等情况，复用铃声机制响铃提醒用户。
用户点进该会话后响铃停止。请注意，webUI最小化不算点开。每个会话也可以出现红点（就像好友列表那样）」。

### 61.1 三件事算「在等用户」，判据在服务端

- **ask（要你回答）**：在**提问的当下**就挂上提醒 —— `WebSink.emit` 里 `kind == "ask"` 处 `note_alert`。
  提问那条路是 `tools/ask.py::blocking_ask`（回合就等在那儿，可能一等就是 `ASK_TIMEOUT_S` = 15 分钟）：
  等回合结束再提醒，等于没提。
- **done（答完了）/ error（出错了）**：`_run_turn` 的出口。**被按停的回合（Esc → `/stop`）不提醒** ——
  「停」的意思是「我在」，不是「提醒我去看看」；回合结束时卡上还挂着没人答的问题，报的是 `ask` 而不是 `done`。
- 提醒住 `fungi/server.py` 的进程内登记表（`_ALERTS` / `_SEEN`）：`/sessions` 每一行多一个 `alert` 字段
  （`"ask"` / `"done"` / `"error"`，安静时 `null`）；删会话顺手忘掉它；**房间停了整体清掉**
  （`room.py::stop`）—— 提醒不该比升起它的那个房间活得久。

### 61.2 「点开」= 页面**可见且聚焦**地在看那个会话（最小化不算）

页面每 3 秒发一次 `POST /session/seen {id, visible}`：`visible:true` 是**声明**（服务端记下时间戳，
并撤掉该会话的提醒），`visible:false` 是**释放声明**（切走 / 隐藏 / 离开页面）。三条判据：

- 声明只在 `!document.hidden && document.hasFocus()` 时发。用户点名的「最小化不算点开」是第一条；
  第二条是同一件事的延伸：窗口在另一块屏上开着、人却在别的窗口里打字，也等于没看。**代价**是抢焦点
  （通知弹窗、切窗口的那一瞬）会多响一声 —— 一声就完的铃（§26.1），而且用户要的正是「别漏了」。
- **声明会过期**（`SEEN_TTL_S = 8s`，约两跳半心跳）：页面崩了 / 关了 / 被冻住时，谁也没法用一份陈旧的
  「我在看」把铃永远按住。切走与隐藏还会立刻 `release` 一次，所以「刚发完就切走、三秒后答完」这种
  最该响的情况，不会因为一份还新鲜的声明被吞掉。
- 挂提醒的一方查的就是这份声明：新鲜 → 不挂（用户正看着，别打扰）；**释放不撤回提醒**，
  只有 `visible:true` 那一下才撤。

### 61.3 铃声复用来信那一套，红点复用好友列表那一套

- `fungi/gui/app.py::_poll_unread`（每秒）现在数两件事：`RoomBase.last_unread`（来信）与
  `session_alerts()`（会话）。**来信守它那 10 秒宽限期**（好友视图要 ~8 秒才把那条标成已读）；
  **会话提醒立刻响** —— 它已经被页面自己的声明挡过一道了，再等 10 秒就太晚。
- 铃声本身一个字节没改：`ring.Ringer`、`config.ring` / `ring_tone`、七个音色、**一声**（§26.1）都还是它；
  一次提醒只响一声（`Ringer.ringing` 从 start 到 stop 都是 True），用户点进那个会话（= 服务端撤提醒）
  才 `stop()`。
- 托盘那半也一起复用了（§25.2 的标题就是「闪动 + 铃声」）：有东西在等就闪，提示语说清是什么
  （`有未读留言` / `有会话在等你` / 两样都有）—— 铃只响一声，闪动才是那个一直在的提示。
- 红点坐在会话行的标题与日期之间（`.session-dot`，accent + 光晕，与好友列表的未读标记同一套语言），
  tooltip 说清哪一种（`Agent 在等你回答` / `回答完毕` / `出错了`）。
- `_session_alerts()` 是**函数内 import**：把 `fungi.server` 拉进 GUI 启动要多花 ~240ms，
  而真正读它的时刻（有房间时）那个模块早被房间 import 过了。

### 61.4 一次请求干两件事，而且不许重画转录

`POST /session/seen` 的**回答就是那张提醒表**（`{会话 id: kind}`）：心跳与红点共用一个请求，
不给页面再添一个轮询。两个 shell 都用 `common.js::initSessionAlerts`（声称 / 心跳 / 释放 / 把表交给
`onChange`），只在**表变了**的时候重画会话列表（§55 的纪律：没变就一个 DOM 都不碰），并且**永不重画转录**
（`test_the_alert_feed_never_repaints_the_transcript` 拿 MutationObserver 钉着）。

- 桌面端 `renderSessionList` 是「复用行 + 就地改」：红点在**新建**与**复用**两条路上都得刷
  （§54 那个坑的同款）。
- `/sessions` payload 里那份 `alert` 会被心跳那张（更新的）表盖掉：刚点开的会话，不该因为一次
  `/sessions` 拉取又把红点长回来。
- 页面打开某个会话时会立刻 `Alerts.tick()`（不等下一跳心跳）：用户点进来就该马上撤提醒、停铃。

### 61.5 验收

- 修前先红，分三层（`git stash push -- <paths>` 逐层还原）：
  - 只还原 `fungi/server.py`：`tests/test_session_alerts.py` → **9 errors**
    （`ImportError: cannot import name 'clear_alerts'`）。
  - 只还原 `web/`：`tests/test_webui_alerts.py` → **3 errors**，页面里 `typeof Alerts` 永远不是 object
    （`Page.wait_for_function: Timeout 30000ms exceeded`）。
  - 还原 `fungi/gui/app.py` + `fungi/gui/trayicon.py`：`test_gui.py -k "session_alert or tray_says_which or
    mail_is_unread"` → **3 failed**（不会响 · 提示语还是「有未读留言」· `set_alert()` 参数数对不上）。
- `tests/test_session_alerts.py`（9 例，**不用浏览器** —— CI 与 Release 都会跑到）：答完 → `done` ·
  卡上挂着没人答的问题 → `ask` · 答过的卡不把 `done` 变成 `ask` · 回合炸了 → `error` · 按停 → 不挂 ·
  `ask` 事件当下就挂 · 声明新鲜时压住、过期后生效、释放不撤回 · 删会话忘掉提醒 ·
  不带 id 的心跳只回答那张表。
- `tests/test_webui_alerts.py`（3 例，真 Chromium）：桌面——红点出现、tooltip 说得对、**点进去红点掉、
  服务端提醒也没了、再拉一次 `/sessions` 也不长回来**；心跳不重画转录；手机——抽屉里也有红点，
  而且把 `document.hidden` / `hasFocus` 改成隐藏之后**同一个提醒真的挂上了**（最小化不算点开），
  回到前台又收回去。
- `tests/test_gui.py` 新增两例：会话提醒不吃宽限期（第一次轮询就响）、用户点进来就停；托盘提示语的三种情形。
- 顺手修掉一个**早就存在**的竞态：`test_webui_transfer.py::test_the_phone_sees_a_file_the_computer_dropped`
  在「自己那句话画出来」的瞬间就问电脑那行在不在，而那一行来自 3 秒一次的传输轮询（或回合结束的整份重载）。
  HEAD 树上单跑它就是红的（`flex-start` ← `None`，与本轮改动无关，已用 `git stash` 证明）；
  补上那一次等待后单跑 3/3 绿。
- 门禁：`python -m ruff check .` 干净 · `python -m ruff format --check fungi tests` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **713 passed / 248s**
  （§60 那次 699：本轮新增 9 例服务端 + 3 例浏览器 + 2 例 GUI）。

### 61.6 没做 / 待定

- **手机端不响铃**：铃声在跑 GUI 的那台机器上响（`ring.Ringer` 是 PyQt5 + QtMultimedia 的东西），
  手机拿到的是红点。要手机也响，得走浏览器音频（`assets/ringtones/*.wav` 走 HTTP + 首次手势解锁
  autoplay）—— 那是另一条腿，等他说了再做。
- **红点不分颜色**：三种 kind 一个样子，区别只写在 tooltip 里。他要的就是「红点（就像好友列表那样）」。
- **提醒不落盘**：它是进程内状态（和 `_TURN_TAPES` 一样），GUI 重启即消失；要活过重启得写进
  `data/sessions/<id>.json`。
- **好友视图那些卡问（out-of-band ask）不算会话提醒**：它们挂在好友线程上，来信那一套（未读徽标 + 铃声）管着。
- **一次提醒期只响一声**（§26.1 的规矩照旧）：两件事先后到来时——比如 A 会话答完、你还没点进去，
  B 会话又出错——只有第一件会响，第二件靠红点与托盘闪动。要让每件都响，得给 `_poll_unread` 记住
  「已经响过的会话集合」并把铃声改成按新 id 触发，那是另一条决定。
- **传输助手不响铃**：他点名的是 Agent 那三种情况（提问 / 答完 / 出错）；对面设备往通道里放了个文件
  不会响（`_shuttle_turn` 根本不走回合）。
- **多标签页会互相盖声明**：`_SEEN` 是「会话 → 时间戳」，不是「标签页 → 会话」，另一个标签页的一次
  `release` 最多让这边 3 秒内重新声明一次（自愈）。要精确到标签页，得给每个页面一个 id。
## 62. 跨次续传（2026-09-23 用户点名）：断掉的传输下次接着传

**用户原话**：「帮我将 flower 和 athand 中的新增功能整合进 fungi」，随后自己收窄成「athand 照你说的，
flower 我只想加一个**断点续传** —— 对于 fungi 的文件传输，包括移动和 PC 之间或 PCs 之间」。

出处是他自己的 Flower（同一作者的分流下载器）：2026-09-23 那一版给 part 旁边写了一张**条子**
（链接、长度、已落盘的区间），进程被 kill、断网、Ctrl-C 之后条子和 part 都留着，下次下**同一个链接**
就从条子说的位置接着下；条子对不上（链接 / 长度 / 摘要变了）就把条子和它指名的 part 一起丢掉。
§50.6 当初把「跨次续传」明确留给二期，理由写在那一节：需要新的 staged id 与「hub 留着暂存」的
双向约定。这一节就是那一期 —— 而且不只第二段腿：四条腿（PC→hub、hub→PC、手机→PC、PC→手机）都有。

### 62.1 动的是失败路径，不是 §49 那条铁律

**终名下永远只出现整份文件**，这条一个字没改。改的是失败路径原先那句「任何失败路径都删掉 part」：
现在失败**留下** part 和它旁边的条子，下一次同一个 staged transfer 从条子上说的位置接着下。
条子的形状与 Flower 同源（`<part>.json`：`transfer` / `size` / `part` / `spans` / `saved`），
名字由 part 派生 —— 找 part 与找条子永远是同一件事。三条保证：

- **只在句柄关闭之后才记**：`Landing.written()` 按 `PERSIST_INTERVAL_S`（2 秒）节流写盘，
  最坏情况是重下一小段，不会出现「条子说到了、盘上没有」；
- **回到盘上再夹一次**：`adopt()` 把每个区间按 part 的**真实长度**夹一遍（`min(last, size)`），
  所以一张在 kill 前一刻写下的条子也只能声称盘上真有的字节；
- **原子写**：先写 `.tmp` 再 `replace`。半张条子 = JSON 解析失败 = 把一份好好的 part 丢掉，
  而那正是这个特性要保住的东西。

### 62.2 身份是「哪一次 staged transfer」，不是文件名

协议里没有校验和（§49.4 的边界：只有长度），而「同名同长」证明不了两次发送是同一份内容 ——
一个改过、长度没变的文件正好会那样。所以续传的判据只有一个：**同一个 staged transfer（同一个 id）
+ 同一个长度**。换了 id 就是另一次传输，磁盘上那份属于别人，丢掉重下（`test_a_note_about_another_transfer_is_not_used`
与客户端那一例 `test_a_different_staged_transfer_does_not_continue_the_old_part` 盯的就是它）。
推论：**要让人真的续上，发送端得复用同一个 id** —— 见 62.3 第一段。

### 62.3 四段腿各自「上次」是什么、怎么接上

| 腿 | 状态住在哪 | 续的判据 | 怎么触发 |
|---|---|---|---|
| 发送端 → hub（PC） | hub 的暂存 + `Transfers` 登记 | 名字 + 长度 + **头 64 KiB 摘要** | 每次发送前先问 `GET /api/transfer/pending` |
| hub → 收件端（PC） | 收件端盘上的 part + 条子 | staged transfer id + 长度 | 同一个 id 再来一次（发送端复用 id，或收件端重试同一张卡） |
| 手机 → PC（上传） | 宿主盘上的 part + 条子；页面的 sid | sid + 名字 + 长度 | 页面按文件指纹取回 sid，先问 `GET /upload?sid=` |
| PC → 手机（下载） | 页面 IndexedDB 里**整窗**的字节 + 切法账本 | 路径 + 长度 + 名字 + 同一种切法 | 再次点那条路径 |

**（1）发送端 → hub：暂存不再一断就丢。** body 比它声明的 `Content-Length` 短 = 连接死了。
以前这条 upload 的直接后果是「删掉暂存 + 400」；现在留下已到的字节，记为 `partial`，并回答
`{id, received, total, partial}`。**partial 永远不 fetchable**（收件端根本不该知道它存在 ——
一个提前的通知就是半份文件被送达的路）。发送端下次带着 `?id=&offset=&total=` 把剩下的交上来。

- 身份用**摘要**而不是名字：hub 手里有暂存的头 `min(64 KiB, 长度)` 个字节，发送端手里有自己的，
  两边算 sha256 比一下（`landing.head_digest`，一个实现两个进程用）。同一份文件重发时这是免费的，
  而它是「同名同长不同内容」唯一拦得住的闸。
- **未完成度**：`min(64 KiB, size)` —— 比这还短的暂存证明不了任何事，一律不认（小文件重传一遍就好）。
- **一份完整的暂存也会被认出来**：重发同一份文件时发送端**一个字节都不再走**，只把信封再发一次
  （对端可能只是还没点接收）。这条在客户端用「把 `_post_upload` 换成断言」钉着。
- 空转的 partial 24 小时清掉（`Transfers.sweep_partials`，跟着 hub 的 reaper 走）；那一份没有清掉的
  结局是磁盘涨 —— 这是它唯一会付出的代价。

**（2）hub → 收件端：窗口级续传。** `_window` 先用条子上的 `end_of_run(start)`：整扇窗都已经在盘上
就**一个请求都不发**；部分在就从它停下的字节续。hub 不认 Range（老版本回 200 全量）时，
把 part **清掉从头**来 —— 不能把整份文件写到某个偏移上。

**（3）手机 → PC：会话是内存，字节是盘。** 宿主的 `UPLOADS` 是内存登记（30 分钟没人碰就忘），
而 part + 条子按 `landing.sweep_parts` 的 7 天走：`session()` 在磁盘上把它找回来（`_resume`），
所以**宿主重启也算**。页面那一半：sid 按「名字 + 长度 + 修改时间」的指纹记在 localStorage，
回来先问 `GET /upload?sid=` 拿 `missing`，只补缺的那几段。指纹不同（换了个文件）就不认那个 part。

**（4）PC → 手机：把一扇窗写完就是一次存档。** 页面拼装只能在内存里（http:// 上没有 File System
Access，§52.3），内存跟着刷新一起没。所以每扇窗**收齐时**整窗写进 IndexedDB（整窗粒度，不是每个
chunk：写一条 48 MiB 的 Blob 比几千条小记录便宜得多），切法（每扇窗的起止）记在 localStorage 的
账本里。下次点同一条路径：账本上的切法一样 → 把已有的窗装回去，只问缺的；切法不一样 → 碎片丢掉
（不同切法拼起来不是文件）。>192 MiB 那一档仍然交给浏览器自己的下载器，它的断点续传是浏览器的事。
private 模式没有 IndexedDB → 退回今天的行为（这份内存，没了就没了）。

### 62.4 协议（envelope）一个字节没动

改动全在 hub 的 HTTP 路由与本地文件：新增 `GET /api/transfer/pending` 与
`GET /api/transfer/state`（都只回答这一单的 src/dst），`/api/transfer/upload` 多了 `id` / `offset` /
`total` 三个可选 query。§48 的裁决（不改协议）仍然成立：信封字段一个没动，房间 token 仍是那扇门；
`pending` 只回答**发送端**（要 src 对上，不然就是替别人查盘），`state` 只回答两端。

### 62.5 边界（如实记）

- **收件端换了机器就没有续传**：状态在盘上（part + 条子），不跟着文件走。换一台机器接收就是从零。
- **hub 进程重启**：暂存是「内存登记 + 盘上的文件」，登记没了 → 新的发送查不到旧暂存、从头来；
  盘上那份旧暂存成为孤儿（历史遗留，本来就没清理）。
- **手机 → PC 要重新选同一个文件**：浏览器不给页面跨刷新的 File 句柄，所以「续」的前提是用户
  重新选中同一个文件（指纹一样才认）。宿主那边的 part 一直在。
- **小文件（< 64 KiB）的暂存不认**：头摘要证明不了，重传一遍 —— 它本来就小。
- **PC → 手机 ≤192 MiB 那一档，一扇没收完的窗会重下一整扇**：整窗记账换来的简单。
- **`--fresh` 的对应物**：`landing.sweep_parts`（7 天）与 `Transfers.sweep_partials`（24 小时）
  是两个 TTL；要立刻清，就删掉 `inbox/*.part*` / hub 暂存目录里的东西。

### 62.6 验收

- `tests/test_landing.py`（17 例，+7）：条子写下来之后第二次尝试拿到同样的区间 ·
  另一个 transfer id 的条子不被采用 · 同一个 id 但长度不同也不采用 · **条子永远不会声称
  part 没有的字节**（夹回真实长度）· 没有 staged transfer 的落地失败仍然什么都不留（§49 照旧）·
  空转的 part 会被 `sweep_parts` 带走 · 半路断掉的 HTTP 交付留下 `.part` 与条子、真名仍然不出现。
- `tests/test_hub_client.py`（8 例，+3 改写）：窗口耗尽次数后留下 part + 条子（真名仍然不出现）·
  **下一次只问缺的**（逐窗比对条子上的 `end_of_run`）· 换了 id 不接别人的 part ·
  断掉的一读现在**保住 `IncompleteRead` 已经交上来的字节**（`_read_body`），续传点因此往前走。
- `tests/test_hub_app.py`（31 例，+8）：早停的上传留下 partial（且不可 fetch、第三方查不到）·
  `pending` 按摘要认出同一份文件、认不出改过内容的同名同长 · `?id=&offset=` 续上并让整份可 fetch ·
  **偏移不对 409 且一个字节都没写进暂存** · 客户端不重发 hub 已经整份持有的文件（把 `_post_upload`
  换成断言）· 客户端从 `offset` 接着交（spy 到 `(offset, id)`）· 暂存没了就自动从头一次 ·
  partial 不给 `/api/transfer`，进度条的总长仍是声明的长度。
- `tests/test_webui_upload.py`（21 例，+2 改写/新增）：会话被扫掉之后 part **与条子**留在盘上、
  同一个 sid 再来一次能接完 · 宿主重启（清掉内存登记）后从盘上的条子续 · 同一个 sid 换个文件仍然
  400，且盘上那份别人的 part 不被采用。
- `tests/test_webui_transfer.py`（真 Chromium）：`test_a_reloaded_phone_page_finishes_the_pull_it_started` ——
  12 MiB 拉一半（第一扇窗写完，其余请求全部失败）→ **刷新页面** → 再点同一条路径 →
  第二轮的 Range 请求里**没有一扇从头开始的窗**、覆盖的字节数正好是缺的那些、
  浏览器存下来的文件与源逐字节相同、IndexedDB 里的碎片也清空了。
- 门禁：`python -m ruff check fungi tests` 干净 · `PYTHONIOENCODING=utf-8 python -m pytest tests -q`
  → **758 passed, 0 skipped**（本机装了 playwright：那 38 例浏览器用例真跑；没装的 runner 上会 skip，数字对不上不是 bug）。
## 63. 桌控三件（2026-09-23 用户点名）：`intent=` 问本机的 decider、托盘浮窗停稳再点、锁屏如实报

**用户原话**：「帮我将 flower 和 athand 中的新增功能整合进 fungi」→ 收窄成「**athand 照你说的**」。
athand 是 2026-09-17 从 `fungi/tools/screen.py` 切出去单独发布的脚本（同一份代码、同一批实测注释）。
这一节把它**切出去之后**改出来的三件事搬回来。其余差异是「一个脚本、一次调用一个进程、没有人可问」
逼出来的形状（`selftest`、CLI、盘上的 listing/labels/strikes、`_escalate` 不提问）—— 那些**不搬**：
Fungi 的 screen 工具在一个 agent 回合里跑，进程内的 `Session` 就是记忆，而且它有一个**用户**可问。

### 63.1 `intent=`：号码可以由本机的一个决策服务来给

工具自己的立场没变：**坐标不是调用方的**，每个手势仍然指向一个程序定位到的候选。变的是
**谁来看那张编号图**。当模型读不出号码、或者好几个控件叫同一个名字时，`click` / `double_click` /
`type` / `scroll` 可以改报 `intent=<这个手势是为了干什么>`，号码交给**这台机器配好的**决策服务。

- **接缝只认配置文件与 HTTP**：`config.json` 的 `decider` 段 —— 设置页「实验性」里的「Bixian」
  就是它的编辑器（地址 + 启动命令两格，回车写盘，「保存并测试」问一次 `/health`），或
  `FUNGI_DECIDER` 环境变量（一次性指到别处，压过配置文件）。键是 `url` / `serve` / `ask` /
  `weights` / `k` / `timeout` / `wait` / `autostart`：**整块原样读写**，设置页没露出来的键（手写的
  `k` / 超时 / `ask`…）存一次也不会丢；启动命令存的是 argv，写盘时从一行切出来 —— 路径先换正斜杠，
  引号不成对就拒写，不让一个切错的 argv 埋一个起不来的服务。进设置页只读盘不联网（`/health` 最坏
  卡界面 3 秒，那是「保存并测试」的事）。**代码里没有任何模型的名字，也不 import 任何模型**：模型
  的运行时与显存完全留在对面（作者自己的 Bixian 是参考实现）。请求是一个 JSON 对象进、一个出：
  `{intent, options:[{id,label,box}], image}` → `{decision, id, p, confidence, threshold, options, marked}`。
- `weights` 是**开关**：它指的路径不在盘上，整个接缝就关掉，并说清它找的是哪个路径。
- **什么时候问**：没有 `target=`/`name=` 而给了 `intent=`；或者 `name=` 命中好几个
  （athand 那边多命中是 `hits[0]` 静默取第一个 —— 这里改成问 decider，它 decline 了就把歧义原样端出来，
  仍然不猜）。
- **fail-open，而且失败是字符串不是异常**：没配 · 权重不在 · 服务不应答 · 权重还在加载 ·
  `UNDECIDED`（peak 没过阈值）—— 每一种都回一句人话，**动作不发生**。
- 画给 decider 的那张图里，候选**先去壳**：窗口自己的壳（2×2 px）与渲染宿主（盖满客户区的 a11y 框）
  实测能把 0.58/0.42 的注意全吃掉，而任务那一行落到 1e-6。最多 k 个，**保留原始编号顺序**
  （模型看到的空间顺序才是人看界面的顺序）。图落在 `writable_dir(PROJECT_ROOT/'data','data')/decider/`，
  请求里给的是路径 —— 对面自己读。
- `autostart` 默认开（与 athand 同）：配了 `serve` 而没人应答就按
  `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 起一个（日志在同一个目录里），然后等它读完权重。
- **权限不变**：intent 仍然是桌控，仍然在 `pc_control` 那个开关后面（关掉 = 工具根本不进工具面）。

### 63.2 托盘浮窗停稳再点（athand 2026-09-17 修的实测缺陷）

通知区的溢出浮窗**边填边长大**，所以刚出现时读到的矩形可能指向隔壁那一格：实测第一次点击落在
**邻居图标**上、把那个应用的面板打开了（唤醒的重试救回来了，但「点开陌生人面板」不该留着）。
现在要等「浮窗自己的矩形 + 那一行的矩形」**连续两次采样不变**再点；到点还没停稳就用最后看到的
那个候选（调用方的重试就是它的兜底），而且只有什么都没找到时才把它关掉。Fungi 这一份是原样搬回来
的 —— 缺陷当初就在这里。

### 63.3 锁屏、或者输入桌面不是本会话时，如实说

`OpenInputDesktop` 能问出「现在接收输入的是哪个桌面」：不是 `Default`（`Screen-saver` / `Winlogon`）
就是锁屏或屏保。那种状态下 `SendInput` 全部失败、`GetCursorPos` 失败、`GetForegroundWindow` 回 0、
抓屏回来是空的 —— 以前这些一律报成「INJECTION FAILED」，也就是**把机器状态说成工具坏了**。

- `desktop_problem()` 在**注入之前**把输入动作拒掉，回的是「机器锁着/屏保，什么都没发出去」；
- **只读动作不受影响**：`windows`、`targets`、窗口 `shot` 仍然描述这台机器（锁屏时也能看）；
- `grab_screen()` 抓不动时抛 `ScreenUnavailable`，由工具入口（`_run`）转成 `ERROR: …`；
  整屏与窗口两条抓屏路径共用一个实现，所以诊断跟着每一张图走；
- **判据必须能在测试桩下活着**：测试里的 `screen._u32` 是个小 Python 桩，没有 `OpenInputDesktop`；
  所以 `input_desktop_name()` 对每个调用都容错，`""` 表示**说不准**（老 Windows、桩），
  这跟「锁着」是两回事 —— 只有真报出别的名字才算诊断。

### 63.4 没搬的（如实记）

- **`selftest`（18 项自检）**：它验的是「一个脚本能不能在这台机器上驱动桌面」，Fungi 的等价物是
  `tests/test_screen.py`（打桩）+ 真机验收配方，不需要第二套。
- **CLI 与 `_dispatch` 的参数解析、盘上的 listing/labels/strikes**：一次调用一个进程的记忆问题，
  Fungi 里 `Session` 就在进程内。
- **`_raise_for_input`**（type/key 要求目标真在前台，否则拒绝）：athand 那边实测到「前台是别的窗口时
  字打进别的窗口，而结果还报 done」。Fungi 这条路先走 `_guarded_input`（按应用自己的门把它抬起来），
  取舍不同所以没搬 —— **但那个洞在 Fungi 这边同样存在**（`set_foreground` 失败时 `type` 会把字打进
  前台那个窗口），值得单独做一次。

### 63.5 验收

`tests/test_screen.py`（+20 例，全部打桩：不碰真桌面、不摸真 UIA、不注入）：

- 浮窗**停稳再点**（两次采样一致才回，且点的是停稳后的那个矩形）· 从不停稳的浮窗仍然交出最后一次
  候选（不挂死、也不把它关掉）；
- 抓不动时 `_run` 回 `ERROR: the screen could not be captured …` · 输入桌面不是 `Default` 时
  **先拒注入**、`targets` 照常工作 · 测试桩下是「说不准」不是「锁着」；
- `intent=` 没配 decider 时给出理由（并且不动手）· 配了 decider 时挑出正确候选、结果里带上
  decider 的置信度 · `UNDECIDED` 时把问题交回调用方、不挑 · `name=` 多命中且没有 intent 时仍然是
  原来那句 `controls match` · 多命中**带** intent 时交给 decider · 没有 listing 时先建一份再问 ·
  schema 里有 `intent` 且仍受 `pc_control` 开关管 · 一个工具调用带着 intent 走完**不注入任何东西**。

- **配置来源与状态行**（63.1 那个入口）：`config.json` 的 `decider` 段被读到、报告里说出它在哪 ·
  `FUNGI_DECIDER` 压过文件 · 写成裸地址的环境变量回一句理由而不是异常 · 没配时状态行
  **一次网络都不碰** · 连上了把模型名带回来（问的就是配置里那个地址） · 没在跑 / 在加载 /
  加载炸了 / 没有权重各是一格 state —— 三句不同的人话，不能都叫「连不上」。

`tests/test_gui.py`（+6 例，offscreen）：

- Bixian 行夹在「实验性」与「拓展」之间 · 两格进页预填、回车写盘且 **decider 之外一个键不动**
  （手写的 `k` / 超时原样留着、粘进来的路径换正斜杠）· 带空格的命令存两次不碎（引号拼回去）·
  引号不成对**拒写**（一个字节不落盘）· 「保存并测试」先存后问、把 `/health` 的事实翻成人话 ·
  **进设置页不联网**（状态行只读盘，`/health` 最坏卡界面 3 秒）。

门禁：见 §62.6（同一棵树、同一次跑）。

## 64. 输出速率（2026-09-25 用户点名）：切换主题左边那个 tok/s

**用户原话**：「为fungi添加小功能：在切换主题的左边，实时显示输出速率，你可以通过计算在某段时间内消耗的
token数得到。」

### 64.1 一个流式 chunk 就是一个输出 token（本机实测，不引 tokenizer）

- **量的口径**：页面自己数 chunk。拿本机这份配置对着真端点校准过一次：**212 个 chunk / 213 个
  `completion_tokens` = 0.995**（真值来自 `stream_options.include_usage`）。所以「数 chunk」既不用
  tokenizer，也不用动协议 —— 一个字节的 Python 都没改。
- **文字与思考都算**：那 213 个 token 里 **180 个是 reasoning**。只数 `text` 的话，思考重的回合会读出
  接近 0 的速率，而模型其实正以全速在烧 token。
- **工具调用参数的 token 不算（如实记）**：页面根本看不到它们 —— `agent.py::wrap_reasoning_events`
  只转发 text/reasoning，tool_call 的参数是在整个调用拼好之后才由服务端发一个 `tool` 事件。于是
  「模型在憋一个长工具调用」的那几秒，读数是安静的。要在那段也有读数，得在服务端 `_apply_delta`
  旁边加计数器 + 一个新事件类型，那是另一条决定（见 64.4）。

### 64.2 两个 shell 都有一份，位置就在切换主题左边

- `web/index.html`（桌面 WebUI）与 `web/m.html`（手机端）各加一个 `<span id="tok-rate">`，紧挨在
  `#theme-switch` **前面**；计量实现只有一份：`web/common.js::tokenRate(el)`
  （挂成 `window.FungiCommon.tokenRate`）。两侧的 `handleTurnEvent` 在 `case 'text'` 与
  `case 'reasoning'` 各喂一口 —— 两份 shell 的 `handleTurnEvent` 本来就是逐字对齐的，所以两边各两行。
- **布局的坑（值得记）**：`.theme-switch` 原来靠 `margin-left:auto` 顶到 header 右端；若再给读数一个
  auto margin，flexbox 会把自由空间**对半分**，数字会飘到 header 中间去。所以那个 auto margin
  **搬到了 `#tok-rate` 身上**，`.theme-switch` 那份删掉。读数收起用 opacity（不是 `display:none`）：
  它必须一直占着位，否则那个 auto margin 一起消失、切换主题的开关会漂回左边
  （`test_the_desktop_reads_the_rate_left_of_the_theme_switch` 里 `themeLeft > 600` 就是钉这个）。
- 手机端 `#title-wrap` 本来就是 `flex:1`，读数直接坐进它和主题按钮之间，不用动锚定；390px 宽的真机
  宽度下实测：标题/模型名先截断，读数与主题按钮各就各位（`12 tok/s`）。

### 64.3 滑窗与生命周期（停笔停在最后一个数，只有新建对话才清空）

- 窗口 `RATE_WINDOW_MS = 1500`（用户说的「某段时间」），每 `RATE_TICK_MS = 150` 重算一次；
  速率 = 窗口里的 chunk 数 ÷ **样本自身的跨度**（`最后一个 chunk - 第一个`，没有地板）。
  - **分母不能用「最老样本到现在的年龄」**：那会把停笔之后的空档一起算进去，数字越等越低 ——
    2026-09-25 改成「停住」之后这条立刻现形（冻结值是 `8.8` 而不是 `20`，被新加的断言当场抓住）。
  - **样本太少的窗口干脆不报**（`RATE_MIN_SPAN_MS = 250` 之前只留上一个数）。原先用「300ms 地板」
    兜冷窗口，看着聪明，其实是**报一个假的低值**：开口第一个 chunk 会算成 `3.3 tok/s`。
    数字留住之后这一下会真的被眼睛看见（`21 → 3.3 → 10 → 20`），所以地板换成「先不说」——
    真出现掉坑时，逐帧记录的那条断言会在 `window.__rates` 里抓到 `3.3 tok/s`（实测过）。
- **生命周期（用户 2026-09-25 原话：「那个速率应该一直显示，对话结束时显示最后一次更新的速率，
  只有新建对话才不显示」）**：静默 `RATE_IDLE_MS = 1000` 之后**只停表**（`clearInterval` + 丢掉窗口），
  数字与 `on` 都留在屏上 —— 那就是「最后一次更新的速率」；**唯一的清空点是 `clear()`**，由两个 shell
  的 `newSession()` 调（桌面「+ New Session」/ 手机抽屉「＋ 新会话」）。**换会话不动它**（规矩就是
  「只有新建对话才不显示」）。interval 仍只在有 chunk 的时候存在 —— header 里常驻 rAF/interval 是
  `docs/webui-ux.md` 明令禁止的东西。
- 显示格式：`>=10` 取整（`42 tok/s`），`<10` 留一位小数（`7.4 tok/s`）；等宽数字
  （`font-variant-numeric:tabular-nums`），免得上跳的数字把旁边的主题开关挤得发抖。

### 64.4 没做 / 待定

- **不是 provider 的真 usage**：真 usage 只在流结束时才到（而且还得服务端主动要
  `stream_options.include_usage`），拿它做不了「实时」。本机实测 chunk≈token（0.995），
  但换一个会攒批的 provider（一个 chunk 里塞多个 token），读数就会偏低。
- **工具调用期间读数是安静的**（64.1）：这是「谁看得见这些字节」的直接后果。
- **好友/信使那条流不显示**：它走 `/comm-log` 轮询拿整段，没有逐 chunk 时间戳，量出来会是假速率。
- **只量本页自己那条流**：同一个会话在别的标签页/手机上开着时，各页面各有自己的读数。

### 64.5 验收

- `tests/test_webui_rate.py`（**3 例**，真 Chromium）：把 `stream_chat` 换成按 20 chunk/s 吐 40 片的假流
  （带哨兵字符串；信使那些调用照 `SilentLLM` 的老规矩瞬间静默）——
  - **桌面**：动手前 `#tok-rate` **存在但 opacity 0**（不是一直挂在那儿的装饰）→ 发一条 → 读数出现且
    落在 8–100（对应 20 chunk/s 的流）→ **位置 `right <= theme-switch.left`、两个中心同行（<12px）**
    → 停笔满 1 秒后数字**留在屏上**（`on` + opacity 1）且再等 1.2 秒**还是那几个字**（停住了，
    不是慢慢淡出）→ 切换主题的开关仍在右端（`themeLeft > 600`，钉 64.2 那个坑）。
  - **冷启动不许报假的低值**：桌面这条用 `MutationObserver` 逐帧把每一次改写记进 `window.__rates`
    （掉坑只持续一两帧，采样抓不住），断言最小值 ≥ 8。
  - **只有新建对话才清空**：停笔后的数字**换会话也不动**，点「+ New Session」才清空并收起；
    清空之后切换主题仍在右端（那个 auto margin 长在读数身上）。
  - **手机**：同一套速率/几何断言 + 停笔留存；抽屉里的「＋ 新会话」同样能清空（`#btn-menu` 先开抽屉）。
- 修前先红（`git stash push -- web`）：**3 failed** —— `停笔后读数不见了` / `停笔后手机上的读数没了` /
  `新建对话没有把读数清掉`。另单独验过「假低值」那条有牙：把 300ms 地板装回 `span` 一行，
  它当场抓到 `['3.3 tok/s', '10 tok/s']`。
- 视觉留证（假流截 header）：桌面上流中是 `10.0 tok/s`、手机 1280 宽 `12 tok/s`、手机 390 宽 `12 tok/s`，
  三处都在切换主题左边同一行；**停笔之后同一位是 `21 tok/s`**（5 秒采样不再变），点新建对话才消失。
- 门禁：`python -m ruff check .` 干净 · 本轮新增的这个测试文件 `ruff format --check` 干净
  （`fungi/` 里有 5 个文件本来就会被打回 —— `gui/config.py`、`hub/app.py`、`hub/client.py`、`landing.py`、
  `server.py`，都非本轮改动，没碰）· `PYTHONIOENCODING=utf-8 python -m pytest tests -q` →
  **760 passed / 271s**（其余 758 例原样，本轮就多这 2 例浏览器用例）。

## 65. 模型三件套只读 `config.json`（2026-09-25 用户点名）：不再认 `OPENAI_*` 通用名字

**用户原话**：「不是啊，为啥不能模型配置只读，非要去读环境变量？」

### 65.1 那次 401 的完整因果（留档）

- 报错：`⚠ (LLM error: HTTP 401 … "Authentication Fails, Your api key: ****sovy is invalid")`。
- 这台机器上**两份** `config.json`（仓库版 / 桌面安装版）里的钥匙都是 `…a5f4`（`api.deepseek.com`）——
  `sovy` 那把钥匙**根本不在任何配置里**。
- `HKCU:\Environment` 里躺着通用名字：`OPENAI_API_KEY = sk-cwgn…sovy`(51 字符) 与
  `OPENAI_BASE_URL = https://api.xiaomimimo.com/v1` —— 是给 Goose / strands 接**小米 MiMo** 的一对
  （`GOOSE_PROVIDER=openai` 也在）。
- 老规矩是 `OPENAI_API_KEY` / `OPENAI_ENDPOINT` / `OPENAI_MODEL` 三个通用名字**压过** config.json；
  环境里只设了钥匙、没设端点 → 端点仍是 config.json 的 DeepSeek：**拿别人的钥匙敲自己的端点**。
- 当时的日志只写 `api_key=True`（`logs/fungi-20260925.log`）—— 钥匙从哪儿来一个字都没有，
  于是那句报错只能靠猜。

### 65.2 裁决与实现

- `fungi/config.py::load_config` 里那三行覆盖**删掉**：模型三件套（api_key / endpoint / model）只有一个
  来源 = `config.json`。文件里没有就是没有，不去问环境。
- 仓库里**没有任何消费者**论证过那三行：`ci.yml` / `release.yml` 不用它，测试里没有一条用例钉它
  （所以删掉不红），发版与冻结包冒烟也不用它；`cli.py` 那句「Edit config.json or set OPENAI_API_KEY」
  跟着改成只提配置文件（GUI 设置页写的也是 config.json，用户有门可走）。
- **Fungi 自己的环境钩子一律带项目前缀**：`FUNGI_DECIDER`（§63）、`FUNGI_GUI_SCALE`（`gui/const.py`）、
  `FUNGI_SELFTEST`（`__main__.py`）。要再开口子照这个规矩来；通用名字不再认 —— ambient 环境里它们
  随时可能属于别的工具（这次就是）。
- 为这件事临时加的「横幅写来源 + 错配告警」**一并撤掉**：覆盖没了，诊断也就没必要了；横幅照旧只写
  「api_key 有没有」（§45）。

### 65.3 验收

- `tests/test_config.py::test_the_model_trio_comes_from_the_file_and_never_from_the_environment`：
  环境里塞满 `OPENAI_API_KEY` / `OPENAI_ENDPOINT` / `OPENAI_MODEL`，`load_config` 仍只认文件里那三样。
- 真机：清不清那两个变量**都一样**（源码版现在不装启动器也能连通）。
- 桌面那份打包版（0.8.0，冻结于 2026-09-21）里仍是老逻辑，**要重打包才吃到这条**；
  在那之前用 `C:\Users\37549\Tools\fungi-clean-env.cmd` 起 exe（脚本头里写着原因）。
- 门禁（都在这棵最终树上跑的）：`python -m ruff check .` 干净 · `python -m ruff format --check .` 干净 ·
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **762 passed / 282s**（64.5 那三条浏览器用例在内）。
- 顺手把 ruff 的历史漂移也收干净（用户：「那几个 ruff 也顺手改了」）：`ruff format --check .` 曾点出
  **7 个**文件（`gui/config.py` / `hub/app.py` / `hub/client.py` / `landing.py` / `server.py` /
  `scripts/make_ringtones.py` / `tests/test_hub_app.py`，共 112+/49-，全是手写换行与内联 dict 的旧形态），
  单独落一个纯格式提交 `254d8fd`，与本节的改动互不掺和。`scripts/check.ps1` 那一步**本来就是** `--check`
  （它头部注释也写着"改写无关文件不是门禁该干的事"）—— 漂移是树没跟上钉死的 `ruff==0.13.0`，不是脚本的错。


## 66. 模型下拉列表（2026-09-25 用户点名）：启动器与 WebUI 头部都改成「选中哪个用哪个」

**用户原话**（一条一条来的）：「启动器除现有输入框以外，添加模型下拉列表，下拉后选中哪个使用哪个」
「原先的输入框从覆盖变成添加，如果与之前模型不一样就新添。添加后自动测试模型调用」
「对于WebUI，需要你在左上角原来显示模型的位置也改成下拉列表，有关设计与启动器类似」。

### 66.1 一份列表，两个界面读它

- `config.json` 多一个 `model_list`：用过/加过的模型，**最近用的在最前**，正在用的那个永远在里头
  （`load_config` 兜底插入 —— 否则下拉列表会开着是空的）。
- **只有列表里还有别的名字时才写这个键**：从没切过模型的人，`config.json` 一个字节都不变。
- `remember_model(cfg, model)`：把名字挪到最前 + 设成 `cfg.model`，返回「是不是新名字」；
  同一个名字再来一次不加第二行（用户要的是"与之前不一样就新添"，不是每次回车都长一行）。
- 手改出来的重复项 / 空串 / 全角空格读进来会被去重丢掉（`dict.fromkeys`），下拉列表里不会出现两行一样。
- **没有第二份真相**：启动器、WebUI 头部、`/configure` 弹窗读写的都是这一个键。

### 66.2 启动器（`fungi/gui/config.py`）

- 「模型」这一处现在是 **两行**：上面一行是**下拉列表**（列表 = 可选模型，当前那个选中），下面一行还是
  原来那个输入框，但它现在是**添加**。挤成一行也放得下（220 + 240），但这一页每一行的控件都从第 90px
  那一列起、宽 360（`fungi/gui/widgets.py::_row`：标签 90 定宽 + 控件自身定宽），一行里塞两个控件会让
  这一行比别的行多探出去 190px —— 离屏抓过图确认现在四行左右两端都齐（`C:/tmp/scratch/fungi-models/`）。
- 下拉选中：写盘 → 自动测一次调用 → 状态行「已切换到 X」。
- 输入框回车：**添加**（不是覆盖）→ 进列表 + 切过去 → 自动测一次 → 「已添加并切换到 X」；
  框清空等下个名字 —— 框里不再印当前模型（那是下拉列表的活儿）。
- 探测走子线程（`llm.PROBE_TIMEOUT = 15`s 的硬期限），结果落到 `_probe_result` 由 250ms 的 `QTimer`
  取走（这一页的老办法：不用 Signal 传参）—— **界面线程不许等网络**（`_refresh_bixian` 立的规矩）。
- 结果说两处：`model_status` 一行（`✓ X 可用`，provider 报的名字和自己不一样就附上；失败 `✗ X 调不通：原话`）
  + 失败再来一条 `InfoBar.error`（6 秒）。**调不通不把选择撤回去**：用户可能就是要拿它当靶子试。
- `_pick_model` 用 `_loading_models` 挡住「程序自己填列表」触发的 `currentIndexChanged`：
  否则每进一次页面就等于自己切了一次模型（要写盘 + 发一次请求）。
- 探测期间用户又切了模型：那份回来的结果说的是上一个，直接丢掉（`model != load_config().model`）。

### 66.3 WebUI（`web/common.js::mountModelPicker`，两个壳共用）

- 头部那个只读的 `#model-name`（span，`/model` 一来就写进去）变成 `#model-select`——
  桌面上就是用户说的「左上角原来显示模型的位置」，手机壳 `/m` 顶上同一份实现（都是 `mountModelPicker`）。
  **自绘**下拉（不是原生 `<select>`：同日第二次报告见 §66.7），面板结构由这个函数自己建，
  触发器留在各自的 HTML 里（桌面在 `#header`、手机在 `#topbar`，CSS 各管各的位置）。
- `GET /model` → `{model, models, endpoint}`（§68 起多回一个 `endpoint`；**密钥永不上页面**）；
  `POST /model {model}` → 写盘（切模型连带换 url+key，§68）+ 探一次，回
  `{ok, model, models, endpoint, fresh, reachable, detail}`：**`ok` = 存住了，`reachable` = 它答了**
  （两件事分开报，不是同一个断言）。
- 结果也是两处：`#status` 一句话（下一轮对话会把它顶掉，所以那条通道只能当"顺口一说"）+
  触发器自己留颜色（`.ok` / `.bad`，8 秒后褪，完整那句留在 `title` 里）—— 句子没了，那次测试的结果还在。
  句尾带上落在哪台主机上（`@ api.xiaomimimo.com`）：一个列表里住两家时，"通了"得看得出通到哪家。
- 老缓存页面（没有 `#model-select`）不炸：`mountModelPicker` 找不到元素就返回 null，脚本其余部分照旧。
- `/configure`（首次配置弹窗那个 Model 框）也从「覆盖」改成「添加」：WebUI 里键入过的名字，
  之后就在头部下拉列表里。

### 66.4 探针为什么不是 `stream_chat`

`fungi/llm.py::probe_model`：**非流式**发一次极小补全（`max_tokens = PROBE_MAX_TOKENS = 16`，
`stream: false`，不带 tools，prompt 就一个 "ping"），只要一个事实 + provider 说它服务的模型名。
`stream_chat` 不合适：它的 `READ_TIMEOUT = 600` 是**每次读**的超时，探针要的是一条硬期限
（15 秒），而且流式那一整套（SSE 解析、tool_call 拼装、错误 payload 落盘）在"它答不答"这个问题上
一个字都用不上。失败时把 provider 的原话（`HTTP 401: …`）原样带回去 —— 设置页要显示的就是它。

### 66.5 验收

- `tests/test_config.py`（3 条）：列表round-trip（加/去重/切回，且只有一个名字时不写这个键）、
  手写文件只给了 `model` 时下拉列表不许空、手写重复项被去掉。
- `tests/test_llm.py`（3 条）：探针只花几个 token（`stream: false`、不带 tools、`max_tokens ≤ 16`）、
  把 provider 报的模型名带回来、失败带回原话、无人监听的端口要**立刻**回 `Connection failed`。
- `tests/test_gui.py`（2 条）：下拉选中 / 输入框添加都写盘 + 自动测（`probe_model` 打桩）+ 状态行出结果 +
  同名不添第二行；调不通时状态行是 `✗` 且选择不被撤回。
- `tests/test_webui_models.py`（6 条，真 Chromium + 真 `RoomServer` + 打桩的探针）：桌面下拉列表 =
  `config.json` 那份（`#model-name` 与任何原生 `<select>` 都已不在页面上）→ 点开 → 选一个 → 自动测 →
  写盘（旧的还在列表里）→ `ok` 颜色 + 状态行 + 自己收起来；**样式与动画**（面板底色 == `--surface`、
  圆角 == `--radius-sm`、开合逐帧采到中间帧）；调不通 → `bad` 颜色 + provider 原话，选择不撤回；
  §68 两条（切过去换 url+key 并在话里报出主机名、回 200 才学）；手机 `/m` 同一套断言。
- 修前红（`git stash push -- <源文件>`）：见 §66.6。
- 门禁（最终树）：见 §66.6。

### 66.6 修前红与门禁

- **修前红**（`git stash push -- fungi/config.py fungi/llm.py fungi/server.py fungi/gui/config.py web`，新用例
  全留在工作区；回合后 `git stash pop` 干净）：
  - `tests/test_config.py` → **3 failed**（`module 'fungi.config' has no attribute 'remember_model'`、
    两处 `'Config' object has no attribute 'model_list'`）；
  - `tests/test_gui.py -k "model or enter_saves"` → **3 failed**（`fungi.gui.config` 没有 `llm_mod`；
    改过契约那条是 `assert 'orig-model' == ''` —— 老代码把当前模型摆在框里，正好撞上新规矩）；
  - `tests/test_llm.py` → **1 error，整个文件收起**（`from fungi.llm import probe_model` 导不出来）；
  - `tests/test_webui_models.py` → **3 errors**（`fungi.server` 没有 `probe_model`）。
  改回实现后这 12 条全绿。
- **改了一条老用例的契约（不是重钉偶然行为）**：`test_enter_saves_config_from_any_field` 原来把「模型框
  回车 = 保存」也算在内，而用户这一条点名要的就是**把模型框从「覆盖」改成「添加」**。现在它只钉 key /
  接口地址两格，顺带断言模型不在输入框里、在下拉列表里选中（`currentText() == "orig-model"`）；模型那
  一格的新契约由两条新用例钉。修前红那一轮里它正是红的。
- **合并的账单（远端 09-23 那条测试基建改动）**：`test: a fresh clone counts as a machine too` 让测试沙箱不再
  继承本机的真实 `config.json`（改成 `{}`）→ `/config-status` 答「未配置」→ API-key 弹层上屏。
  alerts / friend / sessions / transfer 四个浏览器用例文件早就有那句 `config-overlay` 放行，
  `tests/test_webui_rate.py`（本批写的，那会儿本机有真 key，弹层从没上过屏）漏了它，桌面那两条用例点
  `#send` 被弹层挡到 30 秒超时 —— 合完第一次门禁的 4 failed 里就有它俩。已按同一句补上，并给本批两个新
  浏览器文件也装上（那边沙箱配置里写了 key，弹层本不该出现，这句是双保险）。
- **这棵树上的一份打包件不算数**：用户桌面上那份 `Fungi.exe`（`_internal\web\common.js` 里
  `mountModelPicker` 出现 0 次）里没有 §64/§65/§66 和远端 v0.9.0 —— 换 exe 之前他在启动器与 WebUI 里
  都看不到下拉列表。
- 门禁（最终树，含同日的 §66.7 自绘下拉、§68 端点记忆与 §69 启动器删模型）：
  `PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **791 passed**（303s）；`python -m ruff check .`、`python -m ruff format --check .` 全绿；
  工作区除用户自己的 `shots/` 外干净。
  §66 那批单独数过两次：改 §66.2 布局后 `tests/test_gui.py` 整文件 79 passed；§66.7/§68 落地后
  `test_config.py + test_gui.py + test_webui_models.py + test_webui_list_reflow.py` 共 **108 passed**（28s，
  没有一条浏览器用例再落在超时上）。§69 落地后（含「选中删除先收下拉」那条修正、以及把一条
  偶发红的 webui 断言改成等结果）全量 **791 passed**。

### 66.7 自绘下拉（同日第二次报告：「风格和原来的不搭，没有动画效果」）

第一版是原生 `<select>`：弹出层由系统画，吃不到这套 token（`--surface/--radius-sm/--shadow-card/
--t-fast`），也没有开合过程 —— 跟旁边那些自绘控件（主题开关、折叠卡、抽屉）根本不是一个东西。现在：

- **触发器**：沿用原来那行小字的排版（透明底、`--dim`、mono、0.72rem），悬停长边框、`:focus-visible`
  描边、右侧一个 CSS 三角（`.model-caret`）打开时翻 180°；ok/bad/busy 三个状态色照旧留在它身上。
- **面板**：`position:fixed` 挂在 `body` 上（`document.body.appendChild`），**不是**触发器的子节点 ——
  留在原地会被祖先的 `overflow:hidden` 裁掉：手机壳的 `#title-wrap{overflow:hidden}`（会话名省略号
  要用）正好套在触发器外面，实测行都在、就是不可见，Playwright 点不到（第一轮 6 条浏览器用例全红在
  这个 `element is not visible` 上）。位置由 `place()` 按触发器的 `getBoundingClientRect()` 算，
  滚动/改窗口尺寸时跟着走。
- **行**：当前那个带一枚勾（不只靠颜色 —— 色盲模式与打印也看得出）+ `aria-selected`；悬停与键盘
  高亮共用一套底色（`--accent` 12%）；`role="listbox"` / `role="option"`。
- **动效**：`motion.js` 新增 `menuIn/menuOut`（GSAP 0.20s `expo.out` / 0.14s `power2.in`，从**右上角**
  scale 出来 —— 那个角正是箭头所在），调用点一律 `window.fungiMotion?.menuIn?.(menu)`：
  `prefers-reduced-motion` 下模块整体折叠成 `{reduced:true}` → 两个函数不存在 → 瞬时开合，
  可见性本身由 JS 的 `hidden` 管，不靠动画（模块缺失时 UI 照常可用，这是动效契约）。
- **开合状态由 `isOpen` 说了算，不看 `menu.hidden`**：关闭补间还没跑完时 `hidden` 仍是 false，用它
  判断会让「快点两下」卡成「看着开着其实已经收掉」（实测就是这么红的）；`isOpen` 下再点开时
  `overwrite:'auto'` 掐掉那条 out 补间，`hidden=false` 立刻生效。
- **键盘**：触发器上 ArrowDown/Up 走行、Enter/Space 选中、Esc 收起；点别处自动收起。
- **名字包一层 span 再省略号**（`.model-trigger-name`）：裸文本节点在手机顶栏（窄、`#title-wrap`
  是 flex）会被挤成三行 `mimo-\nv2.6-\nflash`；包一层才能 `text-overflow:ellipsis`。
- **面板左沿对齐触发器并夹在视口里**（放不下就整体左移）：按右沿对齐时，手机触发器在顶栏最左边，
  面板从它往左长 → 直接长到屏幕外（实测左边几截字符被切掉）。真正**摆不摆得下**由 `place()`
  按触发器的 `getBoundingClientRect()` 算，滚动/改窗口尺寸时跟着走。
- **手机壳的宽度约束放 `.model-pick` 上**，不是触发器自己：写在触发器身上的 `max-width:44%`，
  包含块是它自己的内容宽度 → 百分比解析成几个字符（实测触发器被掐成 `mim...`）。
- **两条都钉进用例**（`tests/test_webui_models.py::test_the_dropdown_wears_this_uis_tokens_and_animates`）：
  面板底色 == 这一页 `--surface` 的解析值、圆角 == `--radius-sm`（换主题跟着换，不是写死的颜色），
  以及开/关**逐帧**采到的 `opacity` 里必须有中间帧 —— 瞬时出现就是在骗人。


## 67. 会话列表不再「重排」（2026-09-25 用户报告）：两次无谓的 DOM 搬动

**用户原话**：「然后我注意到，现在每次切换会话也好，停止会话也好，就会触发视图重排。具体来说，
每次新建会话，会话会短暂停留在文件传输助手，然后再下移；每次切换会话，底部的会话则会渲染在
好友列表之上。以前没有这个问题，我怀疑是文件传输助手那次引入的」。

### 67.1 真机量到的（探针在 `C:/tmp/scratch/fungi-reflow/`，21 个会话、1280×620、列表可滚动）

| 动作 | 修前 | 修后 |
|---|---|---|
| 一次纯 `renderSessionList()` | 130 次 childList 变更 · 20 帧有行 `position:absolute` · 9 行画到列表外 | **0 · 0 · 0** |
| 切一次会话（scrollTop 先放 150） | 260 次变更 · 22 帧绝对定位行 · **scrollTop 被夹回 0** | **0 · 0 · scrollTop 150→150** |
| 新建会话 | 新行连着 **20 帧停在「文件传输助手」那一格**（top 109；它的终点是 147） | 新行**第一帧就在 147**，0 帧重叠 |

### 67.2 两处根因（都在桌面壳 `web/app.js` + `web/motion.js`）

1. **`renderSessionList` 每行无条件 `list.appendChild(row)`**：`appendChild` 对已在文档里的节点
   是**搬动**，所以一次重绘就是 N 次 DOM 搬动（21 行 = 130 次 childList 变更），连「顺序根本没变」
   的重绘（切会话只改 `.active`、停一轮只改那个圆点、铃声/提醒轮询）也照搬一遍。列表又是个滚动
   容器，搬动把 `scrollTop` 顶掉 —— 用户看到的「视图重排」就是它。
   现在：**只搬位置不对的行**（从后往前放，锚点 = 下一个该在它后面的行），并且**顺序没变时连 Flip
   都不进**。后者也顺手省掉 GSAP 为一次「什么也不动」的动画把测量代理 `<div>` 往列表里插 24 次。
2. **`listFlip` 的 `absolute: true`**（GSAP Flip 会把要移动的行在动画期间 `position:absolute` 拿出
   文档流）有三个后果，用户报的两条占了两个：
   - 列表的**内容高度当场塌掉** → 滚动容器把 `scrollTop` 夹回去，动画结束行回到流里，滚动位置却
     留在顶上（切换会话时 150 → 0）；
   - 绝对定位的行**不再被 `#session-list{overflow:auto}` 裁剪**（它的包含块跑到列表外面去了）
     → 动画期间画到下面的好友列表上（量到 9 行）；
   - 而 `mutate()` 里**新插入**的那一行不在 `Flip.getState` 里（Flip 从没移动过它），于是它就在
     塌掉的流里排版：**新建会话时正好画在「文件传输助手」那一格上**，等动画把别人挪走才落到自己的
     位置 —— 用户那句「会话会短暂停留在文件传输助手，然后再下移」。
   这个列表的行尺寸不变（都一样高），所以 transform 就够：行的包含块还在列表里，被照常裁剪。

### 67.3 为什么「以前没有」

两处缺陷都比 §53 早（`e41fbd7` 的 `absolute: true`、`3ff1cf3` 的 keyed reconciliation 就在），
但它们是**条件性**的：文件传输助手把一行钉在列表顶上（新会话固定插在它下面），于是「新行画在那一
格上」有了固定的对照物，一眼就能看出来；而「滚动位置被顶掉」「行画到好友列表上」要列表长到能滚、
好友区块在下面才看得见。用户的感觉是对的 —— 是这次它变成了每次都能看见。

### 67.4 验收

- `tests/test_webui_list_reflow.py`（3 例，真 Chromium + 真 `RoomServer`，16 个会话 + 620px 高的
  窗口保证列表可滚）：① 切一次会话 = **0 次 childList 变更**、行还是同一批节点、`scrollTop` 不回顶；
  ② 顺序没变的重绘 = 0 次变更；③ 新建会话：逐帧记 `getBoundingClientRect`，断言**没有任何一帧**有行
  是 `position:absolute`、新行的 top **从第一帧起就等于它的终点**、且**没有一帧**落在文件传输助手那一格上。
- 手机壳（`web/m.js`）不在这条路上：它每次重绘是**重建**行（没有 Flip、没有绝对定位），所以既不会
  重叠也不会画到别处；本批没动它（用户报的是桌面那个有好友列表的壳）。
- 门禁：见 §66.6。

## 68. 每个模型记住自己的端点与密钥（2026-09-25 用户点名）：切换模型随之切换 url 和 key

**用户原话**：「确实，每次调用 200 以后，之后切换模型应该是随之切换 url 和 key 的」。

### 68.1 墙在哪：一个 config 只有一套端点

§66 做完，用户把 `Mimo-v2.6-flash` 加进了启动器的下拉列表 —— 然后发现用不了：`fungi/llm.py` 的
`stream_chat` / `probe_model` 只吃**一个** `endpoint` + `api_key`，`config.json` 也只有一格。切模型
只改了 `model` 这个名字，请求照样发到上一家的地址上。而两家根本不通：小米网关（`GET /v1/models`
实测）只服务 9 个 `mimo-*`（v2.5 / v2.6 家族），DeepSeek 那边没有 MiMo —— 所以「一个下拉列表里放
两家模型」在旧结构下根本不成立，这不是配置写错了。

### 68.2 记什么、什么时候记（学下来的两个时刻）

`config.json` 多一个 `model_providers`：模型名 → `{endpoint, api_key}`。

- **探针回来 200**：切/加模型都会探一次（§66），那一刻把它这一次用的 url+key 记在这个名字上。
- **用户手填并保存**：设置页那两个框回车、WebUI 设置弹层的保存 —— 用户写下的就是口供。
- **答不上来不记**：没证据的配对不写进 `config.json`（✗ 那一轮只在屏上报原话）。
- `switch_model(cfg, name)` = `remember_model`（进列表 + 在用）+ `adopt_provider`（把这名字记下的
  那套摊到 `endpoint`/`api_key` 上）—— 两个界面、所有调用点都走它，语义只有一处。
- 没记过的名字**不动现状**：用户可能正打算手填一套，替他把端点指向别处才是意外。
- 手改了一半的记录（缺 `endpoint` 或缺 `api_key`、名字空、值不是 dict）读进来就当没有这一条：
  留着它只会在下次切换时把请求指向一个空地址。
- 名字离开列表也留着记录（一条几十字节）：再敲回那个名字就能直接用，这是记忆的全部意义。
- 写盘只写**变了**的：`remember_provider` 返回 False 时一个字节都不动（每次切/加模型都会走到这条路）。

### 68.3 接线（谁在什么时候调）

| 位置 | 动作 |
|---|---|
| `fungi/config.py` | `provider_for` / `adopt_provider` / `remember_provider` / `switch_model`；`load_config`/`save_config` 读写 `model_providers` |
| `fungi/gui/config.py` | `_write_model` 走 `switch_model`；切完 `_show_provider` 把端点框与密钥掩码一起换；探针回 200 → `_remember_provider`；`_save`（手填那两格）也记一次 |
| `fungi/server.py` | `POST /model` 走 `switch_model`，`reachable` 才记；`/configure` 先切（带出旧记忆）、再让手填的值覆盖它、然后记下这一套；`GET /model` 多回一个 `endpoint`（**密钥永不上页面**） |
| `fungi/agent.py` | 每轮按名字取配对：`provider_for(cfg, model) or (cfg.endpoint, cfg.api_key)` —— 分层模型（`models: {1: 名字}`，trilayer）因此也能坐落在另一家上 |
| `web/common.js` | 结果那句话报出落在哪台主机上（`✓ x answers @ api.xiaomimimo.com`）：一个列表里住两家时，「通了」得看得出通到哪家 |

### 68.4 验收

- `tests/test_config.py`（3 条）：两家来回切的 round-trip（含落盘/读回）、没记过的名字不动现状、
  半条记录被丢掉且不会被采用。
- `tests/test_gui.py`（2 条）：下拉切到有记忆的名字 → 端点框与密钥掩码跟着换 + 盘里换；探针 200 才学、
  ✗ 不学。
- `tests/test_webui_models.py`（2 条，真 Chromium）：切过去写对 url+key、话里报出主机名、切回来那套还在；
  回 200 才学。
- 真机（2026-09-25）：`mimo-v2.6-flash`（`https://api.xiaomimimo.com/v1/chat/completions`，小米那把 key）
  经 Fungi 自己的代码跑通文本/工具/视觉三样（`收到`；`get_time` 被调用；读出手写数字 `7341`）；
  `deepseek-v4-flash-vision-exp` 那套留在记忆里，切换即回到 DeepSeek。探针实测两条都 `reachable`，
  `detail` 与配置名一致。
- 门禁：见 §66.6。

## 69. 启动器能删模型了（2026-09-25 用户点名）：右键菜单 → 确认 → 从列表里拿掉

**用户原话**：「启动器补一个模型的删除，和会话列表那样，悬停显示，点击删除弹出确认」；
试到一半时放宽口径：「没事，你也可以选择别的实现方式，比如右键菜单？」

### 69.1 为什么最后是右键，不是悬停

「悬停显示一个删除钮」这种交互天生属于**常驻列表**（会话列表就是），而 §66 那个模型下拉的
弹出层是**瞬时**的：qfluentwidgets 每次打开都新建一份 `ComboBoxMenu`（`QMenu` + 一堆 `QAction`），
鼠标一离开就没了，在里头做悬停钮既别扭也不好按。右键菜单则正好——弹出层已经是"列表摊开"的状态，
右键那一行、点「删除」、确认，全程不离开那份列表。所以：**一份列表既管切换也管管理**，不新增第二种视图。

### 69.2 怎么接上去（`fungi/gui/config.py::_ModelComboBox`）

- `ComboBox._createComboMenu()` 是**每次打开**都走的钩子 → 在它返回的菜单的 `view` 上装
  `customContextMenuRequested`。那个 view 是 `QListWidget`（实测：`MenuActionListWidget` 就是它），
  行的**序号**正好等于这个 combo 的 item 序号（`_showComboMenu` 按 items 顺序 `addAction`），
  于是 `view.row(item)` → `self.itemText(index)` 就是模型名，不用去猜行上的文字。
- 空白处右键不给菜单（`itemAt(pos)` 为 None 直接返回）——菜单底部有留白，那儿点出来一个"删除"
  只会让人以为删的是别人。
- 菜单只放一条：`删除 <名字>`（`FluentIcon.DELETE`）→ **先把下拉收起来**（`_closeComboMenu()`）再发
  `delete_requested(str)`。这一步是用户实测出来的：「弹出对话框时下拉列表未收回，导致遮挡住了对话框」
  —— 右键是在弹出层里发生的，弹出层这会儿正开着，不收它就压在随后弹出的确认框上。用例钉住
  trigger 之后 `model_combo.dropMenu is None`。
- `_row_menu()`（真正弹菜单那一层）与 `_on_row_menu()`（把坐标解析成名字那一层）分成两个方法：
  模态菜单在用例里不能真弹，测试只打桩前者就能走完"右键 → 菜单 → 删除"整条线（和 `_DayDialog`
  一个路子）；确认框同理，`_ask_delete(body)` 是单独的方法。

### 69.3 删的时候发生什么

- `config.forget_model(cfg, name)`：从 `model_list` 去掉**且**丢掉 `model_providers[name]` ——
  与 §68「离开列表也留着记录」相反的那一半：切换走掉只是不用了，**明确删除是说"这个不会再用"**，
  他手写的那把 key 也该跟着走。
- **在用的那个删得掉**，但确认框先把话说清（「它正在使用，删掉会切到 X」），删完 `switch_model`
  切过去并照常探一次。不让人删自己正在用的东西，只会逼他先切一次再回来删。
- **列表里只剩一个时拒绝**（InfoBar 说明理由，连确认框都不弹）：`cfg.model` 必须留在
  `model_list` 里（§66 的不变式），删空 = 把程序配成没有模型可用。
- 删完重画 `_load_fields()`（下拉 + 端点/密钥那两格；切过模型的话 §68 会连带换 url+key）并刷状态行。

### 69.4 验收

- `tests/test_config.py`（2 条）：`forget_model` 从列表与记忆里一起拿掉（空名字/不在列表里返回
  False）、删完落盘再读回来它不会自己回来。
- `tests/test_gui.py`（5 条）：
  ① **真**弹出层（`_showComboMenu()` 建的那份，只把模态 `exec` 打桩）里右键第二行 → 菜单里就一条
  「删除 m2」→ trigger 它 → **下拉先收起来**、`config.json` 里 m2 连同它的端点+密钥记录一起没了，
  在用的 m1 不动；
  右键落在行外的留白上不给菜单；② 确认框说「算了」就什么都不发生；③ 删在用的那个会先切到剩下的
  并自动测一次；④ 只剩一个时直接拒绝（不弹确认）；⑤ 手改 config.json 后右键一个不在列表里的名字：
  什么也不做。
- WebUI 那侧没动：它读的是同一份 `config.json`，启动器删完，头部的下拉下次 `load()` 就是新的
  （用户点名的是启动器）。
- 门禁：见 §66.6。

## 70. 帮助页整页重写（2026-09-25 用户点名）：通俗、简洁、面向读者，并独立出「模型配置」一节

**用户原话**（一条一条来的）：「完善帮助中有关模型配置的内容，最好独立出一节」
「你这帮助啊，给我写的1.通俗易懂2.简洁，不要把我们开发过程中遇到的事情都写进去，要面向读者」
「整个帮助都得改」。

### 70.1 改了什么

- **整页重写** `fungi/gui/help.py::HELP_SECTIONS`（10 节 → 11 节）与页尾的日志提示。口径就三条：
  通俗易懂、简洁、面向读者——开发过程里的取舍与实现机制不进帮助页，每节只留「读者要做的动作 +
  会看到什么 + 需要知道的边界」。删掉的典型内容：
  - 桌面控制：`（screen）` 标注、「编号图 / 大眼睛 / 小眼睛」那套机制比喻、`API` 字样 →
    换成「填上它的地址，Agent 拿不准该点哪个时，由它先替你看一眼」；
  - GhostWorld：「源码检出 / 解压出来的 exe 发行包那一层 / 已 pip 安装的可留空」这类安装形态括注、
    「坐标和清单不够用时靠这个确认」的实现动机 → 只留读者要做的前提（游戏开着 +
    `ghostworld_dir` 指向游戏目录）；
  - 「Agent 能帮你做什么」：`起一个交互式进程（python 的 REPL、脚手架、开发服务器）` →
    「开个程序边跑边答：你一行行喂给它，它一行行读回来」；
  - 信使：删掉「它向你提问不会把自己卡住」这类实现细节；
  - 页尾日志提示：删掉「游戏通道有没有反应」的排障内幕。
- 字数：原有 10 节 1875 字 → 改写后（不含新节）1512 字，**少 363 字（−19%）**；加上新节
  「模型配置」219 字，全页 1731 字。单节：桌面控制 304→231、GhostWorld 320→206、信使 374→244。
- **新增独立一节「模型配置」**（插在「设置」之前，219 字），把 §65 / §66 / §68 / §69 的用户可见面
  讲成人话：下拉选中即用、输入框回车添加并切过去、Fungi 自动测一次并在下面那行报通没通；
  接口地址与 API Key 回车即保存（留空不改）、每个模型记着自己那一套（切换跟着换、密钥不上屏）；
  下拉里右键删除（只剩一个删不了）；WebUI 左上角是同一份列表，删除在设置页做。
  环境变量、`model_list`、探针参数、掩码实现这类内部口径一概不写。
- 「设置」那节随之瘦掉：模型那一句移进新节，本体只剩页面分区指引（信使 / 来信提醒 / 实验性 / 拓展）
  与 inbox/、README 与 docs/ 的指引。
- 帮助页是**纯文案**：`HELP_SECTIONS` 只被 `HelpPage.__init__` 和 `gui/__init__.py` 的 re-export 引用，
  没有用例钉它的字面量，所以不新增用例。

### 70.2 验收

- 真机平台（`python`，非 offscreen）构造 `HelpPage` 后 `body.grab()` 整页截图：11 节、新节排在
  「设置」**之前**、长段落逐行折行无裁切、页尾「打开日志目录」按钮照旧 →
  `C:/tmp/scratch/fungi-help/help-full.png`（探针 `grab.py` 同目录）。
- 门禁（这棵最终树）：`PYTHONIOENCODING=utf-8 python -m pytest tests -q` → **791 passed**（321s，未新增
  用例）；`python -m ruff check .`、`python -m ruff format --check .` 全绿；工作区除用户自己的 `shots/`
  外干净。
