"""Opt-in isolated MariaDB settings; never inherit production DB credentials."""

import os
from django.core.exceptions import ImproperlyConfigured

from shop_test_settings import *  # noqa: F403

_name = os.environ.get("MINIKANDOO_TEST_DATABASE", "")
_password = os.environ.get("MINIKANDOO_TEST_DATABASE_PASSWORD", "")
_host = os.environ.get("MINIKANDOO_TEST_DATABASE_HOST", "127.0.0.1")
if not _name.startswith("minikandoo_qa_") or not _password or _host != "127.0.0.1":
    raise ImproperlyConfigured("An explicitly isolated minikandoo_qa_ database on loopback is required.")

DATABASES = {"default": {
    "ENGINE": "django.db.backends.mysql", "NAME": _name,
    "USER": "root", "PASSWORD": _password, "HOST": _host,
    "PORT": int(os.environ["MINIKANDOO_TEST_DATABASE_PORT"]), "CONN_MAX_AGE": 0,
    "OPTIONS": {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"},
    "TEST": {"NAME": _name + "_tests", "CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
}}
