import builtins,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch
import jam_app as app

def test_windows_cancellation_stops_only_owned_worker_tree():
    with patch.object(app.os,'name','nt'),patch.object(app.subprocess,'run') as run:
        app.signal_owned_process_group(SimpleNamespace(pid=12345),force=True)
        assert run.call_args.args[0]==['taskkill','/PID','12345','/T','/F']

def test_import_succeeds_without_posix_resource_module():
    script='''
import builtins
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name=='resource':raise ImportError('Windows fixture')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
import jam_app
assert jam_app.resource is None
'''
    subprocess.run([sys.executable,'-c',script],check=True,timeout=60)
