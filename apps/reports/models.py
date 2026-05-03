from django.db import models


class Report(models.Model):
    target_date = models.DateField()
    title = models.CharField(max_length=255)
    drive_file_id = models.CharField(max_length=255, blank=True)
    local_path = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-target_date", "-created_at"]

    def __str__(self):
        return self.title
