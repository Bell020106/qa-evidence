"""Owned staging and atomic publication for completed failing pytest items."""
import json,os,platform,shutil,stat,tempfile
from pathlib import Path
from signup031.contract import utc_now
from signup031.storage import create_run_directory,write_evidence
from signup031.test_context import mask_text


class CaptureStage:
    def __init__(self,output):
        self.output=Path(output).resolve();self.output.mkdir(parents=True,exist_ok=True)
        self.parent=self.output
        self.root=Path(tempfile.mkdtemp(prefix='.qa-capture-',dir=self.parent)).resolve()
        info=self.root.stat();self.identity=(info.st_dev,info.st_ino)
    def checked(self):
        if self.root.parent!=self.parent or not self.root.name.startswith('.qa-capture-') or self.root.resolve()!=self.root:raise ValueError('임시 수집 경로 변경')
        info=self.root.stat()
        if (info.st_dev,info.st_ino)!=self.identity:raise ValueError('임시 수집 디렉터리의 소유 대상이 변경됐습니다')
        for path in [self.root,*self.root.rglob('*')]:
            info=path.lstat()
            if path.is_symlink() or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT or not path.resolve().is_relative_to(self.root):
                raise ValueError('임시 수집 경로의 링크는 정리하지 않습니다')
    def discard(self):
        if not self.root.exists():return
        self.checked();shutil.rmtree(self.root)
    def publish(self,path):
        self.checked();path=Path(path)
        if path.parent.parent!=self.root or path.name!='evidence.json' or not path.is_file():raise ValueError('완결된 수집 자료가 아닙니다')
        if self.output.resolve()!=self.output:raise ValueError('발행 대상 경로 변경')
        self.output.mkdir(parents=True,exist_ok=True);destination=self.output/path.parent.name
        if destination.exists():raise ValueError('기존 실행을 덮어쓸 수 없습니다')
        path.parent.rename(destination);self.discard();return destination/'evidence.json'


def unavailable_failure(stage,nodeid,reports,exitcode,reason):
    """Existing v4 with no invented browser/URL/archive; the pytest error is evidence."""
    eid,root=create_run_directory(stage.root);original=next(row for row in reports if row['outcome']=='failed')
    phase=original['when'];message=mask_text(original.get('message',''))
    identity={'nodeid':nodeid,'outcome':'failed','phase':phase}
    record={'title':nodeid,'start_url':None,'final_url':None,'actions':[],'observed':{'inputs':[],'text':''},'limitations':[reason]}
    payload={'contract_version':'4','tc_id':nodeid,'execution':{'id':eid,'started_at':utc_now()},
        'environment':{'python':platform.python_version(),'platform':platform.platform(),'browser':'미수집'},'test_identity':identity,'selenium_record':record,
        'result':{'business':{'status':'preparation_failed' if phase=='setup' else 'failed','phase':phase,'message':message},
                  'pytest':{'status':'failed','phase':phase,'message':message,'reports':[{**row,'message':mask_text(row.get('message',''))} for row in reports],'session_exitcode':int(exitcode)}},
        'capture':{'status':'not_collected','errors':[reason],'source':'pytest reports only; Selenium not connected'},
        'evidence':{'screenshot':{'status':'not_collected','path':None,'reason':reason}},
        'replay':{'status':'failed','reason':reason},'post_run':{'cleanup':{'status':'no_recorder','errors':[]}}}
    write_evidence(root/'evidence.json',payload);return root/'evidence.json'
