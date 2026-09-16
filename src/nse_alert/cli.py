from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

import click

from nse_alert.config import Settings
from nse_alert.confirm_bot import TelegramConfirmListener
from nse_alert.engine import AlertEngine, parse_thresholds
from nse_alert.feed import KiteFeed, MockFeed
from nse_alert.notify import TelegramNotifier, build_notifier
from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest
from nse_alert.report import (
    build_day_report,
    format_day_report,
    load_events,
    write_day_report,
)
from nse_alert.surveillance import load_asm_symbols, load_nfo_equity_underlyings
from nse_alert.universe import Instrument, build_universe, _kite_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nse_alert")


@click.group()
@click.version_option(version="0.1.0", prog_name="nse-alert")
def main() -> None:
    """Realtime NSE day-move alerts (Kite WebSocket + Telegram)."""


@main.command("universe")
@click.option("--min-turnover-cr", type=float, default=None, help="Min prior-day turnover in ₹ crore")
@click.option("--min-price", type=float, default=None, help="Min close price filter")
def universe_cmd(min_turnover_cr: float | None, min_price: float | None) -> None:
    """Print the liquidity-screened universe (mock, or Kite instruments + quotes)."""
    settings = Settings()
    tovr = min_turnover_cr if min_turnover_cr is not None else settings.min_turnover_cr
    price = min_price if min_price is not None else settings.min_price
    mock = not settings.use_kite
    instruments = build_universe(
        min_turnover_cr=tovr,
        min_price=price,
        kite_api_key=settings.kite_api_key,
        kite_access_token=settings.kite_access_token,
        custom_universe_file=settings.custom_universe_file,
        mock=mock and not settings.custom_universe_file,
    )
    click.echo(f"{'SYMBOL':<12} {'PREV_CLOSE':>12} {'TURNOVER_CR':>12} {'TOKEN':>10}")
    for inst in instruments:
        click.echo(
            f"{inst.symbol:<12} {inst.prev_close:>12.2f} "
            f"{inst.turnover_cr:>12.2f} {inst.instrument_token:>10}"
        )
    click.echo(f"\nTotal: {len(instruments)}")


@main.command("watch")
@click.option(
    "--threshold",
    "threshold_raw",
    type=str,
    default=None,
    help='Alert level(s), comma-separated — e.g. "4,7,11" (default from THRESHOLD_PCT / 13)',
)
@click.option(
    "--feed",
    type=click.Choice(["mock", "kite"], case_sensitive=False),
    default=None,
    help="Price feed source (default from FEED_MODE / .env)",
)
@click.option(
    "--max-ticks",
    type=int,
    default=None,
    help="Mock feed only: stop after N ticks (default 80)",
)
def watch_cmd(
    threshold_raw: str | None,
    feed: str | None,
    max_ticks: int | None,
) -> None:
    """Watch the universe and alert when a stock crosses each ±threshold% today."""
    settings = Settings()
    try:
        thresholds = (
            parse_thresholds(threshold_raw)
            if threshold_raw is not None
            else settings.thresholds
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    feed_mode = (feed or settings.feed_mode).strip().lower()
    use_kite = feed_mode == "kite"

    if use_kite and (not settings.kite_api_key or not settings.kite_access_token):
        raise click.ClickException(
            "FEED_MODE=kite requires KITE_API_KEY and KITE_ACCESS_TOKEN "
            "(paid Kite Connect plan for WebSocket)."
        )

    instruments = build_universe(
        min_turnover_cr=settings.min_turnover_cr,
        min_price=settings.min_price,
        kite_api_key=settings.kite_api_key if use_kite else "",
        kite_access_token=settings.kite_access_token if use_kite else "",
        custom_universe_file=settings.custom_universe_file,
        mock=not use_kite and not settings.custom_universe_file,
    )

    if not use_kite and not settings.custom_universe_file:
        instruments = _ensure_mock_demo(instruments)

    if not instruments:
        raise click.ClickException("Universe is empty after filters")

    prev_closes = {i.symbol: i.prev_close for i in instruments}
    state_path = settings.state_dir / "fired.json"
    # Fresh mock runs should not be blocked by a prior DEMO13 fire today.
    if not use_kite and state_path.exists():
        state_path.unlink()

    fo_symbols: set[str] = set()
    asm_symbols: set[str] = set()
    fo_only = settings.fo_only_threshold_list
    if use_kite:
        kite = _kite_client(settings.kite_api_key, settings.kite_access_token)
        if fo_only:
            try:
                fo_symbols = load_nfo_equity_underlyings(kite)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not load NFO underlyings (%s); FO-only thresholds disabled",
                    exc,
                )
                fo_only = []
        asm_symbols = load_asm_symbols(
            url=settings.asm_sheet_url,
            cache_path=settings.state_dir / "asm_symbols.txt",
            enabled=settings.asm_enabled,
        )
    else:
        # Mock: treat all demo symbols as F&O so FO-only levels still exercise.
        fo_symbols = {i.symbol for i in instruments}

    engine = AlertEngine(
        prev_closes=prev_closes,
        thresholds=thresholds,
        state_path=state_path,
        fo_symbols=fo_symbols,
        fo_only_thresholds=fo_only,
        asm_symbols=asm_symbols,
    )
    notifier = build_notifier(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        always_console=True,
    )
    book = OrderBook(settings.state_dir / "orders.json")
    executor = _build_executor(settings, book)
    tg: TelegramNotifier | None = None
    if settings.telegram_configured:
        tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

    alert_count = {"n": 0}

    def _handle_trade(alert: object) -> None:
        from nse_alert.engine import Alert as AlertType

        assert isinstance(alert, AlertType)
        if not executor.should_trade_alert(
            direction=alert.direction, threshold_pct=alert.threshold_pct
        ):
            return
        req = executor.build_request_from_alert(
            symbol=alert.symbol,
            direction=alert.direction,
            threshold_pct=alert.threshold_pct,
            change_pct=alert.change_pct,
            entry_ltp=alert.ltp,
        )
        mode = executor.mode
        if mode == "confirm":
            pending = book.add_pending(
                req,
                alert_symbol=alert.symbol,
                alert_threshold=alert.threshold_pct,
                alert_direction=alert.direction,
                entry_ltp=alert.ltp,
                ttl_minutes=settings.trade_confirm_ttl_minutes,
            )
            stop_px = executor.stop_price_from_entry(alert.ltp)
            limit_px = executor.stop_limit_price_from_trigger(stop_px)
            msg = (
                f"CONFIRM ORDER `{pending.id}`\n"
                f"{req.side} {req.quantity}x {req.symbol} ({req.product} {req.order_type})\n"
                f"Entry≈`{alert.ltp:.2f}` → SL-Limit trigger≈`{stop_px:.2f}` "
                f"limit≈`{limit_px:.2f}` (-{settings.trade_stop_loss_pct:g}%)\n"
                f"Sizing: {req.reason}\n\n"
                f"Reply: `CONFIRM {pending.id}` or `CANCEL {pending.id}`\n"
                f"Or: `uv run nse-alert confirm {pending.id}`"
            )
            logger.info("Pending order %s for %s", pending.id, req.symbol)
            if tg:
                tg.send_text(msg, parse_mode="Markdown")
            else:
                click.echo(msg)
            return
        if mode in {"dry_run", "auto"}:
            entry, sl = executor.place_entry_with_stop(req, entry_ltp=alert.ltp)
            note = f"TRADE [{entry.mode}] {entry.message}"
            if sl is not None:
                note += f"\n{sl.message}"
            logger.info("%s", note)
            if tg:
                tg.send_text(note)

    def on_tick(symbol: str, ltp: float) -> None:
        trail_msg = executor.manage_open_stops(symbol, ltp)
        if trail_msg:
            logger.info("%s", trail_msg)
            if tg:
                tg.send_text(trail_msg)
        for alert in engine.on_tick(symbol, ltp):
            notifier.send(alert)
            alert_count["n"] += 1
            _handle_trade(alert)

    threshold_label = ",".join(f"{t:g}" for t in thresholds)
    fo_only_label = ",".join(f"{t:g}" for t in fo_only) if fo_only else "none"
    logger.info(
        "Watching %d symbols | thresholds=±%s%% | fo_only=±%s%% (%d F&O) | "
        "asm=%d | feed=%s | telegram=%s | trade=%s",
        len(instruments),
        threshold_label,
        fo_only_label,
        len(fo_symbols),
        len(asm_symbols),
        feed_mode,
        "yes" if settings.telegram_configured else "console-only",
        executor.mode,
    )

    price_feed: MockFeed | KiteFeed
    if use_kite:
        price_feed = KiteFeed(
            api_key=settings.kite_api_key,
            access_token=settings.kite_access_token,
            instruments=instruments,
            on_tick=on_tick,
        )
    else:
        price_feed = MockFeed(
            instruments,
            on_tick,
            interval_sec=0.15,
            max_ticks=max_ticks if max_ticks is not None else 120,
            auto_stop_after_alert=True,
        )

    stop = {"flag": False}
    confirm_listener: TelegramConfirmListener | None = None

    def _on_confirm(pending_id: str) -> None:
        _confirm_pending(settings, executor, book, pending_id, tg)

    def _on_cancel(pending_id: str) -> None:
        item = book.mark_pending(pending_id, "cancelled")
        msg = f"Cancelled pending order {pending_id}" if item else f"Unknown id {pending_id}"
        logger.info("%s", msg)
        if tg:
            tg.send_text(msg)

    def _handle_sig(_signum: int, _frame: object) -> None:
        stop["flag"] = True
        price_feed.stop()
        if confirm_listener:
            confirm_listener.stop()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    if executor.mode == "confirm" and settings.telegram_configured:
        confirm_listener = TelegramConfirmListener(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            on_confirm=_on_confirm,
            on_cancel=_on_cancel,
        )
        confirm_listener.start()

    price_feed.start()
    try:
        if use_kite:
            while not stop["flag"]:
                time.sleep(0.5)
        else:
            assert isinstance(price_feed, MockFeed)
            while price_feed._thread and price_feed._thread.is_alive():  # noqa: SLF001
                time.sleep(0.05)
    finally:
        price_feed.stop()
        if confirm_listener:
            confirm_listener.stop()

    logger.info("Done. Alerts fired this run: %d", alert_count["n"])
    _emit_day_report(settings, telegram=settings.telegram_configured)
    if not use_kite and alert_count["n"] == 0:
        raise SystemExit(1)


def _build_executor(settings: Settings, book: OrderBook) -> OrderExecutor:
    return OrderExecutor(
        api_key=settings.kite_api_key,
        access_token=settings.kite_access_token,
        mode=settings.resolved_trade_mode,
        default_qty=settings.trade_qty,
        product=settings.trade_product,
        order_type=settings.trade_order_type,
        market_protection=settings.trade_market_protection,
        max_orders_per_day=settings.trade_max_orders_per_day,
        trade_on_thresholds=settings.trade_threshold_list,
        trade_sides=settings.trade_sides,
        stop_loss_pct=settings.trade_stop_loss_pct,
        stop_limit_ticks=settings.trade_stop_limit_ticks,
        stop_wait_sec=settings.trade_stop_wait_sec,
        trail_breakeven=settings.trade_trail_breakeven,
        trail_breakeven_pct=settings.trade_trail_breakeven_pct,
        sizing_mode=settings.trade_sizing,
        margin_budget_inr=settings.trade_margin_inr,
        fallback_leverage=settings.trade_fallback_leverage,
        book=book,
    )


def _confirm_pending(
    settings: Settings,
    executor: OrderExecutor,
    book: OrderBook,
    pending_id: str,
    tg: TelegramNotifier | None,
) -> None:
    item = book.get_pending(pending_id)
    if item is None:
        msg = f"No pending order `{pending_id}`"
        logger.warning("%s", msg)
        if tg:
            tg.send_text(msg, parse_mode="Markdown")
        return
    if item.status != "pending":
        msg = f"Pending `{pending_id}` is already {item.status}"
        if tg:
            tg.send_text(msg, parse_mode="Markdown")
        return
    req_data = item.request
    request = OrderRequest(
        symbol=str(req_data["symbol"]),
        side=req_data["side"],  # type: ignore[arg-type]
        quantity=int(req_data["quantity"]),
        product=str(req_data["product"]),
        order_type=str(req_data["order_type"]),
        price=req_data.get("price"),  # type: ignore[arg-type]
        trigger_price=req_data.get("trigger_price"),  # type: ignore[arg-type]
        market_protection=int(req_data.get("market_protection") or 2),
        tag=str(req_data.get("tag") or "nsealrt"),
        reason=str(req_data.get("reason") or "confirmed"),
    )
    force = "dry_run" if executor.mode == "dry_run" else "auto"
    entry_ltp = float(item.entry_ltp or 0.0)
    if entry_ltp <= 0:
        entry_ltp = float(req_data.get("price") or 0.0) or 0.0
    entry, sl = executor.place_entry_with_stop(
        request, entry_ltp=entry_ltp or 0.01, force_mode=force
    )
    book.mark_pending(pending_id, "confirmed" if entry.ok else "pending")
    msg = entry.message
    if sl is not None:
        msg += f"\n{sl.message}"
    if tg:
        tg.send_text(msg)


def _resolve_eod_closes(settings: Settings, events: list) -> tuple[dict[str, float], str]:
    """Fetch EOD closes via Kite when possible; else fall back inside build_day_report."""
    from nse_alert.engine import Alert as AlertType
    from nse_alert.report import fetch_eod_closes_kite

    symbols = sorted({a.symbol for a in events if isinstance(a, AlertType)})
    if not symbols:
        return {}, "none"
    if settings.kite_api_key and settings.kite_access_token:
        try:
            kite = _kite_client(settings.kite_api_key, settings.kite_access_token)
            closes = fetch_eod_closes_kite(kite, symbols)
            if closes:
                return closes, "kite"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch EOD closes via Kite: %s", exc)
    return {}, "none"


def _emit_day_report(settings: Settings, *, telegram: bool) -> None:
    state_path = settings.state_dir / "fired.json"
    events = load_events(state_path)
    closes, source = _resolve_eod_closes(settings, events)
    report = build_day_report(
        events, eod_closes=closes or None, close_prices_source=source
    )
    text = format_day_report(report)
    out_path = settings.state_dir / f"report-{report.report_date.isoformat()}.txt"
    write_day_report(report, out_path)
    click.echo("")
    click.echo(text)
    click.echo(f"\nSaved report: {out_path}")
    if telegram and settings.telegram_bot_token and settings.telegram_chat_id:
        TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        ).send_text(text)


@main.command("report")
@click.option(
    "--date",
    "report_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Calendar day to report (default: today)",
)
@click.option(
    "--telegram/--no-telegram",
    default=False,
    help="Also send the report to Telegram",
)
def report_cmd(report_date: object | None, telegram: bool) -> None:
    """Show end-of-day summary: crossings per threshold and multi-level time gaps."""
    from datetime import date as date_cls

    settings = Settings()
    day = (
        report_date.date()  # type: ignore[attr-defined]
        if report_date is not None
        else date_cls.today()
    )
    state_path = settings.state_dir / "fired.json"
    events = load_events(state_path, as_of=day)
    closes, source = _resolve_eod_closes(settings, events)
    report = build_day_report(
        events,
        report_date=day,
        eod_closes=closes or None,
        close_prices_source=source,
    )
    text = format_day_report(report)
    out_path = settings.state_dir / f"report-{day.isoformat()}.txt"
    write_day_report(report, out_path)
    click.echo(text)
    click.echo(f"\nSaved report: {out_path}")
    if telegram and settings.telegram_bot_token and settings.telegram_chat_id:
        TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        ).send_text(text)
    elif telegram:
        raise click.ClickException(
            "Telegram requested but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset"
        )


def _ensure_mock_demo(instruments: list[Instrument]) -> list[Instrument]:
    symbols = {i.symbol for i in instruments}
    if "DEMO13" not in symbols:
        instruments = list(instruments) + [
            Instrument(
                symbol="DEMO13",
                instrument_token=999001,
                name="Mock Threshold Crosser",
                last_price=100.0,
                prev_close=100.0,
                turnover_cr=100.0,
            )
        ]
    return instruments


@main.command("order")
@click.argument("side", type=click.Choice(["buy", "sell"], case_sensitive=False))
@click.argument("symbol")
@click.option("--qty", type=int, default=None, help="Quantity (default TRADE_QTY)")
@click.option(
    "--dry-run/--live",
    default=True,
    help="Dry-run by default; pass --live to send to Kite",
)
def order_cmd(side: str, symbol: str, qty: int | None, dry_run: bool) -> None:
    """Place a manual NSE equity order (CNC/MIS from .env)."""
    settings = Settings()
    book = OrderBook(settings.state_dir / "orders.json")
    executor = _build_executor(settings, book)
    request = OrderRequest(
        symbol=symbol.upper(),
        side="BUY" if side.lower() == "buy" else "SELL",
        quantity=qty if qty is not None else settings.trade_qty,
        product=settings.trade_product,
        order_type=settings.trade_order_type,
        price=None,
        trigger_price=None,
        market_protection=settings.trade_market_protection,
        tag="nsealrt",
        reason="manual CLI order",
    )
    mode = "dry_run" if dry_run else "auto"
    if mode == "auto" and (not settings.kite_api_key or not settings.kite_access_token):
        raise click.ClickException("Live orders need KITE_API_KEY and KITE_ACCESS_TOKEN")
    result = executor.place(request, force_mode=mode)
    click.echo(result.message)
    if not result.ok:
        raise SystemExit(1)


@main.command("pending")
def pending_cmd() -> None:
    """List orders waiting for Telegram/CLI confirmation."""
    settings = Settings()
    book = OrderBook(settings.state_dir / "orders.json")
    items = book.list_pending()
    if not items:
        click.echo("No pending confirmations")
        return
    for item in items:
        req = item.request
        click.echo(
            f"{item.id}  {req.get('side')} {req.get('quantity')}x {req.get('symbol')}  "
            f"(alert ±{item.alert_threshold:g}% {item.alert_direction})  "
            f"expires {item.expires_at}"
        )


@main.command("confirm")
@click.argument("pending_id")
def confirm_cmd(pending_id: str) -> None:
    """Confirm a pending order id from TRADE_MODE=confirm."""
    settings = Settings()
    book = OrderBook(settings.state_dir / "orders.json")
    executor = _build_executor(settings, book)
    tg = (
        TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        if settings.telegram_configured
        else None
    )
    _confirm_pending(settings, executor, book, pending_id.upper(), tg)
    item = book.get_pending(pending_id)
    if item is None:
        raise click.ClickException(f"Unknown pending id {pending_id}")
    click.echo(f"Pending {pending_id} → {item.status}")


@main.command("login")
@click.option("--port", default=8765, show_default=True, help="Local UI port")
@click.option("--no-browser", is_flag=True, help="Do not auto-open the browser")
def login_cmd(port: int, no_browser: bool) -> None:
    """Open a local page to login with Kite or paste today's access token."""
    from nse_alert.envfile import read_env_value
    from nse_alert.login_ui import redirect_url, run_login_ui

    settings = Settings()
    api_key = settings.kite_api_key or read_env_value("KITE_API_KEY")
    api_secret = settings.kite_api_secret or read_env_value("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise click.ClickException(
            "Set KITE_API_KEY and KITE_API_SECRET in .env first "
            "(from https://developers.kite.trade/apps). "
            f"Also set the app Redirect URL to {redirect_url(port)}"
        )

    click.echo(f"Opening login UI on http://127.0.0.1:{port}/")
    click.echo(f"Kite Redirect URL must be: {redirect_url(port)}")
    try:
        token = run_login_ui(
            api_key=api_key,
            api_secret=api_secret,
            port=port,
            open_browser=not no_browser,
        )
    except KeyboardInterrupt as exc:
        raise click.ClickException("Login cancelled") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "***"
    click.echo(f"Saved access token ({masked}) to .env — run: uv run nse-alert watch")


@main.command("set-token")
@click.argument("access_token")
def set_token_cmd(access_token: str) -> None:
    """Paste today's Kite access token into .env (no browser)."""
    from nse_alert.envfile import upsert_env

    token = access_token.strip()
    if not token:
        raise click.ClickException("Access token is empty")
    upsert_env({"KITE_ACCESS_TOKEN": token, "FEED_MODE": "kite"})
    click.echo("Saved KITE_ACCESS_TOKEN and set FEED_MODE=kite")


@main.command("screen")
@click.option(
    "--force",
    is_flag=True,
    help="Run even before SCREEN_AFTER_HHMM (default 15:40 IST)",
)
@click.option(
    "--telegram/--no-telegram",
    default=True,
    help="Send summary + Excel to Telegram when configured",
)
@click.option(
    "--max-symbols",
    type=int,
    default=None,
    help="Limit symbols (for smoke tests); default from SCREEN_MAX_SYMBOLS",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Excel output path (default under STATE_DIR/screener/)",
)
def screen_cmd(
    force: bool,
    telegram: bool,
    max_symbols: int | None,
    out_path: Path | None,
) -> None:
    """EOD TA screener (daily chart): Excel + Telegram summary after 15:40 IST.

    Separate from intraday ±% alerts / MIS orders. Mandatory: price > SuperTrend
    and EMA20 > EMA50 > EMA200. Optional filters (volume spike, ADX, RSI, MACD,
    near 52w high) are configurable. Rows are ranked with all-pass names first.
    """
    from nse_alert.screener.engine import market_closed_enough, run_screener
    from nse_alert.screener.export import (
        config_from_settings,
        format_screener_summary,
        write_screener_excel,
    )
    from nse_alert.universe import load_custom_universe

    settings = Settings()
    cfg = config_from_settings(settings)
    if max_symbols is not None:
        cfg.max_symbols = max_symbols

    if not force and not market_closed_enough(after_hhmm=cfg.after_hhmm):
        raise click.ClickException(
            f"Screener is meant to run after {cfg.after_hhmm:04d} IST "
            f"(set SCREEN_AFTER_HHMM or pass --force)."
        )

    kite = None
    if settings.kite_api_key and settings.kite_access_token:
        kite = _kite_client(settings.kite_api_key, settings.kite_access_token)
    else:
        logger.warning(
            "No Kite token — using Yahoo history/market-cap only; "
            "turnover filter may be skipped"
        )

    custom = None
    if settings.screen_custom_universe_file:
        custom = load_custom_universe(settings.screen_custom_universe_file)

    result = run_screener(
        cfg,
        state_dir=settings.state_dir,
        kite=kite,
        custom_symbols=custom,
    )
    excel = out_path or (
        settings.state_dir / "screener" / f"screen-{result.as_of.isoformat()}.xlsx"
    )
    write_screener_excel(result, excel)
    summary = format_screener_summary(result, excel_name=excel.name)
    click.echo(summary)
    click.echo(f"\nSaved: {excel}")

    if telegram and settings.telegram_configured:
        tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        tg.send_text(summary)
        tg.send_document(excel, caption=f"EOD screener {result.as_of.isoformat()}")
    elif telegram:
        logger.warning("Telegram not configured — Excel saved locally only")


@main.command("login-hint")
def login_hint() -> None:
    """Deprecated: use `nse-alert login` instead."""
    from nse_alert.login_ui import redirect_url

    click.echo(
        f"""
Preferred: uv run nse-alert login

One-time setup:
1. Paid Connect app at https://developers.kite.trade/
2. .env: KITE_API_KEY=... and KITE_API_SECRET=...
3. App Redirect URL: {redirect_url()}
4. Each trading day: uv run nse-alert login
   (or paste: uv run nse-alert set-token YOUR_ACCESS_TOKEN)
""".strip()
    )


if __name__ == "__main__":
    main()
