"""Compile an explicit-file NSIS installer; never recursively remove an install root."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def nsis(value):
    return str(value).replace('$','$$').replace('"','$\\"')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--compiler',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    bundle=args.bundle.resolve();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    copying=args.compiler.resolve().parent.parent/'COPYING'
    shutil.copy2(copying,bundle/'_internal/THIRD_PARTY/NSIS-COPYING.txt')
    files=sorted(path for path in bundle.rglob('*') if path.is_file())
    directories={Path('.')}
    rows=[];install=[];remove=[];collision=[]
    for path in files:
        if path.resolve()!=path or not path.resolve().is_relative_to(bundle):raise ValueError('Bundle link/path escape')
        relative=path.relative_to(bundle)
        for parent in relative.parents:directories.add(parent)
        rows.append({'path':relative.as_posix(),'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        directory='' if relative.parent==Path('.') else '\\'+nsis(relative.parent)
        install.extend([f'SetOutPath "$INSTDIR{directory}"',f'File "{nsis(path)}"'])
        remove.append(f'Delete "$INSTDIR\\{nsis(relative)}"')
        collision.extend([f'IfFileExists "$INSTDIR\\{nsis(relative)}" 0 +2','Goto unsafe'])
    for directory in sorted(directories,key=lambda path:len(path.parts),reverse=True):
        if directory!=Path('.'):remove.append(f'RMDir "$INSTDIR\\{nsis(directory)}"')
    checks=[]
    for index,directory in enumerate(sorted(directories)):
        relative='' if directory==Path('.') else '\\'+nsis(directory)
        checks.extend([f'System::Call \'kernel32::GetFileAttributesW(w "$INSTDIR{relative}") i.r0\'',
                       f'IntCmp $0 -1 attr_done_{index}', 'IntOp $0 $0 & 0x400',f'IntCmp $0 0 attr_done_{index} unsafe unsafe',f'attr_done_{index}:'])
    includes={'INSTALL_FILES':install,'REMOVE_FILES':remove,'COLLISION_CHECKS':collision,'REPARSE_CHECKS':checks}
    arguments=[]
    for name,lines in includes.items():
        path=output/(name.lower()+'.nsh');path.write_text('\n'.join(lines)+'\n',encoding='utf-8-sig');arguments.append('/D'+name+'='+str(path))
    installer=output/'QA-Evidence-0.1.0-Windows-x64-Setup.exe'
    script=output/'installer.nsi'
    script.write_text((ROOT/'packaging/windows/installer.nsi').read_text(encoding='utf-8'),encoding='utf-8-sig')
    command=[str(args.compiler.resolve()),'/V3','/DOUTPUT='+str(installer),*arguments,str(script)]
    with (output/'nsis-build.log').open('wb') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode:raise RuntimeError('NSIS compilation failed; inspect nsis-build.log')
    report={'installer':str(installer),'bytes':installer.stat().st_size,'sha256':hashlib.sha256(installer.read_bytes()).hexdigest(),'signed':False,'files':rows,'bundle':str(bundle),'remove_policy':'explicit package files; empty directories only; external user data untouched'}
    (output/'installer-manifest.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({key:value for key,value in report.items() if key!='files'},indent=2),flush=True)


if __name__=='__main__':main()
