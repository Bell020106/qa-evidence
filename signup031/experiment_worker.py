"""Owned offline experiment child; original restoration and experiment are separate."""
import json
from pathlib import Path
from queue import Empty,Queue
import sys
from threading import Thread
import time
from uuid import uuid4

from signup031.ingestion_contract import safe_path
from signup031.network_experiment import NetworkExperiment
from signup031.replay import ReplaySession
from signup031.storage import write_evidence


def emit(value):print(json.dumps(value,ensure_ascii=True),flush=True)


def main():
    result={'experiment_version':1,'experiment_id':uuid4().hex,'normal_restore':'not_run','status':'starting'};path=None
    try:
        request=json.loads(sys.stdin.readline(65537));root=Path(request['root']).absolute()
        if any((p/'evidence.json').exists() for p in (root,*root.parents)):raise ValueError('실험은 원본 실행 폴더 밖에 저장해야 합니다')
        path=safe_path(root,'.experiments/'+result['experiment_id']+'/experiment.json');path.parent.mkdir(parents=True)
        commands=Queue(maxsize=32)
        def read_commands():
            for line in sys.stdin:
                try:
                    if len(line)>65536:raise ValueError()
                    command=json.loads(line)
                except ValueError:command={'action':'invalid'}
                commands.put(command)
            commands.put({'action':'stop'})
        Thread(target=read_commands,daemon=True).start()
        with ReplaySession(request['archive'],headless=request.get('headless') is True) as replay:
            normal=replay.restore();result['normal_restore']=normal['status'];result['source_execution_id']=replay.manifest['execution_id']
            experiment=NetworkExperiment(replay)
            manifest=replay.manifest
            if manifest['archive_version']==2:
                clicks=[r for r in manifest['scenario_snapshot']['steps'] if r['action']=='click'];kind='configured'
            else:
                record=manifest.get('manual_record',manifest.get('selenium_record',{}));clicks=[r for r in record.get('actions',[]) if r['action']=='click'];kind='manual'
            emit({'status':'normal_ready','entries':experiment.entries,'click_count':len(clicks),'experiment_id':result['experiment_id']})
            last=None;deadline=time.monotonic()+1800
            while not replay.page.is_closed():
                try:command=commands.get_nowait()
                except Empty:command=None
                if time.monotonic()>deadline or (command and command.get('action')=='stop'):break
                if command:
                    try:
                        if command.get('action')=='arm':experiment.arm(command['index'],command['mode'],command.get('delay_ms',0))
                        elif command.get('action')=='click':
                            index=command.get('index')
                            if experiment.rule is None or type(index) is not int or not 0<=index<len(clicks):raise ValueError('규칙과 기록 클릭을 먼저 선택하세요')
                            if kind=='configured':
                                from signup031.web_scenario import locate
                                target=locate(replay.page,clicks[index])
                            else:
                                from signup031.manual_replay import _locate
                                target=_locate(replay.page,clicks[index])
                            target.click(timeout=5000)
                        else:raise ValueError('지원하지 않는 실험 명령')
                    except (ValueError,KeyError,TypeError) as exc:emit({'status':'command_error','reason':str(exc)[:500]})
                experiment.tick();status=experiment.status()
                if status!=last:
                    emit({'status':'experiment','result':status});last=status
                replay.page.wait_for_timeout(30)
            result.update(status='closed',result=experiment.status(),cancelled_pending=len(experiment.pending),blocked_count=replay.status()['blocked_count'])
        write_evidence(path,result);emit({'status':'closed','result_path':str(path)});return 0
    except Exception as exc:
        result.update(status='failed',reason=type(exc).__name__+' · 원본 복원 또는 실험 실패')
        if path:
            try:write_evidence(path,result)
            except OSError:pass
        emit({'status':'failed','reason':result['reason']});return 1


if __name__=='__main__':raise SystemExit(main())
