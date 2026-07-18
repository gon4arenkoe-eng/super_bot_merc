"""SUPERBOT v5.5.36 - BingX API Client"""
import os
import json
import time
import hmac
import hashlib
import base64
import requests
from urllib.parse import urlencode


class BingXClient:
    """BingX API Client — Spot & Futures"""

    BASE_URL = "https://open-api.bingx.com"
    BASE_URL_VST = "https://open-api-vst.bingx.com"

    def __init__(self, api_key: str, api_secret: str, demo: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo = demo
        self.base_url = self.BASE_URL_VST if demo else self.BASE_URL
        self.session = requests.Session()

    def _generate_signature(self, params: dict) -> str:
        """Generate HMAC SHA256 signature"""
        query_string = urlencode(sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = False):
        """Make API request"""
        url = f"{self.base_url}{endpoint}"
        headers = {}

        if params is None:
            params = {}

        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
            headers['X-BX-APIKEY'] = self.api_key

        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers, timeout=30)
            elif method == 'POST':
                headers['Content-Type'] = 'application/json'
                response = self.session.post(url, json=params, headers=headers, timeout=30)
            elif method == 'DELETE':
                response = self.session.delete(url, params=params, headers=headers, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {'error': str(e)}

    # Account
    def get_balance(self):
        return self._request('GET', '/openApi/swap/v2/user/balance', signed=True)

    def get_positions(self, symbol: str = None):
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._request('GET', '/openApi/swap/v2/user/positions', params, signed=True)

    def get_income(self, start_time: int = None, end_time: int = None, limit: int = 100):
        params = {'limit': limit}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        return self._request('GET', '/openApi/swap/v2/user/income', params, signed=True)

    # Market Data
    def get_ticker(self, symbol: str):
        return self._request('GET', '/openApi/swap/v2/quote/ticker', {'symbol': symbol})

    def get_klines(self, symbol: str, interval: str = '1h', limit: int = 100):
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        return self._request('GET', '/openApi/swap/v3/quote/klines', params)

    def get_depth(self, symbol: str, limit: int = 20):
        return self._request('GET', '/openApi/swap/v2/quote/depth', {
            'symbol': symbol,
            'limit': limit
        })

    # Trading
    def place_order(self, symbol: str, side: str, position_side: str,
                    order_type: str, quantity: float, price: float = None,
                    stop_loss: float = None, take_profit: float = None,
                    leverage: int = 5):
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': order_type,
            'quantity': quantity,
            'leverage': leverage
        }
        if price and order_type != 'MARKET':
            params['price'] = price
        if stop_loss:
            params['stopLoss'] = json.dumps({'stopPrice': stop_loss, 'type': 'STOP_MARKET'})
        if take_profit:
            params['takeProfit'] = json.dumps({'stopPrice': take_profit, 'type': 'TAKE_PROFIT_MARKET'})

        return self._request('POST', '/openApi/swap/v2/trade/order', params, signed=True)

    def close_position(self, symbol: str, position_side: str):
        params = {
            'symbol': symbol,
            'positionSide': position_side,
            'type': 'MARKET'
        }
        return self._request('POST', '/openApi/swap/v2/trade/closePosition', params, signed=True)

    def cancel_order(self, symbol: str, order_id: str):
        params = {
            'symbol': symbol,
            'orderId': order_id
        }
        return self._request('DELETE', '/openApi/swap/v2/trade/order', params, signed=True)

    def set_leverage(self, symbol: str, leverage: int, position_side: str = 'BOTH'):
        params = {
            'symbol': symbol,
            'leverage': leverage,
            'positionSide': position_side
        }
        return self._request('POST', '/openApi/swap/v2/trade/leverage', params, signed=True)
