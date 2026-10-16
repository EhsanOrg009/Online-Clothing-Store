"""
View های عمومی فروشگاه.

نکات:
  • هیچ محصول غیرمنتشر/آرشیوشده در هیچ‌کجای فروشگاه نمایش داده نمی‌شود.
  • جستجو/فیلتر با Query واقعی دیتابیس انجام می‌شود (نه فیلتر در Template).
  • از `select_related` / `prefetch_related` برای جلوگیری از N+1 استفاده شده است.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _

from products.forms import ProductFilterForm
from products.models import Category, Color, Product, ProductVariant, Size

PRODUCTS_PER_PAGE = 12


def _color_choices():
    return list(
        Color.objects.filter(is_active=True).values_list("id", "name")
    )


def _size_choices():
    return list(Size.objects.filter(is_active=True).values_list("id", "name"))


def _product_queryset():
    return (
        Product.objects.live()
        .select_related("category")
        .prefetch_related(
            "images",
            "discount",
            Prefetch(
                "variants",
                queryset=ProductVariant.objects.select_related("color", "size"),
            ),
        )
    )


# ---------------------------------------------------------------------
# صفحه اصلی
# ---------------------------------------------------------------------
def home(request):
    categories = (
        Category.objects.filter(is_active=True, parent__isnull=True)
        .with_live_product_count()
        .order_by("sort_order", "name")[:8]
    )
    new_arrivals = _product_queryset().order_by("-created_at")[:8]
    featured = _product_queryset().featured()[:8]
    discounted = _product_queryset().discounted().order_by("-created_at")[:8]

    hero_image = None
    for product in featured or new_arrivals:
        primary = product.primary_image
        if primary:
            hero_image = primary
            break

    context = {
        "categories": categories,
        "new_arrivals": new_arrivals,
        "featured": featured,
        "discounted": discounted,
        "hero_image": hero_image,
    }
    return render(request, "store/collection_home.html", context)


def _descendant_category_ids(category: Category) -> list[int]:
    """شناسهٔ دسته و همهٔ زیردسته‌های فعال آن در هر عمقی (BFS محدود و بدون حلقه)."""
    ids = [category.id]
    frontier = [category.id]
    while frontier:
        children = list(
            Category.objects.filter(parent_id__in=frontier, is_active=True)
            .exclude(id__in=ids)
            .values_list("id", flat=True)
        )
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


# ---------------------------------------------------------------------
# فهرست محصولات + فیلتر/مرتب‌سازی
# ---------------------------------------------------------------------
def _apply_filters(request, queryset, category: Category | None = None):
    """
    اعمال فیلترهای کاربر + محدودسازی به دسته‌بندی.

    نکته مهم: محدودسازی به دسته‌بندی نباید به اعتبار فرم وابسته باشد؛
    وگرنه صفحه‌ی یک دسته بدون پارامتر Query، همه محصولات را نشان می‌دهد.
    """
    form = ProductFilterForm(
        request.GET or None,
        color_choices=_color_choices(),
        size_choices=_size_choices(),
    )
    is_valid = form.is_valid()
    data = form.cleaned_data if is_valid else {}

    if data.get("q"):
        queryset = queryset.search(data["q"])
    if data.get("min_price") is not None:
        queryset = queryset.filter(base_price__gte=data["min_price"])
    if data.get("max_price") is not None:
        queryset = queryset.filter(base_price__lte=data["max_price"])
    if data.get("discount_only"):
        queryset = queryset.discounted()
    if data.get("in_stock_only"):
        queryset = queryset.in_stock()
    # All variant criteria must match the same colour/size combination.
    variant_filters = {}
    if data.get("colors"):
        variant_filters["variants__color_id__in"] = data["colors"]
    if data.get("sizes"):
        variant_filters["variants__size_id__in"] = data["sizes"]
    if variant_filters:
        variant_filters["variants__is_active"] = True
        if data.get("in_stock_only"):
            variant_filters["variants__stock__gt"] = 0
        queryset = queryset.filter(**variant_filters).distinct()

    category_obj = category
    slug = category.slug if category else data.get("category")
    if slug:
        category_obj = Category.objects.filter(slug=slug, is_active=True).first()
        if category_obj is not None:
            queryset = queryset.filter(
                category_id__in=_descendant_category_ids(category_obj)
            )

    queryset = queryset.sort(data.get("sort"))
    return queryset, form, category_obj


def _paginate(request, queryset):
    paginator = Paginator(queryset, PRODUCTS_PER_PAGE)
    page_number = request.GET.get("page") or 1
    return paginator.get_page(page_number)


def product_list(request, category_slug: str | None = None):
    category = None
    if category_slug:
        category = get_object_or_404(
            Category.objects.filter(is_active=True), slug=category_slug
        )

    queryset, form, category = _apply_filters(request, _product_queryset(), category)
    page_obj = _paginate(request, queryset)

    return render(
        request,
        "store/catalog.html",
        {
            "page_obj": page_obj,
            "filter_form": form,
            "category": category,
            "colors": Color.objects.filter(is_active=True),
            "sizes": Size.objects.filter(is_active=True),
            "result_count": page_obj.paginator.count,
        },
    )


def search(request):
    """صفحه نتایج جستجو."""
    query = (request.GET.get("q") or "").strip()
    queryset, form, _category = _apply_filters(request, _product_queryset())
    page_obj = _paginate(request, queryset)

    return render(
        request,
        "store/search.html",
        {
            "page_obj": page_obj,
            "filter_form": form,
            "query": query,
            "colors": Color.objects.filter(is_active=True),
            "sizes": Size.objects.filter(is_active=True),
            "result_count": page_obj.paginator.count,
        },
    )


# ---------------------------------------------------------------------
# جزئیات محصول
# ---------------------------------------------------------------------
def product_detail(request, slug: str):
    product = get_object_or_404(_product_queryset(), slug=slug)

    # فقط محصولات زنده قابل مشاهده‌اند (لینک محصول غیرمنتشر → 404)
    if not product.is_live:
        raise Http404(_("محصول موردنظر یافت نشد."))

    variants = [
        {
            "id": variant.id,
            "color_id": variant.color_id,
            "color": variant.color.name,
            "size_id": variant.size_id,
            "size": variant.size.name,
            "stock": variant.stock,
            "available": variant.is_in_stock,
            "sku": variant.sku or "",
        }
        for variant in product.variants.all()
        if variant.is_active
    ]

    # گروه‌بندی سایزها بر اساس رنگ برای نمایش وضعیت موجودی
    color_map: dict[int, dict] = {}
    for variant in variants:
        entry = color_map.setdefault(
            variant["color_id"],
            {
                "id": variant["color_id"],
                "name": variant["color"],
                "hex": "",
                "sizes": [],
                "in_stock": False,
            },
        )
        entry["sizes"].append(
            {"id": variant["size_id"], "name": variant["size"], "available": variant["available"]}
        )
        entry["in_stock"] = entry["in_stock"] or variant["available"]

    colors = Color.objects.filter(id__in=color_map.keys()).values("id", "hex_code")
    hex_map = {c["id"]: c["hex_code"] for c in colors}
    for entry in color_map.values():
        entry["hex"] = hex_map.get(entry["id"], "#111827")

    context = {
        "product": product,
        "price_info": product.price_info,
        "images": list(product.images.all()),
        "colors_data": list(color_map.values()),
        "colors_json": json.dumps(list(color_map.values()), ensure_ascii=False),
        "variants_data": variants,
        "variants_json": json.dumps(variants, ensure_ascii=False),
        "max_quantity": min(
            settings.MAX_QUANTITY_PER_ITEM,
            max((v["stock"] for v in variants), default=0),
        ),
        "related_products": _product_queryset()
        .filter(category=product.category)
        .exclude(pk=product.pk)[:4],
    }
    return render(request, "store/product_detail.html", context)


def category_index(request):
    """فهرست دسته‌بندی‌ها."""
    categories = (
        Category.objects.filter(is_active=True)
        .with_live_product_count()
        .order_by("sort_order", "name")
    )
    return render(request, "store/category_directory.html", {"categories": categories})
