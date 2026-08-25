"""Developer machine defaults."""
from .base import *
from .base import INSTALLED_APPS, MIDDLEWARE

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Deliver events immediately rather than waiting on commit, so a shell session
# behaves the same as a request.
EVENT_BUS_IMMEDIATE = False
