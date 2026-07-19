"""SUPERBOT v5.5.38 - Sentiment Analyzer (pure functions)"""
import requests
import logging
from datetime import datetime
from typing import Dict

logger = logging.getLogger(__name__)


def fetch_fear_greed_index() -> Dict:
    """
    Fetch Fear & Greed Index from alternative.me. Pure function.
    
    Returns: {
        'value': int (0-100),
        'classification': str,
        'timestamp': str
    }
    """
    try:
        resp = requests.get('https://api.alternative.me/fng/', timeout=10)
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


def analyze_sentiment(fear_greed: Dict) -> Dict:
    """
    Analyze market sentiment from Fear & Greed data. Pure function.
    
    Returns: {
        'overall_score': float,
        'sentiment': str,
        'fear_greed': Dict,
        'timestamp': str
    }
    """
    score = fear_greed['value']
    
    if score > 75:
        sentiment = 'extreme_greed'
    elif score > 55:
        sentiment = 'greed'
    elif score < 25:
        sentiment = 'extreme_fear'
    elif score < 45:
        sentiment = 'fear'
    else:
        sentiment = 'neutral'
    
    return {
        'overall_score': round(score, 1),
        'sentiment': sentiment,
        'fear_greed': fear_greed,
        'timestamp': datetime.utcnow().isoformat()
    }


class SentimentAnalyzer:
    """Sentiment Analyzer — wrapper around pure functions."""
    
    def __init__(self):
        self.cache = {}
        self.cache_time = 300
    
    def get_fear_greed(self) -> Dict:
        """Fetch Fear & Greed index."""
        return fetch_fear_greed_index()
    
    def analyze(self, symbol: str = 'BTC') -> Dict:
        """Analyze sentiment for a symbol."""
        fear_greed = self.get_fear_greed()
        return analyze_sentiment(fear_greed)
