#!/usr/bin/env python3
"""
SUPERBOT v5.5.37 Mercedes Full
Multi-Exchange Trading Bot - Modular Architecture
Uses: config.py, models.py, exchange_manager.py, engine.py, bingx.py, risk.py, analyzer.py, xgboost_filter.py
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime

from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

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

# Load config
from config import Config
app.config.from_object(Config)
app.config['SECRET_KEY'] = Config.SECRET_KEY

# CORS
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ── Database (SQLAlchemy) ──────────────────────────────────────────────
from models import db, Exchange, BotSettings, Position

# Fix postgres:// -> postgresql:// for SQLAlchemy
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
db.init_app(app)

# ── Login Manager ──────────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id_val, username, password_hash):
        self.id = id_val
        self.username = username
        self.password_hash = password_hash

@login_manager.user_loader
def load_user(user_id):
    # Check auth password first (simple mode)
    auth_pass = os.environ.get('AUTH_PASSWORD')
    if auth_pass:
        return User(1, 'admin', generate_password_hash(auth_pass))
    return None

# ── Context Processor ──────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    return {
        'version': Config.VERSION,
        'edition': Config.EDITION,
        'now': datetime.utcnow()
    }

# ── Routes: Auth ─────────────────────────────────────────────────────────
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        auth_pass = os.environ.get('AUTH_PASSWORD')
        if auth_pass and password == auth_pass:
            user = User(1, 'admin', generate_password_hash(auth_pass))
            login_user(user)
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Неверный пароль')
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    password = data.get('password')
    auth_pass = os.environ.get('AUTH_PASSWORD')
    if auth_pass and password == auth_pass:
        user = User(1, 'admin', generate_password_hash(auth_pass))
        login_user(user)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Неверный пароль'}), 401

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# ── Routes: Dashboard ────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    exchanges = Exchange.query.all()
    positions = Position.query.filter_by(status='OPEN').all()
    demo_only = all(ex.is_demo for ex in exchanges) if exchanges else True
    active_count = sum(1 for ex in exchanges if ex.is_active)

    return render_template('dashboard.html',
        exchanges=exchanges,
        positions=positions,
        demo_only=demo_only,
        active_count=active_count
    )

@app.route('/exchanges')
@login_required
def exchanges_page():
    return render_template('exchanges.html', supported=Config.SUPPORTED_EXCHANGES)

# ── API: Exchanges ───────────────────────────────────────────────────────
from exchange_manager import ExchangeManager

@app.route('/api/exchanges', methods=['GET'])
@login_required
def get_exchanges():
    exchanges = ExchangeManager.get_all_exchanges()
    return jsonify({'success': True, 'data': exchanges})

@app.route('/api/exchanges', methods=['POST'])
@login_required
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
    except Exception as e:
        logger.error(f"Add exchange error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
@login_required
def delete_exchange(exchange_id):
    result = ExchangeManager.delete_exchange(exchange_id)
    return jsonify({'success': result})

@app.route('/api/exchanges/<int:exchange_id>/toggle', methods=['POST'])
@login_required
def toggle_exchange(exchange_id):
    result = ExchangeManager.toggle_active(exchange_id)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404

# ── API: Positions ───────────────────────────────────────────────────────
@app.route('/api/positions')
@login_required
def get_positions():
    positions = Position.query.filter_by(status='OPEN').all()
    return jsonify({'success': True, 'data': [p.to_dict() for p in positions]})

@app.route('/api/positions/live')
@login_required
def get_live_positions():
    # Fetch live from exchanges
    from core.api_clients import BingXClient
    exchanges = Exchange.query.filter_by(is_active=True).all()
    all_positions = []

    for ex in exchanges:
        creds = ExchangeManager.get_decrypted_credentials(ex.id)
        if not creds:
            continue
        try:
            if creds['name'] == 'bingx':
                client = BingXClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
                data = client.get_positions()
                if 'data' in data:
                    for pos in data['data']:
                        all_positions.append({
                            'symbol': pos.get('symbol'),
                            'exchange_id': ex.id,
                            'side': 'LONG' if pos.get('positionSide') == 'LONG' else 'SHORT',
                            'entry_price': float(pos.get('avgPrice', 0)),
                            'size': abs(float(pos.get('positionAmt', 0))),
                            'leverage': int(pos.get('leverage', 5)),
                            'pnl': float(pos.get('unrealizedProfit', 0))
                        })
        except Exception as e:
            logger.error(f"Live positions error for {ex.name}: {e}")

    return jsonify(all_positions)

@app.route('/api/positions/<int:position_id>/close', methods=['POST'])
@login_required
def close_position(position_id):
    pos = Position.query.get(position_id)
    if not pos:
        return jsonify({'success': False, 'error': 'Position not found'}), 404

    # Close via API
    creds = ExchangeManager.get_decrypted_credentials(pos.exchange_id)
    if creds and creds['name'] == 'bingx':
        from core.api_clients import BingXClient
        client = BingXClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
        result = client.close_position(pos.symbol, pos.side)
        pos.status = 'CLOSED'
        pos.closed_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'data': result})

    return jsonify({'success': False, 'error': 'Cannot close position'}), 400

# ── API: Bot Control ───────────────────────────────────────────────────
from engine import TradingEngine

engine = TradingEngine(app)

@app.route('/api/bot/status')
@login_required
def bot_status():
    return jsonify(engine.get_status())

@app.route('/api/bot/start', methods=['POST'])
@login_required
def bot_start():
    engine.start()
    return jsonify({'success': True, 'status': 'running'})

@app.route('/api/bot/stop', methods=['POST'])
@login_required
def bot_stop():
    engine.stop()
    return jsonify({'success': True, 'status': 'stopped'})

# ── API: Sentiment ─────────────────────────────────────────────────────
from core.analyzer import SentimentAnalyzer

sentiment_analyzer = SentimentAnalyzer()

@app.route('/api/sentiment')
@login_required
def get_sentiment():
    symbol = request.args.get('symbol', 'BTC')
    data = sentiment_analyzer.analyze(symbol)
    return jsonify({'success': True, 'data': data})

# ── API: Balance ───────────────────────────────────────────────────────
@app.route('/api/balance')
@login_required
def get_balance():
    from core.api_clients import BingXClient
    exchanges = Exchange.query.filter_by(is_active=True).all()
    balances = {}
    total = 0

    for ex in exchanges:
        creds = ExchangeManager.get_decrypted_credentials(ex.id)
        if not creds:
            continue
        try:
            if creds['name'] == 'bingx':
                client = BingXClient(creds['api_key'], creds['api_secret'], creds['is_demo'])
                data = client.get_balance()
                bal = 0
                if 'data' in data and 'balance' in data['data']:
                    bal = float(data['data']['balance']['balance'])
                balances[ex.name] = {'total': bal, 'available': bal}
                total += bal
        except Exception as e:
            logger.error(f"Balance error for {ex.name}: {e}")

    return jsonify({'balances': balances, 'total': total})

# ── Health Check ─────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': Config.VERSION, 'timestamp': datetime.utcnow().isoformat()})

# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        logger.info("Database tables created")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
