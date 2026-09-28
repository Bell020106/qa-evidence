"""Local, non-destructive contextual help, parented above the invoking dialog."""
from html import escape
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QKeySequence,QShortcut
from PySide6.QtWidgets import QApplication,QDialog,QHBoxLayout,QListWidget,QPushButton,QTextBrowser,QVBoxLayout,QScrollArea,QWidget
from signup031.failure_help_content import TOPICS


class HelpDialog(QDialog):
    def __init__(self,parent,topic='start'):
        super().__init__(parent)
        self.setWindowTitle('사용법 · 용어');self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(840,580);self.setMinimumSize(600,400)
        layout=QVBoxLayout(self);row=QHBoxLayout();layout.addLayout(row,1)
        self.topics=QListWidget();self.topics.setObjectName('helpTopics');self.topics.setWordWrap(True);self.topics.setMaximumWidth(220);self.topics.setMinimumWidth(150)
        self.body=QTextBrowser();self.body.setObjectName('helpBody');self.body.setOpenLinks(False);self.body.setOpenExternalLinks(False)
        self.body.document().setDefaultStyleSheet('body { font-family: "Malgun Gothic"; font-size: 11pt; } h1 {font-size:18pt;color:#174eb6} h2 {font-size:12pt;margin-top:16px} li {margin-bottom:8px} p {line-height:1.5}')
        row.addWidget(self.topics);row.addWidget(self.body,1)
        first=['start','terms','troubleshooting']
        self.ids=first+[key for key in TOPICS if key not in first]
        for key in self.ids:self.topics.addItem(TOPICS[key][0])
        self.topics.currentRowChanged.connect(self._show_topic)
        self.copy_setup=QPushButton('내 폴더로 연결 안내 만들기');self.copy_setup.clicked.connect(self.open_setup_guide);layout.addWidget(self.copy_setup)
        self.setup_guide=None
        close=QPushButton('닫고 돌아가기');close.clicked.connect(self.close);layout.addWidget(close)
        self.select_topic(topic)

    def select_topic(self,topic):self.topics.setCurrentRow(self.ids.index(topic))

    def open_setup_guide(self):
        from signup031.setup_help import SetupGuideDialog
        if self.setup_guide is None:self.setup_guide=SetupGuideDialog(self)
        self.setup_guide.show();self.setup_guide.raise_();self.setup_guide.activateWindow()

    def _show_topic(self,index):
        if index<0:return
        self.current_topic=self.ids[index]
        title,purpose,prepare,steps,result,next_step,limits=TOPICS[self.current_topic]
        content='<h1>'+escape(title)+'</h1><p>'+escape(purpose)+'</p>'
        content+='<h2>준비할 것</h2><p>'+escape(prepare)+'</p><h2>사용 순서</h2><ol>'
        content+=''.join('<li>'+escape(step)+'</li>' for step in steps)+'</ol>'
        for heading,text in [('결과 읽기',result),('다음 행동',next_step),('현재 제한',limits)]:content+='<h2>'+heading+'</h2><p>'+escape(text)+'</p>'
        self.copy_setup.setVisible(self.current_topic=='selenium')
        if self.current_topic=='selenium':content+='<h2>내 경로에 맞는 파일별 복사</h2><p>아래 버튼에서 수집 도구 소스 폴더와 사용자 테스트 프로젝트를 따로 선택하세요. 설치 명령, pytest.ini, conftest.py, 연결 확인 테스트를 파일별로 복사할 수 있습니다.</p>'
        self.body.setHtml(content);self.body.verticalScrollBar().setValue(0)


def open_help(owner,topic=None):
    topic=topic or owner.help_topic
    if getattr(owner,'help_dialog',None) is None:owner.help_dialog=HelpDialog(owner,topic)
    else:owner.help_dialog.select_topic(topic)
    owner.help_dialog.show();owner.help_dialog.raise_();owner.help_dialog.activateWindow()


def add_help(owner,topic,layout=None,*,caption='이 기능 사용법'):
    owner.help_topic=topic
    if getattr(owner,'help_button',None) is not None:return owner.help_button
    owner.help_dialog=None
    owner.help_button=QPushButton(caption,owner);owner.help_button.setObjectName('contextHelpButton')
    owner.help_button.clicked.connect(lambda:open_help(owner))
    owner.help_shortcut=QShortcut(QKeySequence('F1'),owner);owner.help_shortcut.setContext(Qt.ShortcutContext.WindowShortcut);owner.help_shortcut.activated.connect(lambda:open_help(owner))
    if layout is not None:
        row=QHBoxLayout();row.addStretch();row.addWidget(owner.help_button);layout.insertLayout(0,row)
        # Finish after subclass construction (Sheets extends the CSV form).
        # Keep help fixed above a scrollable existing form, without moving fields.
        QTimer.singleShot(0,owner,lambda:_scrollable_form(owner,layout))
    return owner.help_button


def _scrollable_form(owner,layout):
    if getattr(owner,'help_scroll',None) is not None:return
    help_row=layout.takeAt(0).layout()
    content=QWidget();content.setLayout(layout)
    outer=QVBoxLayout(owner);outer.addLayout(help_row)
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QScrollArea.Shape.NoFrame);scroll.setWidget(content)
    outer.addWidget(scroll,1);owner.help_scroll=scroll
    owner.setMinimumSize(400,300)
    available=owner.screen().availableGeometry()
    owner.resize(min(owner.width(),available.width()-60),min(owner.height(),available.height()-80))
