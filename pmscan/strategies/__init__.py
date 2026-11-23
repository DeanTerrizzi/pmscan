from .base import Candidate, ScanContext, Strategy
from .high_prob import HighProbability
from .ladder_arb import LadderArbitrage
from .near_certain import NearCertain
from .neg_risk_arb import NegRiskArbitrage
from .sportsbook import SportsbookValue

# Each name matches a section of ScanConfig (and of pmscan.toml).
ALL_STRATEGIES: dict[str, type[Strategy]] = {
    NegRiskArbitrage.name: NegRiskArbitrage,
    LadderArbitrage.name: LadderArbitrage,
    HighProbability.name: HighProbability,
    NearCertain.name: NearCertain,
    SportsbookValue.name: SportsbookValue,
}

ARBITRAGE = frozenset({NegRiskArbitrage.name, LadderArbitrage.name})

__all__ = [
    "ALL_STRATEGIES", "ARBITRAGE", "Candidate", "HighProbability", "LadderArbitrage",
    "NearCertain", "NegRiskArbitrage", "ScanContext", "SportsbookValue", "Strategy",
]
