"""Windows process ownership. Attach a stdin-blocked child before allowing work."""
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import site
import sys
import time


def python_command(module,*args):
    if getattr(sys,'frozen',False):
        worker=Path(sys.executable).resolve().parent/'QA Evidence Worker.exe'
        if module=='signup031.desktop_worker':return [str(worker),*args]
        return [str(worker),module,*args]
    # The Windows venv launcher can spawn python before assignment. Launch the
    # interpreter directly and supply the current environment's site packages.
    return [getattr(sys,'_base_executable',sys.executable),'-u','-m',module,*args]


def worker_environment():
    env=dict(os.environ)
    if getattr(sys,'frozen',False):
        for name in ('PYTHONPATH','PYTHONHOME'):env.pop(name,None)
        return env
    env['PYTHONPATH']=os.pathsep.join([str(Path(__file__).resolve().parent.parent),*site.getsitepackages(),env.get('PYTHONPATH','')])
    return env


def configure_worker(process,module,args=()):
    """Give legacy Qt workers the same pre-work ownership gate and environment."""
    from PySide6.QtCore import QProcessEnvironment
    command=python_command('signup031.desktop_worker','--startup-gate',module,*args)
    process.setProgram(command[0]);process.setArguments(command[1:])
    environment=QProcessEnvironment()
    for key,value in worker_environment().items():environment.insert(key,value)
    process.setProcessEnvironment(environment)
    job=OwnedJob();process._owned_job=job
    def started():
        try:job.attach(process.processId());process.write(b'start\n')
        except OSError:job.close();process.kill()
    def finished(*_):job.close()
    process.started.connect(started);process.finished.connect(finished)
    process.errorOccurred.connect(lambda error:finished() if error==process.ProcessError.FailedToStart else None)
    return process


def create_worker(parent,module,args=(),error_label=None):
    from PySide6.QtCore import QProcess
    process=QProcess(parent)
    try:return configure_worker(process,module,args)
    except OSError as exc:
        process.deleteLater()
        label=error_label if error_label is not None else parent.status_label
        label.setText('소유 작업자 준비 실패 · '+str(exc))
        return None


class OwnedJob:
    def __init__(self):
        if os.name!='nt':raise OSError('묶음 실행의 프로세스 보호는 Windows에서 지원합니다')
        self.api=ctypes.WinDLL('kernel32',use_last_error=True);self.handle=None
        for name,args,ret in (
            ('CreateJobObjectW',[W.LPVOID,W.LPCWSTR],W.HANDLE),
            ('SetInformationJobObject',[W.HANDLE,ctypes.c_int,W.LPVOID,W.DWORD],W.BOOL),
            ('AssignProcessToJobObject',[W.HANDLE,W.HANDLE],W.BOOL),
            ('OpenProcess',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
            ('CloseHandle',[W.HANDLE],W.BOOL),
            ('TerminateJobObject',[W.HANDLE,W.UINT],W.BOOL),
            ('QueryInformationJobObject',[W.HANDLE,ctypes.c_int,W.LPVOID,W.DWORD,W.LPVOID],W.BOOL)):
            fn=getattr(self.api,name);fn.argtypes=args;fn.restype=ret
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),
                ('LimitFlags',W.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
                ('ActiveProcessLimit',W.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',W.DWORD),('SchedulingClass',W.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(k,ctypes.c_ulonglong) for k in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),
                ('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        info=Extended();info.BasicLimitInformation.LimitFlags=0x2000
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            error=ctypes.WinError(ctypes.get_last_error());self.close();raise error

    def attach(self,pid):
        process=self.api.OpenProcess(0x0100|0x0001,False,pid)
        if not process:raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle,process):raise ctypes.WinError(ctypes.get_last_error())
        finally:self.api.CloseHandle(process)

    def pids(self):
        class IDs(ctypes.Structure):
            _fields_=[('assigned',W.DWORD),('count',W.DWORD),('ids',ctypes.c_size_t*1024)]
        data=IDs()
        if not self.api.QueryInformationJobObject(self.handle,3,ctypes.byref(data),ctypes.sizeof(data),None):raise ctypes.WinError(ctypes.get_last_error())
        return list(data.ids[:data.count])

    def stop(self):
        if not self.api.TerminateJobObject(self.handle,130):raise ctypes.WinError(ctypes.get_last_error())
        deadline=time.monotonic()+5
        while self.pids():
            if time.monotonic()>deadline:raise OSError('소유 프로세스 종료 확인 시간 초과')
            time.sleep(.02)

    def close(self):
        if self.handle:
            handle,self.handle=self.handle,None
            if not self.api.CloseHandle(handle):raise ctypes.WinError(ctypes.get_last_error())
