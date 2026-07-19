"""SUPERBOT v5.5.39 - Pure parsing functions for all exchanges (FIXED)"""
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


def parse_balance_bingx(data: Dict) -> float:
    """Extract USDT balance from BingX response. Pure function."""
    try:
        if 'data' in data and 'balance' in data['data']:
            return float(data['data']['balance']['balance'])
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_balance_binance(data: List[Dict]) -> float:
    """Extract USDT balance from Binance response. Pure function."""
    try:
        for bal in data:
            if bal.get('asset') == 'USDT':
                return float(bal.get('balance', 0))
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_balance_bybit(data: Dict) -> float:
    """Extract USDT balance from Bybit response. Pure function."""
    try:
        if data.get('retCode') == 0:
            for acct in data.get('result', {}).get('list', []):
                for coin in acct.get('coin', []):
                    if coin.get('coin') == 'USDT':
                        return float(coin.get('walletBalance', 0))
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_balance_okx(data: Dict) -> float:
    """Extract USDT balance from OKX response. Pure function."""
    try:
        if data.get('code') == '0':
            for bal_data in data.get('data', []):
                for detail in bal_data.get('details', []):
                    if detail.get('ccy') == 'USDT':
                        return float(detail.get('eq', 0))
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_position_bingx(pos: Dict) -> Dict:
    """Parse single position from BingX. Pure function."""
    try:
        return {
            'symbol': pos.get('symbol') or pos.get('instId') or 'UNKNOWN',
            'side': 'LONG' if str(pos.get('positionSide', '')).upper() == 'LONG' else 'SHORT',
            'size': abs(float(pos.get('positionAmt', 0))),
            'entry_price': float(pos.get('avgPrice', 0)),
            'pnl': float(pos.get('unRealizedProfit', 0)),
            'leverage': int(float(pos.get('leverage', 5))),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_position_binance(pos: Dict) -> Dict:
    """Parse single position from Binance. Pure function."""
    try:
        amt = float(pos.get('positionAmt', 0))
        if amt == 0:
            return None
        return {
            'symbol': pos.get('symbol', 'UNKNOWN'),
            'side': 'LONG' if amt > 0 else 'SHORT',
            'size': abs(amt),
            'entry_price': float(pos.get('entryPrice', 0)),
            'pnl': float(pos.get('unRealizedProfit', 0)),
            'leverage': int(pos.get('leverage', 5)),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_position_bybit(pos: Dict) -> Dict:
    """Parse single position from Bybit. Pure function."""
    try:
        size = float(pos.get('size', 0))
        if size == 0:
            return None
        return {
            'symbol': pos.get('symbol', 'UNKNOWN'),
            'side': str(pos.get('side', 'LONG')).upper(),
            'size': size,
            'entry_price': float(pos.get('avgPrice', 0)),
            'pnl': float(pos.get('unrealisedPnl', 0)),
            'leverage': int(pos.get('leverage', 5)),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_position_okx(pos: Dict) -> Dict:
    """Parse single position from OKX. Pure function."""
    try:
        pos_size = float(pos.get('pos', 0))
        if pos_size == 0:
            return None
        return {
            'symbol': pos.get('instId', 'UNKNOWN'),
            'side': 'LONG' if pos.get('posSide') == 'long' else 'SHORT',
            'size': abs(pos_size),
            'entry_price': float(pos.get('avgPx', 0)),
            'pnl': float(pos.get('upl', 0)),
            'leverage': int(pos.get('lever', 5)),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_klines_bingx(data) -> List[Dict]:
    """Parse klines from BingX. Pure function. FIXED: type safety."""
    candles = []

    # FIX: Check if data is a dict
    if not isinstance(data, dict):
        logger.warning(f"parse_klines_bingx: expected dict, got {type(data).__name__}: {data}")
        return candles

    if 'data' not in data:
        logger.warning(f"parse_klines_bingx: no 'data' key in response: {data}")
        return candles

    try:
        for k in data['data']:
            candles.append({
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            })
    except (IndexError, TypeError, ValueError) as e:
        logger.warning(f"parse_klines_bingx: parse error: {e}")
        pass
    return candles


def parse_all_positions(data, exchange_name: str) -> List[Dict]:
    """Parse all positions from exchange response. Pure function."""
    raw_positions = []

    if exchange_name == 'bingx':
        raw_positions = data.get('data', []) if isinstance(data, dict) else []
        parser = parse_position_bingx
    elif exchange_name == 'binance':
        raw_positions = data if isinstance(data, list) else data.get('data', [])
        parser = parse_position_binance
    elif exchange_name == 'bybit':
        if isinstance(data, dict) and data.get('retCode') == 0:
            raw_positions = data.get('result', {}).get('list', [])
        parser = parse_position_bybit
    elif exchange_name == 'okx':
        if isinstance(data, dict) and data.get('code') == '0':
            raw_positions = data.get('data', [])
        parser = parse_position_okx
    else:
        return []

    result = []
    for pos in raw_positions:
        parsed = parser(pos)
        if parsed:
            result.append(parsed)
    return result


def parse_balance(data, exchange_name: str) -> float:
    """Universal balance parser. Pure function."""
    parsers = {
        'bingx': parse_balance_bingx,
        'binance': parse_balance_binance,
        'bybit': parse_balance_bybit,
        'okx': parse_balance_okx,
    }
    parser = parsers.get(exchange_name)
    if parser:
        return parser(data)
    return 0.0
