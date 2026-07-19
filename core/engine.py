"""SUPERBOT v5.5.38 - Trading Engine (Pure Functions + DI)"""
import os
import time
import logging
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable

from models import db, Position, SentOrder
from exchange_manager import ExchangeManager
from risk import RiskManager, RiskConfig
from bingx import BingXClient

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PURE FUNCTIONS (no side effects, testable in isolation)
# ═══════════════════════════════════════════════════════════════════════

def parse_balance(data: Dict) -> float:
    """Extract balance from API response. Pure function."""
    try:
        if 'data' in data and 'balance' in data['data']:
            return float(data['data']['balance']['balance'])
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def generate_client_order_id(exchange_id: int, symbol: str,
                              side: str, price: float,
                              timestamp_minute: int = None) -> str:
    """Generate deterministic order ID. Pure function, no DB access."""
    if timestamp_minute is None:
        timestamp_minute = int(datetime.utcnow().timestamp() / 60)

    raw = f"{exchange_id}_{symbol}_{side}_{price:.2f}_{timestamp_minute}"
    hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"SB_{exchange_id}_{symbol}_{side}_{hash_suffix}"


def calculate_position_size(balance: float, risk_pct: float = 0.02) -> float:
    """Calculate position size from balance. Pure function."""
    return balance * risk_pct


def prepare_order_params(symbol: str, side: str, position_side: str,
                         quantity: float, leverage: int,
                         stop_loss: float = None, 
                         take_profit: float = None) -> Dict:
    """Prepare order parameters. Pure function."""
    params = {
        'symbol': symbol,
        'side': side,
        'positionSide': position_side,
        'type': 'MARKET',
        'quantity': quantity,
        'leverage': leverage
    }
    if stop_loss:
        import json
        params['stopLoss'] = json.dumps({
            'stopPrice': stop_loss, 
            'type': 'STOP_MARKET'
        })
    if take_profit:
        import json
        params['takeProfit'] = json.dumps({
            'stopPrice': take_profit, 
            'type': 'TAKE_PROFIT_MARKET'
        })
    return params


def analyze_candles(klines_data: Dict) -> List[Dict]:
    """Convert raw klines to candle dicts. Pure function."""
    candles = []
    if 'data' not in klines_data:
        return candles

    for k in klines_data['data']:
        try:
            candles.append({
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            })
        except (IndexError, ValueError):
            continue
    return candles


def should_open_position(signal: Dict, min_confidence: int = 60) -> bool:
    """Check if signal is strong enough. Pure function."""
    return signal.get('signal') != 'NEUTRAL' and signal.get('confidence', 0) > min_confidence


# ═══════════════════════════════════════════════════════════════════════
# DATABASE OPERATIONS (isolated, testable with mocks)
# ═══════════════════════════════════════════════════════════════════════

def is_order_already_sent(client_order_id: str) -> bool:
    """Check database for existing order."""
    return SentOrder.query.filter_by(client_order_id=client_order_id).first() is not None


def record_sent_order(client_order_id: str, exchange_id: int,
                      symbol: str, side: str, quantity: float,
                      price: Optional[float], order_type: str,
                      response: Dict = None) -> bool:
    """Record order in database."""
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
        return True
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to record sent order: {e}")
        return False


def get_open_positions_count(exchange_id: int) -> int:
    """Get count of open positions."""
    return Position.query.filter_by(
        exchange_id=exchange_id, status='OPEN'
    ).count()


def sync_positions_to_db(exchange_id: int, api_positions: List[Dict],
                         risk_manager: RiskManager) -> None:
    """Sync API positions to database."""
    try:
        db_positions = {
            p.symbol: p for p in Position.query.filter_by(
                exchange_id=exchange_id, status='OPEN'
            ).all()
        }

        seen_symbols = set()

        for pos in api_positions:
            symbol = pos.get('symbol')
            if not symbol:
                continue

            seen_symbols.add(symbol)
            side = 'LONG' if pos.get('positionSide') == 'LONG' else 'SHORT'
            current_size = float(pos.get('positionAmt', 0))
            current_pnl = float(pos.get('unrealizedProfit', 0))

            if symbol in db_positions:
                existing = db_positions[symbol]
                existing.size = current_size
                existing.pnl = current_pnl
                existing.entry_price = float(pos.get('avgPrice', existing.entry_price))
            else:
                new_pos = Position(
                    exchange_id=exchange_id,
                    symbol=symbol,
                    side=side,
                    entry_price=float(pos.get('avgPrice', 0)),
                    size=current_size,
                    leverage=int(pos.get('leverage', 5)),
                    pnl=current_pnl,
                    status='OPEN'
                )
                db.session.add(new_pos)

        # Mark closed positions
        for symbol, pos in db_positions.items():
            if symbol not in seen_symbols:
                pos.status = 'CLOSED'
                pos.closed_at = datetime.utcnow()
                risk_manager.update_daily_pnl(pos.pnl)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Position sync error: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TRADING ENGINE (DI container, thin orchestration layer)
# ═══════════════════════════════════════════════════════════════════════

class TradingEngine:
    """
    Trading Engine — thin orchestration layer.

    All business logic is in pure functions above.
    This class only wires dependencies and manages lifecycle.
    """

    def __init__(self,
                 app=None,
                 risk_manager: RiskManager = None,
                 strategies: List = None,
                 symbols: List[str] = None,
                 position_size_fn: Callable = None,
                 order_id_fn: Callable = None,
                 app_context_provider=None):
        """
        Dependency Injection constructor.

        Args:
            app: Flask app (optional, for context)
            risk_manager: Risk manager instance (injected for testability)
            strategies: List of strategy instances
            symbols: List of trading pairs
            position_size_fn: Function to calculate position size
            order_id_fn: Function to generate order IDs
            app_context_provider: Function that yields app context
        """
        self.app = app
        self.risk_manager = risk_manager or RiskManager()
        self.strategies = strategies or []
        self.symbols = symbols or ['BTC-USDT', 'ETH-USDT']
        self.position_size_fn = position_size_fn or calculate_position_size
        self.order_id_fn = order_id_fn or generate_client_order_id
        self.app_context_provider = app_context_provider or self._default_context

        self.running = False
        self.thread = None
        self.clients = {}
        self._last_pnl_reset = datetime.utcnow().date()

        logger.info("Trading Engine initialized (DI mode)")

    def _default_context(self):
        """Default Flask app context provider."""
        if self.app:
            return self.app.app_context()
        # No-op context for testing
        class NoOpContext:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return NoOpContext()

    def _get_client(self, exchange_id: int):
        """Get or create exchange client."""
        if exchange_id in self.clients:
            return self.clients[exchange_id]

        creds = ExchangeManager.get_decrypted_credentials(exchange_id)
        if not creds:
            return None

        if creds['name'] == 'bingx':
            client = BingXClient(
                api_key=creds['api_key'],
                api_secret=creds['api_secret'],
                demo=creds['is_demo']
            )
            self.clients[exchange_id] = client
            return client

        return None

    # ═══════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════

    def start(self):
        if self.running:
            logger.warning("Engine already running")
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
                with self.app_context_provider():
                    self._check_daily_reset()
                    self._process_exchanges()
            except Exception as e:
                logger.error(f"Engine loop error: {e}")

            time.sleep(60)

    def _check_daily_reset(self):
        """Reset daily PnL at midnight."""
        today = datetime.utcnow().date()
        if today != self._last_pnl_reset:
            self.risk_manager.reset_daily_pnl()
            self._last_pnl_reset = today
            logger.info("Daily PnL reset at midnight")

    # ═══════════════════════════════════════════════════════════════════
    # ORCHESTRATION (thin, delegates to pure functions)
    # ═══════════════════════════════════════════════════════════════════

    def _process_exchanges(self):
        """Process all active exchanges."""
        exchanges = ExchangeManager.get_all_exchanges()

        for ex in exchanges:
            if not ex.get('is_active'):
                continue

            exchange_id = ex['id']
            client = self._get_client(exchange_id)
            if not client:
                continue

            try:
                # Pure: parse balance
                balance_data = client.get_balance()
                balance = parse_balance(balance_data)

                # Side effect: sync positions
                positions_data = client.get_positions()
                api_positions = positions_data.get('data', [])
                sync_positions_to_db(exchange_id, api_positions, self.risk_manager)

                # Pure: analyze and trade
                self._analyze_and_trade(exchange_id, client, balance)

            except Exception as e:
                logger.error(f"Error processing exchange {exchange_id}: {e}")

    def _analyze_and_trade(self, exchange_id: int, client, balance: float):
        """Analyze symbols and execute signals."""
        for symbol in self.symbols:
            try:
                klines_data = client.get_klines(symbol, interval='1h', limit=100)
                candles = analyze_candles(klines_data)

                if not candles:
                    continue

                # Run all strategies, pick best signal
                best_signal = {'signal': 'NEUTRAL', 'confidence': 0, 'price': 0}
                for strategy in self.strategies:
                    signal = strategy.analyze(candles)
                    if signal['confidence'] > best_signal['confidence']:
                        best_signal = signal

                if should_open_position(best_signal):
                    self._execute_trade(exchange_id, client, symbol, 
                                       best_signal, balance)

            except Exception as e:
                logger.error(f"Analysis error for {symbol}: {e}")

    def _execute_trade(self, exchange_id: int, client, symbol: str,
                      signal: Dict, balance: float):
        """Execute a single trade with idempotency check."""
        side = signal['signal']
        current_price = signal['price']

        # Pure: risk check
        current_positions = get_open_positions_count(exchange_id)
        position_size = self.position_size_fn(balance)
        can_trade, reason = self.risk_manager.can_open_position(
            balance, position_size, current_positions
        )

        if not can_trade:
            logger.info(f"Risk block: {reason}")
            return

        # Pure: calculate SL/TP and leverage
        sl_tp = self.risk_manager.calculate_sl_tp(current_price, side)
        leverage = self.risk_manager.validate_leverage(5)

        # Side effect: set leverage on exchange
        client.set_leverage(symbol, leverage)

        position_side = 'LONG' if side == 'LONG' else 'SHORT'
        order_side = 'BUY' if side == 'LONG' else 'SELL'
        quantity = round(position_size / current_price, 4)

        # Pure: generate order ID
        client_order_id = self.order_id_fn(
            exchange_id, symbol, side, current_price
        )

        # Side effect: idempotency check
        if is_order_already_sent(client_order_id):
            logger.warning(
                f"IDEMPOTENCY BLOCK: {client_order_id} already sent. "
                f"Skipping {symbol} {side}"
            )
            return

        logger.info(f"Placing {side} order for {symbol} at {current_price}")

        # DEMO mode
        if client.demo:
            logger.info(f"[DEMO] Would place: {symbol} {side} @ {current_price}")
            record_sent_order(
                client_order_id, exchange_id, symbol, side,
                quantity, current_price, 'MARKET', {'demo': True}
            )
            return

        # Side effect: send order
        result = client.place_order(
            symbol=symbol,
            side=order_side,
            position_side=position_side,
            order_type='MARKET',
            quantity=quantity,
            stop_loss=sl_tp['stop_loss'],
            take_profit=sl_tp['take_profit_1'],
            leverage=leverage
        )

        # Side effect: record sent order
        record_sent_order(
            client_order_id, exchange_id, symbol, side,
            quantity, current_price, 'MARKET', result
        )

        logger.info(f"Order result: {result}")

    def get_status(self) -> Dict:
        """Get engine status."""
        return {
            'running': self.running,
            'risk_config': self.risk_manager.config.__dict__,
            'active_exchanges': len(self.clients),
            'daily_pnl': self.risk_manager.daily_pnl,
            'strategies_count': len(self.strategies),
            'symbols': self.symbols
        }

    def manual_close_position(self, exchange_id: int, symbol: str, 
                             position_side: str):
        """Manually close a position."""
        client = self._get_client(exchange_id)
        if not client:
            return {'success': False, 'error': 'Client not found'}

        result = client.close_position(symbol, position_side)
        return {'success': True, 'data': result}
