"""Real HTTP regressions for project identity and the local browser boundary."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from cms.ui import _MemoryCache, make_handler


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    for root in (first, second):
        root.mkdir()
        (root / 'app.py').write_text('value = 1\n')
    monkeypatch.setattr('cms.app._save_workspace_root', lambda root: None)
    handler = make_handler(first, _MemoryCache(first / '.memory/graph.json'))
    monkeypatch.setattr(handler, '_kick_build', lambda self, full: True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def request(path, payload=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request('GET' if payload is None else 'POST', path,
                           body=None if payload is None else json.dumps(payload),
                           headers=headers or {})
        response = connection.getresponse()
        request.last_headers = dict(response.getheaders())
        result = response.status, response.read()
        connection.close()
        return result

    session = json.loads(request('/api/session')[1])
    headers = {'Content-Type': 'application/json', 'X-Atlas-Session': session['token'],
               'X-Atlas-Project': session['project']}
    yield request, headers, first, second
    server.shutdown()
    server.server_close()


def test_foreign_origin_and_host_cannot_read_or_write(viewer):
    request, headers, first, _ = viewer
    assert request('/api/meta', headers={'Host': 'attacker.example'})[0] == 403
    assert request('/api/session', headers={'Origin': 'https://attacker.example'})[0] == 403
    assert request('/api/notes', {'text': 'foreign'}, {**headers, 'Origin': 'https://attacker.example'})[0] == 403
    assert not (first / '.memory/notes.json').exists()


def test_json_session_and_body_bounds_are_enforced(viewer):
    request, headers, _, _ = viewer
    assert request('/api/notes', {}, {**headers, 'Content-Type': 'text/plain'})[0] == 415
    assert request('/api/notes', {}, {**headers, 'X-Atlas-Session': ''})[0] == 403
    assert request('/api/notes', {}, {**headers, 'Content-Length': '1048577'})[0] == 413
    assert request('/api/notes', [], headers)[0] == 400


def test_old_tab_cannot_write_into_new_project(viewer):
    request, old_headers, first, second = viewer
    assert request('/api/switch-root', {'path': str(second)}, old_headers)[0] == 200
    assert request('/api/notes', {'text': 'from the old page'}, old_headers)[0] == 409
    assert request('/api/meta', headers=old_headers)[0] == 409
    assert not (second / '.memory/notes.json').exists()
    new_session = json.loads(request('/api/session')[1])
    assert new_session['project'] != old_headers['X-Atlas-Project']
    assert json.loads(request('/api/meta')[1])['root'] == str(second)


@pytest.mark.parametrize('payload_size', [32, 8192])
def test_rejected_post_body_leaves_a_readable_error_response(viewer, payload_size):
    request, old_headers, _, second = viewer
    assert request('/api/switch-root', {'path': str(second)}, old_headers)[0] == 200
    # Each call opens a fresh real socket; repeated rejection must deliver the
    # error body, not reset the connection while the client's body is arriving.
    for _ in range(40):
        status, body = request('/api/notes', {'text': 'x' * payload_size}, old_headers)
        assert status == 409
        assert request.last_headers['Connection'] == 'close'
        assert json.loads(body)['code'] == 'project_changed'
    assert not (second / '.memory/notes.json').exists()


def test_every_page_loads_session_binding(viewer):
    request, _, _, _ = viewer
    for path in ('/', '/ideas', '/library', '/constellation', '/setup', '/sentinel'):
        status, page = request(path)
        assert status == 200
        assert b'window.ATLAS_SESSION=' in page and b'/assets/client.js' in page
