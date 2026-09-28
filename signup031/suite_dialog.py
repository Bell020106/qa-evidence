"""Desktop suite authoring, sequential execution and explicit baseline comparison."""
import json
from pathlib import Path

from PySide6.QtCore import QProcess,QProcessEnvironment,QTimer,Qt
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QDialog,QFileDialog,QHBoxLayout,QLabel,
    QLineEdit,QListWidget,QPlainTextEdit,QPushButton,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)
from signup031.owned_job import OwnedJob,python_command,worker_environment
from signup031.regression_suite import SuiteStore,compare_runs,freeze_suite,load_definition,save_definition

STATUS_LABELS={'passed':'통과','failed':'실패','preparation_failed':'실행 오류','cancelled':'취소','not_run':'미실행',
    'interrupted':'중단','skipped':'건너뜀','running':'실행 중','completed':'완료','unknown':'미확인'}
CHANGE_LABELS={'recovered':'회복','regressed':'회귀 실패','criteria_changed':'기준 변경','not_comparable':'비교 불가',
    'added':'추가','removed':'제거','unchanged':'변화 없음'}


def status_label(value):return STATUS_LABELS.get(value,value or '없음')


class SuiteDialog(QDialog):
    def __init__(self,viewer):
        super().__init__(viewer);self.viewer=viewer;self.store=SuiteStore(viewer.root)
        self.process=None;self.job=None;self.current_state=None;self.active_id=None;self.buffer=b'';self.worker_error=''
        self.setWindowTitle('여러 테스트 실행·비교');self.setWindowModality(Qt.WindowModality.ApplicationModal);self.resize(1180,900)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'suite',layout);self.edit_area=QWidget();edit=QVBoxLayout(self.edit_area)
        self.name_edit=QLineEdit();self.name_edit.setPlaceholderText('묶음 이름');edit.addWidget(self.name_edit)
        hint=QLabel('명시적으로 작성한 웹 TC JSON만 실행합니다. 수동 TC 원문에는 자동 실행 설정이 없습니다.\n실행 전에 모든 설정을 검증하고 당시 기준을 보존합니다. 최대 100개 · TC당 120초 · 묶음 60분.');hint.setWordWrap(True);edit.addWidget(hint)
        self.paths=QListWidget();self.paths.setMaximumHeight(140);edit.addWidget(self.paths)
        row=QHBoxLayout()
        for text,slot in [('테스트 설정 추가',self.add_files),('선택 삭제',self.remove_selected),('위로',lambda:self.move_selected(-1)),('아래로',lambda:self.move_selected(1)),('묶음 저장',self.save_dialog),('묶음 불러오기',self.load_dialog)]:
            button=QPushButton(text);button.clicked.connect(slot);row.addWidget(button)
        edit.addLayout(row);layout.addWidget(self.edit_area)
        history=QHBoxLayout();history.addWidget(QLabel('보존한 묶음 실행'));self.history=QComboBox();history.addWidget(self.history,1)
        self.refresh_button=QPushButton('실행 기록 새로고침');self.refresh_button.clicked.connect(self.refresh_history);history.addWidget(self.refresh_button);layout.addLayout(history)
        self.history.currentIndexChanged.connect(self.select_history)
        self.summary=QLabel('저장된 실행 없음');self.summary.setWordWrap(True);self.summary.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.summary)
        self.results=QTableWidget(0,6);self.results.setHorizontalHeaderLabels(['순서','TC ID / 제목','판정','개별 실행 ID','정리','사유'])
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setColumnWidth(1,240);self.results.setColumnWidth(3,300);self.results.setColumnWidth(5,300);layout.addWidget(self.results,1)
        baseline=QHBoxLayout();baseline.addWidget(QLabel('비교할 이전 실행'));self.baseline=QComboBox();baseline.addWidget(self.baseline,1)
        self.compare_button=QPushButton('선택 기준과 비교');self.compare_button.clicked.connect(lambda:self.compare_with(self.baseline.currentData()));baseline.addWidget(self.compare_button)
        self.detail_button=QPushButton('선택 항목의 기록 보기');self.detail_button.clicked.connect(self.open_detail);baseline.addWidget(self.detail_button);layout.addLayout(baseline)
        self.comparison=QPlainTextEdit();self.comparison.setReadOnly(True);self.comparison.setMaximumHeight(130);layout.addWidget(self.comparison)
        self.status_label=QLabel('웹 TC 파일을 추가하거나 저장된 묶음을 불러오세요.');self.status_label.setWordWrap(True);self.status_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status_label)
        buttons=QHBoxLayout();self.run_button=QPushButton('묶음 순차 실행');self.run_button.clicked.connect(self.start_run);buttons.addWidget(self.run_button)
        self.cancel_button=QPushButton('현재 묶음 취소');self.cancel_button.clicked.connect(self.cancel_run);buttons.addWidget(self.cancel_button)
        close=QPushButton('닫기');close.clicked.connect(self.close);buttons.addWidget(close);layout.addLayout(buttons)
        self.refresh_history();self._enabled(True)

    def set_paths(self,paths):
        self.paths.clear();self.paths.addItems([str(Path(p).resolve()) for p in paths])

    def source_paths(self):return [self.paths.item(i).text() for i in range(self.paths.count())]

    def add_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'웹 TC 설정 추가','','Web TC (*.json)')
        if paths:self.set_paths(self.source_paths()+paths)

    def remove_selected(self):
        if self.paths.currentRow()>=0:self.paths.takeItem(self.paths.currentRow())

    def move_selected(self,offset):
        index=self.paths.currentRow();target=index+offset
        if index>=0 and 0<=target<self.paths.count():self.paths.insertItem(target,self.paths.takeItem(index));self.paths.setCurrentRow(target)

    def save_to(self,path):
        save_definition(path,self.name_edit.text(),self.source_paths());self.status_label.setText('묶음 정의 저장 완료 · '+str(path))

    def load_from(self,path):
        definition=load_definition(path);self.name_edit.setText(definition['name']);self.set_paths(definition['paths']);self.status_label.setText('묶음 정의 불러오기 완료')

    def save_dialog(self):
        path,_=QFileDialog.getSaveFileName(self,'묶음 저장','','TC suite (*.json)')
        if path:
            try:self.save_to(path)
            except (OSError,ValueError,TypeError) as exc:self.status_label.setText('저장 실패 · '+str(exc))

    def load_dialog(self):
        path,_=QFileDialog.getOpenFileName(self,'묶음 불러오기','','TC suite (*.json)')
        if path:
            try:self.load_from(path)
            except (OSError,ValueError,TypeError) as exc:self.status_label.setText('불러오기 실패 · '+str(exc))

    def _enabled(self,enabled):
        for control in (self.edit_area,self.run_button,self.history,self.refresh_button,self.baseline,self.compare_button,self.detail_button):control.setEnabled(enabled)
        self.cancel_button.setEnabled(not enabled)

    def refresh_history(self):
        if self.process is not None:return
        try:
            try:self.store.recover()
            except ValueError:pass # Another worker owns the lease; its state is read-only here.
            selected=self.current_state['suite_id'] if self.current_state else None
            runs=self.store.list_runs();self.history.blockSignals(True);self.history.clear();self.baseline.clear()
            for run in runs:
                caption=f"{run['name']} · {run['started_at']} · {status_label(run['status'])} · {run['suite_id'][:8]}"
                self.history.addItem(caption,run['suite_id']);self.baseline.addItem(caption,run['suite_id'])
            self.history.setCurrentIndex(max(0,self.history.findData(selected)));self.history.blockSignals(False)
            self.select_history()
        except (OSError,ValueError,KeyError,TypeError) as exc:self.status_label.setText('실행 기록 오류 · '+str(exc))

    def select_history(self):
        suite_id=self.history.currentData()
        if suite_id is None:return
        try:self.current_state=self.store.read(suite_id);self.render_state();self.comparison.clear()
        except (OSError,ValueError,KeyError,TypeError) as exc:self.status_label.setText('기록 읽기 실패 · '+str(exc))

    def render_state(self):
        state=self.current_state;counts=state['summary']
        self.summary.setText(f"묶음 {state['suite_id']} · {status_label(state['status'])}\n전체 {counts['total']} · 통과 {counts['passed']} · 실패 {counts['failed']} · 실행 오류 {counts['preparation_failed']} · 취소 {counts['cancelled']} · 중단 {counts['interrupted']} · 미실행 {counts['not_run']} · 건너뜀 {counts['skipped']} · 진행 {counts['running']} · 통과율 {counts['passed']}/{counts['total']} · 정리 오류 {len(state['cleanup_errors'])}")
        self.results.setRowCount(len(state['items']))
        for row,item in enumerate(state['items']):
            values=[item['position'],item['tc_id']+' / '+item['config']['title'],status_label(item['status']),item['execution_id'] or '아직 없음',status_label((item['cleanup'] or {}).get('status','unknown')),item['reason']]
            for col,value in enumerate(values):self.results.setItem(row,col,QTableWidgetItem(str(value)))

    def start_run(self):
        if self.process is not None:return
        try:
            snapshot=freeze_suite(self.name_edit.text(),self.source_paths());self.job=OwnedJob()
        except (OSError,ValueError,TypeError) as exc:self.status_label.setText('실행 전 검증 실패 · '+str(exc));return
        self.current_state=None;self.active_id=None;self.buffer=b'';self.worker_error='';self.cancel_requested=False;self._enabled(False)
        process=QProcess(self);self.process=process;command=python_command('signup031.suite_worker');process.setProgram(command[0]);process.setArguments(command[1:])
        env=QProcessEnvironment()
        for key,value in worker_environment().items():env.insert(key,value)
        process.setProcessEnvironment(env);process.readyReadStandardOutput.connect(self._read);process.finished.connect(self._finished)
        process.start()
        try:
            if not process.waitForStarted(1500):raise OSError('묶음 작업자를 시작할 수 없습니다')
            self.job.attach(process.processId())
            process.write(json.dumps({'root':str(self.store.root),'snapshot':snapshot},ensure_ascii=False).encode());process.closeWriteChannel()
            self.status_label.setText('묶음 실행 중 · 취소해도 완료한 원본 증거는 유지됩니다')
        except OSError as exc:
            self.worker_error='프로세스 보호/시작 실패 · '+str(exc);process.kill()
            if process.state()==QProcess.ProcessState.NotRunning:self._finished(2,None)

    def _read(self):
        if self.process is None:return
        self.buffer+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                event=json.loads(line)
                if event.get('suite_id'):
                    self.active_id=event['suite_id'];self.current_state=self.store.read(self.active_id);self.render_state()
                    if self.cancel_requested:self._request_cancel()
                if event.get('reason'):self.worker_error=str(event['reason'])
            except (OSError,ValueError,KeyError,TypeError) as exc:self.worker_error='상태 읽기 오류 · '+str(exc)

    def _request_cancel(self):
        if self.active_id:(self.store.path(self.active_id)/'cancel.request').touch(exist_ok=True)

    def cancel_run(self):
        if self.process is None:return
        self.cancel_requested=True;self.status_label.setText('취소 요청 · 소유 작업자/브라우저 정리 중')
        try:self._request_cancel()
        except OSError as exc:self.worker_error='취소 요청 저장 실패 · '+str(exc)
        process=self.process
        def force_close():
            if self.process is process:
                self.worker_error='취소 정리 시간 상한 · 강제 종료 후 중단 기록을 확인하세요'
                if self.job:self.job.close();self.job=None
                process.kill()
        QTimer.singleShot(7000,force_close)

    def _finished(self,code,_status):
        if self.process is None:return
        self._read();process,self.process=self.process,None
        if self.job:
            try:self.job.close()
            except OSError as exc:self.worker_error='프로세스 정리 오류 · '+str(exc)
            self.job=None
        attempt_id=self.active_id;self._enabled(True);self.refresh_history()
        attempt=None
        if attempt_id:
            try:attempt=self.store.read(attempt_id)
            except (OSError,ValueError):pass
        if self.worker_error:self.status_label.setText('실행 실패 · '+self.worker_error)
        elif attempt and ((code==0 and attempt['status']=='completed') or (code==1 and attempt['status']=='cancelled')):
            self.status_label.setText('묶음 실행 종료 · '+status_label(attempt['status']))
        else:self.status_label.setText(f'이번 묶음 실행 실패 · 종료 코드 {code} · '+(status_label(attempt['status']) if attempt else '이번 실행 결과 없음'))
        process.deleteLater()

    def compare_with(self,suite_id):
        if not suite_id or self.current_state is None:return
        try:
            if suite_id==self.current_state['suite_id']:raise ValueError('서로 다른 묶음 실행을 선택하세요')
            rows=compare_runs(self.store.read(suite_id),self.current_state)
            self.baseline.setCurrentIndex(self.baseline.findData(suite_id))
            self.comparison.setPlainText('동일 ID + 동일 URL/단계/기대 기준만 회복·회귀 비교\n'+ '\n'.join(f"{r['tc_id']}: {status_label(r['before'])} → {status_label(r['after'])} · {CHANGE_LABELS[r['change']]}" for r in rows))
        except (OSError,ValueError,KeyError,TypeError) as exc:self.comparison.setPlainText('비교 실패 · '+str(exc))

    def open_detail(self):
        row=self.results.currentRow()
        if self.current_state is None or not 0<=row<len(self.current_state['items']):return
        item=self.current_state['items'][row]
        if not item['evidence']:self.status_label.setText('완료한 개별 증거가 없습니다 · '+status_label(item['status']));return
        path=(self.store.root/item['evidence']).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or not path.is_file():self.status_label.setText('개별 증거 경로 오류');return
        self.viewer._scenario_recorded(str(path));self.hide()

    def reject(self):
        if self.process is not None:self.close();return
        super().reject()

    def closeEvent(self,event):
        if self.process is not None:
            if not self.cancel_requested:self.cancel_run()
            event.ignore();QTimer.singleShot(100,self.close);return
        super().closeEvent(event)
