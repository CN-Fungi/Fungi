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
        "一款局域网多主机 Agent 协作网络。一台电脑「发起房间」，同一网络里的同伴「加入房间」，"
        "从此每台电脑上都住着一位会干活、有记忆的 Agent，而且它们彼此认识。"
        "所有流量都在局域网内，无需公网。",
    ),
    (
        "怎么开始玩",
        "一个人点「发起房间」，拿到房间 IP 和 Token，发给朋友；朋友点「加入房间」，填上这两样"
        "（同一网络下 IP 都可以不填，会自动找到你）。双方各自打开 WebUI，就和自己的 Agent 对话。"
        "Token 就是一张门票：发起前可以自定义，房间开着时改了它，老朋友要用新 Token 重新进来。"
        "房主点「离开房间」，房间解散。",
    ),
    (
        "Agent 能帮你做什么",
        "查资料、写代码、整理文件这些自不必说。它认得图，读得了 docx / pptx / xlsx，"
        "还能看懂视频——「帮我把这个视频讲了什么总结一下」是句寻常的话。"
        "耗时的活它会派小分身去后台并行干，干完自动向你汇报；你也可以让它把一件事托付给"
        "朋友电脑上的 Agent 去办。涉及写文件这类有分寸的动作，它懂得先问你。"
        "边跑边答的活它也接得住：起一个交互式进程（python 的 REPL、脚手架、开发服务器），"
        "一行行喂输入、读回显。",
    ),
    (
        "桌面控制",
        "设置页「实验性」一节有个「桌面控制（screen）」开关，默认关闭。打开它，Agent 就看得见你的屏幕，"
        "也能点控件、粘贴文字、按键——打开开关即表示你同意它直接动手；截图会随工具结果发给模型，"
        "需要管理员权限的窗口它够不到。适合那些没有 API 可调的日常琐事：开个应用、"
        "在聊天窗口里发一句话、把挡路的窗口挪开。关掉开关立刻收回，仍按着的键也一并松开；"
        "离开房间同样即刻收回，不会把你的键盘留在按住的状态。",
    ),
    (
        "GhostWorld 角色控制",
        "设置页「拓展」一节有「GhostWorld 角色控制」开关，默认关闭。打开它，Agent 就能驱动本机 "
        "GhostWorld 里的一个角色：玩家在游戏里说话时它会被叫醒，然后用角色自己的嘴回话、走动、拾取东西。"
        "它还能给自己拍一张画面——角色第一人称看到的天空、地面、建筑、"
        "视野里的人——拍完直接看那张图，坐标和清单不够用时靠这个确认。"
        "前置是游戏正在本机运行，并在 config.json 里把 ghostworld_dir 指向游戏目录"
        "（源码检出，或解压出来的 exe 发行包那一层；已 pip 安装的可留空）。"
        "关掉开关立刻收回：工具从工具面移除，等玩家说话的监视进程也一并结束。"
        "游戏的内部状态它碰不到——它只会通过游戏自己的命令通道说话。",
    ),
    (
        "信使",
        "你的每一位好友主机，都有一位专职对接它的信使。开启信使（设置页，默认开），"
        "朋友找你而你不在电脑前时，它会替你接待：重要的转告你，寻常的替你答——"
        "答的依据是你在信使页留下的两样东西：长期记忆（你的习惯与交代）和四周日历"
        "（哪天几点要去哪，你点日期就能记，Agent 也会自己往里写）。"
        "要约见面、通话、一起做点什么，它会和对面信使把事件、时间、地点一件件定下来，"
        "定好的写进日历，剩下拿不准的才来问你。"
        "它每次汇报下面都留着一个评价框（写着「评价一下」）：想纠正它哪句、让它改自己写错的日程，"
        "写在那里就行——那是给自家信使的话，对面看不到。"
        "它向你提问不会把自己卡住：卡片等你有空再答，它先接着替你招待对面。"
        "朋友发来的文件永远要你点头才落盘；文字留言则不打扰任何人，离线也能收到。"
        "不想让 Agent 参与的话，关掉信使，Fungi 就成了一款简易版局域网微信。",
    ),
    (
        "skill 与日记",
        "Agent 有两种记忆。理性的是技能：干活踩过的坑、摸出的门道，它会整理成笔记沉淀下来，"
        "下次不再犯；你也可以亲自教它。感性的是日记：设置页可以开启——对话里什么触动了它，"
        "它就用自己的语气记下来。日记是它的私人领地，界面找不到入口，我们极不推荐去翻看；"
        "它被问到，也会守口如瓶。",
    ),
    (
        "手机也能用",
        "「手机端」页扫码即可：聊天、好友、收发文件一样不少。躺在床上让家里的电脑干活，"
        "是完全可行的。手机发文件会多一步——先传到电脑，再由电脑送给对方，进度条会一步步告诉你"
        "走到哪了。",
    ),
    (
        "关了窗口房间还在",
        "关闭窗口不停房：Fungi 缩进托盘继续守着——这时点托盘图标就直接进 WebUI 的好友视图，"
        "托盘菜单里另有「显示主界面」与「退出」。想真正离开，用菜单的「退出」。"
        "再次启动程序会直接唤起主界面。",
    ),
    (
        "设置",
        "设置页填模型的 API Key、接口地址和模型名；朋友发来的文件在仓库根的 inbox/ 里，"
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
            "往外连的每一次尝试都写进了运行日志：程序目录下的 logs/ 里，一天一个文件，"
            "保留两周。模型连上没有、房间找到没有、游戏通道有没有反应，里面一行行记着——"
            "报问题的时候带上这个文件（或截几行），比描述快得多。"
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addWidget(hint)
        open_log = PushButton("打开日志目录")
        open_log.clicked.connect(runlog.open_folder)
        lay.addWidget(open_log, 0, Qt.AlignLeft)
        lay.addStretch(1)

        self.setWidget(body)
