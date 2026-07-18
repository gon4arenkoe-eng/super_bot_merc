"""SUPERBOT v5.5.36 - XGBoost Signal Filter"""
import logging
import numpy as np
from typing import List, Dict

logger = logging.getLogger(__name__)


class XGBoostFilter:
    """ML filter for trading signals using XGBoost"""

    def __init__(self):
        self.model = None
        self.feature_names = [
            'rsi', 'macd', 'macd_signal', 'bb_upper', 'bb_lower',
            'volume_ratio', 'price_change_24h', 'atr'
        ]
        logger.info("XGBoost Filter initialized")

    def _calculate_features(self, candles: List[Dict]) -> Dict:
        """Calculate technical features"""
        if len(candles) < 20:
            return {}

        closes = [c['close'] for c in candles]
        volumes = [c['volume'] for c in candles]

        # RSI — FIX: correct calculation when avg_loss == 0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-14:])
        avg_loss = np.mean(losses[-14:])

        if avg_loss == 0:
            rsi = 100.0  # All gains, no losses = fully overbought
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

        # MACD
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd = ema12[-1] - ema26[-1] if len(ema12) > 0 and len(ema26) > 0 else 0

        # Bollinger Bands
        sma20 = np.mean(closes[-20:])
        std20 = np.std(closes[-20:])
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20

        # Volume ratio
        volume_ratio = np.mean(volumes[-5:]) / np.mean(volumes[-20:]) if len(volumes) >= 20 else 1

        # Price change 24h (approx)
        price_change = (closes[-1] - closes[-min(24, len(closes))]) / closes[-min(24, len(closes))] * 100

        # ATR
        atr = self._atr(candles[-14:])

        return {
            'rsi': round(rsi, 2),
            'macd': round(macd, 4),
            'macd_signal': round(macd * 0.9, 4),
            'bb_upper': round(bb_upper, 4),
            'bb_lower': round(bb_lower, 4),
            'volume_ratio': round(volume_ratio, 2),
            'price_change_24h': round(price_change, 2),
            'atr': round(atr, 4)
        }

    def _ema(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []
        multiplier = 2 / (period + 1)
        ema = [prices[0]]
        for p in prices[1:]:
            ema.append((p - ema[-1]) * multiplier + ema[-1])
        return ema

    def _atr(self, candles: List[Dict]) -> float:
        if len(candles) < 2:
            return 0
        trs = []
        for i in range(1, len(candles)):
            high = candles[i]['high']
            low = candles[i]['low']
            prev_close = candles[i-1]['close']
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return np.mean(trs) if trs else 0

    def filter_signal(self, candles: List[Dict], signal: str) -> Dict:
        features = self._calculate_features(candles)

        if not features:
            return {'approved': False, 'confidence': 0, 'reason': 'Insufficient data'}

        score = 50

        if signal == 'LONG' and features['rsi'] > 70:
            score -= 20
        elif signal == 'SHORT' and features['rsi'] < 30:
            score -= 20
        elif signal == 'LONG' and features['rsi'] < 50:
            score += 15
        elif signal == 'SHORT' and features['rsi'] > 50:
            score += 15

        if features['volume_ratio'] > 1.5:
            score += 10

        if features['price_change_24h'] > 5 and signal == 'LONG':
            score -= 10
        elif features['price_change_24h'] < -5 and signal == 'SHORT':
            score -= 10

        approved = score > 60

        return {
            'approved': approved,
            'confidence': min(100, max(0, score)),
            'features': features,
            'reason': 'Signal approved' if approved else 'Signal filtered by ML'
        }
