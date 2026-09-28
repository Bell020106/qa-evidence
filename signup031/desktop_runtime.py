"""Installed desktop resource and user-data boundaries."""
import os
from pathlib import Path
import sys


def user_base():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'QAEvidence'


def configure_runtime():
    if getattr(sys,'frozen',False):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(user_base()/'browsers'))
        for name in ('PYTHONPATH','PYTHONHOME'):
            os.environ.pop(name,None)


def check_data_path(path):
    path=Path(path).resolve()
    if len(str(path))>140:
        raise ValueError('결과 폴더 경로가 너무 깁니다. 140자 이하의 짧은 사용자 폴더를 선택하세요.')
    if getattr(sys,'frozen',False) and path.is_relative_to(Path(sys.executable).resolve().parent):
        raise ValueError('설치 폴더 밖의 사용자 결과 폴더를 선택하세요.')
    return path
