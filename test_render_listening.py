import json
from pathlib import Path
import pytest
import jam_app as a

@pytest.fixture
def preview(tmp_path,monkeypatch):
    path=tmp_path/'history.json';path.write_text('{}')
    monkeypatch.setattr(a,'HISTORY_PATH',path)
    monkeypatch.setattr(a,'open_render_location',lambda path:None)
    monkeypatch.setattr(a.pipeline,'SOURCE_DIR',tmp_path/'source')
    monkeypatch.setattr(a,'ensure_pipeline_state',lambda:pytest.fail('preview lookup must not analyze'))
    return a.app.test_client(),path,tmp_path


def test_promoted_renders_appear_incrementally_and_song_zero(preview):
    client,history,root=preview
    assert client.get('/api/render-previews').json['items']==[]
    first=root/'first.mp3';first.write_bytes(b'first completed MP3')
    history.write_text(json.dumps({'0':[{'path':str(first),'version':1,'created':'1'}]}))
    response=client.get('/api/render-previews').json
    assert len(response['items'])==1 and response['items'][0]['index']==0
    url=response['items'][0]['url'];assert client.get(url).data==first.read_bytes()
    second=root/'second.mp3';second.write_bytes(b'second completed MP3')
    history.write_text(json.dumps({'0':[{'path':str(first),'version':1}], '2':[{'path':str(second),'version':1}]}))
    assert len(client.get('/api/render-previews').json['items'])==2
    assert client.get(url,headers={'Range':'bytes=0-4'}).status_code==206
    assert client.get(url,headers={'Range':'bytes=0-4'}).data==b'first'


def test_playing_url_remains_pinned_to_its_render(preview):
    client,history,root=preview
    old=root/'old.mp3';old.write_bytes(b'old complete render');new=root/'new.mp3';new.write_bytes(b'new complete render')
    entries=[{'path':str(old),'version':1}];history.write_text(json.dumps({'1':entries}))
    url=client.get('/api/render-previews').json['items'][0]['url']
    entries.append({'path':str(new),'version':2});history.write_text(json.dumps({'1':entries}))
    assert client.get(url).data==b'old complete render'
    assert client.get('/api/render-previews').json['items'][0]['url']!=url
    assert client.get('/audio/rendered/1/unregistered').status_code==404


def test_empty_missing_and_invalid_entries_are_not_playable(preview):
    client,history,root=preview
    empty=root/'empty.mp3';empty.touch()
    history.write_text(json.dumps({'1':[{'path':str(empty)}],'2':[{'path':str(root/'missing.mp3')}],'3':[None]}))
    assert client.get('/api/render-previews').json['items']==[]
    assert client.post('/api/open-render-folder').status_code==404


def test_folder_open_uses_registered_output_and_platform(preview,monkeypatch):
    client,history,root=preview
    file=root/'complete.mp3';file.write_bytes(b'completed')
    history.write_text(json.dumps({'1':[{'path':str(file),'created':'1'}]}))
    calls=[];monkeypatch.setattr(a,'open_render_location',calls.append)
    assert client.post('/api/open-render-folder').status_code==200
    assert calls==[root]


def test_source_change_never_exposes_previous_history(preview,monkeypatch):
    client,history,root=preview
    old=root/'old.mp3';old.write_bytes(b'old')
    history.write_text(json.dumps({'1':[{'path':str(old)}]}));url=client.get('/api/render-previews').json['items'][0]['url']
    new_history=root/'other.json';new_history.write_text('{}');monkeypatch.setattr(a,'HISTORY_PATH',new_history)
    assert client.get(url).status_code==404
