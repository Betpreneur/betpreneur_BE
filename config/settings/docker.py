"""Containerised deploys: production hardening, container-friendly hosts."""
from decouple import Csv, config

from .production import *

ALLOWED_HOSTS = config(
    "DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1,backend", cast=Csv()
)
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)
