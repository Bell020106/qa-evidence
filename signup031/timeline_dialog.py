"""Bounded timeline filters and a separately owned offline response experiment."""
import json
from PySide6.QtCore import QProcess,QProcessEnvironment,QTimer,Qt
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QDialog,QDoubleSpinBox,QHBoxLayout,QLabel,
    QPlainTextEdit,QPushButton,QSpinBox,QTableWidget,QTableWidgetItem,QVBoxLayout)
from signup031.owned_job import OwnedJob,python_command,worker_environment
from signup031.timeline import CLOCK_NOTE,load_timeline

LABELS={'action':'동작','console':'콘솔','pageerror':'페이지 오류','request':'요청 시작','response':'응답 수신',
    'request_finished':'요청 완료','request_failed':'전송 실패','redirect':'리디렉션'}


class TimelineDialog(QDialog):
    def __init__(self,viewer,record):
        super().__init__(viewer);self.viewer=viewer;self.record=record;self.process=None;self.job=None;self.buffer=b''
        self.normal_ready=False;self.last_status=None;self.timeline=None;self.result_path=None
        self.setWindowTitle('동작 기록·오류 실험');self.setWindowModality(Qt.WindowModality.ApplicationModal);self.resize(1220,950)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'timeline',layout);note=QLabel(CLOCK_NOTE);note.setWordWrap(True);layout.addWidget(note)
        self.capture_label=QLabel();self.capture_label.setWordWrap(True);self.capture_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.capture_label)
        filters=QHBoxLayout();self.kind_filter=QComboBox();self.kind_filter.addItem('모든 종류',None)
        for key,label in LABELS.items():self.kind_filter.addItem(label,key)
        self.clock_filter=QComboBox();self.clock_filter.addItem('모든 원천 시계',None)
        self.order=QComboBox();self.order.addItems(['수신 순서','선택한 원천 시계 순서'])
        self.start_ms=QDoubleSpinBox();self.end_ms=QDoubleSpinBox()
        for spin in (self.start_ms,self.end_ms):spin.setRange(0,1e9);spin.setSuffix(' ms');spin.setDecimals(3)
        self.end_ms.setValue(1e9)
        for control in (QLabel('종류'),self.kind_filter,QLabel('수신 시간'),self.start_ms,self.end_ms,self.clock_filter,self.order):filters.addWidget(control)
        layout.addLayout(filters)
        self.table=QTableWidget(0,6);self.table.setHorizontalHeaderLabels(['순번','받은 상대 시각(ms)','종류','요청 ID','원천 시계 ID','원천 시각 / 단위'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for i,width in enumerate((60,150,120,180,390,200)):self.table.setColumnWidth(i,width)
        layout.addWidget(self.table,1);self.table.itemSelectionChanged.connect(self.show_detail)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(155);layout.addWidget(self.details)
        for combo in (self.kind_filter,self.clock_filter,self.order):combo.currentIndexChanged.connect(self.filter_events)
        for spin in (self.start_ms,self.end_ms):spin.valueChanged.connect(self.filter_events)
        hint=QLabel('별도 로컬 실험: 원래 관측의 정상 복원을 확인한 뒤, 기록된 GET/HEAD 응답 한 항목에만 적용합니다. 원본 HAR는 유지됩니다.');hint.setWordWrap(True);layout.addWidget(hint)
        setup=QHBoxLayout();self.open_button=QPushButton('원본 복원 · 별도 실험 열기');self.open_button.clicked.connect(self.start_experiment);setup.addWidget(self.open_button)
        self.stop_button=QPushButton('실험 종료');self.stop_button.clicked.connect(self.stop_experiment);setup.addWidget(self.stop_button);layout.addLayout(setup)
        rule=QHBoxLayout();self.responses=QComboBox();rule.addWidget(self.responses,1)
        self.mode=QComboBox();self.mode.addItem('HTTP 500','http500');self.mode.addItem('기록 응답 지연','delay');rule.addWidget(self.mode)
        self.delay=QSpinBox();self.delay.setRange(1,3000);self.delay.setValue(300);self.delay.setSuffix(' ms');rule.addWidget(self.delay)
        self.arm_button=QPushButton('선택 응답에 규칙 적용');self.arm_button.clicked.connect(self.arm);rule.addWidget(self.arm_button);layout.addLayout(rule)
        action=QHBoxLayout();self.clicks=QComboBox();action.addWidget(self.clicks)
        self.click_button=QPushButton('선택한 기록 클릭 반복');self.click_button.clicked.connect(self.repeat_click);action.addWidget(self.click_button)
        action.addWidget(QLabel('또는 열린 오프라인 브라우저에서 직접 조작하세요.'));layout.addLayout(action)
        self.status_label=QLabel('실험 미실행');self.status_label.setWordWrap(True);self.status_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status_label)
        close=QPushButton('닫기');close.clicked.connect(self.close);layout.addWidget(close)
        try:
            self.timeline=load_timeline(record.source_path)
            if self.timeline is None:
                payload=json.loads(record.source_path.read_bytes());failure=payload.get('timeline_capture')
                self.capture_label.setText('타임라인 수집 실패 · '+failure['reason'] if failure else '타임라인 미수집 · 과거 자료의 시각을 생성하지 않습니다')
            else:
                availability=' · '.join(k+(': 수집' if v=='collected' else ': 미수집') for k,v in self.timeline['availability'].items())
                self.capture_label.setText(availability+f" · 누락 {self.timeline['dropped']} · "+'; '.join(self.timeline['limits']))
                for clock in sorted({e['source_clock'] for e in self.timeline['events'] if e['source_clock']}):self.clock_filter.addItem(clock,clock)
                self.filter_events()
        except (OSError,ValueError,KeyError,TypeError) as exc:self.capture_label.setText('타임라인 읽기 실패 · '+str(exc))
        self.controls()

    def filter_events(self):
        events=(self.timeline or {}).get('events',[]);kind=self.kind_filter.currentData();clock=self.clock_filter.currentData()
        rows=[e for e in events if (kind is None or e['kind']==kind) and (clock is None or e['source_clock']==clock) and self.start_ms.value()<=e['received_ms']<=self.end_ms.value()]
        if self.order.currentIndex()==1 and clock is not None:rows.sort(key=lambda e:(e['source_time'] is None,e['source_time'] or 0,e['seq']))
        self.filtered=rows;self.table.setRowCount(len(rows))
        for i,event in enumerate(rows):
            values=[event['seq'],event['received_ms'],LABELS[event['kind']],event['request_id'] or '—',event['source_clock'] or '미수집',str(event['source_time'])+' '+str(event['source_unit']) if event['source_time'] is not None else '미수집']
            for col,value in enumerate(values):self.table.setItem(i,col,QTableWidgetItem(str(value)))
        self.details.clear()

    def show_detail(self):
        row=self.table.currentRow()
        if 0<=row<len(getattr(self,'filtered',[])):
            event=self.filtered[row];self.details.setPlainText('발생 시각: 미변환 · 수신 시각은 발생 시각이 아닙니다\n'+json.dumps(event,ensure_ascii=False,indent=2))

    def controls(self):
        active=self.process is not None;armed=bool(self.last_status and self.last_status['rule'])
        self.open_button.setEnabled(not active and self.record.archive_root is not None);self.stop_button.setEnabled(active)
        for control in (self.responses,self.mode,self.delay,self.arm_button):control.setEnabled(active and self.normal_ready and not armed)
        self.click_button.setEnabled(active and armed and self.clicks.count()>0)

    def send(self,command):
        if self.process:self.process.write((json.dumps(command)+'\n').encode())

    def start_experiment(self):
        if self.process is not None or self.record.archive_root is None:return
        try:self.job=OwnedJob()
        except OSError as exc:self.status_label.setText('프로세스 보호 실패 · '+str(exc));return
        self.normal_ready=False;self.last_status=None;self.result_path=None;self.responses.clear();self.clicks.clear();self.buffer=b''
        process=QProcess(self);self.process=process;command=python_command('signup031.experiment_worker');process.setProgram(command[0]);process.setArguments(command[1:])
        env=QProcessEnvironment()
        for key,value in worker_environment().items():env.insert(key,value)
        process.setProcessEnvironment(env);process.readyReadStandardOutput.connect(self.read_output);process.finished.connect(self._finished)
        process.start();self.status_label.setText('원본 복원 확인 중 · 아직 실험 규칙을 적용하지 않았습니다');self.controls()
        try:
            if not process.waitForStarted(1500):raise OSError('실험 작업자 시작 실패')
            self.job.attach(process.processId());self.send({'archive':str(self.record.archive_root),'root':str(self.viewer.root),'headless':self.viewer.replay_headless})
        except OSError as exc:
            self.status_label.setText('실험 시작 실패 · '+str(exc));process.kill()
            if process.state()==QProcess.ProcessState.NotRunning:self._finished(2,None)

    def read_output(self):
        if self.process is None:return
        self.buffer+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                event=json.loads(line);status=event['status']
                if status=='normal_ready':
                    self.normal_ready=True
                    for entry in event['entries']:self.responses.addItem(f"{entry['index']} · {entry['method']} {entry['display_url']} · HTTP {entry['status']}"+(' · 모호함: 다른 응답 중복' if entry['ambiguous'] else '')+(' · 선택 불가' if entry['unavailable_reason'] else ''),entry)
                    for i in range(event['click_count']):self.clicks.addItem(f'기록 클릭 {i+1}',i)
                    self.status_label.setText('원본 정상 복원 확인 · 별도 실험 규칙을 선택하세요')
                elif status=='experiment':
                    self.last_status=event['result'];r=self.last_status
                    self.status_label.setText(f"원본 복원: 정상 · 별도 실험 {'적용 중' if r['rule'] else '미적용'} · 응답 적용 {r['applied']} · 지연 대기 {r['pending']} · 상한 거절 {r['rejected']}")
                elif status=='closed':self.result_path=event['result_path'];self.status_label.setText('실험 종료 · 다음 일반 재현에는 규칙이 남지 않습니다')
                else:self.status_label.setText('실험 오류 · '+event.get('reason',status))
                self.controls()
            except (ValueError,KeyError,TypeError):self.status_label.setText('실험 작업자 응답 형식 오류')

    def arm(self):
        entry=self.responses.currentData()
        if entry:self.send({'action':'arm','index':entry['index'],'mode':self.mode.currentData(),'delay_ms':self.delay.value() if self.mode.currentData()=='delay' else 0})

    def repeat_click(self):self.send({'action':'click','index':self.clicks.currentData()})

    def stop_experiment(self):
        if self.process is None:return
        self.send({'action':'stop'});process=self.process
        def force():
            if self.process is process:
                if self.job:self.job.close();self.job=None
                process.kill();self.status_label.setText('실험 강제 종료 · 완료 관측은 확인하지 못했습니다')
        QTimer.singleShot(3000,force)

    def _finished(self,code,_status):
        if self.process is None:return
        self.read_output();process,self.process=self.process,None
        if self.job:self.job.close();self.job=None
        if code!=0:self.status_label.setText('실험 실패/중단 · 종료 코드 '+str(code))
        self.controls();process.deleteLater()

    def reject(self):
        if self.process is not None:self.close();return
        super().reject()

    def closeEvent(self,event):
        if self.process is not None:self.stop_experiment();event.ignore();QTimer.singleShot(100,self.close);return
        super().closeEvent(event)
