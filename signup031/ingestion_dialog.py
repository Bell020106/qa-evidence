"""Memory-only server connection dialog backed by an interruptible Qt worker."""
import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtWidgets import (QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)


class ServerImportDialog(QDialog):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.process = None
        self._output = b''
        self._cancelled = False
        self.setWindowTitle('서버 결과 가져오기')
        self.resize(650, 280)
        layout = QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'server',layout)
        form = QFormLayout()
        self.url_edit = QLineEdit('http://127.0.0.1:8765')
        self.url_edit.setObjectName('serverUrl')
        self.project_edit = QLineEdit()
        self.project_edit.setObjectName('serverProject')
        self.token_edit = QLineEdit()
        self.token_edit.setObjectName('serverToken')
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.root_edit = QLineEdit(str(viewer.root))
        self.root_edit.setObjectName('localResultRoot')
        form.addRow('서버 주소', self.url_edit)
        form.addRow('프로젝트 ID', self.project_edit)
        form.addRow('서버 연결용 비밀키', self.token_edit)
        explanation=QLabel('서버를 설정한 사람에게 받은 연결 키입니다. 웹사이트 로그인 비밀번호가 아닙니다. 직접 기록만 할 때는 필요 없습니다.')
        explanation.setWordWrap(True);form.addRow(explanation)
        row = QHBoxLayout(); row.addWidget(self.root_edit)
        self.browse_button = QPushButton('폴더 선택')
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        form.addRow('결과를 저장할 폴더', row)
        layout.addLayout(form)
        self.status_label = QLabel('연결 정보를 입력하세요. 비밀키는 이번 창의 메모리에만 보관합니다.')
        self.status_label.setObjectName('serverImportStatus')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout(); buttons.addStretch(1)
        self.import_button = QPushButton('서버 결과 가져오기')
        self.import_button.setObjectName('serverImportStart')
        self.import_button.clicked.connect(self.start_import)
        self.cancel_button = QPushButton('취소')
        self.cancel_button.setObjectName('serverImportCancel')
        self.cancel_button.clicked.connect(self.cancel_import)
        buttons.addWidget(self.import_button); buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(lambda: self.cancel_import(timed_out=True))
        self.worker_pid = None
        self._active_root = None
        self._timed_out = False

    def _set_inputs_enabled(self, enabled):
        for field in (self.url_edit, self.project_edit, self.token_edit, self.root_edit, self.browse_button):
            field.setEnabled(enabled)

    def _browse(self):
        selected = QFileDialog.getExistingDirectory(self, '결과를 저장할 로컬 폴더', self.root_edit.text())
        if selected: self.root_edit.setText(selected)

    def start_import(self):
        if self.process is not None: return
        from signup031.ingestion_client import IngestionClient
        try:
            IngestionClient(self.url_edit.text().strip(), self.project_edit.text().strip(), self.token_edit.text())
            root = Path(self.root_edit.text()).absolute()
            if not self.root_edit.text().strip() or str(root) == root.anchor:
                raise ValueError('로컬 결과 폴더를 선택하세요')
        except (ValueError, TypeError) as exc:
            self.status_label.setText('연결 설정 오류 · ' + str(exc))
            return
        self._output = b''
        self._cancelled = False
        self._timed_out = False
        self.worker_pid = None
        self._active_root = root
        from signup031.owned_job import create_worker
        process=create_worker(self,'signup031.ingestion_worker')
        if process is None:return
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self.process = process
        self.import_button.setEnabled(False)
        self._set_inputs_enabled(False)
        self.status_label.setText('서버 결과 가져오는 중…')
        process.start()
        if not process.waitForStarted(1500):
            self.status_label.setText('작업자 시작 실패')
            self.import_button.setEnabled(True)
            self._set_inputs_enabled(True)
            self.process = None
            process.deleteLater()
            return
        data = {'url': self.url_edit.text().strip(), 'project': self.project_edit.text().strip(),
                'token': self.token_edit.text(), 'root': str(root)}
        process.write((json.dumps(data, ensure_ascii=False) + '\n').encode())
        self._timeout_timer.start(60_000)

    def _read_output(self):
        if self.process is not None:
            self._output += bytes(self.process.readAllStandardOutput())
            for line in self._output.splitlines():
                try:
                    event = json.loads(line)
                    if event.get('status') == 'started' and type(event.get('pid')) is int:
                        self.worker_pid = event['pid']
                except (ValueError, TypeError):
                    pass

    def _process_error(self, _error):
        if not self._cancelled:
            self.status_label.setText('서버 가져오기 작업자 오류')

    def _finished(self, exit_code, _status):
        process = self.process
        self._read_output()
        self.process = None
        self._timeout_timer.stop()
        self.import_button.setEnabled(True)
        self._set_inputs_enabled(True)
        if self._cancelled:
            self.status_label.setText(('가져오기 시간 초과' if self._timed_out else '가져오기 취소 완료') +
                                      ' · 미완료 자료는 재현할 수 없습니다')
        else:
            try:
                event = json.loads(self._output.splitlines()[-1])
                if event['status'] == 'complete' and exit_code == 0:
                    self.status_label.setText(f'가져오기 완료 · {event["complete"]}개 결과')
                elif event['status'] == 'partial':
                    self.status_label.setText(f'부분 첨부 · {event["partial"]}개 미완료, 재현 제한')
                else:
                    self.status_label.setText('가져오기 실패 · ' + event.get('reason', '작업자 오류'))
            except (IndexError, ValueError, KeyError, TypeError):
                self.status_label.setText('가져오기 실패 · 작업자 응답을 읽을 수 없습니다')
        self.viewer.load_root(self._active_root)
        if process: process.deleteLater()

    def cancel_import(self, timed_out=False):
        if self.process is None:
            self.close()
            return
        self._cancelled = True
        self._timed_out = timed_out
        self.status_label.setText('가져오기 취소 중…' if not timed_out else '가져오기 시간 초과 · 작업 중단 중…')
        self.process.kill()

    def closeEvent(self, event):
        if self.process is not None:
            self.cancel_import()
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self.token_edit.clear()
        super().closeEvent(event)
