from pathlib import Path
from unittest.mock import patch
import jam_mix_pipeline as p

def test_windows_package_finds_ffmpeg_without_system_install(tmp_path):
    binary=tmp_path/'ffmpeg-win-x86_64-v7.1.exe';binary.write_bytes(b'fixture')
    with patch.object(p,'FFMPEG_PATH',None),patch.object(p.sys,'_MEIPASS',str(tmp_path),create=True),patch.object(p.shutil,'which',return_value=None):
        assert p.resolve_ffmpeg()==str(binary)
