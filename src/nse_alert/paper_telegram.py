"""Strict Telegram commands backed only by PaperBook. No broker order client."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime
import logging
import re
import threading
from pathlib import Path

import httpx

from nse_alert.confirm_bot import configured_chat_ids
from nse_alert.lock import acquire_telegram_poller, release_single_instance

logger = logging.getLogger(__name__)
HELP = '''PAPER trading — simulated cash equities only.
Confirm a new PAPER alert with /confirm Pxxxxxxxxxx
Cancel its offer with /cancel Pxxxxxxxxxx
/paper pending
/paper positions
/paper exit ORDER_ID
/paper cancel ORDER_ID
/paper levels ORDER_ID stop=268 target=290
/paper squareoff
/paper pnl
/paper help
Prices are rupees; quantity is whole shares. IDs may be the unique first 8+ characters. Commands require fresh quotes and follow Paperkite risk/session rules. There are no real orders.'''


def parse_command(text, bot_username=''):
    words = str(text).strip().split()
    if not words or not re.fullmatch(r'/(?:paper|confirm|cancel)(?:@[A-Za-z0-9_]+)?', words[0], re.I):
        return None
    if '@' in words[0] and words[0].split('@', 1)[1].lower() != bot_username.lower():
        return None
    root = words[0].split('@',1)[0].lower()
    if root in {'/confirm','/cancel'}:
        if len(words) != 2 or not re.fullmatch(r'P[A-Fa-f0-9]{10}', words[1], re.I):
            raise ValueError('Only new P-prefixed PAPER offer IDs are accepted; old real-order IDs are disabled')
        return ('confirm_offer' if root == '/confirm' else 'cancel_offer'), {'id': words[1].upper()}
    verb = words[1].lower() if len(words) > 1 else 'help'
    args = words[2:]
    if verb in {'help', 'positions', 'status', 'pnl', 'squareoff', 'pending'}:
        if args:
            raise ValueError('Unexpected arguments; use /paper help')
        return verb, {}
    if verb not in {'buy', 'exit', 'cancel', 'levels'} or not args:
        raise ValueError('Unknown command or missing stock/order ID; use /paper help')
    target, *options = args
    opts = {}
    for item in options:
        if item.count('=') != 1:
            raise ValueError('Options must use name=value; use /paper help')
        key, value = item.split('=', 1)
        key = key.lower()
        if not value or key in opts:
            raise ValueError('Empty or duplicate option')
        opts[key] = value
    if verb == 'buy':
        if not re.fullmatch(r'[A-Z0-9&-]{1,30}', target.upper()):
            raise ValueError('Invalid stock symbol')
        if set(opts) - {'qty', 'stop', 'target', 'type', 'entry'} or not {'qty', 'stop', 'target'} <= opts.keys():
            raise ValueError('BUY needs qty=, stop= and target=; use /paper help')
        kind = opts.get('type', 'market').lower()
        if kind not in {'market', 'limit', 'stop'} or (kind != 'market' and 'entry' not in opts):
            raise ValueError('Use type=market, or type=limit/stop with entry=')
        if kind == 'market' and 'entry' in opts:
            raise ValueError('Market entries use the server quote; remove entry=')
        return verb, dict(opts, symbol=target.upper(), type=kind)
    if not re.fullmatch(r'[a-fA-F0-9]{8,32}', target):
        raise ValueError('Use the order ID or a unique prefix of at least 8 characters')
    if verb == 'levels':
        if set(opts) != {'stop', 'target'}:
            raise ValueError('LEVELS needs exactly stop= and target=')
    elif opts:
        raise ValueError('Unexpected arguments')
    return verb, dict(opts, id=target.lower())


class PaperTelegram:
    def __init__(self, book, *, bot_token, chat_ids, user_ids='', state_dir=None, offers=None):
        self.book, self.bot_token = book, bot_token
        self.chat_ids = configured_chat_ids(chat_ids)
        self.user_ids = configured_chat_ids(user_ids)
        if not self.chat_ids or any(not re.fullmatch(r'-?\d+', v) for v in self.chat_ids):
            raise ValueError('Paper Telegram needs numeric TELEGRAM_CHAT_ID values')
        if any(not re.fullmatch(r'[1-9]\d*', v) for v in self.user_ids):
            raise ValueError('PAPER_TELEGRAM_ALLOWED_USER_IDS must contain positive numeric user IDs')
        from nse_alert.paper_offers import PaperOffers
        self.state_dir = Path(state_dir) if state_dir is not None else book.path.parent
        self.offers = offers or PaperOffers(self.state_dir, live=book.live)
        self.fingerprint = hashlib.sha256(bot_token.split(":", 1)[0].encode()).hexdigest()[:20]
        self.username = ''
        self.stop_event = threading.Event()
        self.thread = None
        self.client = None
        self.poll_lock = None
        self.offset_key = 'offset:' + self.fingerprint

    def handle(self, update):
        # Edited/channel/forwarded/anonymous/bot messages never place an order.
        msg = update.get('message')
        if not isinstance(msg, dict):
            return None
        sender, chat = msg.get('from') or {}, msg.get('chat') or {}
        uid, cid = str(sender.get('id', '')), str(chat.get('id', ''))
        if cid not in self.chat_ids or sender.get('is_bot') or msg.get('sender_chat'):
            return None
        if any(k.startswith('forward_') for k in msg):
            return None
        if self.user_ids:
            if uid not in self.user_ids:
                return None
        elif not uid.isdigit() or (not cid.startswith('-') and uid != cid):
            return None
        text = str(msg.get('text') or '')
        if not re.match(r'^\s*/(?:paper|confirm|cancel)(?:\s|@|$)', text, re.I):
            return None
        mid = msg.get('message_id')
        if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
            return None
        key = f'{self.fingerprint}:{cid}:{mid}'
        def action():
            stamp = msg.get('date')
            if not isinstance(stamp, (int, float)) or not 0 <= self.book.now().timestamp() - stamp <= 120:
                raise ValueError('Command is older than 120 seconds or has an invalid timestamp; send a new command')
            parsed = parse_command(text, self.username)
            if parsed is None:
                return ''
            verb, opts = parsed
            if verb == 'help':
                return HELP
            if verb == 'pending':
                existing = {o.get('paper_offer_id') for o in self.book.data['orders']}
                pending = [o for o in self.offers.pending(self.book.now()) if o['id'] not in existing]
                return 'PAPER pending offers:\n' + ('\n'.join(f"{o['id']} BUY {o['symbol']} — /confirm {o['id']}" for o in pending[:25]) or 'None')
            if verb in {'confirm_offer','cancel_offer'}:
                existing = next((o for o in self.book.data['orders'] if o.get('paper_offer_id') == opts['id']), None)
                if existing:
                    return self.order_reply(existing)
                offer = self.offers.get(opts['id'])
                if offer['status'] != 'pending':
                    raise ValueError('Paper offer is already ' + offer['status'])
                if datetime.fromisoformat(offer['expires']) <= self.book.now():
                    raise ValueError('Paper offer has expired; wait for a new alert')
                if verb == 'cancel_offer':
                    self.offers.status(offer['id'], 'cancelled')
                    return 'PAPER offer cancelled: ' + offer['id']
                symbol = offer['symbol']
                if symbol not in self.book.market or not self.book._fresh(symbol):
                    raise ValueError('Wait for a fresh Paperkite quote, then send /confirm again')
                fill = self.book._price(self.book.market[symbol]['price'])
                qty = offer['quantity'] if offer['quantity'] is not None else math.floor(float(offer['budget']) / fill)
                if qty < 1:
                    raise ValueError('Paper cash budget cannot buy one share; adjust sizing for future alerts')
                order = self.book.place(dict(symbol=symbol, qty=qty, type='market',
                    stop=round(fill*(1-offer['stop_pct']/100),2),
                    target=round(fill*(1+offer['target_pct']/100),2),
                    paper_offer_id=offer['id'], note='Telegram confirmation: ' + offer['reason']))
                return self.order_reply(order)
            if verb == 'buy':
                order = self.book.place(dict(opts, note=f'Telegram paper command {mid}'))
                return self.order_reply(order)
            if verb in {'positions', 'status', 'pnl'}:
                snap = self.book.snapshot()
                lines = [f'PAPER ({snap["mode"]} prices) | {snap["date"]}',
                         f'Cash ₹{snap["cash"]:,.2f} | Net P&L ₹{snap["net"]:,.2f}',
                         f'Paid charges ₹{snap["paid"]:,.2f} | Streak {snap["streak"]}/7']
                if verb != 'pnl':
                    active = [o for o in snap['orders'] if o['status'] in {'open', 'pending'}]
                    lines.extend(f'{o["id"][:8]} {o["symbol"]} {o["status"]} qty={o["qty"]} stop={o["stop"]} target={o["target"]}' for o in active[:25])
                    if not active:
                        lines.append('No open positions or pending orders.')
                    if len(active) > 25:
                        lines.append('Additional orders are visible in the browser.')
                return '\n'.join(lines)
            if verb == 'squareoff':
                self.book.squareoff('Telegram paper square off')
                waiting = sum(o['status'] == 'open' for o in self.book.data['orders'])
                return f'PAPER square-off requested. {waiting} positions waiting for fresh quotes.'
            matches = [o for o in self.book.data['orders'] if o['id'].startswith(opts['id'])]
            if len(matches) != 1:
                raise ValueError('Order ID not found or ambiguous; use /paper positions')
            order = matches[0]
            if verb == 'levels':
                self.book.protection(order['id'], opts['stop'], opts['target'])
            else:
                required = 'open' if verb == 'exit' else 'pending'
                if order['status'] != required:
                    raise ValueError(f'{verb.upper()} requires an {required} order')
                self.book.action(order['id'])
            return self.order_reply(order)
        with self.book.lock:
            if self.stop_event.is_set():
                return None
            reply = self.book.telegram_once(key, action)
            # The ledger/receipt transaction commits before updating this informational offer status.
            try:
                parsed = parse_command(text, self.username)
            except ValueError:
                parsed = None
            if parsed and parsed[0] == 'confirm_offer':
                order = next((o for o in self.book.data['orders'] if o.get('paper_offer_id') == parsed[1]['id']), None)
                if order:
                    self.offers.status(parsed[1]['id'], 'rejected' if order['status'] == 'rejected' else 'completed')
        return (cid, reply) if reply else None

    def order_reply(self, o):
        price = o.get('fill', o.get('entry', 0))
        result = f'PAPER {o["status"].upper()} | {o["symbol"]} qty={o["qty"]}\nID {o["id"][:8]} | Entry ₹{price:.2f}\nStop ₹{o["stop"]:.2f} | Target ₹{o["target"]:.2f}'
        if o.get('reason'):
            result += '\n' + o['reason']
        if 'net' in o:
            result += f'\nTrade net P&L ₹{o["net"]:.2f}'
        return result + '\nSimulated only; no broker order.'

    def request(self, method, **kwargs):
        response = self.client.post(f'https://api.telegram.org/bot{self.bot_token}/{method}', **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not payload.get('ok'):
            raise RuntimeError('Telegram API rejected request')
        return payload.get('result')

    def start(self):
        self.poll_lock = acquire_telegram_poller(self.state_dir, self.bot_token)
        self.stop_event.clear()
        self.client = httpx.Client(timeout=35)
        try:
            me = self.request('getMe')
            self.username = me['username']
            # Don't silently delete a webhook owned by another deployment.
            info = self.request('getWebhookInfo')
            if info.get('url'):
                raise ValueError('Telegram webhook is active; resolve the existing bot consumer before enabling paper mode')
        except BaseException as exc:
            self.client.close()
            release_single_instance(self.poll_lock)
            self.poll_lock = None
            if isinstance(exc, (ValueError, KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError('Paper Telegram startup failed (' + type(exc).__name__ +
                               '); check bot credentials and connectivity') from None
        self.thread = threading.Thread(target=self.run, name='telegram-paper', daemon=True)
        self.thread.start()

    def run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                try:
                    offset = int(self.book.telegram_meta(self.offset_key) or 0)
                    updates = self.request('getUpdates', json={'offset': offset, 'timeout': 25,
                                           'allowed_updates': ['message']})
                    for update in updates:
                        if self.stop_event.is_set():
                            break
                        reply = self.handle(update)
                        if reply:
                            # If sending fails, replay returns the durable receipt, never another fill.
                            self.request('sendMessage', json={'chat_id': reply[0], 'text': reply[1][:3900],
                                         'disable_web_page_preview': True}, timeout=10)
                        self.book.telegram_meta(self.offset_key, int(update['update_id']) + 1)
                    failures = 0
                    self.book.telegram_status = 'listening'
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    failures += 1
                    self.book.telegram_status = 'connection error; commands may be delayed'
                    logger.warning('Paper Telegram failure: %s (attempt %d)', type(exc).__name__, failures)
                    self.stop_event.wait(min(60, 2 ** min(failures, 6)))
        finally:
            if self.poll_lock:
                release_single_instance(self.poll_lock)
                self.poll_lock = None

    def stop(self):
        self.stop_event.set()
        if self.client:
            self.client.close()
        if self.thread:
            self.thread.join(timeout=3)
