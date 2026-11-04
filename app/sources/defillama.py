"""DefiLlama free API (без ключа). Проверено 2026-11-04.

- /protocols: поле address вида "robinhood:0x…" — курируемая связь протокол↔токен.
- /protocol/{slug}: история TVL, currentChainTvls (в т.ч. borrowed/staking), raises, audits.
- /summary/fees/{slug}?dataType=dailyFees|dailyRevenue|dailyHoldersRevenue (+methodology).
  Для протоколов без адаптера комиссий — HTTP 400 (это «нет данных», а не 0).
- /summary/dexs/{slug}, /v2/chains, /v2/historicalChainTvl/{chain},
  /overview/dexs/{chain}, /overview/fees/{chain}, stablecoins.llama.fi/stablecoincharts/{chain}.
"""
from __future__ import annotations

from urllib.parse import quote

from ..http import SourceError, get_json

API = "https://api.llama.fi"
STABLES = "https://stablecoins.llama.fi"
SRC = "defillama"


def protocols() -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{API}/protocols", cache_ttl=6 * 3600, save_raw=False)
    return data or [], raw


def protocol(slug: str) -> tuple[dict | None, int | None]:
    return get_json(SRC, f"{API}/protocol/{quote(slug)}", allow_404=True)


def fees_summary(slug: str, data_type: str = "dailyFees") -> tuple[dict | None, int | None]:
    try:
        return get_json(SRC, f"{API}/summary/fees/{quote(slug)}", params={"dataType": data_type}, allow_404=True)
    except SourceError as e:
        if e.status == 400:  # нет адаптера комиссий
            return None, None
        raise


def dex_summary(slug: str) -> tuple[dict | None, int | None]:
    try:
        return get_json(SRC, f"{API}/summary/dexs/{quote(slug)}", allow_404=True)
    except SourceError as e:
        if e.status == 400:
            return None, None
        raise


def chains() -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{API}/v2/chains", cache_ttl=3600)
    return data or [], raw


def chain_tvl_history(chain: str) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{API}/v2/historicalChainTvl/{quote(chain)}", cache_ttl=3600, allow_404=True)
    return data or [], raw


def chain_dex_overview(chain: str) -> tuple[dict | None, int | None]:
    try:
        return get_json(SRC, f"{API}/overview/dexs/{quote(chain)}",
                        params={"excludeTotalDataChartBreakdown": "true"}, cache_ttl=3600, allow_404=True)
    except SourceError as e:
        if e.status == 400:
            return None, None
        raise


def chain_fees_overview(chain: str) -> tuple[dict | None, int | None]:
    try:
        return get_json(SRC, f"{API}/overview/fees/{quote(chain)}",
                        params={"excludeTotalDataChartBreakdown": "true"}, cache_ttl=3600, allow_404=True)
    except SourceError as e:
        if e.status == 400:
            return None, None
        raise


def chain_stablecoins(chain: str) -> tuple[list[dict], int | None]:
    data, raw = get_json(SRC, f"{STABLES}/stablecoincharts/{quote(chain)}", cache_ttl=3600, allow_404=True)
    return data or [], raw
