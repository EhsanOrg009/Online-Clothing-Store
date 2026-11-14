"""Isolated browser QA settings; this module never uses the shop database."""
from config.settings.development import *
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': '/tmp/shop-browser.sqlite3'}}
MEDIA_ROOT = '/tmp/shop-qa-media'
STATIC_ROOT = '/tmp/shop-qa-static'
ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']
SMS_PROVIDER = 'memory'
PAYMENT_GATEWAY = 'zibal'
ZIBAL_MERCHANT = 'zibal'
SITE_URL = 'http://127.0.0.1:8765'
LOG_LEVEL = 'WARNING'
FREE_SHIPPING_MIN_AMOUNT = 2000000
SECRET_KEY = 'temporary-isolated-browser-test-only-secret-never-use-in-production-2026'
ROOT_URLCONF = 'shop_qa_urls'
