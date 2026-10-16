/* Presentation only: native Django formsets, widgets, actions and permissions own behavior. */
(() => {
  'use strict';
  const enhanceRegions = () => {
    document.querySelectorAll('#changelist .results, .inline-group .tabular, #change-history').forEach(region => {
      if (!region.hasAttribute('tabindex')) region.tabIndex = 0;
      if (!region.hasAttribute('role')) region.setAttribute('role', 'region');
      if (!region.hasAttribute('aria-label')) region.setAttribute('aria-label', 'جدول مدیریت؛ برای مشاهده ستون‌ها پیمایش کنید');
    });
  };
  const tableFieldName = input => {
    const cell = input.closest('td');
    const heading = cell?.closest('table')?.tHead?.rows[0]?.cells[cell.cellIndex];
    const headingText = (heading?.querySelector('.text') || heading)?.textContent.trim();
    const row = cell?.parentElement;
    const record = row?.querySelector('th, td.original p')?.textContent.trim();
    return [headingText, record?.slice(0,100)].filter(Boolean).join('، ');
  };
  const enhanceLabels = () => {
    document.querySelectorAll('.help-tooltip[aria-label]:not([role])').forEach(icon => icon.setAttribute('role','img'));
    document.querySelectorAll('.inline-group table input, .inline-group table select, .inline-group table textarea, #result_list input, #result_list select').forEach(input => {
      if (['hidden','submit','button'].includes(input.type) || input.labels?.length || input.hasAttribute('aria-labelledby')) return;
      if (input.hasAttribute('aria-label') && !input.hasAttribute('data-admin-table-label')) return;
      const label = tableFieldName(input);
      if (label) { input.setAttribute('aria-label', label); input.setAttribute('data-admin-table-label',''); }
    });
    document.querySelectorAll('input[type="file"]:not([aria-label]):not([aria-labelledby])').forEach(input => {
      if (input.labels?.length) return;
      const label = input.closest('.form-row')?.querySelector('label, legend')?.textContent.trim() || tableFieldName(input);
      if (label) input.setAttribute('aria-label',label);
    });
    document.querySelectorAll('.select2-selection[role="combobox"]').forEach(selection => {
      const rendered = selection.querySelector('.select2-selection__rendered');
      const sourceId = rendered?.id.replace(/^select2-/,'').replace(/-container$/,'');
      const source = sourceId && document.getElementById(sourceId);
      if (!source) return;
      let label = source.labels?.[0];
      const name = label?.textContent.trim() || source.getAttribute('aria-label');
      if (!name) return;
      if (!label) {
        const labelId = `${source.id}-admin-caption`;
        label = document.getElementById(labelId);
        if (!label) { label = document.createElement('span'); label.id=labelId; label.className='visually-hidden'; source.before(label); }
        if (label.textContent !== name) label.textContent = name;
      } else if (!label.id) label.id = `${source.id}-admin-label`;
      const labelledBy = `${label.id} ${rendered.id}`;
      if (selection.getAttribute('aria-labelledby') !== labelledBy) selection.setAttribute('aria-labelledby',labelledBy);
      rendered.setAttribute('aria-label', name);
    });
    document.querySelectorAll('.select2-search__field:not([aria-label])').forEach(input => input.setAttribute('aria-label','جستجوی گزینه‌ها'));
  };
  const enhance = () => { enhanceRegions(); enhanceLabels(); };
  enhance();
  window.addEventListener('load', enhance);
  document.addEventListener('formset:added', enhance);
  // Native widgets initialize after parsing and may add their accessible controls later.
  let scheduled = false;
  new MutationObserver(records => {
    if (scheduled || !records.some(record => record.addedNodes.length)) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled=false; enhance(); });
  }).observe(document.body,{childList:true,subtree:true});
  // The file remains in the native input; object URLs are local previews only.
  const previews = new Map();
  document.addEventListener('change', event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'file' || !/(^|-)image$/.test(input.name)) return;
    const previous = previews.get(input);
    if (previous) { URL.revokeObjectURL(previous.url); previous.figure.remove(); previews.delete(input); }
    const file = input.files?.[0];
    if (!file || !['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'].includes(file.type)) return;
    const figure = document.createElement('figure');
    figure.className = 'admin-upload-preview';
    const image = document.createElement('img');
    const url = URL.createObjectURL(file);
    image.src = url; image.alt = 'پیش‌نمایش تصویر انتخاب‌شده';
    const caption = document.createElement('figcaption'); caption.textContent = file.name;
    figure.append(image, caption); input.after(figure);
    previews.set(input, {url, figure});
  });
  document.addEventListener('formset:removed', () => {
    for (const [input, preview] of previews) {
      if (!input.isConnected) { URL.revokeObjectURL(preview.url); previews.delete(input); }
    }
  });
  window.addEventListener('pagehide', () => { for (const preview of previews.values()) URL.revokeObjectURL(preview.url); });
})();
