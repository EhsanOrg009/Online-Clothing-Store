"""Permission-scoped, aggregated admin dashboard data."""
from django.db.models import Count, Q, Sum
from django.utils import timezone


def can_view(user, model):
    label = f"{model._meta.app_label}."
    name = model._meta.model_name
    return user.has_perm(f"{label}view_{name}") or user.has_perm(f"{label}change_{name}")


def build_dashboard_context(user) -> dict:
    from accounts.models import User
    from orders.choices import OrderStatus, PaymentStatus
    from orders.models import Order, OrderItem
    from products.models import Product, ProductVariant

    stats = {}
    if can_view(user, User):
        stats["total_users"] = User.objects.count()
    if can_view(user, Product):
        stats.update(Product.objects.aggregate(
            total_products=Count("pk"),
            published_products=Count("pk", filter=Q(is_published=True, is_active=True, archived_at__isnull=True)),
        ))
    if can_view(user, ProductVariant):
        stats["out_of_stock_variants"] = ProductVariant.objects.filter(is_active=True, stock=0).count()
    if can_view(user, Order):
        successful = Q(payment_status=PaymentStatus.SUCCESSFUL)
        stats.update(Order.objects.aggregate(
            new_orders=Count("pk", filter=Q(status__in=[OrderStatus.PENDING_PAYMENT, OrderStatus.PAID])),
            paid_orders=Count("pk", filter=successful),
            shipped_orders=Count("pk", filter=Q(status__in=[OrderStatus.SHIPPED, OrderStatus.DELIVERED])),
            total_sales=Sum("total", filter=successful, default=0),
            pending_payment_orders=Count("pk", filter=Q(status=OrderStatus.PENDING_PAYMENT) & (Q(expires_at__gt=timezone.now()) | Q(expires_at__isnull=True))),
        ))
        stats["recent_orders"] = Order.objects.select_related("customer").order_by("-created_at")[:8]
        if can_view(user, OrderItem):
            stats["total_items_sold"] = OrderItem.objects.filter(order__payment_status=PaymentStatus.SUCCESSFUL).aggregate(total=Sum("quantity", default=0))["total"]
            if can_view(user, Product):
                stats["best_sellers"] = Product.objects.live().annotate(
                    sold=Sum("order_items__quantity", filter=Q(order_items__order__payment_status=PaymentStatus.SUCCESSFUL), default=0)
                ).filter(sold__gt=0).order_by("-sold", "pk")[:5]
    return {"dashboard": stats}
