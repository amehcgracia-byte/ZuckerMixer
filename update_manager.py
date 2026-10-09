"""Explicitly accepted GitHub updates; no source/recording files are modified."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
import uuid
import zipfile

REPO = 'amehcgracia-byte/ZuckerMixer'
API = f'https://api.github.com/repos/{REPO}/releases/latest'


def version_tuple(version):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(version))
    if not match:
        raise ValueError('Invalid release version')
    return tuple(map(int, match.groups()))


def select_release(release, current, platform):
    if release.get('draft') or release.get('prerelease'):
        return None
    version = str(release['tag_name']).removeprefix('v')
    if version_tuple(version) <= version_tuple(current):
        return None
    suffix = '.dmg' if platform == 'darwin' else '-Windows.zip'
    name = f'ZuckerMixer-{version}{suffix}'
    assets = {asset['name']: asset for asset in release.get('assets', [])}
    if name not in assets or 'SHA256SUMS.txt' not in assets:
        raise ValueError('The release does not contain a complete verified update')
    for asset in (assets[name], assets['SHA256SUMS.txt']):
        url = urllib.parse.urlsplit(asset['browser_download_url'])
        prefix = f'/{REPO}/releases/download/v{version}/'
        if url.scheme != 'https' or url.netloc != 'github.com' or not url.path.startswith(prefix):
            raise ValueError('Unexpected update download location')
    return dict(version=version, asset=assets[name], checksums=assets['SHA256SUMS.txt'])


SYSTEM_CA_FILES = ('/etc/ssl/cert.pem',)


def ca_bundle():
    """CA file for GitHub HTTPS, or None for OpenSSL's defaults.

    The frozen python.org runtime looks for certificates inside its own
    framework folder, which is empty on most Macs. Prefer the bundled certifi
    file and fall back to the macOS system bundle.
    """
    try:
        import certifi
        path = certifi.where()
        if os.path.isfile(path):
            return path
    except ImportError:
        pass
    return next((path for path in SYSTEM_CA_FILES if os.path.isfile(path)), None)


def request(url):
    context = ssl.create_default_context(cafile=ca_bundle())
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'ZuckerMixer-Updater', 'Accept': 'application/vnd.github+json'
    }), timeout=20, context=context)


def checksum_for(text, name):
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('*') == name and re.fullmatch('[a-fA-F0-9]{64}', parts[0]):
            return parts[0].lower()
    raise ValueError('Missing SHA-256 checksum for the update')


def extract_windows(package, destination):
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if sum(item.file_size for item in members) > 2*1024**3:
            raise ValueError('Update archive is too large')
        for item in members:
            name = item.filename.replace('\\', '/')
            parts = name.split('/')
            if parts[0] != 'ZuckerMixer' or '..' in parts or ':' in name or name.startswith('/'):
                raise ValueError('Unsafe update archive path')
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Update archive contains a symbolic link')
        archive.extractall(destination)
    return destination/'ZuckerMixer'


def copy_app_bundle(source: Path, destination: Path, platform: str) -> None:
    # macOS stores signatures for non-Mach-O resources in extended attributes.
    # copytree drops those attributes; ditto preserves signed bundle metadata.
    if platform == 'darwin':
        subprocess.run(['ditto', '--rsrc', '--extattr', str(source), str(destination)], check=True, capture_output=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def validate_app(folder, version, platform):
    if platform == 'darwin':
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(folder)], check=True, capture_output=True)
        metadata = folder/'Contents/Resources/build/build_metadata.json'
        exe = folder/'Contents/MacOS/ZuckerMixer'
    else:
        metadata = folder/'_internal/build/build_metadata.json'
        exe = folder/'ZuckerMixer.exe'
    if not exe.is_file() or json.loads(metadata.read_text()).get('app_version') != version:
        raise ValueError('Update package version does not match the release')
    subprocess.run([str(exe), '--self-check'], check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Updater:
    def __init__(self, version, state_root, platform=None, frozen=None):
        self.version = version
        self.root = Path(state_root)/'updates'
        self.platform = platform or sys.platform
        self.frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
        self.lock = threading.Lock()
        self.state = {'status': 'idle', 'current_version': version}
        self.release = None
        # Completed helper copies/downloads are temporary; preserve rollback apps.
        if self.root.exists():
            for previous in self.root.glob('update-*'):
                try:
                    result = json.loads((previous/'result.json').read_text())
                    if result.get('status') in ('installed', 'error'):
                        shutil.rmtree(previous)
                except (OSError, ValueError):
                    pass

    def status(self):
        with self.lock:
            return dict(self.state)

    def set(self, **values):
        with self.lock:
            self.state.update(values)

    def check(self):
        with self.lock:
            if self.state['status'] in ('checking', 'downloading', 'verifying', 'installing'):
                return dict(self.state)
            self.state = {'status': 'checking', 'current_version': self.version}
        threading.Thread(target=self._check, daemon=True).start()
        return self.status()

    def _check(self):
        try:
            with request(API) as response:
                release = json.loads(response.read(2*1024*1024))
            self.release = select_release(release, self.version, self.platform)
            self.set(status='available' if self.release else 'current',
                     version=self.release['version'] if self.release else self.version)
        except Exception as exc:
            self.set(status='check_error', error=str(exc))

    def install(self, active_jobs, finished):
        with self.lock:
            if not self.frozen:
                return {'status': 'error', 'error': 'Updates can only be installed in the desktop application'}
            if self.state['status'] != 'available' or not self.release:
                return {'status': 'error', 'error': 'No verified update is available'}
            if active_jobs():
                return {'status': 'error', 'error': 'Finish the current render or analysis before updating'}
            self.state.update(status='downloading', progress=0)
        threading.Thread(target=self._prepare, args=(active_jobs, finished), daemon=True).start()
        return self.status()

    def _prepare(self, active_jobs, finished):
        stage = None
        work = None
        mounted = None
        try:
            release = self.release
            asset = release['asset']; version = release['version']
            self.root.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix='update-', dir=self.root))
            current = Path(sys.executable).resolve()
            target = current.parents[2] if self.platform == 'darwin' else current.parent
            if self.platform == 'darwin' and target.suffix != '.app':
                raise ValueError('Cannot locate the installed app bundle')
            if not os.access(target.parent, os.W_OK):
                raise PermissionError('Install ZuckerMixer in a folder your account can write to before using automatic updates')
            required = max(2*1024**3, int(asset['size'])*8)
            if min(shutil.disk_usage(work).free, shutil.disk_usage(target.parent).free) < required:
                raise OSError('Not enough free disk space to safely install and keep a rollback copy')
            with request(release['checksums']['browser_download_url']) as response:
                expected = checksum_for(response.read(128*1024).decode(), asset['name'])
            package = work/asset['name']; digest = hashlib.sha256(); count = 0
            with request(asset['browser_download_url']) as response, package.open('wb') as handle:
                while True:
                    block = response.read(1024*1024)
                    if not block:
                        break
                    handle.write(block); digest.update(block); count += len(block)
                    self.set(progress=min(100, int(count/max(1, int(asset['size']))*100)))
            if count != int(asset['size']) or digest.hexdigest() != expected:
                raise ValueError('Update download failed its SHA-256 verification')
            self.set(status='verifying', progress=None)
            if self.platform == 'darwin':
                mounted = work/'mount'; mounted.mkdir()
                subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mounted), str(package)], check=True, capture_output=True)
                source = mounted/'ZuckerMixer.app'
            else:
                source = extract_windows(package, work/'extracted')
            validate_app(source, version, self.platform)
            token = uuid.uuid4().hex
            stage = target.with_name(f'.{target.stem}-update-{token}{target.suffix}')
            copy_app_bundle(source, stage, self.platform)
            helper = work/target.name
            copy_app_bundle(target, helper, self.platform)
            from update_helper import executable
            receipt = work/'startup.json'
            manifest = {'target': str(target), 'stage': str(stage),
                        'backup': str(target.with_name(f'{target.stem}-previous-{self.version}-{token}{target.suffix}')),
                        'parent_pid': os.getpid(), 'receipt': str(receipt), 'version': version}
            path = work/'manifest.json'; path.write_text(json.dumps(manifest))
            if active_jobs():
                raise RuntimeError('A job started during download; finish it and retry the update')
            self.set(status='installing', progress=None)
            process = subprocess.Popen([str(executable(helper)), '--update-helper', str(path)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=(os.name != 'nt'),
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            finished(process)
        except Exception as exc:
            if stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            self.set(status='error', error=str(exc), progress=None)
        finally:
            if mounted:
                subprocess.run(['hdiutil', 'detach', str(mounted)], capture_output=True)
            if work and self.status()['status'] == 'error':
                shutil.rmtree(work, ignore_errors=True)
