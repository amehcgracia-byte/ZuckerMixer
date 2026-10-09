import pytest

import jam_app as app


@pytest.fixture
def client():
    return app.app.test_client()


@pytest.mark.parametrize('host', ['127.0.0.1:5123', 'localhost:5123', 'localhost', '[::1]:5123'])
def test_app_window_requests_are_served(client, host):
    response = client.get('/api/performance', headers={'Host': host, 'Origin': f'http://{host}'})
    assert response.status_code == 200


def test_requests_without_origin_are_served(client):
    assert client.get('/api/performance', headers={'Host': '127.0.0.1:5123'}).status_code == 200


@pytest.mark.parametrize('host', ['evil.example', 'evil.example:5123', '127.0.0.1.evil.example:5123'])
def test_rebound_dns_host_is_rejected(client, host):
    assert client.get('/api/performance', headers={'Host': host}).status_code == 403


@pytest.mark.parametrize('origin', ['https://evil.example', 'http://127.0.0.1.evil.example', 'null', 'file://'])
def test_cross_site_post_is_rejected_before_handler(client, origin, monkeypatch):
    def handler_must_not_run(*_args, **_kwargs):
        raise AssertionError('cross-site request reached the cancel handler')
    monkeypatch.setattr(app, 'request_cancel', handler_must_not_run)
    response = client.post('/api/cancel', data='{}', content_type='text/plain',
                           headers={'Host': '127.0.0.1:5123', 'Origin': origin})
    assert response.status_code == 403
