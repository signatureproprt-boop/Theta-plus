"""Shared account gate, not Celery's per-worker rate_limit setting."""
import hashlib
import time
import uuid
from dataclasses import dataclass
from redis import Redis
from app.config import settings

LUA = '''
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local last = tonumber(redis.call('GET', KEYS[2]) or '0')
local cooldown = tonumber(redis.call('GET', KEYS[3]) or '0')
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 86400000)
local family_last = tonumber(redis.call('GET', KEYS[4]) or '0')
local wait = math.max(last + tonumber(ARGV[1]) - now, family_last + tonumber(ARGV[4]) - now, cooldown - now, 0)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  wait = math.max(wait, tonumber(first[2]) + 86400000 - now + 1)
end
if wait > 0 then return {wait, 0, 0} end
local recent = redis.call('ZCOUNT', KEYS[1], now - 1000, '+inf')
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], 86401000)
redis.call('SET', KEYS[2], now, 'PX', 86401000)
redis.call('SET', KEYS[4], now, 'PX', 86401000)
return {0, last > 0 and now - last or -1, recent + 1}
'''
COOLDOWN = '''
local t = redis.call('TIME')
local until_ms = t[1] * 1000 + math.floor(t[2] / 1000) + tonumber(ARGV[1])
local previous = tonumber(redis.call('GET', KEYS[1]) or '0')
if until_ms > previous then redis.call('SET', KEYS[1], until_ms, 'PX', ARGV[1]) end
return 1
'''


class GateDeferred(Exception):
    def __init__(self, seconds):
        self.seconds = max(1, seconds)


@dataclass
class Slot:
    wait_ms: int
    previous_ms: int | None
    recent: int


def redis_client():
    return Redis.from_url(settings().redis_url, socket_connect_timeout=3, socket_timeout=3)


class RateGate:
    def __init__(self, client_id, redis=None):
        self.account_key = hashlib.sha256(client_id.encode()).hexdigest()
        self.redis = redis if redis is not None else redis_client()
        self.prefix = 'dhan:{' + self.account_key + '}'

    def acquire(self, family='history', family_spacing_ms=0):
        s = settings()
        if s.parallel_budget_required:
            from app.parallel_budget import ready
            if not ready():
                raise GateDeferred(60)
        waited = 0
        while True:
            wait, previous, recent = self.redis.eval(
                LUA, 4, self.prefix + ':day', self.prefix + ':last', self.prefix + ':cooldown', self.prefix + ':family:' + family,
                max(s.dhan_min_spacing_ms, 1100) if s.parallel_budget_required else s.dhan_min_spacing_ms,
                min(s.dhan_daily_guard, 40000) if s.parallel_budget_required else s.dhan_daily_guard, uuid.uuid4().hex, max(0, family_spacing_ms))
            if not wait:
                return Slot(waited, None if previous < 0 else previous, recent)
            if wait > 5000:
                raise GateDeferred((wait + 999) // 1000)
            time.sleep(wait / 1000)
            waited += wait

    def cooldown(self, seconds):
        self.redis.eval(COOLDOWN, 1, self.prefix + ':cooldown', max(1, seconds) * 1000)
