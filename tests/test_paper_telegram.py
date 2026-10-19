from datetime import timedelta
from pathlib import Path

import pytest

from nse_alert.paper_telegram import PaperTelegram, parse_command
from test_paper import desk


@pytest.fixture
def bot(desk, tmp_path):
    book, clock, inst = desk
    controller = PaperTelegram(book, bot_token='dummy', chat_ids='-100', user_ids='42', state_dir=tmp_path)
    controller.username = 'PaperBot'
    return controller, book, clock, inst


def update(bot, text='/paper buy AAA qty=10 stop=97 target=110', mid=1, uid=42, chat=-100):
    return {'update_id': mid, 'message': {'message_id': mid, 'date': int(bot.book.now().timestamp()),
            'chat': {'id': chat}, 'from': {'id': uid, 'is_bot': False}, 'text': text}}


def test_parser_strict_and_bot_mentions():
    assert parse_command('/paper@PaperBot positions', 'PaperBot') == ('positions', {})
    assert parse_command('/paper@OtherBot positions', 'PaperBot') is None
    for text in ['/paper buy AAA qty=10 stop=97', '/paper buy AAA qty=10 qty=20 stop=97 target=110',
                 '/paper buy AAA qty=10 stop=97 target=110 surprise=yes',
                 '/paper buy AAA qty=10 type=limit stop=97 target=110', '/paper exit dead']:
        with pytest.raises(ValueError):
            parse_command(text)


def test_buy_uses_shared_ledger_and_survives_duplicate_delivery(bot):
    controller, book, _, _ = bot
    event = update(controller)
    first = controller.handle(event)
    second = controller.handle(event)
    assert first == second
    assert 'PAPER OPEN' in first[1]
    assert len(book.data['orders']) == 1
    assert book.data['cash'] < 200000


def test_restart_does_not_repeat_an_order(bot):
    from nse_alert.paper import PaperBook
    controller, book, clock, inst = bot
    event = update(controller)
    first = controller.handle(event)
    path = Path(book.db.execute('PRAGMA database_list').fetchone()[2])
    restored = PaperBook(path, [inst], live=True, now=lambda: clock[0])
    other = PaperTelegram(restored, bot_token='dummy', chat_ids='-100', user_ids='42')
    assert other.handle(event) == first
    assert len(restored.data['orders']) == 1
    restored.db.close()


@pytest.mark.parametrize('change', ['user','chat','bot','forward','anonymous','edited','channel'])
def test_untrusted_messages_cannot_trade(bot, change):
    controller, book, _, _ = bot
    event = update(controller)
    if change == 'user': event['message']['from']['id'] = 99
    if change == 'chat': event['message']['chat']['id'] = -200
    if change == 'bot': event['message']['from']['is_bot'] = True
    if change == 'forward': event['message']['forward_origin'] = {'type':'user'}
    if change == 'anonymous': event['message']['sender_chat'] = {'id': -100}
    if change in {'edited','channel'}: event['edited_message' if change == 'edited' else 'channel_post'] = event.pop('message')
    assert controller.handle(event) is None
    assert not book.data['orders']


def test_stale_and_future_messages_rejected(bot):
    controller, book, _, _ = bot
    for i, delta in enumerate([-121, 60], 1):
        event = update(controller, mid=i)
        event['message']['date'] += delta
        assert 'PAPER rejected' in controller.handle(event)[1]
    assert not book.data['orders']


def test_cash_risk_freshness_and_cutoff_are_shared(bot):
    controller, book, clock, _ = bot
    event = update(controller)
    clock[0] += timedelta(seconds=16)
    event['message']['date'] = int(clock[0].timestamp())
    assert 'fresh quote' in controller.handle(event)[1]
    clock[0] = clock[0].replace(hour=16)
    assert 'closed' in controller.handle(update(controller,mid=2))[1]
    assert not book.data['orders']


def test_exit_cancel_and_protection_commands(bot):
    controller, book, _, _ = bot
    controller.handle(update(controller))
    ident = book.data['orders'][0]['id'][:8]
    assert 'requires an pending' in controller.handle(update(controller, f'/paper cancel {ident}', mid=2))[1]
    controller.handle(update(controller, f'/paper levels {ident} stop=98 target=112',mid=3))
    assert book.data['orders'][0]['stop'] == 98
    assert 'PAPER CLOSED' in controller.handle(update(controller,f'/paper exit {ident}',mid=4))[1]
    assert book.data['orders'][0]['status'] == 'closed'


def test_private_chat_defaults_to_its_own_user(desk):
    book, _, _ = desk
    controller = PaperTelegram(book, bot_token='dummy', chat_ids='42', state_dir=Path(book.db.execute('PRAGMA database_list').fetchone()[2]).parent)
    assert controller.handle(update(controller,chat=42,uid=99)) is None
    assert controller.handle(update(controller,'/paper help',chat=42,uid=42))
    group = PaperTelegram(book, bot_token='dummy', chat_ids='-100', state_dir=Path(book.db.execute('PRAGMA database_list').fetchone()[2]).parent)
    assert group.handle(update(group,'/paper help'))


def test_unexpected_failure_rolls_back_trade_and_receipt(bot):
    controller, book, _, _ = bot
    def broken():
        book.place({'symbol':'AAA','qty':10,'type':'market','stop':97,'target':110})
        raise RuntimeError('simulated crash')
    with pytest.raises(RuntimeError):
        book.telegram_once('crash', broken)
    assert not book.data['orders']
    assert book.data['cash'] == 200000
    assert book.db.execute("SELECT COUNT(*) FROM telegram_receipts WHERE key='crash'").fetchone()[0] == 0


def test_legacy_confirm_never_routes_to_broker(bot):
    controller, book, _, _ = bot
    assert 'old real-order IDs are disabled' in controller.handle(update(controller, '/confirm ABCD1234'))[1]
    assert not book.data['orders']


def test_receipt_survives_bot_token_rotation(bot):
    controller, book, _, _ = bot
    controller.bot_token = 'dummy:old'
    event = update(controller)
    controller.handle(event)
    rotated = PaperTelegram(book, bot_token='dummy:new', chat_ids='-100', user_ids='42')
    rotated.handle(event)
    assert len(book.data['orders']) == 1


def test_poller_lock_prevents_two_consumers(tmp_path):
    from nse_alert.lock import acquire_telegram_poller, release_single_instance
    first = acquire_telegram_poller(tmp_path, '123:first')
    try:
        with pytest.raises(SystemExit):
            acquire_telegram_poller(tmp_path, '123:rotated')
    finally:
        release_single_instance(first)


def test_paper_mode_blocks_broker_executor_even_force_auto(tmp_path, monkeypatch):
    from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest
    from nse_alert.config import Settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TRADE_MODE', 'paper')
    monkeypatch.setenv('SQUAREOFF_MODE', 'live')
    settings = Settings()
    assert settings.resolved_trade_mode == 'paper'
    assert settings.resolved_squareoff_mode == 'off'
    executor = OrderExecutor(mode='paper', book=OrderBook(tmp_path/'orders.json'))
    assert not executor.enabled()
    with pytest.raises(RuntimeError, match='disabled'):
        executor._kite()
    request = OrderRequest(symbol='AAA', side='BUY', quantity=10, product='MIS',
        order_type='MARKET', price=None, trigger_price=None, market_protection=2, tag='test', reason='test')
    result = executor.place(request, force_mode='auto')
    assert not result.ok
    assert result.mode == 'paper'
    assert executor.manage_open_stops('AAA', 120) is None
    assert executor.manage_upper_circuit_exit('AAA', 120, 100) is None


def test_live_cli_commands_refused_in_paper_mode(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from nse_alert.cli import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TRADE_MODE','paper')
    runner = CliRunner()
    for args in [['squareoff','--live'], ['trail','AAA','10','--live']]:
        result = runner.invoke(main,args)
        assert result.exit_code != 0
        assert 'TRADE_MODE=paper' in result.output


def make_offer(controller, *, symbol='AAA', source='test'):
    from nse_alert.config import Settings
    return controller.offers.add(symbol=symbol,ltp=100,reason='threshold +13%',source=source,
        settings=Settings(_env_file=None),now=controller.book.now())


def test_alert_confirmation_fills_once_across_different_messages(bot):
    controller, book, _, _ = bot
    offer = make_offer(controller)
    first = controller.handle(update(controller, '/confirm '+offer['id'], mid=30))
    second = controller.handle(update(controller, '/confirm '+offer['id'], mid=31))
    assert first == second
    assert 'PAPER OPEN' in first[1]
    assert len(book.data['orders']) == 1
    order = book.data['orders'][0]
    assert order['paper_offer_id'] == offer['id']
    assert order['qty'] == 100  # 10,000 notional / 100; no leverage
    assert order['stop'] == 97
    assert order['target'] == 106
    assert controller.offers.get(offer['id'])['status'] == 'completed'


def test_cancelled_or_expired_paper_offer_never_fills(bot):
    controller, book, clock, _ = bot
    offer = make_offer(controller)
    assert 'cancelled' in controller.handle(update(controller,'/cancel '+offer['id'],mid=40))[1]
    assert 'already cancelled' in controller.handle(update(controller,'/confirm '+offer['id'],mid=41))[1]
    expired = make_offer(controller,source='expired')
    clock[0] += timedelta(minutes=31)
    assert 'expired' in controller.handle(update(controller,'/confirm '+expired['id'],mid=42))[1]
    assert not book.data['orders']


def test_receipt_gap_between_offer_and_ledger_does_not_duplicate(bot, monkeypatch):
    controller, book, _, _ = bot
    offer = make_offer(controller)
    original = controller.offers.status
    def fail(*args):
        raise RuntimeError('offer status storage unavailable after ledger commit')
    monkeypatch.setattr(controller.offers,'status',fail)
    with pytest.raises(RuntimeError):
        controller.handle(update(controller,'/confirm '+offer['id'],mid=50))
    assert len(book.data['orders']) == 1
    monkeypatch.setattr(controller.offers,'status',original)
    controller.handle(update(controller,'/confirm '+offer['id'],mid=51))
    assert len(book.data['orders']) == 1


def test_mock_alert_to_confirmation_to_shared_paper_ledger(tmp_path,monkeypatch):
    import re
    from click.testing import CliRunner
    from nse_alert.cli import main
    from nse_alert.orders import OrderExecutor
    from nse_alert.paper import PaperBook
    from nse_alert.universe import Instrument
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TRADE_MODE','paper')
    monkeypatch.setenv('STATE_DIR',str(tmp_path/'state'))
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN',raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID',raising=False)
    monkeypatch.setenv('VOLUME_SPIKE_ENABLED','false')
    monkeypatch.setenv('BREAKOUT_52W_ENABLED','false')
    def forbidden(*args,**kwargs):
        raise AssertionError('Broker order client must never be constructed')
    monkeypatch.setattr(OrderExecutor,'_kite',forbidden)
    run = CliRunner().invoke(main,['watch','--feed','mock','--threshold','13','--max-ticks','120'])
    assert run.exit_code == 0, run.output
    ident = re.search(r'/confirm (P[A-F0-9]{10})',run.output).group(1)
    inst = Instrument(symbol='DEMO13',instrument_token=5,name='Demo',last_price=114,prev_close=100,turnover_cr=50)
    book = PaperBook(tmp_path/'paper.sqlite3',[inst])
    book.tick('DEMO13',114)
    controller = PaperTelegram(book,bot_token='dummy',chat_ids='-100',state_dir=tmp_path/'state')
    reply=controller.handle(update(controller,'/confirm '+ident))
    assert 'PAPER OPEN' in reply[1]
    assert book.snapshot()['orders'][0]['symbol'] == 'DEMO13'
    book.db.close()


def test_failed_reply_replays_receipt_without_another_fill(bot,monkeypatch):
    controller, book, _, _ = bot
    offer = make_offer(controller)
    event = update(controller,'/confirm '+offer['id'],mid=70)
    polls, sends = [], []
    def request(method,**kwargs):
        if method == 'getUpdates':
            polls.append(kwargs['json']['offset'])
            if len(polls) == 3:
                controller.stop_event.set()
                return []
            return [event]
        if method == 'sendMessage':
            sends.append(kwargs['json']['text'])
            if len(sends) == 1:
                raise RuntimeError('acknowledgement connection lost')
            return {}
        raise AssertionError(method)
    monkeypatch.setattr(controller,'request',request)
    monkeypatch.setattr(controller.stop_event,'wait',lambda delay:False)
    controller.run()
    assert len(book.data['orders']) == 1
    assert len(sends) == 2 and sends[0] == sends[1]
    assert polls[:2] == [0,0]
    assert book.telegram_meta(controller.offset_key) == '71'


def test_webhook_refusal_releases_poller_lock(bot,monkeypatch):
    from nse_alert.lock import acquire_telegram_poller,release_single_instance
    controller, _, _, _ = bot
    class Client:
        def close(self): pass
    monkeypatch.setattr('nse_alert.paper_telegram.httpx.Client',lambda **kwargs:Client())
    monkeypatch.setattr(controller,'request',lambda method: {'username':'PaperBot'} if method=='getMe' else {'url':'https://existing.example/hook'})
    with pytest.raises(ValueError,match='webhook'):
        controller.start()
    lock=acquire_telegram_poller(controller.state_dir,controller.bot_token)
    release_single_instance(lock)


def test_startup_http_error_does_not_expose_bot_token(bot,monkeypatch):
    import httpx
    controller, _, _, _ = bot
    class Client:
        def close(self): pass
    monkeypatch.setattr('nse_alert.paper_telegram.httpx.Client',lambda **kwargs:Client())
    def error(method):
        req=httpx.Request('POST','https://api.telegram.org/botPRIVATE_TOKEN/getMe')
        raise httpx.HTTPStatusError('private url: '+str(req.url),request=req,response=httpx.Response(401,request=req))
    monkeypatch.setattr(controller,'request',error)
    with pytest.raises(RuntimeError) as exc:
        controller.start()
    assert 'PRIVATE_TOKEN' not in str(exc.value)
