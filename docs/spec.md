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
  服务端从 store 复制暂存（上限 `max_file_mb`，config.json，默认 200），envelope 只传元数据；
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

**真机（2026-09-15 本机实测，无 LLM）**：headless 游戏 + GhostWorld 仓库自带的 `headless_player.py`（联调夹具：起服务、连一个会说活的玩家——
无头游戏里没人说话，它就把这件事做掉），直接调用本模块：

- `send_command({"cmd": "pos"})` → `{"type": "position", "x": 7.5, "y": 1.5, "facing": 1.57, "map": "smoke.json"}`；`look` → 完整 perception；
- `arm()` 之后 **0.4s** 拿到玩家的真实发言（两轮实测：`--say` 那句原样到达）（`seq=1, kind=wake, from=player`，且该事件**发表于它连上之前**——没丢）；
- `disarm()` → 监视器与子进程都消失。

**验收**：`tests/test_ghostworld.py`（19 例，全用假 CLI／假子进程，不碰真游戏、不起真进程）+
`tests/test_gui.py::test_the_ghostworld_switch_sits_under_experimental_and_disarms_when_off`。
**未做**：真机 LLM 回合（要 API key，留给用户）——回合契约由既有的 FakeLLM 测试覆盖，本模块只负责"叫醒"与"命令往返"。
