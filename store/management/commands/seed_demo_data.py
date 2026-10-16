"""
ساخت داده‌های نمونه واقعی برای فروشگاه (Development / Demo).

اجرا:
    uv run python manage.py seed_demo_data
    uv run python manage.py seed_demo_data --flush
    uv run python manage.py seed_demo_data --no-images

آنچه ساخته می‌شود:
  • دسته‌بندی‌ها، رنگ‌ها و سایزها
  • محصولات واقعی با واریانت (رنگ + سایز) و موجودی
  • تصاویر Placeholder واقعی (تولیدشده با Pillow)
  • چند تخفیف معتبر
  • سوپروزر پنل مدیریت + کاربر نمونه
  • دو سفارش نمونه (یکی پرداخت‌شده، یکی در انتظار پرداخت)
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

# ---------------------------------------------------------------------
# داده‌های ثابت دمو
# ---------------------------------------------------------------------
CATEGORIES: list[tuple[str, str | None]] = [
    ("مردانه", None),
    ("پیراهن مردانه", "مردانه"),
    ("تی‌شرت مردانه", "مردانه"),
    ("شلوار مردانه", "مردانه"),
    ("کاپشن و هودی مردانه", "مردانه"),
    ("زنانه", None),
    ("بلوز و مانتو زنانه", "زنانه"),
    ("شلوار زنانه", "زنانه"),
    ("کفش زنانه", "زنانه"),
    ("بچگانه", None),
    ("تی‌شرت بچگانه", "بچگانه"),
    ("اکسسوری", None),
    ("کلاه و شال", "اکسسوری"),
]

COLORS: list[tuple[str, str]] = [
    ("مشکی", "#141414"),
    ("سفید", "#f5f5f0"),
    ("طوسی", "#8b8b8b"),
    ("سرمه‌ای", "#1f3a5f"),
    ("کرم", "#e7d9bd"),
    ("زرشکی", "#7d2230"),
    ("سبز زیتونی", "#5b6b3a"),
    ("آبی آسمانی", "#7fb3d5"),
]

APPAREL_SIZES = ["S", "M", "L", "XL"]
SHOE_SIZES = ["39", "40", "41", "42", "43"]

# (نام محصول، نام دسته، قیمت پایه، توضیح، رنگ‌های مجاز، سایزها، منتخب؟)
PRODUCTS: list[tuple] = [
    (
        "پیراهن مردانه کلاسیک آستین بلند",
        "پیراهن مردانه",
        1_450_000,
        "پیراهن مردانه از پارچه نخی درجه یک با دوخت تمیز و یقه کلاسیک. مناسب استفاده روزمره و مجالس رسمی؛ قابل شستشو در ماشین لباسشویی و اتوپذیر.",
        ["سفید", "سرمه‌ای", "طوسی"],
        APPAREL_SIZES,
        True,
    ),
    (
        "پیراهن جین مردانه",
        "پیراهن مردانه",
        1_290_000,
        "پیراهن جین با پارچه کتان مقاوم، مناسب فصل بهار و پاییز. جیب سینه‌ای و دکمه‌های فلزی.",
        ["آبی آسمانی", "سرمه‌ای"],
        APPAREL_SIZES,
        False,
    ),
    (
        "تی‌شرت مردانه نخی یقه گرد",
        "تی‌شرت مردانه",
        590_000,
        "تی‌شرت ۱۰۰٪ نخی با الیاف تنفس‌پذیر؛ رنگ‌ثابت بعد از شستشو. مناسب استفاده روزمره و ورزش سبک.",
        ["مشکی", "سفید", "طوسی", "سبز زیتونی"],
        APPAREL_SIZES,
        True,
    ),
    (
        "تی‌شرت مردانه طرح‌دار",
        "تی‌شرت مردانه",
        650_000,
        "تی‌شرت طرح‌دار با چاپ سیلک باکیفیت که بعد از شستشو پوسته نمی‌شود.",
        ["مشکی", "زرشکی"],
        APPAREL_SIZES,
        False,
    ),
    (
        "شلوار جین مردانه اسلیم",
        "شلوار مردانه",
        1_850_000,
        "شلوار جین اسلیم‌فیت با کشسانی مناسب و شستشوی آسان؛ فرم بدن را حفظ می‌کند.",
        ["سرمه‌ای", "مشکی", "طوسی"],
        APPAREL_SIZES,
        True,
    ),
    (
        "شلوار کتان مردانه",
        "شلوار مردانه",
        1_390_000,
        "شلوار کتان راحت با بند قابل تنظیم؛ ایده‌آل برای محیط کار و استفاده روزمره.",
        ["کرم", "سبز زیتونی", "سرمه‌ای"],
        APPAREL_SIZES,
        False,
    ),
    (
        "کاپشن مردانه زمستانه",
        "کاپشن و هودی مردانه",
        3_450_000,
        "کاپشن ضدآب با آستر پلار و کلاه جداشونده؛ مناسب دمای زیر صفر درجه.",
        ["مشکی", "سرمه‌ای", "طوسی"],
        APPAREL_SIZES,
        True,
    ),
    (
        "هودی مردانه اورسایز",
        "کاپشن و هودی مردانه",
        1_590_000,
        "هودی اورسایز با پارچه فلیس نرم، کلاه دوبل و جیب کانگورویی.",
        ["طوسی", "مشکی", "کرم"],
        APPAREL_SIZES,
        False,
    ),
    (
        "مانتو زنانه ساده بهاره",
        "بلوز و مانتو زنانه",
        2_650_000,
        "مانتو زنانه با پارچه لطیف و برش مدرن؛ مناسب استفاده اداری و مجلسی.",
        ["کرم", "سرمه‌ای", "زرشکی"],
        APPAREL_SIZES,
        True,
    ),
    (
        "بلوز زنانه ابریشمی",
        "بلوز و مانتو زنانه",
        1_190_000,
        "بلوز زنانه با جنس نرم و درخشش ملایم؛ مناسب مهمانی و استفاده روزمره.",
        ["سفید", "کرم", "زرشکی"],
        APPAREL_SIZES,
        False,
    ),
    (
        "شلوار زنانه راحتی",
        "شلوار زنانه",
        980_000,
        "شلوار زنانه با پارچه کشسان و کمری راحت؛ قابل استفاده در خانه و بیرون.",
        ["مشکی", "طوسی", "کرم"],
        APPAREL_SIZES,
        False,
    ),
    (
        "کفش زنانه اسپرت سبک",
        "کفش زنانه",
        2_890_000,
        "کفش اسپرت زنانه با زیره جاذب ضربه و رویه تنفس‌پذیر؛ مناسب پیاده‌روی طولانی.",
        ["سفید", "کرم", "مشکی"],
        SHOE_SIZES,
        True,
    ),
    (
        "تی‌شرت بچگانه نخی",
        "تی‌شرت بچگانه",
        390_000,
        "تی‌شرت بچگانه از نخ ضدحساسیت با چاپ بدون ضرر برای پوست کودک.",
        ["سفید", "آبی آسمانی", "زرشکی"],
        APPAREL_SIZES,
        False,
    ),
    (
        "شلوار بچگانه کتان",
        "تی‌شرت بچگانه",
        450_000,
        "شلوار بچگانه کتان با دوخت مقاوم و کمر نرم؛ مناسب مدرسه و بازی.",
        ["سرمه‌ای", "طوسی"],
        APPAREL_SIZES,
        False,
    ),
    (
        "کلاه کپ ساده",
        "کلاه و شال",
        350_000,
        "کلاه کپ با قابلیت تنظیم سایز و پارچه مقاوم در برابر نور خورشید.",
        ["مشکی", "کرم", "سبز زیتونی"],
        ["M", "L"],
        False,
    ),
    (
        "شال زنانه بافت",
        "کلاه و شال",
        520_000,
        "شال زنانه بافت با الیاف نرم و غیرحساسیت‌زا؛ مناسب فصل سرد.",
        ["زرشکی", "کرم", "طوسی"],
        ["M", "L"],
        True,
    ),
]

# شماره‌های نمونه (فقط برای محیط توسعه)
ADMIN_PHONE = "09120000000"
ADMIN_PASSWORD = "admin1234"
DEMO_PHONE = "09120000001"


class Command(BaseCommand):
    help = "ساخت داده‌های نمونه (دسته‌بندی، رنگ، سایز، محصول، تصویر، تخفیف، کاربر و سفارش)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="حذف داده‌های قبلی فروشگاه قبل از ساخت داده جدید",
        )
        parser.add_argument(
            "--no-images",
            action="store_true",
            help="بدون تولید تصویر Placeholder",
        )
        parser.add_argument(
            "--admin-password",
            default=ADMIN_PASSWORD,
            help=f"رمز عبور سوپروزر (پیش‌فرض: {ADMIN_PASSWORD})",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        flush = options["flush"]
        with_images = not options["no_images"]
        admin_password = options["admin_password"]

        with transaction.atomic():
            if flush:
                self._flush()

            colors = self._seed_colors()
            sizes = self._seed_sizes()
            categories = self._seed_categories()
            products = self._seed_products(categories, colors, sizes, with_images)

        self._seed_users(admin_password)
        self._seed_orders()

        self.stdout.write(self.style.SUCCESS("\n✓ داده‌های نمونه با موفقیت ساخته شد."))
        self.stdout.write(
            f"  محصولات: {len(products)} | رنگ: {len(colors)} | سایز: {len(sizes)} | "
            f"دسته‌بندی: {len(categories)}"
        )
        self.stdout.write(
            self.style.WARNING(
                f"\n  سوپروزر پنل مدیریت: {ADMIN_PHONE} / رمز: {admin_password}\n"
                f"  کاربر نمونه: {DEMO_PHONE} (ورود با کد OTP از طریق Console)\n"
            )
        )

    # ------------------------------------------------------------------
    def _flush(self):
        from products.models import Category, Color, Discount, Product, Size

        Product.objects.all().delete()  # واریانت‌ها و تصاویر CASCADE می‌شوند
        Discount.objects.all().delete()
        Category.objects.all().delete()
        Color.objects.all().delete()
        Size.objects.all().delete()
        self.stdout.write("داده‌های قبلی فروشگاه پاک شد.")

    # ------------------------------------------------------------------
    def _seed_colors(self):
        from products.models import Color

        colors = {}
        for order, (name, hex_code) in enumerate(COLORS, start=1):
            color, _ = Color.objects.get_or_create(
                name=name, defaults={"hex_code": hex_code, "sort_order": order}
            )
            colors[name] = color
        return colors

    def _seed_sizes(self):
        from products.models import Size

        sizes = {}
        for order, name in enumerate(APPAREL_SIZES + SHOE_SIZES, start=1):
            size, _ = Size.objects.get_or_create(name=name, defaults={"sort_order": order})
            sizes[name] = size
        return sizes

    def _seed_categories(self):
        from products.models import Category

        created: dict[str, Category] = {}
        for order, (name, parent_name) in enumerate(CATEGORIES, start=1):
            category, _ = Category.objects.get_or_create(
                name=name,
                defaults={
                    "parent": created.get(parent_name) if parent_name else None,
                    "sort_order": order,
                    "description": f"محصولات دسته «{name}»",
                },
            )
            created[name] = category
        return created

    # ------------------------------------------------------------------
    def _seed_products(self, categories, colors, sizes, with_images: bool):
        from products.models import Discount, Product, ProductVariant

        products = []
        for index, (
            name,
            category_name,
            price,
            description,
            color_names,
            size_names,
            featured,
        ) in enumerate(PRODUCTS, start=1):
            category = categories[category_name]
            product, created = Product.objects.get_or_create(
                slug=f"product-{index}",
                defaults={
                    "name": name,
                    "category": category,
                    "base_price": Decimal(price),
                    "description": description,
                    "is_published": True,
                    "is_active": True,
                    "is_featured": featured,
                },
            )
            if not created:
                products.append(product)
                continue

            # واریانت‌ها (رنگ × سایز) با موجودی مشخص
            for color_name in color_names:
                for size_index, size_name in enumerate(size_names, start=1):
                    stock = (index + size_index + len(color_name)) % 7
                    ProductVariant.objects.get_or_create(
                        product=product,
                        color=colors[color_name],
                        size=sizes[size_name],
                        defaults={
                            "stock": stock,
                            "is_active": True,
                            "sku": f"SKU-{index:03d}-{color_name[:2]}-{size_name}",
                        },
                    )

            if with_images:
                self._attach_images(product, index, color_names)

            products.append(product)

        # تخفیف برای چند محصول مشخص
        discount_plan = {
            3: (Discount.DiscountType.PERCENT, 15, "تخفیف فصل بهار"),
            7: (Discount.DiscountType.PERCENT, 20, "حراج زمستانه"),
            9: (Discount.DiscountType.PERCENT, 25, "تخفیف ویژه بانوان"),
            13: (Discount.DiscountType.FIXED, 80_000, "تخفیف مبلغی بچگانه"),
            16: (Discount.DiscountType.PERCENT, 10, "تخفیف اکسسوری"),
        }
        for index, (discount_type, value, title) in discount_plan.items():
            if index > len(products):
                continue
            product = products[index - 1]
            Discount.objects.get_or_create(
                product=product,
                defaults={
                    "name": title,
                    "discount_type": discount_type,
                    "value": Decimal(value),
                    "is_active": True,
                },
            )
        return products

    # ------------------------------------------------------------------
    def _attach_images(self, product, index: int, color_names: list[str]):
        """تولید و الحاق تصاویر Placeholder واقعی با Pillow."""
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover
            return

        if product.images.exists():
            return

        base_hex = COLORS[(index - 1) % len(COLORS)][1]
        secondary = COLORS[index % len(COLORS)][1]

        for position in range(2):
            image = Image.new("RGB", (720, 960), base_hex)
            draw = ImageDraw.Draw(image)

            # دایره بزرگ نرم در پس‌زمینه
            draw.ellipse((-160, 480, 560, 1200), fill=secondary)
            # نوار پایین برای عمق بصری
            draw.rectangle((0, 860, 720, 960), fill=secondary)
            # کادر ظریف
            draw.rectangle((24, 24, 696, 936), outline="#ffffff", width=2)

            label = color_names[position % len(color_names)]
            try:
                font_path = (
                    Path(settings.BASE_DIR)
                    / "node_modules"
                    / "vazirmatn"
                    / "fonts"
                    / "ttf"
                    / "Vazirmatn-SemiBold.ttf"
                )
                if font_path.exists():
                    from PIL import ImageFont

                    font = ImageFont.truetype(str(font_path), 44)
                    small = ImageFont.truetype(str(font_path), 28)
                    draw.text(
                        (60, 890), label, font=small, fill="#ffffff", direction="rtl"
                    )
                    draw.multiline_text(
                        (60, 60),
                        product.name,
                        font=font,
                        fill="#ffffff",
                        spacing=12,
                        direction="rtl",
                    )
            except Exception:  # pragma: no cover - در صورت نبود libraqm
                pass

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=86, optimize=True)
            buffer.seek(0)
            product.images.create(
                image=File(buffer, name=f"product-{index}-{position + 1}.jpg"),
                alt_text=f"{product.name} - تصویر {position + 1}",
                is_primary=(position == 0),
                sort_order=position,
            )

    # ------------------------------------------------------------------
    def _seed_users(self, admin_password: str):
        from accounts.models import Address, User

        if not User.objects.filter(phone_number=ADMIN_PHONE).exists():
            User.objects.create_superuser(
                phone_number=ADMIN_PHONE,
                password=admin_password,
                first_name="مدیر",
                last_name="فروشگاه",
            )
            self.stdout.write(f"✓ سوپروزر ساخته شد: {ADMIN_PHONE}")

        demo_user, created = User.objects.get_or_create(
            phone_number=DEMO_PHONE,
            defaults={
                "first_name": "کاربر",
                "last_name": "نمونه",
                "is_phone_verified": True,
            },
        )
        if created:
            self.stdout.write(f"✓ کاربر نمونه ساخته شد: {DEMO_PHONE}")

        if not demo_user.addresses.exists():
            Address.objects.create(
                user=demo_user,
                receiver_name="کاربر نمونه",
                receiver_phone=DEMO_PHONE,
                province="تهران",
                city="تهران",
                address_line="تهران، خیابان ولیعصر، نبش کوچه گل، پلاک ۱۲، واحد ۳",
                postal_code="1234567890",
                is_default=True,
            )

    # ------------------------------------------------------------------
    def _seed_orders(self):
        """دو سفارش نمونه: یکی پرداخت‌شده و یکی در انتظار پرداخت."""
        from accounts.models import Address, User
        from cart.models import Cart, CartItem
        from orders.models import Order
        from orders.services import create_order, mark_order_paid
        from products.models import ProductVariant

        customer = User.objects.filter(phone_number=DEMO_PHONE).first()
        address = Address.objects.filter(user=customer).first()
        if customer is None or address is None:
            return

        if Order.objects.filter(customer=customer).exists():
            return

        rng = random.Random(42)

        # سفارش پرداخت‌شده
        variants = list(ProductVariant.objects.filter(is_active=True, stock__gte=3)[:4])
        if len(variants) >= 3:
            cart = Cart.objects.create(user=customer)
            for variant in variants[:3]:
                CartItem.objects.create(cart=cart, variant=variant, quantity=rng.randint(1, 2))
            paid_order = create_order(
                customer=customer,
                cart=cart,
                address=address,
                customer_note="لطفاً قبل از ارسال تماس بگیرید.",
            )
            mark_order_paid(paid_order)
            paid_order.status = "preparing"
            paid_order.save(update_fields=["status", "updated_at"])
            self.stdout.write(f"✓ سفارش نمونه پرداخت‌شده: {paid_order.order_number}")

        # سفارش در انتظار پرداخت
        pending_variants = list(
            ProductVariant.objects.filter(is_active=True, stock__gte=2).exclude(
                pk__in=[v.pk for v in variants[:3]]
            )[:2]
        )
        if pending_variants:
            cart = Cart.objects.create(user=customer)
            for variant in pending_variants:
                CartItem.objects.create(cart=cart, variant=variant, quantity=1)
            pending_order = create_order(
                customer=customer,
                cart=cart,
                address=address,
                customer_note="",
            )
            self.stdout.write(f"✓ سفارش نمونه در انتظار پرداخت: {pending_order.order_number}")

        # نمایش سفارش‌های منقضی در داشبورد (فقط برای داده‌نمایش)
        Order.objects.filter(customer=customer, status="pending_payment").update(
            expires_at=timezone.now() + timedelta(minutes=25)
        )
