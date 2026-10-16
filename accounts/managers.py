"""Manager سفارشی کاربران (نرمال‌سازی شماره موبایل و سوپروزر)."""

from django.contrib.auth.base_user import BaseUserManager
from django.db import models

from common.validators import normalize_phone_number


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, phone_number: str, password, **extra_fields):
        phone = normalize_phone_number(phone_number)
        if not phone:
            raise ValueError("شماره موبایل نمی‌تواند خالی باشد.")
        user = self.model(phone_number=phone, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, phone_number: str, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(phone_number, password, **extra_fields)

    def create_superuser(self, phone_number: str, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_phone_verified", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("سوپروزر باید is_staff=True داشته باشد.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("سوپروزر باید is_superuser=True داشته باشد.")
        return self._create_user(phone_number, password, **extra_fields)

    def get_by_natural_key(self, phone_number):
        return self.get(**{self.model.USERNAME_FIELD: normalize_phone_number(phone_number)})
