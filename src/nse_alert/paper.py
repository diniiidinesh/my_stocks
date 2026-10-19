"""Paper-only accounting. No broker order API is used by this module."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def rounded(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def charges(price: float, qty: int, sell: bool) -> dict[str, float]:
    """Illustrative NSE cash intraday rates, each component rounded to paise."""
    turnover = Decimal(str(price)) * qty
    brokerage = min(Decimal('20'), turnover * Decimal('.0003'))
    exchange = turnover * Decimal('.0000307')
    sebi = turnover * Decimal('.000001')
    ipft = turnover * Decimal('.000000001')
    parts = dict(brokerage=brokerage, exchange=exchange, sebi=sebi, ipft=ipft,
                 stt=turnover * (Decimal('.00025') if sell else Decimal('0')),
                 stamp=Decimal('0') if sell else turnover * Decimal('.00003'),
                 gst=(brokerage + exchange + sebi + ipft) * Decimal('.18'))
    out = {key: rounded(value) for key, value in parts.items()}
    out['total'] = rounded(sum(out.values()))
    return out


def number(value: object, *, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise ValueError('Enter a valid number')
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError('Enter a finite, non-negative number')
    return result


class PaperBook:
    def __init__(self, path: Path, instruments: list, *, live: bool = False,
                 now: Callable[[], datetime] | None = None, cutoff: time = time(15, 5)):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.lock = threading.RLock()
        self.now = now or (lambda: datetime.now(IST))
        self.live, self.cutoff = live, cutoff
        row = self.db.execute('SELECT body FROM account WHERE id=1').fetchone()
        self.data = json.loads(row[0]) if row else dict(capital=200000, cash=200000,
            loss_cap=0, slippage_bps=5, orders=[], days={}, drawings={}, halted=[])
        self.market = {i.symbol: dict(symbol=i.symbol, base=i.prev_close,
            price=i.last_price, token=i.instrument_token, updated=0, candles=[]) for i in instruments}
        missing = {o['symbol'] for o in self.data['orders'] if o['status'] == 'open'} - self.market.keys()
        if missing:
            self.db.close()
            raise ValueError('Open paper positions are missing from the universe; include: ' + ', '.join(sorted(missing)))
        self.alerts: list[dict] = []
        self.seen: set[tuple] = set()
        self.feed_error = ''
        self._last_body = ''
        self._save()

    def _save(self):
        body = json.dumps(self.data, allow_nan=False)
        if body == self._last_body:
            return
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO account VALUES (1, ?)',
                            (body,))
        self._last_body = body

    def day(self) -> str:
        return self.now().astimezone(IST).date().isoformat()

    def _day(self):
        return self.data['days'].setdefault(self.day(), dict(gross=0, paid=0,
            charges={}, closed=0, entries=0, finalized=False, eligible=True))

    def _paid(self, fees):
        day = self._day()
        day['paid'] = rounded(day['paid'] + fees['total'])
        for key, value in fees.items():
            if key != 'total':
                day['charges'][key] = rounded(day['charges'].get(key, 0) + value)

    def _fresh(self, symbol):
        q = self.market[symbol]
        return 0 <= self.now().timestamp() - q['updated'] <= 15 and q['updated'] > 0

    def _entry_allowed(self):
        clock = self.now().astimezone(IST)
        if self.live and (clock.weekday() >= 5 or not time(9, 15) <= clock.time() < self.cutoff):
            raise ValueError('New entries are closed for this intraday session')
        if self._day()['finalized']:
            raise ValueError('This session has already closed')
        if any(o['status'] == 'open' and o['day'] != self.day() for o in self.data['orders']):
            raise ValueError('Resolve overdue paper positions before new entries')
        if self.day() in self.data['halted']:
            raise ValueError('Daily loss cap reached; new entries are blocked for today')
        if self.data['loss_cap'] and self._net() <= -self.data['loss_cap']:
            self.data['halted'].append(self.day())
            self._save()
            raise ValueError('Daily loss cap reached')

    def _price(self, price, sell=False):
        return rounded(price * (1 + (-1 if sell else 1) * self.data['slippage_bps'] / 10000))

    def _net(self):
        day = self._day()
        net = day['gross'] - day['paid']
        for o in self.data['orders']:
            if o['status'] == 'open':
                q = self.market.get(o['symbol'])
                if q is None:
                    continue
                price = self._price(q['price'], True)
                net += (price - o['fill']) * o['qty'] - charges(price, o['qty'], True)['total']
        return rounded(net)

    def place(self, request: dict):
        with self.lock:
            self._entry_allowed()
            symbol = request.get('symbol')
            if symbol not in self.market or not self._fresh(symbol):
                raise ValueError('Wait for a fresh quote for this stock (maximum age 15 seconds)')
            qty = number(request.get('qty'), minimum=1)
            if not qty.is_integer() or qty > 1_000_000:
                raise ValueError('Quantity must be whole shares, at most 1,000,000')
            kind = request.get('type', 'market')
            if kind not in {'market', 'limit', 'stop'}:
                raise ValueError('Unsupported order type')
            entry = self.market[symbol]['price'] if kind == 'market' else number(request.get('entry'), minimum=.05)
            stop = number(request.get('stop'), minimum=.05)
            target = number(request.get('target'), minimum=.05)
            if not stop < entry < target:
                raise ValueError('Stop must be below entry and target above entry')
            o = dict(id=uuid.uuid4().hex, symbol=symbol, qty=int(qty), type=kind,
                     entry=entry, stop=stop, target=target, status='pending', day=self.day(),
                     created=self.now().isoformat(), note=str(request.get('note', ''))[:1000])
            self.data['orders'].append(o)
            self._process(o)
            self._save()
            return o.copy()

    def _process(self, o):
        if o['symbol'] not in self.market or not self._fresh(o['symbol']):
            return
        p = self.market[o['symbol']]['price']
        if o['status'] == 'pending':
            if o['day'] != self.day():
                o.update(status='cancelled', reason='Expired at end of session')
                return
            try:
                self._entry_allowed()
            except ValueError:
                o.update(status='cancelled', reason='Session closed or risk limit')
                return
            trigger = o['type'] == 'market' or (o['type'] == 'limit' and p <= o['entry']) or (o['type'] == 'stop' and p >= o['entry'])
            if not trigger:
                return
            fill = self._price(p)
            if o['type'] == 'limit':
                fill = min(fill, o['entry'])
            fees = charges(fill, o['qty'], False)
            cost = rounded(fill * o['qty'] + fees['total'])
            if not o['stop'] < fill < o['target'] or cost > self.data['cash']:
                o.update(status='rejected', reason='Insufficient cash or fill outside planned stop/target')
                return
            o.update(status='open', fill=fill, buy_charges=fees, filled_at=self.now().isoformat())
            self.data['cash'] = rounded(self.data['cash'] - cost)
            self._day()['entries'] += 1
            self._paid(fees)
        if o['status'] == 'open':
            if o.get('exit_due') or o['day'] != self.day():
                self._close(o, o.get('exit_due') or 'Overdue session exit')
            elif p <= o['stop']:
                self._close(o, 'Stop loss')
            elif p >= o['target']:
                self._close(o, 'Target')

    def _close(self, o, reason):
        if not self._fresh(o['symbol']):
            raise ValueError('Cannot fill an exit using a stale quote')
        price = self._price(self.market[o['symbol']]['price'], True)
        if reason == 'Target':
            price = max(price, o['target'])
        fees = charges(price, o['qty'], True)
        gross = rounded((price - o['fill']) * o['qty'])
        o.update(status='closed', exit=price, sell_charges=fees, reason=reason,
                 exited_at=self.now().isoformat(), net=rounded(gross-fees['total']-o['buy_charges']['total']))
        self.data['cash'] = rounded(self.data['cash'] + price * o['qty'] - fees['total'])
        day = self._day()
        day['gross'] = rounded(day['gross'] + gross)
        day['closed'] += 1
        self._paid(fees)
        if o['day'] != self.day():
            self.data['days'][o['day']]['eligible'] = False
            day['eligible'] = False

    def protection(self, ident, stop, target):
        with self.lock:
            o = next((o for o in self.data['orders'] if o['id'] == ident), None)
            if o is None or o['status'] != 'open':
                raise ValueError('Select an open paper position')
            if not self._fresh(o['symbol']):
                raise ValueError('Wait for a fresh quote before changing protection')
            stop, target = number(stop, minimum=.05), number(target, minimum=.05)
            if not stop < self.market[o['symbol']]['price'] < target:
                raise ValueError('Stop must be below the current price and target above it')
            o.update(stop=stop, target=target)
            self._save()

    def action(self, ident):
        with self.lock:
            o = next((o for o in self.data['orders'] if o['id'] == ident), None)
            if o is None:
                raise ValueError('Order not found')
            if o['status'] == 'pending':
                o.update(status='cancelled', reason='Manual cancellation')
            elif o['status'] == 'open':
                self._close(o, 'Manual exit')
            else:
                raise ValueError('Order has already completed')
            self._save()

    def squareoff(self, reason='Manual square off'):
        with self.lock:
            for o in self.data['orders']:
                if o['status'] == 'pending':
                    o.update(status='cancelled', reason=reason)
                if o['status'] == 'open':
                    o['exit_due'] = reason
                    if o['symbol'] in self.market and self._fresh(o['symbol']):
                        self._close(o, reason)
            self._save()

    def housekeeping(self):
        with self.lock:
            today = self.day()
            for key, day in self.data['days'].items():
                if key < today and not day['finalized']:
                    day['eligible'] = day['eligible'] and not any(o['status'] == 'open' and o['day'] == key for o in self.data['orders'])
                    day['finalized'] = True
            if self.live and self.now().astimezone(IST).time() >= self.cutoff:
                self.squareoff('Scheduled intraday square off')
                if not any(o['status'] == 'open' for o in self.data['orders']):
                    self._day()['finalized'] = True
            self._save()

    def tick(self, symbol: str, price: float, **kwargs):
        if symbol not in self.market or not math.isfinite(price) or price <= 0:
            return
        with self.lock:
            q = self.market[symbol]
            q.update(price=price, updated=self.now().timestamp())
            minute = int(q['updated'] // 60) * 60
            candles = q['candles']
            if candles and candles[-1]['time'] == minute:
                c = candles[-1]
                c.update(c=price, h=max(c['h'], price), l=min(c['l'], price))
            else:
                candles.append(dict(time=minute, o=price, c=price, h=price, l=price))
                del candles[:-500]
            pct = (price / q['base'] - 1) * 100 if q['base'] > 0 else 0
            for threshold in [7, 11, 13, -7, -11, -13]:
                key = (self.day(), symbol, threshold)
                if key not in self.seen and (pct >= threshold if threshold > 0 else pct <= threshold):
                    self.seen.add(key)
                    self.alerts.insert(0, dict(symbol=symbol, threshold=threshold, price=price,
                                              time=self.now().isoformat()))
            del self.alerts[100:]
            self.housekeeping()
            for o in self.data['orders']:
                if o['symbol'] == symbol:
                    self._process(o)
            if self.data['loss_cap'] and self._net() <= -self.data['loss_cap']:
                if self.day() not in self.data['halted']:
                    self.data['halted'].append(self.day())
                self.squareoff('Daily loss cap')
            self._save()

    def configure(self, request):
        with self.lock:
            cap = number(request.get('loss_cap', self.data['loss_cap']))
            slip = number(request.get('slippage_bps', self.data['slippage_bps']))
            if slip > 1000:
                raise ValueError('Slippage must be at most 1,000 basis points')
            self.data.update(loss_cap=cap, slippage_bps=slip)
            self._save()

    def drawing(self, symbol, values):
        with self.lock:
            if symbol not in self.market:
                raise ValueError('Unknown stock')
            clean = {k: number(values[k], minimum=.05) for k in ['entry', 'stop', 'target']}
            self.data['drawings'][symbol] = clean
            self._save()

    def snapshot(self, *, full=False):
        with self.lock:
            streak, ledger = 0, []
            for key, day in sorted(self.data['days'].items()):
                net = rounded(day['gross'] - day['paid'])
                traded = day['entries'] > 0 or day['closed'] > 0
                if day['finalized'] and traded:
                    streak = streak + 1 if net > 0 and day['eligible'] else 0
                ledger.append(dict(date=key, net=net, streak=streak, **day))
            market = [{k: v for k, v in q.items() if k != 'token' and (full or k != 'candles')}
                      | {'fresh': self._fresh(q['symbol'])} for q in self.market.values()]
            result = dict(charge_model='NSE cash intraday · Zerodha schedule checked 2026-10-19 · per-order paise estimates', mode='live' if self.live else 'mock', cash=self.data['cash'], capital=self.data['capital'],
                          loss_cap=self.data['loss_cap'], slippage_bps=self.data['slippage_bps'],
                          net=self._net(), paid=self._day()['paid'], charge_breakdown=self._day()['charges'],
                          orders=self.data['orders'], sessions=ledger, streak=streak,
                          drawings=self.data['drawings'], market=market, alerts=self.alerts,
                          feed_error=self.feed_error, date=self.day(), cutoff=self.cutoff.isoformat())
            return json.loads(json.dumps(result, allow_nan=False))
