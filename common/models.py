"""Model های پایه قابل استفاده در تمام پروژه."""

from django.db import models


class TimeStampedModel(models.Model):
    """افزودن زمان ایجاد و آخرین بروزرسانی به هر Model."""

    created_at = models.DateTimeField("تاریخ ایجاد", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("تاریخ بروزرسانی", auto_now=True)

    class Meta:
        abstract = True
        ordering = ("-created_at",)
