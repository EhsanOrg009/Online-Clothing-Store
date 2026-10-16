"""تست‌های صفحات فروشگاه (Store Front)."""

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from products.models import (
    Category,
    Color,
    Discount,
    Product,
    ProductVariant,
    Size,
)


class StoreFixtureMixin:
    def build_store_data(self):
        self.parent = Category.objects.create(name="مردانه")
        self.category = Category.objects.create(name="پیراهن", parent=self.parent)
        self.color = Color.objects.create(name="مشکی")
        self.size = Size.objects.create(name="M")

        self.product = Product.objects.create(
            name="پیراهن کلاسیک",
            category=self.category,
            base_price=Decimal("800000"),
            description="پیراهن کلاسیک مردانه با پارچه نخی",
            is_published=True,
            is_active=True,
            is_featured=True,
        )
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=5
        )

        self.draft = Product.objects.create(
            name="پیراهن پیش‌نویس",
            category=self.category,
            base_price=Decimal("700000"),
            is_published=False,
            is_active=True,
        )


class HomePageTests(StoreFixtureMixin, TestCase):
    def setUp(self):
        self.build_store_data()
        self.client = Client()

    def test_home_renders(self):
        response = self.client.get(reverse("store:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پیراهن کلاسیک")
        self.assertTemplateUsed(response, "store/home.html")

    def test_home_does_not_show_drafts(self):
        response = self.client.get(reverse("store:home"))
        self.assertNotContains(response, "پیراهن پیش‌نویس")

    def test_home_shows_featured_products(self):
        response = self.client.get(reverse("store:home"))
        self.assertEqual(len(response.context["featured"]), 1)

    def test_home_shows_discounted_products(self):
        Discount.objects.create(
            name="تخفیف ویژه",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=15,
            is_active=True,
        )
        response = self.client.get(reverse("store:home"))
        self.assertEqual(len(response.context["discounted"]), 1)


class ProductListTests(StoreFixtureMixin, TestCase):
    def setUp(self):
        self.build_store_data()
        self.client = Client()

    def test_list_shows_only_live_products(self):
        response = self.client.get(reverse("store:product_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پیراهن کلاسیک")
        self.assertNotContains(response, "پیراهن پیش‌نویس")
        self.assertEqual(response.context["result_count"], 1)

    def test_category_page_filters_children(self):
        response = self.client.get(
            reverse("store:category", args=[self.parent.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result_count"], 1)
        self.assertEqual(response.context["category"].pk, self.parent.pk)

    def test_unknown_category_returns_404(self):
        response = self.client.get(
            reverse("store:category", args=["does-not-exist"])
        )
        self.assertEqual(response.status_code, 404)

    def test_price_range_filter(self):
        Product.objects.create(
            name="گران‌قیمت",
            category=self.category,
            base_price=Decimal("5000000"),
            is_published=True,
        )
        response = self.client.get(
            reverse("store:product_list"), {"min_price": "1000000"}
        )
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["گران‌قیمت"])

    def test_max_price_filter(self):
        Product.objects.create(
            name="ارزان",
            category=self.category,
            base_price=Decimal("100000"),
            is_published=True,
        )
        response = self.client.get(
            reverse("store:product_list"), {"max_price": "200000"}
        )
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["ارزان"])

    def test_discount_only_filter(self):
        Discount.objects.create(
            name="تخفیف",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=10,
            is_active=True,
        )
        response = self.client.get(reverse("store:product_list"), {"discount_only": "on"})
        self.assertEqual(response.context["result_count"], 1)

    def test_in_stock_only_filter(self):
        ProductVariant.objects.filter(product=self.product).update(stock=0)
        response = self.client.get(reverse("store:product_list"), {"in_stock_only": "on"})
        self.assertEqual(response.context["result_count"], 0)

    def test_color_filter(self):
        red = Color.objects.create(name="قرمز")
        other = Product.objects.create(
            name="پیراهن قرمز",
            category=self.category,
            base_price=Decimal("900000"),
            is_published=True,
        )
        ProductVariant.objects.create(
            product=other, color=red, size=self.size, stock=3
        )

        response = self.client.get(
            reverse("store:product_list"), {"colors": [str(self.color.pk)]}
        )
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["پیراهن کلاسیک"])

    def test_size_filter(self):
        xl = Size.objects.create(name="XL")
        other = Product.objects.create(
            name="پیراهن XL",
            category=self.category,
            base_price=Decimal("900000"),
            is_published=True,
        )
        ProductVariant.objects.create(
            product=other, color=self.color, size=xl, stock=3
        )

        response = self.client.get(
            reverse("store:product_list"), {"sizes": [str(xl.pk)]}
        )
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["پیراهن XL"])

    def test_search_filter(self):
        Product.objects.create(
            name="کاپشن زمستانی",
            category=self.category,
            base_price=Decimal("1200000"),
            is_published=True,
        )
        response = self.client.get(reverse("store:product_list"), {"q": "کاپشن"})
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["کاپشن زمستانی"])

    def test_sort_by_price_asc(self):
        Product.objects.create(
            name="ارزان",
            category=self.category,
            base_price=Decimal("100000"),
            is_published=True,
        )
        response = self.client.get(reverse("store:product_list"), {"sort": "price_asc"})
        names = [p.name for p in response.context["page_obj"].object_list]
        self.assertEqual(names, ["ارزان", "پیراهن کلاسیک"])

    def test_invalid_filter_values_are_ignored(self):
        response = self.client.get(
            reverse("store:product_list"), {"min_price": "abc", "colors": ["999999"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result_count"], 1)

    def test_pagination_page_out_of_range_shows_last_page(self):
        response = self.client.get(reverse("store:product_list"), {"page": "999"})
        self.assertEqual(response.status_code, 200)

    def test_pagination_page_is_non_numeric(self):
        response = self.client.get(reverse("store:product_list"), {"page": "abc"})
        self.assertEqual(response.status_code, 200)


class SearchPageTests(StoreFixtureMixin, TestCase):
    def setUp(self):
        self.build_store_data()
        self.client = Client()

    def test_search_renders(self):
        response = self.client.get(reverse("store:search"), {"q": "پیراهن"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پیراهن کلاسیک")

    def test_search_with_empty_query(self):
        response = self.client.get(reverse("store:search"))
        self.assertEqual(response.status_code, 200)

    def test_search_with_no_results(self):
        response = self.client.get(reverse("store:search"), {"q": "چیزی-وجود-ندارد"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "نتیجه‌ای پیدا نشد")

    def test_search_ignores_drafts(self):
        response = self.client.get(reverse("store:search"), {"q": "پیش‌نویس"})
        self.assertNotContains(response, "پیراهن پیش‌نویس")


class CategoryIndexTests(StoreFixtureMixin, TestCase):
    def setUp(self):
        self.build_store_data()
        self.client = Client()

    def test_category_index_renders(self):
        response = self.client.get(reverse("store:categories"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مردانه")

    def test_only_active_categories_are_listed(self):
        response = self.client.get(reverse("store:categories"))
        names = [c.name for c in response.context["categories"]]
        self.assertIn("مردانه", names)
        self.assertIn("پیراهن", names)

        Category.objects.filter(name="پیراهن").update(is_active=False)
        response = self.client.get(reverse("store:categories"))
        names = [c.name for c in response.context["categories"]]
        self.assertNotIn("پیراهن", names)


class ProductDetailTests(StoreFixtureMixin, TestCase):
    def setUp(self):
        self.build_store_data()
        self.client = Client()

    def test_detail_renders(self):
        response = self.client.get(
            reverse("store:product_detail", args=[self.product.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پیراهن کلاسیک")
        self.assertTemplateUsed(response, "store/product_detail.html")

    def test_detail_404_for_unknown_slug(self):
        response = self.client.get(
            reverse("store:product_detail", args=["does-not-exist"])
        )
        self.assertEqual(response.status_code, 404)

    def test_detail_404_for_draft_product(self):
        response = self.client.get(
            reverse("store:product_detail", args=[self.draft.slug])
        )
        self.assertEqual(response.status_code, 404)

    def test_variants_are_provided_as_json(self):
        response = self.client.get(
            reverse("store:product_detail", args=[self.product.slug])
        )
        import json

        variants = json.loads(response.context["variants_json"])
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["stock"], 5)
        self.assertEqual(variants[0]["color"], "مشکی")
        self.assertEqual(variants[0]["size"], "M")

    def test_stock_quantity_is_summed_across_variants(self):
        Size.objects.create(name="L")
        ProductVariant.objects.create(
            product=self.product, color=self.color, size=Size.objects.get(name="L"), stock=2
        )
        response = self.client.get(
            reverse("store:product_detail", args=[self.product.slug])
        )
        self.assertEqual(response.context["product"].stock_quantity, 7)

    def test_detail_shows_discounted_price(self):
        Discount.objects.create(
            name="تخفیف",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=25,
            is_active=True,
        )
        response = self.client.get(
            reverse("store:product_detail", args=[self.product.slug])
        )
        self.assertContains(response, "۶۰۰,۰۰۰")
        self.assertContains(response, "۲۵٪")

    def test_out_of_stock_product_shows_message(self):
        ProductVariant.objects.filter(product=self.product).update(stock=0)
        response = self.client.get(
            reverse("store:product_detail", args=[self.product.slug])
        )
        self.assertContains(response, "ناموجود")

    def test_persian_slug_url_is_supported(self):
        fa_product = Product.objects.create(
            name="شلوار جین آبی",
            category=self.category,
            base_price=Decimal("600000"),
            is_published=True,
        )
        self.assertTrue(fa_product.slug)
        response = self.client.get(
            reverse("store:product_detail", args=[fa_product.slug])
        )
        self.assertEqual(response.status_code, 200)


class SecurityHeadersTests(TestCase):
    """هدرهای امنیتی باید روی همه پاسخ‌ها اعمال شوند."""

    def setUp(self):
        self.client = Client()

    def test_security_headers_present(self):
        response = self.client.get(reverse("store:home"))
        self.assertIn("X-Frame-Options", response.headers)
        self.assertIn("X-Content-Type-Options", response.headers)
        self.assertIn("Referrer-Policy", response.headers)
        self.assertIn("Content-Security-Policy", response.headers)

    def test_csp_disallows_inline_scripts(self):
        response = self.client.get(reverse("store:home"))
        csp = response.headers["Content-Security-Policy"]

        script_src = next(
            part for part in csp.split(";") if part.strip().startswith("script-src")
        )
        # اسکریپت Inline و Handler های Inline ممنوع‌اند (Nonce جایگزین شده)
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertIn("'nonce-", script_src)
        # Alpine.js به `new Function` نیاز دارد؛ فقط unsafe-eval مجاز است
        self.assertIn("'unsafe-eval'", script_src)

    def test_csp_disallows_inline_event_handlers(self):
        response = self.client.get(reverse("store:home"))
        body = response.content.decode()
        # هیچ Inline Event Handler ای نباید در قالب‌ها باشد
        for handler in ("onclick=", "onsubmit=", "onchange=", "onload="):
            self.assertNotIn(handler, body)

    def test_404_page_uses_custom_template(self):
        response = self.client.get("/this-page-does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "errors/404.html")

    def test_403_page(self):
        response = self.client.get("/accounts/profile/")
        self.assertEqual(response.status_code, 302)
