"""每個來源 IP、每類端點一個 token bucket；超過回 429＋Retry-After"""
import threading
import time


class RateLimiter:
    def __init__(self, rules: dict, max_sources: int = 10000):
        """rules：{"register": (5, 60), …}＝每 60 秒 5 次"""
        self.rules = rules
        self.max_sources = max_sources
        self.buckets = {}
        self.lock = threading.Lock()

    def check(self, source: str, kind: str):
        """回傳 0＝可以；>0＝要等幾秒"""
        if kind not in self.rules:
            return 0
        limit, per = self.rules[kind]
        rate = limit / per
        now = time.monotonic()
        with self.lock:
            if len(self.buckets) > self.max_sources:
                self.buckets.clear()                      # 極端情況：寧可重算也不吃光記憶體
            tokens, last = self.buckets.get((source, kind), (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens >= 1:
                self.buckets[(source, kind)] = (tokens - 1, now)
                return 0
            self.buckets[(source, kind)] = (tokens, now)
            return max(1, int((1 - tokens) / rate + 0.999))


DEFAULT_RULES = {
    "register": (5, 60),
    "registration_poll": (12, 60),
    "heartbeat": (2, 1),
    "renew": (2, 60),
    "disconnect": (6, 60),
    "info": (30, 60),
    "other": (30, 60),
}
