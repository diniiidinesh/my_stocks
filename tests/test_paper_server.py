import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from nse_alert.paper_server import PaperServer


@pytest.fixture
def server(desk):
    book, _, _ = desk
    server = PaperServer(('127.0.0.1', 0), book)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    worker.join(timeout=2)
    server.server_close()


# Reuse the accounting fixture, with real HTTP requests for the security boundary.
from test_paper import desk


def call(server, path, data=None, headers=None):
    url = f'http://127.0.0.1:{server.server_port}' + path
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers=headers or {})
    with urlopen(req, timeout=3) as response:
        return response.status, response.read()


def test_page_and_json_and_no_secret_leak(server):
    assert call(server, '/')[0] == 200
    status, body = call(server, '/api/state')
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot['mode'] == 'live'
    assert snapshot['csrf']
    assert b'access_token' not in body and b'api_key' not in body


def test_mutations_require_origin_and_csrf(server):
    base = f'http://127.0.0.1:{server.server_port}'
    for headers in [{}, {'Origin': 'https://evil.example', 'X-Paper-CSRF': server.csrf},
                    {'Origin': base, 'X-Paper-CSRF': 'wrong'}]:
        with pytest.raises(HTTPError) as exc:
            call(server, '/api/settings', {'loss_cap': 500}, headers)
        assert exc.value.code == 403
    headers = {'Origin': base, 'X-Paper-CSRF': server.csrf, 'Content-Type': 'application/json'}
    assert call(server, '/api/settings', {'loss_cap': 500}, headers)[0] == 200
    assert server.book.data['loss_cap'] == 500


def test_dns_rebinding_host_rejected(server):
    with pytest.raises(HTTPError) as exc:
        call(server, '/api/state', headers={'Host': 'evil.example'})
    assert exc.value.code == 403


def test_order_api_takes_server_price_not_client_price(server):
    headers = {'Origin': f'http://127.0.0.1:{server.server_port}',
               'X-Paper-CSRF': server.csrf, 'Content-Type': 'application/json'}
    status, body = call(server, '/api/orders', dict(symbol='AAA', qty=10,
        type='market', entry=1, stop=97, target=110), headers)
    assert status == 200
    assert json.loads(body)['fill'] == 100


def test_chart_combines_historical_and_live_candle(server):
    class FakeKite:
        def historical_data(self, *args):
            from datetime import datetime
            from nse_alert.paper import IST
            return [dict(date=datetime(2026, 10, 19, 10, 0, tzinfo=IST),
                         open=98, high=102, low=97, close=100)]
    server.kite = FakeKite()
    server.book.tick('AAA', 103)
    candles = server.candles('AAA')
    assert candles[-1]['o'] == 98
    assert candles[-1]['h'] == 103
    assert candles[-1]['l'] == 97
    assert candles[-1]['c'] == 103
