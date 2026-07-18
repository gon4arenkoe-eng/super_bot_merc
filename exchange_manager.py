"""SUPERBOT v5.5.36 - Exchange Manager"""
from models import db, Exchange
from crypto_utils import encrypt_value, decrypt_value


# Whitelist of allowed fields for mass-assignment
ALLOWED_UPDATE_FIELDS = {'display_name', 'is_demo', 'is_active'}


class ExchangeManager:
    """Manage exchange connections and credentials"""

    @staticmethod
    def add_exchange(name: str, display_name: str, api_key: str, api_secret: str,
                     passphrase: str = None, is_demo: bool = True):
        """Add new exchange with encrypted credentials"""
        exchange = Exchange(
            name=name.lower(),
            display_name=display_name,
            api_key_encrypted=encrypt_value(api_key),
            api_secret_encrypted=encrypt_value(api_secret),
            passphrase_encrypted=encrypt_value(passphrase) if passphrase else None,
            is_demo=is_demo,
            is_active=True
        )
        db.session.add(exchange)
        db.session.commit()
        return exchange

    @staticmethod
    def get_all_exchanges(include_secrets=False):
        """Get all exchanges"""
        exchanges = Exchange.query.all()
        return [ex.to_dict(include_secrets=include_secrets) for ex in exchanges]

    @staticmethod
    def get_exchange(exchange_id: int, include_secrets=False):
        """Get single exchange by ID"""
        exchange = Exchange.query.get(exchange_id)
        if exchange:
            return exchange.to_dict(include_secrets=include_secrets)
        return None

    @staticmethod
    def update_exchange(exchange_id: int, **kwargs):
        """Update exchange fields — WHITELIST ONLY"""
        exchange = Exchange.query.get(exchange_id)
        if not exchange:
            return None

        # Handle sensitive fields with explicit encryption
        if 'api_key' in kwargs:
            exchange.api_key_encrypted = encrypt_value(kwargs.pop('api_key'))
        if 'api_secret' in kwargs:
            exchange.api_secret_encrypted = encrypt_value(kwargs.pop('api_secret'))
        if 'passphrase' in kwargs:
            val = kwargs.pop('passphrase')
            exchange.passphrase_encrypted = encrypt_value(val) if val else None

        # Only allow whitelisted fields for remaining kwargs
        for key, value in kwargs.items():
            if key in ALLOWED_UPDATE_FIELDS and hasattr(exchange, key):
                setattr(exchange, key, value)

        db.session.commit()
        return exchange.to_dict()

    @staticmethod
    def delete_exchange(exchange_id: int):
        """Delete exchange"""
        exchange = Exchange.query.get(exchange_id)
        if exchange:
            db.session.delete(exchange)
            db.session.commit()
            return True
        return False

    @staticmethod
    def toggle_active(exchange_id: int):
        """Toggle exchange active status"""
        exchange = Exchange.query.get(exchange_id)
        if exchange:
            exchange.is_active = not exchange.is_active
            db.session.commit()
            return exchange.to_dict()
        return None

    @staticmethod
    def get_decrypted_credentials(exchange_id: int):
        """Get decrypted credentials for API calls"""
        exchange = Exchange.query.get(exchange_id)
        if not exchange:
            return None
        return {
            'name': exchange.name,
            'api_key': decrypt_value(exchange.api_key_encrypted),
            'api_secret': decrypt_value(exchange.api_secret_encrypted),
            'passphrase': decrypt_value(exchange.passphrase_encrypted) if exchange.passphrase_encrypted else None,
            'is_demo': exchange.is_demo
        }
