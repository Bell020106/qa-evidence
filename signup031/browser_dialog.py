import os
from pathlib import Path
from PySide6.QtCore import QProcess,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QPushButton,QPlainTextEdit
from signup031.owned_job import configure_worker
from signup031.desktop_runtime import user_base


class BrowserDialog(QDialog):
    def __init__(self,parent):
        super().__init__(parent);self.process=None;self.setWindowTitle('웹 브라우저 준비');self.resize(800,500)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'browser',layout)
        path=os.environ.get('PLAYWRIGHT_BROWSERS_PATH',str(user_base()/'browsers'))
        note=QLabel('첫 사용에는 인터넷 연결이 필요합니다. 이 버전의 Playwright가 요구하는 Chromium만 다운로드합니다.\n저장 위치: '+path);note.setWordWrap(True);layout.addWidget(note)
        self.output=QPlainTextEdit();self.output.setReadOnly(True);layout.addWidget(self.output)
        self.install=QPushButton('Chromium 다운로드·준비');self.install.clicked.connect(self.start);layout.addWidget(self.install)
        self.stop=QPushButton('취소');self.stop.clicked.connect(self.cancel);self.stop.setEnabled(False);layout.addWidget(self.stop)
    def start(self):
        if self.process is not None:return
        self.output.clear();process=QProcess(self)
        try:configure_worker(process,'signup031.browser_install')
        except OSError as exc:self.output.setPlainText('작업자 준비 실패: '+str(exc));return
        self.process=process;process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.read)
        process.finished.connect(lambda code,status:self.finished(process,code))
        process.errorOccurred.connect(lambda error:self.finished(process,-1) if error==QProcess.ProcessError.FailedToStart else None)
        self.install.setEnabled(False);self.stop.setEnabled(True);process.start()
        QTimer.singleShot(600000,lambda:self.cancel() if self.process is process else None)
    def read(self):
        if self.process is None:return
        text=self.output.toPlainText()+bytes(self.process.readAllStandardOutput()).decode('utf-8','replace')
        self.output.setPlainText(text[-64000:])
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())
    def finished(self,process,code):
        if self.process is not process:return
        self.read();self.process=None;self.install.setEnabled(True);self.stop.setEnabled(False)
        self.output.appendPlainText('브라우저 준비 완료' if code==0 else '브라우저 준비 실패/취소 · 인터넷·저장 권한을 확인하고 다시 실행하세요.')
        process.deleteLater()
    def cancel(self):
        if self.process is not None:
            self.process._owned_job.close();self.process.kill()
    def reject(self):
        if self.process is not None:self.close()
        else:super().reject()
    def closeEvent(self,event):
        if self.process is not None:self.cancel();event.ignore();QTimer.singleShot(50,self.close);return
        super().closeEvent(event)
