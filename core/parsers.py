"""SUPERBOT v5.6.0 - Pure parsing functions for all exchanges (FIXED v3)"""
from typing import Dict, List, Union
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# BALANCE PARSERS
# ═══════════════════════════════════════════════════════════════════════

def parse_balance_bingx(data: Dict) -> float:
    """Extract USDT balance from BingX response. Pure function."""
    try:
        if 'data' in data and 'balance' in data['data']:
            return float(data['data']['balance']['balance'])
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_balance_binance(data: Union[List[Dict], Dict]) -> float:
    """Extract USDT balance from Binance Futures response. Pure function.

    Binance /fapi/v2/balance returns list of dicts with keys:
    - 'asset': str
    - 'walletBalance': str (total balance)
    - 'availableBalance': str (free balance)
    """
    try:
        # Handle error response (dict with 'error' key)
        if isinstance(data, dict):
            if 'error' in data:
                logger.warning(f"Binance balance error: {data['error']}")
                return 0.0
            # Some endpoints return {'data': [...]}
            balances = data.get('data', [])
        else:
            balances = data

        if not isinstance(balances, list):
            return 0.0

        for bal in balances:
            if bal.get('asset') == 'USDT':
                # FIX: Use 'walletBalance' (total) instead of non-existent 'balance'
                wallet_balance = bal.get('walletBalance')
                if wallet_balance is not None:
                    return float(wallet_balance)
                # Fallback to availableBalance
                avail = bal.get('availableBalance')
                if avail is not None:
                    return float(avail)
    except (KeyError, TypeError, ValueError) as e:
        logger.debug(f"Binance balance parse error: {e}")
    return 0.0


def parse_balance_bybit(data: Dict) -> float:
    """Extract USDT balance from Bybit v5 response. Pure function."""
    try:
        if data.get('retCode') == 0:
            for acct in data.get('result', {}).get('list', []):
                for coin in acct.get('coin', []):
                    if coin.get('coin') == 'USDT':
                        # Try walletBalance first, then totalEquity
                        wb = coin.get('walletBalance')
                        if wb is not None:
                            return float(wb)
                        te = coin.get('totalEquity')
                        if te is not None:
                            return float(te)
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


def parse_balance_okx(data: Dict) -> float:
    """Extract USDT balance from OKX response. Pure function."""
    try:
        if data.get('code') == '0':
            for bal_data in data.get('data', []):
                # OKX v5: details is a list of coin balances
                details = bal_data.get('details', [])
                for detail in details:
                    if detail.get('ccy') == 'USDT':
                        # 'eq' = equity (total), 'availEq' = available
                        eq = detail.get('eq')
                        if eq is not None:
                            return float(eq)
    except (KeyError, TypeError, ValueError):
        pass
    return 0.0


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


# ═══════════════════════════════════════════════════════════════════════
# POSITION PARSERS
# ═══════════════════════════════════════════════════════════════════════

def parse_position_bingx(pos: Dict) -> Dict:
    """Parse single position from BingX. Pure function."""
    try:
        # FIX: fallback avgPrice -> entryPrice -> avgCostPrice
        entry_price_raw = pos.get('avgPrice') or pos.get('entryPrice') or pos.get('avgCostPrice', 0)
        return {
            'symbol': pos.get('symbol') or pos.get('instId') or 'UNKNOWN',
            'side': 'LONG' if str(pos.get('positionSide', '')).upper() == 'LONG' else 'SHORT',
            'size': abs(float(pos.get('positionAmt', 0))),
            'entry_price': float(entry_price_raw) if entry_price_raw else 0.0,
            'pnl': float(pos.get('unRealizedProfit', 0)),
            'leverage': int(float(pos.get('leverage', 5))),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_position_binance(pos: Dict) -> Dict:
    """Parse single position from Binance Futures. Pure function."""
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
    """Parse single position from Bybit v5. Pure function."""
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
            'leverage': int(float(pos.get('leverage', 5))),
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
            'leverage': int(float(pos.get('lever', 5))),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_all_positions(data, exchange_name: str) -> List[Dict]:
    """Parse all positions from exchange response. Pure function."""
    raw_positions = []

    # Handle error responses first
    if isinstance(data, dict) and 'error' in data:
        logger.warning(f"parse_all_positions error for {exchange_name}: {data['error']}")
        return []

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


# ═══════════════════════════════════════════════════════════════════════
# KLINE PARSERS — ALL 4 EXCHANGES
# ═══════════════════════════════════════════════════════════════════════

def parse_klines_bingx(data) -> List[Dict]:
    """Parse klines from BingX. Pure function."""
    candles = []
    if not isinstance(data, dict):
        logger.warning(f"parse_klines_bingx: expected dict, got {type(data).__name__}")
        return candles
    if 'error' in data:
        logger.warning(f"parse_klines_bingx: API error: {data['error']}")
        return candles
    code = data.get('code')
    if code is not None and code != 0:
        logger.warning(f"parse_klines_bingx: BingX error code={code}, msg={data.get('msg')}")
        return candles
    if 'data' not in data:
        return candles

    raw_data = data['data']
    if raw_data is None:
        return candles
    if isinstance(raw_data, dict):
        raw_data = list(raw_data.values())
    if not isinstance(raw_data, list):
        return candles

    for k in raw_data:
        try:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            elif isinstance(k, dict):
                candles.append({
                    'timestamp': int(k.get('time', k.get('t', 0))),
                    'open': float(k.get('open', k.get('o', 0))),
                    'high': float(k.get('high', k.get('h', 0))),
                    'low': float(k.get('low', k.get('l', 0))),
                    'close': float(k.get('close', k.get('c', 0))),
                    'volume': float(k.get('volume', k.get('v', 0)))
                })
        except (IndexError, TypeError, ValueError, KeyError) as e:
            logger.debug(f"parse_klines_bingx: skip invalid candle {k}: {e}")
            continue
    return candles


def parse_klines_binance(data: Union[List, Dict]) -> List[Dict]:
    """Parse klines from Binance Futures. Pure function.

    Binance returns: [[timestamp, open, high, low, close, volume, ...], ...]
    """
    candles = []
    if isinstance(data, dict):
        if 'error' in data:
            logger.warning(f"parse_klines_binance: API error: {data['error']}")
            return candles
        raw_data = data.get('data', [])
    else:
        raw_data = data

    if not isinstance(raw_data, list):
        return candles

    for k in raw_data:
        try:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
        except (IndexError, TypeError, ValueError) as e:
            logger.debug(f"parse_klines_binance: skip invalid candle: {e}")
            continue
    return candles


def parse_klines_bybit(data: Dict) -> List[Dict]:
    """Parse klines from Bybit v5. Pure function.

    Bybit returns: {'retCode': 0, 'result': {'list': [[...], ...], 'category': 'linear'}}
    Each candle: [timestamp, open, high, low, close, volume, turnover]
    """
    candles = []
    if not isinstance(data, dict):
        return candles
    if 'error' in data:
        logger.warning(f"parse_klines_bybit: API error: {data['error']}")
        return candles
    if data.get('retCode') != 0:
        return candles

    raw_data = data.get('result', {}).get('list', [])
    if not isinstance(raw_data, list):
        return candles

    for k in raw_data:
        try:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
        except (IndexError, TypeError, ValueError) as e:
            logger.debug(f"parse_klines_bybit: skip invalid candle: {e}")
            continue
    return candles


def parse_klines_okx(data: Dict) -> List[Dict]:
    """Parse klines from OKX. Pure function.

    OKX returns: {'code': '0', 'data': [[timestamp, open, high, low, close, vol, volCcy, volCcyQuote], ...]}
    Note: data is ordered ASC by timestamp (oldest first)
    """
    candles = []
    if not isinstance(data, dict):
        return candles
    if 'error' in data:
        logger.warning(f"parse_klines_okx: API error: {data['error']}")
        return candles
    if data.get('code') != '0':
        return candles

    raw_data = data.get('data', [])
    if not isinstance(raw_data, list):
        return candles

    for k in raw_data:
        try:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
        except (IndexError, TypeError, ValueError) as e:
            logger.debug(f"parse_klines_okx: skip invalid candle: {e}")
            continue
    return candles


def parse_klines(data, exchange_name: str) -> List[Dict]:
    """Universal kline parser — routes to correct exchange parser."""
    parsers = {
        'bingx': parse_klines_bingx,
        'binance': parse_klines_binance,
        'bybit': parse_klines_bybit,
        'okx': parse_klines_okx,
    }
    parser = parsers.get(exchange_name)
    if parser:
        return parser(data)
    return []
