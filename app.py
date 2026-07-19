#!/usr/bin/env python3
"""
SUPERBOT v5.5.39 Mercedes Full - FIXED
Pure functions in core/parsers.py, idempotent orders, auto-reset daily PnL
Fixed: klines error handling, type safety, remove dead code, duplicate exchange protection
"""

import os
import sys
import json
import time
import hmac
import hashlib
import base64
import logging
import threading
import uuid
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from flask import Flask, request, jsonify, render_template, redirect, url_for
from functools import wraps
import jwt
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ── Import pure functions from core/parsers ─────────────────────────────
from core.parsers import parse_balance, parse_all_positions, parse_klines_bingx
from core.strategies.ema_cross import EMAStrategy
from core.sentiment.analyzer import SentimentAnalyzer

# ── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'bot.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('SUPERBOT')

# ── Flask App ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'superbot-secret-key-change-me')
app.config['VERSION'] = '5.5.40'
app.config['EDITION'] = 'Mercedes'
app.config['SUPPORTED_EXCHANGES'] = ['bingx', 'binance', 'bybit', 'okx']

# Database
database_url = os.environ.get('DATABASE_URL', '')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'sslmode': 'require'}}
else:
    db_path = os.path.join(os.path.dirname(__file__), 'superbot.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# CORS
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ── Context Processor ──────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    return {
        'version': app.config['VERSION'],
        'edition': app.config['EDITION'],
        'now': datetime.now(timezone.utc)
    }

# ── Encryption ─────────────────────────────────────────────────────────
MASTER_KEY = os.environ.get('MASTER_KEY')

def get_fernet():
    if not MASTER_KEY:
        return None
    salt = hashes.Hash(hashes.SHA256())
    salt.update(MASTER_KEY.encode())
    salt_bytes = salt.finalize()[:16]
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt_bytes, iterations=480000)
    key = base64.urlsafe_b64encode(kdf.derive(MASTER_KEY.encode()))
    return Fernet(key)

def encrypt_value(value):
    if not value:
        return ""
    f = get_fernet()
    if not f:
        return value
    return f.encrypt(value.encode()).decode()

def decrypt_value(encrypted_value):
    if not encrypted_value:
        return ""
    f = get_fernet()
    if not f:
        return encrypted_value
    try:
        return f.decrypt(encrypted_value.encode()).decode()
    except:
        return encrypted_value

# ── Models ───────────────────────────────────────────────────────────────
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Exchange(db.Model):
    __tablename__ = 'exchanges'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    api_key_encrypted = db.Column(db.Text, nullable=False)
    api_secret_encrypted = db.Column(db.Text, nullable=False)
    passphrase_encrypted = db.Column(db.Text, nullable=True)
    is_demo = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self, include_secrets=False):
        data = {
            'id': self.id, 'name': self.name, 'display_name': self.display_name,
            'is_demo': self.is_demo, 'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_secrets:
            data['api_key'] = decrypt_value(self.api_key_encrypted)
            data['api_secret'] = decrypt_value(self.api_secret_encrypted)
            data['passphrase'] = decrypt_value(self.passphrase_encrypted) if self.passphrase_encrypted else ''
        else:
            ak = decrypt_value(self.api_key_encrypted) if self.api_key_encrypted else ''
            data['api_key_masked'] = ak[:4] + '***' + ak[-4:] if len(ak) > 8 else '*' * len(ak)
        return data

class Position(db.Model):
    __tablename__ = 'positions'
    id = db.Column(db.Integer, primary_key=True)
    exchange_id = db.Column(db.Integer, db.ForeignKey('exchanges.id'), nullable=False)
    symbol = db.Column(db.String(50), nullable=False)
    side = db.Column(db.String(10), nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    size = db.Column(db.Float, nullable=False)
    leverage = db.Column(db.Integer, default=5)
    pnl = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='OPEN')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id, 'exchange_id': self.exchange_id, 'symbol': self.symbol,
            'side': self.side, 'entry_price': self.entry_price, 'size': self.size,
            'leverage': self.leverage, 'pnl': self.pnl, 'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None
        }

class BotSettings(db.Model):
    __tablename__ = 'bot_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class SentOrder(db.Model):
    """Idempotent order tracking — prevents duplicate orders"""
    __tablename__ = 'sent_orders'
    id = db.Column(db.Integer, primary_key=True)
    client_order_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    exchange_id = db.Column(db.Integer, db.ForeignKey('exchanges.id'), nullable=False)
    symbol = db.Column(db.String(50), nullable=False)
    side = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=True)
    order_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='SENT')
    exchange_response = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'client_order_id': self.client_order_id,
            'exchange_id': self.exchange_id, 'symbol': self.symbol,
            'side': self.side, 'quantity': self.quantity,
            'price': self.price, 'order_type': self.order_type,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

# ═══════════════════════════════════════════════════════════════════════
# JWT AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════

JWT_SECRET = os.environ.get('JWT_SECRET_KEY') or os.environ.get('SECRET_KEY') or 'superbot-jwt-secret-change-me-min-32-chars'
JWT_ALGORITHM = 'HS256'
JWT_ACCESS_EXPIRES = timedelta(hours=24)
JWT_REFRESH_EXPIRES = timedelta(days=7)

def create_tokens(user_id, username):
    now = datetime.now(timezone.utc)
    access_payload = {
        'user_id': user_id, 'username': username, 'type': 'access',
        'iat': now, 'exp': now + JWT_ACCESS_EXPIRES
    }
    refresh_payload = {
        'user_id': user_id, 'username': username, 'type': 'refresh',
        'iat': now, 'exp': now + JWT_REFRESH_EXPIRES
    }
    return {
        'access_token': jwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM),
        'refresh_token': jwt.encode(refresh_payload, JWT_SECRET, algorithm=JWT_ALGORITHM),
        'token_type': 'Bearer',
        'expires_in': int(JWT_ACCESS_EXPIRES.total_seconds())
    }

def decode_token(token, token_type='access'):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get('type') != token_type:
            return None, 'Invalid token type'
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, 'Token expired'
    except jwt.InvalidTokenError:
        return None, 'Invalid token'

def get_auth_token():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return None

def jwt_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_auth_token()
        if not token:
            return jsonify({'success': False, 'error': 'Missing authorization token'}), 401
        payload, error = decode_token(token, 'access')
        if error:
            return jsonify({'success': False, 'error': error}), 401
        g.current_user = payload
        g.user_id = payload['user_id']
        g.username = payload['username']
        return f(*args, **kwargs)
    return decorated

def jwt_optional(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_auth_token()
        g.current_user = None
        g.user_id = None
        g.username = None
        if token:
            payload, error = decode_token(token, 'access')
            if not error:
                g.current_user = payload
                g.user_id = payload['user_id']
                g.username = payload['username']
        return f(*args, **kwargs)
    return decorated

# ═══════════════════════════════════════════════════════════════════════
# EXCHANGE API CLIENTS — ALL 4 EXCHANGES
# ═══════════════════════════════════════════════════════════════════════

class BingXClient:
    BASE_URL = "https://open-api.bingx.com"
    BASE_URL_VST = "https://open-api-vst.bingx.com"

    def __init__(self, api_key, api_secret, demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo = demo
        self.base_url = self.BASE_URL_VST if demo else self.BASE_URL
        self.session = requests.Session()

    def _generate_signature(self, params):
        query_string = urlencode(sorted(params.items()))
        return hmac.new(self.api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

    def _request(self, method, endpoint, params=None, signed=False):
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if params is None: params = {}
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
            else:
                return {'error': f'Unsupported method: {method}'}
            response.raise_for_status()
            result = response.json()
            # FIX: Log raw response for debugging
            if isinstance(result, dict) and 'code' in result and result.get('code') != 0:
                logger.warning(f"BingX API warning {endpoint}: code={result.get('code')}, msg={result.get('msg')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"BingX API request error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}
        except Exception as e:
            logger.error(f"BingX API unexpected error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}

    def get_balance(self):
        return self._request('GET', '/openApi/swap/v2/user/balance', signed=True)

    def get_positions(self, symbol=None):
        params = {'symbol': symbol} if symbol else {}
        return self._request('GET', '/openApi/swap/v2/user/positions', params, signed=True)

    def get_income(self, start_time=None, end_time=None, limit=100):
        params = {'limit': limit}
        if start_time: params['startTime'] = start_time
        if end_time: params['endTime'] = end_time
        return self._request('GET', '/openApi/swap/v2/user/income', params, signed=True)

    def get_klines(self, symbol, interval='1h', limit=100):
        return self._request('GET', '/openApi/swap/v3/quote/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})

    def place_order(self, symbol, side, position_side, order_type, quantity, price=None,
                    stop_loss=None, take_profit=None, leverage=5):
        params = {
            'symbol': symbol, 'side': side, 'positionSide': position_side,
            'type': order_type, 'quantity': quantity, 'leverage': leverage
        }
        if price and order_type != 'MARKET':
            params['price'] = price
        if stop_loss:
            params['stopLoss'] = json.dumps({'stopPrice': stop_loss, 'type': 'STOP_MARKET'})
        if take_profit:
            params['takeProfit'] = json.dumps({'stopPrice': take_profit, 'type': 'TAKE_PROFIT_MARKET'})
        return self._request('POST', '/openApi/swap/v2/trade/order', params, signed=True)

    def close_position(self, symbol, position_side):
        params = {'symbol': symbol, 'positionSide': position_side, 'type': 'MARKET'}
        return self._request('POST', '/openApi/swap/v2/trade/closePosition', params, signed=True)

    def set_leverage(self, symbol, leverage, position_side='BOTH'):
        params = {'symbol': symbol, 'leverage': leverage, 'positionSide': position_side}
        return self._request('POST', '/openApi/swap/v2/trade/leverage', params, signed=True)


class BinanceClient:
    BASE_URL = "https://fapi.binance.com"
    BASE_URL_TEST = "https://testnet.binancefuture.com"

    def __init__(self, api_key, api_secret, demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo = demo
        self.base_url = self.BASE_URL_TEST if demo else self.BASE_URL
        self.session = requests.Session()

    def _generate_signature(self, query_string):
        return hmac.new(self.api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

    def _request(self, method, endpoint, params=None, signed=False):
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if params is None: params = {}
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['recvWindow'] = 5000
            query = urlencode(params)
            params['signature'] = self._generate_signature(query)
            headers['X-MBX-APIKEY'] = self.api_key
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers, timeout=30)
            elif method == 'POST':
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                response = self.session.post(url, data=params, headers=headers, timeout=30)
            else:
                return {'error': f'Unsupported method: {method}'}
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Binance API error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}
        except Exception as e:
            logger.error(f"Binance API unexpected error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}

    def get_balance(self):
        return self._request('GET', '/fapi/v2/balance', signed=True)

    def get_positions(self):
        return self._request('GET', '/fapi/v2/positionRisk', signed=True)

    def place_order(self, symbol, side, order_type, quantity, price=None, stop_loss=None, take_profit=None):
        params = {'symbol': symbol, 'side': side, 'type': order_type, 'quantity': quantity}
        if price and order_type != 'MARKET':
            params['price'] = price
        if stop_loss:
            params['stopPrice'] = stop_loss
            params['type'] = 'STOP_MARKET'
        if take_profit:
            params['stopPrice'] = take_profit
            params['type'] = 'TAKE_PROFIT_MARKET'
        return self._request('POST', '/fapi/v1/order', params, signed=True)

    def close_position(self, symbol, side):
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        return self.place_order(symbol, close_side, 'MARKET', 0)


class BybitClient:
    BASE_URL = "https://api.bybit.com"
    BASE_URL_TEST = "https://api-testnet.bybit.com"

    def __init__(self, api_key, api_secret, demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo = demo
        self.base_url = self.BASE_URL_TEST if demo else self.BASE_URL
        self.session = requests.Session()

    def _generate_signature(self, params, timestamp):
        recv_window = '5000'
        param_str = timestamp + self.api_key + recv_window
        if params:
            param_str += json.dumps(params, separators=(',', ':'), sort_keys=True)
        return hmac.new(self.api_secret.encode('utf-8'), param_str.encode('utf-8'), hashlib.sha256).hexdigest()

    def _request(self, method, endpoint, params=None, signed=False):
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if params is None: params = {}
        if signed:
            timestamp = str(int(time.time() * 1000))
            signature = self._generate_signature(params, timestamp)
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-SIGN': signature,
                'X-BAPI-RECV-WINDOW': '5000'
            }
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers, timeout=30)
            elif method == 'POST':
                headers['Content-Type'] = 'application/json'
                response = self.session.post(url, json=params, headers=headers, timeout=30)
            else:
                return {'error': f'Unsupported method: {method}'}
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Bybit API error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}
        except Exception as e:
            logger.error(f"Bybit API unexpected error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}

    def get_balance(self):
        return self._request('GET', '/v5/account/wallet-balance', {'accountType': 'UNIFIED'}, signed=True)

    def get_positions(self):
        return self._request('GET', '/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT'}, signed=True)

    def place_order(self, symbol, side, order_type, quantity, price=None):
        params = {'category': 'linear', 'symbol': symbol, 'side': side, 'orderType': order_type, 'qty': str(quantity)}
        if price and order_type != 'Market':
            params['price'] = str(price)
        return self._request('POST', '/v5/order/create', params, signed=True)

    def close_position(self, symbol, side):
        close_side = 'Sell' if side == 'LONG' else 'Buy'
        return self.place_order(symbol, close_side, 'Market', 0)


class OKXClient:
    BASE_URL = "https://www.okx.com"

    def __init__(self, api_key, api_secret, passphrase, demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo = demo
        self.base_url = self.BASE_URL
        self.session = requests.Session()

    def _generate_signature(self, timestamp, method, endpoint, body=''):
        message = timestamp + method.upper() + endpoint + body
        mac = hmac.new(self.api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _request(self, method, endpoint, params=None, signed=False):
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if params is None: params = {}
        if signed:
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            body = json.dumps(params) if params else ''
            signature = self._generate_signature(timestamp, method, endpoint, body)
            headers = {
                'OK-ACCESS-KEY': self.api_key,
                'OK-ACCESS-SIGN': signature,
                'OK-ACCESS-TIMESTAMP': timestamp,
                'OK-ACCESS-PASSPHRASE': self.passphrase or '',
                'Content-Type': 'application/json'
            }
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers, timeout=30)
            elif method == 'POST':
                response = self.session.post(url, json=params, headers=headers, timeout=30)
            else:
                return {'error': f'Unsupported method: {method}'}
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"OKX API error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}
        except Exception as e:
            logger.error(f"OKX API unexpected error {endpoint}: {type(e).__name__}: {e}")
            return {'error': f'{type(e).__name__}: {e}'}

    def get_balance(self):
        return self._request('GET', '/api/v5/account/balance', signed=True)

    def get_positions(self):
        return self._request('GET', '/api/v5/account/positions', {'instType': 'SWAP'}, signed=True)

    def place_order(self, symbol, side, order_type, quantity, price=None):
        params = {'instId': symbol, 'tdMode': 'cross', 'side': side.lower(), 'ordType': order_type.lower(), 'sz': str(quantity)}
        if price and order_type.lower() != 'market':
            params['px'] = str(price)
        return self._request('POST', '/api/v5/trade/order', params, signed=True)

    def close_position(self, symbol, side):
        close_side = 'sell' if side == 'LONG' else 'buy'
        return self.place_order(symbol, close_side, 'market', 0)

# ═══════════════════════════════════════════════════════════════════════
# EXCHANGE MANAGER
# ═══════════════════════════════════════════════════════════════════════

class ExchangeManager:
    @staticmethod
    def add_exchange(name, display_name, api_key, api_secret, passphrase=None, is_demo=True):
        # Normalize inputs
        name = name.lower().strip() if name else ''
        api_key = api_key.strip() if api_key else ''
        api_secret = api_secret.strip() if api_secret else ''

        # Validate
        if not name:
            raise ValueError("Exchange name is required")
        if not api_key:
            raise ValueError("API key is required")
        if not api_secret:
            raise ValueError("API secret is required")
        if name not in app.config['SUPPORTED_EXCHANGES']:
            raise ValueError(f"Unsupported exchange: {name}. Supported: {', '.join(app.config['SUPPORTED_EXCHANGES'])}")

        # FIX: Prevent duplicate exchanges with same API key
        encrypted_key = encrypt_value(api_key)
        existing = Exchange.query.filter_by(
            name=name,
            api_key_encrypted=encrypted_key
        ).first()
        if existing:
            raise ValueError(f"Exchange {name.upper()} with this API key already exists (ID: {existing.id}, added: {existing.created_at})")

        ex = Exchange(
            name=name,
            display_name=display_name or name.upper(),
            api_key_encrypted=encrypted_key,
            api_secret_encrypted=encrypt_value(api_secret),
            passphrase_encrypted=encrypt_value(passphrase) if passphrase else None,
            is_demo=is_demo,
            is_active=True
        )
        db.session.add(ex)
        db.session.commit()
        logger.info(f"Added exchange: {name.upper()} (ID: {ex.id}, demo: {is_demo})")
        return ex

    @staticmethod
    def get_all_exchanges():
        return [ex.to_dict() for ex in Exchange.query.all()]

    @staticmethod
    def delete_exchange(exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if not ex:
            return False

        # FIX: Delete related positions first (cascade delete)
        deleted_positions = Position.query.filter_by(exchange_id=exchange_id).delete()

        db.session.delete(ex)
        db.session.commit()
        logger.info(f"Deleted exchange {exchange_id} (+{deleted_positions} positions)")
        return True

    @staticmethod
    def toggle_active(exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if ex:
            ex.is_active = not ex.is_active
            db.session.commit()
            return ex.to_dict()
        return None

    @staticmethod
    def get_decrypted_credentials(exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if not ex:
            return None
        return {
            'name': ex.name,
            'api_key': decrypt_value(ex.api_key_encrypted),
            'api_secret': decrypt_value(ex.api_secret_encrypted),
            'passphrase': decrypt_value(ex.passphrase_encrypted) if ex.passphrase_encrypted else None,
            'is_demo': ex.is_demo
        }

    @staticmethod
    def get_client(exchange_id):
        creds = ExchangeManager.get_decrypted_credentials(exchange_id)
        if not creds:
            return None
        name = creds['name']
        if name == 'bingx':
            return BingXClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
        elif name == 'binance':
            return BinanceClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
        elif name == 'bybit':
            return BybitClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
        elif name == 'okx':
            return OKXClient(creds['api_key'], creds['api_secret'], creds['passphrase'], creds['is_demo'])
        return None


# ═══════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.positions_count = 0
        self.config = {
            'max_leverage': 5, 'max_positions': 5, 'max_daily_loss_pct': 5.0,
            'max_position_size_pct': 10.0, 'dca_orders': 5, 'dca_step_pct': 2.0,
            'martingale_pct': 30.0, 'breakeven_pct': 1.0, 'sl_pct': 3.0,
            'tp1_pct': 2.0, 'tp2_pct': 4.0, 'tp3_pct': 6.0
        }

    def can_open_position(self, balance, position_size, current_positions):
        if current_positions >= self.config['max_positions']:
            return False, f"Max positions reached ({self.config['max_positions']})"
        max_size = balance * (self.config['max_position_size_pct'] / 100)
        if position_size > max_size:
            return False, f"Position size {position_size:.2f} exceeds max {max_size:.2f}"
        if self.daily_pnl <= -balance * (self.config['max_daily_loss_pct'] / 100):
            return False, "Daily loss limit reached"
        return True, "OK"

    def calculate_sl_tp(self, entry_price, side):
        sl_pct = self.config['sl_pct'] / 100
        if side == 'LONG':
            sl = entry_price * (1 - sl_pct)
            tp1 = entry_price * (1 + self.config['tp1_pct'] / 100)
            tp2 = entry_price * (1 + self.config['tp2_pct'] / 100)
            tp3 = entry_price * (1 + self.config['tp3_pct'] / 100)
        else:
            sl = entry_price * (1 + sl_pct)
            tp1 = entry_price * (1 - self.config['tp1_pct'] / 100)
            tp2 = entry_price * (1 - self.config['tp2_pct'] / 100)
            tp3 = entry_price * (1 - self.config['tp3_pct'] / 100)
        return {
            'stop_loss': round(sl, 4), 'take_profit_1': round(tp1, 4),
            'take_profit_2': round(tp2, 4), 'take_profit_3': round(tp3, 4)
        }

    def update_daily_pnl(self, pnl):
        self.daily_pnl += pnl

    def reset_daily_pnl(self):
        self.daily_pnl = 0.0


# ═══════════════════════════════════════════════════════════════════════
# TRADING ENGINE — REFACTORED (pure functions from core/parsers)
# ═══════════════════════════════════════════════════════════════════════

class TradingEngine:
    def __init__(self, app_instance=None):
        self.app = app_instance
        self.running = False
        self.thread = None
        self.risk_manager = RiskManager()
        self.ema_strategy = EMAStrategy()
        self.clients = {}
        self._last_pnl_reset = datetime.now(timezone.utc).date()

    def _get_client(self, exchange_id):
        if exchange_id in self.clients:
            return self.clients[exchange_id]
        client = ExchangeManager.get_client(exchange_id)
        if client:
            self.clients[exchange_id] = client
        return client

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Trading Engine started")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Trading Engine stopped")

    def _run_loop(self):
        while self.running:
            try:
                with self.app.app_context():
                    self._check_daily_reset()
                    self._process_exchanges()
            except Exception as e:
                logger.error(f"Engine loop error: {e}")
            time.sleep(60)

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self._last_pnl_reset:
            self.risk_manager.reset_daily_pnl()
            self._last_pnl_reset = today
            logger.info("Daily PnL reset")

    def _process_exchanges(self):
        exchanges = ExchangeManager.get_all_exchanges()
        for ex in exchanges:
            if not ex.get('is_active'):
                continue
            exchange_id = ex['id']
            client = self._get_client(exchange_id)
            if not client:
                continue
            try:
                # Use pure function parse_balance
                balance_data = client.get_balance()
                balance = parse_balance(balance_data, ex['name'])

                # Use pure function parse_all_positions
                positions_data = client.get_positions()
                self._sync_positions(exchange_id, positions_data, ex['name'])

                self._analyze_and_trade(exchange_id, client, balance, ex['name'])
            except Exception as e:
                logger.error(f"Error processing exchange {exchange_id}: {e}")

    def _sync_positions(self, exchange_id, data, exchange_name):
        """Sync positions using pure parsers from core/parsers"""
        try:
            # Parse all positions with pure function
            api_positions = parse_all_positions(data, exchange_name)

            logger.info(f"Sync positions for exchange {exchange_id}: {len(api_positions)} positions found")
            if api_positions:
                logger.info(f"First position sample: {json.dumps(api_positions[0])[:300]}")

            db_positions = {
                p.symbol: p for p in Position.query.filter_by(
                    exchange_id=exchange_id, status='OPEN'
                ).all()
            }

            seen_symbols = set()

            for pos in api_positions:
                symbol = pos['symbol']
                seen_symbols.add(symbol)

                if symbol in db_positions:
                    # Update existing
                    existing = db_positions[symbol]
                    existing.size = pos['size']
                    existing.pnl = pos['pnl']
                    existing.entry_price = pos['entry_price']
                    existing.leverage = pos['leverage']
                else:
                    # Create new
                    new_pos = Position(
                        exchange_id=exchange_id,
                        symbol=symbol,
                        side=pos['side'],
                        entry_price=pos['entry_price'],
                        size=pos['size'],
                        leverage=pos['leverage'],
                        pnl=pos['pnl'],
                        status='OPEN'
                    )
                    db.session.add(new_pos)

            # Mark closed positions
            for symbol, pos in db_positions.items():
                if symbol not in seen_symbols:
                    pos.status = 'CLOSED'
                    pos.closed_at = datetime.now(timezone.utc)
                    self.risk_manager.update_daily_pnl(pos.pnl)

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Position sync error: {e}")

    def _analyze_and_trade(self, exchange_id, client, balance, exchange_name):
        symbols = ['BTC-USDT', 'ETH-USDT']
        for symbol in symbols:
            try:
                # FIX: Get klines with error handling and logging
                klines_data = client.get_klines(symbol, interval='1h', limit=100)

                # FIX: Check if klines_data is valid dict
                if not isinstance(klines_data, dict):
                    logger.warning(f"Klines invalid type for {symbol}: {type(klines_data)} = {klines_data}")
                    continue

                if 'error' in klines_data:
                    logger.warning(f"Klines API error for {symbol}: {klines_data['error']}")
                    continue

                candles = parse_klines_bingx(klines_data)

                if not candles:
                    logger.info(f"No candles for {symbol}, skipping")
                    continue

                signal = self.ema_strategy.analyze(candles)

                if signal['signal'] != 'NEUTRAL' and signal['confidence'] > 60:
                    self._execute_signal(exchange_id, client, symbol, signal, balance, exchange_name)
            except Exception as e:
                logger.error(f"Analysis error for {symbol}: {type(e).__name__}: {e}")

    def _execute_signal(self, exchange_id, client, symbol, signal, balance, exchange_name):
        side = signal['signal']
        current_price = signal['price']

        # Risk check
        current_positions = Position.query.filter_by(
            exchange_id=exchange_id, status='OPEN'
        ).count()

        position_size = balance * 0.02
        can_trade, reason = self.risk_manager.can_open_position(
            balance, position_size, current_positions
        )

        if not can_trade:
            logger.info(f"Risk block: {reason}")
            return

        # Calculate SL/TP
        sl_tp = self.risk_manager.calculate_sl_tp(current_price, side)
        leverage = self.risk_manager.config['max_leverage']

        # ── IDEMPOTENCY CHECK ──────────────────────────────────────────
        client_order_id = self._generate_client_order_id(
            exchange_id, symbol, side, current_price
        )

        existing = SentOrder.query.filter_by(
            client_order_id=client_order_id
        ).first()

        if existing:
            logger.warning(
                f"IDEMPOTENCY BLOCK: Order {client_order_id} already sent. "
                f"Skipping duplicate for {symbol} {side}"
            )
            return

        logger.info(f"Placing {side} order for {symbol} at {current_price}")

        # DEMO mode
        if hasattr(client, 'demo') and client.demo:
            logger.info(f"[DEMO] Would place order: {symbol} {side} @ {current_price}")
            self._record_sent_order(
                client_order_id, exchange_id, symbol, side,
                round(position_size / current_price, 4),
                current_price, 'MARKET', {'demo': True}
            )
            return

        # Execute order
        try:
            if exchange_name == 'bingx':
                client.set_leverage(symbol, leverage)
                result = client.place_order(
                    symbol=symbol,
                    side='BUY' if side == 'LONG' else 'SELL',
                    position_side=side,
                    order_type='MARKET',
                    quantity=round(position_size / current_price, 4),
                    stop_loss=sl_tp['stop_loss'],
                    take_profit=sl_tp['take_profit_1'],
                    leverage=leverage
                )

                # Record sent order
                self._record_sent_order(
                    client_order_id, exchange_id, symbol, side,
                    round(position_size / current_price, 4),
                    current_price, 'MARKET', result
                )

                logger.info(f"Order result: {result}")
        except Exception as e:
            logger.error(f"Order error: {e}")

    def _generate_client_order_id(self, exchange_id, symbol, side, price):
        """Generate deterministic order ID for idempotency"""
        timestamp_minute = int(datetime.now(timezone.utc).timestamp() / 60)
        raw = f"{exchange_id}_{symbol}_{side}_{price:.2f}_{timestamp_minute}"
        hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"SB_{exchange_id}_{symbol}_{side}_{hash_suffix}"

    def _record_sent_order(self, client_order_id, exchange_id, symbol,
                          side, quantity, price, order_type, response):
        """Record sent order to prevent duplicates"""
        try:
            order = SentOrder(
                client_order_id=client_order_id,
                exchange_id=exchange_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                status='SENT',
                exchange_response=str(response) if response else None
            )
            db.session.add(order)
            db.session.commit()
            logger.info(f"Recorded sent order: {client_order_id}")
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to record sent order: {e}")

    def get_status(self):
        return {
            'running': self.running,
            'risk_config': self.risk_manager.config,
            'active_exchanges': len(self.clients),
            'daily_pnl': self.risk_manager.daily_pnl
        }

    def manual_close_position(self, exchange_id, symbol, position_side):
        client = self._get_client(exchange_id)
        if not client:
            return {'success': False, 'error': 'Client not found'}
        result = client.close_position(symbol, position_side)
        return {'success': True, 'data': result}


# Initialize
engine = TradingEngine(app)
sentiment_analyzer = SentimentAnalyzer()

# ═══════════════════════════════════════════════════════════════════════
# ROUTES: AUTH (JWT)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')

    if not username or len(username) < 3:
        return jsonify({'success': False, 'error': 'Username must be at least 3 characters'}), 400
    if not password or len(password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'error': 'Username already taken'}), 409

    user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()

    tokens = create_tokens(user.id, user.username)
    return jsonify({
        'success': True, 'message': 'User registered',
        'user': user.to_dict(), **tokens
    }), 201

@app.route('/api/auth/login', methods=['POST'])
def api_login_jwt():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')

    # Backward compatibility: create first admin from AUTH_PASSWORD
    auth_pass = os.environ.get('AUTH_PASSWORD')
    if auth_pass and not User.query.first():
        admin = User(username='admin', password_hash=generate_password_hash(auth_pass))
        db.session.add(admin)
        db.session.commit()

    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

    tokens = create_tokens(user.id, user.username)
    return jsonify({
        'success': True, 'message': 'Login successful',
        'user': user.to_dict(), **tokens
    })

@app.route('/api/auth/refresh', methods=['POST'])
def api_refresh():
    data = request.get_json() or {}
    refresh_token = data.get('refresh_token')
    if not refresh_token:
        return jsonify({'success': False, 'error': 'Refresh token required'}), 400

    payload, error = decode_token(refresh_token, 'refresh')
    if error:
        return jsonify({'success': False, 'error': error}), 401

    user = User.query.get(payload['user_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401

    tokens = create_tokens(user.id, user.username)
    return jsonify({'success': True, **tokens})

@app.route('/api/auth/me', methods=['GET'])
@jwt_required
def api_me():
    user = User.query.get(g.user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    return jsonify({'success': True, 'user': user.to_dict()})

@app.route('/api/auth/change-password', methods=['POST'])
@jwt_required
def api_change_password():
    data = request.get_json() or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    if not old_password or not new_password or len(new_password) < 6:
        return jsonify({'success': False, 'error': 'Invalid passwords'}), 400

    user = User.query.get(g.user_id)
    if not user or not check_password_hash(user.password_hash, old_password):
        return jsonify({'success': False, 'error': 'Invalid old password'}), 401

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Password changed'})

@app.route('/logout')
def logout():
    # JWT is stateless — client discards token
    return redirect(url_for('index'))


# ═══════════════════════════════════════════════════════════════════════
# ROUTES: PAGES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/dashboard')
def dashboard():
    exchanges = Exchange.query.all()
    positions = Position.query.filter_by(status='OPEN').all()
    demo_only = all(ex.is_demo for ex in exchanges) if exchanges else True
    active_count = sum(1 for ex in exchanges if ex.is_active)
    return render_template('dashboard.html',
        exchanges=exchanges, positions=positions,
        demo_only=demo_only, active_count=active_count
    )

@app.route('/exchanges')
def exchanges_page():
    return render_template('exchanges.html', supported=app.config['SUPPORTED_EXCHANGES'])


# ═══════════════════════════════════════════════════════════════════════
# API: EXCHANGES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/exchanges', methods=['GET'])
@jwt_required
def get_exchanges():
    return jsonify({'success': True, 'data': ExchangeManager.get_all_exchanges()})

@app.route('/api/exchanges', methods=['POST'])
@jwt_required
def add_exchange():
    data = request.get_json() or {}
    try:
        ex = ExchangeManager.add_exchange(
            name=data.get('name'),
            display_name=data.get('display_name', data.get('name', '').upper()),
            api_key=data.get('api_key', '').strip(),
            api_secret=data.get('api_secret', '').strip(),
            passphrase=data.get('passphrase', '').strip() or None,
            is_demo=data.get('is_demo', True)
        )
        return jsonify({'success': True, 'id': ex.id})
    except ValueError as e:
        logger.warning(f"Add exchange validation error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Add exchange error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
@jwt_required
def delete_exchange(exchange_id):
    return jsonify({'success': ExchangeManager.delete_exchange(exchange_id)})

@app.route('/api/exchanges/<int:exchange_id>/toggle', methods=['POST'])
@jwt_required
def toggle_exchange(exchange_id):
    result = ExchangeManager.toggle_active(exchange_id)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


# ═══════════════════════════════════════════════════════════════════════
# API: POSITIONS
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/positions')
@jwt_required
def get_positions():
    positions = Position.query.filter_by(status='OPEN').all()
    return jsonify({'success': True, 'data': [p.to_dict() for p in positions]})

@app.route('/api/positions/live')
@jwt_required
def get_live_positions():
    exchanges = Exchange.query.filter_by(is_active=True).all()
    all_positions = []
    for ex in exchanges:
        client = ExchangeManager.get_client(ex.id)
        if not client:
            continue
        try:
            data = client.get_positions()
            parsed = parse_all_positions(data, ex.name)
            for pos in parsed:
                pos['exchange_id'] = ex.id
                all_positions.append(pos)
        except Exception as e:
            logger.error(f"Live positions error for {ex.name}: {e}")
    return jsonify(all_positions)

@app.route('/api/positions/<int:position_id>/close', methods=['POST'])
@jwt_required
def close_position(position_id):
    pos = db.session.get(Position, position_id)
    if not pos:
        return jsonify({'success': False, 'error': 'Position not found'}), 404
    client = ExchangeManager.get_client(pos.exchange_id)
    if not client:
        return jsonify({'success': False, 'error': 'Client not found'}), 400
    try:
        result = client.close_position(pos.symbol, pos.side)
        pos.status = 'CLOSED'
        pos.closed_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ═══════════════════════════════════════════════════════════════════════
# API: BOT CONTROL
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/bot/status')
@jwt_required
def bot_status():
    return jsonify(engine.get_status())

@app.route('/api/bot/start', methods=['POST'])
@jwt_required
def bot_start():
    engine.start()
    return jsonify({'success': True, 'status': 'running'})

@app.route('/api/bot/stop', methods=['POST'])
@jwt_required
def bot_stop():
    engine.stop()
    return jsonify({'success': True, 'status': 'stopped'})


# ═══════════════════════════════════════════════════════════════════════
# API: SENTIMENT
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/sentiment')
@jwt_required
def get_sentiment():
    symbol = request.args.get('symbol', 'BTC')
    data = sentiment_analyzer.analyze(symbol)
    return jsonify({'success': True, 'data': data})


# ═══════════════════════════════════════════════════════════════════════
# API: BALANCE
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/balance')
@jwt_required
def get_balance():
    exchanges = Exchange.query.filter_by(is_active=True).all()
    balances = {}
    total = 0
    for ex in exchanges:
        client = ExchangeManager.get_client(ex.id)
        if not client:
            continue
        try:
            data = client.get_balance()
            bal = parse_balance(data, ex.name)
            balances[ex.name] = {'total': bal, 'available': bal}
            total += bal
        except Exception as e:
            logger.error(f"Balance error for {ex.name}: {e}")
    return jsonify({'balances': balances, 'total': total})


# ═══════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'version': app.config['VERSION'],
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        logger.info("Database tables created")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
