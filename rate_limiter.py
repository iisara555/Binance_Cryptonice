"""
Rate Limiter Module
==================
Token bucket rate limiter for API requests.
Prevents hitting exchange rate limits.
"""
from __future__ import annotations

import time
import threading
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for API requests.
    
    Features:
    - Configurable rate (tokens per second)
    - Configurable burst capacity
    - Thread-safe operations
    - Automatic refill
    """
    
    def __init__(
        self,
        rate: float = 10.0,
        capacity: int = 10,
        name: str = "default"
    ):
        """
        Initialize rate limiter.
        
        Args:
            rate: Number of tokens added per second
            capacity: Maximum number of tokens (burst size)
            name: Identifier for logging
        """
        if float(rate) <= 0:
            raise ValueError("rate must be > 0")
        if int(capacity) <= 0:
            raise ValueError("capacity must be > 0")

        self.rate = rate
        self.capacity = capacity
        self.name = name
        self._tokens = float(capacity)
        self._last_update = time.time()
        self._lock = threading.Lock()
        self._total_requests = 0
        self._total_waits = 0.0
        self._total_waited = 0.0
        
        logger.info(f"RateLimiter [{name}] initialized: {rate} req/s, burst={capacity}")
    
    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update
        
        # Add tokens based on rate and elapsed time
        tokens_to_add = elapsed * self.rate
        self._tokens = min(self.capacity, self._tokens + tokens_to_add)
        self._last_update = now
    
    def acquire(self, tokens: int = 1, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens from the bucket.
        
        Args:
            tokens: Number of tokens to acquire
            blocking: If True, wait until tokens are available
            timeout: Maximum time to wait (None = wait forever)
            
        Returns:
            True if tokens acquired, False if timeout expired
        """
        if int(tokens) <= 0:
            raise ValueError("tokens must be > 0")
        if timeout is not None and float(timeout) < 0:
            return False
        if tokens > self.capacity:
            # Cannot ever satisfy request larger than bucket capacity.
            logger.warning(
                "RateLimiter [%s] rejected acquire(tokens=%s): exceeds capacity=%s",
                self.name,
                tokens,
                self.capacity,
            )
            return False

        start_time = time.time()
        
        while True:
            with self._lock:
                self._refill()
                
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._total_requests += 1
                    wait_time = time.time() - start_time
                    if wait_time > 0.001:
                        self._total_waits += 1
                        self._total_waited += wait_time
                        logger.debug(f"RateLimiter [{self.name}] waited {wait_time*1000:.1f}ms")
                    return True
                
                if not blocking:
                    return False
                
                # Calculate wait time
                tokens_needed = tokens - self._tokens
                wait_for = tokens_needed / self.rate
                
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed + wait_for > timeout:
                        return False
                
                # Release lock while waiting
                time_to_sleep = min(wait_for, 0.1)  # Sleep max 100ms at a time
            
            time.sleep(time_to_sleep)
    
    def try_acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens without blocking."""
        return self.acquire(tokens, blocking=False)
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        with self._lock:
            self._refill()
            return {
                'name': self.name,
                'available_tokens': self._tokens,
                'capacity': self.capacity,
                'rate': self.rate,
                'total_requests': self._total_requests,
                'total_waits': self._total_waits,
                'total_waited_seconds': round(self._total_waited, 3),
                'avg_wait_ms': round(self._total_waited / self._total_waits * 1000, 2) if self._total_waits > 0 else 0
            }
    
    def reset(self) -> None:
        """Reset rate limiter to full capacity."""
        with self._lock:
            self._tokens = float(self.capacity)
            self._last_update = time.time()
            self._total_requests = 0
            self._total_waits = 0
            self._total_waited = 0.0
            logger.info(f"RateLimiter [{self.name}] reset")


class BitkubRateLimiter:
    """
    Bitkub-specific rate limiter with separate limits for different endpoint types.
    
    Bitkub rate limits:
    - Public endpoints: 60 requests/minute
    - Authenticated endpoints: 30 requests/minute
    - Trading endpoints: 15 requests/minute
    """
    
    # Bitkub rate limits (requests per second)
    PUBLIC_RATE = 1.0        # 60/min = 1/sec
    AUTH_RATE = 0.5          # 30/min = 0.5/sec
    TRADING_RATE = 0.25      # 15/min = 0.25/sec
    
    def __init__(self):
        self.public = TokenBucketRateLimiter(
            rate=self.PUBLIC_RATE,
            capacity=60,
            name="public"
        )
        self.authenticated = TokenBucketRateLimiter(
            rate=self.AUTH_RATE,
            capacity=30,
            name="authenticated"
        )
        self.trading = TokenBucketRateLimiter(
            rate=self.TRADING_RATE,
            capacity=15,
            name="trading"
        )
        
        logger.info(
            f"BitkubRateLimiter initialized: "
            f"public={self.PUBLIC_RATE}/s, "
            f"auth={self.AUTH_RATE}/s, "
            f"trading={self.TRADING_RATE}/s"
        )
    
    def acquire_public(self, blocking: bool = True, timeout: Optional[float] = 10.0) -> bool:
        """Acquire rate limit token for public endpoint."""
        return self.public.acquire(blocking=blocking, timeout=timeout)
    
    def acquire_authenticated(self, blocking: bool = True, timeout: Optional[float] = 30.0) -> bool:
        """Acquire rate limit token for authenticated endpoint."""
        return self.authenticated.acquire(blocking=blocking, timeout=timeout)
    
    def acquire_trading(self, blocking: bool = True, timeout: Optional[float] = 60.0) -> bool:
        """Acquire rate limit token for trading endpoint."""
        return self.trading.acquire(blocking=blocking, timeout=timeout)
    
    def get_all_stats(self) -> dict:
        """Get statistics for all rate limiters."""
        return {
            'public': self.public.get_stats(),
            'authenticated': self.authenticated.get_stats(),
            'trading': self.trading.get_stats()
        }


# Global rate limiter instance
_global_rate_limiter: Optional[BitkubRateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> BitkubRateLimiter:
    """Get or create global rate limiter instance."""
    global _global_rate_limiter
    if _global_rate_limiter is None:
        with _limiter_lock:
            if _global_rate_limiter is None:
                _global_rate_limiter = BitkubRateLimiter()
    return _global_rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter."""
    with _limiter_lock:
        if _global_rate_limiter is not None:
            _global_rate_limiter.public.reset()
            _global_rate_limiter.authenticated.reset()
            _global_rate_limiter.trading.reset()
