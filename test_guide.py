import re
from pathlib import Path

import pytest

import jam_app as app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'GUIDE_SEEN_PATH', tmp_path / 'guide_seen.json')
    monkeypatch.setitem(app.BUILD_METADATA, 'app_version', '2.8.10')
    return app.app.test_client()


def test_guide_is_unseen_until_marked_for_this_version(client, monkeypatch):
    first = client.get('/api/guide').get_json()
    assert first['unseen'] is True and first['version'] == '2.8.10'
    assert 'READ ME FIRST' in first['text']
    assert client.post('/api/guide/seen').status_code == 200
    assert client.get('/api/guide').get_json()['unseen'] is False
    monkeypatch.setitem(app.BUILD_METADATA, 'app_version', '2.8.11')
    assert client.get('/api/guide').get_json()['unseen'] is True


def test_missing_guide_never_prompts(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'GUIDE_PATH', tmp_path / 'missing.md')
    assert client.get('/api/guide').get_json() == {'text': '', 'version': '2.8.10', 'unseen': False}


def test_guide_is_bundled_and_english():
    spec = Path('zucker_mixer.spec').read_text(encoding='utf-8')
    assert '("README_Zucker_Mixer_App.md", ".")' in spec
    text = Path('README_Zucker_Mixer_App.md').read_text(encoding='utf-8')
    assert 'Open Anyway' in text and 'Run anyway' in text
    assert not re.search(r'Ajustes|Privacidad|Abrir igualmente|LÉEME', text)
