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


def test_native_timeframes_include_volume_and_use_separate_cache(server):
    calls = []
    class FakeKite:
        def historical_data(self, token, start, end, interval):
            from datetime import datetime
            from nse_alert.paper import IST
            calls.append(interval)
            stamp = datetime(2026, 10, 19, 0 if interval == 'day' else 9, 0 if interval == 'day' else 15, tzinfo=IST)
            return [dict(date=stamp,
                         open=98, high=102, low=97, close=100, volume=12345)]
    server.kite = FakeKite()
    assert server.candles('AAA', '15minute')[0]['v'] == 12345
    assert server.candles('AAA', 'day')[0]['v'] == 12345
    server.candles('AAA', '15minute')
    assert calls == ['15minute', 'day']
    with pytest.raises(ValueError):
        server.candles('AAA', 'unsupported')


def test_ema_trail_raises_paper_stop_once_per_closed_candle(server, desk):
    from datetime import datetime, timedelta
    from nse_alert.paper import IST
    book, clock, _ = desk
    calls = []
    class FakeKite:
        def historical_data(self, token, start, end, interval):
            calls.append(interval)
            first = datetime(2026, 10, 19, 9, 15, tzinfo=IST)
            # Seven closed candles at 104, then the forming 11:00 candle at 50.
            return [dict(date=first + timedelta(minutes=15 * i), open=c, high=c, low=c, close=c, volume=1)
                    for i, c in enumerate([104] * 7 + [50])]
    server.kite = FakeKite()
    server.trail_ema = (5, 0.3)
    o = book.place(dict(symbol='AAA', qty=10, type='market', stop=97, target=110))
    clock[0] = datetime(2026, 10, 19, 11, 0, 30, tzinfo=IST)
    book.tick('AAA', 105)
    server.trail_stops()
    order = next(x for x in book.data['orders'] if x['id'] == o['id'])
    assert order['stop'] == 103.7
    assert 'EMA5' in order['trail']
    assert calls == ['15minute']
    server.trail_stops()
    assert calls == ['15minute']
    book.tick('AAA', 103.6)
    assert order['status'] == 'closed' and order['reason'] == 'Stop loss'


def test_ema_trail_off_by_default(server, desk):
    book, clock, _ = desk
    book.place(dict(symbol='AAA', qty=10, type='market', stop=97, target=110))
    server.trail_stops()
    assert book.data['orders'][0]['stop'] == 97


def test_indicator_scripts_are_served(server):
    for path in ['/indicators.js', '/chart.js']:
        status, content = call(server, path)
        assert status == 200
        assert len(content) > 100
