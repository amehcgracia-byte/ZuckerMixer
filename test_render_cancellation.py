import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest
import jam_app as a


def test_late_progress_cannot_revive_cancelled_job(monkeypatch):
    monkeypatch.setattr(a, 'cancel_requested', False)
    job={'status':'stopping','cancel_requested':True}
    updates=[]
    monkeypatch.setattr(a,'set_job',lambda *args,**kwargs:updates.append(kwargs))
    a.handle_child_line(job,'APP_PROGRESS {"status":"running","current":2}')
    assert not updates and job['status']=='stopping'
    job['cancel_requested']=False
    a.handle_child_line(job,'APP_PROGRESS {"status":"running","current":2}')
    assert updates[0]['current']==2


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX owned process-group integration')
def test_cancel_kills_resistant_worker_even_while_status_lock_is_busy(monkeypatch):
    proc=subprocess.Popen([sys.executable,'-u','-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert proc.stdout.readline().strip()=='ready'
    job={'id':'test-cancel','status':'running'}
    monkeypatch.setattr(a,'child_processes',{'test-cancel':proc})
    monkeypatch.setattr(a,'jobs',[job])
    monkeypatch.setattr(a,'cancel_requested',False)
    monkeypatch.setattr(a,'write_job_status',lambda job:None)
    monkeypatch.setattr(a,'lifecycle_log',lambda *args,**kwargs:None)
    monkeypatch.setattr(a,'append_log',lambda *args:None)
    try:
        with a.state_lock:
            thread=threading.Thread(target=a.request_cancel,daemon=True);thread.start()
            assert proc.wait(timeout=4)==-9
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert job['status']=='stopping' and job['cancel_requested']
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        proc.stdout.close()


def test_native_close_check_never_waits_for_status_lock(monkeypatch):
    monkeypatch.setattr(a,'jobs',[{'status':'stopping'}])
    class ForbiddenLock:
        def __enter__(self):raise AssertionError('native close must not wait for disk status lock')
        def __exit__(self,*args):pass
    monkeypatch.setattr(a,'state_lock',ForbiddenLock())
    assert a.has_active_jobs()


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX inherited helper group')
def test_cancel_stops_helper_even_after_worker_leader_exits(monkeypatch):
    helper='import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("helper-ready",flush=True); time.sleep(60)'
    leader='import subprocess,sys,time; subprocess.Popen([sys.executable,"-u","-c",'+repr(helper)+']); time.sleep(60)'
    proc=subprocess.Popen([sys.executable,'-u','-c',leader],stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert proc.stdout.readline().strip()=='helper-ready'
    monkeypatch.setattr(a,'child_processes',{'helper-test':proc})
    monkeypatch.setattr(a,'jobs',[])
    monkeypatch.setattr(a,'cancel_requested',False)
    monkeypatch.setattr(a,'lifecycle_log',lambda *args,**kwargs:None)
    monkeypatch.setattr(a,'append_log',lambda *args:None)
    eof=threading.Event()
    reader=threading.Thread(target=lambda:(proc.stdout.read(),eof.set()),daemon=True)
    reader.start()
    try:
        a.request_cancel()
        assert proc.wait(timeout=1)==-15
        assert eof.wait(4),'helper retained the worker stdout after its leader exited'
    finally:
        try:a.signal_owned_process_group(proc,force=True)
        except ProcessLookupError:pass
        reader.join(timeout=1);proc.stdout.close()
