import argparse
import json
import sqlite3
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QProcess, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from signup031.viewer_model import EvidenceRecord, load_evidence_root


STATUS_COLORS = {
    'running': ('#E8EBF1', '#536173'),
    'execution_error': ('#FFF0D6', '#805006'),
    'cancelled': ('#E8EBF1', '#536173'),
    'interrupted': ('#FFF0D6', '#805006'),
    'skipped': ('#E8EBF1', '#536173'),
    'unjudged': ('#E8EBF1', '#536173'),
    "passed": ("#DDF5E8", "#176B47"),
    "failed": ("#FDE5E8", "#A92F42"),
    "preparation_failed": ("#FFF0D6", "#805006"),
    "read_error": ("#FDE5E8", "#A92F42"),
    "incomplete": ("#FFF0D6", "#805006"),
    "legacy": ("#E8EBF1", "#536173"),
}

_FONT_CONFIGURED = False


def _ensure_korean_font() -> None:
    global _FONT_CONFIGURED
    if _FONT_CONFIGURED:
        return
    application = QApplication.instance()
    if application is None:
        return
    preferred_families = (
        "Malgun Gothic",
        "Noto Sans CJK KR",
        "Apple SD Gothic Neo",
    )
    families = set(QFontDatabase.families())
    chosen = next((family for family in preferred_families if family in families), None)
    if chosen is None:
        for font_path in (
            Path("C:/Windows/Fonts/malgun.ttf"),
            Path("C:/Windows/Fonts/malgunbd.ttf"),
        ):
            if not font_path.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id >= 0:
                loaded = QFontDatabase.applicationFontFamilies(font_id)
                if loaded and chosen is None:
                    chosen = loaded[0]
    if chosen:
        font = QFont(chosen, 10)
        if QFontMetrics(font).inFontUcs4(ord("실")):
            application.setFont(font)
    _FONT_CONFIGURED = True


def _display_number(value: int | None) -> str:
    return "미측정" if value is None else str(value)


def _field(title: str, object_name: str) -> tuple[QFrame, QLabel]:
    frame = QFrame()
    frame.setObjectName("metricCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 13, 16, 13)
    caption = QLabel(title)
    caption.setObjectName("metricCaption")
    value = QLabel("—")
    value.setObjectName(object_name)
    layout.addWidget(caption)
    layout.addWidget(value)
    return frame, value


class EvidenceViewerWindow(QMainWindow):
    def __init__(self, root, *, replay_headless=False):
        _ensure_korean_font()
        super().__init__()
        self.root = Path(root).resolve()
        self.records: list[EvidenceRecord] = []
        self._source_pixmap: QPixmap | None = None
        self.replay_process = None
        self.replay_buffer = b''
        self.replay_failure = None
        self.replay_stderr = b''
        self.replay_discard_line = False
        self.replay_limitations = []
        self.local_session=None
        self.local_source=None
        self.local_record=None
        self.local_mode=False
        self.selected_archive = None
        self.replay_headless = replay_headless
        self.scenario_editor = None
        self.manual_dialog = None
        self.investigation_dialog = None
        self.server_import_dialog = None
        self.tc_catalog_dialog = None
        self.suite_dialog = None
        self.timeline_dialog = None
        self.ai_dialog = None
        self.evaluation_dialog = None
        self.android_dialog = None
        self.browser_dialog = None
        self.setWindowTitle("QA Evidence · Selenium 실패 조사")
        self.setMinimumSize(900, 620)
        self.resize(1180, 760)
        self._build_ui()
        self._apply_style()
        self.load_root(self.root)
        self.watch_timer=QTimer(self);self.watch_timer.setInterval(1200);self.watch_timer.timeout.connect(self.poll_failures);self.watch_timer.start()

    def _build_ui(self) -> None:
        central = QWidget()
        page = QVBoxLayout(central)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 15, 22, 15)
        brand = QLabel("QA Evidence · 실패 조사")
        brand.setObjectName("brand")
        header_layout.addWidget(brand)
        header_layout.addStretch(1)
        self.folder_label = QLabel()
        self.folder_label.setObjectName("folderLabel")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_layout.addWidget(self.folder_label, 1)
        self.folder_label.setMinimumWidth(0)
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        from signup031.help_dialog import add_help
        header_layout.addWidget(add_help(self,'start',caption='사용법 · 용어'))
        actions_layout = QHBoxLayout()
        actions_layout.setContentsMargins(12,4,12,4)
        self.configure_button = QPushButton('자동 테스트 만들기')
        self.configure_button.setObjectName('configureScenarioButton')
        self.configure_button.clicked.connect(self.open_scenario_editor)
        self.manual_button = QPushButton('직접 테스트·기록')
        self.manual_button.setObjectName('manualRecordingButton')
        self.manual_button.clicked.connect(self.open_manual_recorder)
        self.server_import_button = QPushButton('서버 결과 가져오기')
        self.server_import_button.setObjectName('serverImportButton')
        self.server_import_button.clicked.connect(self.open_server_import)
        choose_button = QPushButton("실패 수집 폴더 선택")
        choose_button.setObjectName("chooseFolderButton")
        choose_button.clicked.connect(self._choose_root)
        refresh_button = QPushButton("새로고침")
        refresh_button.setObjectName("refreshButton")
        refresh_button.clicked.connect(lambda: self.load_root(self.root))
        actions_layout.addWidget(choose_button)
        actions_layout.addWidget(self.server_import_button)
        actions_layout.addWidget(refresh_button)
        page.addWidget(header)
        page.addLayout(actions_layout)
        self.ide_button=QPushButton('IDE 연결 방법');self.ide_button.clicked.connect(lambda:__import__('signup031.help_dialog',fromlist=['open_help']).open_help(self,'selenium'))
        actions_layout.addWidget(self.ide_button)
        self.new_failure_label=QLabel('완성된 새 실패를 자동으로 확인합니다.');self.new_failure_label.setWordWrap(True);page.addWidget(self.new_failure_label)

        self.other_tools_button=QPushButton('다른 도구 펼치기');self.other_tools_button.setCheckable(True)
        actions_layout.addWidget(self.other_tools_button)
        self.other_tools=QWidget();tc_bar = QGridLayout(self.other_tools)
        self.other_tools_button.hide();self.server_import_button.hide()
        self.other_tools_button.toggled.connect(lambda opened:self.other_tools_button.setText('다른 도구 접기' if opened else '다른 도구 펼치기'))
        tc_bar.addWidget(self.configure_button,2,0);tc_bar.addWidget(self.manual_button,2,1)
        tc_bar.setContentsMargins(12,0,12,6)
        self.tc_catalog_button = QPushButton('테스트 목록·CSV 가져오기')
        self.tc_catalog_button.clicked.connect(self.open_tc_catalog)
        tc_bar.addWidget(self.tc_catalog_button,0,0)
        self.suite_button = QPushButton('여러 테스트 실행·비교')
        self.suite_button.clicked.connect(self.open_suite)
        tc_bar.addWidget(self.suite_button,0,1)
        self.ai_button=QPushButton('AI로 테스트 초안 만들기');self.ai_button.clicked.connect(self.open_ai_assistant);tc_bar.addWidget(self.ai_button,1,0)
        self.evaluation_button=QPushButton('AI 답변 평가');self.evaluation_button.clicked.connect(self.open_evaluation);tc_bar.addWidget(self.evaluation_button,1,1)
        self.android_button=QPushButton('Android 앱 테스트');self.android_button.clicked.connect(self.open_android);tc_bar.addWidget(self.android_button,1,2)
        self.browser_button=QPushButton('웹 브라우저 준비');self.browser_button.clicked.connect(self.open_browser_setup);actions_layout.insertWidget(3,self.browser_button)
        page.addWidget(self.other_tools);self.other_tools.hide()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        list_panel = QFrame()
        list_panel.setObjectName("listPanel")
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(18, 20, 18, 20)
        list_heading = QHBoxLayout()
        heading = QLabel("실패 목록")
        heading.setObjectName("sectionHeading")
        self.count_label = QLabel("0개")
        self.count_label.setObjectName("countLabel")
        list_heading.addWidget(heading)
        list_heading.addStretch(1)
        list_heading.addWidget(self.count_label)
        list_layout.addLayout(list_heading)
        self.run_list = QListWidget()
        self.run_list.setObjectName("runList")
        self.run_list.setSpacing(5)
        self.run_list.currentItemChanged.connect(self._show_current_item)
        list_layout.addWidget(self.run_list)
        splitter.addWidget(list_panel)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail = QWidget()
        detail.setObjectName("detailPanel")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(28, 24, 28, 28)
        detail_layout.setSpacing(14)
        self.start_guide=QFrame();self.start_guide.setObjectName('startGuide')
        guide_layout=QVBoxLayout(self.start_guide)
        guide_title=QLabel('처음 시작하나요?');guide_title.setObjectName('sectionHeading');guide_layout.addWidget(guide_title)
        guide_text=QLabel('① IDE 연결 방법에서 pytest 수집 설정을 연결합니다.\n② IDE에서 평소처럼 Selenium 테스트를 실행합니다.\n③ 실패 수집 폴더를 선택하면 완성된 새 실패만 자동으로 나타납니다.\n④ 로컬에서 확인하고 조사 메모를 저장한 뒤 AI 원인 분석으로 이어갑니다.\n\n앱은 코드를 작성하거나 테스트를 실행하지 않습니다.\nPASS·SKIP·일반 XFAIL은 저장하지 않습니다.\n수집 전에 실패했거나 화면 자료가 없으면 오류 내용만 확인할 수 있습니다.')
        guide_text.setWordWrap(True);guide_layout.addWidget(guide_text);detail_layout.addWidget(self.start_guide)

        top_line = QHBoxLayout()
        self.status_badge = QLabel("—")
        self.status_badge.setObjectName("statusBadge")
        self.tc_label = QLabel("TC")
        self.tc_label.setObjectName("tcLabel")
        self.tc_label.setMinimumWidth(0);self.tc_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        top_line.addWidget(self.status_badge)
        top_line.addWidget(self.tc_label)
        top_line.addStretch(1)
        detail_layout.addLayout(top_line)
        self.detail_title = QLabel("실행 상세")
        self.detail_title.setObjectName("detailTitle")
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        self.failure_summary=QLabel();self.failure_summary.setWordWrap(True);detail_layout.addWidget(self.failure_summary)
        self.execution_label = QLabel("—")
        self.execution_label.setObjectName("executionValue")
        self.execution_label.setWordWrap(True)
        self.execution_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.execution_label)
        self.source_label = QLabel()
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        detail_layout.addWidget(self.source_label)
        source_row = QGridLayout()
        self.draft_button = QPushButton('자동 테스트 초안 만들기')
        self.draft_button.setObjectName('manualToTCButton')
        self.draft_button.clicked.connect(self.create_tc_draft)
        self.source_button = QPushButton('처음 기록 보기')
        self.source_button.clicked.connect(self.open_manual_source)
        source_row.addWidget(self.draft_button,0,0)
        source_row.addWidget(self.source_button,0,1)
        self.investigation_button = QPushButton('조사 메모')
        self.investigation_button.clicked.connect(self.open_investigation)
        self.analysis_button=QPushButton('AI로 실패 원인 분석');self.analysis_button.clicked.connect(self.open_failure_analysis)
        source_row.addWidget(self.investigation_button,1,0)
        source_row.addWidget(self.analysis_button,1,1)
        detail_layout.addLayout(source_row)
        self.details_button=QPushButton('실패 상세·환경 펼치기');self.details_button.setCheckable(True);detail_layout.addWidget(self.details_button)
        self.details_panel=QWidget();details_layout=QVBoxLayout(self.details_panel);detail_layout.addWidget(self.details_panel);self.details_panel.hide()
        self.details_button.toggled.connect(self.details_panel.setVisible)
        self.details_button.toggled.connect(lambda opened:self.details_button.setText('실패 상세·환경 접기' if opened else '실패 상세·환경 펼치기'))

        metrics = QHBoxLayout()
        expected_card, self.expected_value = _field("기대 최대 길이", "expectedValue")
        initial_card, self.initial_value = _field("최초 입력", "initialValue")
        actual_card, self.actual_value = _field("한 글자 추가 후", "actualValue")
        metrics.addWidget(expected_card)
        metrics.addWidget(initial_card)
        metrics.addWidget(actual_card)
        self.metric_captions = [card.findChild(QLabel, 'metricCaption') for card in (expected_card, initial_card, actual_card)]
        details_layout.addLayout(metrics)

        self.checks_label = QLabel()
        self.checks_label.setObjectName('checksDetail')
        self.checks_label.setTextFormat(Qt.TextFormat.PlainText)
        self.checks_label.setWordWrap(True)
        self.checks_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.checks_label.hide()
        details_layout.addWidget(self.checks_label)
        self.android_log_button=QPushButton('Android 앱 로그 보기');self.android_log_button.clicked.connect(self.open_android_log);self.android_log_button.hide();detail_layout.addWidget(self.android_log_button)

        metadata = QFrame()
        metadata.setObjectName("metadataCard")
        metadata_layout = QGridLayout(metadata)
        metadata_layout.setContentsMargins(16, 14, 16, 14)
        metadata_layout.setHorizontalSpacing(18)
        metadata_layout.setVerticalSpacing(9)
        self.pytest_value = QLabel("—")
        self.pytest_value.setObjectName("pytestValue")
        self.phase_value = QLabel("—")
        self.phase_value.setObjectName("phaseValue")
        self.target_value = QLabel("—")
        self.target_value.setObjectName("targetValue")
        self.cleanup_value = QLabel("—")
        self.cleanup_value.setObjectName("cleanupValue")
        rows = (
            ("pytest 원본", self.pytest_value),
            ("업무 단계", self.phase_value),
            ("대상", self.target_value),
            ("후처리", self.cleanup_value),
        )
        for row, (caption, value) in enumerate(rows):
            caption_label = QLabel(caption)
            caption_label.setObjectName("metaCaption")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True);value.setMinimumWidth(0)
            metadata_layout.addWidget(caption_label, row, 0)
            metadata_layout.addWidget(value, row, 1)
            if row == 0:
                self.runner_caption = caption_label
        metadata_layout.setColumnStretch(1, 1)
        details_layout.addWidget(metadata)

        message_heading = QLabel("결과 메시지")
        message_heading.setObjectName("sectionHeading")
        details_layout.addWidget(message_heading)
        self.message_details_button=QPushButton('원본 결과 메시지 펼치기');self.message_details_button.setCheckable(True);details_layout.addWidget(self.message_details_button)
        self.message_label = QLabel("표시할 실행 결과가 없습니다")
        self.message_label.setObjectName("messageLabel")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_layout.addWidget(self.message_label)
        self.message_label.hide();self.message_details_button.toggled.connect(self.message_label.setVisible)

        replay_row = QGridLayout()
        self.replay_button = QPushButton('저장 자료로 재현')
        self.replay_button.setObjectName('replayButton')
        self.replay_button.clicked.connect(lambda: self.start_replay(headless=self.replay_headless))
        self.local_investigation_button=QPushButton('로컬에서 확인')
        self.local_investigation_button.clicked.connect(lambda:self.start_replay(headless=self.replay_headless,investigate=True))
        self.stop_replay_button = QPushButton('재현 종료')
        self.stop_replay_button.setObjectName('stopReplayButton')
        self.stop_replay_button.setEnabled(False)
        self.stop_replay_button.clicked.connect(self.stop_replay)
        self.replay_label = QLabel('재현 자료 없음 · 기록을 켜고 다시 실행')
        self.replay_label.setObjectName('replayStatus')
        self.replay_label.setTextFormat(Qt.TextFormat.PlainText)
        self.replay_label.setWordWrap(True)
        replay_row.addWidget(self.replay_button,0,0)
        replay_row.addWidget(self.stop_replay_button,0,1)
        self.timeline_button = QPushButton('동작 기록·오류 실험')
        self.timeline_button.clicked.connect(self.open_timeline)
        replay_row.addWidget(self.timeline_button,1,0)
        replay_help=QPushButton('이 기능 사용법')
        from signup031.help_dialog import open_help
        replay_help.clicked.connect(lambda:open_help(self,'replay'))
        replay_row.addWidget(replay_help,1,1)
        source_row.addWidget(self.local_investigation_button,0,0,1,2)
        replay_row.addWidget(self.draft_button,2,0);replay_row.addWidget(self.source_button,2,1)
        detail_layout.addLayout(replay_row)
        detail_layout.addWidget(self.replay_label)

        screenshot_heading = QLabel("스크린샷")
        screenshot_heading.setObjectName("sectionHeading")
        detail_layout.addWidget(screenshot_heading)
        self.screenshot_label = QLabel("스크린샷 없음")
        self.screenshot_label.setObjectName("screenshotLabel")
        self.screenshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screenshot_label.setMinimumHeight(270)
        self.screenshot_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        detail_layout.addWidget(self.screenshot_label, 1)
        detail_scroll.setWidget(detail)
        splitter.addWidget(detail_scroll)
        splitter.setSizes([340, 840])
        splitter.setStretchFactor(1, 1)
        page.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.restrict_failure_ui()

    def restrict_failure_ui(self):
        for widget in (self.other_tools_button,self.other_tools,self.server_import_button,self.draft_button,self.source_button,
                       self.replay_button,self.timeline_button,self.android_log_button):widget.hide()

    def poll_failures(self):
        try:
            signature=tuple(sorted((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in self.root.rglob('evidence.json')
                if not any(part.startswith('.qa-capture-') for part in p.relative_to(self.root).parts)))
            if signature!=getattr(self,'_watch_signature',None):
                self._watch_signature=signature;self.load_root(self.root,automatic=True)
        except OSError:self.new_failure_label.setText('실패 폴더 확인 중 오류 · 경로와 접근 권한을 확인하세요.')

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, #detailPanel { background: #F5F7FA; color: #202733; }
            #header { background: #FFFFFF; border-bottom: 1px solid #DDE3EC; }
            #brand { font-size: 17px; font-weight: 700; color: #244B9A; }
            #folderLabel, #countLabel { color: #687588; }
            QPushButton { background: #FFFFFF; border: 1px solid #C9D2DF; border-radius: 6px; padding: 7px 11px; }
            QPushButton:hover { border-color: #315CB9; color: #315CB9; }
            #listPanel { background: #FFFFFF; border-right: 1px solid #DDE3EC; }
            QListWidget { border: 0; background: transparent; outline: 0; }
            QListWidget::item { border: 1px solid #DDE3EC; border-radius: 8px; padding: 12px; margin: 2px; }
            QListWidget::item:selected { background: #EAF1FF; border-color: #315CB9; color: #18396F; }
            #sectionHeading { font-size: 14px; font-weight: 700; color: #334055; }
            #detailTitle { font-size: 24px; font-weight: 700; }
            #executionValue { color: #687588; }
            #statusBadge, #tcLabel { border-radius: 10px; padding: 4px 10px; font-weight: 700; }
            #tcLabel { background: #E8EDF5; color: #536173; }
            #metricCard, #metadataCard { background: #FFFFFF; border: 1px solid #DDE3EC; border-radius: 8px; }
            #metricCaption, #metaCaption { color: #687588; }
            #expectedValue, #initialValue, #actualValue { font-size: 24px; font-weight: 700; }
            #messageLabel { background: #FFFFFF; border: 1px solid #DDE3EC; border-radius: 8px; padding: 13px; }
            #screenshotLabel { background: #FFFFFF; border: 1px solid #DDE3EC; border-radius: 8px; color: #687588; }
            """
        )

    def load_root(self, root, *, automatic=False):
        # Keep records alive while individual Qt item data is replaced.
        previous_records=self.records
        same=Path(root).resolve()==self.root
        selected_record=self.run_list.currentItem().data(Qt.ItemDataRole.UserRole) if same and self.run_list.currentItem() else None
        selected=selected_record.source_path if selected_record is not None else None
        preserved_replay_label=self.replay_label.text() if automatic and selected is not None else None
        previous={record.source_path for record in self.records} if same else set()
        self.root = Path(root).resolve()
        self.folder_label.setText(str(self.root))
        self.folder_label.setToolTip(str(self.root))
        path_error=None
        if getattr(sys,'frozen',False):
            from signup031.desktop_runtime import check_data_path
            try:check_data_path(self.root)
            except ValueError as exc:path_error=str(exc)
        for button in (self.android_button,self.evaluation_button,self.ai_button if hasattr(self,'ai_button') else self.android_button,
                       self.findChild(QPushButton,'configureScenarioButton'),self.findChild(QPushButton,'manualRecordingButton')):
            button.setEnabled(path_error is None)
        if path_error:
            self.records=[]
            while self.run_list.count():self.run_list.takeItem(0)
            self.count_label.setText('0개');self._clear_detail(path_error);return
        loaded=load_evidence_root(self.root)
        failures={'failed','preparation_failed','execution_error'}
        invalid=[record for record in loaded if record.business_status in ('incomplete','read_error','legacy')]
        self.records = [record for record in loaded if record.business_status in failures]
        added=len({record.source_path for record in self.records}-previous)
        if automatic and added:self.new_failure_label.setText(f'새 실패 {added}건을 반영했습니다 · 선택한 기록과 작성 중 메모는 유지됩니다.')
        elif not automatic:self.new_failure_label.setText('완성된 새 실패를 자동으로 확인합니다. PASS·SKIP은 표시하지 않습니다.')
        if invalid:self.new_failure_label.setText(self.new_failure_label.text()+f' · 손상/미지원 자료 {len(invalid)}건은 실패 목록에서 제외했습니다.')
        self.run_list.blockSignals(True)
        existing={self.run_list.item(i).data(Qt.ItemDataRole.UserRole).source_path:self.run_list.item(i) for i in range(self.run_list.count())}
        wanted={record.source_path for record in self.records};removed=[]
        for path,item in existing.items():
            if path not in wanted:removed.append(self.run_list.takeItem(self.run_list.row(item)))
        if selected is None:self._clear_detail(
            "표시할 실행 결과가 없습니다" if not self.records else "실행을 선택하세요"
        )
        for record in self.records:
            started = record.started_at or "시간 정보 없음"
            tc = record.tc_id or "형식 확인 필요"
            item=existing.get(record.source_path)
            if item is None:item=QListWidgetItem();self.run_list.addItem(item)
            item.setText(f"{record.status_label}  ·  {tc}\n{record.execution_id}\n{started}  ·  {record.target_label}")
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setForeground(QColor(STATUS_COLORS[record.business_status][1]))
            item.setToolTip(str(record.source_path))
        self.count_label.setText(f"{len(self.records)}개")
        selected_item=existing.get(selected) if selected in wanted else None
        if selected_item is not None:self.run_list.setCurrentItem(selected_item)
        elif self.records:self.run_list.setCurrentRow(0)
        self.run_list.blockSignals(False)
        if self.records:self._show_current_item(self.run_list.currentItem(),None)
        else:self._clear_detail('표시할 실행 결과가 없습니다')
        if (automatic and selected_item is not None and self.run_list.currentItem() is selected_item
                and selected_record == selected_item.data(Qt.ItemDataRole.UserRole)):
            self.replay_label.setText(preserved_replay_label)
        self.restrict_failure_ui()
        del previous_records

    def _choose_root(self) -> None:
        if self.investigation_dialog is not None and self.investigation_dialog.isVisible():
            self.new_failure_label.setText('작성 중인 조사를 먼저 저장하고 닫은 뒤 다른 폴더를 선택하세요.');return
        selected = QFileDialog.getExistingDirectory(
            self,
            "evidence.json 결과 폴더 선택",
            str(self.root),
        )
        if selected:
            self.load_root(Path(selected))

    def open_tc_catalog(self):
        from signup031.tc_catalog_dialog import TCCatalogDialog
        try:
            self.tc_catalog_dialog = TCCatalogDialog(self)
            self.tc_catalog_dialog.show()
        except (OSError,ValueError,sqlite3.Error) as exc:
            self.source_label.setText('TC 목록 열기 실패 · '+str(exc))

    def open_server_import(self):
        from signup031.ingestion_dialog import ServerImportDialog
        if self.server_import_dialog is None or not self.server_import_dialog.isVisible():
            self.server_import_dialog = ServerImportDialog(self)
        self.server_import_dialog.show()
        self.server_import_dialog.raise_()

    def _show_current_item(self, current, _previous) -> None:
        if current is None:
            self._clear_detail("표시할 실행 결과가 없습니다")
            return
        self.show_record(current.data(Qt.ItemDataRole.UserRole))

    def show_record(self, record: EvidenceRecord) -> None:
        self.start_guide.hide()
        pinned_local_investigation = (
            self.replay_process is not None and self.local_mode and self.local_record is not None
            and record.execution_id == self.local_record.execution_id
        )
        self.investigation_button.setEnabled(
            (self.replay_process is None or pinned_local_investigation)
            and record.business_status in ('passed', 'failed', 'preparation_failed', 'unjudged','execution_error','cancelled','interrupted')
        )
        self.analysis_button.setEnabled(self.replay_process is None and self.investigation_button.isEnabled())
        self.draft_button.setEnabled(record.manual_record is not None and record.archive_root is not None and self.replay_process is None)
        self.source_button.setEnabled(False)
        self.source_label.clear()
        self.manual_source = None
        source = (record.scenario_snapshot or {}).get('source_manual')
        if source:
            from signup031.manual_to_tc import resolve_manual_source
            self.manual_source, message = resolve_manual_source(self.records, source)
            self.source_label.setText('출처 ID · ' + source['execution_id'] + '\n' + message +
                ('\n원본 제한: ' + '; '.join(source['limitations']) if source['limitations'] else ''))
            self.source_button.setEnabled(self.manual_source is not None and self.replay_process is None)
        elif record.manual_record is not None:
            from signup031.manual_to_tc import resolve_manual_source
            children = []
            for child in self.records:
                reference = (child.scenario_snapshot or {}).get('source_manual')
                if reference and reference['execution_id'] == record.execution_id:
                    verified, _ = resolve_manual_source(self.records, reference)
                    children.append(f'{child.tc_id} · {child.execution_id}' + (' · 출처 검증 실패' if verified is None else ''))
            self.source_label.setText('후속 자동 실행: ' + ('\n'.join(children) if children else '없음'))
        if record.remote_details:
            self.source_label.setText((self.source_label.text() + '\n' if self.source_label.text() else '') + record.remote_details)
        self.detail_title.setText(record.title)
        self.selected_archive = record.archive_root
        self.replay_button.setEnabled(record.archive_root is not None and self.replay_process is None)
        self.local_investigation_button.setEnabled(record.archive_root is not None and self.replay_process is None)
        if self.replay_process is None:
            self.replay_label.setText(record.archive_message)
        background, foreground = STATUS_COLORS[record.business_status]
        self.status_badge.setText(record.status_label)
        self.status_badge.setStyleSheet(
            f"background: {background}; color: {foreground}; border-radius: 10px; padding: 4px 10px; font-weight: 700;"
        )
        self.tc_label.setText(record.tc_id or "형식 확인 필요")
        self.execution_label.setText(
            f"실행 ID  {record.execution_id}"
            + (f"   ·   {record.started_at}" if record.started_at else "")
        )
        self.expected_value.setText(
            "—" if record.expected_maximum is None else str(record.expected_maximum)
        )
        self.initial_value.setText(_display_number(record.initial_length))
        self.actual_value.setText(_display_number(record.after_extra_length))
        generic = record.scenario_snapshot is not None
        self.android_log_button.setVisible(record.android_scenario is not None)
        self.android_log_button.setEnabled(record.android_log is not None)
        captions = ('검증 항목', '통과 항목', '실패 항목') if generic else ('기대 최대 길이', '최초 입력', '한 글자 추가 후')
        for label, text in zip(self.metric_captions, captions):
            label.setText(text)
        self.checks_label.setVisible(generic)
        self.runner_caption.setText('실행 엔진' if generic else 'pytest 원본')
        if generic:
            from signup031.web_scenario import CHECK_LABELS
            self.expected_value.setText(str(len(record.scenario_snapshot['checks'])))
            self.initial_value.setText(str(sum(c['passed'] for c in record.checks)) if record.checks else '미측정')
            self.actual_value.setText(str(sum(not c['passed'] for c in record.checks)) if record.checks else '미측정')
            rows = record.checks or record.scenario_snapshot['checks']
            self.checks_label.setText('\n'.join(
                f'{i}. {CHECK_LABELS[c["kind"]]} · {c["target"]}\n'
                f'기대: {c["expected"]}  |  관측: {c.get("actual", "미측정")}'
                for i, c in enumerate(rows, 1)))
        self.pytest_value.setText(
            f"{record.pytest_status} / {record.pytest_phase}"
            if record.pytest_status and record.pytest_phase
            else "—"
        )
        if generic:
            self.pytest_value.setText('웹 시나리오 엔진 · pytest 미실행')
        if record.android_scenario is not None:
            for label,text in zip(self.metric_captions,('검증 항목','통과 항목','실패 항목')): label.setText(text)
            self.expected_value.setText(str(len(record.checks)))
            self.initial_value.setText(str(sum(c['status']=='passed' for c in record.checks)))
            self.actual_value.setText(str(sum(c['status']=='failed' for c in record.checks)))
            self.runner_caption.setText('실행 엔진');self.pytest_value.setText('Android ADB · 선택 기기/앱')
            self.checks_label.setVisible(True)
            self.checks_label.setText('\n'.join(f'{i}. {c["kind"]} · {c["target"]} · {c["status"]}\n기대: {c["expected"]!r} | 관측: {c["actual"]!r}' for i,c in enumerate(record.checks,1))+'\n\n'+record.android_details)
        if record.manual_record is not None:
            manual = record.manual_record
            for label, text in zip(self.metric_captions, ('기록 동작', '관측 입력', '기록 제한')):
                label.setText(text)
            self.expected_value.setText(str(len(manual['actions'])))
            self.initial_value.setText(str(len(manual['observed']['inputs'])))
            self.actual_value.setText(str(len(manual['limitations'])))
            self.runner_caption.setText('기록 방식')
            self.pytest_value.setText('수동 기록 · 자동 테스트 미실행')
            self.checks_label.setVisible(True)
            self.checks_label.setText('복원 대조 범위: 저장 URL · 기록 대상 입력값 · 본문 표시 텍스트\n전체 JS 메모리·전체 상태의 동일성을 보장하지 않습니다.')
        self.phase_value.setText(record.business_phase or "—")
        if record.selenium_record is not None:
            captured = record.selenium_record
            for label, text in zip(self.metric_captions, ('Selenium 동작', '관측 입력', '수집 제한')):
                label.setText(text)
            self.expected_value.setText(str(len(captured['actions'])))
            self.initial_value.setText(str(len(captured['observed']['inputs'])))
            self.actual_value.setText(str(len(captured['limitations'])))
            self.runner_caption.setText('원본 pytest 결과')
            self.checks_label.setVisible(True)
            self.checks_label.setText(record.selenium_details + '\n실제 Selenium 세션의 DOM 이벤트·CDP 응답 수집\n복원 대조: 수집 시점 URL·입력값·본문 텍스트. 전체 JS 메모리/사이트 복제를 보장하지 않습니다.')
        target_text = record.target_label
        if record.target_url:
            target_text += f"  ·  {record.target_url}"
        self.target_value.setText(target_text)
        cleanup_text = record.cleanup_status or "—"
        if record.selenium_record is not None and record.cleanup_status == 'recorder_buffers_cleared':
            cleanup_text = '수집기 메모리 정리 완료 · driver 종료는 호출자 책임/미확인'
        if record.cleanup_errors:
            cleanup_text += "  ·  " + "; ".join(record.cleanup_errors)
        self.cleanup_value.setText(cleanup_text)
        self.message_label.setText(record.message)
        from signup031.failure_presentation import failure_summary
        self.failure_summary.setText(failure_summary(record.message) if record.business_status!='passed' else '원본 자동화 결과: 통과')
        self._show_screenshot(record)
        self.restrict_failure_ui()

    def _show_screenshot(self, record: EvidenceRecord) -> None:
        self._source_pixmap = None
        self.screenshot_label.clear()
        screenshot = record.screenshot
        if screenshot.status == "available" and screenshot.path is not None:
            pixmap = QPixmap(str(screenshot.path))
            if not pixmap.isNull():
                self._source_pixmap = pixmap
                self._scale_screenshot()
                self.screenshot_label.setToolTip(str(screenshot.path))
                return
            text = "스크린샷 이미지 읽기 실패"
        else:
            text = {
                "not_collected": "스크린샷 미수집",
                "collection_failed": "스크린샷 수집 실패",
                "missing": "스크린샷 파일 없음",
                "outside_root": "선택 폴더 밖 스크린샷 — 표시하지 않음",
                "remote_not_allowed": "원격 스크린샷 — 자동 요청하지 않음",
                "not_available": "스크린샷 정보 없음",
            }.get(screenshot.status, f"스크린샷 상태: {screenshot.status}")
        if screenshot.reason:
            text += f"\n{screenshot.reason}"
        self.screenshot_label.setText(text)
        self.screenshot_label.setToolTip("")

    def _scale_screenshot(self) -> None:
        if self._source_pixmap is None:
            return
        available = self.screenshot_label.size()
        self.screenshot_label.setPixmap(
            self._source_pixmap.scaled(
                max(1, available.width() - 18),
                max(1, available.height() - 18),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _clear_detail(self, message: str) -> None:
        self.analysis_button.setEnabled(False);self.failure_summary.clear()
        self.start_guide.setVisible(not self.records)
        self.investigation_button.setEnabled(False)
        self.draft_button.setEnabled(False)
        self.source_button.setEnabled(False)
        self.source_label.clear()
        self.selected_archive = None
        self.replay_button.setEnabled(False)
        self.local_investigation_button.setEnabled(False)
        if self.replay_process is None:
            self.replay_label.setText('재현 자료 없음 · 기록을 켜고 다시 실행')
        self._source_pixmap = None
        self.status_badge.setText("—")
        self.status_badge.setStyleSheet("")
        self.tc_label.setText("TC")
        self.detail_title.setText('실행 상세')
        self.checks_label.hide()
        for label, text in zip(self.metric_captions, ('검증 항목', '통과 항목', '실패 항목')):
            label.setText(text)
        self.execution_label.setText("—")
        for label in (
            self.expected_value,
            self.initial_value,
            self.actual_value,
            self.pytest_value,
            self.phase_value,
            self.target_value,
            self.cleanup_value,
        ):
            label.setText("—")
        self.message_label.setText(message)
        self.screenshot_label.clear()
        self.screenshot_label.setText("스크린샷 없음")
        self.screenshot_label.setToolTip("")

    def select_business_status(self, status: str) -> bool:
        for row in range(self.run_list.count()):
            item = self.run_list.item(row)
            record = item.data(Qt.ItemDataRole.UserRole)
            if record.business_status == status:
                self.run_list.setCurrentRow(row)
                return True
        return False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scale_screenshot()

    def open_timeline(self):
        if self.replay_process is not None or not self.run_list.currentItem():return
        from signup031.timeline_dialog import TimelineDialog
        record=self.run_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.timeline_dialog=TimelineDialog(self,record);self.timeline_dialog.show()

    def start_replay(self, *, headless=False, investigate=False):
        if self.timeline_dialog is not None and self.timeline_dialog.process is not None:return
        if self.selected_archive is None or self.replay_process is not None:
            return
        from signup031.owned_job import create_worker
        args=[str(self.selected_archive)]
        if investigate:args.append('--investigate')
        if headless:args.append('--headless')
        process=create_worker(self,'signup031.replay',args,error_label=self.replay_label)
        if process is None:return
        self.replay_process = process
        self.replay_buffer = b''
        self.replay_failure = None
        self.replay_stderr = b''
        self.replay_discard_line = False
        item = self.run_list.currentItem()
        record = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.local_mode=investigate;self.local_session=None;self.local_record=record if investigate else None;self.local_source=None
        if investigate and record:
            from signup031.investigation import InvestigationStore
            try:self.local_source=InvestigationStore(self.root)._reference(InvestigationStore(self.root).record(record.execution_id))
            except (OSError,ValueError) as exc:
                self.replay_process=None;process.deleteLater();self.replay_label.setText('조사 원본 확인 실패 · '+str(exc));return
        captured = (record.manual_record or record.selenium_record or {}) if record else {}
        self.replay_limitations = captured.get('limitations', [])
        self.replay_label.setText('복원 시작 중 · ' + self.selected_archive.parent.name)
        self.replay_button.setEnabled(False)
        self.stop_replay_button.setEnabled(True)
        self._set_navigation_enabled(False)
        self.local_investigation_button.setEnabled(False)
        if investigate:self.investigation_button.setEnabled(True)
        process.readyReadStandardOutput.connect(self._replay_output)
        process.readyReadStandardError.connect(self._replay_stderr_output)
        process.finished.connect(self._replay_finished)
        process.errorOccurred.connect(self._replay_error)
        process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        process.start()

    def _replay_output(self, *, final=False):
        if self.replay_process is None:
            return
        self.replay_buffer += bytes(self.replay_process.readAllStandardOutput())
        if final and self.replay_buffer and not self.replay_buffer.endswith(b'\n'):
            self.replay_buffer += b'\n'
        while b'\n' in self.replay_buffer:
            line, self.replay_buffer = self.replay_buffer.split(b'\n', 1)
            if self.replay_discard_line or len(line) > 65536:
                self.replay_discard_line = False
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            status = event.get('status')
            if status=='investigation' and self.local_mode:
                from signup031.local_investigation import validate_session,session_text
                try:validate_session(event.get('investigation'))
                except (ValueError,TypeError,KeyError):continue
                if event.get('investigation') is None:continue
                self.local_session=event['investigation']
                from signup031.failure_presentation import restore_summary
                self.replay_label.setText(restore_summary(self.local_session))
                if self.investigation_dialog is not None:self.investigation_dialog.set_local_session(self.local_session,self.local_source)
                continue
            labels = {'starting': '복원 중', 'ready': '수동 테스트 가능',
                      'limited': '제한 상태 · 기록되지 않은 요청 차단',
                      'failed': '복원 실패', 'closed': '재현 종료'}
            if not isinstance(status, str) or status not in labels:
                continue
            reason = str(event.get('reason') or '')[:2000]
            if status == 'failed':
                self.replay_failure = reason or '작업자가 복원 실패를 보고했습니다.'
                self.replay_label.setText(self._replay_failure_text(self.replay_failure))
            elif self.replay_failure is None:
                self.replay_label.setText(labels[status] + (' · ' + reason if reason else ''))
        if len(self.replay_buffer) > 65536:
            self.replay_buffer = b''
            self.replay_discard_line = True

    def _replay_stderr_output(self):
        if self.replay_process is not None:
            self.replay_stderr = (self.replay_stderr + bytes(self.replay_process.readAllStandardError()))[-4096:]

    def _replay_failure_text(self, reason):
        if 'recorded target missing or ambiguous:' in reason:
            message = '복원 실패 · 저장 당시 조작한 요소를 복원 화면에서 찾을 수 없거나 하나로 식별할 수 없습니다.\n상세: ' + reason
        elif 'TimeoutError' in reason:
            message = '복원 실패 · 복원 화면에서 기록된 작업을 제한 시간 내 완료할 수 없습니다.\n상세: ' + reason
        else:
            message = '복원 실패 · ' + reason
        if self.replay_limitations:
            message += '\n수집 당시 제한: ' + '; '.join(str(value) for value in self.replay_limitations)[:1500]
        return message

    def stop_replay(self):
        if self.replay_process is not None:
            self.replay_process.write(b'stop\n')

    def _set_navigation_enabled(self, enabled):
        if not enabled:
            self.investigation_button.setEnabled(False)
            self.analysis_button.setEnabled(False)
            self.draft_button.setEnabled(False)
            self.source_button.setEnabled(False)
        elif self.run_list.currentItem():
            self.show_record(self.run_list.currentItem().data(Qt.ItemDataRole.UserRole))
        self.run_list.setEnabled(enabled)
        for name in ('chooseFolderButton', 'refreshButton', 'configureScenarioButton', 'manualRecordingButton'):
            self.findChild(QPushButton, name).setEnabled(enabled)

    def open_suite(self):
        if self.replay_process is not None or (self.scenario_editor is not None and self.scenario_editor.process is not None) or (self.manual_dialog is not None and self.manual_dialog.process is not None):return
        from signup031.suite_dialog import SuiteDialog
        try:
            if self.suite_dialog is None or self.suite_dialog.store.root != self.root:self.suite_dialog=SuiteDialog(self)
            self.suite_dialog.show();self.suite_dialog.raise_()
        except (OSError,ValueError) as exc:self.source_label.setText('묶음 저장소 오류 · '+str(exc))

    def open_browser_setup(self):
        from signup031.browser_dialog import BrowserDialog
        if self.browser_dialog is None:self.browser_dialog=BrowserDialog(self)
        self.browser_dialog.show();self.browser_dialog.raise_()

    def open_android_log(self):
        from signup031.viewer_model import _load_one
        from PySide6.QtWidgets import QDialog,QPlainTextEdit
        item=self.run_list.currentItem()
        if item is None:return
        record=_load_one(item.data(Qt.ItemDataRole.UserRole).source_path,self.root)
        if record.android_log is None:
            self.statusBar().showMessage('Android 로그 미수집 또는 첨부 검증 실패');return
        self.android_log_dialog=QDialog(self);self.android_log_dialog.setWindowTitle('Android 앱 로그 · PID/실행 구간');self.android_log_dialog.resize(900,650)
        layout=QVBoxLayout(self.android_log_dialog);self.android_log_text=QPlainTextEdit();self.android_log_text.setReadOnly(True)
        self.android_log_text.setPlainText(record.android_log or '(실행 구간의 앱 로그 없음)');layout.addWidget(self.android_log_text)
        self.android_log_dialog.show()

    def open_android(self):
        from signup031.android_dialog import AndroidDialog
        if self.android_dialog is None: self.android_dialog=AndroidDialog(self)
        self.android_dialog.show();self.android_dialog.raise_()

    def open_evaluation(self):
        from signup031.evaluation_dialog import EvaluationDialog
        import sqlite3
        try:
            if self.evaluation_dialog is None:self.evaluation_dialog=EvaluationDialog(self)
        except (ValueError,OSError,sqlite3.Error):self.statusBar().showMessage('AI 평가 저장소 열기 실패 · 저장 경로/잠금 상태를 확인하세요');return
        self.evaluation_dialog.show();self.evaluation_dialog.raise_()

    def open_ai_assistant(self):
        from signup031.ai_dialog import AIAssistantDialog
        if self.ai_dialog is None:self.ai_dialog=AIAssistantDialog(self)
        self.ai_dialog.show();self.ai_dialog.raise_()

    def open_scenario_editor(self):
        if self.suite_dialog is not None and self.suite_dialog.process is not None:return
        if self.replay_process is not None:
            return
        if self.manual_dialog is not None and self.manual_dialog.process is not None:
            return
        from signup031.scenario_editor import ScenarioEditor
        if self.scenario_editor is None:
            self.scenario_editor = ScenarioEditor(self.root, self)
            self.scenario_editor.recorded.connect(self._scenario_recorded)
        self.scenario_editor.artifacts_root = self.root
        self.scenario_editor.show()
        self.scenario_editor.raise_()

    def open_investigation(self):
        if (self.replay_process is not None and not self.local_mode) or not self.run_list.currentItem():
            return
        from signup031.investigation_dialog import InvestigationDialog
        record = self.local_record if self.replay_process is not None and self.local_mode else self.run_list.currentItem().data(Qt.ItemDataRole.UserRole)
        if self.investigation_dialog is not None and self.investigation_dialog.isVisible():
            self.investigation_dialog.raise_();return
        self.investigation_dialog = InvestigationDialog(self.root, record.execution_id, self)
        if self.local_mode and self.local_session and self.local_source and record.execution_id==self.local_source['execution_id']:
            self.investigation_dialog.set_local_session(self.local_session,self.local_source,active=self.replay_process is not None)
        self.investigation_dialog.show()

    def open_failure_analysis(self):
        self.open_investigation()
        if self.investigation_dialog is not None and self.investigation_dialog.isVisible():
            self.investigation_dialog.open_analysis()

    def create_tc_draft(self):
        if self.replay_process is not None or not self.run_list.currentItem():
            return
        if ((self.scenario_editor is not None and self.scenario_editor.process is not None) or
            (self.manual_dialog is not None and self.manual_dialog.process is not None)):
            return
        from signup031.manual_to_tc import draft_from_manual
        try:
            draft = draft_from_manual(self.run_list.currentItem().data(Qt.ItemDataRole.UserRole).source_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.source_label.setText('TC 초안 생성 실패 · ' + str(exc))
            return
        self.open_scenario_editor()
        self.scenario_editor.set_config(draft)
        self.scenario_editor.status_label.setText('TC 초안 · 기대 결과를 직접 작성한 뒤 새 기록을 실행하세요')

    def open_manual_source(self):
        if self.replay_process is not None or self.manual_source is None:
            return
        # Revalidate current files, rather than relying on a stale selected reference.
        current = self.run_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.load_root(self.root)
        from signup031.manual_to_tc import resolve_manual_source
        original, message = resolve_manual_source(self.records, current.scenario_snapshot['source_manual'])
        if original is None:
            self.source_label.setText(message)
            return
        for row in range(self.run_list.count()):
            if self.run_list.item(row).data(Qt.ItemDataRole.UserRole).source_path == original.source_path:
                self.run_list.setCurrentRow(row)
                break

    def open_manual_recorder(self):
        if self.suite_dialog is not None and self.suite_dialog.process is not None:return
        if self.replay_process is not None or (self.scenario_editor is not None and self.scenario_editor.process is not None):
            return
        from signup031.manual_dialog import ManualRecordingDialog
        if self.manual_dialog is None:
            self.manual_dialog = ManualRecordingDialog(self.root, self, headless=self.replay_headless)
            self.manual_dialog.recorded.connect(self._scenario_recorded)
        if self.manual_dialog.process is None:
            self.manual_dialog.artifacts_root = self.root
        self.manual_dialog.show()
        self.manual_dialog.raise_()

    def _scenario_recorded(self, evidence_path):
        self.load_root(self.root)
        for row in range(self.run_list.count()):
            record = self.run_list.item(row).data(Qt.ItemDataRole.UserRole)
            if record.source_path == Path(evidence_path):
                self.run_list.setCurrentRow(row)
                break

    def _replay_error(self, error):
        if self.replay_process is None:
            return
        self.replay_failure = self.replay_process.errorString()[:2000]
        self.replay_label.setText(self._replay_failure_text(self.replay_failure))
        if error == QProcess.ProcessError.FailedToStart:
            self._replay_finished(-1, QProcess.ExitStatus.CrashExit)

    def _replay_finished(self, code, status):
        self._replay_output(final=True)
        self._replay_stderr_output()
        process = self.replay_process
        self.replay_process = None
        self._set_navigation_enabled(True)
        self.stop_replay_button.setEnabled(False)
        self.replay_button.setEnabled(self.selected_archive is not None)
        self.local_investigation_button.setEnabled(self.selected_archive is not None)
        if self.local_mode and self.local_session and self.investigation_dialog is not None:
            self.investigation_dialog.set_local_session(self.local_session,self.local_source,active=False)
        if self.replay_failure is not None:
            self.replay_label.setText(self._replay_failure_text(self.replay_failure))
        elif code != 0:
            detail = ' · 작업자 오류 출력이 있습니다. 브라우저 준비 또는 설치 상태를 확인하세요.' if self.replay_stderr else ''
            self.replay_label.setText(self._replay_failure_text(f'프로세스 종료 코드 {code}' + detail))
        elif code == 0:
            self.replay_label.setText('재현 종료')
        if process:
            process.deleteLater()

    def closeEvent(self, event):
        if hasattr(self,'watch_timer'):self.watch_timer.stop()
        if self.investigation_dialog is not None:
            analysis=self.investigation_dialog.analysis_dialog
            if analysis is not None and analysis.process is not None:
                self.investigation_dialog.close();event.ignore();QTimer.singleShot(100,self,self.close);return
            self.investigation_dialog.close()
        if self.browser_dialog is not None:
            if self.browser_dialog.process is not None:self.browser_dialog.close();event.ignore();QTimer.singleShot(100,self.close);return
            self.browser_dialog.close()
        if self.android_dialog is not None:
            if self.android_dialog.process is not None:self.android_dialog.close();event.ignore();QTimer.singleShot(100,self.close);return
            self.android_dialog.close()
        if self.evaluation_dialog is not None:
            if self.evaluation_dialog.process is not None:self.evaluation_dialog.close();event.ignore();QTimer.singleShot(100,self.close);return
            self.evaluation_dialog.close()
        if self.ai_dialog is not None:
            if self.ai_dialog.process is not None:self.ai_dialog.close();event.ignore();QTimer.singleShot(100,self.close);return
            if any(editor.process is not None for editor in self.ai_dialog.editors):event.ignore();return
            for editor in self.ai_dialog.editors:editor.close()
            self.ai_dialog.close()
        if self.timeline_dialog is not None and self.timeline_dialog.process is not None:
            self.timeline_dialog.close();event.ignore();QTimer.singleShot(150,self.close);return
        if self.suite_dialog is not None and self.suite_dialog.process is not None:
            self.suite_dialog.close();event.ignore();QTimer.singleShot(150,self.close);return
        if (self.tc_catalog_dialog is not None and self.tc_catalog_dialog.sheets_dialog is not None and
                self.tc_catalog_dialog.sheets_dialog.process is not None):
            self.tc_catalog_dialog.sheets_dialog.cancel()
            event.ignore();QTimer.singleShot(150,self.close);return
        if (self.tc_catalog_dialog is not None and self.tc_catalog_dialog.import_dialog is not None and
                self.tc_catalog_dialog.import_dialog.process is not None):
            self.tc_catalog_dialog.import_dialog.cancel()
            event.ignore(); QTimer.singleShot(150, self.close); return
        if self.server_import_dialog is not None and self.server_import_dialog.process is not None:
            self.server_import_dialog.cancel_import()
            event.ignore()
            QTimer.singleShot(150, self.close)
            return
        if self.manual_dialog is not None and self.manual_dialog.process is not None:
            self.manual_dialog.cancel_recording()
            event.ignore()
            QTimer.singleShot(150, self.close)
            return
        if self.scenario_editor is not None and self.scenario_editor.process is not None:
            event.ignore()
            return
        if self.replay_process is not None:
            self.stop_replay()
            event.ignore()
            QTimer.singleShot(150, self.close)
        else:
            if self.tc_catalog_dialog is not None:self.tc_catalog_dialog.close()
            super().closeEvent(event)


def render_window_to_png(window, output):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    QApplication.processEvents()
    pixmap = window.grab()
    return pixmap.save(str(output), "PNG")


def default_result_root() -> Path:
    if getattr(sys,'frozen',False):
        from signup031.desktop_runtime import user_base
        return user_base()/'data'
    project_root = Path(__file__).resolve().parent.parent
    reviewed = project_root / "artifacts" / "review-final-20260915"
    return reviewed if reviewed.is_dir() else project_root / "artifacts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIGNUP-031 데스크톱 증거 조회")
    parser.add_argument("--root", type=Path, default=default_result_root())
    parser.add_argument("--render-png", type=Path)
    parser.add_argument(
        "--select-status",
        choices=("passed", "failed", "preparation_failed", "read_error", "incomplete", "legacy"),
    )
    args = parser.parse_args(argv)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = EvidenceViewerWindow(args.root)
    window.show()
    if args.select_status:
        window.select_business_status(args.select_status)
    if args.render_png:
        application.processEvents()
        success = render_window_to_png(window, args.render_png)
        window.close()
        if success:
            if sys.stdout is not None:print(f"rendered={args.render_png.resolve()}")
            return 0
        if sys.stderr is not None:print(f"render_failed={args.render_png.resolve()}", file=sys.stderr)
        return 1
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
