"""SUPERBOT v5.5.39 - Pure parsing functions for all exchanges (FIXED v2)"""
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
    """
    Parse klines from BingX. Pure function. FIXED v2: handles dict/list/None.

    BingX API can return data['data'] as:
    - list: [[timestamp, open, high, low, close, volume], ...]
    - dict: {"0": [...], "1": [...]} (rare, but happens)
    - None: when no data
    """
    candles = []

    # Must be a dict
    if not isinstance(data, dict):
        logger.warning(f"parse_klines_bingx: expected dict, got {type(data).__name__}: {data}")
        return candles

    # Check for API error
    if 'error' in data:
        logger.warning(f"parse_klines_bingx: API error in response: {data['error']}")
        return candles

    # Check for BingX error code
    code = data.get('code')
    if code is not None and code != 0:
        logger.warning(f"parse_klines_bingx: BingX error code={code}, msg={data.get('msg')}")
        return candles

    if 'data' not in data:
        logger.warning(f"parse_klines_bingx: no 'data' key in response: {list(data.keys())}")
        return candles

    raw_data = data['data']

    # FIX: data['data'] can be None
    if raw_data is None:
        logger.info("parse_klines_bingx: data['data'] is None, no candles available")
        return candles

    # FIX: data['data'] can be dict instead of list
    if isinstance(raw_data, dict):
        logger.info(f"parse_klines_bingx: data['data'] is dict with {len(raw_data)} keys, converting to list")
        raw_data = list(raw_data.values())

    # Must be a list now
    if not isinstance(raw_data, list):
        logger.warning(f"parse_klines_bingx: data['data'] is {type(raw_data).__name__}, expected list")
        return candles

    for k in raw_data:
        try:
            # k can be list [timestamp, open, high, low, close, volume]
            # or dict {"open": ..., "high": ...}
            if isinstance(k, list):
                candles.append({
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            elif isinstance(k, dict):
                candles.append({
                    'open': float(k.get('open', k.get('o', 0))),
                    'high': float(k.get('high', k.get('h', 0))),
                    'low': float(k.get('low', k.get('l', 0))),
                    'close': float(k.get('close', k.get('c', 0))),
                    'volume': float(k.get('volume', k.get('v', 0)))
                })
        except (IndexError, TypeError, ValueError, KeyError) as e:
            logger.debug(f"parse_klines_bingx: skip invalid candle {k}: {e}")
            continue

    logger.info(f"parse_klines_bingx: parsed {len(candles)} candles")
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
