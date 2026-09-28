"""Exact transmission preview and read-only analysis beside failure investigation."""
from copy import deepcopy
import json
from uuid import uuid4
from PySide6.QtCore import QProcess,QTimer,Qt
from PySide6.QtWidgets import QComboBox,QDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,QPlainTextEdit,QPushButton,QTabWidget,QVBoxLayout,QWidget
from signup031.ai_assistant import OpenAIAdapter,digest,strict_json
from signup031.failure_analysis import AnalysisStore,analysis_text,build_request,collect_snapshot,reject_secret
from signup031.owned_job import create_worker


class FailureAnalysisDialog(QDialog):
    def __init__(self,root,execution_id,parent=None):
        super().__init__(parent);self.root=root;self.execution_id=execution_id;self.store=AnalysisStore(root)
        self.process=None;self.request_id=None;self.previewed=None;self.result=None;self.active_request=None;self.buffer=b'';self.test_endpoint=None;self.timeout=45
        self.setWindowTitle('AI로 실패 원인 분석');self.setWindowModality(Qt.WindowModality.WindowModal);self.resize(1050,820)
        layout=QVBoxLayout(self);from signup031.help_dialog import add_help;add_help(self,'failure-analysis',layout)
        note=QLabel('원본 실패와 저장된 조사 메모를 함께 검토합니다. AI는 원인을 확정하거나 테스트 판정을 바꾸지 않습니다.\nAPI 키가 없어도 전송 자료를 준비·내보낼 수 있습니다. 입력 상한과 잘림 여부는 미리보기에 표시됩니다.')
        note.setWordWrap(True);layout.addWidget(note)
        form=QFormLayout();self.model=QLineEdit();self.model.setPlaceholderText('사용할 AI 모델 이름')
        self.key=QLineEdit();self.key.setEchoMode(QLineEdit.EchoMode.Password);self.key.setPlaceholderText('OpenAI 서비스에서 발급받는 별도 연결 키 · 이 창의 메모리에만 유지')
        form.addRow('AI 모델',self.model);form.addRow('API 연결용 비밀키',self.key);layout.addLayout(form)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        inputs=QWidget();inputs_layout=QVBoxLayout(inputs)
        self.collected=QPlainTextEdit();self.collected.setReadOnly(True);self.collected.setMaximumHeight(170);inputs_layout.addWidget(self.collected)
        self.expected=QPlainTextEdit();self.expected.setPlaceholderText('선택: 사용자가 추가할 기대 결과 · 최대 8000자 · 원래 수집한 자료와 구분됩니다.');self.expected.setMaximumHeight(85)
        self.code=QPlainTextEdit();self.code.setPlaceholderText('선택: 사용자가 추가할 자동화 코드 발췌 · 최대 16000자 · 비밀값은 넣지 마세요.');self.code.setMaximumHeight(130)
        inputs_layout.addWidget(QLabel('추가 기대 결과'));inputs_layout.addWidget(self.expected);inputs_layout.addWidget(QLabel('추가 자동화 코드'));inputs_layout.addWidget(self.code)
        self.tabs.addTab(inputs,'분석할 자료')
        self.preview=QPlainTextEdit();self.preview.setReadOnly(True);self.tabs.addTab(self.preview,'실제로 전송할 내용')
        self.output=QPlainTextEdit();self.output.setReadOnly(True);self.tabs.addTab(self.output,'분석 결과 · 추정')
        self.analysis_input=QPlainTextEdit();self.analysis_input.setReadOnly(True);self.tabs.addTab(self.analysis_input,'이 분석의 근거')
        history_row=QHBoxLayout();self.history=QComboBox();history_row.addWidget(self.history,1)
        refresh=QPushButton('저장 이력 새로 읽기');refresh.clicked.connect(self.refresh_history);history_row.addWidget(refresh);layout.addLayout(history_row)
        buttons=QHBoxLayout();self.preview_button=QPushButton('전송 내용 미리보기');self.preview_button.clicked.connect(self.preview_request)
        self.export_button=QPushButton('전송 자료 내보내기');self.export_button.clicked.connect(self.export_request)
        self.generate_button=QPushButton('확인한 내용으로 분석 요청');self.generate_button.clicked.connect(self.generate)
        self.cancel_button=QPushButton('요청 취소');self.cancel_button.clicked.connect(self.cancel)
        for button in (self.preview_button,self.export_button,self.generate_button,self.cancel_button):buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status_label=QLabel('AI 분석 미실행 · 모델을 입력하고 전송할 자료를 확인하세요.');self.status_label.setWordWrap(True);self.status_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status_label)
        close=QPushButton('닫기');close.clicked.connect(self.close);layout.addWidget(close)
        for field in (self.model,self.key):field.textChanged.connect(self.invalidate)
        for field in (self.expected,self.code):field.textChanged.connect(self.invalidate)
        self.history.currentIndexChanged.connect(self.show_history)
        try:
            self.refresh_inputs()
            self.refresh_history()
        except (ValueError,OSError,KeyError,TypeError) as exc:self.message(exc)
        self.controls()
    def snapshot(self):return collect_snapshot(self.root,self.execution_id,self.expected.toPlainText(),self.code.toPlainText())
    def refresh_inputs(self):
        if self.process is not None:return
        from signup031.failure_presentation import readable_evidence
        self.collected.setPlainText(readable_evidence(self.snapshot()));self.invalidate()
    def request(self):
        snapshot=self.snapshot();reject_secret(snapshot,self.key.text())
        request=build_request(self.model.text(),snapshot);reject_secret(request,self.key.text());return request
    def message(self,error):
        text=str(error)[:500]
        if self.key.text():text=text.replace(self.key.text(),'[REDACTED]')
        self.status_label.setText(text)
    def controls(self):
        active=self.process is not None
        for widget in (self.model,self.key,self.expected,self.code,self.preview_button,self.export_button):widget.setEnabled(not active)
        self.generate_button.setEnabled(not active and self.previewed is not None and bool(self.key.text()));self.cancel_button.setEnabled(active)
    def invalidate(self,*_):
        self.previewed=None;self.preview.clear();self.controls()
        if self.result:self.render_result()
    def preview_request(self):
        if self.process is not None:return
        try:
            self.refresh_inputs();self.previewed=self.request();self.preview.setPlainText(json.dumps(self.previewed,ensure_ascii=False,indent=2));self.tabs.setCurrentIndex(1)
            self.status_label.setText('AI 분석 미실행 · 위 자료만 전송합니다. 인증 키는 별도 헤더로 보내며 저장하지 않습니다.'+(' · HTTP 검증 대역' if self.test_endpoint else ''))
        except (ValueError,OSError,KeyError,TypeError) as exc:self.previewed=None;self.preview.clear();self.message(exc)
        self.controls()
    def export_request(self):
        if self.process is not None:return
        try:
            if self.previewed is None or self.request()!=self.previewed:raise ValueError('현재 전송 내용을 먼저 미리보기로 확인하세요.')
            from pathlib import Path
            path=self.store.qa._write(Path('exports')/('analysis-input-'+uuid4().hex+'.json'),json.dumps(self.previewed,ensure_ascii=False,indent=2))
            self.status_label.setText('전송 자료 내보냄 · AI 분석 미실행 · '+str(path));return path
        except (ValueError,OSError,KeyError,TypeError) as exc:self.message(exc)
    def generate(self):
        if self.process is not None:return
        try:
            request=self.request()
            if self.previewed is None or request!=self.previewed:raise ValueError('자료가 변경됐습니다. 현재 전송 내용을 다시 미리보기로 확인하세요.')
            OpenAIAdapter(self.key.text(),test_endpoint=self.test_endpoint,timeout=self.timeout)
            process=create_worker(self,'signup031.failure_analysis_worker')
            if process is None:return
        except (ValueError,OSError,KeyError,TypeError) as exc:self.message(exc);return
        self.process=process;self.request_id=uuid4().hex;rid=self.request_id;self.active_request=deepcopy(request);self.buffer=b''
        payload=json.dumps({'request_id':rid,'request':request,'key':self.key.text(),'test_endpoint':self.test_endpoint,'timeout':self.timeout},ensure_ascii=False).encode()+b'\n'
        def started():process.write(payload);process.closeWriteChannel()
        process.started.connect(started);process.readyReadStandardOutput.connect(lambda:self.read_output(process))
        process.finished.connect(lambda code,status:self.finished_request(process,rid,code))
        process.errorOccurred.connect(lambda error:self.finished_request(process,rid,-1) if error==QProcess.ProcessError.FailedToStart else None)
        self.status_label.setText('HTTP 검증 대역 요청 중 · 실제 AI 분석 아님' if self.test_endpoint else 'AI 분석 요청 중 · 취소할 수 있습니다.')
        self.controls();process.start();QTimer.singleShot(60000,self,lambda:self.cancel() if self.process is process else None)
    def read_output(self,process):
        if self.process is not process:return
        self.buffer+=bytes(process.readAllStandardOutput())
        if len(self.buffer)>350000:self.cancel();self.message('분석 응답 크기 상한 초과')
    def finished_request(self,process,rid,code):
        if self.process is not process:return
        self.read_output(process);self.process=None
        if self.request_id==rid:
            try:
                event=strict_json(self.buffer)
                if event.get('request_id')!=rid:raise ValueError('분석 요청 ID 불일치')
                if code!=0 or event.get('status')!='complete':raise ValueError(event.get('reason','AI 분석 작업 실패'))
                result=self.store.validate(event['analysis'],self.execution_id)
                if result['request_sha256']!=digest(self.active_request):raise ValueError('분석 응답이 현재 요청과 다릅니다.')
                reject_secret(result,self.key.text());self.store.save(result);self.result=result;self.refresh_history()
                self.render_result();self.tabs.setCurrentIndex(2)
                self.status_label.setText('분석 이력 저장 완료 · HTTP 검증 대역, 실제 AI 분석 아님' if self.test_endpoint else 'AI 분석 저장 완료 · 근거와 한계를 직접 검토하세요.')
            except (ValueError,OSError,KeyError,TypeError) as exc:self.message(exc)
        self.buffer=b'';self.active_request=None;self.request_id=None;process.deleteLater();self.controls()
    def cancel(self,*_):
        self.request_id=None;self.buffer=b'';self.active_request=None
        if self.process is not None:self.process._owned_job.close();self.process.kill()
        self.status_label.setText('분석 요청 취소 · 늦은 응답은 저장하지 않습니다.');self.controls()
    def refresh_history(self):
        try:
            rows=self.store.load(self.execution_id);self.history.blockSignals(True);self.history.clear()
            for result in rows:self.history.addItem(result['created_at']+' · '+result['provider'],result)
            self.history.blockSignals(False)
            if rows:self.show_history(0)
        except (ValueError,OSError,KeyError,TypeError) as exc:self.message(exc)
    def show_history(self,index):
        value=self.history.itemData(index)
        if value:self.result=value;self.render_result()
    def render_result(self):
        try:stale=digest(self.snapshot())!=self.result['input_sha256']
        except (ValueError,OSError,KeyError,TypeError):stale=True
        self.output.setPlainText(analysis_text(self.result,stale))
        self.analysis_input.setPlainText(json.dumps(self.result['snapshot'],ensure_ascii=False,indent=2))
    def reject(self):
        if self.process is not None:self.close();return
        self.key.clear();super().reject()
    def closeEvent(self,event):
        if self.process is not None:self.cancel();event.ignore();QTimer.singleShot(50,self,self.close);return
        self.key.clear();super().closeEvent(event)
