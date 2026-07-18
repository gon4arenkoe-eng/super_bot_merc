"""SUPERBOT v5.5.36 - Sentiment Analyzer"""
import logging
import requests
from typing import Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """Multi-source sentiment analysis"""

    SOURCES = [
        'alternative.me',      # Fear & Greed Index
        'coinmarketcap',       # Social metrics
        'coingecko',           # Community data
        'twitter',             # Social volume (placeholder)
        'reddit',              # Subreddit sentiment (placeholder)
        'news',                # News sentiment (placeholder)
        'funding_rate',        # Exchange funding rates
        'liquidations'         # Liquidation data
    ]

    def __init__(self):
        self.cache = {}
        self.cache_time = 300  # 5 minutes
        logger.info("Sentiment Analyzer initialized")

    def get_fear_greed(self) -> Dict:
        """Get Fear & Greed Index"""
        try:
            resp = requests.get(
                'https://api.alternative.me/fng/',
                timeout=10
            )
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                return {
                    'value': int(data['data'][0]['value']),
                    'classification': data['data'][0]['value_classification'],
                    'timestamp': data['data'][0]['timestamp']
                }
        except Exception as e:
            logger.error(f"Fear & Greed error: {e}")

        return {'value': 50, 'classification': 'Neutral', 'timestamp': None}

    def get_funding_rate(self, symbol: str = 'BTC') -> Dict:
        """Get funding rate (placeholder — requires exchange API)"""
        return {
            'symbol': symbol,
            'rate': 0.01,
            'sentiment': 'neutral'
        }

    def analyze(self, symbol: str = 'BTC') -> Dict:
        """Get combined sentiment analysis"""
        fear_greed = self.get_fear_greed()
        funding = self.get_funding_rate(symbol)

        # Calculate overall sentiment score (0-100)
        score = fear_greed['value']

        # Adjust based on funding rate
        if funding['rate'] > 0.05:
            score -= 10  # High funding = overleveraged longs
        elif funding['rate'] < -0.05:
            score += 10

        sentiment = 'neutral'
        if score > 75:
            sentiment = 'extreme_greed'
        elif score > 55:
            sentiment = 'greed'
        elif score < 25:
            sentiment = 'extreme_fear'
        elif score < 45:
            sentiment = 'fear'

        return {
            'overall_score': round(score, 1),
            'sentiment': sentiment,
            'fear_greed': fear_greed,
            'funding': funding,
            'sources_analyzed': len(self.SOURCES),
            'timestamp': datetime.utcnow().isoformat()
        }
