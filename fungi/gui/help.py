"""Built-in help text and its page."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    PushButton,
    SubtitleLabel,
    TitleLabel,
)

from .. import runlog

HELP_SECTIONS = [
    (
        "Fungi 是什么",
        "一款局域网里的多人 Agent 协作网络。一台电脑「发起房间」，同一网络里的朋友「加入房间」，"
        "从此每台电脑上都住着一位会干活、有记忆的 Agent，而且它们彼此认识。"
        "不用公网，所有流量只在局域网里走。",
    ),
    (
        "怎么开始玩",
        "点「发起房间」，拿到房间 IP 和 Token 发给朋友；朋友点「加入房间」，填上这两样就进来"
        "（同一网络下 IP 可以不填，会自动找到你）。双方打开 WebUI，就能和自己的 Agent 对话。"
        "Token 像一张门票，可以自己定；房间开着时换了它，朋友要拿新的才能再进来。"
        "房主点「离开房间」，房间解散。",
    ),
    (
        "Agent 能帮你做什么",
        "查资料、写代码、整理文件是基本功。它认得图，读得了 docx / pptx / xlsx，也看得懂视频——"
        "「把这个视频讲了什么总结一下」就是一句话的事。耗时的活它会派帮手去后台并行干，"
        "干完向你汇报；也可以整件事交给朋友电脑上的 Agent 去办。动你的文件之前，它会先问一句。"
        "它还能开个程序边跑边答：你一行行喂给它，它一行行读回来。",
    ),
    (
        "桌面控制",
        "设置页「实验性」一节有「桌面控制」开关，默认关。打开它，Agent 就看得见你的屏幕，"
        "也能点控件、打字、按键——打开即表示你同意它直接动手；截图会随结果发给模型，"
        "需要管理员权限的窗口它够不到。适合那些没有接口可调的琐事：开个应用、"
        "在聊天窗口发句话、把挡路的窗口挪开。关掉立刻收回，按着的键也会松开；离开房间同样。"
        "同一节还有「Bixian」：填上它的地址，Agent 拿不准该点哪个时，由它先替你看一眼；"
        "不填就照旧由 Agent 自己看，实在认不出才拿着图来问你。",
    ),
    (
        "GhostWorld 角色控制",
        "设置页「拓展」一节有「GhostWorld 角色控制」开关，默认关。打开它，Agent 能驱动本机 "
        "GhostWorld 里的一个角色：玩家在游戏里说话时它会被叫醒，用角色自己的嘴回话、走动、拾东西，"
        "还能拍一张第一人称的画面，看看视野里有什么。前提是游戏正开着，"
        "并在 config.json 里把 ghostworld_dir 指向游戏目录。关掉立刻收回。"
        "它碰不到游戏的内部状态，只会通过游戏自己的命令通道说话。",
    ),
    (
        "信使",
        "你的每一位好友主机，都有一位专职对接它的信使。开启信使（设置页，默认开）后，"
        "朋友找你而你不在电脑前，它替你接待：重要的转告你，寻常的替你答——"
        "答的依据是信使页上的长期记忆（你的习惯与交代）和四周日历（点日期就能记，"
        "Agent 也会自己写）。约见面、通话这类事，它会和对面信使把时间地点一件件定下来，"
        "写进日历，拿不准的才来问你。每次汇报下面都有评价框，想纠正它就写在那里，对面看不到。"
        "朋友发来的文件要你点头才落盘。不想让 Agent 参与就关掉信使，"
        "Fungi 便成了一款简易版局域网微信。",
    ),
    (
        "skill 与日记",
        "Agent 有两种记忆。理性的是技能：踩过的坑、摸出的门道，它会记下来，下次不再犯，"
        "你也可以亲自教它。感性的是日记：设置页可以开启，什么触动了它，它就用自己的语气记下来。"
        "日记是它的私人领地，界面没有入口，我们不建议翻看；它被问到也会守口如瓶。",
    ),
    (
        "手机也能用",
        "「手机端」页扫码即可：聊天、好友、收发文件一样不少。躺着让家里的电脑干活，完全可行。"
        "手机发文件多一步：先传到电脑，再由电脑送给对方，进度条会告诉你走到哪了。",
    ),
    (
        "关了窗口房间还在",
        "关掉窗口并不停房：Fungi 缩进托盘继续守着，这时点托盘图标就直接进 WebUI 的好友视图；"
        "托盘菜单里还有「显示主界面」与「退出」。想真正离开，用菜单的「退出」。"
        "再启动程序会直接唤起主界面。",
    ),
    (
        "模型配置",
        "设置页最上面就是模型配置：下拉列表里是能用的模型，选中哪个就用哪个；"
        "下面的输入框里打一个新名字回车，它就加进列表并切过去，Fungi 会自动测一次，"
        "通没通写在下面那一行。接口地址和 API Key 回车即保存，留空表示不改；"
        "每个模型都记着自己那一套，切过去就跟着换，不用重填，密钥也不会显示在界面上。"
        "不要的模型，在下拉列表里右键它，选「删除」并确认；列表里只剩一个时删不了。"
        "WebUI 左上角是同一份列表，两边切都管用（删除在设置页做）。",
    ),
    (
        "设置",
        "除了模型（见上一节），设置页往下还有信使、来信提醒、实验性（日记、桌面控制）、"
        "拓展（GhostWorld、视频理解）几节。朋友发来的文件在仓库根的 inbox/ 里，"
        "按来源主机分好了文件夹。更多细节见仓库 README 与 docs/。",
    ),
]


class HelpPage(QScrollArea):
    """帮助页：Face 式分节说明（侧栏常驻入口，替代旧的帮助按钮弹窗）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("helpPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(48, 32, 48, 32)
        lay.setSpacing(10)

        lay.addWidget(TitleLabel("帮助"))
        lay.addSpacing(6)
        for heading, text in HELP_SECTIONS:
            lay.addWidget(SubtitleLabel(heading))
            label = BodyLabel(text)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lay.addWidget(label)
            lay.addSpacing(8)
        lay.addWidget(SubtitleLabel("连不上、不听话的时候"))
        hint = BodyLabel(
            "每次联网的尝试都写进了日志：程序目录下的 logs/ 里，一天一个文件，保留两周。"
            "模型通没通、房间找到没有，里面一行行记着——报问题时带上这个文件（或截几行），"
            "比描述快得多。"
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addWidget(hint)
        open_log = PushButton("打开日志目录")
        open_log.clicked.connect(runlog.open_folder)
        lay.addWidget(open_log, 0, Qt.AlignLeft)
        lay.addStretch(1)

        self.setWidget(body)
