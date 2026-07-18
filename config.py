"""SUPERBOT v5.5.36 - Configuration"""
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration"""
    # FIX: Use env var or generate once and reuse (not os.urandom on every import)
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        # For local dev only — generate once and warn
        import secrets
        SECRET_KEY = secrets.token_hex(32)
        print("WARNING: SECRET_KEY not set, using generated key. Set SECRET_KEY env var for production!")

    AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD')

    database_url = os.environ.get('DATABASE_URL', '')
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MASTER_KEY = os.environ.get('MASTER_KEY')

    DEMO_MODE = True
    VERSION = "5.5.36"
    EDITION = "Mercedes"

    SUPPORTED_EXCHANGES = ['bingx', 'binance', 'bybit', 'okx']

    DEFAULT_LEVERAGE = 5
    DEFAULT_DCA_ORDERS = 5
    DEFAULT_DCA_STEP = 2.0
    DEFAULT_MARTINGALE = 30.0
    DEFAULT_BREAKEVEN = 1.0

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': ProductionConfig
}
