import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
import worker_lifecycle as w


def test_watch_does_not_signal_nonisolated_process(monkeypatch):
    monkeypatch.setattr(w.os,'getpid',lambda:100)
    if os.name!='nt':monkeypatch.setattr(w.os,'getpgrp',lambda:50)
    if os.name!='nt':assert w.start_parent_watch(200) is None
    assert w.start_parent_watch(100) is None
    assert w.start_parent_watch(0) is None


@pytest.mark.skipif(os.name=='nt',reason='POSIX process-group integration; Windows path uses taskkill')
@pytest.mark.parametrize('blocked_status_callback',[False,True])
def test_parent_death_stops_worker_and_resistant_helper(tmp_path,blocked_status_callback):
    marker=tmp_path/'closed'
    worker='''import os,sys,time,subprocess
from pathlib import Path
from worker_lifecycle import start_parent_watch
marker=Path(sys.argv[2])
def closed():
 marker.write_text('cancelled')
 if sys.argv[3]=='blocked':time.sleep(60)
start_parent_watch(int(sys.argv[1]),closed,interval=.05)
helper=subprocess.Popen([sys.executable,'-u','-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
print(str(os.getpid())+','+str(helper.pid),flush=True)
time.sleep(60)
'''
    parent='''import subprocess,os,sys
worker=subprocess.Popen([sys.executable,'-u','-c',sys.argv[1],str(os.getpid()),sys.argv[2],sys.argv[3]],stdout=subprocess.PIPE,text=True,start_new_session=True)
print(worker.stdout.readline().strip(),flush=True)
'''
    start=time.monotonic()
    result=subprocess.run([sys.executable,'-u','-c',parent,worker,str(marker),'blocked' if blocked_status_callback else 'normal'],capture_output=True,text=True,check=True,timeout=10)
    ids=list(map(int,result.stdout.strip().split(',')))
    try:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            statuses=[subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip() for pid in ids]
            if all(not status or status.startswith('Z') for status in statuses):break
            time.sleep(.05)
        assert all(not status or status.startswith('Z') for status in statuses),statuses
        assert marker.read_text()=='cancelled'
        assert time.monotonic()-start<6
    finally:
        try:os.killpg(ids[0],9)
        except ProcessLookupError:pass


def test_windows_watch_uses_parent_handle_and_closes_it(monkeypatch):
    from types import SimpleNamespace
    finished = w.threading.Event()
    handles = []
    class OpenProcess:
        def __call__(self, rights, inherit, pid):
            assert (rights, inherit, pid) == (0x00100000, False, 200)
            return 123
    kernel = SimpleNamespace(
        OpenProcess=OpenProcess(),
        WaitForSingleObject=lambda handle, timeout: 0,
        CloseHandle=lambda handle: handles.append(handle.value),
    )
    monkeypatch.setattr(w, 'os', SimpleNamespace(name='nt', getpid=lambda: 100))
    monkeypatch.setattr(w.ctypes, 'windll', SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(w, 'force_worker_tree_exit', lambda: None)
    def closed():
        finished.set()
    stopped = w.start_parent_watch(200, closed, interval=.01)
    assert finished.wait(2)
    deadline = time.monotonic() + 2
    while not handles and time.monotonic() < deadline:
        time.sleep(.01)
    stopped.set()
    assert handles == [123]


def test_windows_forced_exit_kills_entire_worker_tree(monkeypatch):
    from types import SimpleNamespace
    commands = []
    exits = []
    monkeypatch.setattr(w, 'os', SimpleNamespace(name='nt', getpid=lambda: 100, _exit=exits.append))
    monkeypatch.setattr(w.subprocess, 'run', lambda command, **kwargs: commands.append(command))
    w.force_worker_tree_exit()
    assert commands == [['taskkill', '/PID', '100', '/T', '/F']]
    assert exits == [143]
