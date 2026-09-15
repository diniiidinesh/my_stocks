from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

import click

from nse_alert.config import Settings
from nse_alert.engine import AlertEngine, parse_thresholds
from nse_alert.feed import KiteFeed, MockFeed
from nse_alert.notify import build_notifier
from nse_alert.universe import Instrument, build_universe

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

    engine = AlertEngine(
        prev_closes=prev_closes,
        thresholds=thresholds,
        state_path=state_path,
    )
    notifier = build_notifier(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        always_console=True,
    )

    alert_count = {"n": 0}

    def on_tick(symbol: str, ltp: float) -> None:
        for alert in engine.on_tick(symbol, ltp):
            notifier.send(alert)
            alert_count["n"] += 1

    threshold_label = ",".join(f"{t:g}" for t in thresholds)
    logger.info(
        "Watching %d symbols | thresholds=±%s%% | feed=%s | telegram=%s",
        len(instruments),
        threshold_label,
        feed_mode,
        "yes" if settings.telegram_configured else "console-only",
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

    def _handle_sig(_signum: int, _frame: object) -> None:
        stop["flag"] = True
        price_feed.stop()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

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

    logger.info("Done. Alerts fired this run: %d", alert_count["n"])
    if not use_kite and alert_count["n"] == 0:
        raise SystemExit(1)


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
