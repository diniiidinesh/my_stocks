"""Cross-process, paper-only alert offers shared by watch and Paperkite."""
from __future__ import annotations

import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


class PaperOffers:
    def __init__(self, state_dir: Path, live: bool):
        self.path = Path(state_dir) / ('paper-offers-live.sqlite3' if live else 'paper-offers-mock.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS offers (id TEXT PRIMARY KEY, source TEXT UNIQUE, body TEXT NOT NULL, status TEXT NOT NULL)')
        self.path.chmod(0o600)

    def add(self, *, symbol, ltp, reason, source, settings, now):
        offer = dict(id='P'+uuid.uuid4().hex[:10].upper(), symbol=symbol,
            ltp=ltp, reason=reason, created=now.isoformat(),
            expires=(now+timedelta(minutes=settings.trade_confirm_ttl_minutes)).isoformat(),
            quantity=settings.trade_qty if settings.trade_sizing == 'fixed' else None,
            budget=settings.trade_margin_inr,
            stop_pct=settings.paper_stop_loss_pct, target_pct=settings.paper_target_pct)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('INSERT OR IGNORE INTO offers VALUES (?, ?, ?, ?)',
                       (offer['id'], source, json.dumps(offer), 'pending'))
            row = db.execute('SELECT body,status FROM offers WHERE source=?',(source,)).fetchone()
            return dict(json.loads(row[0]), status=row[1])

    def get(self, ident):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            row = db.execute('SELECT body,status FROM offers WHERE id=?',(ident,)).fetchone()
            if row is None:
                raise ValueError('Paper offer not found. Real-order IDs cannot be confirmed in paper mode.')
            return dict(json.loads(row[0]),status=row[1])

    def status(self, ident, status):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('UPDATE offers SET status=? WHERE id=?',(status,ident))

    def pending(self, now):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute("SELECT body FROM offers WHERE status='pending' ORDER BY rowid DESC LIMIT 100").fetchall()
        return [o for row in rows if datetime.fromisoformat((o:=json.loads(row[0]))['expires'])>now]


def offer_text(o):
    sizing = f"qty={o['quantity']}" if o['quantity'] is not None else f"cash budget ₹{o['budget']:,.0f} (no leverage)"
    return (f"PAPER ORDER {o['id']}\nBUY {o['symbol']} | {sizing}\n"
            f"Stop {o['stop_pct']:g}% below fill | Target {o['target_pct']:g}% above fill\n"
            f"Current price/quantity/levels are checked when confirmed.\n"
            f"/confirm {o['id']}\n/cancel {o['id']}\n"
            f"Expires {datetime.fromisoformat(o['expires']).astimezone(ZoneInfo('Asia/Kolkata')).strftime('%H:%M IST')}\n"
            'Simulated only — visible in Paperkite, no broker order.')
