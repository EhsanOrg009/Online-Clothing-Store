/* Progressive enhancement: every purchase/sort/navigation form has an HTML fallback. */
(() => {
  'use strict';
  const search = document.getElementById('header-search');
  if (search) {
    const hint = document.querySelector('[data-search-shortcut]');
    if (hint) hint.hidden = false;
    document.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); search.focus(); search.select();
      }
    });
  }
  const mobileMenu = document.querySelector('.mobile-menu');
  if (mobileMenu) {
    mobileMenu.addEventListener('keydown', event => {
      if (event.key === 'Escape' && mobileMenu.open) {
        mobileMenu.open = false; mobileMenu.querySelector('summary').focus();
      }
    });
  }
  const zoom = document.querySelector('[data-gallery-zoom]');
  const dialog = document.getElementById('gallery-dialog');
  if (zoom && dialog && typeof dialog.showModal === 'function') {
    zoom.hidden = false;
    zoom.addEventListener('click', () => {
      const source = document.getElementById('product-image');
      const large = dialog.querySelector('[data-gallery-large]');
      if (!source || source.dataset.imageFailed) return;
      large.src = source.src; large.alt = source.alt; dialog.showModal();
    });
    dialog.querySelector('[data-gallery-close]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  }
  // Keep a failed media URL legible without substituting unrelated photography.
  const missingImage = image => {
    if (!image.matches('.product-card__media img, .gallery-main img, .collection-photo img, .category-entry img')) return;
    if (image.dataset.imageFailed) return;
    image.dataset.imageFailed = 'true';
    image.hidden = true;
    const caption = document.createElement('span');
    caption.className = 'media-fallback';
    caption.textContent = image.alt ? `تصویر ${image.alt} در دسترس نیست` : 'تصویر در دسترس نیست';
    image.after(caption);
    if (image.id === 'product-image' && zoom) zoom.hidden = true;
  };
  document.addEventListener('error', event => {
    if (event.target instanceof HTMLImageElement) missingImage(event.target);
  }, true);
  document.querySelectorAll('img').forEach(image => {
    if (image.complete && image.naturalWidth === 0) missingImage(image);
  });
  const rails = document.querySelectorAll('[data-rail-controls]');
  const syncRails = () => {
    rails.forEach(controls => {
      const buttons = controls.querySelectorAll('[data-rail-step]');
      const rail = document.getElementById(buttons[0]?.dataset.railTarget);
      if (!rail) return;
      const maximum = rail.scrollWidth - rail.clientWidth;
      controls.hidden = maximum < 2;
      buttons.forEach(button => {
        const position = Math.abs(rail.scrollLeft);
        button.disabled = button.dataset.railStep === '-1' ? position < 2 : position >= maximum - 2;
      });
    });
  };
  rails.forEach(controls => {
    const target = controls.querySelector('[data-rail-target]')?.dataset.railTarget;
    document.getElementById(target)?.addEventListener('scroll', syncRails, {passive:true});
  });
  window.addEventListener('resize', syncRails, {passive:true});
  syncRails();
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-rail-step]');
    if (!button) return;
    const rail = document.getElementById(button.dataset.railTarget);
    if (!rail) return;
    const direction = getComputedStyle(rail).direction === 'rtl' ? -1 : 1;
    rail.scrollBy({left: direction * Number(button.dataset.railStep) * rail.clientWidth * .8, behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  });
  const filters = document.querySelector('[data-catalog-filters]');
  if (filters && window.matchMedia('(max-width: 1023px)').matches && !filters.querySelector('[aria-invalid="true"], [role="alert"]')) filters.open = false;
  document.addEventListener('click', event => {
    const imageButton = event.target.closest('[data-gallery-src]');
    if (imageButton) {
      const main = document.getElementById('product-image');
      if (main) {
        main.parentNode.querySelector('.media-fallback')?.remove();
        main.hidden = false; delete main.dataset.imageFailed;
        main.src = imageButton.dataset.gallerySrc; main.alt = imageButton.dataset.galleryAlt;
        if (zoom && dialog && typeof dialog.showModal === 'function') zoom.hidden = false;
      }
      document.querySelectorAll('[data-gallery-src]').forEach(button => button.setAttribute('aria-pressed', String(button === imageButton)));
    }
  });
  const form = document.querySelector('[data-product-form]');
  if (form) {
    const variants = JSON.parse(document.getElementById('variants-data').textContent);
    const colors = JSON.parse(document.getElementById('colors-data').textContent);
    const select = form.querySelector('[name="variant_id"]');
    const quantity = form.querySelector('[name="quantity"]');
    const submit = form.querySelector('[type="submit"]');
    const feedback = document.getElementById('variant-feedback');
    const colorOptions = form.querySelector('[data-color-options]');
    const sizeOptions = form.querySelector('[data-size-options]');
    const limit = Number(form.dataset.maxQuantity);
    let colorId = null;
    const sync = () => {
      const selected = variants.find(variant => String(variant.id) === select.value);
      const stock = selected ? Math.min(limit, selected.stock) : 1;
      quantity.max = String(stock);
      quantity.value = String(Math.max(1, Math.min(stock, Number(quantity.value) || 1)));
      submit.disabled = !selected || !selected.available;
      feedback.textContent = selected ? `موجودی ${selected.color} / ${selected.size}: ${selected.stock} عدد` : 'یک سایز را انتخاب کنید.';
    };
    const option = (name, value, label, disabled, callback) => {
      const wrapper = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio'; input.name = name; input.value = value; input.disabled = disabled;
      input.addEventListener('change', callback);
      wrapper.append(input, document.createTextNode(label));
      return wrapper;
    };
    colors.forEach(color => {
      colorOptions.append(option('product_color', color.id, color.name, !color.in_stock, () => {
        colorId = color.id; select.value = ''; sizeOptions.replaceChildren();
        variants.filter(variant => variant.color_id === colorId).forEach(variant => {
          sizeOptions.append(option('product_size', variant.size_id, variant.size, !variant.available, () => { select.value = String(variant.id); sync(); }));
        });
        quantity.value = '1'; sync();
      }));
    });
    select.required = false;
    form.querySelector('[data-variant-fallback]').hidden = true;
    form.querySelector('[data-variant-controls]').hidden = false;
    feedback.textContent = 'ابتدا رنگ، سپس سایز را انتخاب کنید.';
    submit.disabled = true;
    form.addEventListener('click', event => {
      const button = event.target.closest('[data-product-qty]');
      if (!button) return;
      const maximum = Number(quantity.max) || 1;
      quantity.value = String(Math.max(1, Math.min(maximum, (Number(quantity.value) || 1) + Number(button.dataset.productQty))));
      quantity.dispatchEvent(new Event('input', { bubbles: true }));
    });
    form.addEventListener('submit', event => { if (!select.value) { event.preventDefault(); feedback.textContent = 'رنگ و سایز را انتخاب کنید.'; } });
  }
  document.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target.id !== 'cart-content') return;
    const networkError = document.querySelector('[data-cart-network-error]');
    if (networkError) networkError.hidden = true;
    const count = event.detail.target.querySelector('[data-cart-total-count]');
    if (count) document.querySelectorAll('[data-cart-count]').forEach(node => { node.textContent = count.dataset.cartTotalCount; });
    const focus = event.detail.target.querySelector('[data-cart-feedback]');
    if (focus) focus.focus({ preventScroll: true });
  });
  const showCartError = () => {
    const feedback = document.querySelector('[data-cart-network-error]');
    if (feedback) feedback.hidden = false;
  };
  ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach(name => document.addEventListener(name, showCartError));
})();
