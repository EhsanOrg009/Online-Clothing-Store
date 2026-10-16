"""تست‌های محصولات: قیمت‌گذاری، Manager ها و واریانت‌ها."""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from products.models import Category, Color, Discount, Product, ProductVariant, Size
from products.services.pricing import calculate_price


class PricingTests(TestCase):
    """قوانین قیمت‌گذاری — تنها منبع حقیقت قیمت در Backend است."""

    def setUp(self):
        self.category = Category.objects.create(name="پیراهن")
        self.product = Product.objects.create(
            name="پیراهن نمونه",
            category=self.category,
            base_price=Decimal("2000000"),
            is_published=True,
            is_active=True,
        )

    def _set_discount(self, **kwargs):
        defaults = {
            "name": "تخفیف",
            "product": self.product,
            "discount_type": Discount.DiscountType.PERCENT,
            "value": Decimal("25"),
            "is_active": True,
        }
        defaults.update(kwargs)
        Discount.objects.all().delete()
        return Discount.objects.create(**defaults)

    def test_no_discount_returns_base_price(self):
        info = calculate_price(self.product)
        self.assertFalse(info.has_discount)
        self.assertEqual(info.final_price, Decimal("2000000"))
        self.assertEqual(info.discount_amount, Decimal("0"))
        self.assertEqual(info.discount_percent, 0)

    def test_percent_discount(self):
        self._set_discount(value=Decimal("25"))
        info = calculate_price(self.product)
        self.assertTrue(info.has_discount)
        self.assertEqual(info.final_price, Decimal("1500000"))
        self.assertEqual(info.discount_amount, Decimal("500000"))
        self.assertEqual(info.discount_percent, 25)

    def test_fixed_discount(self):
        self._set_discount(
            discount_type=Discount.DiscountType.FIXED, value=Decimal("300000")
        )
        info = calculate_price(self.product)
        self.assertEqual(info.final_price, Decimal("1700000"))
        self.assertEqual(info.discount_amount, Decimal("300000"))

    def test_discount_can_never_exceed_base_price(self):
        self._set_discount(value=Decimal("5000000"))
        info = calculate_price(self.product)
        self.assertEqual(info.final_price, Decimal("0"))
        self.assertEqual(info.discount_amount, Decimal("2000000"))
        self.assertEqual(info.discount_percent, 100)

    def test_inactive_discount_is_ignored(self):
        self._set_discount(is_active=False)
        info = calculate_price(self.product)
        self.assertFalse(info.has_discount)
        self.assertEqual(info.final_price, Decimal("2000000"))

    def test_not_started_discount_is_ignored(self):
        self._set_discount(starts_at=timezone.now() + timedelta(days=1))
        info = calculate_price(self.product)
        self.assertFalse(info.has_discount)

    def test_expired_discount_is_ignored(self):
        self._set_discount(ends_at=timezone.now() - timedelta(minutes=1))
        info = calculate_price(self.product)
        self.assertFalse(info.has_discount)

    def test_discount_in_valid_window_applies(self):
        self._set_discount(
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1),
        )
        info = calculate_price(self.product)
        self.assertTrue(info.has_discount)

    def test_price_is_decimal_never_float(self):
        info = calculate_price(self.product)
        self.assertIsInstance(info.final_price, Decimal)


class ProductManagerTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="تی‌شرت")
        self.live = Product.objects.create(
            name="محصول منتشر",
            category=self.category,
            base_price=100000,
            is_published=True,
            is_active=True,
            is_featured=True,
        )
        self.draft = Product.objects.create(
            name="محصول پیش‌نویس",
            category=self.category,
            base_price=100000,
            is_published=False,
            is_active=True,
        )
        self.archived = Product.objects.create(
            name="محصول آرشیو",
            category=self.category,
            base_price=100000,
            is_published=True,
            is_active=True,
            archived_at=timezone.now(),
        )
        self.off = Product.objects.create(
            name="محصول غیرفعال",
            category=self.category,
            base_price=100000,
            is_published=True,
            is_active=False,
        )

    def test_live_excludes_draft_archived_and_inactive(self):
        names = set(Product.objects.live().values_list("name", flat=True))
        self.assertEqual(names, {"محصول منتشر"})

    def test_featured(self):
        names = set(Product.objects.featured().values_list("name", flat=True))
        self.assertEqual(names, {"محصول منتشر"})

    def test_search_is_case_and_diacritic_tolerant(self):
        Product.objects.create(
            name="پیراهن مردانه",
            category=self.category,
            base_price=100000,
            is_published=True,
        )
        self.assertIn(
            "پیراهن مردانه",
            set(Product.objects.search("پیراهن").values_list("name", flat=True)),
        )
        self.assertEqual(Product.objects.search("چیزی-که-وجود-ندارد").count(), 0)

    def test_sort_by_price(self):
        Product.objects.create(
            name="ارزان", category=self.category, base_price=1000, is_published=True
        )
        Product.objects.create(
            name="گران", category=self.category, base_price=9000, is_published=True
        )
        prices = list(
            Product.objects.live().sort("price_asc").values_list("base_price", flat=True)
        )
        self.assertEqual(prices, sorted(prices))

    def test_discounted_manager(self):
        Discount.objects.create(
            name="تخفیف",
            product=self.live,
            discount_type=Discount.DiscountType.PERCENT,
            value=10,
            is_active=True,
        )
        self.assertEqual(Product.objects.discounted().count(), 1)


class CategoryTests(TestCase):
    def test_slug_is_generated_from_name(self):
        category = Category.objects.create(name="کفش ورزشی")
        self.assertTrue(category.slug)

    def test_product_count_property(self):
        category = Category.objects.create(name="شلوار")
        Product.objects.create(
            name="شلوار جین", category=category, base_price=1000, is_published=True
        )
        self.assertEqual(category.product_count, 1)


class VariantTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="تی‌شرت")
        self.product = Product.objects.create(
            name="تی‌شرت",
            category=self.category,
            base_price=500000,
            is_published=True,
            is_active=True,
        )
        self.color = Color.objects.create(name="مشکی")
        self.size = Size.objects.create(name="M")

    def test_unique_variant_per_color_size(self):
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=5
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductVariant.objects.create(
                    product=self.product, color=self.color, size=self.size, stock=5
                )

    def test_negative_stock_is_rejected_by_check_constraint(self):
        """موجودی منفی نباید هرگز وارد دیتابیس شود (شرط SQL + محدودیت فیلد)."""
        variant = ProductVariant(
            product=self.product, color=self.color, size=self.size, stock=-1
        )
        with self.assertRaises((IntegrityError, DataError)):
            with transaction.atomic():
                variant.save()

    def test_full_clean_rejects_negative_base_price(self):
        product = Product(
            name="محصول منفی", category=self.category, base_price=-1000
        )
        with self.assertRaises(ValidationError):
            product.full_clean()

    def test_stock_quantity_sums_variants(self):
        size_l = Size.objects.create(name="L")
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=3
        )
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=size_l, stock=4
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)
        self.assertTrue(self.product.is_in_stock)

    def test_out_of_stock_product(self):
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=0
        )
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_in_stock)

    def test_variant_is_priced_from_product_not_stored(self):
        variant = ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=2
        )
        Discount.objects.create(
            name="تخفیف",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=20,
            is_active=True,
        )
        self.assertEqual(variant.product.final_price, Decimal("400000"))


class ProductModelTests(TestCase):
    def test_is_live_flag(self):
        category = Category.objects.create(name="کاپشن")
        product = Product.objects.create(
            name="کاپشن", category=category, base_price=1000, is_published=True
        )
        self.assertTrue(product.is_live)
        product.is_published = False
        product.save()
        self.assertFalse(product.is_live)

    def test_is_archived_flag(self):
        category = Category.objects.create(name="کلاه")
        product = Product.objects.create(
            name="کلاه", category=category, base_price=1000, is_published=True
        )
        self.assertFalse(product.is_archived)
        product.archived_at = timezone.now()
        product.save()
        self.assertTrue(product.is_archived)
