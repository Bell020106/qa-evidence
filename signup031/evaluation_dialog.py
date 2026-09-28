"""Desktop AI evaluation datasets, frozen runs, independent review and exports."""
import json
import sqlite3
from PySide6.QtCore import QProcess,QProcessEnvironment,QTimer,Qt
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,
    QPlainTextEdit,QPushButton,QSpinBox,QSplitter,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)
from signup031.ai_assistant import OpenAIAdapter,digest,strict_json
from signup031.ai_evaluation import EvaluationStore,validate_dataset,request_for,summarize,compare_runs,ensure_no_key
from signup031.evaluation_worker import encode_request
from signup031.owned_job import OwnedJob,python_command,worker_environment

GEN={'queued':'대기','not_run':'미실행','running':'생성 중','completed':'생성 완료','generation_error':'생성 오류','cancelled':'취소','interrupted':'중단'}
RUN={**GEN,'completed':'처리 완료'}
AUTO={'passed':'통과','failed':'실패','not_evaluated':'미평가','not_configured':'기준 없음'}


def storage_guard(method):
    def guarded(self,*args,**kwargs):
        try:return method(self,*args,**kwargs)
        except (sqlite3.Error,OSError,ValueError) as exc:self.message(exc)
    return guarded


class EvaluationDialog(QDialog):
    def __init__(self,viewer):
        super().__init__(viewer);self.viewer=viewer;self.store=EvaluationStore(viewer.root);self.process=None;self.job=None;self.run_id=None
        recovery=self.store.recover()
        self.current_run=None;self.preview_hash=None;self.test_endpoint=None;self.buffer=b'';self.case_serial=0
        self.setWindowTitle('AI 답변 평가 · 자동 기준과 사람 판단');self.resize(1250,1000);self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'evaluation',layout);note=QLabel('선택한 프롬프트와 사례 입력만 대상 API에 보냅니다. 자동 기준은 문자열/JSON 계약 검사이며 의미·공정성·안전성의 일반 보장이 아닙니다. AI 평가자는 미설정/미실행입니다.');note.setWordWrap(True);layout.addWidget(note)
        split=QSplitter(Qt.Orientation.Vertical);layout.addWidget(split,1);self.form=QWidget();top=QVBoxLayout(self.form);split.addWidget(self.form)
        row=QHBoxLayout();self.versions=QComboBox();row.addWidget(self.versions,1);button=QPushButton('저장 버전 불러오기');button.clicked.connect(self.load_version);row.addWidget(button);top.addLayout(row)
        fields=QFormLayout();ids=QHBoxLayout();self.dataset_id=QLineEdit();self.version=QLineEdit();self.title_edit=QLineEdit()
        for label,widget in (('ID',self.dataset_id),('버전',self.version),('제목',self.title_edit)):ids.addWidget(QLabel(label));ids.addWidget(widget)
        fields.addRow(ids);settings=QHBoxLayout();self.model=QLineEdit();self.model.setPlaceholderText('사용할 AI 모델 이름');self.tokens=QSpinBox();self.tokens.setRange(1,6000);self.tokens.setValue(1000)
        self.normalization=QComboBox();self.normalization.addItem('문자 그대로 (공백·대소문자 유지)','literal');self.normalization.addItem('앞뒤 공백 제거 + Unicode casefold','trim_casefold')
        settings.addWidget(self.model);settings.addWidget(QLabel('답변 길이 한도(토큰)'));settings.addWidget(self.tokens);settings.addWidget(self.normalization);fields.addRow(settings)
        token_help=QLabel('토큰은 AI가 글을 처리하는 단위로, 글자 수와 같지 않습니다. 이 숫자로 답변 생성 길이의 한도를 정합니다.')
        token_help.setWordWrap(True);fields.addRow(token_help)
        self.prompt=QPlainTextEdit();self.prompt.setMaximumHeight(65);fields.addRow('AI에게 줄 지시문',self.prompt);top.addLayout(fields)
        self.cases=QTableWidget(0,4);self.cases.setHorizontalHeaderLabels(['질문 구분 이름','선택 입력','자동 검사 조건(고급·JSON)','사람이 판단할 기준']);self.cases.setColumnWidth(0,110);self.cases.setColumnWidth(1,280);self.cases.setColumnWidth(2,480);self.cases.setColumnWidth(3,250);top.addWidget(self.cases,1)
        row=QHBoxLayout()
        for label,method in (('사례 추가',self.add_case),('선택 사례 삭제',self.remove_case),('새 버전 저장',self.save_version),('기준 형식 안내',self.criteria_help)):
            button=QPushButton(label);button.clicked.connect(method);row.addWidget(button)
        top.addLayout(row)
        lower=QWidget();bottom=QVBoxLayout(lower);split.addWidget(lower)
        row=QHBoxLayout();self.key=QLineEdit();self.key.setEchoMode(QLineEdit.EchoMode.Password);self.key.setPlaceholderText('별도 OpenAI API 키 · 메모리만 사용');row.addWidget(self.key)
        self.preview_button=QPushButton('전송 미리보기');self.preview_button.clicked.connect(self.preview_requests);row.addWidget(self.preview_button)
        self.run_button=QPushButton('저장 버전 평가 실행');self.run_button.clicked.connect(self.start_run);row.addWidget(self.run_button)
        self.cancel_button=QPushButton('실행 취소');self.cancel_button.clicked.connect(self.cancel);row.addWidget(self.cancel_button);bottom.addLayout(row)
        key_help=QLabel('API 키는 OpenAI 서비스에서 발급받는 별도 연결용 비밀키입니다. 웹사이트 비밀번호나 서버 연결 키와 다릅니다. 직접 기록만 할 때는 필요 없습니다.')
        key_help.setWordWrap(True);bottom.addWidget(key_help)
        row=QHBoxLayout();self.history=QComboBox();self.history.currentIndexChanged.connect(self.select_run);row.addWidget(self.history,1)
        self.baseline=QComboBox();row.addWidget(self.baseline,1);button=QPushButton('기준 실행과 비교');button.clicked.connect(self.compare);row.addWidget(button)
        button=QPushButton('실행·판단 내보내기');button.clicked.connect(self.export_dialog);row.addWidget(button);bottom.addLayout(row)
        self.stats=QLabel('평가 미실행');self.stats.setWordWrap(True);bottom.addWidget(self.stats)
        self.results=QTableWidget(0,5);self.results.setHorizontalHeaderLabels(['사례','생성 상태','자동 기준','사람 판단','응답 모델']);self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for i,width in enumerate((150,150,150,150,370)):self.results.setColumnWidth(i,width)
        self.results.itemSelectionChanged.connect(self.show_case);bottom.addWidget(self.results,1)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(140);bottom.addWidget(self.details)
        row=QHBoxLayout();self.verdict=QComboBox();self.verdict.addItem('사람 통과','passed');self.verdict.addItem('사람 실패','failed');row.addWidget(self.verdict)
        self.reason=QPlainTextEdit();self.reason.setPlaceholderText('선택한 질문의 판단 기준에 따른 근거 (필수)');self.reason.setMaximumHeight(60);row.addWidget(self.reason,1)
        button=QPushButton('사람 판단 이력 추가');button.clicked.connect(self.save_review);row.addWidget(button);bottom.addLayout(row)
        self.status_label=QLabel('키·모델 설정과 저장 버전을 확인하세요');self.status_label.setTextFormat(Qt.TextFormat.PlainText);self.status_label.setWordWrap(True);layout.addWidget(self.status_label)
        close=QPushButton('닫기');close.clicked.connect(self.close);layout.addWidget(close)
        for control in (self.dataset_id,self.version,self.title_edit,self.model):control.textChanged.connect(self.invalidate)
        self.prompt.textChanged.connect(self.invalidate);self.cases.itemChanged.connect(self.invalidate);self.tokens.valueChanged.connect(self.invalidate);self.normalization.currentIndexChanged.connect(self.invalidate)
        self.refresh_versions();self.refresh_runs();self.controls();split.setSizes([440,510])
        if recovery['recovered']:self.status_label.setText(f"소유자 없는 실행 {recovery['recovered']}개를 중단으로 회수했습니다. 남은 사례는 미실행입니다.")
        elif recovery['active_owner']:self.status_label.setText('다른 창의 살아 있는 평가를 보존합니다. 새 실행은 현재 평가 종료 후 가능합니다.')

    def invalidate(self,*_):self.preview_hash=None
    def controls(self):
        active=self.process is not None;self.form.setEnabled(not active);self.key.setEnabled(not active);self.run_button.setEnabled(not active);self.preview_button.setEnabled(not active);self.cancel_button.setEnabled(active)
    def message(self,exc):
        message='평가 저장/조회 실패 · '+type(exc).__name__ if isinstance(exc,sqlite3.Error) else str(exc)[:400]
        if self.key.text():message=message.replace(self.key.text(),'[REDACTED]')
        self.status_label.setText(message)
    def add_case(self):
        row=self.cases.rowCount();self.cases.insertRow(row)
        for col,value in enumerate(('', '', '[]','')):self.cases.setItem(row,col,QTableWidgetItem(value))
    def remove_case(self):
        if self.cases.currentRow()>=0:self.cases.removeRow(self.cases.currentRow());self.invalidate()
    def dataset(self):
        cases=[]
        for row in range(self.cases.rowCount()):
            values=[self.cases.item(row,col).text() if self.cases.item(row,col) else '' for col in range(4)]
            cases.append({'id':values[0],'input':values[1],'criteria':strict_json(values[2]),'rubric':values[3]})
        return validate_dataset({'format_version':1,'id':self.dataset_id.text(),'version':self.version.text(),'title':self.title_edit.text(),'requested_model':self.model.text(),
            'prompt':self.prompt.toPlainText(),'settings':{'max_output_tokens':self.tokens.value()},'normalization':self.normalization.currentData(),'cases':cases})
    def set_dataset(self,value):
        value=validate_dataset(value)
        for widget,key in ((self.dataset_id,'id'),(self.version,'version'),(self.title_edit,'title'),(self.model,'requested_model')):widget.setText(value[key])
        self.prompt.setPlainText(value['prompt']);self.tokens.setValue(value['settings']['max_output_tokens']);self.normalization.setCurrentIndex(self.normalization.findData(value['normalization']));self.cases.setRowCount(0)
        for case in value['cases']:
            self.add_case();row=self.cases.rowCount()-1
            for col,key in enumerate(('id','input','criteria','rubric')):self.cases.item(row,col).setText(json.dumps(case[key],ensure_ascii=False) if key=='criteria' else case[key])
        self.invalidate()
    @storage_guard
    def refresh_versions(self):
        self.versions.clear()
        for d in self.store.datasets():self.versions.addItem(d['id']+' / '+d['version'],d)
    def load_version(self):
        try:
            if self.versions.currentData():
                value=self.versions.currentData();self.set_dataset(self.store.load_dataset(value['id'],value['version']))
        except (ValueError,OSError,sqlite3.Error) as exc:self.message(exc)
    def save_version(self):
        try:self.store.save_dataset(self.dataset(),secret=self.key.text());self.refresh_versions();self.status_label.setText('데이터셋 버전 저장 · 기존 버전은 수정하지 않습니다')
        except (ValueError,TypeError,RecursionError,OSError,sqlite3.Error) as exc:self.message(exc)
    def text_window(self,title,value):
        self.text_dialog=QDialog(self);self.text_dialog.setWindowTitle(title);self.text_dialog.resize(1000,700);layout=QVBoxLayout(self.text_dialog)
        area=QPlainTextEdit();area.setReadOnly(True);area.setPlainText(value);layout.addWidget(area);close=QPushButton('닫기');close.clicked.connect(self.text_dialog.close);layout.addWidget(close);self.text_dialog.show()
    def criteria_help(self):
        self.text_window('명시적 기준 형식','자동 기준은 JSON 배열입니다. 각 항목은 id, kind, expected 세 필드입니다.\nkind: exact / includes / excludes / json_contract\n문자열 기준 expected는 비어 있지 않은 문자열입니다.\nJSON 계약은 type: string/integer/number/boolean/null 또는 array(type,items), object(type,properties,required,additionalProperties)입니다. bool과 int는 별개입니다.\n문자열 정규화는 선택한 정책만 적용하며 JSON 값에는 적용하지 않습니다. 자동 기준 []는 사람 rubric이 있을 때만 허용합니다.')
    def preview_requests(self):
        if self.process is not None:return
        try:
            data=self.dataset();ensure_no_key(data,self.key.text());requests=[request_for(data,c) for c in data['cases']]
            self.text_window('전송 내용 · '+('로컬 HTTP 검증 대역' if self.test_endpoint else 'OpenAI Responses API'),json.dumps(requests,ensure_ascii=False,indent=2))
            self.preview_hash=digest(data);self.status_label.setText('선택한 입력/프롬프트만 전송 · 기준/rubric·HAR·기존 실행은 전송하지 않습니다')
        except (ValueError,TypeError,RecursionError) as exc:self.message(exc)
    def start_run(self):
        if self.process is not None:return
        try:
            data=self.dataset();client=OpenAIAdapter(self.key.text(),test_endpoint=self.test_endpoint);ensure_no_key(data,self.key.text())
            if digest(data)!=self.preview_hash:raise ValueError('현재 묶음의 전송 미리보기를 먼저 확인하세요')
            if self.store.load_dataset(data['id'],data['version'])!=data:raise ValueError('현재 설정을 새 버전으로 저장하세요')
            # Verify maximal wire wrapping before creating a persistent run.
            encode_request(str(self.viewer.root),data,self.key.text(),'0'*32,self.test_endpoint)
            self.job=OwnedJob();state=self.store.create_run(data,client.provider,secret=self.key.text());self.run_id=state['run_id']
            payload=encode_request(str(self.viewer.root),data,self.key.text(),self.run_id,self.test_endpoint)
        except (ValueError,OSError,sqlite3.Error) as exc:
            if self.job:self.job.close();self.job=None
            self.message(exc);return
        self.buffer=b'';self.case_serial=0;process=QProcess(self);self.process=process;command=python_command('signup031.evaluation_worker');process.setProgram(command[0]);process.setArguments(command[1:])
        env=QProcessEnvironment()
        for key,value in worker_environment().items():env.insert(key,value)
        process.setProcessEnvironment(env)
        def started():
            try:self.job.attach(process.processId());process.write(payload);process.closeWriteChannel()
            except OSError:self.cancel(interrupted=True)
        process.started.connect(started);process.readyReadStandardOutput.connect(lambda:self.read_output(process))
        process.finished.connect(lambda code,status:self.finished_run(process,code))
        process.errorOccurred.connect(lambda error:self.finished_run(process,-1) if error==QProcess.ProcessError.FailedToStart else None)
        self.controls();self.refresh_runs(self.run_id);self.status_label.setText('순차 평가 중 · 생성 오류는 별도 표시 후 다음 사례를 계속합니다');process.start()
        QTimer.singleShot(60000*len(data['cases'])+30000,lambda:self.cancel(interrupted=True) if self.process is process else None)
    def read_output(self,process):
        if self.process is not process:return
        self.buffer+=bytes(process.readAllStandardOutput())
        if len(self.buffer)>65536:self.cancel(interrupted=True);return
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                event=json.loads(line)
                if event.get('run_id')!=self.run_id:continue
                self.case_serial+=1;serial=self.case_serial
                if event['status']=='running':QTimer.singleShot(60000,lambda serial=serial:self.cancel(interrupted=True) if self.process is process and self.case_serial==serial else None)
                self.refresh_runs(self.run_id)
            except (ValueError,KeyError,TypeError):self.cancel(interrupted=True)
    def finished_run(self,process,code):
        if self.process is not process:return
        self.read_output(process);self.process=None
        if self.job:self.job.close();self.job=None
        try:
            state=self.store.load_run(self.run_id)
            if state['status'] in ('queued','running'):self.store.cancel(self.run_id,interrupted=True)
            self.refresh_runs(self.run_id);self.status_label.setText('평가 처리 종료 · '+RUN.get(self.current_run['status'],self.current_run['status'])+' · 자동/사람 판단을 각각 확인하세요')
        except (ValueError,OSError,sqlite3.Error):self.status_label.setText('평가 종료 후 저장/조회 실패 · 완료를 확정하지 못했습니다. 앱 재시작 후 고아 실행을 회수합니다.')
        finally:self.store.release_run();self.controls();process.deleteLater()
    def cancel(self,checked=False,*,interrupted=False):
        if self.process is None:return
        if self.job:self.job.close();self.job=None
        self.process.kill()
        try:
            self.store.cancel(self.run_id,interrupted=interrupted);self.status_label.setText('평가 중단' if interrupted else '평가 취소 · 미완료 응답은 저장하지 않습니다')
        except (ValueError,OSError,sqlite3.Error):self.status_label.setText('작업자 종료 · 중단 상태 저장 실패. 앱 재시작 후 고아 실행을 회수합니다.')
        finally:self.store.release_run()
    @storage_guard
    def refresh_runs(self,selected=None):
        previous=selected or self.history.currentData();baseline=self.baseline.currentData();self.history.blockSignals(True);self.history.clear();self.baseline.clear()
        for state in self.store.runs():
            label=state['dataset_id']+'/'+state['dataset_version']+' · '+state['run_id'][:8]+' · '+RUN.get(state['status'],state['status'])
            self.history.addItem(label,state['run_id']);self.baseline.addItem(label,state['run_id'])
        if previous:self.history.setCurrentIndex(max(0,self.history.findData(previous)))
        if baseline:self.baseline.setCurrentIndex(max(0,self.baseline.findData(baseline)))
        self.history.blockSignals(False);self.select_run()
    @storage_guard
    def select_run(self,*_):
        rid=self.history.currentData()
        if not rid:return
        self.current_run=self.store.load_run(rid);reviews=self.store.reviews(rid);latest={r['case_id']:r for r in reviews};counts=summarize(self.current_run,reviews)
        def rate(count):return '미평가' if count['rate'] is None else f"{count['passed']}/{count['evaluated']} ({count['rate']:.0%})"
        g=counts['generation'];self.stats.setText(f"전체 {counts['total']} · 생성 완료 {g['completed']} / 생성 중 {g['running']} / 오류 {g['generation_error']} / 취소 {g['cancelled']} / 중단 {g['interrupted']} / 미실행 {g['not_run']}\n자동 통과 {rate(counts['automatic'])} · 미평가 {counts['automatic']['not_evaluated']} / 기준 없음 {counts['automatic']['not_configured']}\n사람 통과 {rate(counts['human'])} · 미검토 {counts['human_unreviewed']} / 검토불가 {counts['human_unavailable']} / rubric 없음 {counts['human_not_configured']} · AI 평가자 미실행 {counts['ai_judge_not_run']}")
        cases={c['id']:c for c in self.current_run['snapshot']['cases']}
        self.results.setRowCount(len(self.current_run['items']))
        for row,item in enumerate(self.current_run['items']):
            human=AUTO[latest[item['case_id']]['verdict']] if item['case_id'] in latest else 'rubric 없음' if not cases[item['case_id']]['rubric'].strip() else '검토불가' if item['generation']!='completed' else '미검토'
            values=(item['case_id'],GEN[item['generation']],AUTO[item['auto_status']],human,item['returned_model'] or '미수집')
            for col,value in enumerate(values):self.results.setItem(row,col,QTableWidgetItem(value))
        self.details.clear()
    @storage_guard
    def show_case(self):
        row=self.results.currentRow()
        if self.current_run and 0<=row<len(self.current_run['items']):
            item=self.current_run['items'][row];case=next(c for c in self.current_run['snapshot']['cases'] if c['id']==item['case_id'])
            self.details.setPlainText(json.dumps({'case':case,'result':item,'human_history':[r for r in self.store.reviews(self.current_run['run_id']) if r['case_id']==item['case_id']],'ai_judge':self.current_run['ai_judge']},ensure_ascii=False,indent=2))
    def save_review(self):
        try:
            row=self.results.currentRow()
            if not self.current_run or row<0:raise ValueError('판단할 사례를 선택하세요')
            self.store.review(self.current_run['run_id'],self.current_run['items'][row]['case_id'],self.verdict.currentData(),self.reason.toPlainText(),secret=self.key.text())
            self.select_run();self.results.selectRow(row);self.status_label.setText('사람 판단 이력 추가 · 원 응답과 자동 판정은 유지됩니다')
        except (ValueError,OSError,sqlite3.Error) as exc:self.message(exc)
    @storage_guard
    def compare(self,*_):
        if not self.current_run or not self.baseline.currentData():return
        rows=compare_runs(self.store.load_run(self.baseline.currentData()),self.current_run,self.store.reviews(self.baseline.currentData()),self.store.reviews(self.current_run['run_id']))
        changes={'added':'추가 사례','removed':'제거 사례','conditions_changed':'조건 변경','not_comparable':'비교 불가','response_changed':'응답 변화','unchanged':'응답 동일'}
        human={**AUTO,'unreviewed':'미검토','unavailable':'검토 불가','absent':'사례 없음'}
        summaries=[]
        for row in rows:
            summaries.append(row['case_id']+' · '+changes[row['change']]+' · 자동 '+AUTO.get(row['before_auto'],'사례 없음')+' → '+AUTO.get(row['after_auto'],'사례 없음')+
                ' · 사람 '+human[row['before_human']['status']]+' → '+human[row['after_human']['status']])
            if row['differences']:summaries.append('  변경 조건: '+', '.join(row['differences']))
            for label,key in (('이전 근거','before_human'),('이후 근거','after_human')):
                if row[key]['reason']:summaries.append('  '+label+': '+row[key]['reason'][:120])
        self.text_window('실행 비교 · 응답 변화는 품질 저하 단정이 아닙니다','\n'.join(summaries)+'\n\n세부 JSON (조건과 근거 전체)\n'+json.dumps(rows,ensure_ascii=False,indent=2))
    def export_to(self,path):
        if not self.current_run:raise ValueError('내보낼 실행을 선택하세요')
        self.store.export(self.current_run['run_id'],path,self.baseline.currentData())
    def export_dialog(self):
        path,_=QFileDialog.getSaveFileName(self,'평가 실행·판단 내보내기','','JSON (*.json)')
        if path:
            try:self.export_to(path);self.status_label.setText('스냅샷·응답·자동 판정·사람 이력·집계 내보내기 완료')
            except (ValueError,OSError,sqlite3.Error) as exc:self.message(exc)
    def reject(self):
        if self.process is not None:self.close();return
        self.key.clear();super().reject()
    def closeEvent(self,event):
        if self.process is not None:self.cancel();event.ignore();QTimer.singleShot(50,self.close);return
        self.key.clear();super().closeEvent(event)
