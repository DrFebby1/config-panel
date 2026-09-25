/* ---------------------------------------------------------------------------
   صفحه اختصاصی کاربر: نمایش حجم مصرفی، روزهای باقی‌مانده و کانفیگ‌ها
--------------------------------------------------------------------------- */
'use strict';

const $ = (id) => document.getElementById(id);

const TOKEN = (function () {
  const parts = location.pathname.split('/').filter(Boolean);
  // /u/<token>
  return parts.length >= 2 && parts[0] === 'u' ? decodeURIComponent(parts[1]) : '';
})();

let timer = null;

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Display-only Persian digits; copied values keep their ASCII form. */
const FA_DIGITS = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];
function fa(value) {
  return String(value ?? '').replace(/[0-9]/g, (digit) => FA_DIGITS[Number(digit)]);
}

function fmtBytes(bytes, unlimitedLabel) {
  if (bytes === null || bytes === undefined) return unlimitedLabel || 'بی‌نهایت';
  let value = Number(bytes);
  if (!isFinite(value)) return '-';
  if (value < 0) value = 0;
  const units = ['بایت', 'کیلوبایت', 'مگابایت', 'گیگابایت', 'ترابایت'];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return fa(value.toFixed(index >= 3 ? 2 : index >= 2 ? 1 : 0) + ' ' + units[index]);
}

function fmtDate(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts * 1000).toLocaleString('fa-IR', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (e) { return '—'; }
}

function toast(message, kind) {
  const node = document.createElement('div');
  node.className = 'toast ' + (kind || '');
  node.textContent = message;
  $('toast-root').appendChild(node);
  setTimeout(() => node.remove(), 3400);
}

async function copyText(value, label) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const area = document.createElement('textarea');
      area.value = value; area.setAttribute('readonly', '');
      area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select();
      document.execCommand('copy'); area.remove();
    }
    toast((label || 'متن') + ' کپی شد', 'ok');
  } catch (e) { toast('کپی نشد، دستی انتخاب کنید', 'err'); }
}

function ringSvg(percent, colorClass, centerBig, centerSmall) {
  const radius = 56;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = circumference * (1 - clamped / 100);
  return '<svg class="ring" viewBox="0 0 132 132">' +
    '<circle class="bg" cx="66" cy="66" r="' + radius + '"></circle>' +
    '<circle class="fg ' + colorClass + '" cx="66" cy="66" r="' + radius + '" ' +
      'stroke-dasharray="' + circumference.toFixed(2) + '" ' +
      'stroke-dashoffset="' + offset.toFixed(2) + '" ' +
      'transform="rotate(-90 66 66)"></circle>' +
    '<text class="ring-label" x="66" y="62"><tspan class="big" x="66">' + esc(centerBig) + '</tspan></text>' +
    '<text class="ring-label" x="66" y="80"><tspan class="small-t" x="66">' + esc(centerSmall) + '</tspan></text>' +
  '</svg>';
}

function colorClass(percent) {
  if (percent >= 90) return 'danger';
  if (percent >= 70) return 'warn';
  return '';
}

function render(data) {
  $('loading').classList.add('hidden');
  $('content').classList.remove('hidden');
  $('brand-name').textContent = data.brand || 'کانفیگ من';
  document.title = (data.brand || 'کانفیگ') + ' - ' + data.name;

  const percent = data.unlimited ? 0 : (data.usage_percent || 0);
  const quotaText = data.unlimited ? 'بی‌نهایت' : fmtBytes(data.quota_bytes);
  const remText = data.unlimited ? 'بی‌نهایت' : fmtBytes(data.remaining_bytes);
  const ringBig = data.unlimited ? fmtBytes(data.used_bytes) : fa(Math.round(percent) + '%');
  const ringSmall = data.unlimited ? 'مصرف‌شده' : (fmtBytes(data.used_bytes) + ' مصرف');

  const blocked = data.blocked;
  const blockedBox = $('error-box');
  if (blocked) {
    blockedBox.classList.remove('hidden');
    blockedBox.innerHTML = '<b>' + esc(data.status_label) + '</b>' +
      '<div class="small">' + esc(reasonFor(data.status)) + '</div>';
  } else {
    blockedBox.classList.add('hidden');
  }

  const configs = data.configs || [];
  const configsHtml = configs.length
    ? configs.map((config) =>
        '<div class="cfg">' +
          '<div class="cfg-head"><span class="pill">' + esc(config.protocol) + '</span>' +
            '<span class="small muted">' + esc(config.server) + '</span>' +
            '<span class="spacer"></span>' +
            '<button class="btn sm" data-copy="' + esc(config.link) + '">کپی</button></div>' +
          '<div class="cfg-url">' + esc(config.link) + '</div>' +
        '</div>').join('')
    : '<div class="empty">' +
        (blocked ? 'به دلیل وضعیت بالا فعلاً کانفیگی ارائه نمی‌شود.'
                 : 'کانفیگی برای شما ثبت نشده است. با پشتیبانی تماس بگیرید.') + '</div>';

  const dailyRow = data.daily_enabled
    ? '<div class="card mt">' +
        '<h4>مصرف امروز</h4>' +
        '<div class="small">' + esc(fmtBytes(data.daily_used_bytes)) + ' از ' +
          esc(fmtBytes(data.daily_quota_bytes)) + ' · باقی‌مانده ' +
          esc(fmtBytes(data.daily_remaining_bytes)) + '</div>' +
        '<div class="bar mt"><i class="' + colorClass(Math.min(100, data.daily_percent || 0)) +
          '" style="width:' + Math.min(100, data.daily_percent || 0).toFixed(1) + '%"></i></div>' +
        '<div class="muted small">هر روز به وقت تهران صفر می‌شود.</div>' +
      '</div>'
    : '';

  $('content').innerHTML =
    '<div class="card">' +
      '<div class="row wrap" style="margin-bottom:.6rem">' +
        '<h2 style="margin:0">' + esc(data.name) + '</h2>' +
        '<span class="badge ' + esc(data.status) + '">' + esc(data.status_label) + '</span>' +
      '</div>' +
      '<div class="ring-wrap">' +
        ringSvg(percent, colorClass(percent), ringBig, ringSmall) +
        '<div class="grow">' +
          '<dl class="kv">' +
            '<dt>حجم کل</dt><dd>' + esc(quotaText) + '</dd>' +
            '<dt>مصرف‌شده</dt><dd>' + esc(fmtBytes(data.used_bytes)) + '</dd>' +
            '<dt>باقی‌مانده</dt><dd>' + esc(remText) + '</dd>' +
            '<dt>روزهای باقی‌مانده</dt><dd>' + fa(data.days_left) + ' روز از ' + fa(data.days_total) + '</dd>' +
            '<dt>روزهای مصرف‌شده</dt><dd>' + fa(data.days_used) + ' روز</dd>' +
            '<dt>تاریخ انقضا</dt><dd>' + esc(data.expires_at ? fmtDate(data.expires_at) : 'در انتظار اولین اتصال') + '</dd>' +
          '</dl>' +
        '</div>' +
      '</div>' +
    '</div>' +

    dailyRow +

    '<div class="card mt">' +
      '<h4>لینک اشتراک</h4>' +
      '<p class="muted small">این لینک را در برنامه (v2rayNG، Nekobox، Streisand، Clash، Hiddify) ' +
        'به‌عنوان Subscription اضافه کن تا کانفیگ‌ها خودکار بروز شوند.</p>' +
      '<div class="cfg">' +
        '<div class="cfg-head"><span class="pill">Subscription</span><span class="spacer"></span>' +
          '<button class="btn sm" id="copy-sub">کپی</button>' +
          '<a class="btn sm" href="' + esc(data.sub_url) + '" target="_blank" rel="noopener">باز کردن</a>' +
        '</div>' +
        '<div class="cfg-url">' + esc(data.sub_url) + '</div>' +
      '</div>' +
      '<div class="center mt"><div class="qr-box" style="display:inline-block">' +
        '<img src="/api/portal/' + encodeURIComponent(TOKEN) + '/qr.svg" alt="QR" ' +
        'style="width:100%;height:100%"></div>' +
        '<div class="muted small">برای افزودن سریع، QR را با گوشی اسکن کن</div>' +
      '</div>' +
    '</div>' +

    '<div class="card mt"><h4>کانفیگ‌ها (' + fa(configs.length) + ')</h4>' + configsHtml + '</div>' +

    '<p class="muted small center mt">آخرین بروزرسانی: ' + esc(fmtDate(Math.floor(Date.now() / 1000))) +
      ' · این صفحه هر ۶۰ ثانیه خودکار بروز می‌شود.</p>';

  const copySub = $('copy-sub');
  if (copySub) copySub.onclick = () => copyText(data.sub_url, 'لینک اشتراک');
  document.querySelectorAll('#content [data-copy]').forEach((node) => {
    node.onclick = () => copyText(node.dataset.copy, 'کانفیگ');
  });

  const support = $('support-slot');
  if (support && data.support_url) {
    support.innerHTML = ' پشتیبانی: <a href="' + esc(data.support_url) + '" target="_blank" rel="noopener">' +
      esc(data.support_url) + '</a>';
  }
}

function reasonFor(status) {
  const map = {
    expired: 'زمان این کانفیگ به پایان رسیده است. برای تمدید با پشتیبانی تماس بگیرید.',
    exhausted: 'حجم این کانفیگ تمام شده است. برای خرید حجم بیشتر با پشتیبانی تماس بگیرید.',
    daily_limited: 'سقف مصرف امروز شما پر شده است. از فردا دوباره فعال می‌شود.',
    disabled: 'این کانفیگ توسط مدیر غیرفعال شده است.',
    pending: 'این کانفیگ با اولین اتصال شما فعال می‌شود.',
  };
  return map[status] || '';
}

async function load() {
  if (!TOKEN) {
    $('loading').classList.add('hidden');
    const box = $('error-box');
    box.classList.remove('hidden');
    box.textContent = 'لینک نامعتبر است.';
    return;
  }
  try {
    const response = await fetch('/api/portal/' + encodeURIComponent(TOKEN), { cache: 'no-store' });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!response.ok) {
      throw new Error((data && data.detail) || 'دریافت اطلاعات ناموفق بود');
    }
    data.sub_url = data.sub_url || (location.origin + '/sub/' + TOKEN);
    render(data);
  } catch (e) {
    $('loading').classList.add('hidden');
    const box = $('error-box');
    box.classList.remove('hidden');
    box.textContent = e.message;
  }
}

$('refresh-btn').onclick = () => { load(); toast('بروزرسانی شد', 'ok'); };

load();
timer = setInterval(load, 60000);
window.addEventListener('beforeunload', () => clearInterval(timer));
