"""What slips provides to the modules below it.

Both registrations invert a dependency that would otherwise point upward:
billing needs to know whether a review was delivered before refunding an
expired reservation, and pricing calibrates ticket risk against legs that have
actually settled. Neither can reach slips, so slips supplies the answers.
"""
from __future__ import annotations


def register() -> None:
    from .services.billing_delivery import register as register_billing_delivery
    from .services.calibration import register as register_calibration
    from .services.priority_fixtures import register as register_priority_fixtures

    register_billing_delivery()
    register_calibration()
    # Scoring refreshes lineups only for fixtures users still have money on.
    register_priority_fixtures()
