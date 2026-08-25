"""Model base classes shared by every module."""
from __future__ import annotations

from django.db import models


class TimeStampedModel(models.Model):
    """created_at / updated_at, which nearly every table here already has."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
