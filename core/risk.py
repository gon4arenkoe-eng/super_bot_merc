"""SUPERBOT v5.5.36 - Risk Management Engine"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """Risk configuration"""
    max_leverage: int = 5
    max_positions: int = 5
    max_daily_loss_pct: float = 5.0
    max_position_size_pct: float = 10.0
    dca_orders: int = 5
    dca_step_pct: float = 2.0
    martingale_pct: float = 30.0
    breakeven_pct: float = 1.0
    sl_pct: float = 3.0
    tp1_pct: float = 2.0
    tp2_pct: float = 4.0
    tp3_pct: float = 6.0


class RiskManager:
    """Manages trading risk and position protection"""

    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()
        self.daily_pnl = 0.0
        self.positions_count = 0
        self._last_reset_date = datetime.now().date()

    def _check_and_reset_daily_pnl(self):
        """Сбросить daily_pnl если наступил новый день"""
        today = datetime.now().date()
        if today != self._last_reset_date:
            logger.info(f"New day: {self._last_reset_date} → {today}. Resetting daily PnL.")
            self.daily_pnl = 0.0
            self._last_reset_date = today

    def can_open_position(self, balance: float, position_size: float, 
                         current_positions: int) -> Tuple[bool, str]:
        """Check if new position can be opened"""
        self._check_and_reset_daily_pnl()
        
        if current_positions >= self.config.max_positions:
            return False, f"Max positions reached ({self.config.max_positions})"

        max_size = balance * (self.config.max_position_size_pct / 100)
        if position_size > max_size:
            return False, f"Position size {position_size:.2f} exceeds max {max_size:.2f}"

        if self.daily_pnl <= -balance * (self.config.max_daily_loss_pct / 100):
            return False, "Daily loss limit reached"

        return True, "OK"

    def calculate_dca_levels(self, entry_price: float, side: str) -> List[Dict]:
        """Calculate DCA grid levels"""
        levels = []
        step = self.config.dca_step_pct / 100

        for i in range(self.config.dca_orders):
            multiplier = 1 + (self.config.martingale_pct / 100)
            size_multiplier = multiplier ** i

            if side == 'LONG':
                price = entry_price * (1 - step * (i + 1))
            else:
                price = entry_price * (1 + step * (i + 1))

            levels.append({
                'level': i + 1,
                'price': round(price, 4),
                'size_multiplier': round(size_multiplier, 2)
            })

        return levels

    def calculate_sl_tp(self, entry_price: float, side: str) -> Dict:
        """Calculate Stop Loss and Take Profit levels"""
        sl_pct = self.config.sl_pct / 100

        if side == 'LONG':
            sl = entry_price * (1 - sl_pct)
            tp1 = entry_price * (1 + self.config.tp1_pct / 100)
            tp2 = entry_price * (1 + self.config.tp2_pct / 100)
            tp3 = entry_price * (1 + self.config.tp3_pct / 100)
        else:
            sl = entry_price * (1 + sl_pct)
            tp1 = entry_price * (1 - self.config.tp1_pct / 100)
            tp2 = entry_price * (1 - self.config.tp2_pct / 100)
            tp3 = entry_price * (1 - self.config.tp3_pct / 100)

        return {
            'stop_loss': round(sl, 4),
            'take_profit_1': round(tp1, 4),
            'take_profit_2': round(tp2, 4),
            'take_profit_3': round(tp3, 4),
            'breakeven': round(entry_price * (1 + self.config.breakeven_pct / 100) if side == 'LONG' 
                              else entry_price * (1 - self.config.breakeven_pct / 100), 4)
        }

    def update_daily_pnl(self, pnl: float):
        """Update daily PnL tracking"""
        self._check_and_reset_daily_pnl()
        self.daily_pnl += pnl
        logger.info(f"Daily PnL updated: {self.daily_pnl:.2f} USDT")

    def reset_daily_pnl(self):
        """Reset daily PnL (call at midnight)"""
        self.daily_pnl = 0.0
        self._last_reset_date = datetime.now().date()
        logger.info("Daily PnL reset")

    def validate_leverage(self, leverage: int) -> int:
        """Validate and cap leverage"""
        if leverage > self.config.max_leverage:
            logger.warning(f"Leverage {leverage} capped to {self.config.max_leverage}")
            return self.config.max_leverage
        return max(1, leverage)
