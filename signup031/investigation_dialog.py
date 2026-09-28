"""QA investigation UI; original machine results remain read-only."""
from copy import deepcopy

from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                               QTabWidget, QVBoxLayout, QWidget)

from signup031.investigation import (CLASSIFICATIONS, REPORT_FIELDS, InvestigationStore,
                                    collected_environment, execution_differences, investigation_candidates)


class InvestigationDialog(QDialog):
    def __init__(self, root, execution_id, parent=None):
        super().__init__(parent)
        self.execution_id = execution_id
        self.doc = None
        self.last_export = None
        self.analysis_dialog=None
        self.setWindowTitle('실패 조사 메모')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(1050, 880)
        layout = QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'report',layout)
        self.original_label = QLabel()
        self.original_label.setWordWrap(True)
        self.original_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.original_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        from signup031.local_investigation import LocalInvestigationStore,VERDICTS
        self.local_store=LocalInvestigationStore(root);self.local_doc=None
        local_page=QWidget();local_layout=QVBoxLayout(local_page)
        self.local_state=QLabel('저장된 로컬 조사');self.local_state.setWordWrap(True);self.local_state.setTextFormat(Qt.TextFormat.PlainText);local_layout.addWidget(self.local_state)
        self.local_original=QPlainTextEdit();self.local_original.setReadOnly(True);self.local_original.setMaximumHeight(170);local_layout.addWidget(self.local_original)
        self.local_details_button=QPushButton('원본 오류·복원 상세 펼치기');self.local_details_button.setCheckable(True)
        local_layout.insertWidget(1,self.local_details_button);self.local_details_button.toggled.connect(self.local_original.setVisible);self.local_original.hide()
        self.local_verdict=QComboBox()
        for key,label in VERDICTS.items():self.local_verdict.addItem(label,key)
        local_layout.addWidget(QLabel('직접 조작한 결과 · 원본 테스트 판정과 별도'));local_layout.addWidget(self.local_verdict)
        self.local_notes=QPlainTextEdit();self.local_notes.setPlaceholderText('무엇을 클릭·입력했고 어떤 결과가 보였는지 기록하세요. 복원 제한으로 판단하지 못했다면 그 이유도 남기세요.');local_layout.addWidget(self.local_notes)
        local_row=QHBoxLayout();self.local_save=QPushButton('수동 조사 결과·메모 저장');self.local_save.clicked.connect(self.save_local);local_row.addWidget(self.local_save)
        self.local_stop=QPushButton('조사 브라우저 종료');self.local_stop.setEnabled(False)
        if parent is not None and hasattr(parent,'stop_replay'):self.local_stop.clicked.connect(parent.stop_replay)
        local_row.addWidget(self.local_stop);local_layout.addLayout(local_row)
        self.analysis_button=QPushButton('조사 저장 후 AI 분석');self.analysis_button.clicked.connect(self.open_analysis);local_layout.addWidget(self.analysis_button)
        self.local_tab=self.tabs.addTab(local_page,'로컬 수동 조사')
        investigation = QWidget()
        form = QFormLayout(investigation)
        self.classification = QComboBox()
        self.classification.addItems(CLASSIFICATIONS)
        form.addRow('문제 분류 · 원본 판정과 별도', self.classification)
        self.grounds = QPlainTextEdit()
        self.notes = QPlainTextEdit()
        self.candidates = QPlainTextEdit()
        self.candidates.setReadOnly(True)
        form.addRow('분류 근거', self.grounds)
        form.addRow('조사 메모', self.notes)
        form.addRow('기계 후보 · 원인 미확인', self.candidates)
        self.tabs.addTab(investigation, 'QA 조사')

        report_page = QWidget()
        report_layout = QFormLayout(report_page)
        self.report_fields = {}
        for key, caption in REPORT_FIELDS.items():
            field = QPlainTextEdit()
            field.setMinimumHeight(65)
            field.setMaximumHeight(120)
            self.report_fields[key] = field
            report_layout.addRow(caption, field)
        report_scroll = QScrollArea()
        report_scroll.setWidgetResizable(True)
        report_scroll.setWidget(report_page)
        self.tabs.addTab(report_scroll, '버그 보고 초안')

        retest_page = QWidget()
        retest_layout = QVBoxLayout(retest_page)
        retest_layout.addWidget(QLabel('후속 실행을 선택하고 TC·환경 차이와 연결 이유를 남기세요. 최초 실패와 자동 상태는 유지됩니다.'))
        self.retest_combo = QComboBox()
        self.retest_combo.addItem('후속 실행 선택', None)
        retest_layout.addWidget(self.retest_combo)
        self.comparison = QPlainTextEdit()
        self.comparison.setReadOnly(True)
        retest_layout.addWidget(self.comparison)
        self.link_reason = QLineEdit()
        self.link_reason.setPlaceholderText('재검증 연결 이유 · 차이가 있다면 비교 범위도 설명하세요')
        retest_layout.addWidget(self.link_reason)
        self.link_button = QPushButton('재검증 연결 · QA 변경 함께 저장')
        self.link_button.clicked.connect(self.link_retest)
        retest_layout.addWidget(self.link_button)
        self.retests = QPlainTextEdit()
        self.retests.setReadOnly(True)
        retest_layout.addWidget(self.retests)
        self.tabs.addTab(retest_page, '재검증')
        self.retest_combo.currentIndexChanged.connect(self.compare)
        # Legacy documents remain readable by the stores; the failure-only app has one workflow.
        while self.tabs.count()>1:
            old=self.tabs.widget(1);self.tabs.removeTab(1);old.hide()

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        self.save_button = QPushButton('QA 분류·보고서 저장')
        self.save_button.clicked.connect(self.save)
        self.export_button = QPushButton('저장·Markdown 내보내기')
        self.export_button.clicked.connect(self.export)
        self.jira_button = QPushButton('Jira 상태·서버 동기화')
        self.jira_button.clicked.connect(self.open_jira)
        self.jira_dialog = None
        close = QPushButton('닫기')
        close.clicked.connect(self.close)
        for button in (self.save_button, self.export_button, self.jira_button, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.tabs.currentChanged.connect(self.update_save_actions);self.update_save_actions()
        try:
            self.store = InvestigationStore(root)
            self.doc = self.store.load(execution_id)
            record = self.store.record(execution_id)
            self.local_doc=self.local_store.load(execution_id)
            from signup031.failure_presentation import restore_summary,failure_summary
            self.original_summary='원본 자동화 결과\n'+failure_summary(record.message)
            self.local_state.setText(self.original_summary+'\n\n저장된 조사 상태\n'+restore_summary(self.local_doc['session']))
            self.local_notes.setPlainText(self.local_doc['notes']);self.local_verdict.setCurrentIndex(self.local_verdict.findData(self.local_doc['verdict']))
            limits=(record.selenium_record or record.manual_record or {}).get('limitations',[])
            from signup031.test_context import load_test_context
            context=load_test_context(record.source_path)
            import json
            self.original_details='원본 실패·결과: '+record.message+'\n기대 결과: '+self.doc['report']['expected']+'\n수집 제한: '+('; '.join(limits) or '기록된 제한 없음')+'\n수집된 테스트 자료: '+(json.dumps(context['test_context'],ensure_ascii=False,indent=2) if context else '미수집')
            self.local_original.setPlainText(self.original_details+'\n복원 상세: '+json.dumps(self.local_doc['session'],ensure_ascii=False,indent=2))
            self.original_label.setText(f'원본 상태: {record.status_label} · {record.tc_id}\n실행 ID: {execution_id}\n자료 상태: {record.archive_message}\nQA 판단은 원본 evidence·archive·자동 판정을 변경하지 않습니다.')
            self.classification.setCurrentText(self.doc['classification'])
            self.grounds.setPlainText(self.doc['grounds'])
            self.notes.setPlainText(self.doc['notes'])
            for key, field in self.report_fields.items():
                field.setPlainText(self.doc['report'][key])
            self.candidates.setPlainText('\n\n'.join(
                f'{c["status"]} · {c["candidate"]}\n근거 위치/관측: {c["evidence"]}\n부족한 정보: {c["missing"]}\n다음 확인: {c["next"]}'
                for c in investigation_candidates(record)))
            for candidate in self.store.records():
                if candidate.execution_id != execution_id:
                    self.retest_combo.addItem(f'{candidate.tc_id} · {candidate.status_label} · {candidate.execution_id}', candidate.execution_id)
            self.refresh_retests()
            self.status_label.setText('조사 메모는 닫기 전 저장하세요. 저장하지 않은 편집은 닫으면 사라집니다.')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status_label.setText('조사 자료 읽기 실패 · ' + str(exc))
            for control in (self.tabs, self.save_button, self.export_button, self.link_button):
                control.setEnabled(False)

    def set_local_session(self,session,source,*,active=True):
        from signup031.local_investigation import validate_session
        from signup031.failure_presentation import restore_summary
        if self.local_doc is None or self.local_doc['source']!=source:return
        validate_session(session)
        previous=self.local_doc['session']
        if previous is None or previous['session_id']!=session['session_id']:
            self.local_verdict.setCurrentIndex(self.local_verdict.findData('undetermined'))
            self.local_notes.clear()
        self.local_doc['session']=deepcopy(session)
        self.local_state.setText(self.original_summary+'\n\n'+('현재 조사 세션\n' if active else '조사 브라우저 종료 · 마지막 복원 상태\n')+restore_summary(session))
        import json
        self.local_original.setPlainText(self.original_details+'\n복원 상세: '+json.dumps(session,ensure_ascii=False,indent=2))
        self.local_stop.setEnabled(active);self.tabs.setCurrentIndex(self.local_tab)

    def save_local(self):
        if self.local_doc is None:return False
        try:
            doc=deepcopy(self.local_doc);doc['notes']=self.local_notes.toPlainText();doc['verdict']=self.local_verdict.currentData()
            self.local_doc=self.local_store.save(doc)
            self.status_label.setText('수동 조사 저장 완료 · 원본 실패 판정 유지')
            return True
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self.status_label.setText('조사 저장 실패 · 입력은 창에 남아 있습니다 · '+str(exc));return False

    def update_save_actions(self,*_):
        for button in (self.save_button,self.export_button,self.jira_button):button.hide()

    def open_analysis(self):
        if self.local_doc is None or not self.save_local():return
        from signup031.failure_analysis_dialog import FailureAnalysisDialog
        if self.analysis_dialog is None:self.analysis_dialog=FailureAnalysisDialog(self.store.root,self.execution_id,self)
        try:self.analysis_dialog.refresh_inputs()
        except (OSError,ValueError,KeyError,TypeError) as exc:self.status_label.setText('분석 자료 읽기 실패 · '+str(exc));return
        self.analysis_dialog.show();self.analysis_dialog.raise_()

    def closeEvent(self,event):
        if self.analysis_dialog is not None:
            if self.analysis_dialog.process is not None:self.analysis_dialog.close();event.ignore();QTimer.singleShot(100,self,self.close);return
            self.analysis_dialog.close()
        super().closeEvent(event)

    def open_jira(self):
        if self.doc is None: return
        from signup031.jira_dialog import JiraSyncDialog
        self.jira_dialog = JiraSyncDialog(self)
        self.jira_dialog.show()

    def edited(self):
        doc = deepcopy(self.doc)
        doc['classification'] = self.classification.currentText()
        doc['grounds'] = self.grounds.toPlainText()
        doc['notes'] = self.notes.toPlainText()
        doc['report'] = {key: field.toPlainText() for key, field in self.report_fields.items()}
        return doc

    def save(self):
        try:
            self.doc = self.store.save(self.edited())
            self.status_label.setText(f'QA 저장 완료 · 수정 {self.doc["revision"]}회 · 원본 상태 유지')
            return True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status_label.setText('QA 저장 실패 · 편집 내용은 창에 남아 있습니다 · ' + str(exc))
            return False

    def export(self):
        if not self.save():
            return
        try:
            self.last_export = self.store.export(self.doc)
            self.status_label.setText('보고서 내보내기 완료 · ' + str(self.last_export))
        except (OSError, ValueError) as exc:
            self.status_label.setText('내보내기 실패 · QA 저장 자료는 유지됩니다 · ' + str(exc))

    def compare(self):
        target_id = self.retest_combo.currentData()
        if target_id is None:
            self.comparison.clear()
            return
        try:
            source, target = self.store.record(self.execution_id), self.store.record(target_id)
            differences = execution_differences(source, target)
            self.comparison.setPlainText(f'원본 {source.status_label} → 후속 {target.status_label}\n' +
                ('\n'.join(differences) or '수집된 TC/URL 차이 없음') +
                '\n원본 환경: ' + str(collected_environment(source)) + '\n후속 환경: ' + str(collected_environment(target)) +
                '\n미수집 환경은 비교할 수 없습니다. URL 일치만으로 동일 환경을 보장하지 않습니다.')
        except (OSError, ValueError) as exc:
            self.comparison.setPlainText('비교 불가 · ' + str(exc))

    def link_retest(self):
        try:
            self.doc = self.store.link_retest(self.edited(), self.retest_combo.currentData(), self.link_reason.text())
            self.refresh_retests()
            self.status_label.setText('재검증 연결 완료 · 최초 기록과 상태 유지')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status_label.setText('재검증 연결 실패 · ' + str(exc))

    def refresh_retests(self):
        self.retests.setPlainText('\n\n'.join(
            f'{d["target"]["execution_id"]}\n{d["message"]}\n차이: {"; ".join(d["differences"]) or "수집된 차이 없음"}\n연결 이유: {d["reason"]}\n연결 시각: {d["linked_at"]}'
            for d in self.store.retest_details(self.doc)) or '연결된 재검증 없음')
