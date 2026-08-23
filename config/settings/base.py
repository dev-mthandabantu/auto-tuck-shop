"""
Base Django settings for auto-tuck-shop project.
"""

import environ
from pathlib import Path
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Initialize environ
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

# Read .env file if it exists
environ.Env.read_env(BASE_DIR / ".env")

# Sentry Configuration
sentry_sdk.init(
    dsn=env("SENTRY_DSN", default=None),
    integrations=[DjangoIntegration()],
    # Set traces_sample_rate to 1.0 to capture 100% of transactions for performance monitoring.
    # Adjust this value in production to reduce volume
    traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
    # Set profiles_sample_rate to profile 10% of sampled transactions.
    profiles_sample_rate=env.float("SENTRY_PROFILES_SAMPLE_RATE", default=0.1),
    # Add data like request headers and IP for users
    send_default_pii=True,
    # Set environment
    environment=env("SENTRY_ENVIRONMENT", default="production"),
)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    # Local apps
    "apps.core",
    "apps.catalog",
    "apps.sales",
    "apps.inventory",
    "apps.whatsapp",
]

MIDDLEWARE = [
    "config.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3"),
}
DATABASES["default"]["CONN_MAX_AGE"] = 600
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# REST Framework
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# CORS
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

# WhatsApp Features
ENABLE_WHATSAPP_TYPING = env.bool("ENABLE_WHATSAPP_TYPING", default=True)
ENABLE_WHATSAPP_MARK_AS_READ = env.bool("ENABLE_WHATSAPP_MARK_AS_READ", default=True)

# Nudge / drip system
USE_TEMPLATE_MESSAGES = env.bool("USE_TEMPLATE_MESSAGES", default=False)
NUDGE_SEND_WINDOW_START = 7   # 07:00 CAT — nudges only fire in this window
NUDGE_SEND_WINDOW_END = 10    # 10:00 CAT
# CTA gates — flip to True when the corresponding feature ships
NUDGE_ENABLE_UNDO_CTA = env.bool("NUDGE_ENABLE_UNDO_CTA", default=False)
NUDGE_ENABLE_INVENTORY_CTA = env.bool("NUDGE_ENABLE_INVENTORY_CTA", default=False)
NUDGE_ENABLE_COMMUNITY_CTA = env.bool("NUDGE_ENABLE_COMMUNITY_CTA", default=False)
NUDGE_ENABLE_OPTIONS_CTA = env.bool("NUDGE_ENABLE_OPTIONS_CTA", default=False)

# Meta WhatsApp Business Cloud API Configuration
META_WHATSAPP_ACCESS_TOKEN = env("META_WHATSAPP_ACCESS_TOKEN", default="")
META_WHATSAPP_PHONE_NUMBER_ID = env("META_WHATSAPP_PHONE_NUMBER_ID", default="")
META_WHATSAPP_VERIFY_TOKEN = env("META_WHATSAPP_VERIFY_TOKEN", default="")  # Token for webhook verification
META_WHATSAPP_APP_SECRET = env("META_WHATSAPP_APP_SECRET", default="")  # For signature verification

# Admin Configuration
ADMIN_PHONE_NUMBER = env("ADMIN_PHONE_NUMBER", default=None)  # Receives waitlist approval notifications

# Support Configuration
SUPPORT_PHONE_NUMBER = env("SUPPORT_PHONE_NUMBER", default="")  # Shown to shop owners in the help menu (#131)

# OpenRouter Configuration
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
OPENROUTER_MODEL = env("OPENROUTER_MODEL", default="google/gemini-2.5-flash-lite")

# Gemini direct fallback (used when OpenRouter returns 402)
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")

# Eleven Labs Configuration
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY", default="")

# Cloudflare R2 Storage Configuration
R2_ACCESS_KEY_ID = env("R2_ACCESS_KEY_ID", default="")
R2_SECRET_ACCESS_KEY = env("R2_SECRET_ACCESS_KEY", default="")
R2_ENDPOINT_URL = env("R2_ENDPOINT_URL", default="")
R2_BUCKET_NAME = env("R2_BUCKET_NAME").strip()
R2_PUBLIC_URL = env("R2_PUBLIC_URL", default="")

# Mock WhatsApp client for staging/test (avoid sending real messages)
USE_MOCK_WHATSAPP = env.bool("USE_MOCK_WHATSAPP", default=False)

# Logging Configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
