"""Desktop forms for the constrained web scenario format, not a raw JSON editor."""
import json
from copy import deepcopy
from pathlib import Path
import sys
from urllib.parse import urlsplit
from signup031.scenario_inputs import TargetInput, ValueInput, limitation_summary

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QTableWidget, QVBoxLayout, QWidget)

from signup031.web_scenario import (ACTION_LABELS, CHECK_LABELS, LOCATOR_LABELS,
                                    load_scenario, save_scenario, validate_scenario)

CHECK_LABELS = {'input_length':'입력한 글자 수','text':'화면에 나오는 글자','visible':'화면에 보이는지'}
ACTION_LABELS = {**ACTION_LABELS,'wait':'대상 나타날 때까지 기다리기'}


class ScenarioEditor(QDialog):
    recorded = Signal(str)

    def __init__(self, artifacts_root, parent=None):
        super().__init__(parent)
        self.artifacts_root = Path(artifacts_root).resolve()
        self.process = None
        self.last_evidence = None
        self._stdout = b''
        self.targets = {}
        self.setWindowTitle('자동 테스트 만들기')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(1040, 820)
        layout = QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'scenario',layout)
        self.form_area = QWidget()
        fields = QVBoxLayout(self.form_area)
        heading = QLabel('자동 테스트 설정')
        heading.setStyleSheet('font-size:22px;font-weight:700')
        fields.addWidget(heading)
        description = QLabel('① 무엇을 할지 → ② 어디에서 할지 → ③ 어떤 결과가 나와야 할지 순서로 정하세요.')
        description.setWordWrap(True)
        fields.addWidget(description)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText('테스트 구분 이름 · 예: LOGIN-01')
        self.title_edit = QLineEdit()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('http://127.0.0.1:8000/test-page')
        form.addRow('테스트 ID', self.id_edit)
        form.addRow('제목', self.title_edit)
        form.addRow('시작할 사이트 주소', self.url_edit)
        fields.addLayout(form)
        self.source_label = QLabel()
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        fields.addWidget(self.source_label)
        self.advanced_check=QCheckBox('대상 코드 직접 수정(고급)');fields.addWidget(self.advanced_check)
        self.target_hint=QLabel('목록에는 이 설정에 들어 있는 대상만 보입니다. 원하는 대상이 없다면 직접 테스트·기록에서 해당 부분을 기록한 뒤 자동 테스트 초안을 만들거나 고급에서 지정하세요.')
        self.target_hint.setWordWrap(True);fields.addWidget(self.target_hint)
        self.advanced_hint=QLabel('CSS는 화면 요소를 찾는 코드입니다(예: #name). 정확한 라벨은 입력칸에 연결된 이름(label/aria-label)이며, 화면에 보이는 아무 글자나 입력하는 방식이 아닙니다.')
        self.advanced_hint.setWordWrap(True);self.advanced_hint.hide();fields.addWidget(self.advanced_hint)
        self.ai_notes_button=QPushButton('AI 검토 항목 전체 보기');self.ai_notes_button.clicked.connect(self.show_ai_notes);fields.addWidget(self.ai_notes_button)
        self.review_check=QCheckBox('QA가 URL·요소·기대 결과를 검토했습니다. 저장 후 직접 실행합니다.')
        fields.addWidget(self.review_check);self.review_check.hide()
        self.steps_table, self.add_step_button = self._table(fields, '실행 단계 (위에서 아래 순서)', ACTION_LABELS, '동작', '입력값 / 키', '단계 추가')
        self.checks_table, self.add_check_button = self._table(fields, '③ 확인할 결과 · 결과 추가 후 종류 → 대상 → 기대값을 고르세요', CHECK_LABELS, '무엇을 확인할지', '기대값', '결과 추가')
        self.advanced_check.toggled.connect(self._set_advanced)
        hint = QLabel('입력한 글자 수: 예 5 · 화면에 나오는 글자: 예 완료(정확히 비교)\n키 입력: 예 Enter, Backspace, Control+A · 클릭과 대기는 입력값이 필요 없습니다.')
        hint.setWordWrap(True)
        fields.addWidget(hint)
        self.details_toggle=QCheckBox('기록 제한·오류 원문 상세');fields.addWidget(self.details_toggle)
        self.details_text=QPlainTextEdit();self.details_text.setReadOnly(True);self.details_text.setMaximumHeight(100);self.details_text.hide();fields.addWidget(self.details_text)
        self.details_toggle.toggled.connect(self.details_text.setVisible)
        file_row = QHBoxLayout()
        for title, callback in [('새 테스트', self.clear_config), ('설정 불러오기', self._load_dialog), ('설정 저장', self._save_dialog)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            file_row.addWidget(button)
        fields.addLayout(file_row)
        caution = QLabel('이 테스트는 실제 사이트에서 실행됩니다. 클릭·입력이 사이트에 반영될 수 있습니다. 설정과 응답 내용은 이 컴퓨터에 저장됩니다.')
        caution.setWordWrap(True)
        fields.addWidget(caution)
        layout.addWidget(self.form_area, 1)
        self.status_label = QLabel('설정 후 기록 실행을 누르세요. 결과는 선택한 결과 폴더에 새 실행으로 저장됩니다.')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.run_button = QPushButton('기록 실행')
        self.run_button.clicked.connect(self.start_run)
        self.close_button = QPushButton('닫기')
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.run_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.clear_config()

    def _set_advanced(self, advanced):
        self._refresh_targets()
        self.advanced_hint.setVisible(advanced)
        for table in (self.steps_table,self.checks_table):
            table.setColumnHidden(1,not advanced)
            for row in range(table.rowCount()):table.cellWidget(row,2).set_advanced(advanced)

    def _refresh_targets(self):
        for table in (self.steps_table,self.checks_table):
            for row in range(table.rowCount()):
                cell=table.cellWidget(row,2)
                if cell and cell.text() and cell.pair() not in self.targets:
                    self.targets[cell.pair()]=f'직접 입력한 대상 {len(self.targets)+1}'
        for table in (self.steps_table,self.checks_table):
            for row in range(table.rowCount()):
                cell=table.cellWidget(row,2)
                if cell:cell.refresh()

    def _table(self, layout, heading, choices, first_caption, value_caption, add_caption):
        layout.addWidget(QLabel(heading))
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels([first_caption, '요소 지정', '대상', value_caption])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(155)
        table.setColumnHidden(1,not self.advanced_check.isChecked())
        layout.addWidget(table, 1)
        row = QHBoxLayout()
        add = QPushButton(add_caption)
        add.clicked.connect(lambda: self._add_row(table, choices))
        remove = QPushButton('선택 행 삭제')
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None)
        up = QPushButton('위로')
        down = QPushButton('아래로')
        up.clicked.connect(lambda: self._move_row(table, choices, -1))
        down.clicked.connect(lambda: self._move_row(table, choices, 1))
        for button in (add, remove, up, down):
            row.addWidget(button)
        layout.addLayout(row)
        return table, add

    def _add_row(self, table, choices, values=None):
        row = table.rowCount()
        table.insertRow(row)
        for col, options in ((0, choices), (1, LOCATOR_LABELS)):
            combo = QComboBox()
            for key, text in options.items():
                combo.addItem(text, key)
            table.setCellWidget(row, col, combo)
        kind=table.cellWidget(row,0)
        target=TargetInput(self,table.cellWidget(row,1));target.set_advanced(self.advanced_check.isChecked())
        table.setCellWidget(row,2,target)
        value=ValueInput(kind.currentData());table.setCellWidget(row,3,value)
        kind.currentIndexChanged.connect(lambda:value.set_kind(kind.currentData()))
        if values:
            for col in (0, 1):
                combo = table.cellWidget(row, col)
                combo.setCurrentIndex(combo.findData(values[col]))
            for col in (2, 3):
                table.cellWidget(row, col).setText(values[col])

    @staticmethod
    def _row_values(table, row):
        return [table.cellWidget(row, 0).currentData(), table.cellWidget(row, 1).currentData(),
                table.cellWidget(row, 2).text(), table.cellWidget(row, 3).text()]

    def _move_row(self, table, choices, offset):
        current = table.currentRow()
        target = current + offset
        if current < 0 or not 0 <= target < table.rowCount():
            return
        rows = [self._row_values(table, row) for row in range(table.rowCount())]
        caches = [deepcopy(table.cellWidget(row,3).saved) for row in range(table.rowCount())]
        rows[current], rows[target] = rows[target], rows[current]
        caches[current], caches[target] = caches[target], caches[current]
        table.setRowCount(0)
        for index,values in enumerate(rows):
            self._add_row(table, choices, values)
            table.cellWidget(index,3).saved=caches[index]
        table.setCurrentCell(target, 0)

    def clear_config(self):
        self.source_manual = None
        self.targets={}
        self.details_text.clear();self.details_toggle.setChecked(False)
        self.source_ai=None;self.saved_ai=None;self.review_check.setChecked(False);self.review_check.hide()
        self.ai_notes_button.hide()
        self.source_label.clear()
        for edit in (self.id_edit, self.title_edit, self.url_edit):
            edit.clear()
        for table, choices in ((self.steps_table, ACTION_LABELS), (self.checks_table, CHECK_LABELS)):
            table.setRowCount(0)
            self._add_row(table, choices)

    def config(self, *, for_save=False):
        for edit,name in ((self.id_edit,'테스트 ID'),(self.title_edit,'제목'),(self.url_edit,'사이트 주소')):
            if not edit.text().strip():raise ValueError(f'{name}를 입력하세요.')
        try:
            parsed=urlsplit(self.url_edit.text());valid=parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username and not parsed.password
            parsed.port
        except ValueError:valid=False
        if not valid:raise ValueError('사이트 주소를 http:// 또는 https://로 시작하는 올바른 주소로 입력하세요.')
        if not for_save and not self.steps_table.rowCount():raise ValueError('실행할 동작이 없습니다. 단계 추가를 눌러 동작과 대상을 정하세요.')
        if not for_save and not self.checks_table.rowCount():raise ValueError('확인할 결과가 아직 없습니다. 결과 추가를 눌러 종류, 대상, 기대값을 정하세요.')
        config = {'version': 1, 'id': self.id_edit.text(), 'title': self.title_edit.text(),
                  'url': self.url_edit.text(), 'steps': [], 'checks': []}
        if self.source_manual is not None:
            config['source_manual'] = deepcopy(self.source_manual)
        if self.source_ai is not None:
            config['source_ai']=deepcopy(self.source_ai);config['source_ai']['reviewed']=self.review_check.isChecked()
            if not self.review_check.isChecked():config['draft']=True
        for table, group in ((self.steps_table, 'steps'), (self.checks_table, 'checks')):
            for row in range(table.rowCount()):
                kind, locator, target, value = self._row_values(table, row)
                label='동작' if group=='steps' else '결과'
                if not target.strip():raise ValueError(f'{label} {row+1}번: 대상을 선택하세요. 목록에 없으면 직접 기록하거나 고급에서 지정하세요.')
                if kind=='press' and not value.strip():raise ValueError(f'동작 {row+1}번: 누를 키를 입력하세요. 예: Enter')
                if group == 'steps':
                    item = {'action': kind, 'locator': locator, 'target': target, 'value': value}
                else:
                    if kind == 'input_length':
                        try:
                            value = int(value)
                        except ValueError as exc:
                            raise ValueError(f'결과 {row + 1}번: 글자 수를 숫자로 입력하세요. 예: 5') from exc
                        if not 0<=value<=1000000:raise ValueError(f'결과 {row+1}번: 글자 수는 0~1000000 사이의 숫자로 입력하세요.')
                    elif kind == 'visible':
                        if value.lower() not in ('true', 'false'):
                            raise ValueError(f'검증 {row + 1}: 기대 표시 여부는 true/false로 입력하세요')
                        value = value.lower() == 'true'
                    item = {'kind': kind, 'locator': locator, 'target': target, 'expected': value}
                config[group].append(item)
        if for_save and (not config['steps'] or not config['checks']):
            config['draft'] = True
        try:return validate_scenario(config, allow_draft=for_save)
        except ValueError as exc:
            self.details_text.appendPlainText(str(exc))
            raise ValueError('설정에 사용할 수 없는 값이 있습니다. 입력 내용을 확인하세요. 원인은 오류 원문 상세에서 볼 수 있습니다.') from exc

    def set_config(self, config):
        config = validate_scenario(config, allow_draft=True)
        self.targets={}
        for group in ('steps','checks'):
            for index,item in enumerate(config[group],1):
                pair=(item['locator'],item['target'])
                if pair not in self.targets:self.targets[pair]=f"{'기록한' if config.get('source_manual') and group=='steps' else '설정의'} {index}번 {'동작' if group=='steps' else '결과'}의 대상"
        self.details_text.clear();self.details_toggle.setChecked(False)
        self.source_manual = deepcopy(config.get('source_manual'))
        self.source_ai=deepcopy(config.get('source_ai'));self.saved_ai=None
        self.ai_notes_button.setVisible(self.source_ai is not None)
        self.review_check.setVisible(self.source_ai is not None);self.review_check.setChecked(bool(self.source_ai and self.source_ai['reviewed']))
        if self.source_manual:
            reasons = self.source_manual['limitations']
            self.source_label.setText('원본 수동 기록 · ' + self.source_manual['execution_id'] +
                '\n기대 결과는 QA가 직접 작성합니다.' +
                ('\n기록 제한: ' + limitation_summary(reasons) if reasons else '') +
                ('\n동작 없음 · 단계를 직접 작성해야 실행할 수 있습니다.' if not config['steps'] else ''))
            self.details_text.setPlainText('\n'.join(reasons))
        else:
            self.source_label.clear()
        if self.source_ai:
            self.source_label.setText('AI 제안 · '+self.source_ai['provider']+' / 요청 '+self.source_ai['requested_model']+' / 응답 '+self.source_ai['returned_model']+' · '+self.source_ai['proposal_id']+
                '\n페이지 관측/테스트 결과가 아닙니다. QA가 요소와 기대값을 확인하세요.\n'+'; '.join(self.source_ai['review_notes'])[:400])
        self.id_edit.setText(config['id'])
        self.title_edit.setText(config['title'])
        self.url_edit.setText(config['url'])
        for table, group, choices in ((self.steps_table, 'steps', ACTION_LABELS), (self.checks_table, 'checks', CHECK_LABELS)):
            table.setRowCount(0)
            for item in config[group]:
                kind = item['action' if group == 'steps' else 'kind']
                value = item['value' if group == 'steps' else 'expected']
                value = str(value).lower() if type(value) is bool else str(value)
                self._add_row(table, choices, [kind, item['locator'], item['target'], value])

    def show_ai_notes(self):
        if not self.source_ai:return
        self.notes_dialog=QDialog(self);self.notes_dialog.setWindowTitle('AI 검토 항목 · 미관측 제안');self.notes_dialog.resize(800,450)
        layout=QVBoxLayout(self.notes_dialog);text=QPlainTextEdit();text.setReadOnly(True);text.setPlainText('\n\n'.join(self.source_ai['review_notes']));layout.addWidget(text)
        close=QPushButton('닫기');close.clicked.connect(self.notes_dialog.close);layout.addWidget(close);self.notes_dialog.show()

    def save_to(self, path):
        path = Path(path).resolve()
        # A TC setting is a new artifact; never write it inside an evidence run.
        if any((parent / 'evidence.json').is_file() for parent in path.parents):
            raise ValueError('원본 기록 폴더에 TC 설정을 저장할 수 없습니다. 별도 위치를 선택하세요')
        config=self.config(for_save=True);save_scenario(path, config)
        if self.source_ai:self.saved_ai=deepcopy(config)
        missing=[]
        if not config['steps']:missing.append('실행할 동작 추가')
        if not config['checks']:missing.append('확인할 결과 추가')
        if self.source_ai and not self.review_check.isChecked():missing.append('AI 초안 검토 확인 후 다시 저장')
        message='초안 저장됨 · '+', '.join(missing)+'가 필요합니다' if missing else '설정 저장 완료'
        if missing==['확인할 결과 추가']:message='초안 저장됨 · 확인할 결과를 추가하면 실행할 수 있습니다'
        self.status_label.setText(f'{message} · {path}')

    def load_from(self, path):
        self.set_config(load_scenario(path))
        if self.source_ai:self.saved_ai=self.config(for_save=True)
        self.status_label.setText(f'설정 불러오기 완료 · {path}')

    def _save_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, 'TC 설정 저장', '', 'TC settings (*.json)')
        if path:
            try:
                self.save_to(path)
            except Exception as exc:
                self.details_text.appendPlainText(str(exc))
                message=str(exc) if isinstance(exc,ValueError) else '저장 위치와 쓰기 권한을 확인하세요. 원인은 오류 원문 상세에서 볼 수 있습니다.'
                self.status_label.setText(f'저장 실패 · {message}')

    def _load_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, 'TC 설정 불러오기', '', 'TC settings (*.json)')
        if path:
            try:
                self.load_from(path)
            except Exception as exc:
                self.details_text.appendPlainText(str(exc))
                self.status_label.setText('불러오기 실패 · 이 앱에서 저장한 설정 파일인지, 파일 형식과 버전이 맞는지 확인하세요. 원인은 오류 원문 상세에서 볼 수 있습니다.')

    def start_run(self):
        if self.process is not None:
            return
        try:
            config = self.config()
            if self.source_ai and config!=self.saved_ai:raise ValueError('QA 검토 후 현재 설정을 저장해야 실행할 수 있습니다')
        except ValueError as exc:
            self.status_label.setText(f'설정 오류 · {exc}')
            return
        self.last_evidence = None
        self._stdout = b''
        from signup031.owned_job import create_worker
        process=create_worker(self,'signup031.web_runner',['--stdin-config','--artifacts-dir',str(self.artifacts_root)])
        if process is None:return
        self.process = process
        self.form_area.setEnabled(False)
        self.run_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status_label.setText('실행·기록 중 · 별도 프로세스에서 브라우저를 실행합니다')
        payload = json.dumps(config, ensure_ascii=True).encode('utf-8')
        def send_config():
            process.write(payload)
            process.closeWriteChannel()
        process.started.connect(send_config)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        process.start()

    def _read_output(self):
        if self.process is not None:
            self._stdout += bytes(self.process.readAllStandardOutput())

    def _error(self, error):
        if self.process is not None and error == QProcess.ProcessError.FailedToStart:
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _finished(self, code, _exit_status):
        if self.process is None:
            return
        self._read_output()
        process, self.process = self.process, None
        self.form_area.setEnabled(True)
        self.run_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.status_label.setText(f'실행 프로세스 실패 · 종료 코드 {code}')
        for line in self._stdout.splitlines():
            try:
                event = json.loads(line)
                if 'evidence' in event:
                    path = Path(event['evidence']).resolve()
                    if not path.is_relative_to(self.artifacts_root) or not path.is_file():
                        raise ValueError('결과 경로가 올바르지 않습니다')
                    self.last_evidence = path
                    status = {'passed': '통과', 'failed': '실패', 'preparation_failed': '준비 실패'}.get(event['status'], event['status'])
                    self.status_label.setText(f'실행 완료 · {status} · 결과 창에서 기록 상태와 재현 가능 여부를 확인하세요')
                    self.recorded.emit(str(path))
                elif event.get('reason'):
                    self.status_label.setText('실행 실패 · ' + str(event['reason']))
            except (ValueError, KeyError, TypeError) as exc:
                self.status_label.setText(f'실행 결과 읽기 실패 · {exc}')
        process.deleteLater()

    def reject(self):
        if self.process is None:
            super().reject()

    def closeEvent(self, event):
        if self.process is not None:
            event.ignore()
        else:
            super().closeEvent(event)
