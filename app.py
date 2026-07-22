#!/usr/bin/env python3
"""
SUPERBOT v5.6.0 Mercedes Full - JWT Auth Edition
Multi-user JWT auth, idempotent orders, auto-reset daily PnL
Fixed: close_position with exact size, klines all exchanges, type safety
Duplicate exchange prevention added
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
import jwt
import requests
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from functools import wraps

from flask import Flask, request, jsonify, render_template, redirect, url_for, make_response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ── Import pure functions from core/parsers ─────────────────────────────
from core.parsers import parse_balance, parse_all_positions, parse_klines
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
# Security: SECRET_KEY must be set in production
# For CI/testing, auto-generate a temporary key (DO NOT use in production!)
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if os.environ.get('RENDER') or os.environ.get('PRODUCTION'):
        raise RuntimeError(
            "FATAL: SECRET_KEY environment variable is not set! "
            "Set it in Render Dashboard → Environment Variables. "
            "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    # CI/development fallback - generate temporary key
    import secrets
    SECRET_KEY = secrets.token_hex(32)
    logger.warning("WARNING: Using auto-generated SECRET_KEY. Set SECRET_KEY in production!")

app.config['SECRET_KEY'] = SECRET_KEY
app.config['VERSION'] = '5.6.0'
app.config['EDITION'] = 'Mercedes'
app.config['SUPPORTED_EXCHANGES'] = ['bingx', 'binance', 'bybit', 'okx']

# JWT Config
JWT_SECRET = os.environ.get('JWT_SECRET', SECRET_KEY)
JWT_ACCESS_EXPIRE = int(os.environ.get('JWT_ACCESS_EXPIRE_MINUTES', '60'))
JWT_REFRESH_EXPIRE = int(os.environ.get('JWT_REFRESH_EXPIRE_DAYS', '7'))

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
    # Allow specific origins - update with your actual domain
    allowed_origins = os.environ.get('ALLOWED_ORIGINS', 'https://super-bot-merc.onrender.com').split(',')
    origin = request.headers.get('Origin', '')
    if origin in allowed_origins or '*' in allowed_origins:
        response.headers.add('Access-Control-Allow-Origin', origin or allowed_origins[0])
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

# ═══════════════════════════════════════════════════════════════════════
# JWT HELPERS
# ═══════════════════════════════════════════════════════════════════════

def generate_tokens(user_id: int, username: str) -> dict:
    """Generate access + refresh JWT tokens."""
    now = datetime.now(timezone.utc)
    access_payload = {
        'user_id': user_id,
        'username': username,
        'type': 'access',
        'iat': now,
        'exp': now + timedelta(minutes=JWT_ACCESS_EXPIRE)
    }
    refresh_payload = {
        'user_id': user_id,
        'username': username,
        'type': 'refresh',
        'iat': now,
        'exp': now + timedelta(days=JWT_REFRESH_EXPIRE)
    }
    access_token = jwt.encode(access_payload, JWT_SECRET, algorithm='HS256')
    refresh_token = jwt.encode(refresh_payload, JWT_SECRET, algorithm='HS256')
    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_in': JWT_ACCESS_EXPIRE * 60
    }


def decode_token(token: str, token_type: str = 'access') -> dict:
    """Decode and validate JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if payload.get('type') != token_type:
            return {'error': f'Invalid token type, expected {token_type}'}
        return payload
    except jwt.ExpiredSignatureError:
        return {'error': 'Token expired'}
    except jwt.InvalidTokenError:
        return {'error': 'Invalid token'}


def get_auth_user() -> 'User':
    """Get current user from Authorization header."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:]
    payload = decode_token(token, 'access')
    if 'error' in payload:
        return None
    return User.query.get(payload.get('user_id'))


def jwt_required(f):
    """Decorator to protect API routes with JWT."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_auth_user()
        if not user:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        request.current_user = user
        return f(*args, **kwargs)
    return decorated


def jwt_optional(f):
    """Decorator for routes that accept JWT but don't require it."""
    @wraps(f)
    def decorated(*args, **kwargs):
        request.current_user = get_auth_user()
        return f(*args, **kwargs)
    return decorated


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
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_active': self.is_active
        }

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Exchange(db.Model):
    __tablename__ = 'exchanges'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
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
            'id': self.id, 'user_id': self.user_id,
            'name': self.name, 'display_name': self.display_name,
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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
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
            'id': self.id, 'user_id': self.user_id, 'exchange_id': self.exchange_id,
            'symbol': self.symbol, 'side': self.side, 'entry_price': self.entry_price,
            'size': self.size, 'leverage': self.leverage, 'pnl': self.pnl,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None
        }


class BotSettings(db.Model):
    __tablename__ = 'bot_settings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    key = db.Column(db.String(100), nullable=False)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint('user_id', 'key', name='uix_user_setting'),)


class SentOrder(db.Model):
    """Idempotent order tracking — prevents duplicate orders"""
    __tablename__ = 'sent_orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
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
            'id': self.id, 'user_id': self.user_id,
            'client_order_id': self.client_order_id,
            'exchange_id': self.exchange_id, 'symbol': self.symbol,
            'side': self.side, 'quantity': self.quantity,
            'price': self.price, 'order_type': self.order_type,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


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

    def get_klines(self, symbol, interval='1h', limit=100):
        # Binance uses different interval format: 1h -> 1h
        binance_interval = interval
        return self._request('GET', '/fapi/v1/klines', {
            'symbol': symbol.replace('-', ''),
            'interval': binance_interval,
            'limit': limit
        })

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
        positions = self.get_positions()
        if 'error' in positions:
            return positions
        position_amt = None
        for pos in positions:
            if pos.get('symbol') == symbol:
                amt = float(pos.get('positionAmt', 0))
                if (side == 'LONG' and amt > 0) or (side == 'SHORT' and amt < 0):
                    position_amt = abs(amt)
                    break
        if position_amt is None or position_amt == 0:
            return {'error': f'No open position found for {symbol} {side}'}
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        return self.place_order(symbol, close_side, 'MARKET', position_amt)


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

    def get_klines(self, symbol, interval='1h', limit=100):
        # Bybit uses format like 60 for 1h
        bybit_interval = interval
        return self._request('GET', '/v5/market/kline', {
            'category': 'linear',
            'symbol': symbol,
            'interval': bybit_interval,
            'limit': limit
        })

    def place_order(self, symbol, side, order_type, quantity, price=None):
        params = {'category': 'linear', 'symbol': symbol, 'side': side, 'orderType': order_type, 'qty': str(quantity)}
        if price and order_type != 'Market':
            params['price'] = str(price)
        return self._request('POST', '/v5/order/create', params, signed=True)

    def close_position(self, symbol, side):
        positions = self.get_positions()
        if 'error' in positions:
            return positions
        if positions.get('retCode') != 0:
            return {'error': f"Bybit API error: {positions.get('retMsg', 'Unknown')}"}
        position_size = None
        for pos in positions.get('result', {}).get('list', []):
            if pos.get('symbol') == symbol and pos.get('side', '').upper() == side:
                position_size = float(pos.get('size', 0))
                break
        if position_size is None or position_size == 0:
            return {'error': f'No open position found for {symbol} {side}'}
        close_side = 'Sell' if side == 'LONG' else 'Buy'
        params = {
            'category': 'linear',
            'symbol': symbol,
            'side': close_side,
            'orderType': 'Market',
            'qty': str(position_size),
            'reduceOnly': True
        }
        return self._request('POST', '/v5/order/create', params, signed=True)


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

    def get_klines(self, symbol, interval='1h', limit=100):
        # OKX uses interval like 1H
        okx_interval = interval.upper()
        return self._request('GET', '/api/v5/market/candles', {
            'instId': symbol,
            'bar': okx_interval,
            'limit': str(limit)
        })

    def place_order(self, symbol, side, order_type, quantity, price=None):
        params = {'instId': symbol, 'tdMode': 'cross', 'side': side.lower(), 'ordType': order_type.lower(), 'sz': str(quantity)}
        if price and order_type.lower() != 'market':
            params['px'] = str(price)
        return self._request('POST', '/api/v5/trade/order', params, signed=True)

    def close_position(self, symbol, side):
        positions = self.get_positions()
        if 'error' in positions:
            return positions
        if positions.get('code') != '0':
            return {'error': f"OKX API error: {positions.get('msg', 'Unknown')}"}
        position_size = None
        for pos in positions.get('data', []):
            if pos.get('instId') == symbol and pos.get('posSide') == ('long' if side == 'LONG' else 'short'):
                position_size = abs(float(pos.get('pos', 0)))
                break
        if position_size is None or position_size == 0:
            return {'error': f'No open position found for {symbol} {side}'}
        close_side = 'sell' if side == 'LONG' else 'buy'
        return self.place_order(symbol, close_side, 'market', position_size)


# ═══════════════════════════════════════════════════════════════════════
# EXCHANGE MANAGER
# ═══════════════════════════════════════════════════════════════════════

class ExchangeManager:
    @staticmethod
    def add_exchange(user_id, name, display_name, api_key, api_secret, passphrase=None, is_demo=True):
        name = name.lower().strip() if name else ''
        api_key = api_key.strip() if api_key else ''
        api_secret = api_secret.strip() if api_secret else ''

        if not name:
            raise ValueError("Exchange name is required")
        if not api_key:
            raise ValueError("API key is required")
        if not api_secret:
            raise ValueError("API secret is required")
        if name not in app.config['SUPPORTED_EXCHANGES']:
            raise ValueError(f"Unsupported exchange: {name}. Supported: {', '.join(app.config['SUPPORTED_EXCHANGES'])}")

        # FIX: Prevent duplicate exchanges for the same user
        existing = Exchange.query.filter_by(
            user_id=user_id, name=name
        ).first()
        if existing:
            raise ValueError(f"Exchange {name.upper()} already exists for this user (ID: {existing.id}). Use update to change API keys.")

        encrypted_key = encrypt_value(api_key)
        ex = Exchange(
            user_id=user_id,
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
        logger.info(f"Added exchange: {name.upper()} for user {user_id} (ID: {ex.id}, demo: {is_demo})")
        return ex

    @staticmethod
    def get_user_exchanges(user_id):
        return [ex.to_dict() for ex in Exchange.query.filter_by(user_id=user_id).all()]

    @staticmethod
    def delete_exchange(user_id, exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if not ex or ex.user_id != user_id:
            return False
        deleted_positions = Position.query.filter_by(exchange_id=exchange_id).delete()
        db.session.delete(ex)
        db.session.commit()
        logger.info(f"Deleted exchange {exchange_id} for user {user_id} (+{deleted_positions} positions)")
        return True

    @staticmethod
    def toggle_active(user_id, exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if not ex or ex.user_id != user_id:
            return None
        ex.is_active = not ex.is_active
        db.session.commit()
        return ex.to_dict()

    @staticmethod
    def get_decrypted_credentials(user_id, exchange_id):
        ex = db.session.get(Exchange, exchange_id)
        if not ex or ex.user_id != user_id:
            return None
        return {
            'name': ex.name,
            'api_key': decrypt_value(ex.api_key_encrypted),
            'api_secret': decrypt_value(ex.api_secret_encrypted),
            'passphrase': decrypt_value(ex.passphrase_encrypted) if ex.passphrase_encrypted else None,
            'is_demo': ex.is_demo
        }

    @staticmethod
    def get_client(user_id, exchange_id):
        creds = ExchangeManager.get_decrypted_credentials(user_id, exchange_id)
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
# TRADING ENGINE
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
        # Klines cache: { (exchange_id, symbol, interval): (timestamp, data) }
        self._klines_cache = {}
        self._klines_cache_ttl = 240  # 4 minutes (slightly less than 4h timeframe)

    def _get_client(self, user_id, exchange_id):
        cache_key = f"{user_id}_{exchange_id}"
        if cache_key in self.clients:
            return self.clients[cache_key]
        client = ExchangeManager.get_client(user_id, exchange_id)
        if client:
            self.clients[cache_key] = client
        return client

    def _get_cached_klines(self, client, exchange_id, symbol, interval, limit):
        """Get klines with caching and rate limit protection."""
        cache_key = (exchange_id, symbol, interval)
        now = time.time()

        # Check cache
        if cache_key in self._klines_cache:
            cached_time, cached_data = self._klines_cache[cache_key]
            if now - cached_time < self._klines_cache_ttl:
                logger.info(f"Using cached klines for {symbol} (age={int(now-cached_time)}s)")
                return cached_data

        # Fetch fresh data
        klines_data = client.get_klines(symbol, interval=interval, limit=limit)

        # Store in cache
        self._klines_cache[cache_key] = (now, klines_data)

        # Rate limit protection: sleep 300ms between requests
        time.sleep(0.3)

        return klines_data

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
                    self._process_all_users()
            except Exception as e:
                logger.error(f"Engine loop error: {e}")
            time.sleep(60)

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self._last_pnl_reset:
            self.risk_manager.reset_daily_pnl()
            self._last_pnl_reset = today
            logger.info("Daily PnL reset")

    def _process_all_users(self):
        users = User.query.filter_by(is_active=True).all()
        for user in users:
            try:
                self._process_user(user.id)
            except Exception as e:
                logger.error(f"Error processing user {user.id}: {e}")

    def _process_user(self, user_id):
        exchanges = ExchangeManager.get_user_exchanges(user_id)
        for ex in exchanges:
            if not ex.get('is_active'):
                continue
            exchange_id = ex['id']
            client = self._get_client(user_id, exchange_id)
            if not client:
                continue
            try:
                balance_data = client.get_balance()
                balance = parse_balance(balance_data, ex['name'])

                positions_data = client.get_positions()
                self._sync_positions(user_id, exchange_id, positions_data, ex['name'])

                self._analyze_and_trade(user_id, exchange_id, client, balance, ex['name'])
            except Exception as e:
                logger.error(f"Error processing exchange {exchange_id} for user {user_id}: {e}")

    def _sync_positions(self, user_id, exchange_id, data, exchange_name):
        try:
            api_positions = parse_all_positions(data, exchange_name)
            logger.info(f"Sync positions for user {user_id}, exchange {exchange_id}: {len(api_positions)} positions found")

            db_positions = {
                p.symbol: p for p in Position.query.filter_by(
                    user_id=user_id, exchange_id=exchange_id, status='OPEN'
                ).all()
            }

            seen_symbols = set()
            for pos in api_positions:
                symbol = pos['symbol']
                seen_symbols.add(symbol)
                if symbol in db_positions:
                    existing = db_positions[symbol]
                    existing.size = pos['size']
                    existing.pnl = pos['pnl']
                    existing.entry_price = pos['entry_price']
                    existing.leverage = pos['leverage']
                else:
                    new_pos = Position(
                        user_id=user_id,
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

            for symbol, pos in db_positions.items():
                if symbol not in seen_symbols:
                    pos.status = 'CLOSED'
                    pos.closed_at = datetime.now(timezone.utc)
                    self.risk_manager.update_daily_pnl(pos.pnl)

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Position sync error: {e}")

    def _analyze_and_trade(self, user_id, exchange_id, client, balance, exchange_name):
        symbols = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'AVAX-USDT', 'LINK-USDT', 'DOT-USDT', 'XRP-USDT', 'ADA-USDT']
        for symbol in symbols:
            try:
                klines_data = client.get_klines(symbol, interval='4h', limit=100)
                if not isinstance(klines_data, dict):
                    logger.warning(f"Klines invalid type for {symbol}: {type(klines_data)} = {klines_data}")
                    continue
                if 'error' in klines_data:
                    logger.warning(f"Klines API error for {symbol}: {klines_data['error']}")
                    continue

                # FIX: Use universal parse_klines instead of parse_klines_bingx
                candles = parse_klines(klines_data, exchange_name)

                if not candles:
                    logger.info(f"No candles for {symbol}, skipping")
                    continue
                signal = self.ema_strategy.analyze(candles)
                if signal['signal'] != 'NEUTRAL' and signal['confidence'] > 50:
                    self._execute_signal(user_id, exchange_id, client, symbol, signal, balance, exchange_name)
            except Exception as e:
                logger.error(f"Analysis error for {symbol}: {type(e).__name__}: {e}")

    def _execute_signal(self, user_id, exchange_id, client, symbol, signal, balance, exchange_name):
        side = signal['signal']
        current_price = signal['price']
        current_positions = Position.query.filter_by(
            user_id=user_id, exchange_id=exchange_id, status='OPEN'
        ).count()
        position_size = balance * 0.02
        can_trade, reason = self.risk_manager.can_open_position(
            balance, position_size, current_positions
        )
        if not can_trade:
            logger.info(f"Risk block: {reason}")
            return

        sl_tp = self.risk_manager.calculate_sl_tp(current_price, side)
        leverage = self.risk_manager.config['max_leverage']
        client_order_id = self._generate_client_order_id(
            user_id, exchange_id, symbol, side, current_price
        )
        existing = SentOrder.query.filter_by(
            user_id=user_id, client_order_id=client_order_id
        ).first()
        if existing:
            logger.warning(f"IDEMPOTENCY BLOCK: Order {client_order_id} already sent.")
            return

        logger.info(f"Placing {side} order for {symbol} at {current_price}")
        if hasattr(client, 'demo') and client.demo:
            logger.info(f"[DEMO] Would place order: {symbol} {side} @ {current_price}")
            self._record_sent_order(
                user_id, client_order_id, exchange_id, symbol, side,
                round(position_size / current_price, 4),
                current_price, 'MARKET', {'demo': True}
            )
            return

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
                self._record_sent_order(
                    user_id, client_order_id, exchange_id, symbol, side,
                    round(position_size / current_price, 4),
                    current_price, 'MARKET', result
                )
                logger.info(f"Order result: {result}")
        except Exception as e:
            logger.error(f"Order error: {e}")

    def _generate_client_order_id(self, user_id, exchange_id, symbol, side, price):
        timestamp_minute = int(datetime.now(timezone.utc).timestamp() / 60)
        raw = f"{user_id}_{exchange_id}_{symbol}_{side}_{price:.2f}_{timestamp_minute}"
        hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"SB_{user_id}_{exchange_id}_{symbol}_{side}_{hash_suffix}"

    def _record_sent_order(self, user_id, client_order_id, exchange_id, symbol,
                          side, quantity, price, order_type, response):
        try:
            order = SentOrder(
                user_id=user_id,
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

    def manual_close_position(self, user_id, exchange_id, symbol, position_side):
        client = self._get_client(user_id, exchange_id)
        if not client:
            return {'success': False, 'error': 'Client not found'}
        result = client.close_position(symbol, position_side)
        return {'success': True, 'data': result}


# Initialize
engine = TradingEngine(app)
sentiment_analyzer = SentimentAnalyzer()


# ═══════════════════════════════════════════════════════════════════════
# ROUTES: PAGES (JWT-protected via cookie)
# ═══════════════════════════════════════════════════════════════════════

def page_auth_required(f):
    """Decorator for page routes — checks JWT in cookie or redirects to login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('access_token')
        if not token:
            return redirect(url_for('login_page'))
        payload = decode_token(token, 'access')
        if 'error' in payload:
            return redirect(url_for('login_page'))
        user = User.query.get(payload.get('user_id'))
        if not user:
            return redirect(url_for('login_page'))
        request.current_user = user
        return f(*args, **kwargs)
    return decorated


@app.route('/')
def index():
    token = request.cookies.get('access_token')
    if token:
        payload = decode_token(token, 'access')
        if 'error' not in payload:
            return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/dashboard')
@page_auth_required
def dashboard():
    user = request.current_user
    exchanges = Exchange.query.filter_by(user_id=user.id).all()
    positions = Position.query.filter_by(user_id=user.id, status='OPEN').all()
    demo_only = all(ex.is_demo for ex in exchanges) if exchanges else True
    active_count = sum(1 for ex in exchanges if ex.is_active)
    return render_template('dashboard.html',
        exchanges=exchanges, positions=positions,
        demo_only=demo_only, active_count=active_count
    )


@app.route('/exchanges')
@page_auth_required
def exchanges_page():
    return render_template('exchanges.html', supported=app.config['SUPPORTED_EXCHANGES'])


@app.route('/logout')
def logout():
    resp = redirect(url_for('login_page'))
    resp.delete_cookie('access_token')
    resp.delete_cookie('refresh_token')
    return resp


# ═══════════════════════════════════════════════════════════════════════
# API: AUTH (JWT)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')
    invite_code = data.get('invite_code', '')

    # Check if any users already exist - if yes, require invite code
    existing_user_count = User.query.count()
    if existing_user_count > 0:
        # For additional users, require invite code (set in env)
        required_code = os.environ.get('INVITE_CODE')
        if required_code and invite_code != required_code:
            logger.warning(f"Registration attempt with invalid invite code: {username}")
            return jsonify({'success': False, 'error': 'Invalid or missing invite code'}), 403

    if not username or not password:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400
    if len(username) < 3:
        return jsonify({'success': False, 'error': 'Username must be at least 3 characters'}), 400

    existing = User.query.filter_by(username=username).first()
    if existing:
        return jsonify({'success': False, 'error': 'Username already exists'}), 409

    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    tokens = generate_tokens(user.id, user.username)
    logger.info(f"New user registered: {username} (ID: {user.id})")
    return jsonify({'success': True, **tokens})


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

    tokens = generate_tokens(user.id, user.username)
    logger.info(f"User logged in: {username} (ID: {user.id})")
    return jsonify({'success': True, **tokens})


@app.route('/api/auth/refresh', methods=['POST'])
def api_refresh():
    data = request.get_json() or {}
    refresh_token = data.get('refresh_token', '')
    if not refresh_token:
        return jsonify({'success': False, 'error': 'Refresh token required'}), 400

    payload = decode_token(refresh_token, 'refresh')
    if 'error' in payload:
        return jsonify({'success': False, 'error': payload['error']}), 401

    user = User.query.get(payload.get('user_id'))
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401

    tokens = generate_tokens(user.id, user.username)
    return jsonify({'success': True, **tokens})


@app.route('/api/auth/me', methods=['GET'])
@jwt_required
def api_me():
    return jsonify({'success': True, 'data': request.current_user.to_dict()})


# ═══════════════════════════════════════════════════════════════════════
# API: EXCHANGES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/exchanges', methods=['GET'])
@jwt_required
def get_exchanges():
    return jsonify({'success': True, 'data': ExchangeManager.get_user_exchanges(request.current_user.id)})


@app.route('/api/exchanges', methods=['POST'])
@jwt_required
def add_exchange():
    data = request.get_json() or {}
    try:
        # FIX: handle None values safely
        api_key = (data.get('api_key') or '').strip()
        api_secret = (data.get('api_secret') or '').strip()
        passphrase = (data.get('passphrase') or '').strip() or None
        name = (data.get('name') or '').strip().lower()
        display_name = (data.get('display_name') or name.upper() or 'Exchange').strip()
        is_demo = data.get('is_demo', True)

        ex = ExchangeManager.add_exchange(
            user_id=request.current_user.id,
            name=name,
            display_name=display_name,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            is_demo=is_demo
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
    success = ExchangeManager.delete_exchange(request.current_user.id, exchange_id)
    return jsonify({'success': success})


@app.route('/api/exchanges/<int:exchange_id>/toggle', methods=['POST'])
@jwt_required
def toggle_exchange(exchange_id):
    result = ExchangeManager.toggle_active(request.current_user.id, exchange_id)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


# ═══════════════════════════════════════════════════════════════════════
# API: EXCHANGE SELECTOR (Active Exchange)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/exchange/current', methods=['GET'])
@jwt_required
def get_current_exchange():
    """Get currently selected active exchange for the user."""
    user_id = request.current_user.id
    # Get the first active exchange (strict mode — one active)
    active_ex = Exchange.query.filter_by(user_id=user_id, is_active=True).first()
    if active_ex:
        return jsonify({
            'success': True,
            'data': active_ex.to_dict()
        })
    # No active exchange — return first available
    first_ex = Exchange.query.filter_by(user_id=user_id).first()
    if first_ex:
        return jsonify({
            'success': True,
            'data': first_ex.to_dict()
        })
    return jsonify({'success': False, 'error': 'No exchanges configured'}), 404


@app.route('/api/exchange/select', methods=['POST'])
@jwt_required
def select_exchange():
    """Select (activate) a specific exchange. Strict mode: only one active."""
    data = request.get_json() or {}
    exchange_id = data.get('exchange_id')

    if not exchange_id:
        return jsonify({'success': False, 'error': 'exchange_id required'}), 400

    user_id = request.current_user.id

    # Verify exchange belongs to user
    target = db.session.get(Exchange, exchange_id)
    if not target or target.user_id != user_id:
        return jsonify({'success': False, 'error': 'Exchange not found'}), 404

    # STRICT MODE: Deactivate all other exchanges, activate selected
    Exchange.query.filter_by(user_id=user_id).update({'is_active': False})
    target.is_active = True
    db.session.commit()

    logger.info(f"User {user_id} selected exchange {target.name} (ID: {exchange_id})")
    return jsonify({
        'success': True,
        'data': target.to_dict()
    })


# ═══════════════════════════════════════════════════════════════════════
# API: POSITIONS
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/positions')
@jwt_required
def get_positions():
    positions = Position.query.filter_by(
        user_id=request.current_user.id, status='OPEN'
    ).all()
    return jsonify({'success': True, 'data': [p.to_dict() for p in positions]})


@app.route('/api/positions/live')
@jwt_required
def get_live_positions():
    user_id = request.current_user.id
    exchanges = Exchange.query.filter_by(user_id=user_id, is_active=True).all()
    all_positions = []
    for ex in exchanges:
        client = ExchangeManager.get_client(user_id, ex.id)
        if not client:
            continue
        try:
            data = client.get_positions()
            parsed = parse_all_positions(data, ex.name)
            for pos in parsed:
                pos['exchange_id'] = ex.id
                pos['exchange_name'] = ex.name
                all_positions.append(pos)
        except Exception as e:
            logger.error(f"Live positions error for {ex.name}: {e}")
    return jsonify(all_positions)


@app.route('/api/positions/<int:position_id>/close', methods=['POST'])
@jwt_required
def close_position(position_id):
    user_id = request.current_user.id
    pos = db.session.get(Position, position_id)
    if not pos or pos.user_id != user_id:
        return jsonify({'success': False, 'error': 'Position not found'}), 404
    client = ExchangeManager.get_client(user_id, pos.exchange_id)
    if not client:
        return jsonify({'success': False, 'error': 'Client not found'}), 400
    try:
        result = client.close_position(pos.symbol, pos.side)
        pos.status = 'CLOSED'
        pos.closed_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        db.session.rollback()
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
    user_id = request.current_user.id
    exchanges = Exchange.query.filter_by(user_id=user_id, is_active=True).all()
    balances = {}
    total = 0
    for ex in exchanges:
        client = ExchangeManager.get_client(user_id, ex.id)
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
# API: DIAGNOSE (временный, для отладки)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/diagnose')
@jwt_required
def diagnose():
    user_id = request.current_user.id
    exchanges = ExchangeManager.get_user_exchanges(user_id)
    active = [e for e in exchanges if e.get('is_active')]
    
    results = []
    for ex in active:
        client = ExchangeManager.get_client(user_id, ex['id'])
        if not client:
            results.append({'exchange': ex['name'], 'error': 'No client'})
            continue
        
        try:
            bal = client.get_balance()
            pos = client.get_positions()
            klines = client.get_klines('BTC-USDT', '1h', 10)
            
            results.append({
                'exchange': ex['name'],
                'id': ex['id'],
                'demo': getattr(client, 'demo', 'unknown'),
                'balance_type': type(bal).__name__,
                'balance_sample': str(bal)[:300],
                'positions_type': type(pos).__name__,
                'positions_sample': str(pos)[:300],
                'klines_type': type(klines).__name__,
                'klines_sample': str(klines)[:300],
            })
        except Exception as e:
            results.append({'exchange': ex['name'], 'error': str(e)})
    
    return jsonify({
        'success': True,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'exchanges_total': len(exchanges),
        'active': len(active),
        'engine_running': engine.running,
        'engine_daily_pnl': engine.risk_manager.daily_pnl,
        'results': results
    })


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
# DATABASE INIT
# ═══════════════════════════════════════════════════════════════════════

@app.route('/init-db')
@jwt_optional
def init_db_route():
    """Initialize database tables via HTTP call. Requires auth if users exist."""
    with app.app_context():
        try:
            # If users already exist, require auth
            user_count = User.query.count()
            if user_count > 0:
                user = get_auth_user()
                if not user:
                    return jsonify({'success': False, 'error': 'Authentication required. Database already initialized.'}), 401
                if user.id != 1:
                    return jsonify({'success': False, 'error': 'Admin access required'}), 403

            db.create_all()
            logger.info("Database initialized via /init-db")
            return jsonify({
                'success': True,
                'message': 'Database initialized. Tables created.',
                'tables': ['users', 'exchanges', 'positions', 'bot_settings', 'sent_orders']
            })
        except Exception as e:
            logger.error(f"init-db error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/reset-db')
@jwt_required
def reset_db_route():
    """DROP ALL tables and recreate them. WARNING: DESTROYS ALL DATA! Admin only."""
    user = request.current_user
    # Only first registered user (ID=1) or explicit admin can reset
    if user.id != 1:
        logger.warning(f"Unauthorized /reset-db attempt by user {user.id} ({user.username})")
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    with app.app_context():
        try:
            db.drop_all()
            logger.warning(f"Database DROP ALL executed by admin {user.username} via /reset-db")
            db.create_all()
            logger.info("Database recreated via /reset-db")
            return jsonify({
                'success': True,
                'message': 'Database reset complete. All tables dropped and recreated.',
                'tables': ['users', 'exchanges', 'positions', 'bot_settings', 'sent_orders']
            })
        except Exception as e:
            logger.error(f"reset-db error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        logger.info("Database tables created")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
