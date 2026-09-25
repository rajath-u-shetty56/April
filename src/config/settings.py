import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [
    host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "analytics_platform.catalog",
    "analytics_platform.event_catalog",
    "analytics_platform.events",
    "analytics_platform.group_analytics",
    "analytics_platform.ingestion",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise ImproperlyConfigured("DATABASE_URL is required")
DATABASES = {"default": dj_database_url.parse(database_url, conn_max_age=60)}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ]
}

# Ingestion limits are bytes of UTF-8 JSON and a count of batch items.
INGESTION_MAX_REQUEST_BYTES = int(os.environ.get("INGESTION_MAX_REQUEST_BYTES", 1048576))
INGESTION_MAX_PROPERTY_BYTES = int(os.environ.get("INGESTION_MAX_PROPERTY_BYTES", 65536))
INGESTION_MAX_BATCH_EVENTS = int(os.environ.get("INGESTION_MAX_BATCH_EVENTS", 500))
INGESTION_MAX_GROUPS = int(os.environ.get("INGESTION_MAX_GROUPS", 5))

# Rejection records are already JSON and contain a log-derived counter sample.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"ingestion_console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "analytics.ingestion": {
            "handlers": ["ingestion_console"],
            "level": "INFO",
            "propagate": True,
        },
    },
}
