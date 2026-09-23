# sheets package: Google Sheets control-room adapter (Phase D).
#
# ARCHITECTURE SEPARATION (master rule): the signal engine NEVER calls Google
# APIs. Flow is strictly  SignalDecision/MarketFeatures -> Sheets adapter.
# All credentials come from environment variables — never hardcoded, never logged.
