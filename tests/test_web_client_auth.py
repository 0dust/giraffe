"""Test authentication must not depend on TestClient's HTTP backend internals."""
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import testclient

from giraffe.web import create_app


@pytest.mark.parametrize('keyword_app', [False, True])
@pytest.mark.parametrize('headers,status', [(None, 200), ({'x-giraffe-token': 'wrong'}, 401)])
def test_direct_client_auth_without_transport_app(tmp_path, monkeypatch, keyword_app, headers, status):
    original = testclient._TestClientTransport

    class TransportWithoutApp(original.__bases__[0]):
        # TestClient backends need not expose the application to httpx.Client.
        def __init__(self, *args, **kwargs):
            self.delegate = original(*args, **kwargs)

        def handle_request(self, request):
            return self.delegate.handle_request(request)

        def close(self):
            self.delegate.close()

    monkeypatch.setattr(testclient, '_TestClientTransport', TransportWithoutApp)
    app = create_app(tmp_path)
    args, kwargs = ((), {'app': app}) if keyword_app else ((app,), {})
    with TestClient(*args, **kwargs, base_url='http://127.0.0.1:8787', headers=headers) as client:
        response = client.get('/api/storage')
        assert response.status_code == status, response.text
        if status == 200:
            assert response.json()['runs_bytes'] == 0
            assert client.headers['X-Giraffe-Token'] == app.state.giraffe_token


async def test_asgi_clients_keep_automatic_auth(tmp_path):
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url='http://127.0.0.1:8787') as client:
        response = await client.get('/api/storage')
        assert response.status_code == 200
        assert response.json()['runs_bytes'] == 0


class TestAnonymousClients:
    @pytest.fixture
    def web_auto_auth(self):
        return False

    def test_direct_client_requires_real_credentials(self, tmp_path):
        app = create_app(tmp_path)
        with TestClient(app, base_url='http://127.0.0.1:8787') as client:
            assert client.get('/api/storage').status_code == 401
            assert client.get('/api/storage', headers={
                'X-Giraffe-Token': app.state.giraffe_token}).status_code == 200
