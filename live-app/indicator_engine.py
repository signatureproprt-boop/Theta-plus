"""Authoritative, prefix-only indicators from closed provider minutes.

No provider calls, orders, browser calculations or synthetic gap filling.
EMA is SMA-seeded; RSI/DI/ADX use Wilder smoothing (14 changes, 27 for ADX).
"""
from datetime import datetime, timedelta
from math import isfinite
from runtime import IST

VERSION = 'closed-candle-1.0'
PERIODS = (1, 5, 15, 60)


def origin(t):
    return datetime.fromtimestamp(t, IST).replace(hour=9, minute=15, second=0, microsecond=0)


def normalized(candles, clock):
    rows = {}; conflicts = set(); excluded = 0
    for row in candles:
        t = row.get('timestamp')
        if (isinstance(t, bool) or not isinstance(t, (int, float)) or not isfinite(t)
                or t % 60 or t + 60 > clock.timestamp()):
            excluded += 1; continue
        at = datetime.fromtimestamp(t, IST); offset = (at-origin(t)).total_seconds()/60
        values = [row.get(k) for k in ('open', 'high', 'low', 'close')]
        if (at.weekday() > 4 or not 0 <= offset < 375
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0 for v in values)
                or row['low'] > min(row['open'], row['close'])
                or row['high'] < max(row['open'], row['close']) or row['high'] < row['low']):
            excluded += 1; continue
        if t in rows and any(rows[t].get(k) != row.get(k) for k in ('open', 'high', 'low', 'close', 'volume')):
            conflicts.add(t)
        else:
            rows[int(t)] = row
    return {t: r for t, r in rows.items() if t not in conflicts}, excluded, len(conflicts)


def grid(rows, minutes, clock):
    bars = []
    for start in sorted({int(origin(t).timestamp()) for t in rows}):
        count = min(375, max(0, int((clock.timestamp()-start)//60)))
        for offset in range(0, count-minutes+1, minutes):
            time = start + offset*60
            group = [rows.get(time+i*60) for i in range(minutes)]
            if any(r is None for r in group):
                bars.append({'time': time}); continue
            bars.append({'time': time, 'open': group[0]['open'], 'high': max(r['high'] for r in group),
                         'low': min(r['low'] for r in group), 'close': group[-1]['close'],
                         'volume': sum(r.get('volume', 0) if isinstance(r.get('volume', 0), (int, float)) and isfinite(r.get('volume', 0)) and r.get('volume', 0) >= 0 else 0 for r in group)})
    return bars


def consecutive(previous, current, minutes):
    if previous is None: return True
    if origin(previous) == origin(current): return current-previous == minutes*60
    # Carry overnight only from the final full bucket to the next session open.
    return (previous == int(origin(previous).timestamp()) + (375//minutes-1)*minutes*60
            and current == int(origin(current).timestamp()))


def frame(rows, minutes, clock):
    return calculate(grid(rows, minutes, clock), minutes)


def calculate(bars, minutes):
    names = ('ema20', 'ema50', 'rsi14', 'adx14', 'plus_di', 'minus_di')
    series = {k: [] for k in names}; states = []; sums = {20: 0., 50: 0.}; averages = {20: None, 50: None}
    n = 0; previous = None; previous_time = None; smoothed = None; seed = [0.]*5; changes = 0; dx_seed = 0.; dx_count = 0; adx = None
    for bar in bars:
        t = bar['time']
        if 'close' not in bar or not consecutive(previous_time, t, minutes):
            n = 0; sums = {20: 0., 50: 0.}; averages = {20: None, 50: None}
            previous = None; smoothed = None; seed = [0.]*5; changes = 0; dx_seed = 0.; dx_count = 0; adx = None
        values = {k: None for k in names}
        if 'close' in bar:
            n += 1
            for period in (20, 50):
                if n <= period: sums[period] += bar['close']
                if n == period: averages[period] = sums[period]/period
                elif n > period: averages[period] += (bar['close']-averages[period])*2/(period+1)
                values['ema'+str(period)] = averages[period]
            if previous is not None:
                delta = bar['close']-previous['close']; up = bar['high']-previous['high']; down = previous['low']-bar['low']
                sample = [max(delta, 0), max(-delta, 0), max(bar['high']-bar['low'], abs(bar['high']-previous['close']), abs(bar['low']-previous['close'])), up if up > down and up > 0 else 0, down if down > up and down > 0 else 0]
                changes += 1
                if changes <= 14:
                    seed = [a+b for a,b in zip(seed, sample)]
                    if changes == 14: smoothed = [v/14 for v in seed]
                else: smoothed = [(a*13+b)/14 for a,b in zip(smoothed, sample)]
                if smoothed is not None:
                    gain, loss, tr, plus, minus = smoothed
                    values['rsi14'] = 100-100/(1+gain/loss) if loss else 100. if gain else 50.
                    pdi = 100*plus/tr if tr else 0.; mdi = 100*minus/tr if tr else 0.
                    values.update(plus_di=pdi, minus_di=mdi)
                    dx = 100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi else 0.
                    dx_count += 1
                    if dx_count <= 14: dx_seed += dx
                    if dx_count == 14: adx = dx_seed/14
                    elif dx_count > 14: adx = (adx*13+dx)/14
                    values['adx14'] = adx
            previous = bar
        else: previous = None
        previous_time = t
        e20, e50 = values['ema20'], values['ema50']
        states.append({'bars': n, 'ema20': e20, 'ema50': e50, 'previous_ema20': series['ema20'][-1].get('value') if series['ema20'] and n > 1 else None,
                       'bias': 'UNAVAILABLE' if e20 is None or e50 is None else 'BULLISH' if e20 > e50 else 'BEARISH' if e20 < e50 else 'SIDEWAYS'})
        for name in names:
            series[name].append({'time': t, **({'value': values[name]} if values[name] is not None else {})})
    last = bars[-1] if bars else None
    latest = {k: series[k][-1].get('value') if series[k] else None for k in names}
    e20, e50 = latest['ema20'], latest['ema50']
    snapshot = {'bars': sum('close' in r for r in bars), 'consecutive_bars': n,
                'missing_buckets': sum('close' not in r for r in bars), **latest,
                'timestamp': last['time'] if last else None,
                'bias': 'UNAVAILABLE' if e20 is None or e50 is None else 'BULLISH' if e20 > e50 else 'BEARISH' if e20 < e50 else 'SIDEWAYS',
                'previous_ema20': states[-1]['previous_ema20'] if states else None,
                'state': 'MISSING' if not last else 'GAP' if 'close' not in last else 'WARMUP' if e50 is None or latest['adx14'] is None else 'CALCULATED'}
    return {'bars': [{k:v for k,v in r.items() if k != 'volume'} for r in bars], **series, 'snapshot': snapshot, 'states': states}


def opening_range(rows, day, clock):
    start = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15)
    opening = [rows.get(int(start.timestamp())+i*60) for i in range(15)]
    complete = clock >= start+timedelta(minutes=15) and all(opening)
    return {'state': 'COMPLETE' if complete else 'INCOMPLETE', 'date': day.isoformat(),
            'high': max(r['high'] for r in opening) if complete else None,
            'low': min(r['low'] for r in opening) if complete else None}


def build(candles, clock):
    rows, excluded, conflicts = normalized(candles, clock)
    frames = {str(m): frame(rows, m, clock) for m in PERIODS}
    for f in frames.values(): f.pop('states')
    loaded_day = datetime.fromtimestamp(max(rows), IST).date() if rows else None
    latest = [r for t,r in rows.items() if datetime.fromtimestamp(t, IST).date() == loaded_day]
    orb = opening_range(rows, loaded_day, clock) if loaded_day else {'state': 'INCOMPLETE', 'date': None, 'high': None, 'low': None}
    levels = []
    if latest:
        label = 'Today' if loaded_day == clock.astimezone(IST).date() else 'Loaded session'
        levels = [{'title': label+' high', 'price': max(r['high'] for r in latest), 'color': '#8b6b37'},
                  {'title': label+' low', 'price': min(r['low'] for r in latest), 'color': '#8b6b37'}]
        if orb['state'] == 'COMPLETE':
            levels += [{'title': 'ORB 15m high', 'price': orb['high'], 'color': '#7755b5'},
                       {'title': 'ORB 15m low', 'price': orb['low'], 'color': '#7755b5'}]
    return {'version': VERSION, 'clock': clock.isoformat(), 'frames': frames, 'levels': levels, 'orb': orb,
            'loadedDay': loaded_day.isoformat() if loaded_day else None, 'excluded': excluded, 'conflicts': conflicts,
            'loadedHigh': max(r['high'] for r in latest) if latest else None,
            'loadedLow': min(r['low'] for r in latest) if latest else None,
            'basis': 'Closed Dhan OHLC only; SMA-seeded EMA; Wilder RSI/ADX; gaps reset warm-up; ORB 09:15–09:30 IST. Values do not certify freshness.'}
