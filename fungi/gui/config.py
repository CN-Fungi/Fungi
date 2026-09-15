"""Settings page: model config, courier switch, VidSense."""

import os
import shutil
import subprocess
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    FluentIcon,
    InfoBar,
    LineEdit,
    PushButton,
    SubtitleLabel,
    SwitchButton,
)

from .. import config as config_mod
from ..config import DEFAULT_API_KEY, PROJECT_ROOT
from ..tools.video import _HEALABLE, _module_available, _video_ready
from . import ring
from .widgets import _row


def _hf_hub_missing() -> bool:
    """True when Fungi's Python lacks huggingface_hub (download deps)."""
    return not _module_available("huggingface_hub")


class ConfigPage(QWidget):
    """模型配置：迁移自 WebUI 的配置弹窗（api_key / endpoint / model）。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("configPage")
        self._preview: ring.Ringer | None = None  # 试听用的播放器（懒建）

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 14, 48, 14)
        root.setSpacing(6)
        title = SubtitleLabel("设置")
        root.addWidget(title)

        self.key_edit = LineEdit()
        self.key_edit.setFixedWidth(360)
        self.key_edit.setPlaceholderText("API Key（留空 = 保持不变）")
        root.addWidget(_row("API Key", self.key_edit))

        self.endpoint_edit = LineEdit()
        self.endpoint_edit.setFixedWidth(360)
        root.addWidget(_row("接口地址", self.endpoint_edit))

        self.model_edit = LineEdit()
        self.model_edit.setFixedWidth(360)
        root.addWidget(_row("模型", self.model_edit))

        # 没有保存按钮：三个输入框回车即写盘（见文件末尾 returnPressed 接线），
        # 用鼠标点走不算（editingFinished 故意不接）。
        root.addSpacing(4)

        # 信使：消息信使（自动回复，可注入记忆）+ 文件信使（consent 卡片，零 Agent）
        root.addSpacing(10)
        root.addWidget(SubtitleLabel("信使"))
        msg_row = QHBoxLayout()
        msg_lbl = BodyLabel("信使")
        msg_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        msg_row.addWidget(msg_lbl)
        msg_row.addSpacing(8)
        self.courier_switch = SwitchButton()
        self.courier_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # setChecked BEFORE connect: the InfoBar needs window_ref (see diary).
        self.courier_switch.setChecked(config_mod.load_config().courier)
        msg_row.addWidget(self.courier_switch)
        self.courier_switch.checkedChanged.connect(self._toggle_courier)
        msg_row.addStretch(1)
        root.addLayout(msg_row)
        courier_hint = BodyLabel(
            "开着时，你不在电脑前也有人替你接待朋友：重要的转告你，寻常的代你答。\n"
            "关掉则留言直达会话视图，不惊动 Agent。即时生效。"
        )
        courier_hint.setWordWrap(True)
        root.addWidget(courier_hint)
        file_hint = BodyLabel("文件不经信使：一律先弹卡片征求你的同意，收下的文件落在 inbox/ 里。")
        file_hint.setWordWrap(True)
        root.addWidget(file_hint)

        # 来信提醒：铃声开关 + 铃声选择（关掉就把下面那行收起来）
        root.addSpacing(10)
        root.addWidget(SubtitleLabel("来信提醒"))
        ring_row = QHBoxLayout()
        ring_lbl = BodyLabel("铃声")
        # SwitchButton's default size policy is Expanding: without pinning both
        # widgets to Fixed the switch drifts to mid-row (same as diary below).
        ring_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        ring_row.addWidget(ring_lbl)
        ring_row.addSpacing(8)
        self.ring_switch = SwitchButton()
        self.ring_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # setChecked BEFORE connect (checkedChanged fires on programmatic sets)
        self.ring_switch.setChecked(config_mod.load_config().ring)
        ring_row.addWidget(self.ring_switch)
        self.ring_switch.checkedChanged.connect(self._toggle_ring)
        ring_row.addStretch(1)
        root.addLayout(ring_row)
        ring_hint = BodyLabel(
            "朋友留言还没读时响一声，托盘图标一直闪到那条被读掉；点开好友视图即停。\n"
            "关掉只是不响，未读照样闪。"
        )
        ring_hint.setWordWrap(True)
        root.addWidget(ring_hint)
        self.tone_combo = ComboBox()
        self.tone_combo.addItems([label for _, label in ring.TONES])
        saved_tone = config_mod.load_config().ring_tone
        self.tone_combo.setCurrentIndex(
            ring.TONE_IDS.index(saved_tone) if saved_tone in ring.TONE_IDS else 0
        )
        self.tone_combo.setFixedWidth(180)
        self.tone_combo.currentIndexChanged.connect(self._preview_tone)
        # 当前选中的那一首也要能听（换选项才响的旧行为，选回原样就没法试听）
        self.preview_btn = PushButton("试听")
        self.preview_btn.clicked.connect(self._preview_selected)
        self.tone_row = _row("铃声选择", self.tone_combo, self.preview_btn)
        self.tone_row.setVisible(self.ring_switch.isChecked())
        root.addWidget(self.tone_row)

        # 实验性（大标题）：还在长、随时会改的功能——日记 / 桌面控制。
        # 「拓展」（下一节）装的是已经有独立项目的现成能力（GhostWorld / VidSense）。
        root.addSpacing(10)
        root.addWidget(SubtitleLabel("实验性"))
        diary_title_row = QHBoxLayout()
        diary_lbl = BodyLabel("Diary")
        # SwitchButton's default size policy is Expanding: without pinning
        # both widgets to Fixed the switch drifts to mid-row instead of
        # sitting right next to the label.
        diary_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        diary_title_row.addWidget(diary_lbl)
        diary_title_row.addSpacing(8)
        self.diary_switch = SwitchButton()
        self.diary_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # setChecked BEFORE connecting: checkedChanged fires on programmatic
        # changes too, and _toggle_diary pops an InfoBar that needs window_ref.
        self.diary_switch.setChecked(config_mod.load_config().diary)
        diary_title_row.addWidget(self.diary_switch)
        self.diary_switch.checkedChanged.connect(self._toggle_diary)
        diary_title_row.addStretch(1)
        root.addLayout(diary_title_row)
        diary_hint = BodyLabel(
            "让 Agent 记一本自己的日记——只有它自己能看，被问到也守口如瓶。\n"
            "关闭后工具与记忆注入一并移除。"
        )
        diary_hint.setWordWrap(True)
        root.addWidget(diary_hint)

        # 桌面控制（小标题）：默认关；开着时本机 Agent 能看屏并动手（spec §35）
        root.addSpacing(10)
        pc_row = QHBoxLayout()
        pc_lbl = BodyLabel("桌面控制（screen）")
        pc_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        pc_row.addWidget(pc_lbl)
        pc_row.addSpacing(8)
        self.pc_switch = SwitchButton()
        self.pc_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # setChecked BEFORE connect (checkedChanged fires on programmatic sets)
        self.pc_switch.setChecked(config_mod.load_config().pc_control)
        pc_row.addWidget(self.pc_switch)
        self.pc_switch.checkedChanged.connect(self._toggle_pc_control)
        pc_row.addStretch(1)
        root.addLayout(pc_row)
        pc_hint = BodyLabel(
            "让本机 Agent 看屏幕、点控件、粘文本。打开开关即表示你同意它直接动手；\n"
            "关掉开关立刻收回，并释放所有还按着的键。\n"
            "注意：截图会随工具结果发给模型；需要管理员权限的窗口它够不到。"
        )
        pc_hint.setWordWrap(True)
        root.addWidget(pc_hint)

        # 拓展（大标题）：已经有独立项目的现成能力搬进来——VidSense 与 GhostWorld
        # 都是能单独跑的东西，不是还在长的实验品（用户 2026-09-15 定调）。
        root.addSpacing(10)
        root.addWidget(SubtitleLabel("拓展"))
        # GhostWorld 角色控制（小标题）：默认关；开着时本机 Agent 驱动游戏里的一个角色（spec §43）
        root.addSpacing(10)
        gw_row = QHBoxLayout()
        gw_lbl = BodyLabel("GhostWorld 角色控制")
        gw_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        gw_row.addWidget(gw_lbl)
        gw_row.addSpacing(8)
        self.gw_switch = SwitchButton()
        self.gw_switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # setChecked BEFORE connect (checkedChanged fires on programmatic sets)
        self.gw_switch.setChecked(config_mod.load_config().ghostworld)
        gw_row.addWidget(self.gw_switch)
        self.gw_switch.checkedChanged.connect(self._toggle_ghostworld)
        gw_row.addStretch(1)
        root.addLayout(gw_row)
        gw_hint = BodyLabel(
            "让本机 Agent 在本机的 GhostWorld 里控制一个角色：玩家说话它就醒过来，用角色自己的嘴回话、\n"
            "走动、拾取东西。需要游戏正在运行（config.json 的 ghostworld_dir 指向游戏目录；"
            "已 pip 安装的可留空）。\n"
            "关掉开关立刻收回：工具从工具面移除，等玩家说话的监视进程也一并结束。"
        )
        gw_hint.setWordWrap(True)
        root.addWidget(gw_hint)

        # 视频理解（小标题）：进场自动检查，缺失才给下载入口（video 工具拒绝现场下载）
        root.addSpacing(10)
        video_lbl = BodyLabel("视频理解")
        video_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        root.addWidget(video_lbl)
        self.video_label = video_lbl  # tests pin its place under 拓展
        self.video_status = BodyLabel()
        self.video_status.setWordWrap(True)
        root.addWidget(self.video_status)
        self.download_btn = PushButton(FluentIcon.DOWNLOAD, "下载缺失模型")
        self.download_btn.clicked.connect(self._download_models)
        root.addWidget(self.download_btn)

        root.addStretch(1)
        self.status = BodyLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        # reactive status line: reflect unsaved edits live
        for edit in (self.key_edit, self.endpoint_edit, self.model_edit):
            edit.textChanged.connect(self._refresh_status)
        # Enter commits from whichever field you are in, exactly like 保存配置
        # (which also clears all three afterwards: the key never lingers on
        # screen). editingFinished would save on tab-out too — not wanted.
        for edit in (self.key_edit, self.endpoint_edit, self.model_edit):
            edit.returnPressed.connect(self._save)
        self._load_fields()
        self._refresh_status()

        # 视频模型状态轮询：下载子进程退出后自动复检（不用 Signal 传参）
        self._dl_proc: subprocess.Popen | None = None
        self._dl_steps: list[tuple[str, list[str]]] = []
        self._dl_stage = ""
        self._dl_timer = QTimer(self)
        self._dl_timer.setInterval(1000)
        self._dl_timer.timeout.connect(self._poll_download)
        self._check_video_models()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # 模型可能在别处（命令行/WebUI）改了：进页就照当前配置刷新三个框
        self._load_fields()
        # 模型可能在别处（命令行）补装了；下载中则保持进度文案不动
        if self._dl_proc is None:
            self._check_video_models()

    def _check_video_models(self) -> None:
        try:
            ready = _video_ready()
        except OSError as exc:
            self.video_status.setText(f"VidSense 状态检查失败：{exc}")
            self.download_btn.hide()
            return
        marks = " · ".join(f"{name} {'✓' if ok else '✗'}" for name, ok in ready.items())
        if getattr(sys, "frozen", False):
            # A frozen exe has no interpreter of its own (vidsense runs as
            # `sys.executable -m vidsense.cli`, which there is Fungi.exe) and
            # cannot see a system Python's site-packages either, so installing
            # anything is a dead end — say so instead of offering the button
            # (2026-09-11). The rest of Fungi is unaffected.
            self.video_status.setText(
                f"{marks}\n本地视频理解只在源码方式下可用（exe 里没有 Python 解释器，"
                "跑不了 vidsense 子进程）：需要它就用 python start.py 跑源码。"
            )
            self.download_btn.setVisible(False)
            self.download_btn.setEnabled(False)
            return
        missing = [name for name, ok in ready.items() if not ok]
        healable = [name for name in missing if name in _HEALABLE]
        if missing:
            hint = "点「下载缺失模型」自动补齐" if healable else "需手动安装 VidSense 依赖"
            self.video_status.setText(f"{marks}\n缺 {'、'.join(missing)}，{hint}")
        else:
            self.video_status.setText(f"{marks}\n已就绪，video 工具可用")
        # 只有可自愈缺失才亮下载按钮（不需要就没有按钮）；下载进行中禁点
        self.download_btn.setVisible(bool(healable))
        self.download_btn.setEnabled(bool(healable) and self._dl_proc is None)

    def _python_cmd(self) -> str | None:
        """Interpreter for helper subprocesses: Fungi's own Python in dev;
        frozen exe has none, fall back to a system Python on PATH."""
        if not getattr(sys, "frozen", False):
            return sys.executable
        return shutil.which("python")

    def _download_models(self) -> None:
        if self._dl_proc is not None:
            return
        py = self._python_cmd()
        if py is None:
            self.video_status.setText("未找到系统 Python（exe 模式需先安装 Python 并加入 PATH）")
            return
        script = PROJECT_ROOT / "scripts" / "download_video_models.py"
        if not script.is_file():
            self.video_status.setText(
                "下载脚本缺失（scripts/download_video_models.py），请手动预装模型。"
            )
            return
        # 阶段链：缺 huggingface_hub 就先自动装依赖，再下模型
        self._dl_steps: list[tuple[str, list[str]]] = []
        if _hf_hub_missing():
            self._dl_steps.append(
                ("依赖 huggingface_hub", [py, "-m", "pip", "install", "huggingface_hub"])
            )
        self._dl_steps.append(("VidSense", [py, str(script)]))
        self._start_next_dl_step()

    def _start_next_dl_step(self) -> None:
        stage, cmd = self._dl_steps.pop(0)
        self._dl_stage = stage
        try:
            self._dl_proc = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.video_status.setText(f"下载启动失败：{exc}")
            self._dl_proc = None
            return
        self._check_video_models()  # 先禁用按钮, 再写进度文案(复检会覆写状态行)
        self.video_status.setText(f"正在下载{stage}…（进度见弹出的控制台，完成后自动复检）")
        self._dl_timer.start()

    def _poll_download(self) -> None:
        if self._dl_proc is None or self._dl_proc.poll() is None:
            return
        code = self._dl_proc.returncode
        self._dl_proc = None
        if code == 0 and self._dl_steps:
            self._start_next_dl_step()  # 依赖装完 -> 接着下模型
            return
        self._dl_timer.stop()
        self._check_video_models()
        if code == 0:
            InfoBar.success("下载完成", "VidSense 已就绪", duration=2500, parent=self.window_ref)
        else:
            InfoBar.error(
                "下载失败",
                f"「{self._dl_stage}」步骤退出码 {code}，详见其控制台窗口",
                duration=4000,
                parent=self.window_ref,
            )

    @staticmethod
    def _key_mask(key: str) -> str:
        """Display form of the stored key: recognisable, not usable."""
        if not key or key == DEFAULT_API_KEY:
            return ""
        return (key[:5] + "…" + key[-4:]) if len(key) > 12 else "…" + key[-4:]

    def _load_fields(self) -> None:
        """Show the stored values in the boxes (2026-09-10 user report: three
        empty boxes meant "what is configured?" was only readable from the
        status line). Endpoint and model carry the real values — they are not
        secrets. The key is masked into the placeholder instead: the box shows
        which key is in use, the secret itself stays on disk, and an empty box
        keeps meaning "leave it alone" (typing into it never nests inside a
        displayed value)."""
        cfg = config_mod.load_config()
        mask = self._key_mask(cfg.api_key)
        self.key_edit.setPlaceholderText(
            f"当前 {mask}（留空 = 保持不变）" if mask else "API Key（留空 = 保持不变）"
        )
        self.endpoint_edit.setPlaceholderText("留空 = 保持不变")
        self.model_edit.setPlaceholderText("留空 = 保持不变")
        self.endpoint_edit.setText(cfg.endpoint)
        self.model_edit.setText(cfg.model)
        self.key_edit.clear()  # 只有掩码在占位符里：真 key 从不上屏

    def _refresh_status(self) -> None:
        cfg = config_mod.load_config()
        key = self.key_edit.text().strip() or cfg.api_key
        state = "已配置" if key and key != DEFAULT_API_KEY else "未配置（使用占位 key，无法对话）"
        self.status.setText(
            f"当前状态：{state}\n改完按回车即保存（三个输入框各自生效；留空 = 保持不变）"
        )

    def _save(self) -> None:
        cfg = config_mod.load_config()
        if self.key_edit.text().strip():
            cfg.api_key = self.key_edit.text().strip()
        if self.endpoint_edit.text().strip():
            cfg.endpoint = self.endpoint_edit.text().strip()
        if self.model_edit.text().strip():
            cfg.model = self.model_edit.text().strip()
        config_mod.save_config(cfg)
        # 保存后回到当前值（不是清空）：框里始终显示的就是正在用的配置
        self._load_fields()
        self._refresh_status()
        InfoBar.success(
            "已保存", "模型配置已写入 config.json", duration=2500, parent=self.window_ref
        )

    def _toggle_diary(self, checked: bool) -> None:
        """实验性日记开关：即时写盘，下一轮对话生效（agent 每轮重建）。"""
        cfg = config_mod.load_config()
        cfg.diary = bool(checked)
        config_mod.save_config(cfg)
        InfoBar.success(
            "已保存",
            "日记已开启，下一轮对话生效" if checked else "日记已关闭，下一轮对话生效",
            duration=2500,
            parent=self.window_ref,
        )

    def _toggle_pc_control(self, checked: bool) -> None:
        """桌面控制开关（spec §35.2）：即时写盘，本机 Agent 下一轮拿到/丢掉工具。

        关掉时顺手收回已授权的动作——不然「现在不许」要等 10 分钟才生效。
        """
        cfg = config_mod.load_config()
        cfg.pc_control = bool(checked)
        config_mod.save_config(cfg)
        if not checked:
            from ..tools import screen  # noqa: PLC0415 (desktop control only)

            screen.disarm()
        InfoBar.success(
            "已保存",
            "桌面控制已开启：下一轮对话里 Agent 能看屏并直接动手"
            if checked
            else "桌面控制已关闭：下一轮起工具移除，已授权的动作也一并收回",
            duration=2500,
            parent=self.window_ref,
        )

    def _toggle_ghostworld(self, checked: bool) -> None:
        """GhostWorld 角色控制开关（spec §43）：即时写盘；关掉时结束监视进程。

        监视器是常驻子进程，所以关掉要顺手 disarm——不然"现在不许"要等到房间退出才生效。
        开启在下一轮对话生效（工具补进 clone，监视器随之 arm）。
        """
        cfg = config_mod.load_config()
        cfg.ghostworld = bool(checked)
        config_mod.save_config(cfg)
        if not checked:
            from ..tools import ghostworld  # noqa: PLC0415 (character control only)

            ghostworld.disarm()
        InfoBar.success(
            "已保存",
            "角色控制已开启：下一轮对话里 Agent 能驱动 GhostWorld 里的角色"
            if checked
            else "角色控制已关闭：下一轮起工具移除，等玩家说话的监视进程也一并结束",
            duration=2500,
            parent=self.window_ref,
        )

    def _toggle_ring(self, checked: bool) -> None:
        """来信铃声开关：即时写盘（下一次响铃就按新设置来）。"""
        cfg = config_mod.load_config()
        cfg.ring = bool(checked)
        config_mod.save_config(cfg)
        self.tone_row.setVisible(bool(checked))  # 关掉就不显示铃声选择
        InfoBar.success(
            "已保存",
            "来信会响铃" if checked else "来信不再响铃（托盘仍闪动）",
            duration=2500,
            parent=self.window_ref,
        )

    def _preview_tone(self, index: int) -> None:
        """选一个铃声就试听一次，同时写盘：只听名字分不出哪个是哪个。"""
        tone = ring.TONE_IDS[index] if 0 <= index < len(ring.TONE_IDS) else ring.DEFAULT_TONE
        cfg = config_mod.load_config()
        cfg.ring_tone = tone
        config_mod.save_config(cfg)
        if self._preview is None:
            self._preview = ring.Ringer()
        self._preview.preview(tone)

    def _preview_selected(self) -> None:
        """试听按钮：听**当前选中**的那一首（下拉框的槽只在换选项时才响）。

        `clicked` 传的是 checked(bool)，不是索引——所以这条不带参数。
        """
        self._preview_tone(self.tone_combo.currentIndex())

    def _toggle_courier(self, checked: bool) -> None:
        """信使开关：即时写盘；通讯 clone 每个信封重读配置，无需重启。"""
        cfg = config_mod.load_config()
        cfg.courier = bool(checked)
        config_mod.save_config(cfg)
        InfoBar.success(
            "已保存",
            "信使已开启" if checked else "信使已关闭：对面消息直达，Agent 零消耗",
            duration=2500,
            parent=self.window_ref,
        )
