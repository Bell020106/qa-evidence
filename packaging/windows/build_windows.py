"""Build an auditable, unsigned Windows desktop bundle from explicit inputs."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[2]


def build_environment():
    env=dict(os.environ)
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QML2_IMPORT_PATH','QML_IMPORT_PATH'):
        env.pop(name,None)
    windows=Path(env.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join(map(str,(Path(sys.executable).parent,Path(sys.base_prefix),windows/'System32',windows)))
    return env


def stage_notices(output):
    notices=output/'notices';notices.mkdir(parents=True,exist_ok=True)
    versions={}
    for line in (ROOT/'packaging/windows/requirements-build.txt').read_text().splitlines():
        name,wanted=line.split('==');distribution=metadata.distribution(name)
        if distribution.version!=wanted:raise ValueError(f'{name}: expected {wanted}, installed {distribution.version}')
        versions[name]=wanted
        for file in distribution.files or []:
            if any(word in str(file).lower() for word in ('license','copying','notice')) and file.name.lower().endswith(('.txt','.psf','license','copying','notice')):
                source=Path(distribution.locate_file(file))
                if source.is_file():
                    target=notices/name/Path(str(file)).name;target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists() and target.read_bytes()!=source.read_bytes():target=target.with_name(source.parent.name+'-'+target.name)
                    shutil.copy2(source,target)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    shutil.copy2(python_license,notices/'Python-LICENSE.txt')
    licenses={'LGPL-3.0.txt':'https://raw.githubusercontent.com/qt/qtbase/dev/LICENSES/LGPL-3.0-only.txt',
              'GPL-3.0.txt':'https://raw.githubusercontent.com/qt/qtbase/dev/LICENSES/GPL-3.0-only.txt'}
    for filename,url in licenses.items():
        target=notices/filename
        if not target.exists():
            with urllib.request.urlopen(url,timeout=30) as response:data=response.read(100000)
            if len(data)<5000:raise ValueError('Incomplete license download')
            target.write_bytes(data)
    index='''QA Evidence — third-party notices

This local unsigned build includes dynamically loaded Python, Qt/PySide6/shiboken,
Playwright (with its Node driver), pyee and greenlet, plus the PyInstaller bootloader.
Original license/notice files are retained in this directory and package metadata.
Qt DLLs remain separate in _internal and are not statically linked or restricted
against compatible replacement. Qt/PySide are available under LGPLv3 alternatives;
the wheel's commercial-license notice is retained without claiming a commercial license.

Qt/PySide source and third-party attributions:
https://code.qt.io/cgit/qt/qtbase.git/ (Qt 6.11.2)
https://code.qt.io/cgit/pyside/pyside-setup.git/ (6.11.2)
https://doc.qt.io/qt-6/licenses-used-in-qt.html
https://doc.qt.io/qtforpython-6/licenses.html
https://www.qt.io/download-qt-installer-oss

Python 3.11 source: https://www.python.org/downloads/source/
Playwright: https://github.com/microsoft/playwright-python (v1.62.0)
Node and Playwright third-party notices are included from the installed distribution.
PyInstaller: https://pyinstaller.org/en/stable/license.html (bootloader exception)
NSIS installer: https://nsis.sourceforge.io/License (zlib/lzma components)

Browsers are NOT redistributed in this installer. The browser preparation command
downloads only Playwright's pinned Chromium revision from its official distribution.
Selenium collection adapters, hosted server and CI execution are separate deployments.
This is an internal acceptance artifact, not a signed/public release certification.
'''
    (notices/'THIRD-PARTY-NOTICES.txt').write_text(index,encoding='utf-8')
    (notices/'versions.json').write_text(json.dumps(versions,indent=2))
    return notices,versions


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--acceptance',action='store_true');args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    notices,versions=stage_notices(output)
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(output/'dist'),'--workpath',str(output/'work'),str(ROOT/'packaging/windows/qa_evidence.spec')]
    print('BUILD_START '+str(output),flush=True)
    environment={**build_environment(),'QA_BUILD_NOTICES':str(notices)}
    if args.acceptance:environment['QA_ACCEPTANCE_ENTRY']=str(ROOT/'review_checks/frozen_acceptance.py')
    with (output/'pyinstaller.log').open('wb') as log:
        completed=subprocess.run(command,cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
    if completed.returncode:raise RuntimeError('PyInstaller failed; inspect '+str(output/'pyinstaller.log'))
    bundle=output/'dist/QA Evidence'
    files=[{'path':str(path.relative_to(bundle)).replace('\\','/'),'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(bundle.rglob('*')) if path.is_file()]
    report={'bundle':str(bundle),'versions':versions,'python':sys.version,'architecture':'Windows x64','files':files,'total_bytes':sum(f['size'] for f in files),'browsers':'first-use download; not bundled','signed':False}
    (output/'bundle-manifest.json').write_text(json.dumps(report,indent=2))
    print('BUNDLE_READY '+str(bundle),flush=True)


if __name__=='__main__':main()
