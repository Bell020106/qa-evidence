"""Explicit request preview, cancellable API call and a separate QA draft editor."""
import json
from uuid import uuid4
from PySide6.QtCore import QProcess,QProcessEnvironment,QTimer,Qt
from PySide6.QtWidgets import QDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,QPlainTextEdit,QPushButton,QVBoxLayout
from signup031.ai_assistant import build_request,OpenAIAdapter
from signup031.owned_job import OwnedJob,python_command,worker_environment
from signup031.web_scenario import validate_scenario


class AIAssistantDialog(QDialog):
    def __init__(self,viewer):
        super().__init__(viewer);self.viewer=viewer;self.process=None;self.job=None;self.proposal=None;self.previewed=None
        self.request_id=None;self.test_endpoint=None;self.editor=None;self.editors=[];self.output=b''
        self.setWindowTitle('AI로 테스트 초안 만들기 · 검토 필요');self.resize(1050,900);self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'ai',layout);note=QLabel('사용자가 입력한 내용으로 미검토 TC 초안을 제안합니다. 페이지를 관측하거나 테스트를 실행한 결과가 아닙니다.\n전송 내용을 확인한 뒤 생성하세요. 기존 기록/HAR/로그인 세션을 자동으로 읽지 않습니다.');note.setWordWrap(True);layout.addWidget(note)
        form=QFormLayout();self.model=QLineEdit();self.model.setPlaceholderText('사용할 Responses API 모델 ID (필수)')
        self.key=QLineEdit();self.key.setEchoMode(QLineEdit.EchoMode.Password);self.key.setPlaceholderText('별도 OpenAI API 키 · 이 창의 메모리에만 유지')
        self.url=QLineEdit();self.goal=QPlainTextEdit();self.description=QPlainTextEdit();self.goal.setMaximumHeight(90);self.description.setMaximumHeight(110)
        self.description.setPlaceholderText('선택 사항: 직접 제공할 요소/페이지 설명. 비밀값을 넣지 마세요.')
        for label,control in (('모델',self.model),('API 키',self.key),('대상 사이트 주소',self.url),('테스트 목표',self.goal),('선택한 페이지 정보',self.description)):form.addRow(label,control)
        layout.addLayout(form)
        key_help=QLabel('API 키는 OpenAI 서비스에서 발급받는 별도 연결용 비밀키입니다. 웹사이트 비밀번호나 서버 연결 키와 다르며, 직접 기록만 할 때는 필요 없습니다.')
        key_help.setWordWrap(True);layout.addWidget(key_help)
        row=QHBoxLayout();self.preview_button=QPushButton('전송 내용 미리보기');self.preview_button.clicked.connect(self.preview_request);row.addWidget(self.preview_button)
        self.generate_button=QPushButton('확인한 내용으로 초안 요청');self.generate_button.clicked.connect(self.generate);row.addWidget(self.generate_button)
        self.cancel_button=QPushButton('요청 취소');self.cancel_button.clicked.connect(self.cancel);row.addWidget(self.cancel_button);layout.addLayout(row)
        self.preview=QPlainTextEdit();self.preview.setReadOnly(True);layout.addWidget(self.preview,1)
        self.status_label=QLabel('설정 필요 · 모델과 별도 API 키를 입력하세요. Codex 계정은 사용하지 않습니다.');self.status_label.setWordWrap(True);self.status_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status_label)
        self.open_button=QPushButton('검토 편집기로 초안 열기');self.open_button.clicked.connect(self.open_draft);layout.addWidget(self.open_button)
        close=QPushButton('닫기');close.clicked.connect(self.close);layout.addWidget(close)
        for control in (self.model,self.key,self.url):control.textChanged.connect(self.invalidate)
        for control in (self.goal,self.description):control.textChanged.connect(self.invalidate)
        self.controls()

    def invalidate(self):self.previewed=None;self.preview.clear();self.controls()
    def request(self):return build_request(self.model.text(),self.url.text(),self.goal.toPlainText(),self.description.toPlainText())
    def controls(self):
        active=self.process is not None
        for control in (self.model,self.key,self.url,self.goal,self.description,self.preview_button):control.setEnabled(not active)
        self.generate_button.setEnabled(not active and self.previewed is not None);self.cancel_button.setEnabled(active)
        self.open_button.setEnabled(not active and self.proposal is not None)

    def preview_request(self):
        if self.process is not None:return
        try:
            request=self.request();raw=json.dumps(request,ensure_ascii=False,indent=2)
            if self.key.text() and self.key.text() in raw:raise ValueError('설명에 API 키를 넣을 수 없습니다')
            self.previewed=request;self.preview.setPlainText(raw)
            self.status_label.setText(('로컬 HTTP 검증 대역 · 실제 AI 생성 아님' if self.test_endpoint else 'OpenAI Responses API로 전송')+' · 위 JSON만 전송합니다. 인증 키는 별도 헤더이며 저장하지 않습니다.')
        except ValueError as exc:self.previewed=None;self.preview.clear();self.status_label.setText(str(exc))
        self.controls()

    def generate(self):
        if self.process is not None:return
        try:
            request=self.request()
            if self.previewed is None or request!=self.previewed:raise ValueError('현재 전송 내용을 먼저 미리보기로 확인하세요')
            OpenAIAdapter(self.key.text(),test_endpoint=self.test_endpoint)
            self.job=OwnedJob()
        except (ValueError,OSError) as exc:self.status_label.setText(str(exc));return
        self.proposal=None;self.output=b'';self.request_id=uuid4().hex;request_id=self.request_id
        payload=json.dumps({'request_id':request_id,'request':request,'key':self.key.text(),'test_endpoint':self.test_endpoint},ensure_ascii=False).encode()+b'\n'
        process=QProcess(self);self.process=process;command=python_command('signup031.ai_worker');process.setProgram(command[0]);process.setArguments(command[1:])
        environment=QProcessEnvironment()
        for key,value in worker_environment().items():environment.insert(key,value)
        process.setProcessEnvironment(environment)
        def started():
            try:self.job.attach(process.processId());process.write(payload);process.closeWriteChannel()
            except OSError:self.cancel('소유 프로세스 보호 실패')
        process.started.connect(started);process.readyReadStandardOutput.connect(lambda:self.read_output(process))
        process.finished.connect(lambda code,status:self.finished_request(process,request_id,code))
        process.errorOccurred.connect(lambda error:self.finished_request(process,request_id,-1) if error==QProcess.ProcessError.FailedToStart else None)
        self.status_label.setText('제안 요청 중 · 60초 상한 · 자동 재시도 없음');self.controls();process.start()
        QTimer.singleShot(60000,lambda:self.cancel('전체 요청 시간 초과') if self.process is process else None)

    def read_output(self,process):
        if self.process is not process:return
        self.output+=bytes(process.readAllStandardOutput())
        if len(self.output)>1_000_000:self.cancel('출력 상한 초과')

    def finished_request(self,process,request_id,code):
        if self.process is not process:return
        self.read_output(process);self.process=None
        if self.job:self.job.close();self.job=None
        if self.request_id==request_id:
            try:
                result=json.loads(self.output)
                if result.get('request_id')!=request_id:raise ValueError('요청 ID 불일치')
                if code!=0 or result.get('status')!='complete':raise ValueError(result.get('reason','AI 요청 실패'))
                self.proposal=validate_scenario(result['proposal'],allow_draft=True)
                if not self.proposal.get('draft') or self.proposal['source_ai']['reviewed']:raise ValueError('미검토 초안 계약 오류')
                self.status_label.setText(('HTTP 대역 제안' if self.test_endpoint else 'AI 제안')+' 수신 · QA 검토 필요 · 실제 관측/실행 없음\n'+'\n'.join(self.proposal['source_ai']['review_notes'])[:400])
                self.preview.setPlainText('수신한 미검토 제안 (전송 미리보기와 별개)\n'+json.dumps(self.proposal,ensure_ascii=False,indent=2))
            except (ValueError,KeyError,TypeError):
                self.proposal=None;self.status_label.setText('제안 실패 · '+str(locals().get('result',{}).get('reason','응답 형식/프로세스 오류'))[:300])
        self.output=b'';process.deleteLater();self.controls()

    def cancel(self,reason='요청 취소 · 늦은 응답은 폐기합니다'):
        if not isinstance(reason,str):reason='요청 취소 · 늦은 응답은 폐기합니다'
        self.request_id=None;self.proposal=None;self.output=b''
        if self.process is not None:
            if self.job:self.job.close();self.job=None
            self.process.kill()
        self.status_label.setText(reason);self.controls()

    def open_draft(self):
        if self.process is not None or self.proposal is None:return
        from signup031.scenario_editor import ScenarioEditor
        self.editor=ScenarioEditor(self.viewer.root,self.viewer);self.editors.append(self.editor)
        self.editor.recorded.connect(self.viewer._scenario_recorded);self.editor.set_config(self.proposal)
        self.editor.status_label.setText('AI 미검토 초안 · 요소/기대값 검토와 저장 후 직접 기록 실행하세요')
        self.proposal=None;self.controls();self.hide();self.editor.show()

    def reject(self):
        if self.process is not None:self.close();return
        self.key.clear();super().reject()
    def closeEvent(self,event):
        if self.process is not None:self.cancel();event.ignore();QTimer.singleShot(50,self.close);return
        self.key.clear();super().closeEvent(event)
