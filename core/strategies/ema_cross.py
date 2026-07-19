"""SUPERBOT v5.5.38 - EMA Cross Strategy (pure functions)"""
from typing import Dict, List


def calculate_ema(prices: List[float], period: int) -> List[float]:
    """Calculate EMA for a list of prices. Pure function."""
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def analyze_ema(candles: List[Dict], fast_period: int = 9,
                slow_period: int = 21, trend_period: int = 50) -> Dict:
    """
    Analyze candles for EMA crossover signal. Pure function.
    
    Returns: {
        'signal': 'LONG' | 'SHORT' | 'NEUTRAL',
        'confidence': float (0-100),
        'fast_ema': float,
        'slow_ema': float,
        'trend_ema': float,
        'price': float (last close)
    }
    """
    if len(candles) < trend_period + 5:
        return {'signal': 'NEUTRAL', 'confidence': 0, 'price': 0}
    
    closes = [c['close'] for c in candles]
    ema_fast = calculate_ema(closes, fast_period)
    ema_slow = calculate_ema(closes, slow_period)
    ema_trend = calculate_ema(closes, trend_period)
    
    if len(ema_fast) < 2 or len(ema_slow) < 2:
        return {'signal': 'NEUTRAL', 'confidence': 0, 'price': closes[-1]}
    
    fast_now, slow_now, trend_now = ema_fast[-1], ema_slow[-1], ema_trend[-1]
    fast_prev, slow_prev = ema_fast[-2], ema_slow[-2]
    
    cross_up = fast_prev < slow_prev and fast_now > slow_now
    cross_down = fast_prev > slow_prev and fast_now < slow_now
    above_trend = closes[-1] > trend_now
    below_trend = closes[-1] < trend_now
    
    signal, confidence = 'NEUTRAL', 0
    
    if cross_up and above_trend:
        signal = 'LONG'
        confidence = min(100, abs(fast_now - slow_now) / slow_now * 10000)
    elif cross_down and below_trend:
        signal = 'SHORT'
        confidence = min(100, abs(fast_now - slow_now) / slow_now * 10000)
    
    return {
        'signal': signal,
        'confidence': round(confidence, 1),
        'fast_ema': round(fast_now, 4),
        'slow_ema': round(slow_now, 4),
        'trend_ema': round(trend_now, 4),
        'price': closes[-1]
    }


class EMAStrategy:
    """EMA Cross Strategy — wrapper around pure functions."""
    
    def __init__(self, fast_ema: int = 9, slow_ema: int = 21, trend_ema: int = 50):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.trend_ema = trend_ema
    
    def analyze(self, candles: List[Dict]) -> Dict:
        """Analyze candles using EMA crossover."""
        return analyze_ema(
            candles,
            fast_period=self.fast_ema,
            slow_period=self.slow_ema,
            trend_period=self.trend_ema
        )
