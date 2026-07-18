"""SUPERBOT v5.5.36 - Grid Trading Strategy"""
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class GridStrategy:
    """DCA Grid Strategy with Martingale"""

    def __init__(self, grid_levels: int = 5, grid_step: float = 2.0, 
                 martingale: float = 30.0):
        self.grid_levels = grid_levels
        self.grid_step = grid_step / 100
        self.martingale = martingale / 100

    def calculate_grid(self, entry_price: float, side: str) -> List[Dict]:
        """Calculate grid levels"""
        grid = []

        for i in range(self.grid_levels):
            if side == 'LONG':
                price = entry_price * (1 - self.grid_step * (i + 1))
            else:
                price = entry_price * (1 + self.grid_step * (i + 1))

            size_multiplier = (1 + self.martingale) ** i

            grid.append({
                'level': i + 1,
                'price': round(price, 4),
                'size_multiplier': round(size_multiplier, 2),
                'triggered': False
            })

        return grid

    def check_grid_trigger(self, current_price: float, grid: List[Dict], side: str) -> Optional[Dict]:
        """Check if any grid level should be triggered"""
        for level in grid:
            if level['triggered']:
                continue

            if side == 'LONG' and current_price <= level['price']:
                level['triggered'] = True
                return level
            elif side == 'SHORT' and current_price >= level['price']:
                level['triggered'] = True
                return level

        return None

    def get_params(self) -> Dict:
        """Get strategy parameters"""
        return {
            'name': 'DCA Grid',
            'grid_levels': self.grid_levels,
            'grid_step': self.grid_step * 100,
            'martingale': self.martingale * 100
        }
