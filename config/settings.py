from datetime import timedelta
from pathlib import Path

from decouple import Csv, config


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-only-change-me")
DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "corsheaders",
    "apps.common",
    "apps.accounts",
    "apps.bankroll",
    "apps.reports",
    "apps.algo",
    "apps.integrations",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": config("DB_ENGINE", default="django.db.backends.sqlite3"),
        "NAME": config("DB_NAME", default=str(BASE_DIR / "db.sqlite3")),
        "USER": config("DB_USER", default=""),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default=""),
        "PORT": config("DB_PORT", default=""),
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = config("CELERY_TASK_TIME_LIMIT", default=60 * 60 * 3, cast=int)
CELERY_TASK_SOFT_TIME_LIMIT = config("CELERY_TASK_SOFT_TIME_LIMIT", default=60 * 60 * 2, cast=int)

_cors_allowed_origins = config("CORS_ALLOWED_ORIGINS", default="")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in _cors_allowed_origins.split(",")
    if origin.strip()
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="noreply@betpreneur.local")

GRIND_ALGO = {
    "APS_KEY": config("APS_KEY", default=""),
    "APS_MAX_FIXTURES": config("APS_MAX_FIXTURES", default="90"),
    "GEMINI_API_KEY": config("GEMINI_API_KEY", default=""),
    "GEMINI_MODEL": config("GEMINI_MODEL", default="gemini-2.5-flash"),
    "KEY_FILE": config("KEY_FILE", default=""),
    "SHEET_NAME": config("SHEET_NAME", default="GrindAlgo Tracker"),
    "DRIVE_FOLDER": config("DRIVE_FOLDER", default="GrindAlgo Reports"),
    "EMAIL_RECIPIENT": config("EMAIL_RECIPIENT", default=""),
}

# Auth settings
RESEND_API_KEY = config("RESEND_API_KEY", default="")
RESEND_FROM_NAME = config("RESEND_FROM_NAME", default="Betpreneur")
RESEND_FROM_EMAIL_PREFIX = config("RESEND_FROM_EMAIL_PREFIX", default="support")
RESEND_FROM_EMAIL_DOMAIN = config("RESEND_FROM_EMAIL_DOMAIN", default="betpreneur.ng")
RESEND_FROM_EMAIL = config(
    "RESEND_FROM_EMAIL",
    default=f"{RESEND_FROM_EMAIL_PREFIX}@{RESEND_FROM_EMAIL_DOMAIN}",
)
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:3000")

# JWT Settings
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_TOKEN": True,
}

# Swagger/OpenAPI Settings
SPECTACULAR_SETTINGS = {
    "TITLE": "Betpreneur API",
    "DESCRIPTION": """
## Betpreneur - Sports Betting Intelligence Engine

A Django REST API backend for the GrindAlgo betting intelligence system.

### Authentication
This API uses JWT (JSON Web Tokens) for authentication.
1. Obtain tokens via `/api/auth/login/` or `/api/auth/signup/`
2. Include access token in header: `Authorization: Bearer <access_token>`
3. Refresh expired tokens via `/api/auth/token/refresh/`

### Key Features
- **User Authentication**: Signup, login, email verification, password reset
- **Algo Management**: Run betting algorithms, track picks
- **Bankroll Tracking**: Monitor betting bankroll
- **Reports**: Generate and track reports
    """,
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "displayOperationId": True,
        "defaultModelsExpandDepth": 2,
        "defaultModelExpandDepth": 2,
        "persistAuthorization": True,
    },
    "COMPONENT_SPLITING": False,
    "APPEND_TAG": [
        {
            "name": "Authentication",
            "description": "User authentication endpoints (signup, login, logout, password reset)",
        },
        {
            "name": "Algo",
            "description": "Betting algorithm run management",
        },
        {
            "name": "Bankroll",
            "description": "Bankroll tracking endpoints",
        },
        {
            "name": "Reports",
            "description": "Report generation and tracking",
        },
        {
            "name": "Health",
            "description": "System health check endpoints",
        },
    ],
    "TAGS": [],
}
