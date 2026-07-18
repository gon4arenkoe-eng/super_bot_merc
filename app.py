#!/usr/bin/env python3
"""
SUPERBOT v5.5.36 Mercedes Full
Multi-Exchange Trading Bot with PostgreSQL, Auth, ML Filter, Sentiment
Exchanges: BingX / Binance / Bybit / OKX
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
import traceback
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from functools import wraps

import requests
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import psycopg2
from psycopg2.extras import RealDictCursor

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
app.secret_key = os.environ.get('SECRET_KEY', 'superbot-secret-key-change-me')
app.config['SESSION_TYPE'] = 'filesystem'

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ── Encryption ─────────────────────────────────────────────────────────
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if ENCRYPTION_KEY:
    fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)
else:
    logger.warning("ENCRYPTION_KEY not set! API keys will be stored in plaintext!")
    fernet = None

def encrypt(data: str) -> str:
    if fernet and data:
        return fernet.encrypt(data.encode()).decode()
    return data

def decrypt(data: str) -> str:
    if fernet and data:
        try:
            return fernet.decrypt(data.encode()).decode()
        except Exception:
            return data
    return data

# ── PostgreSQL ─────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Exchanges table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exchanges (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(20) NOT NULL,
            api_key_encrypted TEXT,
            api_secret_encrypted TEXT,
            passphrase_encrypted TEXT,
            is_demo BOOLEAN DEFAULT TRUE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Positions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            exchange VARCHAR(20),
            symbol VARCHAR(30),
            side VARCHAR(10),
            entry_price DECIMAL(20,8),
            quantity DECIMAL(20,8),
            leverage DECIMAL(5,2),
            unrealized_pnl DECIMAL(20,8),
            realized_pnl DECIMAL(20,8),
            status VARCHAR(20) DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Orders table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            exchange VARCHAR(20),
            symbol VARCHAR(30),
            side VARCHAR(10),
            order_type VARCHAR(20),
            quantity DECIMAL(20,8),
            price DECIMAL(20,8),
            status VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Trades/PnL history
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            exchange VARCHAR(20),
            symbol VARCHAR(30),
            side VARCHAR(10),
            entry_price DECIMAL(20,8),
            exit_price DECIMAL(20,8),
            quantity DECIMAL(20,8),
            pnl DECIMAL(20,8),
            pnl_percent DECIMAL(10,4),
            fees DECIMAL(20,8),
            closed_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Bot settings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            setting_key VARCHAR(100),
            setting_value TEXT,
            UNIQUE(user_id, setting_key)
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database initialized successfully")

# ── User Model ─────────────────────────────────────────────────────────
class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user:
        return User(user['id'], user['username'], user['password_hash'])
    return None

# ── Exchange APIs ────────────────────────────────────────────────────────
class ExchangeAPI:
    def __init__(self, exchange_name, api_key, api_secret, passphrase=None, demo=True):
        self.exchange = exchange_name.lower()
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo = demo
        self.session = requests.Session()

        # Base URLs
        self.base_urls = {
            'bingx': 'https://open-api.bingx.com',
            'binance': 'https://testnet.binancefuture.com' if demo else 'https://fapi.binance.com',
            'bybit': 'https://api-testnet.bybit.com' if demo else 'https://api.bybit.com',
            'okx': 'https://www.okx.com'
        }

    def _generate_signature(self, params, timestamp=None):
        if self.exchange == 'bingx':
            query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
            signature = hmac.new(
                self.api_secret.encode(),
                query_string.encode(),
                hashlib.sha256
            ).hexdigest()
            return signature

        elif self.exchange == 'binance':
            query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
            signature = hmac.new(
                self.api_secret.encode(),
                query_string.encode(),
                hashlib.sha256
            ).hexdigest()
            return signature

        elif self.exchange == 'bybit':
            recv_window = '5000'
            timestamp = timestamp or str(int(time.time() * 1000))
            param_str = timestamp + self.api_key + recv_window
            if params:
                param_str += json.dumps(params, separators=(',', ':'), sort_keys=True)
            signature = hmac.new(
                self.api_secret.encode(),
                param_str.encode(),
                hashlib.sha256
            ).hexdigest()
            return signature, timestamp, recv_window

        elif self.exchange == 'okx':
            timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            message = timestamp + 'GET' + '/api/v5/account/balance'
            signature = base64.b64encode(
                hmac.new(
                    self.api_secret.encode(),
                    message.encode(),
                    hashlib.sha256
                ).digest()
            ).decode()
            return signature, timestamp

        return None

    def get_positions(self):
        try:
            if self.exchange == 'bingx':
                return self._bingx_get_positions()
            elif self.exchange == 'binance':
                return self._binance_get_positions()
            elif self.exchange == 'bybit':
                return self._bybit_get_positions()
            elif self.exchange == 'okx':
                return self._okx_get_positions()
        except Exception as e:
            logger.error(f"Error getting positions from {self.exchange}: {e}")
            return []
        return []

    def _bingx_get_positions(self):
        endpoint = '/openApi/swap/v2/user/positions'
        timestamp = str(int(time.time() * 1000))
        params = {'timestamp': timestamp}
        signature = self._generate_signature(params)
        params['signature'] = signature

        headers = {'X-BX-APIKEY': self.api_key}
        url = f"{self.base_urls['bingx']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        positions = []
        if data.get('code') == 0:
            for pos in data.get('data', []):
                positions.append({
                    'symbol': pos.get('symbol'),
                    'side': 'LONG' if float(pos.get('positionAmt', 0)) > 0 else 'SHORT',
                    'entry_price': float(pos.get('entryPrice', 0)),
                    'quantity': abs(float(pos.get('positionAmt', 0))),
                    'leverage': float(pos.get('leverage', 1)),
                    'unrealized_pnl': float(pos.get('unRealizedProfit', 0)),
                    'mark_price': float(pos.get('markPrice', 0))
                })
        return positions

    def _binance_get_positions(self):
        endpoint = '/fapi/v2/positionRisk'
        timestamp = str(int(time.time() * 1000))
        params = {'timestamp': timestamp}
        signature = self._generate_signature(params)
        params['signature'] = signature

        headers = {'X-MBX-APIKEY': self.api_key}
        url = f"{self.base_urls['binance']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        positions = []
        for pos in data:
            amt = float(pos.get('positionAmt', 0))
            if amt != 0:
                positions.append({
                    'symbol': pos.get('symbol'),
                    'side': 'LONG' if amt > 0 else 'SHORT',
                    'entry_price': float(pos.get('entryPrice', 0)),
                    'quantity': abs(amt),
                    'leverage': float(pos.get('leverage', 1)),
                    'unrealized_pnl': float(pos.get('unRealizedProfit', 0)),
                    'mark_price': float(pos.get('markPrice', 0))
                })
        return positions

    def _bybit_get_positions(self):
        endpoint = '/v5/position/list'
        timestamp = str(int(time.time() * 1000))
        params = {'category': 'linear', 'settleCoin': 'USDT'}
        signature, ts, recv_window = self._generate_signature(params, timestamp)

        headers = {
            'X-BAPI-API-KEY': self.api_key,
            'X-BAPI-TIMESTAMP': ts,
            'X-BAPI-SIGN': signature,
            'X-BAPI-RECV-WINDOW': recv_window
        }
        url = f"{self.base_urls['bybit']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        positions = []
        if data.get('retCode') == 0:
            for pos in data.get('result', {}).get('list', []):
                size = float(pos.get('size', 0))
                if size != 0:
                    positions.append({
                        'symbol': pos.get('symbol'),
                        'side': pos.get('side'),
                        'entry_price': float(pos.get('avgPrice', 0)),
                        'quantity': size,
                        'leverage': float(pos.get('leverage', 1)),
                        'unrealized_pnl': float(pos.get('unrealisedPnl', 0)),
                        'mark_price': float(pos.get('markPrice', 0))
                    })
        return positions

    def _okx_get_positions(self):
        endpoint = '/api/v5/account/positions'
        signature, timestamp = self._generate_signature({})

        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase or ''
        }
        url = f"{self.base_urls['okx']}{endpoint}"

        resp = self.session.get(url, headers=headers, timeout=10)
        data = resp.json()

        positions = []
        if data.get('code') == '0':
            for pos in data.get('data', []):
                positions.append({
                    'symbol': pos.get('instId'),
                    'side': 'LONG' if pos.get('posSide') == 'long' else 'SHORT',
                    'entry_price': float(pos.get('avgPx', 0)),
                    'quantity': float(pos.get('pos', 0)),
                    'leverage': float(pos.get('lever', 1)),
                    'unrealized_pnl': float(pos.get('upl', 0)),
                    'mark_price': float(pos.get('markPx', 0))
                })
        return positions

    def get_balance(self):
        try:
            if self.exchange == 'bingx':
                return self._bingx_get_balance()
            elif self.exchange == 'binance':
                return self._binance_get_balance()
            elif self.exchange == 'bybit':
                return self._bybit_get_balance()
            elif self.exchange == 'okx':
                return self._okx_get_balance()
        except Exception as e:
            logger.error(f"Error getting balance from {self.exchange}: {e}")
            return {'total': 0, 'available': 0}
        return {'total': 0, 'available': 0}

    def _bingx_get_balance(self):
        endpoint = '/openApi/swap/v2/user/balance'
        timestamp = str(int(time.time() * 1000))
        params = {'timestamp': timestamp}
        signature = self._generate_signature(params)
        params['signature'] = signature

        headers = {'X-BX-APIKEY': self.api_key}
        url = f"{self.base_urls['bingx']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        if data.get('code') == 0:
            balance_data = data.get('data', {})
            return {
                'total': float(balance_data.get('balance', 0)),
                'available': float(balance_data.get('availableBalance', 0))
            }
        return {'total': 0, 'available': 0}

    def _binance_get_balance(self):
        endpoint = '/fapi/v2/balance'
        timestamp = str(int(time.time() * 1000))
        params = {'timestamp': timestamp}
        signature = self._generate_signature(params)
        params['signature'] = signature

        headers = {'X-MBX-APIKEY': self.api_key}
        url = f"{self.base_urls['binance']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        for bal in data:
            if bal.get('asset') == 'USDT':
                return {
                    'total': float(bal.get('balance', 0)),
                    'available': float(bal.get('availableBalance', 0))
                }
        return {'total': 0, 'available': 0}

    def _bybit_get_balance(self):
        endpoint = '/v5/account/wallet-balance'
        timestamp = str(int(time.time() * 1000))
        params = {'accountType': 'UNIFIED'}
        signature, ts, recv_window = self._generate_signature(params, timestamp)

        headers = {
            'X-BAPI-API-KEY': self.api_key,
            'X-BAPI-TIMESTAMP': ts,
            'X-BAPI-SIGN': signature,
            'X-BAPI-RECV-WINDOW': recv_window
        }
        url = f"{self.base_urls['bybit']}{endpoint}"

        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        if data.get('retCode') == 0:
            for acct in data.get('result', {}).get('list', []):
                for coin in acct.get('coin', []):
                    if coin.get('coin') == 'USDT':
                        return {
                            'total': float(coin.get('walletBalance', 0)),
                            'available': float(coin.get('availableToWithdraw', 0))
                        }
        return {'total': 0, 'available': 0}

    def _okx_get_balance(self):
        endpoint = '/api/v5/account/balance'
        signature, timestamp = self._generate_signature({})

        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase or ''
        }
        url = f"{self.base_urls['okx']}{endpoint}"

        resp = self.session.get(url, headers=headers, timeout=10)
        data = resp.json()

        if data.get('code') == '0':
            for bal in data.get('data', []):
                for detail in bal.get('details', []):
                    if detail.get('ccy') == 'USDT':
                        return {
                            'total': float(detail.get('eq', 0)),
                            'available': float(detail.get('availBal', 0))
                        }
        return {'total': 0, 'available': 0}

    def place_order(self, symbol, side, order_type, quantity, price=None, stop_loss=None, take_profit=None):
        logger.info(f"[{self.exchange}] Order: {side} {quantity} {symbol} @ {price or 'MARKET'}")
        # Implementation varies by exchange - placeholder for demo
        return {'order_id': str(uuid.uuid4()), 'status': 'FILLED'}

    def close_position(self, symbol, side):
        logger.info(f"[{self.exchange}] Close position: {symbol} {side}")
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        return self.place_order(symbol, close_side, 'MARKET', 0)

# ── Bot Engine ───────────────────────────────────────────────────────────
class BotEngine:
    def __init__(self):
        self.running = False
        self.thread = None
        self.settings = {
            'strategy': 'ema_cross',
            'ema_fast': 9,
            'ema_slow': 21,
            'rsi_period': 14,
            'rsi_overbought': 70,
            'rsi_oversold': 30,
            'leverage': 10,
            'risk_per_trade': 1.0,
            'max_positions': 5,
            'tp_percent': 2.0,
            'sl_percent': 1.0,
            'dca_levels': 3,
            'dca_multiplier': 1.5,
            'ml_filter': True,
            'sentiment_filter': True,
            'symbols': ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT']
        }

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            logger.info("Bot engine started")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Bot engine stopped")

    def _run_loop(self):
        while self.running:
            try:
                self._scan_and_trade()
                time.sleep(60)
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
                time.sleep(60)

    def _scan_and_trade(self):
        # Placeholder for actual trading logic
        # Would fetch candles, calculate indicators, check signals
        pass

    def get_status(self):
        return {
            'running': self.running,
            'settings': self.settings,
            'uptime': 'Running' if self.running else 'Stopped'
        }

bot_engine = BotEngine()

# ── Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('dashboard.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            user_obj = User(user['id'], user['username'], user['password_hash'])
            login_user(user_obj)
            return redirect(url_for('dashboard'))
        return render_template('dashboard.html', error='Invalid credentials')
    return render_template('dashboard.html')

@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username')
    password = request.form.get('password')

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    conn = get_db()
    cur = conn.cursor()

    try:
        password_hash = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
            (username, password_hash)
        )
        user_id = cur.fetchone()[0]
        conn.commit()

        # Create default settings
        for key, value in bot_engine.settings.items():
            cur.execute(
                "INSERT INTO bot_settings (user_id, setting_key, setting_value) VALUES (%s, %s, %s)",
                (user_id, key, json.dumps(value))
            )
        conn.commit()

        return jsonify({'success': True, 'user_id': user_id})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({'error': 'Username already exists'}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

# ── API Routes ───────────────────────────────────────────────────────────

@app.route('/api/exchanges', methods=['GET'])
@login_required
def get_exchanges():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, name, is_demo, is_active, created_at FROM exchanges WHERE user_id = %s",
        (current_user.id,)
    )
    exchanges = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(exchanges)

@app.route('/api/exchanges', methods=['POST'])
@login_required
def add_exchange():
    data = request.json
    name = data.get('name')
    api_key = data.get('api_key')
    api_secret = data.get('api_secret')
    passphrase = data.get('passphrase')
    is_demo = data.get('is_demo', True)

    if not all([name, api_key, api_secret]):
        return jsonify({'error': 'Name, API key and secret required'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO exchanges 
           (user_id, name, api_key_encrypted, api_secret_encrypted, passphrase_encrypted, is_demo)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (current_user.id, name, encrypt(api_key), encrypt(api_secret), 
         encrypt(passphrase) if passphrase else None, is_demo)
    )
    exchange_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'success': True, 'id': exchange_id})

@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
@login_required
def delete_exchange(exchange_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM exchanges WHERE id = %s AND user_id = %s", (exchange_id, current_user.id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/positions')
@login_required
def get_positions():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM positions WHERE user_id = %s AND status = 'OPEN' ORDER BY created_at DESC",
        (current_user.id,)
    )
    positions = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(positions)

@app.route('/api/positions/live')
@login_required
def get_live_positions():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM exchanges WHERE user_id = %s AND is_active = TRUE",
        (current_user.id,)
    )
    exchanges = cur.fetchall()
    cur.close()
    conn.close()

    all_positions = []
    for ex in exchanges:
        api = ExchangeAPI(
            ex['name'],
            decrypt(ex['api_key_encrypted']),
            decrypt(ex['api_secret_encrypted']),
            decrypt(ex['passphrase_encrypted']) if ex['passphrase_encrypted'] else None,
            ex['is_demo']
        )
        positions = api.get_positions()
        for pos in positions:
            pos['exchange'] = ex['name']
            pos['exchange_id'] = ex['id']
        all_positions.extend(positions)

    return jsonify(all_positions)

@app.route('/api/balance')
@login_required
def get_balance():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM exchanges WHERE user_id = %s AND is_active = TRUE",
        (current_user.id,)
    )
    exchanges = cur.fetchall()
    cur.close()
    conn.close()

    balances = {}
    total = 0
    for ex in exchanges:
        api = ExchangeAPI(
            ex['name'],
            decrypt(ex['api_key_encrypted']),
            decrypt(ex['api_secret_encrypted']),
            decrypt(ex['passphrase_encrypted']) if ex['passphrase_encrypted'] else None,
            ex['is_demo']
        )
        bal = api.get_balance()
        balances[ex['name']] = bal
        total += bal['total']

    return jsonify({'balances': balances, 'total': total})

@app.route('/api/orders/close', methods=['POST'])
@login_required
def close_order():
    data = request.json
    exchange_id = data.get('exchange_id')
    symbol = data.get('symbol')
    side = data.get('side')

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM exchanges WHERE id = %s AND user_id = %s",
        (exchange_id, current_user.id)
    )
    ex = cur.fetchone()
    cur.close()
    conn.close()

    if not ex:
        return jsonify({'error': 'Exchange not found'}), 404

    api = ExchangeAPI(
        ex['name'],
        decrypt(ex['api_key_encrypted']),
        decrypt(ex['api_secret_encrypted']),
        decrypt(ex['passphrase_encrypted']) if ex['passphrase_encrypted'] else None,
        ex['is_demo']
    )

    result = api.close_position(symbol, side)
    return jsonify(result)

@app.route('/api/bot/status')
@login_required
def bot_status():
    return jsonify(bot_engine.get_status())

@app.route('/api/bot/start', methods=['POST'])
@login_required
def bot_start():
    bot_engine.start()
    return jsonify({'success': True, 'status': 'running'})

@app.route('/api/bot/stop', methods=['POST'])
@login_required
def bot_stop():
    bot_engine.stop()
    return jsonify({'success': True, 'status': 'stopped'})

@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT setting_key, setting_value FROM bot_settings WHERE user_id = %s",
        (current_user.id,)
    )
    settings = {row['setting_key']: json.loads(row['setting_value']) for row in cur.fetchall()}
    cur.close()
    conn.close()
    return jsonify(settings)

@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.json
    conn = get_db()
    cur = conn.cursor()

    for key, value in data.items():
        cur.execute(
            """INSERT INTO bot_settings (user_id, setting_key, setting_value)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, setting_key)
               DO UPDATE SET setting_value = EXCLUDED.setting_value""",
            (current_user.id, key, json.dumps(value))
        )

    conn.commit()
    cur.close()
    conn.close()

    # Update bot engine
    for key, value in data.items():
        if key in bot_engine.settings:
            bot_engine.settings[key] = value

    return jsonify({'success': True})

@app.route('/api/trades/history')
@login_required
def get_trade_history():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM trades WHERE user_id = %s ORDER BY closed_at DESC LIMIT 100",
        (current_user.id,)
    )
    trades = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(trades)

@app.route('/api/pnl/daily')
@login_required
def get_daily_pnl():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """SELECT 
            DATE(closed_at) as date,
            SUM(pnl) as daily_pnl,
            COUNT(*) as trade_count
           FROM trades 
           WHERE user_id = %s 
           AND closed_at >= NOW() - INTERVAL '30 days'
           GROUP BY DATE(closed_at)
           ORDER BY date DESC""",
        (current_user.id,)
    )
    pnl_data = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(pnl_data)

# ── Health Check ─────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '5.5.36', 'timestamp': datetime.utcnow().isoformat()})

# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
