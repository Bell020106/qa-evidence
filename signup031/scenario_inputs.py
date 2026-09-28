"""Guided widgets that keep the existing locator and value contracts unchanged."""
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QWidget,QVBoxLayout,QStackedWidget,QComboBox,QLineEdit,QLabel


class TargetInput(QWidget):
    def __init__(self,editor,locator):
        super().__init__();self.editor=editor;self.locator=locator
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.stack=QStackedWidget();layout.addWidget(self.stack)
        self.choice=QComboBox();self.choice.setMinimumContentsLength(12)
        self.choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.raw=QLineEdit();self.raw.setPlaceholderText('예: #name 또는 입력칸에 연결된 이름')
        self.stack.addWidget(self.choice);self.stack.addWidget(self.raw)
        self.choice.currentIndexChanged.connect(self._selected)
        self.raw.textChanged.connect(lambda:self.refresh())
        self.raw.editingFinished.connect(editor._refresh_targets)
        self.locator.currentIndexChanged.connect(lambda:self.refresh())
        self.refresh()

    def pair(self):return (self.locator.currentData(),self.raw.text())
    def text(self):return self.raw.text()
    def setText(self,text):self.raw.setText(text)
    def set_advanced(self,advanced):self.stack.setCurrentIndex(1 if advanced else 0)

    def refresh(self):
        pair=self.pair();self.choice.blockSignals(True);self.choice.clear();self.choice.addItem('대상 선택',None)
        for key,title in self.editor.targets.items():self.choice.addItem(title,key)
        if pair[1] and pair not in self.editor.targets:self.choice.addItem('이 행에서 직접 입력한 대상',pair)
        selected=next((i for i in range(1,self.choice.count()) if tuple(self.choice.itemData(i))==pair),0)
        self.choice.setCurrentIndex(selected);self.choice.blockSignals(False)

    def _selected(self,index):
        pair=self.choice.itemData(index)
        if pair is None:self.raw.clear();return
        locator,target=pair
        self.locator.setCurrentIndex(self.locator.findData(locator));self.raw.setText(target)


class ValueInput(QWidget):
    def __init__(self,kind):
        super().__init__();self.kind=None;self.saved={}
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.stack=QStackedWidget();layout.addWidget(self.stack)
        self.edit=QLineEdit();self.number=QLineEdit();self.number.setValidator(QIntValidator(0,1000000,self));self.number.setPlaceholderText('예: 5 (글자 수)')
        self.boolean=QComboBox();self.boolean.addItem('보여야 함',True);self.boolean.addItem('보이지 않아야 함',False)
        self.empty=QLabel('입력할 값 없음')
        for widget in (self.edit,self.number,self.boolean,self.empty):self.stack.addWidget(widget)
        self.set_kind(kind)

    def text(self):
        if self.kind=='visible':return 'true' if self.boolean.currentData() else 'false'
        if self.kind=='input_length':return self.number.text()
        if self.kind in ('wait','click'):return ''
        return self.edit.text()

    def setText(self,value):
        if self.kind=='visible':
            if str(value).lower() not in ('true','false'):raise ValueError('보여야 함 또는 보이지 않아야 함을 선택하세요.')
            self.boolean.setCurrentIndex(self.boolean.findData(str(value).lower()=='true'))
        elif self.kind=='input_length':self.number.setText(str(value))
        elif self.kind not in ('wait','click'):self.edit.setText(str(value))

    def set_kind(self,kind):
        if self.kind is not None:self.saved[self.kind]=self.text()
        self.kind=kind
        self.stack.setCurrentIndex(2 if kind=='visible' else 1 if kind=='input_length' else 3 if kind in ('wait','click') else 0)
        self.edit.setPlaceholderText({'fill':'입력할 내용 · 예: 사과','press':'누를 키 · 예: Enter','text':'화면에 나와야 할 문구 · 예: 완료'}.get(kind,''))
        self.setText(self.saved.get(kind,'true' if kind=='visible' else ''))


def limitation_summary(reasons):
    known={'URL changed during recording':'기록 중 사이트 주소가 달라졌습니다.',
           'sensitive field value omitted':'비밀번호 등 민감한 입력값은 기록에서 제외했습니다.',
           'sensitive field interaction omitted':'민감한 입력칸에서 한 동작은 기록에서 제외했습니다.',
           'unsupported input type; action omitted':'지원하지 않는 입력칸의 동작은 기록하지 못했습니다.',
           'unsupported control click omitted':'지원하지 않는 컨트롤의 클릭은 기록하지 못했습니다.',
           'unsupported modified or repeated click':'조합 키를 누른 클릭이나 반복 클릭은 기록하지 못했습니다.',
           'ambiguous duplicate id; action omitted':'같은 표시를 가진 대상이 여러 개여서 일부 동작을 기록하지 못했습니다.',
           'iframe content not recorded':'화면 안에 따로 삽입된 페이지의 내용은 기록하지 못했습니다.',
           'unexpected document navigation; earlier actions unavailable':'다른 페이지로 이동해 이전 화면의 동작 일부가 없습니다.',
           'document navigation: earlier document actions unavailable':'다른 페이지로 이동해 이전 화면의 동작 일부가 없습니다.',
           'new tab not recorded':'새 탭에서 한 동작은 기록하지 못했습니다.',
           'download not recorded':'다운로드한 파일은 기록에 포함되지 않았습니다.'}
    result=[]
    for reason in reasons:
        if reason in known:message=known[reason]
        elif reason.startswith('unsupported keyboard action: '):message='지원하지 않는 키 입력이 있었습니다: '+reason.split(': ',1)[1]
        else:message='추가 제한이 있습니다. 아래 원문 상세를 확인하세요.'
        if message not in result:result.append(message)
    return '\n'.join(result)
