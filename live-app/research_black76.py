"""Explicit research-only Black-76 option pricing adapter.

Inputs are forward and option prices in points, annual continuously compounded
rate, years to expiry, and decimal volatility. No market data is fetched here.
"""
from math import isfinite


def calculate(side, forward, strike, years, rate, volatility):
    from vollib.black import black
    from vollib.black.greeks.analytical import delta, gamma, theta, vega
    values = (forward, strike, years, rate, volatility)
    if side not in ('CE', 'PE') or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
        raise ValueError('Invalid inputs')
    if forward <= 0 or strike <= 0 or years <= 0 or volatility <= 0:
        raise ValueError('Forward, strike, time and volatility must be positive')
    flag = 'c' if side == 'CE' else 'p'
    args = (flag, forward, strike, years, rate, volatility)
    result = {'price': float(black(*args)), 'delta': float(delta(*args)), 'gamma': float(gamma(*args)), 'theta': float(theta(*args)), 'vega': float(vega(*args))}
    if not all(isfinite(v) for v in result.values()):
        raise ValueError('Non-finite result')
    return {'model': 'Black-76', 'inputs': {'side': side, 'forward': forward, 'strike': strike, 'years': years, 'rate': rate, 'volatility': volatility}, 'units': {'theta': 'per calendar day', 'vega': 'per one percentage point of volatility'}, **result}
