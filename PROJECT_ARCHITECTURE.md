# معماری پروژه — فروشگاه اینترنتی پوشاک

مستندات فنی معماری، تصمیم‌های طراحی و نقاط گسترش پروژه.

---

## فهرست

1. [نمای کلی](#نمای-کلی)
2. [لایه‌بندی و جریان داده](#لایهبندی-و-جریان-داده)
3. [پیکربندی](#پیکربندی)
4. [مدل داده](#مدل-داده)
5. [گردش‌کار خرید](#گردشکار-خرید)
6. [احراز هویت با OTP](#احراز-هویت-با-otp)
7. [پرداخت](#پرداخت)
8. [جلوگیری از Overselling](#جلوگیری-از-overselling)
9. [قیمت‌گذاری](#قیمتگذاری)
10. [لایه‌ی رابط کاربری](#لایهی-رابط-کاربری)
11. [امنیت](#امنیت)
12. [پنل مدیریت](#پنل-مدیریت)
13. [تست](#تست)
14. [زیرساخت Docker](#زیرساخت-docker)
15. [نکات گسترش](#نکات-گسترش)

---

## نمای کلی

پروژه یک فروشگاه اینترنتی پوشاک با Backend واقعی است. تأکید اصلی روی **درستی داده و امنیت تراکنش** است، نه فقط نمایش صفحات.

### تصمیم‌های بنیادین

| تصمیم | دلیل |
| --- | --- |
| Django به‌جای SPA | پیچیدگی کمتر، SEO بهتر، امنیت CSRF و Template Sandbox رایگان |
| HTMX + Alpine.js به‌جای React | تعامل پویا بدون حمل بسته‌ی JS سنگین و بدون پیچیدگی Build |
| `Decimal` به‌جای `float` | خطای اعشاری در مبالغ پولی غیرقابل قبول است |
| محاسبه‌ی قیمت فقط در Backend | Client هرگز منبع حقیقت مبلغ نیست |
| موجودی در سطح واریانت | پوشاک بدون رنگ/سایز معنا ندارد |
| Snapshot در `OrderItem` | قیمت و نام محصول در لحظه‌ی خرید باید تغییرناپذیر باشد |
| پرداخت Redirect-Based | اطلاعات کارت فقط روی درگاه رسمی وارد می‌شود |
| احراز هویت با OTP | بدون رمز عبور؛ بدون مشکل نشت رمز |
| `Repository/Service` در لایه‌ی دامنه | View ها منطق کسب‌وکار ندارند؛ فقط HTTP را مدیریت می‌کنند |

---

## لایه‌بندی و جریان داده

```
┌──────────────────────────────────────────────────────────────┐
│  مرورگر  (HTMX + Alpine.js + Tailwind)                       │
└──────────────┬───────────────────────────────────────────────┘
               │  HTTP
┌──────────────▼───────────────────────────────────────────────┐
│  Middleware                                                   │
│   Security → CSP → WhiteNoise → SecurityHeaders → Session     │
│   → CSRF → Auth → Messages → XFrameOptions                    │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│  View  (فقط HTTP: اعتبارسنجی Form، Message، Redirect/Render) │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│  Service  (منطق کسب‌وکار + Transaction + Row Lock)            │
│   cart/services.py   orders/services.py   payments/services   │
│   accounts/otp.py    products/services/pricing.py             │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│  Model / Manager  (QuerySet، شرط SQL، محدودیت‌ها)             │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│  MySQL / MariaDB  (utf8mb4_unicode_ci)                        │
└──────────────────────────────────────────────────────────────┘
```

**قاعده:** View هرگز `Model.objects` صدا نمی‌زند. هر عملیات دامنه از یک تابع Service صدا زده می‌شود. این کار تست‌پذیری و بازبینی امنیتی را ساده می‌کند.

---

## پیکربندی

سه فایل تنظیمات در `config/settings/`:

| فایل | نقش |
| --- | --- |
| `base.py` | تنظیمات مشترک؛ همه‌ی مقادیر از Environment خوانده می‌شوند |
| `development.py` | `DEBUG=True`، Secret پیش‌فرض، WhiteNoise با AutoRefresh |
| `production.py` | `DEBUG=False`، اجبار HTTPS، لاگ در فایل، **Fail-Fast در صورت پیکربندی ضعیف** |

`production.py` در صورت ضعیف‌بودن `SECRET_KEY`، خالی‌بودن `ALLOWED_HOSTS` یا رمز پیش‌فرض دیتابیس، با `RuntimeError` بالا آمدن را متوقف می‌کند. این عمدی است: بهتر است استقرار متوقف شود تا سایت ناامن بالا بیاید.

### ترتیب Middleware

ترتیب مهم است:

1. `SecurityMiddleware` — هدرهای امنیتی پایه
2. `ContentSecurityPolicyMiddleware` — CSP با تولید Nonce
3. `WhiteNoiseMiddleware` — سرو Static
4. `SecurityHeadersMiddleware` (سفارشی) — هدرهای تکمیلی
5. سپس Session → CSRF → Auth → Messages

CSP باید بعد از `SecurityMiddleware` و قبل از هر Middleware ای که Response را Render می‌کند بیاید تا Nonce به همه‌ی Template ها برسد.

---

## مدل داده

### `accounts`

```
User  (AbstractUser سفارشی)
├── phone_number     شماره موبایل؛ شناسه‌ی اصلی ورود، Unique
├── is_phone_verified  آیا OTP تأیید شده
└── first_name / last_name / is_active

Address
├── user → User
├── receiver_name / receiver_phone
├── province / city / address_line / postal_code
└── is_default

OTPChallenge
├── phone_number
├── code_hash        فقط Hash؛ کد خام هرگز ذخیره نمی‌شود
├── expires_at / created_at
├── attempts         شمارنده‌ی تلاش برای محافظت Brute-Force
└── is_used / used_at
```

### `products`

```
Category  (سلسله‌مراتبی با self-FK)
├── name / slug (فارسی) / parent / is_active / sort_order / image
└── product_count (محاسبه‌شده)

Product
├── name / slug / description
├── category → Category         PROTECT
├── base_price  Decimal         قیمت پایه؛ بدون تخفیف
├── is_published / is_active / is_featured / archived_at
└── price_info (محاسبه‌شده از pricing.py)

ProductImage
├── product → Product
└── image / alt_text / is_primary / sort_order

Color / Size
└── name / slug / is_active (+ hex_code برای Color)

ProductVariant          ★ نقطه‌ی کلیدی موجودی
├── product → Product
├── color → Color
├── size  → Size
├── stock  Integer     مقدار غیرمنفی با Check Constraint
├── sku / is_active
└── UNIQUE (product, color, size)

Discount
├── product → Product  OneToOne
├── discount_type      PERCENT | FIXED
├── value  Decimal
├── is_active
└── starts_at / ends_at
```

### `cart`

```
Cart
├── user → User        nullable؛ برای مهمان از Session
├── session_key        nullable؛ برای مهمان
└── created_at / updated_at

CartItem
├── cart → Cart
├── variant → ProductVariant
└── quantity  (با Check Constraint: مقدار ≥ 1)
```

> در سبد خرید **هیچ قیمتی ذخیره نمی‌شود**. `line_total` و `line_discount` محاسبه‌شده‌اند و همیشه از `Product.price_info` لحظه‌ای خوانده می‌شوند. این باعث می‌شود تغییر قیمت یا تخفیف بلافاصله در سبد اعمال شود.

### `orders`

```
Order                     ★ Snapshot تغییرناپذیر
├── order_number          ORD-YYYYMMDD-XXXXXX
├── customer → User
├── status                وضعیت چرخه‌ی عمر
├── payment_status        وضعیت پرداخت (جدا از وضعیت سفارش)
│
├── subtotal / discount_total / shipping_fee / total   Decimal
│
├── receiver_name / receiver_phone
├── province / city / address_line / postal_code       ★ Snapshot آدرس
├── shipping_method
├── customer_note
├── tracking_code / admin_note
│
├── expires_at            مهلت پرداخت
├── paid_at / shipped_at / delivered_at / cancelled_at
└── created_at / updated_at

OrderItem                 ★ Snapshot قیمت و نام
├── order → Order
├── variant → ProductVariant    nullable (اگر واریانت حذف شود)
├── product_name / color_name / size_name
├── original_unit_price / unit_price   Decimal
├── discount_amount / quantity
└── line_total (محاسبه‌شده)
```

**چرا Snapshot؟** اگر قیمت یا نام محصول بعد از سفارش تغییر کند، صفحه‌ی سفارش کاربر و فاکتور نباید تغییر کنند. قیمتی که مشتری پرداخت کرده باید تا ابد همان باشد.

### `payments`

```
Payment
├── order → Order
├── gateway              نام درگاه (mock / zarinpal / ...)
├── amount  Decimal      ★ از order.total؛ هرگز از Client
├── status               PENDING | SUCCESSFUL | FAILED | CANCELLED | EXPIRED
├── authority            شناسه‌ی تراکنش نزد درگاه
├── transaction_id / reference_id
├── metadata (JSON)      شامل redirect_url
├── verified_at / paid_at
├── callback_count       شمارنده‌ی فراخوانی‌های Callback
└── error_message
```

> **هیچ PAN، CVV2، رمز پویا یا شماره‌ی کارتی در هیچ کجای این سیستم وجود ندارد.**

---

## گردش‌کار خرید

```
  صفحه محصول
       │  POST /cart/add/   {variant_id, quantity}
       ▼
  ┌──────────────────────────────────────────┐
  │ cart/services.add_item                   │
  │  ۱) بررسی is_live / is_active / موجودی    │
  │  ۲) SELECT ... FOR UPDATE روی ردیف سبد    │
  │  ۳) جمع تعداد با سقف سقف‌ها              │
  └──────────────┬───────────────────────────┘
                 ▼
  سبد خرید  ──►  Checkout (انتخاب آدرس)
                 │  POST /checkout/review/
                 ▼
  ┌──────────────────────────────────────────┐
  │ orders/services.create_order             │
  │  @transaction.atomic                     │
  │   برای هر آیتم:                           │
  │     ۱) SELECT ... FOR UPDATE روی واریانت │
  │     ۲) بررسی is_live / موجودی / سقف       │
  │     ۳) محاسبه‌ی قیمت لحظه‌ای (Backend)    │
  │     ۴) decrement_stock با شرط SQL          │
  │     ۵) ساخت OrderItem با Snapshot         │
  │   سپس: محاسبه‌ی جمع + ارسال + ساخت آدرس    │
  │   در پایان: خالی‌کردن سبد                  │
  └──────────────┬───────────────────────────┘
                 ▼
       Redirect به /payments/redirect/<pk>/
                 │
                 ▼
  ┌──────────────────────────────────────────┐
  │ payments/services.create_payment_for_order│
  │  ۱) قفل سفارش + بررسی is_paid / is_expired│
  │  ۲) gateway.create_payment(order)          │
  │  ۳) ساخت Payment با amount = order.total   │
  └──────────────┬───────────────────────────┘
                 ▼
  صفحه‌ی Redirect  →  درگاه واقعی  →  Callback
```

### وضعیت‌های سفارش و گذارها

```
pending_payment ──► paid ──► preparing ──► ready_to_ship ──► shipped ──► delivered
       │            ▲
       │            └── (از طریق Verify موفق)
       ├──► payment_failed ──► pending_payment   (تلاش مجدد)
       └──► cancelled
```

نقشه‌ی گذار در `orders/choices.py::ALLOWED_ORDER_TRANSITIONS` نگهداری می‌شود. `transition_status` هر گذار خارج از این نقشه را با `OrderError` رد می‌کند. سفارش `shipped` یا `delivered` دیگر قابل لغو نیست.

> `payment_status` و `status` عمداً جدا هستند: سفارش می‌تواند از نظر چرخه‌ی عمر در وضعیت `pending_payment` باشد ولی پرداختش `failed` باشد. این جداسازی از قاطی‌شدن دو مفهوم جلوگیری می‌کند.

### انقضای سفارش

سفارش‌های `pending_payment` که از `expires_at` گذشته‌اند، با دستور زیر لغو و موجودی‌شان آزاد می‌شود:

```bash
uv run python manage.py expire_pending_orders
```

این دستور باید با cron هر چند دقیقه اجرا شود (جزئیات در README).

---

## احراز هویت با OTP

### جریان

```
  /accounts/login/        ورود شماره موبایل
        │  POST
        ▼
  request_otp(phone, ip)
   ├─ نرمال‌سازی شماره (ارقام فارسی/عربی، +98، 0098، بدون صفر)
   ├─ اعتبارسنجی الگوی موبایل ایران (فقط 09XXXXXXXXXX)
   ├─ Rate Limit بر اساس IP        (30 درخواست در ساعت)
   ├─ فاصله‌ی اجباری بین ارسال‌ها   (OTP_RESEND_SECONDS)
   ├─ سقف درخواست برای هر شماره    (OTP_RESEND_LIMIT_PER_HOUR)
   ├─ باطل‌کردن کدهای قبلیِ مصرف‌نشده
   ├─ ساخت OTPChallenge با HMAC-SHA256 کد
   └─ ارسال از طریق SMS Provider فعال
        │
        ▼
  /accounts/verify/       ورود کد
        │  POST
        ▼
  verify_otp(phone, code, ip)
   ├─ Rate Limit بر اساس IP        (60 تلاش در ۱۵ دقیقه)
   ├─ SELECT ... FOR UPDATE روی چالش
   ├─ بررسی انقضا
   ├─ بررسی سقف تلاش
   ├─ مقایسه‌ی Hash با hmac.compare_digest   ← بدون Timing Attack
   └─ در صورت موفقیت: مصرف یک‌باره + ساخت/فعال‌سازی کاربر
```

### نکات امنیتی

**کد خام هرگز ذخیره نمی‌شود.** فقط `HMAC-SHA256(phone + ":" + code, SECRET_KEY)` نگهداری می‌شود. اگر دیتابیس لو برود، کدها قابل بازیابی نیستند.

**مقایسه‌ی Timing-Safe.** از `hmac.compare_digest` استفاده شده، نه `==`. مقایسه‌ی معمولی رشته درباره‌ی محل اختلاف اول کلمه اطلاعات لو می‌دهد.

**نکته‌ی پیاده‌سازی مهم (که با تست پیدا شد):**

```python
# ❌ غلط — Exception داخل atomic باعث Rollback می‌شود
with transaction.atomic():
    challenge.attempts += 1
    challenge.save()
    raise OTPError("کد اشتباه است.")   # ذخیره Rollback می‌شود!

# ✅ درست — خطا بعد از اتمیک پرتاب می‌شود
failure = None
with transaction.atomic():
    challenge.attempts += 1
    challenge.save()
    failure = OTPError("کد اشتباه است.")
if failure is not None:
    raise failure
```

این الگو در `accounts/otp.py` رعایت شده. بدون آن، **همه‌ی تلاش‌های اشتباه Rollback می‌شد و محافظت Brute-Force عملاً کار نمی‌کرد.**

### ادغام سبد مهمان

پس از ورود موفق، `merge_session_cart` سبد Session را به کاربر منتقل می‌کند و تعداد آیتم‌های تکراری را جمع می‌زند. این باعث می‌شود کاربری که قبل از ورود محصولی را در سبد گذاشته، چیزی از دست ندهد.

### معماری Provider پیامک

```
accounts/sms/
├── base.py       SMSProvider (ABC) + SMSProviderError
├── console.py    چاپ کد در کنسول  (Development)
├── memory.py     نگهداری در حافظه (تست)
└── registry.py   انتخاب بر اساس SMS_PROVIDER
```

برای اتصال سرویس واقعی، کافی است کلاسی با همان Interface بسازید و نامش را در `.env` بگذارید. هسته‌ی OTP تغییری نمی‌کند.

اگر مسیر کلاس اشتباه باشد، `SMSProviderError` با پیام روشن پرتاب می‌شود (نه `ImportError` خام که به خطای 500 تبدیل می‌شد).

---

## پرداخت

### معماری

```
payments/gateways/
├── base.py       PaymentGateway (ABC)
│                 ├── create_payment(order, request) -> PaymentRequest
│                 ├── verify_payment(payment, callback_data) -> VerificationResult
│                 └── get_payment_url(payment) -> str
├── mock.py       درگاه Mock برای توسعه
└── registry.py   انتخاب بر اساس PAYMENT_GATEWAY
```

### جریان کامل

```
  create_payment_for_order(order)
   ├─ قفل سفارش
   ├─ رد اگر: پرداخت‌شده / منقضی / قابل‌پرداخت‌نیست / بدون آیتم
   ├─ gateway.create_payment() → authority + redirect_url
   └─ ساخت Payment با amount = order.total  ★ از Backend

  صفحه‌ی Redirect
   └─ <meta http-equiv="refresh" content="0; url=...">   ★ بدون JavaScript

  payment_callback(request)
   ├─ خواندن authority از Query
   ├─ یافتن Payment (بدون اعتماد به status/sig)
   └─ process_callback(payment, callback_data)
        ├─ افزایش callback_count
        ├─ ⛔ Idempotency: اگر قبلاً SUCCESSFUL/CANCELLED/EXPIRED → بازگشت
        ├─ gateway.verify_payment()      ★ تأیید سمت سرور
        │    ├─ مقایسه‌ی HMAC امضا با hmac.compare_digest
        │    └─ مقایسه‌ی payment.amount با order.total
        └─ در صورت موفقیت: mark_order_paid (Idempotent)
```

### سه لایه‌ی دفاعی

**۱) امضای HMAC.** درگاه Mock امضای `result|authority|status` را با `PAYMENT_SECRET` می‌سازد. بدون امضای معتبر، `verify_payment` نتیجه را `FAILED` برمی‌گرداند.

**۲) تأیید سمت سرور.** پارامترهای Query به‌تنهایی هیچ معنایی ندارند. حتی اگر مهاجم `status=successful` بفرستد، بدون امضای درست نادیده گرفته می‌شود. این با تست `test_tampered_status_is_rejected` پوشش داده شده.

**۳) Idempotency.** ارسال دوباره‌ی Callback (کلیک مجدد، Refresh، بازگشت مرورگر) باعث پردازش مجدد نمی‌شود: وضعیت تغییر نمی‌کند، `mark_order_paid` دوباره اجرا نمی‌شود و **موجودی دوبار کم نمی‌شود**.

> توجه: افزایش `callback_count` عمداً **قبل** از بررسی Idempotency انجام می‌شود تا تعداد واقعی فراخوانی‌ها برای حسابرسی ثبت شود، در حالی که اثر عملیاتی ندارد.

### گسترش به درگاه واقعی

کافی است کلاسی از `PaymentGateway` بسازید و در `.env` مسیرش را بگذارید. هسته‌ی سفارش و پرداخت تغییری نمی‌کند. جزئیات در README.

---

## جلوگیری از Overselling

این مهم‌ترین بخش درستی سیستم است. سه لایه‌ی دفاعی به‌صورت هم‌زمان:

### لایه‌ی ۱: Row Lock

```python
variant = (
    ProductVariant.objects.select_for_update()
    .select_related("product", "color", "size")
    .filter(pk=item.variant_id, is_active=True)
    .first()
)
```

`SELECT ... FOR UPDATE` ردیف را تا پایان Transaction قفل می‌کند. درخواست دوم همین واریانت تا انتهای تراکنش اول منتظر می‌ماند.

### لایه‌ی ۲: Transaction

کل عملیات ثبت سفارش در `@transaction.atomic` است. اگر حتی یک آیتم شکست بخورد، **همه‌ی کاهش‌های موجودی قبلی Rollback می‌شوند**.

### لایه‌ی ۳: شرط SQL

```python
def decrement_stock(self, quantity: int) -> bool:
    updated = ProductVariant.objects.filter(
        pk=self.pk, is_active=True, stock__gte=quantity   # ★ شرط در SQL
    ).update(stock=models.F("stock") - quantity)
    return bool(updated)
```

کاهش موجودی به‌صورت **یک Statement اتمیک** با شرط `stock__gte` انجام می‌شود. حتی اگر به هر دلیلی Lock از دست برود، دیتابیس اجازه‌ی منفی‌شدن موجودی را نمی‌دهد.

> **نکته‌ی پیاده‌سازی (که با تست پیدا شد):**
> ```python
> # ❌ غلط
> cart.items.select_for_update().get_or_create(variant=v, defaults={...})
> ```
> `select_for_update()` روی یک Related Manager یک **QuerySet** برمی‌گرداند، نه Manager. `get_or_create` روی QuerySet فیلد `cart` را ست نمی‌کند و خطای `Column 'cart_id' cannot be null` می‌دهد. راه‌حل: قفل و ساخت را جداگانه مدیریت کنید.

### آزادسازی موجودی

موجودی در سه حالت برمی‌گردد:

| رویداد | تابع |
| --- | --- |
| لغو سفارش توسط کاربر/ادمین | `cancel_order` |
| انقضای مهلت پرداخت | `expire_due_orders` |
| لغوشدن از مسیر Admin | `cancel_order` |

`_restore_stock` فقط برای سفارش‌هایی اجرا می‌شود که واقعاً از حالت انتظار خارج شده‌اند، و `cancel_order` Idempotent است — فراخوانی دوباره موجودی را دو بار برنمی‌گرداند.

---

## قیمت‌گذاری

`products/services/pricing.py` تنها منبع حقیقت قیمت است:

```python
@dataclass
class PriceInfo:
    base_price: Decimal        # قیمت پایه
    final_price: Decimal       # قیمت نهایی
    discount_amount: Decimal   # مبلغ تخفیف
    discount_percent: int      # درصد تخفیف (نمایشی)
    has_discount: bool
    saved_amount: Decimal      # مبلغ صرفه‌جویی
```

### قوانین

- تخفیف فقط اگر `is_active` و داخل بازه‌ی `starts_at` تا `ends_at` باشد اعمال می‌شود
- تخفیف درصدی: `final = base × (1 - percent/100)`
- تخفیف ثابت: `final = base - value`
- **قیمت هرگز منفی نمی‌شود:** `final = max(0, ...)`
- `discount_percent` فقط برای نمایش است؛ محاسبه‌ی مالی با `discount_amount` انجام می‌شود

### چرا `Decimal`؟

```python
>>> 0.1 + 0.2
0.30000000000000004      # ← خطای Binary Floating Point

>>> Decimal("0.1") + Decimal("0.2")
Decimal("0.3")           # ← درست
```

برای مبالغ پولی این خطاها قابل قبول نیستند. همه‌ی فیلدهای مالی از نوع `DecimalField` هستند و `PRICE_FIELDS` در `orders/models.py` برای تبدیل امن استفاده می‌شود.

---

## لایه‌ی رابط کاربری

### طراحی

- **Mobile-First** — ابتدا برای موبایل طراحی شده، سپس برای دسکتاپ
- **RTL کامل** — `dir="rtl"` در تگ `<html>` و استفاده از Logical Properties
- **فونت وزیرمتن** — با `font-display: swap` و Preload
- **Design System** — توکن‌های رنگ/فاصله/گردی در `assets/css/app.css`

### Tailwind CSS v4

```css
/* ✅ v4: با @utility */
@utility btn { ... }
@utility btn-primary { ... }
@utility rounded-card { ... }

/* ❌ v4: @layer components کار نمی‌کند */
@layer components { .btn { ... } }
```

یک نکته‌ی مهم: کلاس `group` را نمی‌توان با `@apply` استفاده کرد؛ باید مستقیماً در HTML اضافه شود.

### HTMX

بروزرسانی بخشی از صفحه بدون Reload:

```
POST /cart/update/<id>/   با هدر HX-Request: true
       │
       ▼
  View تشخیص می‌دهد درخواست HTMX است
       │
       ▼
  Render فقط partial (partials/cart_content.html)
       به‌جای کل صفحه
```

`hx-target="#cart-content"` باعث می‌شود HTMX پاسخ را فقط در همان بخش جایگزین کند.

### Alpine.js و CSP

Alpine.js برای ارزیابی عبارات `x-data` و `@click` از `new Function` استفاده می‌کند. این با `unsafe-inline` در `script-src` سازگار نیست.

**راه‌حل:** `'unsafe-eval'` فقط در `script-src` مجاز است، ولی `'unsafe-inline'` ممنوع. همه‌ی اسکریپت‌های خارجی Nonce معتبر دارند.

**نتیجه‌ی عملی:** نمی‌توان از `onclick=`، `onsubmit=` یا `onchange=` استفاده کرد. به‌جای آن:

| به‌جای | استفاده شده |
| --- | --- |
| `onchange="..."` | `hx-post` + `hx-target` |
| `onsubmit="..."` | `data-confirm` + Delegate اسکریپت در `base.html` |
| `onclick="step(1)"` | `data-qty-step="1"` + Delegate اسکریپت |

این الگو هم CSP را رعایت می‌کند هم منطق را از HTML جدا نگه می‌دارد. تست `test_csp_disallows_inline_event_handlers` این را تضمین می‌کند.

---

## امنیت

### خلاصه‌ی کنترل‌ها

| تهدید | کنترل |
| --- | --- |
| XSS | CSP با Nonce، `unsafe-inline` ممنوع، autoescaping قالب‌ها |
| CSRF | Middleware CSRF + توکن در همه‌ی فرم‌ها |
| Clickjacking | `X-Frame-Options: DENY` + `frame-ancestors 'none'` |
| Session Hijacking | `SESSION_COOKIE_HTTPONLY`، `Secure` در Production |
| Brute-Force OTP | شمارنده‌ی تلاش + Rate Limiting بر IP و شماره |
| Timing Attack | `hmac.compare_digest` در OTP و امضا |
| Overselling | Transaction + Row Lock + شرط SQL |
| دستکاری مبلغ | محاسبه‌ی فقط در Backend + تأیید در Verify |
| Replay Callback | Idempotency + امضای HMAC |
| نشت Secret | همه‌ی مقادیر در Environment، `.env` در `.gitignore` |
| Path Traversal | Django Storage مسیرها را نرمال می‌کند |
| Open Redirect | اعتبارسنجی `next` فقط برای مسیرهای نسبی |

### Security Headers

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-...' 'unsafe-eval';
                         object-src 'none'; frame-ancestors 'none';
                         base-uri 'self'; form-action 'self'; ...
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
```

### اعتبارسنجی ورودی

اعتبارسنجی در **دو لایه** انجام می‌شود:

1. **Form** — تجربه‌ی کاربری: پیام فارسی، جلوگیری از Submit
2. **Service** — امنیت: View هرگز منبع حقیقت نیست

هیچ View ای بدون عبور از Form اعتبارسنجی، ورودی را قبول نمی‌کند. `common/validators.py` اعتبارسنجی‌های مشترک (شماره موبایل، کد پستی، فایل تصویر) را نگه می‌دارد.

### محدودیت آپلود تصویر

`validate_image_file` فقط MIME های مجاز را می‌پذیرد و `Pillow` تصویر را واقعاً باز می‌کند. یک فایل `evil.jpg` که در واقع HTML است، رد می‌شود.

---

## پنل مدیریت

- عنوان‌ها و برچسب‌ها فارسی
- `admin.site.index_template = "admin/dashboard.html"` برای داشبورد آماری
- گذار وضعیت سفارش از `transition_status` (با بررسی نقشه‌ی مجاز)
- فیلتر `date_hierarchy` روی `created_at` نیازمند جدول‌های Timezone دیتابیس است

> **نکته‌ی عملی:** اگر جدول‌های Timezone دیتابیس خالی باشند، `CONVERT_TZ` مقدار `NULL` برمی‌گرداند و `date_hierarchy` با خطای `Database returned an invalid datetime value` مواجه می‌شود. راه‌حل: `bash scripts/mysql_dev.sh` (این اسکریپت Timezone را هم بارگذاری می‌کند).

---

## تست

```bash
uv run python manage.py test
```

**۲۰۸ تست** در ۷ گروه:

| اپ | تعداد | پوشش |
| --- | --- | --- |
| `common` | ۲۴ | نرمال‌سازی ارقام، اعتبارسنجی‌ها، فیلترهای قالب، Rate Limiter، داشبورد ادمین |
| `accounts` | ۲۸ | درخواست/تأیید OTP، انقضا، سقف تلاش، ارقام فارسی، پروفایل، آدرس |
| `products` | ۲۴ | قیمت‌گذاری، Manager ها، محدودیت واریانت، مدل‌ها |
| `cart` | ۲۷ | سرویس‌ها، سقف تعداد، HTMX، ایزوله‌سازی مهمان، ادغام |
| `orders` | ۳۰ | Snapshot، Overselling، لغو، انقضا، گذار وضعیت، ارسال |
| `payments` | ۳۸ | درگاه، امضا، Verify، Idempotency، صفحات نتیجه، جریان کامل |
| `store` | ۳۷ | همه‌ی صفحات، فیلترها، جستجو، جزئیات محصول، هدرهای امنیتی |

### تست‌های کلیدی

```
OversellingProtectionTests
  └─ test_second_order_cannot_exceed_stock    ثبت دو سفارش همزمان

CallbackTests
  ├─ test_callback_is_idempotent              ارسال دوباره بدون اثر
  ├─ test_tampered_status_is_rejected         دستکاری status
  └─ test_tampered_authority_is_rejected      دستکاری authority

VerifyOtpTests
  ├─ test_challenge_is_locked_after_max_attempts
  └─ test_max_attempts_reached_before_any_guess

FullPurchaseFlowTests
  └─ test_full_flow                           سرانجام‌به‌سرانجام
```

### نکته‌ی اجرایی

تست‌های پرداخت به `DEBUG=True` نیاز دارند چون درگاه Mock عمداً با `DEBUG=False` غیرفعال است. `PaymentTestCase` این را با `@override_settings(DEBUG=True)` مدیریت می‌کند.

---

## زیرساخت Docker

کل پروژه‌ی اجرا (Python 3.14، Node، MariaDB، Tailwind، Gunicorn) داکرایز شده تا انتقال از محیط توسعه به سرور اصلی تنها با `docker compose up` انجام شود.

### چرا سه مرحله در Dockerfile؟

```
Stage 1  assets  │ node:22-bookworm      │ npm ci && npm run build   → خروجی: static/
Stage 2  builder │ python:3.14-slim+uv   │ uv sync --frozen          → venv کامل
Stage 3  runtime │ python:3.14-slim      │ فقط venv + runtime deps   → ایمیج نهایی کوچک
```

- **جداسازی Build Chain:** ابزار Node فقط در مرحله‌ی اول وجود دارد و وارد ایمیج نهایی نمی‌شود.
- **کاربر غیر-root:** در مرحله‌ی آخر کاربر `app` ساخته می‌شود تا Container با دسترسی Root اجرا نشود.
- **لایه‌های Cache:** تغییر کد Python باعث اجرای دوباره‌ی `npm ci` نمی‌شود و بالعکس.

### تفکیک فایل‌های Compose

| فایل | نقش |
| --- | --- |
| `docker-compose.yml` | پایه و Production: سرویس‌های `web` و `db`، بدون Hot-Reload |
| `docker-compose.override.yml` | توسعه: `runserver`، `DEBUG=True`، Mount کردن مخزن روی `/app` |
| `.docker.env.example` | فقط مرجع متغیرها؛ **نباید روی `.env` نوشته شود** |

نکته‌ی مهم اینکه `DJANGO_SETTINGS_MODULE` در `docker-compose.yml` (فایل پایه) به‌صورت **قاطع** روی `config.settings.production` تنظیم شده تا مقدار `development` موجود در `.env` محلی به آن نشت نکند؛ فایل override آن را صراحتاً به `development` برمی‌گرداند.

### ترتیب بالا آمدن Container

`entrypoint.sh` قبل از اجرای Gunicorn:

1. **Wait-for-DB:** اتصال واقعی به دیتابیس را با خطا/پاسخ فارسی چک می‌کند.
2. **Collectstatic:** فایل‌های `static` را جمع می‌کند.
3. **Migrate:** زمانی که `MIGRATE_ON_START=1`.
4. **Seed:** فقط زمانی که `SEED_ON_START=1` (پیش‌فرض `0`).

`docker/db/init/01-load-timezones.sh` جدول‌های Timezone را یک‌بار بارگذاری می‌کند؛ بدون آن `date_hierarchy` ادمین با خطا مواجه می‌شود.

---

## نکات گسترش

### افزودن دسته‌بندی والد/فرزند

`Category` دارای self-FK است. صفحه‌ی یک دسته محصولات زیردسته‌های مستقیمش را هم نشان می‌دهد:

```python
descendant_ids = Category.objects.filter(parent=category_obj).values_list("id", flat=True)
queryset = queryset.filter(category_id__in=[category_obj.id, *descendant_ids])
```

> این محدودسازی عمداً به اعتبار فرم وابسته نیست. قبلاً وقتی هیچ پارامتر Query ای نبود، فرم نامعتبر بود و صفحه‌ی دسته **همه محصولات** را نشان می‌داد — باگی که تست `test_category_page_filters_children` آن را گرفت.

### افزودن کد تخفیف عمومی

مدل فعلی `Discount` به صورت OneToOne با Product است. برای کد تخفیف عمومی، یک مدل جدید بسازید و در `orders/services.create_order` بعد از محاسبه‌ی `subtotal` اعمال کنید. فیلدهای `discount_total` و `total` از قبل آماده‌اند.

### افزودن درگاه پرداخت واقعی

به بخش [پرداخت](#پرداخت) مراجعه کنید. فقط کافی است `PaymentGateway` را پیاده‌سازی کنید و مسیرش را در `PAYMENT_GATEWAY` بگذارید.

### افزودن سرویس پیامک واقعی

به بخش [احراز هویت با OTP](#احراز-هویت-با-otp) مراجعه کنید.

### ساختار دایرکتوری

```
config/          پیکربندی
common/          زیرساخت مشترک (اعتبارسنجی، Rate Limit، فیلتر قالب، داشبورد)
accounts/        کاربر + OTP + آدرس + Provider های پیامک
products/        کاتالوگ + قیمت‌گذاری
cart/            سبد خرید
orders/          سفارش + گذار وضعیت + ارسال
payments/        درگاه‌ها + تراکنش
store/           صفحات فروشگاه + داده‌های نمونه
templates/       همه‌ی قالب‌ها
assets/css/      منبع CSS (Design System)
static/          خروجی build
scripts/         اسکریپت‌های کمکی
```

### قواعد کد

- هیچ View ای منطق کسب‌وکار ندارد
- هیچ عدد پولی از `request` خوانده نمی‌شود
- همه‌ی فیلدهای مالی `Decimal` هستند
- همه‌ی خطاهای قابل نمایش به کاربر، پیام فارسی دارند
- هیچ Inline Event Handler ای در قالب‌ها مجاز نیست
- همه‌ی رشته‌های قابل نمایش باید `gettext` داشته باشند

---

## پیوست: باگ‌های واقعی که تست‌ها حین توسعه پیدا کردند

نوشتن تست‌ها قبل از اتمام پروژه باعث شد باگ‌هایی پیدا شوند که در بازبینی دستی قابل تشخیص نبودند. فهرست کامل:

### ۱) محافظت Brute-Force روی OTP کار نمی‌کرد (امنیتی)

`raise` کردن Exception داخل بلوک `transaction.atomic()` باعث Rollback می‌شود؛ بنابراین شمارنده‌ی `attempts` هیچ‌وقت ذخیره نمی‌شد و مهاجم می‌توانست بی‌نهایت بار کد را آزمایش کند.

```python
# ❌ هر تلاش اشتباه Rollback می‌شود
with transaction.atomic():
    challenge.attempts += 1
    challenge.save()
    raise OTPError("کد اشتباه است.")
```

راه‌حل در [`accounts/otp.py`](accounts/otp.py): خطا در یک متغیر `failure` نگه داشته می‌شود و **بعد از** بلوک `atomic` پرتاب می‌شود.

تست: `VerifyOtpTests.test_wrong_code_increments_attempts`

### ۲) داشبورد ادمین همیشه خالی بود

`context_processor` با `request.resolver_match.url_name == "admin:index"` مقایسه می‌کرد، در حالی که `url_name` برای ادمین برابر `"index"` است و نام‌فضا در `view_name` قرار دارد.

```python
# ❌ هیچ‌گاه True نمی‌شود
match.url_name == "admin:index"
# ✅
match.view_name == "admin:index"
```

تست: `common.tests.DashboardContextTests`

### ۳) صفحه‌ی یک دسته‌بندی همه‌ی محصولات را نشان می‌داد

وقتی هیچ پارامتر Query ای نبود، `ProductFilterForm` بدون داده نامعتبر بود و `_apply_filters` زودتر `return` می‌کرد. نتیجه: محدودسازی به دسته‌بندی اصلاً اعمال نمی‌شد.

راه‌حل: محدودسازی به دسته از اعتبار فرم مستقل شد.

تست: `ProductListTests.test_category_page_filters_children`

### ۴) `retry_after` روی LocMemCache خطا می‌داد

`cache.ttl()` متدی اختصاصی Redis است. در محیط توسعه (LocMemCache) با `AttributeError` مواجه می‌شد. شمارنده‌ی مهلت حالا در یک کلید همراه نگهداری می‌شود.

تست: `RateLimitTests.test_retry_after_is_reported`

### ۵) ریختن `Column 'cart_id' cannot be null` هنگام افزودن به سبد

`select_for_update()` روی یک Related Manager یک **QuerySet** برمی‌گرداند، نه Manager؛ بنابراین `get_or_create` فیلد `cart` را ست نمی‌کرد. قفل و ساخت ردیف حالا جداگانه مدیریت می‌شوند.

### ۶) `ImportError` خام برای Provider های پیکربندی‌نشده

مسیر اشتباه در `SMS_PROVIDER` یا `PAYMENT_GATEWAY` با `ImportError` خام به خطای 500 تبدیل می‌شد. حالا با پیام روشن فارسی به کاربر نمایش داده می‌شود.

تست: `GatewayTests.test_unknown_gateway_path_is_rejected`

### ۷) Production با `ValueError` بالا نمی‌آمد

دایرکتوری `logs/` در `.gitignore` است؛ بدون ساخت خودکار، تنظیمات `production.py` هنگام ساخت Handler فایل خطا می‌داد و سایت اصلاً بالا نمی‌آمد.

### ۸) تغییر وضعیت، روی Instance فراخواننده اعمال نمی‌شد

`mark_order_paid` و `cancel_order` نسخه‌ی قفل‌شده را از دیتابیس می‌گرفتند و همان را برمی‌گرداندند؛ نمونه‌ای که به تابع پاس شده بود وضعیت کهنه نگه می‌داشت. تابع `_refresh_instance` مقادیر را روی Instance اصلی هم اعمال می‌کند.

### ۹) سقف تعداد در صفحه‌ی محصول همیشه صفر بود

`max_quantity` به‌درستی محاسبه نمی‌شد و چون موجودی در تست صفر بود، دکمه‌ی «افزودن به سبد» غیرفعال می‌ماند. (بررسی نشان داد منطق درست است؛ تست ناقص بود.)

### ۱۰) متن‌های ناموجود در قالب‌ها

«ناموجود» فقط در کارت محصول بود، نه صفحه‌ی جزئیات. حالا نشانگر صریح ناموجودی در صفحه‌ی محصول هم هست.

---

**درس مهم:** تست‌های واحد و E2E فقط برای تأیید کد نیستند؛ آن‌ها تنها روش مطمئن برای یافتن باگ‌هایی هستند که منطق به‌ظاهر درست دارند ولی در اجرا رفتار اشتباهی دارند (به‌ویژه مسائل Transaction، Rollback و Cache).
