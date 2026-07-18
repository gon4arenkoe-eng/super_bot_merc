"""SUPERBOT v5.5.36 Mercedes Edition - Flask Server"""
import os
import functools
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session

from config import config
from models import db, Exchange, BotSettings, Position
from crypto_utils import encrypt_value, decrypt_value
from exchange_manager import ExchangeManager
from core.engine import TradingEngine
from core.sentiment.analyzer import SentimentAnalyzer
from core.ml.xgboost_filter import XGBoostFilter

# Ensure logs directory exists
os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)


    def to_dict(self, include_secrets=False):
        """Serialize to dict"""
        data = {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'is_demo': self.is_demo,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_secrets:
            from crypto_utils import decrypt_value
            data['api_key'] = decrypt_value(self.api_key_encrypted)
            data['api_secret'] = decrypt_value(self.api_secret_encrypted)
            data['passphrase'] = decrypt_value(self.passphrase_encrypted) if self.passphrase_encrypted else ''
        else:
            # Mask secrets for display
            data['api_key_masked'] = self._mask_string(decrypt_value(self.api_key_encrypted)) if self.api_key_encrypted else ''
        return data


# ═══════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_password = app.config.get('AUTH_PASSWORD')
        if not auth_password:
            return f(*args, **kwargs)

        if session.get('authenticated'):
            return f(*args, **kwargs)

        auth_header = request.headers.get('X-Auth-Token')
        if auth_header == auth_password:
            return f(*args, **kwargs)

        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'success': False, 'error': 'Authentication required'}), 401

        return redirect(url_for('login_page', next=request.path))
    return decorated


# ═══════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════

@app.route('/login', methods=['GET'])
def login_page():
    auth_password = app.config.get('AUTH_PASSWORD')
    if not auth_password:
        session['authenticated'] = True
        next_page = request.args.get('next', '/')
        return redirect(next_page)
    return render_template('login.html', version=app.config['VERSION'])


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    password = data.get('password', '')
    auth_password = app.config.get('AUTH_PASSWORD')

    if not auth_password:
        session['authenticated'] = True
        return jsonify({'success': True})

    if password == auth_password:
        session['authenticated'] = True
        return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'Invalid password'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('authenticated', None)
    return jsonify({'success': True})


# ═══════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════

@app.route('/')
@require_auth
def dashboard():
    exchanges = ExchangeManager.get_all_exchanges()
    active_exchanges = [ex for ex in exchanges if ex.get('is_active')]
    demo_only = all(ex.get('is_demo', True) for ex in exchanges) if exchanges else True

    positions = Position.query.filter_by(status='OPEN').all()
    positions_data = [{
        'id': p.id,
        'symbol': p.symbol,
        'side': p.side,
        'entry_price': p.entry_price,
        'size': p.size,
        'leverage': p.leverage,
        'pnl': p.pnl,
        'exchange_id': p.exchange_id
    } for p in positions]

    return render_template('dashboard.html',
                         version=app.config['VERSION'],
                         edition=app.config['EDITION'],
                         exchanges=exchanges,
                         active_count=len(active_exchanges),
                         demo_only=demo_only,
                         positions=positions_data,
                         now=datetime.utcnow())


# ═══════════════════════════════════════════
# EXCHANGES
# ═══════════════════════════════════════════

@app.route('/exchanges')
@require_auth
def exchanges_page():
    exchanges = ExchangeManager.get_all_exchanges()
    supported = app.config['SUPPORTED_EXCHANGES']
    return render_template('exchanges.html',
                         exchanges=exchanges,
                         supported=supported,
                         version=app.config['VERSION'])


@app.route('/api/exchanges', methods=['GET'])
@require_auth
def api_get_exchanges():
    exchanges = ExchangeManager.get_all_exchanges()
    return jsonify({'success': True, 'data': exchanges})


@app.route('/api/exchanges', methods=['POST'])
@require_auth
def api_add_exchange():
    data = request.get_json()

    required = ['name', 'display_name', 'api_key', 'api_secret']
    for field in required:
        if not data.get(field):
            return jsonify({'success': False, 'error': f'Missing field: {field}'}), 400

    name = data['name'].lower()
    if name not in app.config['SUPPORTED_EXCHANGES']:
        return jsonify({'success': False, 'error': f'Unsupported exchange: {name}'}), 400

    try:
        exchange = ExchangeManager.add_exchange(
            name=name,
            display_name=data['display_name'],
            api_key=data['api_key'],
            api_secret=data['api_secret'],
            passphrase=data.get('passphrase'),
            is_demo=data.get('is_demo', True)
        )
        return jsonify({'success': True, 'data': exchange.to_dict()}), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/exchanges/<int:exchange_id>', methods=['GET'])
@require_auth
def api_get_exchange(exchange_id):
    exchange = ExchangeManager.get_exchange(exchange_id)
    if exchange:
        return jsonify({'success': True, 'data': exchange})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


@app.route('/api/exchanges/<int:exchange_id>', methods=['PUT'])
@require_auth
def api_update_exchange(exchange_id):
    data = request.get_json()
    result = ExchangeManager.update_exchange(exchange_id, **data)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
@require_auth
def api_delete_exchange(exchange_id):
    if ExchangeManager.delete_exchange(exchange_id):
        return jsonify({'success': True, 'message': 'Exchange deleted'})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


@app.route('/api/exchanges/<int:exchange_id>/toggle', methods=['POST'])
@require_auth
def api_toggle_exchange(exchange_id):
    result = ExchangeManager.toggle_active(exchange_id)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': 'Exchange not found'}), 404


# ═══════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════

@app.route('/api/positions', methods=['GET'])
@require_auth
def api_get_positions():
    positions = Position.query.filter_by(status='OPEN').all()
    data = [{
        'id': p.id,
        'symbol': p.symbol,
        'side': p.side,
        'entry_price': p.entry_price,
        'size': p.size,
        'leverage': p.leverage,
        'pnl': p.pnl,
        'exchange_id': p.exchange_id,
        'created_at': p.created_at.isoformat() if p.created_at else None
    } for p in positions]
    return jsonify({'success': True, 'data': data})


@app.route('/api/positions/<int:position_id>/close', methods=['POST'])
@require_auth
def api_close_position(position_id):
    position = Position.query.get(position_id)
    if not position:
        return jsonify({'success': False, 'error': 'Position not found'}), 404

    result = engine.manual_close_position(
        position.exchange_id,
        position.symbol,
        position.side
    )

    if result.get('success'):
        position.status = 'CLOSED'
        position.closed_at = datetime.utcnow()
        db.session.commit()

    return jsonify(result)


# ═══════════════════════════════════════════
# BOT CONTROL
# ═══════════════════════════════════════════

@app.route('/api/bot/status', methods=['GET'])
@require_auth
def api_bot_status():
    exchanges = ExchangeManager.get_all_exchanges()
    demo_only = all(ex.get('is_demo', True) for ex in exchanges) if exchanges else True
    engine_status = engine.get_status()

    return jsonify({
        'success': True,
        'data': {
            'version': app.config['VERSION'],
            'edition': app.config['EDITION'],
            'status': 'RUNNING' if engine.running else 'STOPPED',
            'mode': 'DEMO' if demo_only else 'MIXED',
            'exchanges_count': len(exchanges),
            'active_exchanges': len([ex for ex in exchanges if ex.get('is_active')]),
            'engine': engine_status,
            'timestamp': datetime.utcnow().isoformat()
        }
    })


@app.route('/api/bot/start', methods=['POST'])
@require_auth
def api_bot_start():
    engine.start()
    return jsonify({'success': True, 'message': 'Engine started'})


@app.route('/api/bot/stop', methods=['POST'])
@require_auth
def api_bot_stop():
    engine.stop()
    return jsonify({'success': True, 'message': 'Engine stopped'})


@app.route('/api/bot/settings', methods=['GET'])
@require_auth
def api_get_settings():
    settings = BotSettings.query.all()
    return jsonify({'success': True, 'data': {s.key: s.value for s in settings}})


# ═══════════════════════════════════════════
# SENTIMENT & ML
# ═══════════════════════════════════════════

@app.route('/api/sentiment', methods=['GET'])
@require_auth
def api_get_sentiment():
    symbol = request.args.get('symbol', 'BTC')
    data = sentiment_analyzer.analyze(symbol)
    return jsonify({'success': True, 'data': data})


@app.route('/api/ml/filter', methods=['POST'])
@require_auth
def api_ml_filter():
    data = request.get_json()
    candles = data.get('candles', [])
    signal = data.get('signal', '')

    result = ml_filter.filter_signal(candles, signal)
    return jsonify({'success': True, 'data': result})


# ═══════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return "<h1>404 - Page Not Found</h1><a href='/'>Go Home</a>", 404


@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
    return "<h1>500 - Internal Server Error</h1><a href='/'>Go Home</a>", 500


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
