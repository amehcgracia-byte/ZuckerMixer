from pathlib import Path
import hashlib
import io
import json
import zipfile
import pytest
import update_manager as u
import update_helper as h


def release(version='2.8.3', platform='win32'):
    name=f'ZuckerMixer-{version}'+('.dmg' if platform=='darwin' else '-Windows.zip')
    return {'tag_name':'v'+version,'draft':False,'prerelease':False,'assets':[
        {'name':n,'size':100,'browser_download_url':f'https://github.com/{u.REPO}/releases/download/v{version}/{n}'}
        for n in (name,'SHA256SUMS.txt')]}

@pytest.mark.parametrize('platform', ['darwin','win32'])
def test_new_release_and_numeric_versions(platform):
    assert u.select_release(release(platform=platform),'2.8.2',platform)['version']=='2.8.3'
    assert u.select_release(release('2.8.2',platform),'2.8.2',platform) is None
    assert u.select_release(release('2.8.1',platform),'2.8.2',platform) is None
    assert u.version_tuple('2.8.10') > u.version_tuple('2.8.9')

@pytest.mark.parametrize('change', ['draft','prerelease','missing','host','version'])
def test_reject_incomplete_or_untrusted_release(change):
    r=release()
    if change in ('draft','prerelease'):
        r[change]=True; assert u.select_release(r,'2.8.2','win32') is None;return
    if change=='missing':r['assets'].pop()
    if change=='host':r['assets'][0]['browser_download_url']='https://example.com/app.zip'
    if change=='version':r['tag_name']='vnext'
    with pytest.raises((ValueError,KeyError)):u.select_release(r,'2.8.2','win32')


def test_checksums():
    digest=hashlib.sha256(b'audio').hexdigest()
    assert u.checksum_for(digest+'  app.zip\n','app.zip')==digest
    with pytest.raises(ValueError):u.checksum_for('broken app.zip','app.zip')

@pytest.mark.parametrize('name', ['../escape','ZuckerMixer/../../escape','ZuckerMixer/C:/escape','/ZuckerMixer/escape','ZuckerMixer/..\\escape'])
def test_zip_paths_rejected(tmp_path,name):
    p=tmp_path/'update.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr(name,b'bad')
    with pytest.raises(ValueError):u.extract_windows(p,tmp_path/'extract')
    assert not (tmp_path/'escape').exists()


def test_valid_windows_zip_and_symlinks(tmp_path):
    p=tmp_path/'update.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr('ZuckerMixer/ZuckerMixer.exe',b'MZ')
    assert (u.extract_windows(p,tmp_path/'good')/'ZuckerMixer.exe').read_bytes()==b'MZ'
    with zipfile.ZipFile(p,'w') as z:
        info=zipfile.ZipInfo('ZuckerMixer/link');info.external_attr=(0o120777<<16);z.writestr(info,'/outside')
    with pytest.raises(ValueError):u.extract_windows(p,tmp_path/'bad')


def manifest(tmp_path):
    target=tmp_path/'ZuckerMixer';stage=tmp_path/'.ZuckerMixer-stage'
    target.mkdir();stage.mkdir()
    (target/'ZuckerMixer.exe').write_text('old');(stage/'ZuckerMixer.exe').write_text('new')
    return dict(target=str(target),stage=str(stage),backup=str(tmp_path/'previous'),receipt=str(tmp_path/'startup.json'),parent_pid=123,version='2.8.3')

class Process:
    def __init__(self,code=None):self.code=code
    def poll(self):return self.code
    def terminate(self):self.code=-1
    def wait(self,timeout):return self.code


def test_swap_waits_and_receipt_confirms_startup(tmp_path):
    m=manifest(tmp_path);events=[]
    def wait(pid):
        events.append(pid);assert Path(m['target']).joinpath('ZuckerMixer.exe').read_text()=='old'
    def start(folder,receipt=None):
        assert (folder/'ZuckerMixer.exe').read_text()=='new'
        receipt.write_text(json.dumps({'version':'2.8.3'}));return Process()
    result=h.apply_update(m,wait=wait,start=start)
    assert result['status']=='installed' and events==[123]
    assert not Path(m['backup']).exists()
    assert result['backup'] is None

@pytest.mark.parametrize('failure',['exit','timeout','wrong_version'])
def test_rollback_and_relaunch_old_app(tmp_path,failure):
    m=manifest(tmp_path);calls=[]
    def start(folder,receipt=None):
        calls.append((folder/'ZuckerMixer.exe').read_text())
        if receipt and failure=='wrong_version':receipt.write_text('{"version":"2.0.0"}')
        return Process(1 if failure=='exit' else None)
    with pytest.raises((RuntimeError,TimeoutError)):
        h.apply_update(m,wait=lambda pid:None,start=start,receipt_timeout=.01 if failure=='timeout' else 1)
    assert calls==['new','old']
    assert Path(m['target']).joinpath('ZuckerMixer.exe').read_text()=='old'


def test_parent_not_closed_never_replaces_target(tmp_path):
    m=manifest(tmp_path)
    def wait(pid):raise TimeoutError('still running')
    with pytest.raises(TimeoutError):h.apply_update(m,wait=wait)
    assert Path(m['target']).joinpath('ZuckerMixer.exe').read_text()=='old'


def test_offline_check_and_active_job_refusal(tmp_path,monkeypatch):
    updater=u.Updater('2.8.2',tmp_path,platform='win32',frozen=True)
    monkeypatch.setattr(u,'request',lambda url:(_ for _ in ()).throw(OSError('offline')))
    updater._check();assert updater.status()['status']=='check_error'
    updater.release=u.select_release(release(),'2.8.2','win32');updater.set(status='available')
    result=updater.install(lambda:True,lambda process:None)
    assert result['status']=='error' and updater.status()['status']=='available'


def test_bad_download_never_runs_helper(tmp_path,monkeypatch):
    updater=u.Updater('2.8.2',tmp_path,platform='win32',frozen=True)
    updater.release=u.select_release(release(),'2.8.2','win32');updater.set(status='downloading')
    target=tmp_path/'app';target.mkdir();exe=target/'ZuckerMixer.exe';exe.write_text('old')
    monkeypatch.setattr(u.sys,'executable',str(exe))
    def request(url):
        return io.BytesIO((hashlib.sha256(b'good').hexdigest()+'  '+updater.release['asset']['name']).encode() if url.endswith('.txt') else b'corrupt')
    monkeypatch.setattr(u,'request',request)
    updater._prepare(lambda:False,lambda process:pytest.fail('helper started for bad package'))
    assert updater.status()['status']=='error' and 'SHA-256' in updater.status()['error']
    assert exe.read_text()=='old'


def test_restart_is_independent_of_helper_process(tmp_path,monkeypatch):
    folder=tmp_path/'ZuckerMixer';folder.mkdir();calls=[]
    monkeypatch.setattr(h.subprocess,'Popen',lambda args,**kwargs:calls.append((args,kwargs)))
    h.launch(folder,tmp_path/'receipt.json')
    assert calls[0][1]['start_new_session']==(h.os.name!='nt')
    assert calls[0][0][1]=='--update-receipt'
