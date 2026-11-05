"""Риск-менеджмент: жёсткие лимиты, cooldown, kill switch и размер позиции. Версия параметров: RISK_VERSION.

Параметры обоснованы в docs/trading_lab/STAGE_A_ARCHITECTURE.md §8. Любое изменение — новая версия.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .execution import ZERO, CostModel, dec, max_trade_usd

RISK_VERSION = "risk-v1"


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: Decimal       # доля equity, теряемая при срабатывании стопа (с издержками)
    max_exposure: Decimal         # максимальная доля equity в FABLES
    period_sec: int               # длительность периода для лимита убытка
    period_loss_limit: Decimal    # доля equity на начало периода
    max_drawdown: Decimal         # от пика equity → kill switch портфеля
    loss_streak: int              # столько убыточных сделок подряд → cooldown
    loss_streak_cooldown: int     # баров
    stop_cooldown: int            # баров после стопа
    anomaly_tr_atr: float         # TR > N·ATR → аномалия
    anomaly_pause: int            # баров
    min_rr: float                 # минимальный чистый R:R
    min_trade_usd: Decimal = Decimal("20")

    def d(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


RISK = {
    "1h": RiskConfig(Decimal("0.0075"), Decimal("0.60"), 86400, Decimal("0.02"), Decimal("0.12"), 3, 24, 4, 4.0, 3, 2.0),
    "4h": RiskConfig(Decimal("0.010"), Decimal("0.80"), 7 * 86400, Decimal("0.04"), Decimal("0.18"), 3, 12, 2, 4.0, 2, 2.0),
    "1d": RiskConfig(Decimal("0.015"), Decimal("0.90"), 30 * 86400, Decimal("0.08"), Decimal("0.25"), 2, 5, 1, 4.0, 1, 2.5),
}


def params_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


@dataclass
class RiskState:
    peak_equity: Decimal
    period_start_ts: int = 0
    period_start_equity: Decimal = ZERO
    loss_streak: int = 0
    cooldown_until_idx: int = -1
    cooldown_reason: str = ""
    killed: bool = False
    kill_reason: str = ""
    history: list = field(default_factory=list)

    def to_state(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

    @classmethod
    def from_state(cls, s: dict) -> "RiskState":
        r = cls(dec(s["peak_equity"]))
        for k, v in s.items():
            setattr(r, k, dec(v) if k in ("peak_equity", "period_start_equity") else v)
        return r


def on_bar(rs: RiskState, cfg: RiskConfig, equity: Decimal, ts: int) -> None:
    """Обновление пика, периода и kill switch по equity на закрытии бара."""
    if equity > rs.peak_equity:
        rs.peak_equity = equity
    if rs.period_start_ts == 0 or ts - rs.period_start_ts >= cfg.period_sec:
        rs.period_start_ts = ts - (ts % cfg.period_sec) if cfg.period_sec <= 86400 else ts
        rs.period_start_equity = equity
    dd = (rs.peak_equity - equity) / rs.peak_equity if rs.peak_equity > 0 else ZERO
    if not rs.killed and dd >= cfg.max_drawdown:
        rs.killed, rs.kill_reason = True, f"просадка {dd * 100:.1f}% ≥ лимита {cfg.max_drawdown * 100:.0f}% — портфель остановлен"


def on_trade_closed(rs: RiskState, cfg: RiskConfig, pnl: Decimal, by_stop: bool, idx: int) -> None:
    rs.loss_streak = rs.loss_streak + 1 if pnl < 0 else 0
    if rs.loss_streak >= cfg.loss_streak:
        _cool(rs, idx + cfg.loss_streak_cooldown, f"{rs.loss_streak} убыточных сделок подряд")
    if by_stop:
        _cool(rs, idx + cfg.stop_cooldown, "cooldown после стопа")


def on_anomaly(rs: RiskState, cfg: RiskConfig, idx: int, tr_atr: float) -> None:
    _cool(rs, idx + cfg.anomaly_pause, f"аномальная свеча: TR = {tr_atr:.1f}·ATR")


def _cool(rs: RiskState, until: int, why: str) -> None:
    if until > rs.cooldown_until_idx:
        rs.cooldown_until_idx, rs.cooldown_reason = until, why


def entry_checks(rs: RiskState, cfg: RiskConfig, equity: Decimal, idx: int, rr: float | None,
                 global_kill: bool = False) -> list[dict]:
    """Список проверок; вход разрешён, если все ok."""
    period_loss = (rs.period_start_equity - equity) / rs.period_start_equity if rs.period_start_equity > 0 else ZERO
    return [
        {"name": "Global kill switch", "ok": not global_kill, "detail": "система остановлена" if global_kill else "выключен"},
        {"name": "Kill switch портфеля", "ok": not rs.killed, "detail": rs.kill_reason or "не сработал"},
        {"name": "Лимит убытка периода", "ok": period_loss < cfg.period_loss_limit,
         "detail": f"убыток периода {period_loss * 100:.2f}% (лимит {cfg.period_loss_limit * 100:.1f}%)"},
        {"name": "Cooldown", "ok": idx > rs.cooldown_until_idx,
         "detail": f"до бара {rs.cooldown_until_idx}: {rs.cooldown_reason}" if idx <= rs.cooldown_until_idx else "нет"},
        {"name": f"Чистый R:R ≥ {cfg.min_rr}", "ok": rr is not None and rr >= cfg.min_rr,
         "detail": "не рассчитан" if rr is None else f"{rr:.2f}"},
    ]


def position_size(cfg: RiskConfig, m: CostModel, equity: Decimal, cash: Decimal, entry_eff: Decimal,
                  stop_eff: Decimal) -> tuple[Decimal, str]:
    """USD на покупку. entry_eff/stop_eff — эффективные цены с издержками (цена покупки и цена продажи на стопе)."""
    per_unit = entry_eff - stop_eff
    if per_unit <= 0:
        return ZERO, "стоп не ниже входа"
    by_risk = equity * cfg.risk_per_trade / per_unit * entry_eff
    caps = {"риск": by_risk, "доля капитала": equity * cfg.max_exposure, "кеш": cash - m.gas_usd * 2,
            "ликвидность": max_trade_usd(m)}
    usd = min(caps.values())
    binding = min(caps, key=lambda k: caps[k])
    if usd < cfg.min_trade_usd:
        return ZERO, f"размер {usd:.2f} < минимума {cfg.min_trade_usd} (ограничение: {binding})"
    return usd, binding
