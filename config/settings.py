from datetime import timedelta
from pathlib import Path

from decouple import Csv, config


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-only-change-me")
DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
ENABLE_WEBSOCKETS = config("ENABLE_WEBSOCKETS", default=False, cast=bool)

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

if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"]["OPTIONS"] = {
        "connect_timeout": config("DB_CONNECT_TIMEOUT", default=10, cast=int),
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
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True

if ENABLE_WEBSOCKETS:
    INSTALLED_APPS.append("channels")
    ASGI_APPLICATION = "config.asgi.application"
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [CELERY_BROKER_URL],
            },
        },
    }

CELERY_TASK_TIME_LIMIT = config("CELERY_TASK_TIME_LIMIT", default=60 * 60 * 3, cast=int)
CELERY_TASK_SOFT_TIME_LIMIT = config("CELERY_TASK_SOFT_TIME_LIMIT", default=60 * 60 * 2, cast=int)
SLIP_REVIEW_QUEUE = config("SLIP_REVIEW_QUEUE", default="slip_review")
SLIP_REVIEW_IMPORT_QUEUE = config("SLIP_REVIEW_IMPORT_QUEUE", default="slip_review_import")
SLIP_REVIEW_LEG_QUEUE = config("SLIP_REVIEW_LEG_QUEUE", default="slip_review_leg")
SLIP_REVIEW_FINALIZE_QUEUE = config("SLIP_REVIEW_FINALIZE_QUEUE", default="slip_review_finalize")
ALGO_DAILY_QUEUE = config("ALGO_DAILY_QUEUE", default="algo_daily")
ALGO_SCORING_QUEUE = config("ALGO_SCORING_QUEUE", default="algo_scoring")
ALGO_LLM_QUEUE = config("ALGO_LLM_QUEUE", default="algo_llm")
ALGO_STATPAL_QUEUE = config("ALGO_STATPAL_QUEUE", default="algo_statpal")
ALGO_SETTLEMENT_QUEUE = config("ALGO_SETTLEMENT_QUEUE", default="algo_settlement")
ALGO_MAINTENANCE_QUEUE = config("ALGO_MAINTENANCE_QUEUE", default="algo_maintenance")
CELERY_TASK_ROUTES = {
    "apps.algo.tasks.generate_daily_picks": {"queue": ALGO_DAILY_QUEUE},
    "apps.algo.tasks.publish_daily_run": {"queue": ALGO_DAILY_QUEUE},
    "apps.algo.tasks.recover_daily_run": {"queue": ALGO_DAILY_QUEUE},
    "apps.algo.tasks.score_fixture_for_daily_run": {"queue": ALGO_SCORING_QUEUE},
    "apps.algo.tasks.explain_picks_for_run": {"queue": ALGO_LLM_QUEUE},
    "apps.algo.tasks.build_statpal_daily_cache": {"queue": ALGO_STATPAL_QUEUE},
    "apps.algo.tasks.sync_fixture_horizon": {"queue": ALGO_STATPAL_QUEUE},
    "apps.algo.tasks.fit_score_models": {"queue": ALGO_STATPAL_QUEUE},
    "apps.algo.tasks.settle_daily_results": {"queue": ALGO_SETTLEMENT_QUEUE},
    "apps.algo.tasks.settle_slip_selections": {"queue": ALGO_SETTLEMENT_QUEUE},
    "apps.algo.tasks.refresh_imminent_lineups": {"queue": ALGO_MAINTENANCE_QUEUE},
    "apps.algo.tasks.refresh_player_availability": {"queue": ALGO_MAINTENANCE_QUEUE},
    "apps.algo.tasks.recover_stale_slip_reviews": {"queue": ALGO_MAINTENANCE_QUEUE},
    "apps.algo.tasks.run_monthly_auditor": {"queue": ALGO_MAINTENANCE_QUEUE},
    "apps.algo.tasks.import_slip_review": {"queue": SLIP_REVIEW_IMPORT_QUEUE},
    "apps.algo.tasks.analyse_slip_review_leg": {"queue": SLIP_REVIEW_LEG_QUEUE},
    "apps.algo.tasks.finalize_slip_review_import": {"queue": SLIP_REVIEW_FINALIZE_QUEUE},
}
CELERY_TASK_ANNOTATIONS = {
    "apps.algo.tasks.score_fixture_for_daily_run": {
        "rate_limit": config("ALGO_SCORING_RATE_LIMIT", default="6/m"),
        "time_limit": config("ALGO_SCORING_TIME_LIMIT", default=1200, cast=int),
        "soft_time_limit": config("ALGO_SCORING_SOFT_TIME_LIMIT", default=900, cast=int),
    },
    "apps.algo.tasks.explain_picks_for_run": {
        "rate_limit": config("ALGO_LLM_RATE_LIMIT", default="12/m"),
        "time_limit": config("ALGO_LLM_TIME_LIMIT", default=420, cast=int),
        "soft_time_limit": config("ALGO_LLM_SOFT_TIME_LIMIT", default=300, cast=int),
    },
}

_cors_allowed_origins = config("CORS_ALLOWED_ORIGINS", default="")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in _cors_allowed_origins.split(",")
    if origin.strip()
]
CORS_EXPOSE_HEADERS = ["ETag"]

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
    "APS_MAX_FIXTURES": config("APS_MAX_FIXTURES", default="0"),
    "APS_TRACK_ALL_LEAGUES": config("APS_TRACK_ALL_LEAGUES", default="False"),
    "APS_EXTRA_TRACKED_LEAGUES": config("APS_EXTRA_TRACKED_LEAGUES", default=""),
    "GEMINI_API_KEY": config("GEMINI_API_KEY", default=""),
    "GEMINI_MODEL": config("GEMINI_MODEL", default="gemini-2.5-flash"),
    "MINIMAX_API_KEY": config("MINIMAX_API_KEY", default=""),
    "MINIMAX_BASE_URL": config("MINIMAX_BASE_URL", default="https://api.minimax.io/v1"),
    "MINIMAX_MODEL": config("MINIMAX_MODEL", default="MiniMax-M2.7-highspeed"),
    "ALGO_LLM_REASONING_ENABLED": config("ALGO_LLM_REASONING_ENABLED", default=""),
    "KEY_FILE": config("KEY_FILE", default=""),
    "SHEET_NAME": config("SHEET_NAME", default="GrindAlgo Tracker"),
    "DRIVE_FOLDER": config("DRIVE_FOLDER", default="GrindAlgo Reports"),
    "EMAIL_RECIPIENT": config("EMAIL_RECIPIENT", default=""),
    "ALGO_MIN_EV": config("ALGO_MIN_EV", default="0.02"),
    "ALGO_MIN_MARKET_SAMPLE": config("ALGO_MIN_MARKET_SAMPLE", default="15"),
    "ALGO_MAX_DAILY_PICKS": config("ALGO_MAX_DAILY_PICKS", default="15"),
    "ALGO_PUBLISH_MIN_CONFIDENCE": config("ALGO_PUBLISH_MIN_CONFIDENCE", default="70"),
    "ALGO_PUBLISH_MIN_EV": config("ALGO_PUBLISH_MIN_EV", default="0.03"),
    "ALGO_PUBLISH_WILD_CARDS": config("ALGO_PUBLISH_WILD_CARDS", default="False"),
    "ALGO_LEAGUE_MARKET_MIN_SAMPLE": config("ALGO_LEAGUE_MARKET_MIN_SAMPLE", default="8"),
    "ALGO_PROBATION_CONFIDENCE_EXTRA": config("ALGO_PROBATION_CONFIDENCE_EXTRA", default="5"),
    "ALGO_PROBATION_EV_EXTRA": config("ALGO_PROBATION_EV_EXTRA", default="0.03"),
    "ALGO_CONFIDENCE_BAND_MIN_SAMPLE": config("ALGO_CONFIDENCE_BAND_MIN_SAMPLE", default="20"),
    "ALGO_CALIBRATION_CONFIDENCE_EXTRA": config("ALGO_CALIBRATION_CONFIDENCE_EXTRA", default="3"),
    "ALGO_CALIBRATION_EV_EXTRA": config("ALGO_CALIBRATION_EV_EXTRA", default="0.02"),
    "ALGO_MAX_DAILY_DC12_PICKS": config("ALGO_MAX_DAILY_DC12_PICKS", default="0"),
    "ALGO_PUBLISH_DC12": config("ALGO_PUBLISH_DC12", default="False"),
    "ALGO_MAX_DAILY_SAME_MARKET_PICKS": config("ALGO_MAX_DAILY_SAME_MARKET_PICKS", default="0"),
    "ALGO_REQUIRE_REAL_ODDS": config("ALGO_REQUIRE_REAL_ODDS", default="False"),
    "ALGO_ALLOW_ESTIMATED_PICKS": config("ALGO_ALLOW_ESTIMATED_PICKS", default="False"),
    "ALGO_CORNER_MIN_CONFIDENCE": config("ALGO_CORNER_MIN_CONFIDENCE", default="68"),
    "ALGO_ENABLE_CORNERS": config("ALGO_ENABLE_CORNERS", default="True"),
    "ALGO_CORNER_MIN_LINE": config("ALGO_CORNER_MIN_LINE", default="7.5"),
    "ALGO_CORNER_MAX_ODDS": config("ALGO_CORNER_MAX_ODDS", default="3.5"),
    "ALGO_CORNER_MIN_PROFILE_GAMES": config("ALGO_CORNER_MIN_PROFILE_GAMES", default="6"),
    "STATPAL_ENABLED": config("STATPAL_ENABLED", default="False"),
    "STATPAL_ACCESS_KEY": config("STATPAL_ACCESS_KEY", default=""),
    "STATPAL_BASE_URL": config("STATPAL_BASE_URL", default="https://statpal.io/api/v2"),
    "STATPAL_USAGE_BASE_URL": config("STATPAL_USAGE_BASE_URL", default="https://statpal.io/api"),
    "STATPAL_TIMEOUT": config("STATPAL_TIMEOUT", default=20, cast=int),
    "STATPAL_ENDPOINT_SOCCER_LEAGUES": config("STATPAL_ENDPOINT_SOCCER_LEAGUES", default="soccer/leagues"),
    "STATPAL_ENDPOINT_SOCCER_LIVE_MATCHES": config("STATPAL_ENDPOINT_SOCCER_LIVE_MATCHES", default="soccer/matches/live"),
    "STATPAL_ENDPOINT_SOCCER_LEAGUE_SEASONS": config(
        "STATPAL_ENDPOINT_SOCCER_LEAGUE_SEASONS",
        default="soccer/leagues/seasons",
    ),
    "STATPAL_ENDPOINT_SOCCER_MATCHES_DAILY": config(
        "STATPAL_ENDPOINT_SOCCER_MATCHES_DAILY",
        default="soccer/matches/daily",
    ),
    "STATPAL_ENDPOINT_SOCCER_LEAGUE_MATCHES": config(
        "STATPAL_ENDPOINT_SOCCER_LEAGUE_MATCHES",
        default="soccer/leagues/{league_id}/matches",
    ),
    "STATPAL_ENDPOINT_SOCCER_LEAGUE_MATCH_STATS": config(
        "STATPAL_ENDPOINT_SOCCER_LEAGUE_MATCH_STATS",
        default="soccer/leagues/{league_id}/matches/stats",
    ),
    "STATPAL_ENDPOINT_SOCCER_LEAGUE_STANDINGS": config(
        "STATPAL_ENDPOINT_SOCCER_LEAGUE_STANDINGS",
        default="soccer/leagues/{league_id}/standings",
    ),
    "STATPAL_ENDPOINT_SOCCER_LEAGUE_STATS": config(
        "STATPAL_ENDPOINT_SOCCER_LEAGUE_STATS",
        default="soccer/leagues/{league_id}/stats",
    ),
    "STATPAL_ENDPOINT_SOCCER_HEAD_TO_HEAD": config(
        "STATPAL_ENDPOINT_SOCCER_HEAD_TO_HEAD",
        default="soccer/head-to-head",
    ),
    "STATPAL_ENDPOINT_SOCCER_INJURIES_SUSPENSIONS": config(
        "STATPAL_ENDPOINT_SOCCER_INJURIES_SUSPENSIONS",
        default="soccer/injuries-suspensions",
    ),
    "STATPAL_ENDPOINT_SOCCER_TEAM": config("STATPAL_ENDPOINT_SOCCER_TEAM", default="soccer/teams/{team_id}"),
    "STATPAL_ENDPOINT_SOCCER_PLAYER": config("STATPAL_ENDPOINT_SOCCER_PLAYER", default="soccer/players/{player_id}"),
    "STATPAL_ENDPOINT_SOCCER_COACH": config("STATPAL_ENDPOINT_SOCCER_COACH", default="soccer/coaches/{coach_id}"),
    "STATPAL_ENDPOINT_SOCCER_IMAGES": config("STATPAL_ENDPOINT_SOCCER_IMAGES", default="soccer/images"),
    "STATPAL_ENDPOINT_SOCCER_LIVE_STORYLINES": config(
        "STATPAL_ENDPOINT_SOCCER_LIVE_STORYLINES",
        default="soccer/live-storylines",
    ),
    "STATPAL_ENDPOINT_SOCCER_TEAM_LINEUPS": config(
        "STATPAL_ENDPOINT_SOCCER_TEAM_LINEUPS",
        default="soccer/team-lineups",
    ),
    "STATPAL_ENDPOINT_SOCCER_WEATHER_FORECAST": config(
        "STATPAL_ENDPOINT_SOCCER_WEATHER_FORECAST",
        default="soccer/weather-forecast",
    ),
    "STATPAL_ENDPOINT_SOCCER_PREDICTIONS": config("STATPAL_ENDPOINT_SOCCER_PREDICTIONS", default="soccer/predictions"),
    "STATPAL_ENDPOINT_SOCCER_PREMATCH_ODDS": config(
        "STATPAL_ENDPOINT_SOCCER_PREMATCH_ODDS",
        default="soccer/leagues/{league_id}/odds/prematch",
    ),
    "STATPAL_ENDPOINT_SOCCER_LIVE_ODDS": config("STATPAL_ENDPOINT_SOCCER_LIVE_ODDS", default="soccer/odds/live"),
    "STATPAL_ENDPOINT_SOCCER_LIVE_ODDS_MARKETS": config(
        "STATPAL_ENDPOINT_SOCCER_LIVE_ODDS_MARKETS",
        default="soccer/odds/live/markets",
    ),
    "STATPAL_ENDPOINT_SOCCER_LIVE_ODDS_MATCH_STATES": config(
        "STATPAL_ENDPOINT_SOCCER_LIVE_ODDS_MATCH_STATES",
        default="soccer/odds/live/match-states",
    ),
    # Backward-compatible aliases from early StatPal integration stages.
    "STATPAL_ENDPOINT_SOCCER_PLAYER_STATS": config(
        "STATPAL_ENDPOINT_SOCCER_PLAYER_STATS",
        default="soccer/players/{player_id}",
    ),
    "STATPAL_ENDPOINT_SOCCER_TEAM_STATS": config(
        "STATPAL_ENDPOINT_SOCCER_TEAM_STATS",
        default="soccer/teams/{team_id}",
    ),
    "STATPAL_ENDPOINT_SOCCER_LINEUPS": config(
        "STATPAL_ENDPOINT_SOCCER_LINEUPS",
        default="soccer/team-lineups",
    ),
    "STATPAL_ENDPOINT_SOCCER_DETAILED_STATS": config(
        "STATPAL_ENDPOINT_SOCCER_DETAILED_STATS",
        default="soccer/leagues/{league_id}/matches/stats",
    ),
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
ALGO_READ_CACHE_SECONDS = config("ALGO_READ_CACHE_SECONDS", default=300, cast=int)

# JWT Settings
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        hours=config("JWT_ACCESS_TOKEN_HOURS", default=24, cast=int)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        hours=config("JWT_REFRESH_TOKEN_HOURS", default=24, cast=int)
    ),
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
    "TAGS": [
        {
            "name": "Authentication",
            "description": "User authentication endpoints (signup, login, logout, password reset)",
        },
        {
            "name": "Picks",
            "description": "Authenticated app endpoints for daily picks, top pick, downloads, and I-backed-this actions.",
        },
        {
            "name": "Public Record",
            "description": "Public audited track-record endpoints for landing pages and proof widgets.",
        },
        {
            "name": "Admin Algo",
            "description": "Internal staff-only endpoints for manual Celery triggers, run inspection, and task status.",
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
}
