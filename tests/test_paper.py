from datetime import datetime, timedelta, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nse_alert.paper import IST, PaperBook, charges


@pytest.fixture
def desk(tmp_path):
    clock = [datetime(2026, 10, 19, 10, 0, tzinfo=IST)]
    inst = SimpleNamespace(symbol='AAA', prev_close=100, last_price=100, instrument_token=1)
    book = PaperBook(tmp_path / 'paper.db', [inst], live=True, now=lambda: clock[0])
    book.configure({'slippage_bps': 0})
    book.tick('AAA', 100)
    yield book, clock, inst
    book.db.close()


def request(**kwargs):
    return dict(symbol='AAA', qty=10, type='market', stop=97, target=110, **kwargs)


def test_round_trip_net_matches_cash_and_persists(desk):
    book, now, inst = desk
    o = book.place(request())
    book.tick('AAA', 105)
    book.action(o['id'])
    snap = book.snapshot()
    fees = charges(100, 10, False)['total'] + charges(105, 10, True)['total']
    assert snap['cash'] == pytest.approx(200000 + 50 - fees)
    assert snap['net'] == pytest.approx(50 - fees)
    restored = PaperBook(Path(book.db.execute('PRAGMA database_list').fetchone()[2]), [inst], live=True, now=lambda: now[0])
    assert restored.snapshot()['cash'] == snap['cash']
    assert restored.data['orders'][0]['status'] == 'closed'
    restored.db.close()


def test_stale_quote_blocks_entry_and_exit(desk):
    book, now, _ = desk
    o = book.place(request())
    now[0] += timedelta(seconds=16)
    with pytest.raises(ValueError, match='fresh quote'):
        book.place(request())
    with pytest.raises(ValueError, match='stale'):
        book.action(o['id'])
    book.squareoff()
    assert book.data['orders'][0]['status'] == 'open'
    book.tick('AAA', 95)
    assert book.data['orders'][0]['exit'] == 95


def test_gap_stop_does_not_invent_stop_fill(desk):
    book, _, _ = desk
    book.place(request())
    book.tick('AAA', 90)
    assert book.data['orders'][0]['exit'] == 90
    assert book.data['orders'][0]['reason'] == 'Stop loss'


def test_limit_waits_and_never_exceeds_limit(desk):
    book, _, _ = desk
    book.configure({'slippage_bps': 10})
    order = book.place(dict(symbol='AAA', qty=10, type='limit', entry=99, stop=96, target=110))
    assert order['status'] == 'pending'
    book.tick('AAA', 99)
    assert book.data['orders'][0]['fill'] == 99


def test_loss_cap_latches_even_if_settings_changed(desk):
    book, _, _ = desk
    book.configure({'loss_cap': 10})
    book.place(request())
    book.tick('AAA', 98)
    assert book.data['orders'][0]['status'] == 'closed'
    book.configure({'loss_cap': 0})
    with pytest.raises(ValueError, match='blocked'):
        book.place(request())


def test_squareoff_finalizes_only_after_fresh_exit(desk):
    book, now, _ = desk
    book.place(request())
    now[0] = now[0].replace(hour=15, minute=5)
    book.housekeeping()
    assert not book.data['days'][book.day()]['finalized']
    assert book.data['orders'][0]['exit_due']
    book.tick('AAA', 101)
    book.housekeeping()
    assert book.data['days'][book.day()]['finalized']
    assert book.snapshot()['streak'] == 1
    with pytest.raises(ValueError, match='closed'):
        book.place(request())


def test_no_trade_day_pauses_and_loss_day_resets(desk):
    book, now, _ = desk
    book.place(request())
    book.tick('AAA', 110)
    now[0] = now[0].replace(hour=15, minute=5)
    book.housekeeping()
    assert book.snapshot()['streak'] == 1
    now[0] += timedelta(days=1)
    book.housekeeping()
    assert book.snapshot()['streak'] == 1
    now[0] = (now[0] + timedelta(days=1)).replace(hour=10, minute=0)
    book.tick('AAA', 100)
    book.place(request())
    book.tick('AAA', 95)
    now[0] = now[0].replace(hour=15, minute=5)
    book.housekeeping()
    assert book.snapshot()['streak'] == 0


def test_overnight_outage_disqualifies_days(desk):
    book, now, _ = desk
    book.place(request())
    now[0] += timedelta(days=1)
    book.tick('AAA', 120)
    assert book.data['orders'][0]['reason'] == 'Overdue session exit'
    assert all(not d['eligible'] for d in book.data['days'].values())


@pytest.mark.parametrize('field,value', [('qty', 1.5), ('qty', -1), ('qty', float('nan')), ('target', float('inf')), ('stop', -1)])
def test_bad_orders_do_not_touch_cash(desk, field, value):
    book, _, _ = desk
    r = request()
    r[field] = value
    with pytest.raises(ValueError):
        book.place(r)
    assert book.data['cash'] == 200000
    assert not book.data['orders']


def test_insufficient_cash_rejects_without_fees(desk):
    book, _, _ = desk
    r = request()
    r['qty'] = 3000
    assert book.place(r)['status'] == 'rejected'
    assert book.data['cash'] == 200000
    assert book.snapshot()['paid'] == 0


def test_protection_can_trail_above_entry_without_changing_fill(desk):
    book, _, _ = desk
    o = book.place(request())
    book.tick('AAA', 105)
    book.protection(o['id'], 102, 115)
    book.tick('AAA', 101)
    assert book.data['orders'][0]['fill'] == 100
    assert book.data['orders'][0]['exit'] == 101
    assert book.data['orders'][0]['reason'] == 'Stop loss'


def test_fee_model_includes_all_components():
    fees = charges(1000, 100, True)
    assert fees['brokerage'] == 20
    assert fees['exchange'] == 3.07
    assert fees['stt'] == 25
    assert fees['stamp'] == 0
    assert fees['total'] == pytest.approx(sum(v for k, v in fees.items() if k != 'total'))


def test_entry_status_reflects_session_clock_and_cap(desk):
    book, now, _ = desk
    assert book.snapshot()['entries_open']
    now[0] = now[0].replace(hour=15, minute=5)
    assert not book.snapshot()['entries_open']


def test_entry_status_false_for_latched_daily_cap(desk):
    book, _, _ = desk
    book.data['halted'].append(book.day())
    assert not book.snapshot()['entries_open']
