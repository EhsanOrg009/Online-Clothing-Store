"""Creates explicitly labelled QA products only in /tmp/shop-browser.sqlite3."""
import os, sys, json, secrets
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['DJANGO_SETTINGS_MODULE'] = 'shop_qa_settings'
import django
django.setup()
from django.core.management import call_command
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.core.files.base import ContentFile
from accounts.models import Address
from products.models import Category, Product, ProductVariant, Color, Size, Discount, ProductImage
from cart.models import Cart, CartItem
from orders.services import create_order
from orders.choices import OrderStatus, PaymentStatus
from payments.services import create_payment_for_order, process_callback
from payments.gateways.mock import MockGateway
from accounts.otp import request_otp
from io import BytesIO
from PIL import Image, ImageDraw
if str(settings.DATABASES['default']['NAME']) != '/tmp/shop-browser.sqlite3':
 raise RuntimeError('Browser fixtures may only write to the isolated /tmp database.')
call_command('migrate', verbosity=0)
User=get_user_model()
password=secrets.token_urlsafe(24)
admin, _=User.objects.get_or_create(phone_number='09120000001', defaults={'first_name':'مدیر','last_name':'آزمایش','is_staff':True,'is_superuser':True,'is_phone_verified':True})
admin.set_password(password);admin.save()
customer, _=User.objects.get_or_create(phone_number='09120000002', defaults={'first_name':'کاربر','last_name':'آزمایش','is_phone_verified':True})
address, _=Address.objects.get_or_create(user=customer, defaults={'receiver_name':'کاربر آزمایش','receiver_phone':customer.phone_number,'province':'تهران','city':'تهران','address_line':'نشانی آزمایشی برای بررسی فرم و تحویل','postal_code':'1234567890','is_default':True})
category, _=Category.objects.get_or_create(slug='qa-clothing', defaults={'name':'پوشاک آزمایش','description':'داده موقت مخصوص بررسی رابط در مرورگر'})
color, _=Color.objects.get_or_create(name='آبی آزمایش', defaults={'hex_code':'#245b80'})
second, _=Color.objects.get_or_create(name='روشن آزمایش', defaults={'hex_code':'#d6dfe8'})
size, _=Size.objects.get_or_create(name='M')
large, _=Size.objects.get_or_create(name='L')
for i in range(14):
 product,_=Product.objects.get_or_create(slug=f'qa-product-{i}',defaults={'name':f'لباس آزمایشی {i+1}','category':category,'base_price':500000+i*75000,'description':'این محصول فقط در پایگاه داده موقت بررسی مرورگر قرار دارد.\nرنگ و سایز را برای بررسی فرم انتخاب کنید.','is_published':True,'is_featured':i<4})
 variant,_=ProductVariant.objects.get_or_create(product=product,color=color,size=size,defaults={'stock':20})
 ProductVariant.objects.get_or_create(product=product,color=second,size=large,defaults={'stock':0 if i==0 else 12})
 if not product.images.exists():
  for j in range(2 if i==0 else 1):
   im=Image.new('RGB',(600,800),'#e2e9ef');d=ImageDraw.Draw(im);d.polygon([(160,170),(245,125),(355,125),(440,170),(515,280),(440,335),(405,295),(405,665),(195,665),(195,295),(160,335),(85,280)],fill='#245b80' if j==0 else '#33485e');d.line([(245,125),(260,190),(340,190),(355,125)],fill='#d6dfe8',width=8);b=BytesIO();im.save(b,format='PNG');ProductImage.objects.create(product=product,image=ContentFile(b.getvalue(),name=f'qa-{i}-{j}.png'),is_primary=j==0,alt_text='تصویر ساختگی فقط برای آزمایش مرورگر')
 if i==0: Discount.objects.get_or_create(product=product,defaults={'name':'تخفیف آزمایش','discount_type':'percent','value':20})
cart, _=Cart.objects.get_or_create(user=customer)
CartItem.objects.get_or_create(cart=cart,variant=ProductVariant.objects.filter(product__slug='qa-product-0',color=color).first(),defaults={'quantity':2})
from orders.models import Order
if not Order.objects.filter(customer=customer).exists():
 order=create_order(customer=customer,cart=cart,address=address)
 payment=create_payment_for_order(order)
 from payments.gateways.mock import MockGateway
 gateway=MockGateway();process_callback(payment,{'status':'successful','sig':gateway.sign(f'result|{payment.authority}|successful')})
 CartItem.objects.create(cart=cart,variant=ProductVariant.objects.filter(product__slug='qa-product-0',color=color).first(),quantity=2)
incomplete, _=User.objects.get_or_create(phone_number='09120000003', defaults={'is_phone_verified':True})
Group.objects.get_or_create(name='گروه آزمایش رابط مدیریت')
request_otp(customer.phone_number, '127.0.0.1')
verify_session=SessionStore();verify_session['otp_phone']=customer.phone_number;verify_session.save()
sessions={'guest_verify': verify_session.session_key}
for key,user in [('admin',admin),('customer',customer),('customer_complete',incomplete)]:
 s=SessionStore();s[SESSION_KEY]=str(user.pk);s[BACKEND_SESSION_KEY]='django.contrib.auth.backends.ModelBackend';s[HASH_SESSION_KEY]=user.get_session_auth_hash();s.save();sessions[key]=s.session_key
sessions['admin_password']=password
sessions['order_number']=Order.objects.filter(customer=customer, payment_status=PaymentStatus.SUCCESSFUL).first().order_number
sessions['customer_routes']=[f'/accounts/addresses/{address.pk}/edit/']
sessions['payment_routes']=[]
gateway=MockGateway()
for outcome in ['failed', 'cancelled', 'pending']:
 qa_cart=Cart.objects.create(user=customer)
 CartItem.objects.create(cart=qa_cart,variant=ProductVariant.objects.filter(product__slug='qa-product-1',color=color).first(),quantity=1)
 qa_order=create_order(customer=customer,cart=qa_cart,address=address)
 payment=create_payment_for_order(qa_order)
 if outcome!='pending':
  process_callback(payment,{'status':outcome,'sig':gateway.sign(f'result|{payment.authority}|{outcome}')})
  sessions['payment_routes'].append(reverse(f'payments:result_{outcome}',args=[qa_order.order_number]))
 else:
  sessions['payment_routes'].append(gateway.get_payment_url(payment))
 qa_cart.delete()
sessions['payment_routes'].append(reverse('payments:result_success',args=[sessions['order_number']]))
request=RequestFactory().get('/');request.user=admin
sessions['admin_routes']=['/admin/', '/admin/products/', '/admin/password_change/']
long_guest_cart, _=Cart.objects.get_or_create(user=None,session_key='qa-long-session-key-0123456789abcdef0123456789abcdef')
sessions['admin_routes'].append(reverse('admin:cart_cart_history',args=[long_guest_cart.pk]))
for model, model_admin in django_admin.site._registry.items():
 label=f'admin:{model._meta.app_label}_{model._meta.model_name}'
 sessions['admin_routes'].append(reverse(f'{label}_changelist'))
 if model_admin.has_add_permission(request):
  sessions['admin_routes'].append(reverse(f'{label}_add'))
 obj=model.objects.first()
 if obj:
  sessions['admin_routes'].extend([reverse(f'{label}_change',args=[obj.pk]),reverse(f'{label}_history',args=[obj.pk])])
  if model_admin.has_delete_permission(request,obj):
   sessions['admin_routes'].append(reverse(f'{label}_delete',args=[obj.pk]))
# Keep the actual shopping cart most recent so temporary payment fixtures cannot replace it.
from django.utils import timezone
Cart.objects.filter(pk=cart.pk).update(updated_at=timezone.now())
auth_file = Path('/tmp/shop-qa-auth.json')
auth_file.write_text(json.dumps(sessions))
auth_file.chmod(0o600)
print('Isolated browser fixtures ready; no project/customer database touched.')
