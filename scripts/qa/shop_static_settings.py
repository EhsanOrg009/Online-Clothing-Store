"""Exercise the production static storage against isolated QA paths."""
from shop_test_settings import *

DEBUG = False
STATIC_ROOT = "/tmp/shop-qa-manifest-static"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
