/* Fungi WebUI i18n — two languages, Chinese by default.
 *
 * Loaded before common.js in both shells (desktop index.html, mobile m.html),
 * right after /lang.js. The language is a LAUNCHER setting (settings page →
 * config.json `ui_lang`), not a WebUI control: /lang.js is generated from
 * `load_config()` on every request and sets `window.FUNGI_LANG` before this
 * file runs. No localStorage override — the launcher is the one place it is
 * set, so the desktop page and the phone cannot disagree.
 *
 * Strings are flat keys. `t(key, vars)` substitutes `{name}` placeholders.
 * Static markup carries the key, never the text:
 *   data-i18n              -> textContent
 *   data-i18n-html         -> innerHTML (only for strings that need <br>)
 *   data-i18n-placeholder  -> placeholder attribute
 *   data-i18n-title        -> title attribute
 * JS-built rows call t() directly at paint time.
 *
 * Changing the setting takes effect on the next page load (a reload is what
 * the settings page tells the user); there is no in-page switch to reload for.
 */
(function () {
  'use strict';

  const DICT = {
    zh: {
      /* ---- shared ---- */
      'ui.close': '关闭',
      'ui.send': '发送',
      'confirm.title': '确认？',
      'confirm.ok': '确定',
      'confirm.cancel': '取消',
      'time.now': '刚刚',
      'time.today': '今天',
      'time.yesterday': '昨天',
      'time.beforeYesterday': '前天',
      'session.untitled': '未命名',
      'session.emptyTitle': '空白会话',
      'shuttle.title': '文件传输助手',

      /* ---- config modal ---- */
      'cfg.title': '配置 Fungi',
      'cfg.intro': '填好接口地址和密钥就能开始。',
      'cfg.endpoint': '接口地址',
      'cfg.apiKey': 'API Key',
      'cfg.model': '模型',
      'cfg.skip': '跳过',
      'cfg.save': '保存',
      /* 403 不是「未配置」：页面能打开，但读不到任何数据（BUG-08） */
      'auth.expired': '未授权：这个页面需要令牌才能读取数据。请回到启动器「手机端」页，用带令牌的链接重新打开。',
      'cfg.keyRequired': '请填写 API Key。',

      /* ---- desktop shell (index.html) ---- */
      'ui.filter': '筛选会话…',
      'ui.newSession': '＋ 新建会话',
      'ui.sessionsEmpty': '还没有会话。<br>点「＋ 新建会话」开始。',
      'ui.friends': '好友',
      'ui.friendsEmpty': '房间暂无其他主机。',
      'ui.toggleSidebar': '展开/收起侧栏',
      'ui.modelTitle': '当前模型：点开换一个，换完自动测一次调用',
      'ui.rateTitle': '输出速率：每秒输出 token 数（流式 chunk ≈ 1 token，文字与思考都算）；停笔后停在最后一个数，新建对话才清空',
      'ui.themeTitle': '切换深浅主题',
      'ui.friendBarFiles': '来自该好友的文件/权限请求',
      'ui.inputPlaceholder': '输入消息…（Enter 发送，Shift+Enter 换行）',
      'ui.friendInputPlaceholder': '给好友发消息…（Enter 发送，Shift+Enter 换行）',
      'ui.browseFile': '选择文件',
      'ui.sendFile': '发送文件',
      'ui.scrollBottom': '回到底部',
      'ui.deleteSession': '删除会话',
      'ui.subagents': '子代理',

      /* ---- mobile shell (m.html) ---- */
      'mobile.menu': '会话列表',
      'mobile.back': '返回会话',
      'mobile.newSession': '＋ 新会话',
      'mobile.filter': '筛选会话…',
      'mobile.sessionsEmpty': '还没有会话。<br>点「＋ 新会话」开始。',
      'mobile.sessionsEmptyPlain': '还没有会话。',
      'mobile.friends': '好友',
      'mobile.friendsEmpty': '房间暂无其他主机。',
      'mobile.friendBarPerm': '来自该好友的权限请求',
      'mobile.rescanTitle': '链接已失效或 token 不对。',
      'mobile.rescanHint': '请回到电脑端 GUI「手机端」页重新扫码。',

      /* ---- transcript / mail / files ---- */
      'transcript.fromUser': '来自 {who} 的用户',
      'transcript.thinking': '思考中…',
      'transcript.silent': '（Agent不说话）',
      'live.running': '运行中…',
      'live.error': '出错',
      'file.fromPhone': '手机上传',
      'file.fromPc': '电脑发送',
      'file.file': '文件',
      'file.pull': '下载到手机',
      'file.reveal': '打开所在目录',
      'mail.placeholder': '评价一下',
      'mail.send': '发送',
      'mail.sending': '发送中…',
      'mail.sent': '已发给信使',
      'mail.failed': '发送失败',
      'mail.notReady': '信使还没就绪，稍后再试',
      'spawn.title': '点按查看子代理详情',

      /* ---- ask cards ---- */
      'ask.submit': '提交',
      'ask.required': '必填',
      'ask.customPlaceholder': '或者自己输入…',
      'ask.answerPlaceholder': '你的回答…',
      'ask.noAnswer': '\u23F3 未回答',

      /* ---- consent ---- */
      'consent.allow': '允许',
      'consent.ask': '询问',
      'consent.deny': '禁止',
      'consent.send': '发送',
      'consent.fromFallback': '远端主机',
      'consent.placeholder': '自定义回复（可选）',
      'consent.placeholderDesktop': '自定义回复（可选，留空直接点允许/禁止）',
      'consent.hint': '· 需要长期放行？在好友会话顶部把滑块拨到「允许」',

      /* ---- transfer modal ---- */
      'xfer.waiting': '等待…',
      'xfer.done': '完成',
      'xfer.failed': '失败',
      'xfer.uploadIncompleteParts': '上传未完成：还有分片没到',
      'xfer.uploadIncomplete': '上传未完成',
      'xfer.interruptedAt': '中断在 {at} 字节',
      'xfer.partial': '只收到 {got} / {total} 字节',
      'xfer.cannotFetch': '取不到这个文件',
      'xfer.browserDownload': '交给浏览器下载',
      'xfer.waitingPeer': '等待对方接收…',
      'xfer.peerGotPath': '对方已收到：{path}',
      'xfer.peerGot': '对方已收到',
      'xfer.peerRefused': '对方没有收下',
      'xfer.peerStillReceiving': '对方仍在接收，结果会出现在会话里',
      'xfer.stepUploadRoom': '① 上传到房间',
      'xfer.stepPeerReceive': '② 对方接收',
      'xfer.stepUploadPc': '① 上传到电脑',
      'xfer.stepUploadRoom2': '② 上传到房间',
      'xfer.stepPeerReceive3': '③ 对方接收',
      'xfer.sending': '正在发送…',
      'xfer.uploading': '正在上传…',
      'xfer.sendingToPeer': '正在送往对方…',

      /* ---- model picker ---- */
      'model.switching': '正在切换到',
      'model.answers': '可用',
      'model.failed': '调不通',
      'model.servedAs': '实际是',

      /* ---- desktop shell (app.js) ---- */
      'desktop.rename': '重命名',
      'desktop.delete': '删除',
      'desktop.deleteTitle': '删除会话',
      'desktop.deleteMessage': '「{title}」将被永久删除，不可恢复。',
      'desktop.deleteConfirm': '删除',
      'desktop.noMatches': '没有匹配的会话。',
      'desktop.noSessions': '还没有会话。',
      'desktop.shuttleTitle': '文件传输助手：两台设备之间只搬文件，不会改名也不会被删除',
      'desktop.shuttleMeta': '只搬文件',
      'status.thinking': '思考中…',
      'status.retrying': '重试中…',
      'status.resuming': '后台任务完成，继续…',
      'status.reattaching': '回合仍在服务器上运行，正在接续…',
      'status.writing': '正在输出…',
      'status.runningTool': '正在运行 {name}…',
      'status.stopping': '正在停止…',
      'status.aborted': '已中止。',
      'status.turnFailed': '回合失败。',
      'status.connectionLost': '连接中断。',
      'status.retryHint': '连接中断。按 Alt+R 重试。',
      'status.http': '错误：HTTP {status}',
      'status.error': '错误：{msg}',
      'agent.bgCommand': 'Bg 命令',
      'agent.l3Worker': 'L3 Worker',
      'agent.l2TaskAgent': 'L2 Task Agent',
      'agent.goal': '目标：',
      'agent.replyFormat': '回复格式：',
      'agent.status': '状态：',
      'agent.unknown': '未知',
      'agent.free': '（自由）',
      'agent.finished': '✔ 完成',
      'agent.failed': '✘ 失败',
      'agent.stopped': '■ 停止',
      'friend.transfer': '文件传输',
      'friend.delegatedTo': '📥 委派给 {host}：',
      'friend.replied': '✔ {host} 回复：',
      'friend.me': '我',
      'friend.userFrom': '来自 {host} 的用户',
      'friend.agentOf': '{host} 的 Agent',
      'alert.ask': 'Agent 在等你回答',
      'alert.done': '回答完毕',
      'alert.error': '出错了',
      'alert.new': '有新消息',
      'xfer.sendToFriend': '发送文件给 {name}',

      /* ---- mobile shell (m.js) ---- */
      'mobile.stop': '停止',
      'mobile.stopForce': '再点一次强制断开',
      'mobile.retry': '重试上一回合',
      'mobile.stopping': '正在停止…',
      'mobile.disconnected': '已断开。',
      'mobile.retrying': '重试中…',
      'mobile.resuming': '后台任务完成，继续…',
      'mobile.reattaching': '回合仍在服务器上运行，正在接续…',
      'mobile.errorHttp': '错误：HTTP {status}',
      'mobile.errorMsg': '错误：{msg}',
      'mobile.aborted': '已中止。',
      'mobile.turnFailed': '回合失败。',
      'mobile.connectionLost': '连接中断。',
      'mobile.runningTool': '⚙ {name}…',
      'mobile.pullTitle': '从电脑取文件',
      'mobile.pullStep': '下载到手机 · {name}',
      'mobile.pulling': '正在取…',
      'mobile.browserDownloaded': '大文件已交给浏览器下载',
      'mobile.savedToPhone': '已保存到手机：{name}',
      'mobile.pullFailed': '下载失败：{msg}',
      'mobile.uploadTitle': '上传文件到电脑',
      'mobile.uploadStep': '上传到电脑 · {name}',
      'mobile.uploading': '正在上传…',
      'mobile.savedToPc': '已保存到电脑',
      'mobile.uploadFailed': '上传失败：{msg}',
      'mobile.noEvents': '（暂无事件）',
      'mobile.replyFormat': '回复格式：{fmt}',
      'mobile.fileFallback': '文件',
    },

    en: {
      'ui.close': 'Close',
      'ui.send': 'Send',
      'confirm.title': 'Are you sure?',
      'confirm.ok': 'OK',
      'confirm.cancel': 'Cancel',
      'time.now': 'now',
      'time.today': 'Today',
      'time.yesterday': 'Yesterday',
      'time.beforeYesterday': 'Two days ago',
      'session.untitled': 'Untitled',
      'session.emptyTitle': 'Empty',
      'shuttle.title': 'File transfer assistant',

      'cfg.title': 'Configure Fungi',
      'cfg.intro': 'Set your API endpoint and key to get started.',
      'cfg.endpoint': 'Endpoint',
      'cfg.apiKey': 'API Key',
      'cfg.model': 'Model',
      'cfg.skip': 'Skip',
      'cfg.save': 'Save',
      /* 403 is not "not configured": the shell loads, nothing inside does (BUG-08) */
      'auth.expired': 'Not authorized: this page needs a token to read anything. Reopen it from the launcher\'s phone page (the link carries the token).',
      'cfg.keyRequired': 'API key is required.',

      'ui.filter': 'Filter...',
      'ui.newSession': '+ New Session',
      'ui.sessionsEmpty': 'No sessions yet.<br>Click + New Session to start.',
      'ui.friends': 'Friends',
      'ui.friendsEmpty': 'No other hosts in the room.',
      'ui.toggleSidebar': 'Toggle sidebar',
      'ui.modelTitle': 'Current model: click to switch; the switch is tested once',
      'ui.rateTitle': 'Output rate: tokens per second (one streamed chunk ≈ 1 token, text and reasoning alike); it freezes when the model stops and clears only on a new conversation',
      'ui.themeTitle': 'Toggle light/dark theme',
      'ui.friendBarFiles': 'Files and permission requests from this friend',
      'ui.inputPlaceholder': 'Ask anything... (Enter to send, Shift+Enter for newline)',
      'ui.friendInputPlaceholder': 'Message your friend... (Enter to send, Shift+Enter for newline)',
      'ui.browseFile': 'Browse for file',
      'ui.sendFile': 'Send a file',
      'ui.scrollBottom': 'Scroll to bottom',
      'ui.deleteSession': 'Delete session',
      'ui.subagents': 'Subagents',

      'mobile.menu': 'Sessions',
      'mobile.back': 'Back to session',
      'mobile.newSession': '+ New Session',
      'mobile.filter': 'Filter sessions...',
      'mobile.sessionsEmpty': 'No sessions yet.<br>Click + New Session to start.',
      'mobile.sessionsEmptyPlain': 'No sessions yet.',
      'mobile.friends': 'Friends',
      'mobile.friendsEmpty': 'No other hosts in the room.',
      'mobile.friendBarPerm': 'Permission requests from this friend',
      'mobile.rescanTitle': 'This link is stale or the token is wrong.',
      'mobile.rescanHint': 'Scan the QR code again from the desktop GUI\u2019s Phone page.',

      'transcript.fromUser': 'User from {who}',
      'transcript.thinking': 'Thinking…',
      'transcript.silent': '(Agent stays silent)',
      'live.running': 'running…',
      'live.error': 'error',
      'file.fromPhone': 'From phone',
      'file.fromPc': 'From computer',
      'file.file': 'file',
      'file.pull': 'Download to phone',
      'file.reveal': 'Open containing folder',
      'mail.placeholder': 'Add a note',
      'mail.send': 'Send',
      'mail.sending': 'Sending…',
      'mail.sent': 'Sent to the courier',
      'mail.failed': 'Send failed',
      'mail.notReady': 'The courier is not ready yet, try again later',
      'spawn.title': 'Click to view subagent details',

      'ask.submit': 'Submit',
      'ask.required': 'Required',
      'ask.customPlaceholder': 'Or type your own...',
      'ask.answerPlaceholder': 'Your answer...',
      'ask.noAnswer': '\u23F3 No answer',

      'consent.allow': 'Allow',
      'consent.ask': 'Ask',
      'consent.deny': 'Deny',
      'consent.send': 'Send',
      'consent.fromFallback': 'remote host',
      'consent.placeholder': 'Custom reply (optional)',
      'consent.placeholderDesktop': 'Custom reply (optional; leave empty to just allow/deny)',
      'consent.hint': '· Need standing permission? Move the slider to "Allow" at the top of the friend view',

      'xfer.waiting': 'Waiting…',
      'xfer.done': 'Done',
      'xfer.failed': 'Failed',
      'xfer.uploadIncompleteParts': 'Upload incomplete: some parts never arrived',
      'xfer.uploadIncomplete': 'Upload incomplete',
      'xfer.interruptedAt': 'Interrupted at {at} bytes',
      'xfer.partial': 'Received only {got} / {total} bytes',
      'xfer.cannotFetch': 'Cannot fetch this file',
      'xfer.browserDownload': 'Handed to the browser',
      'xfer.waitingPeer': 'Waiting for the other side…',
      'xfer.peerGotPath': 'Delivered: {path}',
      'xfer.peerGot': 'Delivered',
      'xfer.peerRefused': 'The other side did not accept it',
      'xfer.peerStillReceiving': 'The other side is still receiving; the result will appear in the conversation',
      'xfer.stepUploadRoom': '① Upload to the room',
      'xfer.stepPeerReceive': '② The other side receives',
      'xfer.stepUploadPc': '① Upload to the computer',
      'xfer.stepUploadRoom2': '② Upload to the room',
      'xfer.stepPeerReceive3': '③ The other side receives',
      'xfer.sending': 'Sending…',
      'xfer.uploading': 'Uploading…',
      'xfer.sendingToPeer': 'Sending to the other side…',

      'model.switching': 'Switching to',
      'model.answers': 'answers',
      'model.failed': 'failed',
      'model.servedAs': 'served as',

      'desktop.rename': 'Rename',
      'desktop.delete': 'Delete',
      'desktop.deleteTitle': 'Delete session',
      'desktop.deleteMessage': '"{title}" will be permanently removed. This cannot be undone.',
      'desktop.deleteConfirm': 'Delete',
      'desktop.noMatches': 'No matches.',
      'desktop.noSessions': 'No sessions yet.',
      'desktop.shuttleTitle': 'File transfer assistant: moves files between your devices; it cannot be renamed or deleted',
      'desktop.shuttleMeta': 'Files only',
      'status.thinking': 'Thinking...',
      'status.retrying': 'Retrying...',
      'status.resuming': 'Background task finished - continuing...',
      'status.reattaching': 'Turn still running on the server...',
      'status.writing': 'Writing...',
      'status.runningTool': 'Running {name}...',
      'status.stopping': 'Stopping...',
      'status.aborted': 'Aborted.',
      'status.turnFailed': 'Turn failed.',
      'status.connectionLost': 'Connection lost.',
      'status.retryHint': 'Connection lost. Press Alt+R to retry.',
      'status.http': 'Error: HTTP {status}',
      'status.error': 'Error: {msg}',
      'agent.bgCommand': 'Bg Command',
      'agent.l3Worker': 'L3 Worker',
      'agent.l2TaskAgent': 'L2 Task Agent',
      'agent.goal': 'Goal:',
      'agent.replyFormat': 'Reply format:',
      'agent.status': 'Status:',
      'agent.unknown': 'unknown',
      'agent.free': '(free)',
      'agent.finished': '✔ finished',
      'agent.failed': '✘ failed',
      'agent.stopped': '■ stopped',
      'friend.transfer': 'file transfer',
      'friend.delegatedTo': '📥 delegated to {host}:',
      'friend.replied': '✔ {host} replied:',
      'friend.me': 'Me',
      'friend.userFrom': 'User from {host}',
      'friend.agentOf': "{host}'s Agent",
      'alert.ask': 'The agent is waiting for your answer',
      'alert.done': 'Answer ready',
      'alert.error': 'Something went wrong',
      'alert.new': 'New message',
      'xfer.sendToFriend': 'Sending a file to {name}',

      'mobile.stop': 'Stop',
      'mobile.stopForce': 'Click again to force disconnect',
      'mobile.retry': 'Retry the last turn',
      'mobile.stopping': 'Stopping…',
      'mobile.disconnected': 'Disconnected.',
      'mobile.retrying': 'Retrying…',
      'mobile.resuming': 'Background task finished, continuing…',
      'mobile.reattaching': 'Turn still running on the server, reattaching…',
      'mobile.errorHttp': 'Error: HTTP {status}',
      'mobile.errorMsg': 'Error: {msg}',
      'mobile.aborted': 'Aborted.',
      'mobile.turnFailed': 'Turn failed.',
      'mobile.connectionLost': 'Connection lost.',
      'mobile.runningTool': '⚙ {name}…',
      'mobile.pullTitle': 'Fetch a file from the computer',
      'mobile.pullStep': 'Download to phone · {name}',
      'mobile.pulling': 'Fetching…',
      'mobile.browserDownloaded': 'Large file handed to the browser',
      'mobile.savedToPhone': 'Saved to the phone: {name}',
      'mobile.pullFailed': 'Download failed: {msg}',
      'mobile.uploadTitle': 'Upload files to the computer',
      'mobile.uploadStep': 'Upload to the computer · {name}',
      'mobile.uploading': 'Uploading…',
      'mobile.savedToPc': 'Saved to the computer',
      'mobile.uploadFailed': 'Upload failed: {msg}',
      'mobile.noEvents': '(no events yet)',
      'mobile.replyFormat': 'Reply format: {fmt}',
      'mobile.fileFallback': 'file',
    },
  };

  // /lang.js ran first and carries the launcher's choice. Chinese when it is
  // absent or names anything we do not have — never the browser's locale.
  const lang = window.FUNGI_LANG === 'en' ? 'en' : 'zh';

  function t(key, vars) {
    const table = DICT[lang] || DICT.zh;
    let s = table[key];
    if (s === undefined) s = DICT.en[key];
    if (s === undefined) s = key;
    if (vars) {
      for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
    }
    return s;
  }

  function apply(root) {
    root = root || document;
    document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : 'en');
    root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    root.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.setAttribute('placeholder', t(el.dataset.i18nPlaceholder));
    });
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.setAttribute('title', t(el.dataset.i18nTitle));
    });
  }

  window.FungiI18N = {
    t,
    apply,
    get lang() { return lang; },
  };

  apply();  // scripts sit at the end of <body>: the markup is already there
})();
