"""
Model های محصولات.

نکات مهم معماری:
  • قیمت نهایی ذخیره نمی‌شود؛ همیشه از `base_price` + `Discount` محاسبه می‌شود.
  • موجودی در سطح واریانت (رنگ + سایز) نگهداری می‌شود.
  • محصول آرشیوشده حذف فیزیکی نمی‌شود تا سوابق سفارش‌ها محفوظ بماند.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel
from common.validators import validate_image_file

from .managers import CategoryManager, ProductManager
from .services.pricing import calculate_price


def product_image_upload_path(instance, filename: str) -> str:
    """مسیر امن ذخیره تصویر محصول با نام یکتای تصادفی."""
    import os
    from pathlib import PurePath

    safe_name = os.path.basename(PurePath(filename)).replace(" ", "-")
    return f"products/{instance.product_id or 'unassigned'}/{safe_name}"


class Category(TimeStampedModel):
    """دسته‌بندی محصولات (قابل مدیریت از ادمین، بدون Hardcode)."""

    name = models.CharField(_("نام"), max_length=120, unique=True)
    slug = models.SlugField(
        _("اسلاگ"),
        max_length=140,
        unique=True,
        allow_unicode=True,
        db_index=True,
    )
    description = models.TextField(_("توضیحات"), blank=True)
    image = models.ImageField(
        _("تصویر"),
        upload_to="categories/",
        blank=True,
        validators=[validate_image_file],
    )
    parent = models.ForeignKey(
        "self",
        verbose_name=_("دسته‌بندی والد"),
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.SET_NULL,
    )
    is_active = models.BooleanField(_("فعال"), default=True, db_index=True)
    sort_order = models.PositiveIntegerField(_("ترتیب نمایش"), default=0)

    objects = CategoryManager

    class Meta:
        verbose_name = "دسته‌بندی"
        verbose_name_plural = "دسته‌بندی‌ها"
        ordering = ("sort_order", "name")
        indexes = [models.Index(fields=["is_active", "sort_order"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or f"category-{self.pk or ''}"
        super().save(*args, **kwargs)

    @property
    def product_count(self) -> int:
        # اگر در View با `with_live_product_count` مقداردهی شده باشد، همان را
        # برمی‌گرداند تا در صفحاتی که چندین دسته را نمایش می‌دهند N+1 رخ ندهد.
        annotated = getattr(self, "live_product_count", None)
        if annotated is not None:
            return annotated
        return self.products.live().count()


class Product(TimeStampedModel):
    """محصول فروشگاه."""

    name = models.CharField(_("نام محصول"), max_length=200, db_index=True)
    slug = models.SlugField(
        _("اسلاگ"),
        max_length=220,
        unique=True,
        allow_unicode=True,
        db_index=True,
    )
    description = models.TextField(_("توضیحات"), blank=True)
    category = models.ForeignKey(
        Category,
        verbose_name=_("دسته‌بندی"),
        related_name="products",
        on_delete=models.PROTECT,
        db_index=True,
    )
    base_price = models.DecimalField(
        _("قیمت پایه (تومان)"),
        max_digits=12,
        decimal_places=0,
        help_text=_("قیمت بدون احتساب تخفیف"),
    )
    is_published = models.BooleanField(
        _("منتشر شود"), default=False, db_index=True,
        help_text=_("در صورت غیرفعال بودن، محصول در فروشگاه دیده نمی‌شود."),
    )
    is_active = models.BooleanField(_("فعال"), default=True, db_index=True)
    is_featured = models.BooleanField(_("محصول منتخب"), default=False, db_index=True)
    archived_at = models.DateTimeField(
        _("زمان آرشیو"), null=True, blank=True, db_index=True
    )

    objects = ProductManager

    class Meta:
        verbose_name = "محصول"
        verbose_name_plural = "محصولات"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["is_published", "is_active", "archived_at"]),
            models.Index(fields=["category", "is_published"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_price__gte=0),
                name="product_base_price_non_negative",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name, allow_unicode=True) or "product"
            slug = base
            counter = 1
            while (
                Product.objects.filter(slug=slug)
                .exclude(pk=self.pk)
                .exists()
            ):
                counter += 1
                slug = f"{base}-{counter}"
            self.slug = slug
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.base_price is not None and self.base_price < 0:
            raise ValidationError({"base_price": _("قیمت نمی‌تواند منفی باشد.")})

    # ------------------------------------------------------------------
    # وضعیت‌ها
    # ------------------------------------------------------------------
    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_live(self) -> bool:
        return self.is_published and self.is_active and not self.is_archived

    @property
    def stock_quantity(self) -> int:
        return sum(v.stock for v in self.variants.all() if v.is_active)

    @property
    def is_in_stock(self) -> bool:
        # از کش prefetch استفاده می‌کند (در صفحات فهرست، variants از قبل prefetch
        # شده) تا به‌ازای هر محصول یک کوئری EXISTS اضافه اجرا نشود.
        return any(v.is_active and v.stock > 0 for v in self.variants.all())

    # ------------------------------------------------------------------
    # قیمت (همیشه در Backend محاسبه می‌شود)
    # ------------------------------------------------------------------
    @property
    def price_info(self):
        return calculate_price(self)

    @property
    def final_price(self):
        return self.price_info.final_price

    @property
    def discount_percent(self) -> int:
        return self.price_info.discount_percent

    @property
    def has_discount(self) -> bool:
        return self.price_info.has_discount

    # ------------------------------------------------------------------
    # عملیات وضعیتی
    # ------------------------------------------------------------------
    def publish(self, save: bool = True):
        self.is_published = True
        if save:
            self.save(update_fields=["is_published", "updated_at"])

    def unpublish(self, save: bool = True):
        self.is_published = False
        if save:
            self.save(update_fields=["is_published", "updated_at"])

    def archive(self):
        """آرشیو: حذف نرم؛ سوابق سفارش‌ها دست‌نخورده می‌ماند."""
        self.archived_at = timezone.now()
        self.is_published = False
        self.is_active = False
        self.save(update_fields=["archived_at", "is_published", "is_active", "updated_at"])

    def restore_from_archive(self):
        self.archived_at = None
        self.is_active = True
        self.save(update_fields=["archived_at", "is_active", "updated_at"])

    @property
    def primary_image(self):
        # از کش prefetch استفاده می‌کند؛ ترتیب پیش‌فرض ImageMeta تصویر اصلی را
        # ابتدا می‌آورد، اما برای اطمینان، `is_primary` صریح بررسی می‌شود.
        images = list(self.images.all())
        if not images:
            return None
        for image in images:
            if image.is_primary:
                return image
        return images[0]


class ProductImage(models.Model):
    """تصاویر محصول (یک تصویر اصلی + تصاویر گالری)."""

    product = models.ForeignKey(
        Product,
        verbose_name=_("محصول"),
        related_name="images",
        on_delete=models.CASCADE,
    )
    image = models.ImageField(
        _("تصویر"),
        upload_to=product_image_upload_path,
        validators=[validate_image_file],
    )
    alt_text = models.CharField(
        _("متن جایگزین"), max_length=200, blank=True,
        help_text=_("برای بهینه‌سازی SEO و دسترس‌پذیری"),
    )
    is_primary = models.BooleanField(_("تصویر اصلی"), default=False)
    sort_order = models.PositiveIntegerField(_("ترتیب"), default=0)

    class Meta:
        verbose_name = "تصویر محصول"
        verbose_name_plural = "تصاویر محصول"
        ordering = ("-is_primary", "sort_order", "id")

    def __str__(self) -> str:
        return f"تصویر {self.product_id}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            (
                ProductImage.objects.filter(product=self.product)
                .exclude(pk=self.pk)
                .update(is_primary=False)
            )

    def clean(self):
        super().clean()
        validate_image_file(self.image)

    @property
    def alt(self) -> str:
        return self.alt_text or (self.product.name if self.product_id else "")


class Color(models.Model):
    """رنگ به‌صورت Entity مستقل (قابل استفاده مجدد بین محصولات)."""

    name = models.CharField(_("نام رنگ"), max_length=60, unique=True)
    slug = models.SlugField(_("اسلاگ"), max_length=70, unique=True, allow_unicode=True)
    hex_code = models.CharField(
        _("کد رنگ"), max_length=7, default="#111827",
        help_text=_("مانند #111827 برای نمایش دایره رنگ"),
    )
    is_active = models.BooleanField(_("فعال"), default=True)
    sort_order = models.PositiveIntegerField(_("ترتیب نمایش"), default=0)

    class Meta:
        verbose_name = "رنگ"
        verbose_name_plural = "رنگ‌ها"
        ordering = ("sort_order", "name")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or f"color-{self.pk or ''}"
        super().save(*args, **kwargs)


class Size(models.Model):
    """سایز به‌صورت Entity مستقل."""

    name = models.CharField(_("نام سایز"), max_length=30, unique=True)
    slug = models.SlugField(_("اسلاگ"), max_length=40, unique=True, allow_unicode=True)
    sort_order = models.PositiveIntegerField(_("ترتیب نمایش"), default=0)
    is_active = models.BooleanField(_("فعال"), default=True)

    class Meta:
        verbose_name = "سایز"
        verbose_name_plural = "سایزها"
        ordering = ("sort_order", "name")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or f"size-{self.pk or ''}"
        super().save(*args, **kwargs)


class VariantStockError(Exception):
    """خطای موجودی هنگام کم/زیاد کردن موجودی."""


class ProductVariant(models.Model):
    """
    ترکیب رنگ + سایز یک محصول؛ واحد اصلی موجودی و فروش.

    نمونه: «تیشرت مردانه / مشکی / M / موجودی 5»
    """

    product = models.ForeignKey(
        Product,
        verbose_name=_("محصول"),
        related_name="variants",
        on_delete=models.CASCADE,
    )
    color = models.ForeignKey(
        Color,
        verbose_name=_("رنگ"),
        related_name="variants",
        on_delete=models.PROTECT,
    )
    size = models.ForeignKey(
        Size,
        verbose_name=_("سایز"),
        related_name="variants",
        on_delete=models.PROTECT,
    )
    sku = models.CharField(
        _("کد کالا (SKU)"),
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
    )
    stock = models.PositiveIntegerField(_("موجودی"), default=0)
    is_active = models.BooleanField(_("فعال"), default=True)

    class Meta:
        verbose_name = "واریانت"
        verbose_name_plural = "واریانت‌ها"
        ordering = ("product", "color", "size")
        constraints = [
            models.UniqueConstraint(
                fields=["product", "color", "size"],
                name="unique_variant_per_product_color_size",
            ),
            models.CheckConstraint(
                condition=models.Q(stock__gte=0),
                name="variant_stock_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["product", "is_active"])]

    def __str__(self) -> str:
        return f"{self.product} - {self.color} - {self.size}"

    @property
    def is_in_stock(self) -> bool:
        return self.is_active and self.stock > 0

    @property
    def label(self) -> str:
        return f"{self.color} / {self.size}"

    def decrement_stock(self, quantity: int) -> bool:
        """
        کم کردن موجودی به‌صورت اتمیک و امن در برابر Race Condition.

        شرط `stock__gte` در خود SQL بررسی می‌شود؛ بنابراین دو کاربر همزمان
        نمی‌توانند آخرین موجودی را هر دو بخرند.
        """
        if quantity <= 0:
            raise VariantStockError("تعداد باید بزرگ‌تر از صفر باشد.")
        updated = ProductVariant.objects.filter(
            pk=self.pk, is_active=True, stock__gte=quantity
        ).update(stock=models.F("stock") - quantity)
        if updated:
            self.refresh_from_db(fields=["stock"])
            return True
        return False

    def restore_stock(self, quantity: int) -> None:
        """بازگرداندن موجودی (لغو سفارش/انقضای رزرو)."""
        if quantity <= 0:
            raise VariantStockError("تعداد باید بزرگ‌تر از صفر باشد.")
        ProductVariant.objects.filter(pk=self.pk).update(
            stock=models.F("stock") + quantity
        )
        self.refresh_from_db(fields=["stock"])


class Discount(TimeStampedModel):
    """تخفیف روی محصول (درصدی یا مبلغی)؛ قیمت نهایی در Backend محاسبه می‌شود."""

    class DiscountType(models.TextChoices):
        PERCENT = "percent", _("درصدی")
        FIXED = "fixed", _("مبلغی (تومان)")

    name = models.CharField(_("عنوان داخلی"), max_length=120)
    product = models.OneToOneField(
        Product,
        verbose_name=_("محصول"),
        related_name="discount",
        on_delete=models.CASCADE,
    )
    discount_type = models.CharField(
        _("نوع تخفیف"),
        max_length=10,
        choices=DiscountType.choices,
        default=DiscountType.PERCENT,
    )
    value = models.DecimalField(
        _("مقدار تخفیف"),
        max_digits=12,
        decimal_places=0,
        help_text=_("در حالت درصدی: عدد ۱ تا ۱۰۰ | در حالت مبلغی: مبلغ به تومان"),
    )
    is_active = models.BooleanField(_("فعال"), default=True, db_index=True)
    starts_at = models.DateTimeField(
        _("شروع"), null=True, blank=True,
        help_text=_("خالی = از همین حالا"),
    )
    ends_at = models.DateTimeField(
        _("پایان"), null=True, blank=True,
        help_text=_("خالی = بدون محدودیت زمانی"),
    )

    class Meta:
        verbose_name = "تخفیف"
        verbose_name_plural = "تخفیف‌ها"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(value__gt=0),
                name="discount_value_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_discount_type_display()})"

    def clean(self):
        super().clean()
        errors = {}
        if self.value is not None and self.value <= 0:
            errors["value"] = _("مقدار تخفیف باید بزرگ‌تر از صفر باشد.")

        if (
            self.value is not None
            and self.discount_type == self.DiscountType.PERCENT
            and self.value > 100
        ):
            errors["value"] = _("درصد تخفیف نمی‌تواند بیشتر از ۱۰۰ باشد.")

        if (
            self.value is not None
            and self.product_id
            and self.discount_type == self.DiscountType.FIXED
            and self.product_id
            and self.product.base_price
            and self.value > self.product.base_price
        ):
            errors["value"] = _("مبلغ تخفیف نمی‌تواند از قیمت محصول بیشتر باشد.")

        if (
            self.starts_at
            and self.ends_at
            and self.ends_at <= self.starts_at
        ):
            errors["ends_at"] = _("تاریخ پایان باید بعد از تاریخ شروع باشد.")

        if errors:
            raise ValidationError(errors)

    @property
    def is_valid_now(self) -> bool:
        """آیا تخفیف هم‌اکون قابل اعمال است؟"""
        if not self.is_active:
            return False
        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True
