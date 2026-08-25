from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
    is_email_verified = models.BooleanField(default=False)
    whatsapp_country_code = models.CharField(max_length=5, blank=True)  # e.g., "+234"
    whatsapp_number = models.CharField(max_length=15, blank=True)  # e.g., "8012345678"
    
    # Email verification
    email_verification_code = models.CharField(max_length=6, blank=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    
    # Password reset
    password_reset_code = models.CharField(max_length=6, blank=True)
    password_reset_sent_at = models.DateTimeField(null=True, blank=True)
    
    REQUIRED_FIELDS = ["email"]

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.username