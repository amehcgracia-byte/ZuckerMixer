import re
from pathlib import Path

import pytest

import jam_app as app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'TUTORIAL_STATE_PATH', tmp_path / 'tutorial.json')
    return app.app.test_client()


def test_tutorial_answer_is_asked_once_and_persisted(client):
    assert client.get('/api/tutorial').get_json() == {'answer': None}
    assert client.post('/api/tutorial', json={'answer': 'maybe'}).status_code == 400
    assert client.get('/api/tutorial').get_json() == {'answer': None}
    assert client.post('/api/tutorial', json={'answer': 'no'}).get_json() == {'answer': 'no'}
    assert client.get('/api/tutorial').get_json() == {'answer': 'no'}
    assert client.post('/api/tutorial', json={'answer': 'yes'}).status_code == 200
    assert client.get('/api/tutorial').get_json() == {'answer': 'yes'}


def test_corrupt_answer_is_asked_again(client):
    app.TUTORIAL_STATE_PATH.write_text('{"answer": "sure"}')
    assert client.get('/api/tutorial').get_json() == {'answer': None}


def test_every_tutorial_step_points_at_a_real_control():
    tour = Path('static/tutorial.js').read_text(encoding='utf-8')
    ui = Path('templates/index.html').read_text(encoding='utf-8') + Path('static/app.js').read_text(encoding='utf-8')
    selectors = re.findall(r"^\s*\['[^']+', '([^']+)',", tour, flags=re.M)
    assert len(selectors) >= 15
    for selector in selectors:
        for token in re.findall(r'[#.][\w-]+', selector):
            if token.startswith('#'):
                assert f'id="{token[1:]}"' in ui, f'tutorial step targets missing {token}'
            else:
                assert re.search(rf'class="[^"]*\b{re.escape(token[1:])}\b', ui) or f'"{token[1:]} ' in ui or f'`{token[1:]} ' in ui, f'tutorial step targets missing {token}'
    assert Path('static/tutorial-einstein.png').stat().st_size > 100_000
