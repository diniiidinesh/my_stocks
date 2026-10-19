"""Loopback/SSH-tunnel browser service. Market data in, paper ledger only."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time as clock
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click

from nse_alert.paper import IST, PaperBook

logger = logging.getLogger(__name__)
WEB = Path(__file__).with_name('paper_web')


class PaperServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, book, kite=None):
        super().__init__(address, PaperHandler)
        self.book, self.kite = book, kite
        self.csrf = secrets.token_urlsafe(32)
        self.history_lock = threading.Lock()
        self.history: dict[tuple[str, str], tuple[float, list]] = {}
        self.last_history_request = 0.0

    def candles(self, symbol, interval='minute'):
        periods = {'minute': (5, 60), '5minute': (20, 300), '15minute': (45, 900),
                   '60minute': (120, 3600), 'day': (500, 86400)}
        if symbol not in self.book.market or interval not in periods:
            raise ValueError('Unknown stock or unsupported timeframe')
        days, seconds = periods[interval]
        key = (symbol, interval)
        cached = None
        if self.kite:
            with self.history_lock:
                cached = self.history.get(key)
                if not cached or clock.monotonic() - cached[0] > 60:
                    now = datetime.now(IST)
                    try:
                        clock.sleep(max(0, .5 - (clock.monotonic() - self.last_history_request)))
                        self.last_history_request = clock.monotonic()
                        rows = self.kite.historical_data(self.book.market[symbol]['token'],
                            now - timedelta(days=days), now, interval)
                        candles = [dict(time=int(c['date'].timestamp()), o=c['open'],
                            h=c['high'], l=c['low'], c=c['close'], v=c.get('volume')) for c in rows][-700:]
                        self.history[key] = (clock.monotonic(), candles)
                        # Bound chart caches on the small VM; only requested charts are retained.
                        while len(self.history) > 12:
                            del self.history[next(iter(self.history))]
                    except Exception:
                        logger.warning('Historical chart unavailable for %s', symbol)
                        if not cached:
                            raise ValueError('Historical data unavailable; check Kite access and daily login')
                cached = self.history.get(key)
        with self.book.lock:
            merged = {c['time']: c.copy() for c in (cached[1] if cached else [])}
            live = {}
            for c in self.book.market[symbol]['candles']:
                # Intraday buckets start at 09:15 IST, matching exchange candles.
                dt = datetime.fromtimestamp(c['time'], IST)
                anchor = int(dt.replace(hour=0 if interval == 'day' else 9,
                    minute=0 if interval == 'day' else 15, second=0, microsecond=0).timestamp())
                # After-hours prints must not extend the regular cash-session chart.
                if self.book.live and not (9*60+15 <= dt.hour*60+dt.minute < 15*60+30):
                    continue
                bucket = anchor + ((c['time'] - anchor) // seconds) * seconds
                if bucket not in live:
                    live[bucket] = dict(c, time=bucket)
                else:
                    b = live[bucket]
                    b.update(c=c['c'], h=max(b['h'], c['h']), l=min(b['l'], c['l']))
            current = self.book.now().astimezone(IST)
            current_anchor = int(current.replace(hour=0 if interval == 'day' else 9,
                minute=0 if interval == 'day' else 15, second=0, microsecond=0).timestamp())
            current_bucket = current_anchor + ((int(current.timestamp()) - current_anchor) // seconds) * seconds
            for bucket, c in live.items():
                if bucket in merged and bucket != current_bucket:
                    continue
                if bucket in merged:
                    old = merged[bucket]
                    # Historical volume is authoritative and refreshed each minute.
                    merged[bucket] = dict(old, c=c['c'], h=max(c['h'], old['h']), l=min(c['l'], old['l']))
                else:
                    merged[bucket] = dict(c, v=None)
        return [merged[k] for k in sorted(merged)][-700:]


class PaperHandler(BaseHTTPRequestHandler):
    server: PaperServer

    def log_message(self, fmt, *args):
        # URLs and bodies may contain private journal text; don't log requests.
        pass

    def reply(self, status, body, mime='application/json'):
        raw = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        host = self.headers.get('Host', '')
        return host in {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Open through the documented localhost SSH tunnel'})
        url = urlsplit(self.path)
        try:
            if url.path == '/api/state':
                return self.reply(200, self.server.book.snapshot() | {'csrf': self.server.csrf})
            if url.path == '/api/chart':
                symbol = parse_qs(url.query).get('symbol', [''])[0]
                interval = parse_qs(url.query).get('interval', ['minute'])[0]
                return self.reply(200, {'candles': self.server.candles(symbol, interval), 'interval': interval})
            if url.path == '/api/export':
                return self.reply(200, self.server.book.snapshot(full=True))
            if url.path in {'/', '/index.html', '/app.js', '/indicators.js', '/chart.js'}:
                path = WEB / (url.path.lstrip('/') if url.path.endswith('.js') else 'index.html')
                mime = 'application/javascript; charset=utf-8' if path.suffix == '.js' else 'text/html; charset=utf-8'
                return self.reply(200, path.read_bytes(), mime)
            self.reply(404, {'error': 'Not found'})
        except (ValueError, TypeError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            logger.exception('Paper data request failed')
            self.reply(500, {'error': 'Paper service error; check VM logs'})

    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
            return self.reply(403, {'error': 'Same-origin browser request required'})
        if not secrets.compare_digest(self.headers.get('X-Paper-CSRF', ''), self.server.csrf):
            return self.reply(403, {'error': 'Reload the page to refresh your session'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16000:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            book = self.server.book
            path = urlsplit(self.path).path
            if path == '/api/orders':
                result = book.place(body)
            elif path == '/api/action':
                book.action(body.get('id'))
                result = {'ok': True}
            elif path == '/api/protection':
                book.protection(body.get('id'), body.get('stop'), body.get('target'))
                result = {'ok': True}
            elif path == '/api/squareoff':
                book.squareoff()
                result = {'ok': True}
            elif path == '/api/settings':
                book.configure(body)
                result = {'ok': True}
            elif path == '/api/drawing':
                book.drawing(body.get('symbol'), body)
                result = {'ok': True}
            else:
                return self.reply(404, {'error': 'Not found'})
            self.reply(200, result)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            logger.exception('Paper action failed')
            self.reply(500, {'error': 'Paper service error; check VM logs'})


@click.command('paper')
@click.option('--feed', type=click.Choice(['mock', 'kite']), default='mock', show_default=True)
@click.option('--host', type=click.Choice(['127.0.0.1', '0.0.0.0']), default='127.0.0.1')
@click.option('--port', type=click.IntRange(1024, 65535), default=8787, show_default=True)
def paper_cmd(feed, host, port):
    """Run the browser paper desk. Never places a real broker order."""
    from nse_alert.config import Settings
    from nse_alert.feed import KiteFeed, MockFeed
    from nse_alert.ipv4 import force_ipv4
    from nse_alert.lock import acquire_single_instance, release_single_instance
    from nse_alert.session import hhmm_to_time
    from nse_alert.universe import _kite_client, build_universe

    settings = Settings()
    live = feed == 'kite'
    kite = None
    state_dir = settings.state_dir / ('paper-live' if live else 'paper-mock')
    lock = acquire_single_instance(state_dir)
    price_feed = server = book = worker = None
    stop = threading.Event()
    try:
        if live:
            if not settings.kite_api_key or not settings.kite_access_token:
                raise click.ClickException('Run the existing daily Kite login first')
            if settings.kite_force_ipv4:
                force_ipv4()
            kite = _kite_client(settings.kite_api_key, settings.kite_access_token)
            try:
                kite.profile()
            except Exception:
                raise click.ClickException('Kite login validation failed; check connectivity and daily token') from None
        instruments = build_universe(min_turnover_cr=settings.min_turnover_cr,
            min_price=settings.min_price, kite_api_key=settings.kite_api_key if live else '',
            kite_access_token=settings.kite_access_token if live else '',
            custom_universe_file=settings.custom_universe_file if live else '',
            mock=not live, state_dir=settings.state_dir)
        if not instruments or len(instruments) > 3000:
            raise click.ClickException('Paper feed requires 1–3000 stocks; restrict CUSTOM_UNIVERSE_FILE')
        book = PaperBook(state_dir / 'ledger.sqlite3', instruments, live=live,
                         cutoff=hhmm_to_time(settings.squareoff_hhmm))
        server = PaperServer((host, port), book, kite)
        def health(_message):
            with book.lock:
                book.feed_error = 'Kite feed interrupted. Quotes expire after 15 seconds. Check connection and daily login.'
        def on_tick(symbol, price, **kwargs):
            with book.lock:
                if stop.is_set():
                    return
                book.feed_error = ''
                book.tick(symbol, price, **kwargs)
        price_feed = KiteFeed(api_key=settings.kite_api_key, access_token=settings.kite_access_token,
            instruments=instruments, on_tick=on_tick, on_health_alert=health,
            on_feed_dead=health) if live else MockFeed(instruments, on_tick, max_ticks=None, interval_sec=1)
        def maintenance():
            while not stop.wait(1):
                try:
                    book.housekeeping()
                except Exception:
                    logger.exception('Paper session maintenance failed')
                    health('maintenance')
        worker = threading.Thread(target=maintenance, name='paper-session-clock', daemon=True)
        worker.start()
        price_feed.start()
        click.echo(f'Paperkite: http://localhost:{port} · {"LIVE prices, PAPER trades" if live else "MOCK data"}')
        click.echo('Use an SSH tunnel; keep this port private. Existing alert watcher is independent.')
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if price_feed:
            price_feed.stop()
        if server:
            server.server_close()
        if worker:
            worker.join(timeout=3)
        if book:
            with book.lock:
                book.db.close()
        release_single_instance(lock)
