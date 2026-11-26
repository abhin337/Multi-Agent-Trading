"""Deterministic NSE market fixtures for offline replay, demos, and testing."""

from __future__ import annotations

from datetime import date, timedelta

from schemas.models import MarketNewsItem, NSEQuote, OHLCVBar

# Canonical active NSE symbols for the allowlist & fixtures
ACTIVE_NSE_SYMBOLS: dict[str, str] = {
    "INFY": "Infosys Limited",
    "TCS": "Tata Consultancy Services Limited",
    "RELIANCE": "Reliance Industries Limited",
    "ICICIBANK": "ICICI Bank Limited",
    "SBIN": "State Bank of India",
    "LT": "Larsen & Toubro Limited",
    "HDFCBANK": "HDFC Bank Limited",
    "TATAMOTORS": "Tata Motors Limited",
    "TATASTEEL": "Tata Steel Limited",
    "SUZLON": "Suzlon Energy Limited",
    "IDEA": "Vodafone Idea Limited",
}

# Delisted / merged NSE tickers that must be caught and never traded as stale symbols
MERGED_OR_DELISTED_SYMBOLS: dict[str, str] = {
    "HDFC": "Merged into HDFCBANK (delisted standalone ticker)",
    "MINDTREE": "Merged into LTIM",
    "LTI": "Merged into LTIM",
}

# Ambiguous group / partial company names requiring disambiguation
AMBIGUOUS_COMPANY_ALIASES: dict[str, list[str]] = {
    "TATA": ["TCS", "TATAMOTORS", "TATASTEEL"],
    "RELIANCE GROUP": ["RELIANCE"],
    "HDFC GROUP": ["HDFCBANK"],
}


def _generate_bullish_ohlcv(
    base_price: float,
    final_close: float,
    base_volume: int,
    breakout_volume_mult: float = 1.85,
    bars: int = 35,
) -> list[OHLCVBar]:
    """Build deterministic ascending OHLCV bars ending in a clean volume breakout."""
    start = date(2026, 10, 1)
    series: list[OHLCVBar] = []
    # First (bars - 1) bars rise steadily inside a consolidation channel
    pre_target = final_close * 0.965
    step = (pre_target - base_price) / max(bars - 2, 1)
    for idx in range(bars - 1):
        close = round(base_price + step * idx + ((-1) ** idx) * (base_price * 0.002), 2)
        open_p = round(close * 0.996, 2)
        high = round(close * 1.006, 2)
        low = round(close * 0.991, 2)
        vol = int(base_volume * (0.95 + (idx % 4) * 0.04))
        series.append(
            OHLCVBar(
                date=(start + timedelta(days=idx)).isoformat(),
                open=open_p,
                high=high,
                low=low,
                close=close,
                volume=vol,
            )
        )

    # Final breakout bar above prior resistance on expanded volume
    prev_close = series[-1].close
    series.append(
        OHLCVBar(
            date=(start + timedelta(days=bars - 1)).isoformat(),
            open=round(prev_close * 1.004, 2),
            high=round(final_close * 1.008, 2),
            low=round(prev_close * 1.001, 2),
            close=round(final_close, 2),
            volume=int(base_volume * breakout_volume_mult),
        )
    )
    return series


def _generate_flat_weak_ohlcv(
    base_price: float, base_volume: int, bars: int = 35
) -> list[OHLCVBar]:
    """Build deterministic range-bound/weak OHLCV bars that should NOT trigger a breakout."""
    start = date(2026, 10, 1)
    series: list[OHLCVBar] = []
    for idx in range(bars):
        drift = -0.0015 * idx + (0.002 if idx % 2 == 0 else -0.002)
        close = round(base_price * (1.0 + drift), 2)
        series.append(
            OHLCVBar(
                date=(start + timedelta(days=idx)).isoformat(),
                open=round(close * 1.003, 2),
                high=round(close * 1.007, 2),
                low=round(close * 0.992, 2),
                close=close,
                volume=int(base_volume * 0.75),
            )
        )
    return series


FIXTURE_OHLCV: dict[str, list[OHLCVBar]] = {
    "INFY": _generate_bullish_ohlcv(1495.0, 1565.0, 4_200_000, breakout_volume_mult=1.9),
    "TCS": _generate_bullish_ohlcv(3720.0, 3920.0, 2_100_000, breakout_volume_mult=1.75),
    "RELIANCE": _generate_bullish_ohlcv(2780.0, 2945.0, 6_500_000, breakout_volume_mult=1.8),
    "ICICIBANK": _generate_bullish_ohlcv(1195.0, 1280.0, 8_100_000, breakout_volume_mult=1.7),
    "SBIN": _generate_bullish_ohlcv(765.0, 825.0, 11_200_000, breakout_volume_mult=1.65),
    "LT": _generate_bullish_ohlcv(3420.0, 3620.0, 1_900_000, breakout_volume_mult=1.75),
    "HDFCBANK": _generate_bullish_ohlcv(1610.0, 1710.0, 9_400_000, breakout_volume_mult=1.6),
    "TATAMOTORS": _generate_flat_weak_ohlcv(940.0, 5_500_000),
    "TATASTEEL": _generate_flat_weak_ohlcv(152.0, 18_000_000),
    "SUZLON": _generate_flat_weak_ohlcv(64.0, 25_000_000),
    "IDEA": _generate_flat_weak_ohlcv(11.5, 90_000_000),
}


def build_fixture_quotes() -> dict[str, NSEQuote]:
    """Derive consistent live NSEQuote snapshots from the latest OHLCV fixture bar."""
    quotes: dict[str, NSEQuote] = {}
    for symbol, bars in FIXTURE_OHLCV.items():
        latest = bars[-1]
        prev = bars[-2]
        chg = round(((latest.close - prev.close) / prev.close) * 100.0, 2)
        quotes[symbol] = NSEQuote(
            symbol=symbol,
            exchange="NSE",
            ltp=latest.close,
            open_price=latest.open,
            high_price=latest.high,
            low_price=latest.low,
            volume=latest.volume,
            change_pct=chg,
            timestamp=f"{latest.date}T15:15:00+05:30",
        )
    return quotes


FIXTURE_QUOTES: dict[str, NSEQuote] = build_fixture_quotes()

FIXTURE_NEWS: dict[str, list[MarketNewsItem]] = {
    "INFY": [
        MarketNewsItem(
            source="NSE Corporate Filings",
            headline="Infosys announces USD 1.5B multi-year cloud & AI transformation deal",
            body=(
                "Infosys reported strong deal pipeline expansion in BFSI and raised FY "
                "constant-currency revenue guidance following large enterprise AI wins."
            ),
            published_at="2026-11-26T09:05:00+05:30",
        ),
        MarketNewsItem(
            source="Economic Markets Wire",
            headline="IT index rallies as institutional inflows lift INFY above key resistance",
            body=(
                "Analysts highlight improving operating margins and healthy order book visibility."
            ),
            published_at="2026-11-26T10:20:00+05:30",
        ),
    ],
    "TCS": [
        MarketNewsItem(
            source="NSE Corporate Filings",
            headline="TCS flags unexpected Q3 margin compression and major client ramp-downs",
            body=(
                "Management issued a sharp earnings warning citing delayed discretionary "
                "spending in North America and unexpected contract cancellations."
            ),
            published_at="2026-11-26T08:45:00+05:30",
        ),
    ],
    "RELIANCE": [
        MarketNewsItem(
            source="Market Desk",
            headline="Reliance Industries retail and digital ARPU expansion beats street estimates",
            body="Brokerages reiterate positive outlook on O2C recovery and telecom growth.",
            published_at="2026-11-26T09:30:00+05:30",
        ),
    ],
    "ICICIBANK": [
        MarketNewsItem(
            source="Banking Wire",
            headline="ICICI Bank posts stable asset quality and 18% YoY credit growth",
            body="Net interest margins remained resilient with benign credit costs.",
            published_at="2026-11-26T09:40:00+05:30",
        ),
    ],
    "SBIN": [
        MarketNewsItem(
            source="PSU Markets",
            headline="SBI reports steady deposit growth and improved treasury gains",
            body="Corporate loan pipeline shows sustained momentum across infrastructure.",
            published_at="2026-11-26T10:00:00+05:30",
        ),
    ],
    "LT": [
        MarketNewsItem(
            source="Infra Digest",
            headline="Larsen & Toubro secures mega grid-transmission EPC order",
            body="Order inflows remain ahead of annual guidance across domestic and MENA projects.",
            published_at="2026-11-26T10:15:00+05:30",
        ),
    ],
}
