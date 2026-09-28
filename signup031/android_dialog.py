"""Limited Android scenario editor and owned ADB worker controls."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import Qt,QProcess,QProcessEnvironment,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLineEdit,QLabel,QPushButton,QComboBox,QTableWidget,QTableWidgetItem,QFileDialog,QWidget
from signup031.android_adapter import validate_scenario,_atomic_json
from signup031.owned_job import OwnedJob,python_command,worker_environment


def finish_interrupted(path,status,message):
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload['result']['status']=='running':
        payload['result'].update(status=status,message=message)
        for step in payload['steps']:
            if step['status']=='running':step['status']='error' if status=='execution_error' else status
        payload['finished_at']=datetime.now(timezone.utc).isoformat()
        _atomic_json(path,payload)


class AndroidDialog(QDialog):
    def __init__(self,viewer):
        super().__init__(viewer);self.viewer=viewer;self.process=None;self.job=None;self.output=None;self.buffer=b'';self.cancel_reason=None
        self.setWindowTitle('Android 앱 테스트 · 선택 기기와 앱');self.resize(1000,800);self.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'android',layout)
        note=QLabel('ADB server를 별도로 시작하고 USB 디버깅을 승인하세요. 앱 실행·요소 대기·ASCII 입력·탭과 명시적 기대값을 지원합니다. 기기 초기화/앱 제거/AVD 부팅은 수행하지 않습니다.');note.setWordWrap(True);layout.addWidget(note)
        self.form=QWidget();form_layout=QVBoxLayout(self.form);layout.addWidget(self.form)
        form=QFormLayout();form_layout.addLayout(form)
        sdk=Path(os.environ.get('LOCALAPPDATA',''))/'Android/Sdk/platform-tools/adb.exe'
        self.adb=QLineEdit(str(sdk) if sdk.is_file() else '');form.addRow('adb 실행 파일',self.adb)
        row=QHBoxLayout();self.devices=QComboBox();self.devices.addItem('기기 목록을 조회하세요',None);row.addWidget(self.devices,1)
        self.refresh_button=QPushButton('기기 조회');self.refresh_button.clicked.connect(self.refresh_devices);row.addWidget(self.refresh_button);form.addRow('선택 기기',row)
        self.identifier=QLineEdit();self.title_edit=QLineEdit();self.package=QLineEdit();self.activity=QLineEdit()
        for label,widget in [('테스트 ID',self.identifier),('제목',self.title_edit),('앱 식별자(package)',self.package),('시작 화면(activity)',self.activity)]:form.addRow(label,widget)
        self.steps=QTableWidget(0,4);self.steps.setHorizontalHeaderLabels(['동작 wait / fill / tap','locator content_desc / resource_id','요소 값','입력 값'])
        self.checks=QTableWidget(0,4);self.checks.setHorizontalHeaderLabels(['검증 text / visible / text_length','locator content_desc / resource_id','요소 값','기대값'])
        for title,table in [('실행 단계',self.steps),('검증 항목',self.checks)]:
            form_layout.addWidget(QLabel(title));form_layout.addWidget(table)
            table.horizontalHeader().setStretchLastSection(True)
            row=QHBoxLayout();add=QPushButton('행 추가');add.clicked.connect(lambda _,t=table:self.add_row(t));row.addWidget(add)
            remove=QPushButton('선택 행 삭제');remove.clicked.connect(lambda _,t=table:t.removeRow(t.currentRow()) if t.currentRow()>=0 else None);row.addWidget(remove);form_layout.addLayout(row)
        row=QHBoxLayout()
        for title,slot in [('TC 불러오기',self.load_config),('TC 저장',self.save_config)]:
            button=QPushButton(title);button.clicked.connect(slot);row.addWidget(button)
        form_layout.addLayout(row)
        row=QHBoxLayout();self.run_button=QPushButton('선택 기기에서 실행');self.run_button.clicked.connect(self.run_test);row.addWidget(self.run_button)
        self.cancel_button=QPushButton('취소');self.cancel_button.clicked.connect(self.cancel);self.cancel_button.setEnabled(False);row.addWidget(self.cancel_button);layout.addLayout(row)
        self.status_label=QLabel('입력은 최대 200자: 영문·숫자·공백·_.,@+- 지원. Unicode/percent/셸 문법은 실행 전에 거부합니다.');self.status_label.setWordWrap(True);self.status_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status_label)

    def add_row(self,table,values=('', '', '', '')):
        row=table.rowCount();table.insertRow(row)
        for col,value in enumerate(values):table.setItem(row,col,QTableWidgetItem(str(value)))

    def config(self):
        device=self.devices.currentData()
        if not device or device['state']!='device':raise ValueError('준비 완료 기기를 선택하세요')
        config={'android_scenario_version':1,'id':self.identifier.text(),'title':self.title_edit.text(),'serial':device['serial'],'package':self.package.text(),'activity':self.activity.text(),'steps':[],'checks':[]}
        for group,table in [('steps',self.steps),('checks',self.checks)]:
            for row in range(table.rowCount()):
                values=[table.item(row,col).text() if table.item(row,col) else '' for col in range(4)]
                item={('action' if group=='steps' else 'kind'):values[0],'locator':values[1],'target':values[2]}
                if group=='steps' and values[0]=='fill':item['value']=values[3]
                if group=='checks':
                    item['expected']=json.loads(values[3]) if values[0] in ('visible','text_length') else values[3]
                config[group].append(item)
        return validate_scenario(config)

    def set_config(self,config):
        config=validate_scenario(config)
        for field,widget in [('id',self.identifier),('title',self.title_edit),('package',self.package),('activity',self.activity)]:widget.setText(config[field])
        # Saved serial remains explicit; the worker rechecks its current state.
        self.devices.clear();self.devices.addItem(config['serial']+' · 실행 시 연결 재확인',{'serial':config['serial'],'state':'device'})
        for group,table in [('steps',self.steps),('checks',self.checks)]:
            table.setRowCount(0)
            for item in config[group]:
                value=item.get('value',item.get('expected',''))
                if type(value) is bool:value=json.dumps(value)
                self.add_row(table,(item.get('action',item.get('kind')),item['locator'],item['target'],value))

    def load_config(self):
        filename,_=QFileDialog.getOpenFileName(self,'Android 앱 테스트 불러오기','','JSON (*.json)')
        if filename:
            try:
                path=Path(filename)
                if path.stat().st_size>128*1024:raise ValueError('TC 파일 상한 초과')
                self.set_config(json.loads(path.read_text(encoding='utf-8')))
            except (OSError,ValueError) as exc:self.status_label.setText(str(exc))

    def save_config(self):
        try:config=self.config()
        except ValueError as exc:self.status_label.setText(str(exc));return
        filename,_=QFileDialog.getSaveFileName(self,'Android 앱 테스트 저장','','JSON (*.json)')
        if filename:
            try:_atomic_json(Path(filename),config)
            except OSError as exc:self.status_label.setText(str(exc))

    def refresh_devices(self):
        if self.process is None:self.start({'mode':'devices'})

    def run_test(self):
        if self.process is not None:return
        try:config=self.config()
        except ValueError as exc:self.status_label.setText(str(exc));return
        self.output=self.viewer.root/('android-'+uuid4().hex)
        self.start({'mode':'run','config':config,'output':str(self.output)})

    def start(self,request):
        try:
            if not Path(self.adb.text()).is_file():raise ValueError('adb 실행 파일을 확인하세요')
            self.job=OwnedJob()
        except (ValueError,OSError) as exc:self.status_label.setText(str(exc));return
        self.mode=request['mode'];self.cancel_reason=None;self.stop_status='cancelled';self.buffer=b'';request['adb']=self.adb.text()
        process=QProcess(self);self.process=process;command=python_command('signup031.android_worker')
        process.setProgram(command[0]);process.setArguments(command[1:]);environment=QProcessEnvironment()
        for key,value in worker_environment().items():environment.insert(key,value)
        process.setProcessEnvironment(environment)
        def started():
            try:self.job.attach(process.processId());process.write(json.dumps(request).encode()+b'\n');process.closeWriteChannel()
            except OSError:self.cancel('프로세스 소유권 설정 실패')
        process.started.connect(started);process.readyReadStandardOutput.connect(self.read_output)
        process.finished.connect(lambda code,status:self.finished(process,code))
        process.errorOccurred.connect(lambda error:self.finished(process,-1) if error==QProcess.ProcessError.FailedToStart else None)
        self.form.setEnabled(False);self.run_button.setEnabled(False);self.cancel_button.setEnabled(True)
        self.status_label.setText('Android '+('기기 조회 중' if self.mode=='devices' else '실행 중 · 180초 상한'))
        process.start();QTimer.singleShot(190000 if self.mode=='run' else 15000,lambda:self.cancel('Android 시간 초과','execution_error') if self.process is process else None)

    def read_output(self):
        if self.process is not None:
            self.buffer+=bytes(self.process.readAllStandardOutput())
            if len(self.buffer)>32768:self.cancel('Android 출력 상한 초과')

    def cancel(self,reason='Android 실행 취소',status='cancelled'):
        if self.process is None:return
        self.cancel_reason=reason if isinstance(reason,str) else 'Android 실행 취소'
        self.stop_status=status
        if self.job:self.job.close();self.job=None
        self.process.kill()

    def finished(self,process,code):
        if self.process is not process:return
        self.read_output();self.process=None
        if self.job:self.job.close();self.job=None
        try:
            response=json.loads(self.buffer) if self.buffer else {}
            if self.mode=='run' and self.output is not None:
                path=self.output/'evidence.json'
                if path.is_file():
                    finish_interrupted(path,self.stop_status if self.cancel_reason else 'interrupted',self.cancel_reason or 'Android worker interrupted')
                    self.viewer._scenario_recorded(path)
            if self.cancel_reason:self.status_label.setText(self.cancel_reason)
            elif code or 'error' in response:self.status_label.setText(response.get('error','Android 실행 프로세스 오류'))
            elif self.mode=='devices':
                self.devices.clear()
                for device in response['devices']:self.devices.addItem(f"{device['serial']} · {device['state']} · {device['model']}",device)
                self.status_label.setText(f"연결 기기 {self.devices.count()}대 · device 상태의 기기를 선택하세요")
            else:self.status_label.setText('Android 결과: '+response.get('status','미완료'))
        except (OSError,ValueError,KeyError) as exc:self.status_label.setText('Android 결과 처리 오류: '+str(exc))
        process.deleteLater();self.form.setEnabled(True);self.run_button.setEnabled(True);self.cancel_button.setEnabled(False)

    def reject(self):
        if self.process is not None:self.close()
        else:super().reject()
    def closeEvent(self,event):
        if self.process is not None:self.cancel();event.ignore();QTimer.singleShot(50,self.close);return
        super().closeEvent(event)
