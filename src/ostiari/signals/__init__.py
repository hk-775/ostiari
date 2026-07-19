"""Risk signal providers — pluggable inputs to the gateway's risk score.

Each provider inspects an action + its params and returns a RiskSignal (or None).
The gateway sums signal contributions into the final 0-100 score. This is the
seam for making risk *parameter-aware* rather than action-name-only.
"""

from ostiari.signals.parameter_risk import ParameterRiskSignal

__all__ = ["ParameterRiskSignal"]
