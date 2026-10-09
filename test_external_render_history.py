from unittest.mock import patch
import jam_app as app

def test_external_render_is_visible_and_not_duplicated(tmp_path):
    internal=tmp_path/'internal';internal.mkdir()
    external=tmp_path/'external';external.mkdir()
    mp3=external/'01 - Song_v1_20261003_120000.mp3';mp3.write_bytes(b'fixture')
    history={'1':[{'path':str(mp3),'song_id':1,'version':1,'created':'20261003_120000','lufs':-12.6}]}
    with patch.object(app,'out_dir',return_value=internal),patch.object(app,'load_json',return_value=history):
        rows=app.disk_versions()
    assert len(rows[1])==1
    assert rows[1][0]['path']==str(mp3)
    assert rows[1][0]['lufs']==-12.6

def test_missing_registered_file_is_not_reported_as_completed(tmp_path):
    history={'1':[{'path':str(tmp_path/'missing.mp3'),'version':1}]}
    with patch.object(app,'out_dir',return_value=tmp_path),patch.object(app,'load_json',return_value=history):
        assert app.disk_versions()=={}
