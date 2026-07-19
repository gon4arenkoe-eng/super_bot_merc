"""SUPERBOT v5.5.37 - Trading Engine (Idempotent Orders)"""
import os
import time
import logging
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from models import db, Position, SentOrder
from exchange_manager import ExchangeManager
from risk import RiskManager, RiskConfig
from ema_cross import EMAStrategy
from grid import GridStrategy
from bingx import BingXClient

logger = logging.getLogger(__name__)


class TradingEngine:
    """Main trading engine — runs strategies and manages positions"""

    def __init__(self, app=None):
        self.app = app
        self.running = False
        self.thread = None
        self.risk_manager = RiskManager()
        self.ema_strategy = EMAStrategy()
        self.grid_strategy = GridStrategy()
        self.clients = {}
        self.positions_cache = {}
        self._last_pnl_reset = datetime.utcnow().date()
        logger.info("Trading Engine initialized")

    def _get_client(self, exchange_id: int):
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
    # IDEMPOTENT ORDER ID GENERATION
    # ═══════════════════════════════════════════════════════════════════

    def _generate_client_order_id(self, exchange_id: int, symbol: str,
                                   side: str, price: float, 
                                   timestamp_minute: int = None) -> str:
        """
        Generate deterministic client_order_id for idempotency.

        Format: "{exchange_id}_{symbol}_{side}_{minute_timestamp_hash}"

        Same signal in the same minute = same ID = no duplicate.
        Different minute = different ID = new order allowed.
        """
        if timestamp_minute is None:
            timestamp_minute = int(datetime.utcnow().timestamp() / 60)

        raw = f"{exchange_id}_{symbol}_{side}_{price:.2f}_{timestamp_minute}"
        hash_suffix = hashlib.md5(raw.encode()).hexdigest()[:8]

        return f"SB_{exchange_id}_{symbol}_{side}_{hash_suffix}"

    def _is_order_already_sent(self, client_order_id: str) -> bool:
        """Check if this exact order was already sent"""
        existing = SentOrder.query.filter_by(
            client_order_id=client_order_id
        ).first()
        return existing is not None

    def _record_sent_order(self, client_order_id: str, exchange_id: int,
                           symbol: str, side: str, quantity: float,
                           price: Optional[float], order_type: str,
                           response: Dict = None):
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
                with self.app.app_context():
                    self._check_daily_reset()
                    self._process_exchanges()
            except Exception as e:
                logger.error(f"Engine loop error: {e}")

            time.sleep(60)

    def _check_daily_reset(self):
        """FIX: Reset daily PnL at midnight"""
        today = datetime.utcnow().date()
        if today != self._last_pnl_reset:
            self.risk_manager.reset_daily_pnl()
            self._last_pnl_reset = today
            logger.info("Daily PnL reset at midnight")

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
                balance_data = client.get_balance()
                balance = self._parse_balance(balance_data)

                positions_data = client.get_positions()
                self._sync_positions(exchange_id, positions_data)

                self._analyze_and_trade(exchange_id, client, balance)

            except Exception as e:
                logger.error(f"Error processing exchange {exchange_id}: {e}")

    def _parse_balance(self, data: Dict) -> float:
        try:
            if 'data' in data and 'balance' in data['data']:
                return float(data['data']['balance']['balance'])
        except:
            pass
        return 0.0

    def _sync_positions(self, exchange_id: int, data: Dict):
        """FIX: Sync positions without marking all as closed first"""
        try:
            api_positions = data.get('data', [])

            # Get currently open positions from DB for this exchange
            db_positions = {
                p.symbol: p for p in Position.query.filter_by(
                    exchange_id=exchange_id, status='OPEN'
                ).all()
            }

            # Track which API positions we've seen
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
                    # Update existing position
                    existing = db_positions[symbol]
                    existing.size = current_size
                    existing.pnl = current_pnl
                    existing.entry_price = float(pos.get('avgPrice', existing.entry_price))
                else:
                    # Create new position
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

            # Mark DB positions not in API response as closed
            for symbol, pos in db_positions.items():
                if symbol not in seen_symbols:
                    pos.status = 'CLOSED'
                    pos.closed_at = datetime.utcnow()
                    # Track realized PnL for daily limit
                    self.risk_manager.update_daily_pnl(pos.pnl)

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Position sync error: {e}")

    def _analyze_and_trade(self, exchange_id: int, client, balance: float):
        symbols = ['BTC-USDT', 'ETH-USDT']

        for symbol in symbols:
            try:
                klines_data = client.get_klines(symbol, interval='1h', limit=100)

                if 'data' not in klines_data:
                    continue

                candles = []
                for k in klines_data['data']:
                    candles.append({
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    })

                signal = self.ema_strategy.analyze(candles)

                if signal['signal'] != 'NEUTRAL' and signal['confidence'] > 60:
                    self._execute_signal(exchange_id, client, symbol, signal, balance)

            except Exception as e:
                logger.error(f"Analysis error for {symbol}: {e}")

    def _execute_signal(self, exchange_id: int, client, symbol: str,
                       signal: Dict, balance: float):
        side = signal['signal']

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

        current_price = signal['price']
        sl_tp = self.risk_manager.calculate_sl_tp(current_price, side)
        leverage = self.risk_manager.validate_leverage(5)

        client.set_leverage(symbol, leverage)
        position_side = 'LONG' if side == 'LONG' else 'SHORT'
        order_side = 'BUY' if side == 'LONG' else 'SELL'
        quantity = round(position_size / current_price, 4)

        # ═══════════════════════════════════════════════════════════════
        # IDEMPOTENCY CHECK
        # ═══════════════════════════════════════════════════════════════
        client_order_id = self._generate_client_order_id(
            exchange_id, symbol, side, current_price
        )

        if self._is_order_already_sent(client_order_id):
            logger.warning(
                f"IDEMPOTENCY BLOCK: Order {client_order_id} already sent. "
                f"Skipping duplicate for {symbol} {side}"
            )
            return

        logger.info(f"Placing {side} order for {symbol} at {current_price}")

        if client.demo:
            logger.info(f"[DEMO] Would place order: {symbol} {side} @ {current_price}")
            self._record_sent_order(
                client_order_id, exchange_id, symbol, side,
                quantity, current_price, 'MARKET',
                {'demo': True}
            )
            return

        # Send order
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

        # Record sent order (even if exchange returned error)
        self._record_sent_order(
            client_order_id, exchange_id, symbol, side,
            quantity, current_price, 'MARKET', result
        )

        logger.info(f"Order result: {result}")

    def get_status(self) -> Dict:
        return {
            'running': self.running,
            'risk_config': self.risk_manager.config.__dict__,
            'active_exchanges': len(self.clients),
            'daily_pnl': self.risk_manager.daily_pnl
        }

    def manual_close_position(self, exchange_id: int, symbol: str, position_side: str):
        client = self._get_client(exchange_id)
        if not client:
            return {'success': False, 'error': 'Client not found'}

        result = client.close_position(symbol, position_side)
        return {'success': True, 'data': result}
