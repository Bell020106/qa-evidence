"""Google metadata/tab selection with the same mapping and registration UI as CSV."""
import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QProcess,QProcessEnvironment
from PySide6.QtWidgets import QComboBox,QFileDialog,QHBoxLayout,QLabel,QLineEdit,QPushButton
from signup031.tc_catalog_dialog import CsvImportDialog
from signup031.sheets_import import parse_link


class SheetsImportDialog(CsvImportDialog):
    def __init__(self,catalog,*,test_endpoint=None):
        self.test_endpoint=test_endpoint;self.session=None;self.metadata_link=None
        super().__init__(catalog,'Google Sheets',auto_preview=False)
        self.help_topic='sheets'
        self.setWindowTitle('Google Sheets 링크 · 테스트 등록');self.resize(1100,980)
        self.form.labelForField(self.path_edit).setText('Google Sheets 링크')
        self.path_edit.setReadOnly(False);self.path_edit.clear()
        self.form.labelForField(self.source_edit).setText('문서 ID + sheetId 출처 (자동)')
        self.source_edit.clear();self.source_edit.setReadOnly(True)
        self.encoding.hide();self.form.labelForField(self.encoding).hide()
        self.api_key_edit=QLineEdit();self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.form.insertRow(2,'공개 API key (메모리만)',self.api_key_edit)
        self.client_file=QLineEdit();self.client_file.setReadOnly(True)
        config_row=QHBoxLayout();config_row.addWidget(self.client_file)
        self.config_button=QPushButton('Desktop OAuth JSON 선택');self.config_button.clicked.connect(self.choose_config);config_row.addWidget(self.config_button)
        self.connect_button=QPushButton('브라우저로 읽기 전용 연결');self.connect_button.clicked.connect(self.start_connect);config_row.addWidget(self.connect_button)
        self.form.insertRow(3,'비공개 / OAuth',config_row)
        self.tabs=QComboBox();self.metadata_button=QPushButton('문서 탭 읽기');self.metadata_button.clicked.connect(self.start_metadata)
        tabs_row=QHBoxLayout();tabs_row.addWidget(self.tabs,1);tabs_row.addWidget(self.metadata_button);self.form.addRow('탭 선택 (sheetId)',tabs_row)
        self.csv_button=QPushButton('연결 설정이 없으면 CSV 파일로 가져오기');self.csv_button.clicked.connect(self.csv_alternative)
        self.layout().insertWidget(1,self.csv_button)
        self.preview_button.setText('선택 탭 읽기 · 열 연결 미리보기')
        self.status_label.setText(('로컬 HTTP 대역 · 실제 Google 연결 검증 아님. ' if test_endpoint else '')+
            '공개 API key 또는 OAuth 연결 설정이 필요합니다. 브라우저는 연결 버튼을 눌러야 열립니다.')
        self.import_button.setEnabled(False)

    def choose_config(self):
        path,_=QFileDialog.getOpenFileName(self,'Google Desktop OAuth 클라이언트 JSON','','JSON (*.json)')
        if path:self.client_file.setText(path)

    def csv_alternative(self):
        if self.process is None:self.close();self.catalog.choose_file()

    def start_connect(self):
        if not self.client_file.text():self.status_label.setText('Desktop OAuth 클라이언트 JSON을 먼저 선택하세요');return
        self._start('oauth')
    def start_metadata(self):self._start('metadata')
    def start_preview(self):self._start('preview')
    def start_import(self):
        if self.snapshot is None:return
        if self.snapshot['header_row']!=self.header_row.value() or self.snapshot['provider_metadata']['sheet_id']!=self.tabs.currentData():
            self.status_label.setText('헤더/탭 선택이 바뀌었습니다. 다시 미리보기 하세요');return
        self._start('import')

    def _enabled(self,enabled):
        super()._enabled(enabled)
        if hasattr(self,'api_key_edit'):
            for control in (self.api_key_edit,self.path_edit,self.config_button,self.connect_button,self.tabs,self.metadata_button,self.csv_button):control.setEnabled(enabled)

    def _start(self,action):
        if self.process is not None:return
        request={'action':action,'root':str(self.catalog.store.root),'project':self.project,'test_endpoint':self.test_endpoint}
        if action=='oauth':request.update(client_file=self.client_file.text(),open_browser=True)
        else:
            try:
                link=parse_link(self.path_edit.text())
                if self.session and self.session['expires_at']<=time.time():
                    self.session=None;raise ValueError('OAuth 세션이 만료됐습니다. 다시 연결하세요')
                if not self.session and not self.api_key_edit.text():raise ValueError('연결 설정 필요: API key/OAuth 또는 CSV 가져오기를 선택하세요')
                if action in ('preview','import') and (self.metadata_link!=link or self.tabs.currentData() is None):
                    raise ValueError('문서 탭 읽기를 실행하고 탭을 명시적으로 선택하세요')
            except ValueError as exc:self.status_label.setText(str(exc));return
            request.update(link=self.path_edit.text().strip(),api_key=self.api_key_edit.text(),access_token=self.session['access_token'] if self.session else '',
                sheet_id=self.tabs.currentData(),header_row=self.header_row.value(),mapping={k:c.currentData() for k,c in self.mappings.items()})
            if action=='import':request.update(sha256=self.snapshot['sha256'],headers=self.snapshot['headers'],source_id=self.snapshot['source_id'])
            else:self.snapshot=None
        self.action=action;self.cancelled=False;self.output=b''
        from signup031.owned_job import create_worker
        process=create_worker(self,'signup031.sheets_worker')
        if process is None:return
        process.readyReadStandardOutput.connect(self._read);process.finished.connect(self._finished)
        process.errorOccurred.connect(lambda _:self.status_label.setText('Sheets 작업자 오류'))
        self.process=process;self._enabled(False)
        self.status_label.setText('브라우저 인증 대기 · 취소할 수 있습니다' if action=='oauth' else 'Sheets 읽기/등록 중…')
        process.start()
        if not process.waitForStarted(1500):self._finished(2,None);return
        process.write((json.dumps(request)+'\n').encode());process.closeWriteChannel();self.timer.start(150000 if action=='oauth' else 60000)

    def _finished(self,code,_status):
        if self.action in ('preview','import'):
            super()._finished(code,_status)
            if self.snapshot is not None:self.source_edit.setText(self.snapshot['source_id'])
            if self.test_endpoint:self.status_label.setText('로컬 HTTP 대역 · '+self.status_label.text())
            return
        self._read();process,self.process=self.process,None;self.timer.stop()
        try:
            if self.cancelled:raise ValueError('연결 작업 취소 · 기존 TC는 유지됩니다')
            reply=json.loads(self.output.splitlines()[-1])
            if code!=0 or reply['status']!='complete':raise ValueError(reply.get('reason','작업자 오류'))
            result=reply['result']
            if self.action=='oauth':
                self.session=result;self.status_label.setText('읽기 전용 세션 연결됨 · 문서 탭 읽기를 누르세요. 이 창을 닫으면 연결 정보가 사라집니다.')
            else:
                self.metadata_link=result['link'];self.tabs.clear()
                for tab in result['tabs']:self.tabs.addItem(tab['title']+' · '+str(tab['sheet_id']),tab['sheet_id'])
                gid=self.metadata_link['gid'];self.tabs.setCurrentIndex(self.tabs.findData(gid) if gid is not None else -1)
                if self.metadata_link['gid_conflict']:message='query/fragment gid가 다릅니다. 원하는 탭을 직접 선택하세요'
                elif gid is not None and self.tabs.currentIndex()<0:message='링크의 gid가 문서에 없습니다. 탭을 확인해 직접 선택하세요'
                else:message='탭을 확인하고 선택 탭 읽기를 누르세요'
                self.status_label.setText(('로컬 HTTP 대역 · ' if self.test_endpoint else '')+message)
        except (ValueError,KeyError,IndexError,TypeError) as exc:self.status_label.setText('실패 · '+str(exc))
        self.output=b'';self._enabled(True)
        if process is not None:process.deleteLater()

    def closeEvent(self,event):
        if self.process is None:
            self.session=None;self.api_key_edit.clear();self.client_file.clear();self.output=b''
        super().closeEvent(event)
