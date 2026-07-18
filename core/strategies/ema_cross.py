"""SUPERBOT v5.5.36 - EMA Cross Strategy"""
import numpy as np
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class EMAStrategy:
    """Multi-Timeframe EMA Cross Strategy"""

    def __init__(self, fast_ema: int = 9, slow_ema: int = 21, trend_ema: int = 50):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.trend_ema = trend_ema

    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """Calculate EMA"""
        if len(prices) < period:
            return []

        multiplier = 2 / (period + 1)
        ema = [prices[0]]

        for price in prices[1:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])

        return ema

    def analyze(self, candles: List[Dict]) -> Dict:
        """Analyze market and return signal"""
        if len(candles) < self.trend_ema + 5:
            return {'signal': 'NEUTRAL', 'confidence': 0}

        closes = [c['close'] for c in candles]

        ema_fast = self.calculate_ema(closes, self.fast_ema)
        ema_slow = self.calculate_ema(closes, self.slow_ema)
        ema_trend = self.calculate_ema(closes, self.trend_ema)

        if len(ema_fast) < 2 or len(ema_slow) < 2:
            return {'signal': 'NEUTRAL', 'confidence': 0}

        # Current values
        fast_now = ema_fast[-1]
        slow_now = ema_slow[-1]
        trend_now = ema_trend[-1]

        fast_prev = ema_fast[-2]
        slow_prev = ema_slow[-2]

        # Cross detection
        cross_up = fast_prev < slow_prev and fast_now > slow_now
        cross_down = fast_prev > slow_prev and fast_now < slow_now

        # Trend filter
        above_trend = closes[-1] > trend_now
        below_trend = closes[-1] < trend_now

        signal = 'NEUTRAL'
        confidence = 0

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

    def get_params(self) -> Dict:
        """Get strategy parameters"""
        return {
            'name': 'EMA Cross',
            'fast_ema': self.fast_ema,
            'slow_ema': self.slow_ema,
            'trend_ema': self.trend_ema
        }
