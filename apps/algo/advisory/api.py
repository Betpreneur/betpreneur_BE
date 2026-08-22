"""Public advisory API.

Advisory turns market probabilities, data quality, bookmaker context, and leg
state into the claim the product is willing to publish.
"""

from .leg_state import LegAssessment, LegState, assess_leg, may_publish_probability
from .statpal_advisory import StatPalAdvisory, StatPalMarketAdvisoryService, statpal_market_advisory

__all__ = [
    "LegAssessment",
    "LegState",
    "StatPalAdvisory",
    "StatPalMarketAdvisoryService",
    "assess_leg",
    "may_publish_probability",
    "statpal_market_advisory",
]
