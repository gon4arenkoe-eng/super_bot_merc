"""SUPERBOT v5.5.36 - Database Models"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Exchange(db.Model):
    """Exchange API credentials storage"""
    __tablename__ = 'exchanges'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # bingx, binance, etc.
    display_name = db.Column(db.String(100), nullable=False)
    api_key_encrypted = db.Column(db.Text, nullable=False)
    api_secret_encrypted = db.Column(db.Text, nullable=False)
    passphrase_encrypted = db.Column(db.Text, nullable=True)  # For OKX
    is_demo = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    @staticmethod
    def _mask_string(s: str, visible_chars: int = 4) -> str:
        """Mask string showing only last N chars"""
        if len(s) <= visible_chars * 2:
            return '*' * len(s)
        return s[:visible_chars] + '***' + s[-visible_chars:]


class BotSettings(db.Model):
    """Bot configuration settings"""
    __tablename__ = 'bot_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Position(db.Model):
    """Trading positions"""
    __tablename__ = 'positions'

    id = db.Column(db.Integer, primary_key=True)
    exchange_id = db.Column(db.Integer, db.ForeignKey('exchanges.id'), nullable=False)
    symbol = db.Column(db.String(50), nullable=False)
    side = db.Column(db.String(10), nullable=False)  # LONG / SHORT
    entry_price = db.Column(db.Float, nullable=False)
    size = db.Column(db.Float, nullable=False)
    leverage = db.Column(db.Integer, default=5)
    pnl = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='OPEN')  # OPEN, CLOSED
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
