"""Manual TC catalog and explicit CSV mapping/change application."""
import json
import os
from pathlib import Path
import sqlite3
import sys

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout,QLabel,QLineEdit,QPlainTextEdit,QPushButton,QSpinBox,QTableWidget,QTableWidgetItem,QVBoxLayout)
from signup031.tc_import import FIELDS, TCStore

CAPTIONS = {'original_id':'원본 테스트 ID','title':'제목 (필수)','preconditions':'사전 조건',
            'steps':'절차','expected':'기대 결과','priority':'우선순위'}


class CsvImportDialog(QDialog):
    def __init__(self,catalog,path,*,auto_preview=True):
        super().__init__(catalog)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.catalog=catalog; self.process=None; self.snapshot=None; self.cancelled=False
        self.project=catalog.project_edit.text().strip()
        self.setWindowTitle('CSV 열 연결 · 테스트 등록'); self.resize(1000,780)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'catalog',layout); form=QFormLayout();self.form=form
        self.path_edit=QLineEdit(str(path)); self.path_edit.setReadOnly(True)
        self.source_edit=QLineEdit(os.path.normcase(str(Path(path).resolve())))
        self.encoding=QComboBox(); self.encoding.addItem('UTF-8 / BOM','utf-8-sig'); self.encoding.addItem('CP949 (직접 선택)','cp949')
        self.header_row=QSpinBox(); self.header_row.setRange(1,100)
        project_label=QLabel(self.project); project_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow('프로젝트',project_label); form.addRow('파일',self.path_edit); form.addRow('출처 ID (같은 원본에만 재사용)',self.source_edit)
        form.addRow('문자 형식(인코딩)',self.encoding); form.addRow('열 제목이 있는 행',self.header_row)
        layout.addLayout(form)
        self.preview_button=QPushButton('다시 읽기 · 헤더 확인'); self.preview_button.clicked.connect(self.start_preview); layout.addWidget(self.preview_button)
        mapping_form=QFormLayout(); self.mappings={}
        for field in FIELDS:
            combo=QComboBox(); self.mappings[field]=combo; mapping_form.addRow(CAPTIONS[field],combo)
        layout.addLayout(mapping_form)
        self.preview=QTableWidget(); self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); layout.addWidget(self.preview,1)
        self.status_label=QLabel('파일을 읽는 중…'); self.status_label.setWordWrap(True); self.status_label.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status_label)
        self.outcomes=QPlainTextEdit(); self.outcomes.setReadOnly(True); self.outcomes.setMaximumHeight(120); layout.addWidget(self.outcomes)
        buttons=QHBoxLayout(); self.import_button=QPushButton('열을 연결한 테스트 등록'); self.import_button.clicked.connect(self.start_import)
        self.cancel_button=QPushButton('취소 / 닫기'); self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.import_button); buttons.addWidget(self.cancel_button); layout.addLayout(buttons)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.cancel)
        if auto_preview:self.start_preview()

    def _enabled(self,enabled):
        for control in (self.preview_button,self.source_edit,self.encoding,self.header_row,*self.mappings.values()): control.setEnabled(enabled)
        self.import_button.setEnabled(enabled and self.snapshot is not None)

    def start_preview(self): self._start('preview')
    def start_import(self):
        if self.snapshot is None: return
        if self.snapshot['header_row'] != self.header_row.value() or self.snapshot['encoding'] != self.encoding.currentData():
            self.status_label.setText('헤더/인코딩을 변경했습니다. 다시 읽기를 누르세요'); return
        self._start('import')

    def _start(self,action):
        if self.process is not None: return
        request={'action':action,'root':str(self.catalog.store.root),'project':self.project,'path':self.path_edit.text(),
            'encoding':self.encoding.currentData(),'header_row':self.header_row.value(),'source_id':self.source_edit.text(),
            'mapping':{k:c.currentData() for k,c in self.mappings.items()}}
        if action=='import': request.update(sha256=self.snapshot['sha256'],headers=self.snapshot['headers'])
        else: self.snapshot=None
        self.action=action; self.cancelled=False; self.output=b''
        from signup031.owned_job import create_worker
        process=create_worker(self,'signup031.tc_import_worker')
        if process is None:return
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(lambda _:self.status_label.setText('CSV 작업자 시작/실행 오류'))
        self.process=process; self._enabled(False); self.status_label.setText('읽는 중…' if action=='preview' else '등록 중…')
        process.start()
        if not process.waitForStarted(1500): self._finished(2,None); return
        process.write((json.dumps(request)+'\n').encode()); process.closeWriteChannel(); self.timer.start(60000)

    def _read(self):
        if self.process is not None: self.output+=bytes(self.process.readAllStandardOutput())

    def _finished(self,code,_status):
        self._read(); process,self.process=self.process,None; self.timer.stop()
        if self.cancelled:
            self.status_label.setText('취소 요청 완료 · 저장 직후였다면 반영됐을 수 있습니다. TC 목록에서 확인하세요.')
        else:
            try:
                reply=json.loads(self.output.splitlines()[-1])
                if code!=0 or reply['status']!='complete': raise ValueError(reply.get('reason','작업자 오류'))
                result=reply['result']
                if self.action=='preview':
                    self.snapshot=result
                    aliases={'original_id':['ID','테스트 ID','tc_id'],'title':['제목','title'],'preconditions':['사전 조건','preconditions'],
                             'steps':['절차','steps'],'expected':['기대 결과','expected'],'priority':['우선순위','priority']}
                    mapping=result['mapping']
                    for field,combo in self.mappings.items():
                        combo.clear(); combo.addItem('선택 안 함',None)
                        for index,name in enumerate(result['headers']): combo.addItem(f'{index+1}: {name or "(빈 헤더)"}',index)
                        matches=[i for i,name in enumerate(result['headers']) if name in aliases[field]]
                        selected=mapping.get(field) if mapping else matches[0] if len(matches)==1 else None
                        combo.setCurrentIndex(combo.findData(selected))
                    self.preview.setColumnCount(len(result['headers'])); self.preview.setHorizontalHeaderLabels(result['headers'])
                    self.preview.setRowCount(len(result['preview']))
                    for row_index,row in enumerate(result['preview']):
                        self.preview.setVerticalHeaderItem(row_index,QTableWidgetItem(str(row['logical_row'])))
                        for column,value in enumerate(row['cells'][:len(result['headers'])]): self.preview.setItem(row_index,column,QTableWidgetItem(value))
                    self.status_label.setText(f"데이터 {result['total_rows']}행 · 처음 20행 미리보기 · " +
                        ('저장된 동일 헤더 매핑 사용' if mapping else '열을 확인하고 매핑하세요') +
                        (' · 중복 헤더: 열 번호로 선택 필요' if result['duplicate_headers'] else ''))
                else:
                    self.last_result=result; counts=result['counts']
                    self.status_label.setText(f"등록 {counts['registered']} · 중복 {counts['duplicate']} · 변경 후보 {counts['candidate']} · 오류 {counts['error']} · 검토 필요 {counts['review_needed']} (유효 행 중 일부)")
                    self.outcomes.setPlainText('\n'.join(f"논리행 {r['logical_row']} / 물리줄 {r['physical_start']}–{r['physical_end']}: {r['status']} {r['reason']}" for r in result['rows']))
            except (ValueError,KeyError,IndexError,TypeError) as exc: self.status_label.setText('실패 · '+str(exc))
        self._enabled(True); self.catalog.refresh()
        if process is not None: process.deleteLater()

    def cancel(self):
        if self.process is None: self.close(); return
        self.cancelled=True; self.process.kill()

    def closeEvent(self,event):
        if self.process is not None:
            self.cancel(); event.ignore(); QTimer.singleShot(100,self.close); return
        super().closeEvent(event)


class TCCatalogDialog(QDialog):
    def __init__(self,viewer):
        super().__init__(viewer); self.store=TCStore(viewer.root); self.import_dialog=None;self.sheets_dialog=None
        self.case_offset=0; self.candidate_offset=0; self.page_size=200
        self.setWindowTitle('테스트 목록·CSV 가져오기'); self.resize(1100,780)
        layout=QVBoxLayout(self); from signup031.help_dialog import add_help; add_help(self,'catalog',layout); top=QHBoxLayout()
        top.addWidget(QLabel('프로젝트')); self.project_edit=QLineEdit('default'); top.addWidget(self.project_edit)
        self.import_button=QPushButton('CSV 파일 선택'); self.import_button.clicked.connect(self.choose_file); top.addWidget(self.import_button)
        self.sheets_button=QPushButton('Google Sheets 링크');self.sheets_button.clicked.connect(self.open_sheets);top.addWidget(self.sheets_button)
        refresh=QPushButton('목록 새로고침'); refresh.clicked.connect(self.refresh); top.addWidget(refresh); layout.addLayout(top)
        self.status_label=QLabel(); self.status_label.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status_label)
        pages=QHBoxLayout(); self.previous_button=QPushButton('항목 이전 200개'); self.next_button=QPushButton('항목 다음 200개')
        self.previous_button.clicked.connect(lambda:self.change_page(-1)); self.next_button.clicked.connect(lambda:self.change_page(1))
        pages.addWidget(self.previous_button); pages.addWidget(self.next_button); layout.addLayout(pages)
        self.table=QTableWidget(0,5); self.table.setHorizontalHeaderLabels(['원본 ID / 로컬 ID','제목','출처 ID','버전','상태'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_case); layout.addWidget(self.table,1)
        self.version_combo=QComboBox(); self.version_combo.currentIndexChanged.connect(self.show_version)
        version_row=QHBoxLayout(); version_row.addWidget(QLabel('선택 항목의 보존 버전')); version_row.addWidget(self.version_combo); layout.addLayout(version_row)
        self.details=QPlainTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details,1)
        choices=QHBoxLayout(); self.candidates=QComboBox(); self.candidates.currentIndexChanged.connect(self.show_candidate)
        self.targets=QComboBox(); choices.addWidget(QLabel('변경 후보')); choices.addWidget(self.candidates,1)
        choices.addWidget(QLabel('적용 대상')); choices.addWidget(self.targets,1); layout.addLayout(choices)
        layout.addWidget(QLabel('ID 없는 후보의 기존 적용 대상은 위 TC 목록 페이지에서 선택합니다.'))
        candidate_pages=QHBoxLayout(); self.candidate_previous=QPushButton('후보 이전 200개'); self.candidate_next=QPushButton('후보 다음 200개')
        self.candidate_previous.clicked.connect(lambda:self.change_page(-1,candidates=True)); self.candidate_next.clicked.connect(lambda:self.change_page(1,candidates=True))
        candidate_pages.addWidget(self.candidate_previous); candidate_pages.addWidget(self.candidate_next); layout.addLayout(candidate_pages)
        self.candidate_details=QPlainTextEdit(); self.candidate_details.setReadOnly(True); self.candidate_details.setMaximumHeight(130); layout.addWidget(self.candidate_details)
        self.apply_button=QPushButton('선택한 변경 적용'); self.apply_button.clicked.connect(self.apply_candidate); layout.addWidget(self.apply_button)
        self.review_button=QPushButton('현재 버전과 다시 비교'); self.review_button.clicked.connect(self.review_candidate); layout.addWidget(self.review_button)
        self.project_edit.editingFinished.connect(self.reset_pages); self.refresh()

    def reset_pages(self):
        self.case_offset=0; self.candidate_offset=0; self.refresh()

    def change_page(self,direction,*,candidates=False):
        if candidates: self.candidate_offset=max(0,self.candidate_offset+direction*self.page_size)
        else: self.case_offset=max(0,self.case_offset+direction*self.page_size)
        self.refresh()

    def choose_file(self):
        if not self.project_edit.text().strip(): self.status_label.setText('프로젝트를 입력하세요'); return
        path,_=QFileDialog.getOpenFileName(self,'CSV TC 파일 선택','','CSV (*.csv)')
        if path:
            self.import_dialog=CsvImportDialog(self,path); self.import_dialog.show()

    def open_sheets(self):
        if not self.project_edit.text().strip():self.status_label.setText('프로젝트를 입력하세요');return
        from signup031.sheets_dialog import SheetsImportDialog
        self.sheets_dialog=SheetsImportDialog(self);self.sheets_dialog.show()

    def refresh(self):
        try:
            project=self.project_edit.text().strip(); selected=self.candidates.currentData()
            self.rows=self.store.list_cases(project,limit=self.page_size,offset=self.case_offset)
            self.table.setRowCount(len(self.rows))
            for row_index,row in enumerate(self.rows):
                for column,value in enumerate([row['original_id'] or row['local_id'],row['fields']['title'],row['source_id'],str(row['version']),
                    '검토 필요 · 수동' if row['review_needed'] else '수동 · 실행 설정 없음']): self.table.setItem(row_index,column,QTableWidgetItem(value))
            self.table.resizeColumnsToContents()
            count=self.store.count_cases(project); pending_count=self.store.count_candidates(project)
            self.previous_button.setEnabled(self.case_offset>0); self.next_button.setEnabled(self.case_offset+len(self.rows)<count)
            self.candidate_previous.setEnabled(self.candidate_offset>0); self.candidate_next.setEnabled(self.candidate_offset+self.page_size<pending_count)
            self.status_label.setText(f"TC {count}개 · TC 페이지 {self.case_offset//self.page_size+1} · 변경 후보 {pending_count}개 / 페이지 {self.candidate_offset//self.page_size+1} · 자동 실행 없음")
            self.pending=self.store.list_candidates(project,limit=self.page_size,offset=self.candidate_offset); self.candidates.blockSignals(True); self.candidates.clear()
            for candidate in self.pending: self.candidates.addItem(candidate['payload']['fields']['title']+' · '+candidate['reason'],candidate['id'])
            retained=self.candidates.findData(selected)
            if retained>=0:self.candidates.setCurrentIndex(retained)
            self.candidates.blockSignals(False); self.show_candidate()
            self.show_case()
        except (ValueError,OSError,sqlite3.Error) as exc: self.status_label.setText('목록 오류 · '+str(exc))

    def show_case(self):
        index=self.table.currentRow()
        if 0<=index<len(getattr(self,'rows',[])):
            row=self.rows[index]
            self.version_combo.blockSignals(True); self.version_combo.clear()
            for version in range(row['version'],0,-1): self.version_combo.addItem('v'+str(version),version)
            self.version_combo.blockSignals(False); self.show_version()
        else:
            self.version_combo.clear(); self.details.clear()

    def show_version(self):
        index=self.table.currentRow(); version=self.version_combo.currentData()
        if 0<=index<len(getattr(self,'rows',[])) and version is not None:
            row=self.rows[index]; snapshot=self.store.version(row['local_id'],version)
            self.details.setPlainText('수동 TC · 자동 실행 설정 없음\n로컬 ID: '+row['local_id']+' · v'+str(version)+'\n'+json.dumps(snapshot,ensure_ascii=False,indent=2))

    def show_candidate(self):
        index=self.candidates.currentIndex(); self.targets.clear(); self.candidate_details.clear()
        if not 0<=index<len(getattr(self,'pending',[])): self.apply_button.setEnabled(False); return
        candidate=self.pending[index]; self.apply_button.setEnabled(True)
        self.candidate_details.setPlainText('적용 전 후보 원문 · 기존 버전은 유지됩니다\n'+json.dumps(candidate['payload']['fields'],ensure_ascii=False,indent=2))
        if candidate['target_id'] is None: self.targets.addItem('새 TC로 등록',None)
        targets=self.rows
        if candidate['target_id'] and not any(row['local_id']==candidate['target_id'] for row in targets):
            targets=[self.store.case(candidate['target_id'])]
        for row in targets:
            if row['source_id']==candidate['source_id'] and (row['local_id']==candidate['target_id'] or (candidate['target_id'] is None and not row['original_id'])):
                self.targets.addItem(row['fields']['title']+' · v'+str(row['version']),{'id':row['local_id'],'version':row['version']})

    def apply_candidate(self):
        index=self.candidates.currentIndex()
        if not 0<=index<len(self.pending): return
        candidate=self.pending[index]; target=self.targets.currentData()
        try:
            self.store.apply_candidate(self.project_edit.text().strip(),candidate['id'],as_new=target is None,
                target_id=target['id'] if target else None,expected_version=target['version'] if target else None)
            self.refresh(); self.status_label.setText('변경 적용 완료 · 이전 TC 버전과 실행 증거는 유지됩니다')
        except (ValueError,OSError,sqlite3.Error) as exc: self.status_label.setText('적용 실패 · '+str(exc))

    def review_candidate(self):
        index=self.candidates.currentIndex(); target=self.targets.currentData()
        if not 0<=index<len(self.pending) or target is None: return
        try:
            self.store.review_candidate(self.project_edit.text().strip(),self.pending[index]['id'],expected_version=target['version'])
            self.refresh(); self.status_label.setText('현재 버전 기준으로 재검토했습니다. 후보 원문을 비교한 뒤 적용하세요.')
        except (ValueError,OSError,sqlite3.Error) as exc: self.status_label.setText('재검토 실패 · '+str(exc))

    def closeEvent(self,event):
        if self.sheets_dialog is not None and self.sheets_dialog.process is not None:
            self.sheets_dialog.cancel();event.ignore();QTimer.singleShot(100,self.close);return
        if self.import_dialog is not None and self.import_dialog.process is not None:
            self.import_dialog.cancel(); event.ignore(); QTimer.singleShot(100,self.close); return
        if self.sheets_dialog is not None:self.sheets_dialog.close()
        super().closeEvent(event)
