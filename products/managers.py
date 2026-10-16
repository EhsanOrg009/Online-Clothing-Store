"""QuerySet و Manager سفارشی محصولات (نمایش، جستجو، مرتب‌سازی)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q


class ProductQuerySet(models.QuerySet):
    def live(self):
        """محصولات قابل نمایش در فروشگاه: منتشر + فعال + غیرآرشیو."""
        return self.filter(is_published=True, is_active=True, archived_at__isnull=True)

    def featured(self):
        return self.live().filter(is_featured=True)

    def discounted(self):
        """محصولاتی که هم‌اکنون تخفیف معتبر دارند."""
        from django.utils import timezone

        now = timezone.now()
        return self.live().filter(discount__is_active=True).filter(
            Q(discount__starts_at__isnull=True) | Q(discount__starts_at__lte=now),
            Q(discount__ends_at__isnull=True) | Q(discount__ends_at__gte=now),
        )

    def in_stock(self):
        return self.filter(variants__is_active=True, variants__stock__gt=0).distinct()

    def out_of_stock(self):
        return self.exclude(pk__in=self.in_stock().values("pk"))

    def search(self, query: str):
        """جستجوی واقعی در نام، توضیحات و نام دسته‌بندی."""
        query = (query or "").strip()
        if not query:
            return self
        return self.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(category__name__icontains=query)
        )

    def sort(self, order: str | None):
        """مرتب‌سازی: جدیدترین / ارزان‌ترین / گران‌ترین."""
        if order == "price_asc":
            return self.order_by("base_price", "id")
        if order == "price_desc":
            return self.order_by("-base_price", "id")
        if order == "oldest":
            return self.order_by("created_at", "id")
        return self.order_by("-created_at", "id")


ProductManager = ProductQuerySet.as_manager()


class CategoryQuerySet(models.QuerySet):
    def with_live_product_count(self):
        """شمارش محصولات «زنده» هر دسته با یک Query واحد (جلوگیری از N+1).

        نتیجه در خصیصهٔ `live_product_count` قرار می‌گیرد و `Category.product_count`
        در صورت وجود، همان را به‌جای یک COUNT جداگانه برمی‌گرداند.
        """
        return self.annotate(
            live_product_count=models.Count(
                "products",
                filter=models.Q(
                    products__is_published=True,
                    products__is_active=True,
                    products__archived_at__isnull=True,
                ),
            )
        )


CategoryManager = CategoryQuerySet.as_manager()
