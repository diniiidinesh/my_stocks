from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import click

from nse_alert.ipv4 import force_ipv4
from nse_alert.confirm_bot import TelegramConfirmListener
from nse_alert.config import Settings
from nse_alert.engine import AlertEngine, parse_thresholds
from nse_alert.lock import acquire_single_instance, release_single_instance
from nse_alert.feed import KiteFeed, MockFeed
from nse_alert.notify import TelegramNotifier, build_notifier
from nse_alert.orders import (
    OrderBook,
    OrderExecutor,
    OrderRequest,
    SizingRefusedError,
    format_expiry_message,
)
from nse_alert.report import (
    DayReport,
    build_day_report,
    format_day_report,
    load_events,
    write_day_report,
)
from nse_alert.surveillance import load_asm_symbols, load_nfo_equity_underlyings
from nse_alert.trailing import TrailingStopRunner
from nse_alert.universe import Instrument, build_universe, _kite_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO, and the Telegram Bot API puts the
# bot token in the URL path (.../bot<TOKEN>/sendMessage) — WARNING avoids
# writing the token into logs that may be world-readable.
logging.getLogger("httpx").setLevel(logging.WARNING)
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
        state_dir=settings.state_dir,
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

    lock_fh = acquire_single_instance(settings.state_dir)

    feed_mode = (feed or settings.feed_mode).strip().lower()
    use_kite = feed_mode == "kite"

    if use_kite and (not settings.kite_api_key or not settings.kite_access_token):
        raise click.ClickException(
            "FEED_MODE=kite requires KITE_API_KEY and KITE_ACCESS_TOKEN "
            "(paid Kite Connect plan for WebSocket)."
        )

    if use_kite:
        from kiteconnect.exceptions import TokenException

        try:
            _kite_client(settings.kite_api_key, settings.kite_access_token).profile()
        except TokenException as exc:
            msg = (
                f"🔑 Kite token expired or invalid — watcher cannot start.\n{exc}\n"
                "Run the daily login, then: docker compose up -d --build --force-recreate"
            )
            logger.error("%s", msg)
            if settings.telegram_configured:
                TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id
                ).send_text(msg)
            release_single_instance(lock_fh)
            raise SystemExit(1)

    instruments = build_universe(
        min_turnover_cr=settings.min_turnover_cr,
        min_price=settings.min_price,
        kite_api_key=settings.kite_api_key if use_kite else "",
        kite_access_token=settings.kite_access_token if use_kite else "",
        custom_universe_file=settings.custom_universe_file,
        mock=not use_kite and not settings.custom_universe_file,
        state_dir=settings.state_dir,
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
        try:
            req = executor.build_request_from_alert(
                symbol=alert.symbol,
                direction=alert.direction,
                threshold_pct=alert.threshold_pct,
                change_pct=alert.change_pct,
                entry_ltp=alert.ltp,
            )
        except SizingRefusedError as exc:
            msg = f"🔑 {alert.symbol} trade skipped: {exc}"
            logger.error("%s", msg)
            if tg:
                tg.send_text(msg)
            return
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
                f"Reply: `/confirm {pending.id}` or `/cancel {pending.id}`\n"
                f"(slash command — required in groups with default bot privacy)\n"
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

    EXPIRY_SWEEP_INTERVAL_SEC = 30.0
    _last_expiry_sweep = {"t": 0.0}

    def _sweep_expired_orders() -> None:
        now = time.time()
        if now - _last_expiry_sweep["t"] < EXPIRY_SWEEP_INTERVAL_SEC:
            return
        _last_expiry_sweep["t"] = now
        newly = book.sweep_expired()
        if not newly:
            return
        if len(newly) > 3:
            messages = [
                f"⏱ {len(newly)} orders EXPIRED unconfirmed — see "
                "`nse-alert pending` / orders.json"
            ]
        else:
            messages = [format_expiry_message(item) for item in newly]
        for msg in messages:
            logger.warning("%s", msg)
            if tg:
                tg.send_text(msg)

    def on_tick(symbol: str, ltp: float) -> None:
        trail_msg = executor.manage_open_stops(symbol, ltp)
        if trail_msg:
            logger.info("%s", trail_msg)
            if tg:
                tg.send_text(trail_msg)
        prev_close = engine.prev_closes.get(symbol)
        if prev_close:
            circuit_msg = executor.manage_upper_circuit_exit(symbol, ltp, prev_close)
            if circuit_msg:
                logger.info("%s", circuit_msg)
                if tg:
                    tg.send_text(circuit_msg)
        alerts = engine.on_tick(symbol, ltp)
        if alerts:
            notifier.send_many(alerts)
            alert_count["n"] += len(alerts)
            for alert in alerts:
                _handle_trade(alert)
        if executor.mode == "confirm":
            _sweep_expired_orders()

    threshold_label = ",".join(f"{t:g}" for t in thresholds)
    fo_only_label = ",".join(f"{t:g}" for t in fo_only) if fo_only else "none"
    qty_note = (
        f"TRADE_QTY={executor.default_qty} unused (TRADE_SIZING=margin)"
        if executor.sizing_mode == "margin"
        else f"TRADE_QTY={executor.default_qty}"
    )
    logger.info(
        "Watching %d symbols | thresholds=±%s%% | fo_only=±%s%% (%d F&O) | "
        "asm=%d | feed=%s | telegram=%s | trade=%s | sizing=%s budget=₹%.0f | %s | "
        "orders=%s",
        len(instruments),
        threshold_label,
        fo_only_label,
        len(fo_symbols),
        len(asm_symbols),
        feed_mode,
        "yes" if settings.telegram_configured else "console-only",
        executor.mode,
        executor.sizing_mode,
        executor.margin_budget_inr,
        qty_note,
        (settings.state_dir / "orders.json").resolve(),
    )

    stop = {"flag": False}
    feed_dead = {"v": False}

    def _feed_health_alert(msg: str) -> None:
        # Only market hours: an overnight/weekend reconnect loop is noise —
        # the watcher isn't expected to be doing anything useful then anyway.
        if tg and _in_ist_market_hours():
            tg.send_text(msg)

    def _feed_dead(msg: str) -> None:
        feed_dead["v"] = True
        if tg and _in_ist_market_hours():
            tg.send_text(msg)
        stop["flag"] = True

    price_feed: MockFeed | KiteFeed
    if use_kite:
        price_feed = KiteFeed(
            api_key=settings.kite_api_key,
            access_token=settings.kite_access_token,
            instruments=instruments,
            on_tick=on_tick,
            on_health_alert=_feed_health_alert,
            on_feed_dead=_feed_dead,
        )
    else:
        price_feed = MockFeed(
            instruments,
            on_tick,
            interval_sec=0.15,
            max_ticks=max_ticks if max_ticks is not None else 120,
            auto_stop_after_alert=True,
        )

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
            on_health_alert=tg.send_text if tg else None,
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
        release_single_instance(lock_fh)

    logger.info("Done. Alerts fired this run: %d", alert_count["n"])
    _emit_day_report(settings, telegram=settings.telegram_configured)
    if feed_dead["v"]:
        raise SystemExit(1)
    if not use_kite and alert_count["n"] == 0:
        raise SystemExit(1)


def _in_ist_market_hours(now_utc: datetime | None = None) -> bool:
    """Mon-Fri 09:15-15:30 IST. Fixed +5:30 offset — no DST, no tzdata needed."""
    now_utc = now_utc or datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:  # Sat=5, Sun=6
        return False
    minutes = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _build_executor(settings: Settings, book: OrderBook) -> OrderExecutor:
    if settings.kite_force_ipv4:
        force_ipv4()
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
        exit_on_upper_circuit=settings.trade_exit_on_upper_circuit,
        upper_circuit_pct=settings.trade_upper_circuit_pct,
        sizing_mode=settings.trade_sizing,
        margin_budget_inr=settings.trade_margin_inr,
        fallback_leverage=settings.trade_fallback_leverage,
        book=book,
    )


def _pending_not_found_msg(
    settings: Settings, book: OrderBook, pending_id: str
) -> str:
    path = (settings.state_dir / "orders.json").resolve()
    known = sorted(book._data.get("pending", {}).keys())  # noqa: SLF001
    known_s = ", ".join(known) if known else "(none)"
    return (
        f"No pending order `{pending_id}` in {path} "
        f"(book date={book._data.get('date')}, ids={known_s}). "  # noqa: SLF001
        "Confirm on the same machine as `watch`, using the same STATE_DIR. "
        "Docker watch: `docker compose exec nse-alert nse-alert confirm ID` "
        "(do not mix Docker volume vs host `.nse_alert`)."
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
        msg = _pending_not_found_msg(settings, book, pending_id)
        logger.warning("%s", msg)
        if tg:
            tg.send_text(msg)
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


def _build_heartbeat(
    settings: Settings, report: DayReport, book: OrderBook, day: date
) -> str:
    """One-message-a-day summary. The point isn't the content — it's that it
    arrives at all: every 2026-09-17/18 incident was something that *didn't*
    happen, and a missing heartbeat by ~16:00 IST is itself the alarm.
    """
    total = len(report.events)
    per_threshold = "  ".join(
        f"{t:g}%:{c}" for t, c in sorted(report.counts_by_threshold.items())
    )
    alerts_line = f"Alerts: {total} fired" + (f" ({per_threshold})" if per_threshold else "")

    status_counts = book.counts_by_status()
    expired = status_counts.get("expired", 0)
    orders_line = f"Orders: {book.placed_count} placed, {expired} expired unconfirmed"

    screener_path = settings.state_dir / "screener" / f"screen-{day.isoformat()}.xlsx"
    screener_line = (
        "Screener: already ran today"
        if screener_path.exists()
        else "Screener: scheduled 20:30 IST"
    )

    warnings: list[str] = []
    if expired > 0:
        warnings.append(f"{expired} order(s) expired unconfirmed")
    warnings_line = "Warnings: " + (", ".join(warnings) if warnings else "none")

    return (
        f"📊 Daily heartbeat — {day.isoformat()}\n"
        f"Mode: {settings.resolved_trade_mode} | Feed: {settings.feed_mode}\n"
        f"{alerts_line}\n"
        f"{orders_line}\n"
        f"{screener_line}\n"
        f"{warnings_line}"
    )


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
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        notifier.send_text(text)
        book = OrderBook(settings.state_dir / "orders.json")
        notifier.send_text(_build_heartbeat(settings, report, book, day))
    elif telegram:
        raise click.ClickException(
            "Telegram requested but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset"
        )


def _quote_ltp(settings: Settings, symbol: str) -> float:
    """Best-effort NSE LTP for CLI margin sizing. 0 if unavailable."""
    if not settings.kite_api_key or not settings.kite_access_token:
        return 0.0
    try:
        kite = _kite_client(settings.kite_api_key, settings.kite_access_token)
        payload = kite.quote([f"NSE:{symbol.upper()}"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch LTP for %s: %s", symbol, exc)
        return 0.0
    row = payload.get(f"NSE:{symbol.upper()}") if isinstance(payload, dict) else None
    if not isinstance(row, dict):
        return 0.0
    ltp = row.get("last_price") or (row.get("ohlc") or {}).get("close") or 0
    try:
        return float(ltp)
    except (TypeError, ValueError):
        return 0.0


def _cli_size_quantity(
    settings: Settings,
    executor: OrderExecutor,
    symbol: str,
    side: str,
) -> tuple[int, str]:
    """Size a manual CLI order. TRADE_QTY is only used when TRADE_SIZING=fixed."""
    if executor.sizing_mode != "margin":
        qty = max(1, int(settings.trade_qty))
        return qty, f"fixed TRADE_QTY={qty}"
    px = _quote_ltp(settings, symbol)
    if px <= 0:
        raise click.ClickException(
            "No --qty given and could not fetch LTP for margin sizing. "
            "Pass --qty N, or run `nse-alert login` so TRADE_MARGIN_INR can size "
            "the order. Commenting out TRADE_QTY=1 does not enable 10k sizing — "
            "set TRADE_SIZING=margin (default) instead."
        )
    try:
        return executor.size_quantity(symbol=symbol, price=px, side=side)  # type: ignore[arg-type]
    except SizingRefusedError as exc:
        raise click.ClickException(str(exc)) from exc


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
@click.option(
    "--qty",
    type=int,
    default=None,
    help="Quantity (omit to use TRADE_SIZING / TRADE_MARGIN_INR)",
)
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
    txn: str = "BUY" if side.lower() == "buy" else "SELL"
    size_note = "manual CLI order"
    if qty is None:
        qty, size_note = _cli_size_quantity(settings, executor, symbol.upper(), txn)
        click.echo(f"Sizing: {size_note}")
    request = OrderRequest(
        symbol=symbol.upper(),
        side=txn,  # type: ignore[arg-type]
        quantity=qty,
        product=settings.trade_product,
        order_type=settings.trade_order_type,
        price=None,
        trigger_price=None,
        market_protection=settings.trade_market_protection,
        tag="nsealrt",
        reason=size_note,
    )
    mode = "dry_run" if dry_run else "auto"
    if mode == "auto" and (not settings.kite_api_key or not settings.kite_access_token):
        raise click.ClickException("Live orders need KITE_API_KEY and KITE_ACCESS_TOKEN")
    result = executor.place(request, force_mode=mode)
    click.echo(result.message)
    if not result.ok:
        raise SystemExit(1)


@main.command("size")
@click.argument("symbol")
@click.option("--price", type=float, default=None, help="Override LTP (skip Kite quote)")
def size_cmd(symbol: str, price: float | None) -> None:
    """Print how many shares TRADE_MARGIN_INR would buy (no order placed).

    Paste this output when qty comes out as 1 — it shows whether Kite margin
    per share already consumes the ₹10k budget (not TRADE_QTY).
    """
    settings = Settings()
    book = OrderBook(settings.state_dir / "orders.json")
    executor = _build_executor(settings, book)
    sym = symbol.upper()
    click.echo(
        f"TRADE_SIZING={executor.sizing_mode}  TRADE_MARGIN_INR={executor.margin_budget_inr:g}  "
        f"TRADE_QTY={executor.default_qty}  product={executor.product}  "
        f"fallback_lev={executor.fallback_leverage:g}x"
    )
    px = float(price) if price is not None else _quote_ltp(settings, sym)
    if px <= 0:
        raise click.ClickException(
            "No LTP. Pass --price 1234.5 or run `nse-alert login` first."
        )
    click.echo(f"LTP used={px:.2f}")
    raw_row: dict[str, object] | None = None
    if settings.kite_api_key and settings.kite_access_token:
        try:
            _total, _lev, raw_row = executor._margin_for_quantity(  # noqa: SLF001
                symbol=sym, side="BUY", quantity=1, price=px
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"order_margins failed: {exc}")
    if raw_row is not None:
        click.echo(
            "Kite order_margins row: "
            f"total={raw_row.get('total')}  leverage={raw_row.get('leverage')}  "
            f"var={raw_row.get('var')}  span={raw_row.get('span')}  "
            f"exposure={raw_row.get('exposure')}"
        )
        if executor.sizing_mode == "margin":
            try:
                m1 = float(raw_row.get("total") or 0)
                if m1 > 0:
                    click.echo(
                        f"formula: floor({executor.margin_budget_inr:g} / {m1:g}) = "
                        f"{int(executor.margin_budget_inr // m1)} "
                        f"(qty is 1 whenever margin/share > budget/2)"
                    )
            except (TypeError, ValueError):
                pass
    try:
        qty, note = executor.size_quantity(symbol=sym, price=px, side="BUY")
    except SizingRefusedError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"qty={qty}")
    click.echo(f"note={note}")


@main.command("pending")
def pending_cmd() -> None:
    """List orders waiting for Telegram/CLI confirmation."""
    settings = Settings()
    path = (settings.state_dir / "orders.json").resolve()
    book = OrderBook(settings.state_dir / "orders.json")
    click.echo(f"Order book: {path}  (date={book._data.get('date')})")  # noqa: SLF001
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
        raise click.ClickException(
            _pending_not_found_msg(settings, book, pending_id.upper())
        )
    click.echo(f"Pending {pending_id} → {item.status}")


@main.command("trail")
@click.argument("symbol")
@click.argument("qty", type=int)
@click.option(
    "--pct",
    type=float,
    default=None,
    help="Trailing distance %% below the running high (default: TRADE_STOP_LOSS_PCT)",
)
@click.option(
    "--poll-sec",
    type=float,
    default=5.0,
    show_default=True,
    help="Seconds between LTP polls",
)
@click.option(
    "--dry-run/--live",
    default=True,
    help="Dry-run by default; pass --live to send real orders to Kite",
)
def trail_cmd(symbol: str, qty: int, pct: float | None, poll_sec: float, dry_run: bool) -> None:
    """Attach a trailing stop-loss to a position you already hold (MIS/intraday).

    Kite has no native trailing-SL for intraday products, so this places a
    SELL SL-Limit order and polls the LTP, ratcheting the trigger up as the
    price makes new highs — never down. Runs until the stop is hit or
    cancelled (Ctrl+C, or Telegram `/trail_cancel SYMBOL` if TELEGRAM_* is
    configured). One process manages one symbol.
    """
    settings = Settings()
    trail_pct = pct if pct is not None else settings.trade_stop_loss_pct
    mode = "dry_run" if dry_run else "live"
    if mode == "live" and (not settings.kite_api_key or not settings.kite_access_token):
        raise click.ClickException("Live trailing needs KITE_API_KEY and KITE_ACCESS_TOKEN")
    if settings.kite_force_ipv4:
        force_ipv4()

    tg = (
        TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        if settings.telegram_configured
        else None
    )

    def _notify(text: str) -> None:
        click.echo(text)
        if tg:
            tg.send_text(text)

    runner = TrailingStopRunner(
        kite_api_key=settings.kite_api_key,
        kite_access_token=settings.kite_access_token,
        symbol=symbol,
        quantity=qty,
        trail_pct=trail_pct,
        stop_limit_ticks=settings.trade_stop_limit_ticks,
        product=settings.trade_product,
        poll_sec=poll_sec,
        mode=mode,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        on_message=_notify,
    )

    def _handle_sig(_signum: int, _frame: object) -> None:
        runner.cancel(reason="stopped (signal)")

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        runner.run()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


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


@main.command("telegram-chats")
def telegram_chats_cmd() -> None:
    """List recent chats this bot can see (copy a group id into TELEGRAM_CHAT_ID).

    Stop `watch` first so this command can peek at getUpdates. Then send any
    message in the group (or `/confirm test` — it will not place an order)
    and re-run this command.
    """
    import httpx

    from nse_alert.confirm_bot import summarize_chats

    settings = Settings()
    if not settings.telegram_bot_token:
        raise click.ClickException("Set TELEGRAM_BOT_TOKEN in .env")
    token = settings.telegram_bot_token
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10.0,
        ).raise_for_status()
        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"timeout": 0},
            timeout=15.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Telegram API failed: {exc}") from exc
    if not payload.get("ok", True):
        raise click.ClickException(f"Telegram error: {payload.get('description')}")
    chats = summarize_chats(payload.get("result") or [])
    click.echo(f"Configured TELEGRAM_CHAT_ID={settings.telegram_chat_id or '(empty)'}")
    if not chats:
        click.echo(
            "No recent messages. Add the bot to the group, send a message there, "
            "then run this again (stop `watch` first). Group ids are negative, "
            "e.g. -1001234567890."
        )
        return
    click.echo("Recent chats this bot can see:\n")
    for chat in chats:
        mark = (
            "  ← current TELEGRAM_CHAT_ID"
            if chat["id"] == str(settings.telegram_chat_id).strip()
            else ""
        )
        click.echo(
            f"  {chat['id']}  [{chat['type']}] {chat['title'] or '(no title)'}{mark}"
        )
        if chat["text"]:
            click.echo(f"      last: {chat['text']}")
    click.echo(
        "\nTo receive alerts in a group, set TELEGRAM_CHAT_ID to that group's id, "
        "then in BotFather: /setprivacy → Disable (so non-slash messages are seen). "
        "Confirms should use `/confirm <id>`."
    )


@main.command("public-ip")
def public_ip_cmd() -> None:
    """Show this machine's public IPv4 and IPv6 (what Kite whitelist sees)."""
    import httpx

    def _fetch(url: str) -> str:
        try:
            return httpx.get(url, timeout=8.0).text.strip()
        except httpx.HTTPError as exc:
            return f"(failed: {exc})"

    v4 = _fetch("https://api.ipify.org")
    v6 = _fetch("https://api6.ipify.org")
    click.echo(f"IPv4 egress: {v4}")
    click.echo(f"IPv6 egress: {v6}")
    click.echo(
        "Kite Profile → IP Whitelist must contain the address that "
        "`place_order` actually uses. AWS dual-stack often uses IPv6 even "
        "when an Elastic IPv4 is attached. This app defaults to "
        "KITE_FORCE_IPV4=true so orders go out the IPv4. "
        "You can change the whitelist only once per calendar week."
    )


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
