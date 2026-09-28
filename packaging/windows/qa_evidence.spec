# Two native entry points share an onedir dependency set. No project directory tree is bundled.
from pathlib import Path
import os
import json
from PyInstaller.utils.hooks import copy_metadata

root=Path(SPECPATH).parents[1]
notices=Path(os.environ['QA_BUILD_NOTICES'])
datas=[(str(root/'signup031/manual_capture.js'),'signup031'),
       (str(root/'signup031/timeline_capture.js'),'signup031'),
       (str(root/'signup031/demos'),'signup031/demos'),(str(notices),'THIRD_PARTY')]
for package in ('playwright','pyee','greenlet','PySide6','PySide6_Essentials','shiboken6'):
    datas+=copy_metadata(package)
workers=['signup031.'+name for name in ('web_runner','manual_recording','replay','ingestion_worker','jira_sync_worker','tc_import_worker','sheets_worker','suite_worker','experiment_worker','ai_worker','evaluation_worker','failure_analysis_worker','android_worker','browser_install')]
entries=[str(root/'packaging/windows/gui_entry.py'),str(root/'packaging/windows/worker_entry.py')]
probe_entry=os.environ.get('QA_ACCEPTANCE_ENTRY')
if probe_entry:entries.append(probe_entry)
a=Analysis(entries,pathex=[str(root)],
           binaries=[],datas=datas,hiddenimports=workers,hookspath=[],runtime_hooks=[],
           excludes=['pytest','selenium','fastapi','uvicorn','IPython','numpy','matplotlib'],noarchive=False)
pyz=PYZ(a.pure)
assert not any(row[0].startswith(('review_checks','tests.','frozen_acceptance')) for row in a.pure)
(notices.parent/'analysis-inputs.json').write_text(json.dumps({'modules':[row[0] for row in a.pure],'scripts':[row[0] for row in a.scripts],'binaries':[{'destination':row[0],'source':row[1]} for row in a.binaries]},indent=2))
gui=EXE(pyz,[row for row in a.scripts if row[0] not in ('worker_entry','frozen_acceptance')],[],exclude_binaries=True,name='QA Evidence',debug=False,strip=False,upx=False,console=False)
worker=EXE(pyz,[row for row in a.scripts if row[0] not in ('gui_entry','frozen_acceptance')],[],exclude_binaries=True,name='QA Evidence Worker',debug=False,strip=False,upx=False,console=True)
bundle=COLLECT(gui,worker,a.binaries,a.datas,strip=False,upx=False,name='QA Evidence')
if probe_entry:
    probe=EXE(pyz,[row for row in a.scripts if row[0] not in ('gui_entry','worker_entry')],[],exclude_binaries=True,name='QA Acceptance Probe',debug=False,strip=False,upx=False,console=True)
    acceptance=COLLECT(probe,worker,a.binaries,a.datas,strip=False,upx=False,name='QA Acceptance')
