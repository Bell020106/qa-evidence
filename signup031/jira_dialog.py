"""Asynchronous QA/Jira status, publish and correlation recovery controls."""
import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout)


class JiraSyncDialog(QDialog):
    def __init__(self, investigation):
        super().__init__(investigation)
        self.investigation = investigation
        self.process = None
        self.cancelled = False
        self.output = b''
        self.setWindowTitle('Jira 상태·서버 동기화')
        self.resize(820, 620)
        layout = QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'jira',layout)
        self.status_label = QLabel('저장한 QA만 서버로 전송됩니다. 서버 메모는 로컬 편집을 자동으로 덮어쓰지 않습니다.')
        self.status_label.setWordWrap(True); self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        form = QFormLayout()
        self.token_edit = QLineEdit(); self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('서버 연결용 비밀키', self.token_edit)
        explanation=QLabel('서버를 설정한 사람에게 받은 연결 키입니다. 웹사이트 로그인 비밀번호가 아닙니다. 직접 기록만 할 때는 필요 없습니다.')
        explanation.setWordWrap(True);form.addRow(explanation)
        self.jobs = QComboBox(); form.addRow('복구 / 설정 오류 작업', self.jobs)
        self.key_edit = QLineEdit(); form.addRow('기존 Jira 이슈 키', self.key_edit)
        self.comment_edit = QLineEdit(); form.addRow('댓글 작업의 기존 댓글 ID', self.comment_edit)
        layout.addLayout(form)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details)
        buttons = QHBoxLayout(); self.actions = []
        for caption, action in [('상태 새로고침', 'refresh'), ('저장한 QA 전송', 'publish'),
                                ('빈 로컬 QA에 서버 메모 가져오기', 'import_qa'), ('기존 키 검증·복구', 'recover'),
                                ('설정 수정 후 재시도', 'retry')]:
            button = QPushButton(caption); button.clicked.connect(lambda _checked=False, a=action: self.start_action(a))
            buttons.addWidget(button); self.actions.append(button)
        layout.addLayout(buttons)
        self.cancel_button = QPushButton('취소 / 닫기'); self.cancel_button.clicked.connect(self.cancel)
        layout.addWidget(self.cancel_button)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.cancel)
        try:
            record = investigation.store.record(investigation.execution_id)
            source = json.loads((record.source_path.parent / 'remote-source.json').read_text(encoding='utf-8'))
            self.status_label.setText(source['server_origin'] + ' · ' + source['project_id'] + '\n수신 서버 인증으로 연결합니다. Jira 인증 값은 서버에서만 사용합니다.')
            self.render(json.loads((record.source_path.parent / 'remote-jira.json').read_text(encoding='utf-8')))
        except (OSError, ValueError, KeyError):
            self.status_label.setText('먼저 이 실행을 수신 서버에 전송하고 서버 결과 가져오기로 내려받으세요.')
            for button in self.actions: button.setEnabled(False)

    def render(self, state):
        self.jobs.clear()
        for job in state['jobs']:
            if job['status'] in ('uncertain', 'configuration_error'):
                self.jobs.addItem(job['status'] + ' · ' + job['kind'] + ' · ' + job['id'], job['id'])
        lines = [('로컬 HTTP 대역 · 실제 Jira 연결 검증 아님' if state.get('integration_mode') == 'loopback-test' else 'Jira Cloud 설정'),
                 '자동등록: ' + ('활성' if state['enabled'] else '비활성'),
                 '이슈 키: ' + (state['issue_key'] or '아직 연결되지 않음'),
                 '서버 QA 수정: ' + str(state['qa_revision'])]
        for job in state['jobs']:
            lines.append(f"{job['kind']} · {job['status']} · 시도 {job['attempts']}회 · {job['last_error'] or ''}" +
                         (f" · 원격 ID {job['remote_id']}" if job.get('remote_id') else ''))
        if state['qa']:
            lines.extend(['', '서버 QA 메모:', state['qa']['notes'], '서버 재검증:'])
            lines.extend(link['target']['execution_id'] + ' · ' + link['target_status'] + ' · ' + link['reason'] for link in state['qa']['retests'])
        self.details.setPlainText('\n'.join(lines))

    def start_action(self, action):
        if self.process is not None: return
        if not self.token_edit.text():
            self.status_label.setText('수신 서버 토큰을 입력하세요'); return
        request = {'root': str(self.investigation.store.root), 'execution_id': self.investigation.execution_id,
            'token': self.token_edit.text(), 'action': action, 'job_id': self.jobs.currentData(),
            'issue_key': self.key_edit.text().strip(), 'comment_id': self.comment_edit.text().strip() or None}
        from signup031.owned_job import create_worker
        process=create_worker(self,'signup031.jira_sync_worker')
        if process is None:return
        environment = process.processEnvironment()
        environment.insert('PYTHONIOENCODING', 'utf-8')
        for name in ('QA_JIRA_TOKEN', 'QA_JIRA_EMAIL', 'QA_PROJECT_TOKENS'): environment.remove(name)
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self.read_output)
        process.finished.connect(self._worker_finished)
        process.errorOccurred.connect(lambda _: self.status_label.setText('동기화 작업자 오류'))
        self.output = b''; self.cancelled = False; self.process = process
        for button in self.actions: button.setEnabled(False)
        self.token_edit.setEnabled(False)
        self.status_label.setText('서버 동기화 중…')
        process.start()
        if not process.waitForStarted(1500):
            self._worker_finished(2, None); return
        process.write((json.dumps(request, ensure_ascii=False) + '\n').encode())
        process.closeWriteChannel(); self.timer.start(30000)

    def read_output(self):
        if self.process is not None: self.output += bytes(self.process.readAllStandardOutput())

    def _worker_finished(self, code, _status):
        self.read_output(); process, self.process = self.process, None; self.timer.stop()
        for button in self.actions: button.setEnabled(True)
        self.token_edit.setEnabled(True)
        if self.cancelled:
            self.status_label.setText('취소됨 · 서버에 적용됐을 수 있으므로 상태를 새로고침하세요. 로컬 QA는 보존됩니다.')
        else:
            try:
                event = json.loads(self.output.splitlines()[-1])
                if code == 0 and event['status'] == 'complete':
                    self.render(event['state'])
                    self.status_label.setText('동기화 완료 · 로컬 QA 편집은 보존됩니다. 가져온 메모는 조사 창을 다시 열어 확인하세요.')
                else: self.status_label.setText(event['reason'])
            except (ValueError, IndexError, KeyError): self.status_label.setText('작업자 응답 오류 · 서버 상태를 다시 확인하세요')
        if process is not None: process.deleteLater()

    def cancel(self):
        if self.process is None: self.close(); return
        self.cancelled = True; self.process.kill()

    def closeEvent(self, event):
        if self.process is not None:
            self.cancel(); event.ignore(); QTimer.singleShot(100, self.close); return
        self.token_edit.clear(); super().closeEvent(event)
