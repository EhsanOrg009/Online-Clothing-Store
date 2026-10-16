/**
 * کپی فایل‌های Vendor به پوشه static پروژه.
 * این اسکریپت فقط برای توسعه است؛ خروجی آن در مخزن نگه داشته می‌شود
 * تا اجرای پروژه بدون Node هم ممکن باشد.
 */
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const vendorDir = path.join(root, "static", "vendor");
const fontDir = path.join(root, "static", "fonts");

fs.mkdirSync(vendorDir, { recursive: true });
fs.mkdirSync(fontDir, { recursive: true });

const files = [
  ["assets/fonts/InterVariable.woff2", "fonts/InterVariable.woff2"],
  ["assets/fonts/Inter-LICENSE.txt", "fonts/Inter-LICENSE.txt"],
  ["node_modules/alpinejs/dist/cdn.min.js", "vendor/alpine.min.js"],
  ["node_modules/htmx.org/dist/htmx.min.js", "vendor/htmx.min.js"],
];

for (const [src, dest] of files) {
  const from = path.join(root, src);
  const to = path.join(root, "static", dest);
  if (fs.existsSync(from)) {
    fs.copyFileSync(from, to);
    console.log(`✓ ${dest}`);
  } else {
    console.warn(`! یافت نشد: ${src}`);
  }
}

// فونت وزیرمتن: فقط وزن‌های موردنیاز برای کاهش حجم
const fontSource = path.join(root, "node_modules", "vazirmatn", "fonts", "webfonts");
const wanted = [
  "Vazirmatn-Regular.woff2",
  "Vazirmatn-Medium.woff2",
  "Vazirmatn-SemiBold.woff2",
  "Vazirmatn-Bold.woff2",
  "Vazirmatn-ExtraBold.woff2",
];
for (const name of wanted) {
  const from = path.join(fontSource, name);
  const to = path.join(fontDir, name);
  if (fs.existsSync(from)) {
    fs.copyFileSync(from, to);
    console.log(`✓ fonts/${name}`);
  } else {
    console.warn(`! فونت یافت نشد: ${name}`);
  }
}
