import json
from pathlib import Path
import sys
from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from signup031.manual_recording import validate_manual_url


class ManualRecordingDialog(QDialog):
    recorded = Signal(str)

    def __init__(self, artifacts_root, parent=None, *, headless=False):
        super().__init__(parent)
        self.artifacts_root = Path(artifacts_root).resolve()
        self.headless = headless
        self.process = None
        self.last_evidence = None
        self.buffer = b''
        self.terminal_event = False
        self.setWindowTitle('직접 테스트·기록')
        # Modeless: the user must be able to focus the separate browser.
        self.resize(760, 330)
        layout = QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'manual',layout)
        form = QFormLayout()
        self.url_edit = QLineEdit()
        self.title_edit = QLineEdit()
        form.addRow('시작할 사이트 주소', self.url_edit)
        form.addRow('기록 제목', self.title_edit)
        layout.addLayout(form)
        notice = QLabel('새 브라우저에서 직접 텍스트 입력·일반 클릭을 하세요. 문제가 보이면 현재 시점을 저장합니다.\n단일 탭·주 문서만 지원하며 비밀번호/민감 필드 값은 제외됩니다. 일반 입력·페이지 응답에는 민감정보가 남을 수 있습니다.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.status_label = QLabel('시작 전 · 자동 테스트 판정을 수행하지 않습니다')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.start_button = QPushButton('기록 시작')
        self.save_button = QPushButton('현재 시점 저장·종료')
        self.cancel_button = QPushButton('기록 취소·종료')
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        for button in (self.start_button, self.save_button, self.cancel_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.start_button.clicked.connect(self.start_recording)
        self.save_button.clicked.connect(self.save_recording)
        self.cancel_button.clicked.connect(self.cancel_recording)

    def start_recording(self):
        if self.process is not None:
            return
        try:
            validate_manual_url(self.url_edit.text())
            if not self.title_edit.text().strip():
                raise ValueError('기록 제목을 입력하세요')
        except ValueError as exc:
            self.status_label.setText('설정 오류 · ' + str(exc))
            return
        from signup031.owned_job import create_worker
        args=['--url',self.url_edit.text(),'--title',self.title_edit.text(),'--artifacts-dir',str(self.artifacts_root)]
        if self.headless:args.append('--headless')
        process=create_worker(self,'signup031.manual_recording',args)
        if process is None:return
        self.last_evidence = None
        self.buffer = b''
        self.terminal_event = False
        self.stopping = False
        self.process = process
        self.start_button.setEnabled(False)
        self.url_edit.setEnabled(False)
        self.title_edit.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_label.setText('브라우저 시작 중')
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        process.start()

    def save_recording(self):
        if self.process is None or self.stopping:
            return
        self.stopping = True
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.status_label.setText('현재 시점 저장 중 · 원본 세션 종료 후 자원 확정')
        self.process.write(b'save\n')

    def cancel_recording(self):
        if self.process is None or self.stopping:
            return
        self.stopping = True
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.status_label.setText('취소 중 · 미저장 종료')
        self.process.write(b'cancel\n')

    def _read(self):
        if self.process is None:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                event = json.loads(line)
                status = event.get('status')
                if status == 'recording':
                    if not self.stopping:
                        self.save_button.setEnabled(True)
                        limits = event.get('limitations', [])
                        self.status_label.setText(f'기록 중 · 동작 {event.get("count", 0)}개' +
                                                  (' · 제한: ' + '; '.join(limits) if limits else ''))
                else:
                    self.terminal_event = True
                    label = {'saved':'저장 완료 · 판정 미입력', 'limited':'부분 기록 저장 · 판정 미입력',
                             'cancelled':'취소 · 미저장 종료', 'failed':'기록/저장 실패'}.get(status, str(status))
                    self.status_label.setText(label + (' · '+str(event['reason']) if event.get('reason') else ''))
                    if event.get('evidence'):
                        path = Path(event['evidence']).resolve()
                        if not path.is_relative_to(self.artifacts_root) or not path.is_file():
                            raise ValueError('invalid result path')
                        self.last_evidence = path
                        self.recorded.emit(str(path))
            except (ValueError, TypeError, KeyError) as exc:
                self.status_label.setText('기록 응답 오류 · ' + str(exc))

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _finished(self, code, _status):
        if self.process is None:
            return
        self._read()
        process, self.process = self.process, None
        self.start_button.setEnabled(True)
        self.url_edit.setEnabled(True)
        self.title_edit.setEnabled(True)
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        if not self.terminal_event:
            self.status_label.setText(f'기록기 종료 · 미저장/실패 · 종료 코드 {code}')
        process.deleteLater()

    def reject(self):
        if self.process is not None:
            self.cancel_recording()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.process is not None:
            self.cancel_recording()
            event.ignore()
        else:
            super().closeEvent(event)
