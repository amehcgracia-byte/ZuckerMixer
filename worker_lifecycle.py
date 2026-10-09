"""Keep isolated audio workers tied to their desktop launcher."""
import ctypes
import os
import signal
import subprocess
import threading


def force_worker_tree_exit():
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill','/PID',str(os.getpid()),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,check=False)
        finally:
            os._exit(143)
    else:
        os.killpg(os.getpid(),signal.SIGKILL)


def start_parent_watch(parent_pid, on_parent_exit=None, interval=.25):
    """Return a stop event; activate only inside the app's isolated worker."""
    parent_pid=int(parent_pid or 0)
    if parent_pid<=1 or parent_pid==os.getpid():
        return None
    if os.name!='nt' and os.getpgrp()!=os.getpid():
        return None  # Never signal a shell, test runner or unrelated group.
    stopped=threading.Event()
    def watch():
        handle=None
        kernel=None
        if os.name=='nt':
            kernel=ctypes.windll.kernel32
            kernel.OpenProcess.restype=ctypes.c_void_p
            handle=kernel.OpenProcess(0x00100000,False,parent_pid)
        try:
            while not stopped.is_set():
                gone=(not handle or kernel.WaitForSingleObject(ctypes.c_void_p(handle),0)==0) if kernel else os.getppid()!=parent_pid
                if gone:
                    # Status I/O may itself be stuck. Termination gets its own
                    # deadline, independent of the main DSP and callback.
                    timer=threading.Timer(1.,force_worker_tree_exit);timer.daemon=True;timer.start()
                    try:
                        if on_parent_exit:on_parent_exit()
                    finally:
                        force_worker_tree_exit()
                    return
                stopped.wait(interval)
        finally:
            if handle:kernel.CloseHandle(ctypes.c_void_p(handle))
    threading.Thread(target=watch,name='worker-parent-watch',daemon=True).start()
    return stopped
