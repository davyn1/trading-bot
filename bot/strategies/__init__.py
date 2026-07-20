"""Strategy registry. main.py builds strategies purely from config, so a new
instrument (or a removed one) never requires touching strategy code."""

import config

from .base import Signal, Strategy
from .mean_reversion import MeanReversion
from .momentum_breakout import MomentumBreakout
from .trend_following import TrendFollowing

REGISTRY = {
    MeanReversion.name: MeanReversion,
    MomentumBreakout.name: MomentumBreakout,
    TrendFollowing.name: TrendFollowing,
}


def build_strategy(symbol: str) -> Strategy:
    """Instantiate the configured strategy for a symbol, applying any
    per-instrument parameter overrides from config.INSTRUMENTS."""
    inst = config.INSTRUMENTS[symbol]
    strat_name = inst["strategy"]
    if strat_name not in REGISTRY:
        raise ValueError(f"Unknown strategy '{strat_name}' for {symbol}")
    params = dict(config.STRATEGIES[strat_name]["params"])
    params.update(inst.get("params", {}))
    return REGISTRY[strat_name](symbol, params)
