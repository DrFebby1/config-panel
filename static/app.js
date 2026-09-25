/* ---------------------------------------------------------------------------
   پنل مدیریت کانفیگ - منطق سمت مرورگر
   Vanilla JS, no build step, all values escaped before rendering.
--------------------------------------------------------------------------- */
'use strict';

const S = {
  me: null,
  brand: 'پنل مدیریت کانفیگ',
  users: [],
  servers: [],
  stats: null,
  settings: {},
  view: 'dashboard',
  search: '',
  statusFilter: '',
};

const MIN_DAYS = 1;
const MAX_DAYS = 30;
const MIN_QUOTA_GB = 2;

/* ------------------------------- utilities ------------------------------- */

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* Display-only Persian digits. Never applied to values that get copied or
   filled into form fields, so numbers stay machine-readable. */
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
  const digits = index >= 3 ? 2 : index >= 2 ? 1 : 0;
  return fa(value.toFixed(digits) + ' ' + units[index]);
}

function fmtGB(bytes) {
  if (bytes === null || bytes === undefined) return 'بی‌نهایت';
  return fa((Number(bytes) / 1073741824).toFixed(2) + ' گیگ');
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

/* Gregorian epoch seconds -> Jalali date without a time component. */
function fmtDateOnly(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts * 1000).toLocaleDateString('fa-IR', {
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
  } catch (e) { return '—'; }
}

function fmtDay(day) {
  if (!day) return '—';
  const parts = String(day).split('-');
  if (parts.length !== 3) return day;
  return fa(parts[2] + '/' + parts[1]);
}

function barClass(percent) {
  if (percent >= 90) return 'danger';
  if (percent >= 70) return 'warn';
  return '';
}

function statusBadge(user) {
  return '<span class="badge ' + esc(user.status) + '">' + esc(user.status_label) + '</span>';
}

async function api(path, options) {
  const opts = Object.assign({ method: 'GET', headers: {} }, options || {});
  if (opts.body !== undefined && opts.body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  let data = null;
  const text = await response.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
  if (!response.ok) {
    if (response.status === 401 && S.me) { showAuth(); }
    const detail = (data && (data.detail || data.message)) || ('خطای ' + response.status);
    throw new Error(typeof detail === 'string' ? detail : 'خطای سرور');
  }
  return data;
}

function toast(message, kind) {
  const node = document.createElement('div');
  node.className = 'toast ' + (kind || '');
  node.textContent = message;
  $('toast-root').appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

async function copyText(value, label) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    toast((label || 'متن') + ' کپی شد', 'ok');
  } catch (e) {
    toast('کپی نشد، دستی انتخاب کنید', 'err');
  }
}

/* --------------------------------- modal -------------------------------- */

function openModal(innerHtml, afterMount) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'overlay';
  overlay.innerHTML = innerHtml;
  overlay.addEventListener('mousedown', (event) => {
    if (event.target === overlay) closeModal();
  });
  $('modal-root').appendChild(overlay);
  if (typeof afterMount === 'function') afterMount(overlay);
  return overlay;
}

function closeModal() {
  const existing = $('overlay');
  if (existing) existing.remove();
}

function confirmAction(message, onYes) {
  openModal(
    '<div class="modal" style="max-width:420px">' +
      '<h3>تأیید</h3><p>' + esc(message) + '</p>' +
      '<div class="modal-foot">' +
        '<button class="btn danger" id="cf-yes">بله، انجام بده</button>' +
        '<button class="btn ghost" id="cf-no">انصراف</button>' +
      '</div>' +
    '</div>',
    () => {
      $('cf-yes').onclick = () => { closeModal(); onYes(); };
      $('cf-no').onclick = closeModal;
    }
  );
}

/* ---------------------------------- auth -------------------------------- */

function showAuth() {
  S.me = null;
  $('app-view').classList.add('hidden');
  $('auth-view').classList.remove('hidden');
}

function showApp() {
  $('auth-view').classList.add('hidden');
  $('app-view').classList.remove('hidden');
  $('whoami').textContent = S.me ? S.me.username : '';
}

async function boot() {
  let status;
  try {
    status = await api('/api/auth/status');
  } catch (e) {
    document.body.innerHTML = '<p class="empty">اتصال به سرور برقرار نشد: ' + esc(e.message) + '</p>';
    return;
  }
  S.brand = status.brand || S.brand;
  $('brand-name').textContent = S.brand;
  document.title = S.brand;

  const setupMode = !!status.setup_required;
  $('auth-title').textContent = setupMode ? 'ساخت مدیر پنل' : 'ورود به پنل';
  $('auth-sub').textContent = setupMode
    ? 'اولین بار است که پنل اجرا می‌شود. نام کاربری و رمز عبور مدیر را بساز.'
    : 'برای مدیریت کانفیگ‌ها وارد شوید.';
  $('auth-pass-label').textContent = setupMode ? 'رمز عبور (حداقل ۶ کاراکتر)' : 'رمز عبور';
  $('auth-submit').textContent = setupMode ? 'ساخت مدیر و ورود' : 'ورود';
  $('auth-form').dataset.mode = setupMode ? 'setup' : 'login';

  if (status.authenticated) {
    S.me = { username: status.username };
    showApp();
    await Promise.all([loadServers(), refreshUsers(), loadStats(), loadSettings()]);
    renderView();
  } else {
    showAuth();
  }
}

$('auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const mode = $('auth-form').dataset.mode || 'login';
  const username = $('auth-username').value.trim();
  const password = $('auth-password').value;
  $('auth-err').textContent = '';
  $('auth-submit').disabled = true;
  try {
    const result = await api('/api/auth/' + (mode === 'setup' ? 'setup' : 'login'), {
      method: 'POST',
      body: { username, password },
    });
    S.me = { username: result.username };
    $('auth-password').value = '';
    showApp();
    await Promise.all([loadServers(), refreshUsers(), loadStats(), loadSettings()]);
    renderView();
  } catch (e) {
    $('auth-err').textContent = e.message;
  } finally {
    $('auth-submit').disabled = false;
  }
});

$('logout-btn').onclick = async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  location.reload();
};

/* --------------------------------- data --------------------------------- */

async function loadServers() {
  const data = await api('/api/servers');
  S.servers = data.items || [];
}

async function refreshUsers() {
  const params = new URLSearchParams();
  if (S.search) params.set('search', S.search);
  if (S.statusFilter) params.set('status_filter', S.statusFilter);
  const query = params.toString();
  const data = await api('/api/users' + (query ? '?' + query : ''));
  S.users = data.items || [];
}

async function loadStats() {
  S.stats = await api('/api/stats?days=14');
}

async function loadSettings() {
  try { S.settings = await api('/api/settings'); } catch (e) { S.settings = {}; }
}

function defaultDays() { return Number(S.settings.default_duration_days) || MAX_DAYS; }
function defaultQuotaGb() {
  const value = Number(S.settings.default_quota_gb);
  return isFinite(value) && value > 0 ? value : 50;
}
function defaultDailyGb() { return Number(S.settings.default_daily_gb) || 0; }
function defaultUnlimited() {
  const value = Number(S.settings.default_quota_gb);
  return !isFinite(value) || value <= 0;
}

function serverName(id) {
  const found = S.servers.find((s) => Number(s.id) === Number(id));
  return found ? found.name : ('#' + id);
}

/* --------------------------------- router -------------------------------- */

$('nav').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-view]');
  if (!button) return;
  S.view = button.dataset.view;
  renderView();
});

function renderView() {
  ['dashboard', 'users', 'servers', 'settings'].forEach((name) => {
    const section = $('view-' + name);
    if (!section) return;
    section.classList.toggle('hidden', name !== S.view);
  });
  Array.from($('nav').querySelectorAll('button')).forEach((button) => {
    button.classList.toggle('active', button.dataset.view === S.view);
  });
  if (S.view === 'dashboard') renderDashboard();
  if (S.view === 'users') renderUsers();
  if (S.view === 'servers') renderServers();
  if (S.view === 'settings') renderSettings();
}

/* ------------------------------- dashboard ------------------------------- */

function chartSvg(series) {
  const width = 600;
  const height = 170;
  const pad = 24;
  const max = Math.max.apply(null, series.map((p) => p.bytes).concat([1]));
  const slot = (width - pad * 2) / Math.max(series.length, 1);
  const barWidth = Math.max(6, slot * 0.6);
  let bars = '';
  series.forEach((point, index) => {
    const ratio = point.bytes / max;
    const barHeight = Math.max(2, ratio * (height - pad * 2 - 14));
    const x = pad + index * slot + (slot - barWidth) / 2;
    const y = height - pad - barHeight;
    bars += '<rect class="bar-rect' + (point.bytes ? '' : ' empty') + '" x="' +
      x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barWidth.toFixed(1) +
      '" height="' + barHeight.toFixed(1) + '" rx="3"><title>' +
      esc(point.day) + ': ' + esc(fmtBytes(point.bytes)) + '</title></rect>';
    if (index % 2 === 0 || series.length <= 8) {
      bars += '<text x="' + (x + barWidth / 2).toFixed(1) + '" y="' + (height - 8) +
        '" text-anchor="middle">' + esc(fmtDay(point.day)) + '</text>';
    }
  });
  return '<svg class="chart" viewBox="0 0 ' + width + ' ' + height + '">' +
    '<defs><linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="#38bdf8"/><stop offset="100%" stop-color="#6366f1"/>' +
    '</linearGradient></defs>' + bars + '</svg>';
}

function renderDashboard() {
  const stats = S.stats;
  if (!stats) { $('view-dashboard').innerHTML = '<div class="empty">در حال بارگذاری…</div>'; return; }
  const counts = stats.status_counts || {};
  const totalQuota = stats.total_quota_bytes
    ? fmtGB(stats.total_quota_bytes) : 'بی‌نهایت';
  const percent = stats.total_quota_bytes
    ? Math.min(100, (stats.total_used_bytes * 100) / stats.total_quota_bytes) : 0;

  const recent = S.users.slice(0, 6);
  const recentRows = recent.map((user) =>
    '<tr>' +
      '<td><a href="#" data-open-user="' + user.id + '">' + esc(user.name) + '</a></td>' +
      '<td>' + statusBadge(user) + '</td>' +
      '<td class="nowrap">' + esc(fmtBytes(user.used_bytes)) +
        (user.unlimited ? ' / بی‌نهایت' : ' / ' + esc(fmtBytes(user.quota_bytes))) + '</td>' +
      '<td class="nowrap">' + fa(user.days_left) + ' روز مانده</td>' +
    '</tr>'
  ).join('');

  $('view-dashboard').innerHTML =
    '<div class="grid stats">' +
      statCard('کاربران', stats.users_total, counts.active + ' فعال', 'accent') +
      statCard('مصرف امروز', fmtBytes(stats.today_bytes),
        'تا الان · ' + fmtDate(Math.floor(Date.now() / 1000)), 'ok') +
      statCard('حجم کل مصرف‌شده', fmtBytes(stats.total_used_bytes), 'از ' + totalQuota, '') +
      statCard('در آستانه انقضا', stats.expiring_soon, 'کمتر از ۳ روز', stats.expiring_soon ? 'warn' : '') +
    '</div>' +
    '<div class="card mt">' +
      '<div class="card-head"><h2>مصرف ۱۴ روز گذشته</h2>' +
        '<span class="spacer"></span><span class="muted small">منطقه زمانی: ' +
        esc(stats.timezone) + '</span></div>' +
      chartSvg(stats.series || []) +
    '</div>' +
    '<div class="grid two mt">' +
      '<div class="card">' +
        '<h3>وضعیت کانفیگ‌ها</h3>' +
        '<dl class="kv">' +
          '<dt>فعال</dt><dd>' + fa(counts.active || 0) + '</dd>' +
          '<dt>در انتظار اتصال</dt><dd>' + fa(counts.pending || 0) + '</dd>' +
          '<dt>منقضی</dt><dd>' + fa(counts.expired || 0) + '</dd>' +
          '<dt>حجم تمام‌شده</dt><dd>' + fa(counts.exhausted || 0) + '</dd>' +
          '<dt>سقف روزانه پر</dt><dd>' + fa(counts.daily_limited || 0) + '</dd>' +
          '<dt>غیرفعال</dt><dd>' + fa(counts.disabled || 0) + '</dd>' +
        '</dl>' +
        '<p class="muted small" style="margin-top:.8rem">مجموع ظرفیت تخصیص‌داده‌شده: ' +
          esc(totalQuota) + '</p>' +
        '<div class="bar"><i style="width:' + percent.toFixed(1) + '%"></i></div>' +
      '</div>' +
      '<div class="card">' +
        '<h3>آخرین کاربران</h3>' +
        (recentRows
          ? '<div class="table-scroll"><table><thead><tr><th>نام</th><th>وضعیت</th>' +
            '<th>مصرف</th><th>زمان</th></tr></thead><tbody>' + recentRows + '</tbody></table></div>'
          : '<div class="empty">هنوز کاربری ساخته نشده است.</div>') +
      '</div>' +
    '</div>';

  $('view-dashboard').querySelectorAll('[data-open-user]').forEach((link) => {
    link.onclick = (event) => { event.preventDefault(); openUserDetail(Number(link.dataset.openUser)); };
  });
}

function statCard(label, value, sub, kind) {
  return '<div class="stat ' + (kind || '') + '">' +
    '<div class="label">' + esc(label) + '</div>' +
    '<div class="value">' + esc(fa(value)) + '</div>' +
    (sub ? '<div class="sub">' + esc(fa(sub)) + '</div>' : '') +
  '</div>';
}

/* --------------------------------- users --------------------------------- */

function renderUsers() {
  const filterOptions = [
    ['', 'همه'], ['active', 'فعال'], ['pending', 'در انتظار اتصال'],
    ['expired', 'منقضی'], ['exhausted', 'حجم تمام‌شده'],
    ['daily_limited', 'سقف روزانه پر'], ['disabled', 'غیرفعال'],
  ].map((pair) =>
    '<option value="' + pair[0] + '"' + (S.statusFilter === pair[0] ? ' selected' : '') + '>' +
    esc(pair[1]) + '</option>'
  ).join('');

  const rows = S.users.map((user) => {
    const percent = Math.min(100, user.usage_percent || 0);
    const dailyCell = user.daily_unlimited
      ? '<span class="muted small">بدون سقف</span>'
      : esc(fmtBytes(user.daily_used_bytes)) + ' / ' + esc(fmtBytes(user.daily_quota_bytes));
    return '<tr>' +
      '<td><a href="#" data-open-user="' + user.id + '">' + esc(user.name) + '</a>' +
        (user.note ? '<div class="muted small">' + esc(user.note) + '</div>' : '') + '</td>' +
      '<td>' + statusBadge(user) + '</td>' +
      '<td style="min-width:150px">' +
        '<div class="nowrap small">' + esc(fmtBytes(user.used_bytes)) + ' / ' +
          esc(user.unlimited ? 'بی‌نهایت' : fmtBytes(user.quota_bytes)) + '</div>' +
        '<div class="bar"><i class="' + barClass(percent) + '" style="width:' + percent.toFixed(1) + '%"></i></div>' +
        '<div class="muted small">' + (user.unlimited ? 'بدون محدودیت' : 'باقی‌مانده: ' + esc(fmtBytes(user.remaining_bytes))) + '</div>' +
      '</td>' +
      '<td class="nowrap small">' + fa(user.days_left) + ' مانده<br>' + fa(user.days_used) + ' مصرف‌شده</td>' +
      '<td class="small nowrap">' + dailyCell + '</td>' +
      '<td class="small nowrap">' + esc(user.expires_at ? fmtDateOnly(user.expires_at) : '—') + '</td>' +
      '<td><div class="actions">' +
        '<button class="btn sm" data-open-user="' + user.id + '">جزئیات</button>' +
        '<button class="btn sm" data-copy="' + esc(user.sub_url) + '">کپی لینک</button>' +
        '<button class="btn sm ok" data-renew="' + user.id + '">تمدید</button>' +
        '<button class="btn sm danger" data-delete="' + user.id + '">حذف</button>' +
      '</div></td>' +
    '</tr>';
  }).join('');

  $('view-users').innerHTML =
    '<div class="card">' +
      '<div class="card-head">' +
        '<h2>کاربران (' + fa(S.users.length) + ')</h2>' +
        '<span class="spacer"></span>' +
        '<input id="user-search" placeholder="جست‌وجو بر اساس نام، یادداشت یا لینک" ' +
          'value="' + esc(S.search) + '" style="max-width:280px">' +
        '<select id="user-status-filter" style="max-width:180px">' + filterOptions + '</select>' +
        '<button class="btn primary" id="new-user">+ کاربر جدید</button>' +
      '</div>' +
      (S.users.length
        ? '<div class="table-scroll"><table><thead><tr>' +
            '<th>نام</th><th>وضعیت</th><th>حجم</th><th>زمان</th>' +
            '<th>مصرف امروز</th><th>انقضا</th><th>عملیات</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<div class="empty">کاربری پیدا نشد. با دکمه «کاربر جدید» اولین کانفیگ را بساز.</div>') +
    '</div>';

  const search = $('user-search');
  let searchTimer = null;
  search.oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      S.search = search.value.trim();
      await refreshUsers();
      renderUsers();
      const box = $('user-search');
      if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
    }, 320);
  };
  $('user-status-filter').onchange = async (event) => {
    S.statusFilter = event.target.value;
    await refreshUsers();
    renderUsers();
  };
  $('new-user').onclick = () => openUserForm(null);

  $('view-users').querySelectorAll('[data-open-user]').forEach((node) => {
    node.onclick = (event) => {
      event.preventDefault();
      openUserDetail(Number(node.dataset.openUser));
    };
  });
  $('view-users').querySelectorAll('[data-copy]').forEach((node) => {
    node.onclick = () => copyText(node.dataset.copy, 'لینک اشتراک');
  });
  $('view-users').querySelectorAll('[data-renew]').forEach((node) => {
    node.onclick = () => openRenewForm(Number(node.dataset.renew));
  });
  $('view-users').querySelectorAll('[data-delete]').forEach((node) => {
    node.onclick = () => {
      const id = Number(node.dataset.delete);
      const user = S.users.find((u) => u.id === id);
      confirmAction('کاربر «' + (user ? user.name : id) + '» و همه سابقه مصرفش حذف شود؟', async () => {
        await api('/api/users/' + id, { method: 'DELETE' });
        toast('کاربر حذف شد', 'ok');
        await refreshAll();
      });
    };
  });
}

async function refreshAll() {
  await Promise.all([refreshUsers(), loadStats(), loadServers()]);
  renderView();
}

/* ------------------------------ user form ------------------------------- */

function serverCheckboxes(selected) {
  if (!S.servers.length) {
    return '<p class="muted small">هنوز سروری تعریف نشده. از تب «سرورها» یکی بساز تا کانفیگ ساخته شود.</p>';
  }
  return S.servers.map((server) =>
    '<label class="check"><input type="checkbox" value="' + server.id + '"' +
      ((selected || []).map(Number).includes(Number(server.id)) ? ' checked' : '') + '>' +
      '<span>' + esc(server.name) + ' <span class="pill">' + esc(server.protocol) + '</span>' +
      ' <span class="muted small">' + esc(server.address) + ':' + esc(server.port) + '</span></span></label>'
  ).join('');
}

function openUserForm(user) {
  const editing = !!user;
  const quotaGb = editing ? (user.quota_bytes ? user.quota_bytes / 1073741824 : 0) : defaultQuotaGb();
  const dailyGb = editing ? (user.daily_quota_bytes ? user.daily_quota_bytes / 1073741824 : 0) : defaultDailyGb();
  const unlimited = editing ? !!user.unlimited : defaultUnlimited();
  const noDaily = editing ? !!user.daily_unlimited : defaultDailyGb() <= 0;
  const days = editing ? user.duration_days : defaultDays();

  openModal(
    '<div class="modal">' +
      '<div class="modal-head"><h3>' + (editing ? 'ویرایش کاربر' : 'کاربر جدید') + '</h3></div>' +
      '<label class="field"><span>نام کاربر</span>' +
        '<input id="f-name" value="' + esc(editing ? user.name : '') + '" placeholder="مثلاً: علی رضایی"></label>' +
      '<label class="field"><span>یادداشت (اختیاری)</span>' +
        '<input id="f-note" value="' + esc(editing ? user.note : '') + '" placeholder="شماره تماس، توضیح…"></label>' +

      '<label class="field"><span>مدت اعتبار: <b id="f-days-label">' + fa(days) + '</b> روز</span>' +
        '<input id="f-days" type="range" min="' + MIN_DAYS + '" max="' + MAX_DAYS + '" value="' + days + '">' +
        '<div class="hint">از ۱ تا ۳۰ روز قابل انتخاب است.</div></label>' +

      '<label class="check"><input type="checkbox" id="f-unlimited"' + (unlimited ? ' checked' : '') + '>' +
        '<span>حجم بی‌نهایت</span></label>' +
      '<label class="field"><span>حجم کل (گیگابایت)</span>' +
        '<input id="f-quota" type="number" min="' + MIN_QUOTA_GB + '" step="1" value="' +
          (unlimited ? MIN_QUOTA_GB : (quotaGb || 50)) + '"></label>' +
      '<div class="hint" style="margin-top:-.5rem;margin-bottom:.7rem">' +
        'حداقل حجم قابل تخصیص ' + fa(MIN_QUOTA_GB) + ' گیگابایت است یا گزینه بی‌نهایت را بزن.</div>' +

      '<label class="check"><input type="checkbox" id="f-nodaily"' + (noDaily ? ' checked' : '') + '>' +
        '<span>بدون محدودیت روزانه</span></label>' +
      '<label class="field"><span>محدودیت مصرف روزانه (گیگابایت)</span>' +
        '<input id="f-daily" type="number" min="0.1" step="0.1" value="' + (dailyGb || 1) + '">' +
        '<div class="hint">هر روز ساعت ۰۰:۰۰ به وقت تهران صفر می‌شود.</div></label>' +

      '<label class="check"><input type="checkbox" id="f-onfirst"' +
        (editing && user.activate_on_first_use ? ' checked' : '') + '>' +
        '<span>شروع شمارش روز‌ها از اولین اتصال</span></label>' +
      '<label class="check"><input type="checkbox" id="f-enabled"' +
        ((!editing || user.enabled) ? ' checked' : '') + '><span>فعال باشد</span></label>' +

      '<div class="field"><span>سرورها / کانفیگ‌ها</span>' +
        '<div style="max-height:170px;overflow-y:auto;border:1px solid var(--line);border-radius:10px;padding:.5rem">' +
        serverCheckboxes(editing ? user.server_ids : S.servers.map((s) => s.id)) + '</div></div>' +

      '<label class="field"><span>کانفیگ دستی (هر خط یک لینک، اختیاری)</span>' +
        '<textarea id="f-extra" placeholder="vless://...">' +
        esc(editing ? user.extra_configs : '') + '</textarea></label>' +

      '<div id="f-err" class="err"></div>' +
      '<div class="modal-foot">' +
        '<button class="btn primary" id="f-save">' + (editing ? 'ذخیره تغییرات' : 'ساخت کانفیگ') + '</button>' +
        '<button class="btn ghost" id="f-cancel">انصراف</button>' +
      '</div>' +
    '</div>',
    (overlay) => {
      const quotaInput = $('f-quota');      const dailyInput = $('f-daily');
      const unlimitedBox = $('f-unlimited');
      const noDailyBox = $('f-nodaily');
      const syncQuota = () => {
        quotaInput.disabled = unlimitedBox.checked;
        quotaInput.style.opacity = unlimitedBox.checked ? '.5' : '1';
      };
      const syncDaily = () => {
        dailyInput.disabled = noDailyBox.checked;
        dailyInput.style.opacity = noDailyBox.checked ? '.5' : '1';
      };
      unlimitedBox.onchange = syncQuota;
      noDailyBox.onchange = syncDaily;
      syncQuota();
      syncDaily();
      $('f-days').oninput = (event) => { $('f-days-label').textContent = fa(event.target.value); };
      $('f-cancel').onclick = closeModal;
      $('f-save').onclick = async () => {
        const payload = {
          name: $('f-name').value.trim(),
          note: $('f-note').value.trim(),
          duration_days: Number($('f-days').value),
          quota_gb: unlimitedBox.checked ? 0 : Number(quotaInput.value),
          daily_gb: noDailyBox.checked ? 0 : Number(dailyInput.value),
          server_ids: Array.from(overlay.querySelectorAll('input[type=checkbox][value]'))
            .filter((box) => box.checked).map((box) => Number(box.value)),
          extra_configs: $('f-extra').value,
          activate_on_first_use: $('f-onfirst').checked,
          enabled: $('f-enabled').checked,
        };
        $('f-err').textContent = '';
        try {
          if (editing) {
            await api('/api/users/' + user.id, { method: 'PATCH', body: payload });
          } else {
            const created = await api('/api/users', { method: 'POST', body: payload });
            toast('کانفیگ ساخته شد', 'ok');
            closeModal();
            await refreshAll();
            openUserDetail(created.id);
            return;
          }
          toast('ذخیره شد', 'ok');
          closeModal();
          await refreshAll();
        } catch (e) {
          $('f-err').textContent = e.message;
        }
      };
    }
  );
}

/* ----------------------------- user detail ------------------------------ */

async function openUserDetail(userId) {
  let user;
  try {
    user = await api('/api/users/' + userId);
  } catch (e) { toast(e.message, 'err'); return; }

  const percent = Math.min(100, user.usage_percent || 0);
  const quotaText = user.unlimited ? 'بی‌نهایت' : fmtBytes(user.quota_bytes);
  const remText = user.unlimited ? 'بی‌نهایت' : fmtBytes(user.remaining_bytes);
  const dailyText = user.daily_unlimited
    ? 'بدون محدودیت'
    : fmtBytes(user.daily_used_bytes) + ' از ' + fmtBytes(user.daily_quota_bytes);

  const configs = (user.configs_all && user.configs_all.length ? user.configs_all : user.configs) || [];
  const configsHtml = configs.length
    ? configs.map((config) =>
        '<div class="cfg">' +
          '<div class="cfg-head">' +
            '<span class="pill">' + esc(config.protocol) + '</span>' +
            '<span class="small muted">' + esc(config.server) + '</span>' +
            '<span class="spacer"></span>' +
            '<button class="btn sm" data-copy="' + esc(config.link) + '">کپی</button>' +
          '</div>' +
          '<div class="cfg-url">' + esc(config.link) + '</div>' +
        '</div>'
      ).join('')
    : '<div class="empty">کانفیگی برای این کاربر ساخته نشده. سرورها را در فرم ویرایش انتخاب کن.</div>';

  const blockedNote = user.blocked
    ? '<div class="blocked-note">وضعیت: ' + esc(user.status_label) +
      ' — لینک اشتراک برای کاربر خالی برگردانده می‌شود تا اتصال برقرار نشود.</div>'
    : '';

  openModal(
    '<div class="modal wide">' +
      '<div class="modal-head">' +
        '<h3>' + esc(user.name) + '</h3>' + statusBadge(user) +
        '<span class="spacer"></span>' +
        '<button class="btn sm ghost" id="d-close">بستن</button>' +
      '</div>' +
      blockedNote +
      '<div class="grid two">' +
        '<div class="card">' +
          '<h4>حجم</h4>' +
          '<div class="small">مصرف‌شده: <b>' + esc(fmtBytes(user.used_bytes)) + '</b> از ' + esc(quotaText) + '</div>' +
          '<div class="bar mt" style="height:9px"><i class="' + barClass(percent) +
            '" style="width:' + percent.toFixed(1) + '%"></i></div>' +
          '<div class="muted small">باقی‌مانده: ' + esc(remText) + '</div>' +
          '<hr style="border:none;border-top:1px solid var(--line-soft);margin:.8rem 0">' +
          '<div class="small">مصرف امروز: <b>' + esc(dailyText) + '</b></div>' +
          (user.daily_unlimited ? '' :
            '<div class="bar mt"><i class="' + barClass(Math.min(100, user.daily_percent || 0)) +
            '" style="width:' + Math.min(100, user.daily_percent || 0).toFixed(1) + '%"></i></div>') +
        '</div>' +
        '<div class="card">' +
          '<h4>زمان</h4>' +
          '<dl class="kv">' +
            '<dt>مدت کل</dt><dd>' + fa(user.days_total) + ' روز</dd>' +
            '<dt>مصرف‌شده</dt><dd>' + fa(user.days_used) + ' روز</dd>' +
            '<dt>باقی‌مانده</dt><dd>' + fa(user.days_left) + ' روز</dd>' +
            '<dt>تاریخ انقضا</dt><dd>' + esc(user.expires_at ? fmtDateOnly(user.expires_at) : 'در انتظار شروع') + '</dd>' +
            '<dt>ساخته شده</dt><dd>' + esc(fmtDateOnly(user.created_at)) + '</dd>' +
            '<dt>آخرین اتصال</dt><dd>' + esc(user.last_online_at ? fmtDate(user.last_online_at) : '—') + '</dd>' +
          '</dl>' +
        '</div>' +
      '</div>' +

      '<div class="card mt">' +
        '<h4>لینک اختصاصی کاربر</h4>' +
        '<div class="cfg">' +
          '<div class="cfg-head"><span class="pill">صفحه مدیریت کاربر</span><span class="spacer"></span>' +
            '<a class="btn sm" href="' + esc(user.portal_url) + '" target="_blank" rel="noopener">باز کردن</a>' +
            '<button class="btn sm" data-copy="' + esc(user.portal_url) + '">کپی</button></div>' +
          '<div class="cfg-url">' + esc(user.portal_url) + '</div>' +
        '</div>' +
        '<div class="cfg">' +
          '<div class="cfg-head"><span class="pill">لینک اشتراک (Subscription)</span><span class="spacer"></span>' +
            '<a class="btn sm" href="' + esc(user.sub_url) + '" target="_blank" rel="noopener">تست</a>' +
            '<button class="btn sm" data-copy="' + esc(user.sub_url) + '">کپی</button></div>' +
          '<div class="cfg-url">' + esc(user.sub_url) + '</div>' +
        '</div>' +
        '<div class="center mt"><div class="qr-box" style="display:inline-block">' +
          '<img src="/api/users/' + user.id + '/qr.svg" alt="QR" style="width:100%;height:100%">' +
        '</div><div class="muted small">اسکن QR برای افزودن سریع</div></div>' +
      '</div>' +

      '<div class="card mt"><h4>کانفیگ‌ها (' + fa(configs.length) + ')</h4>' + configsHtml + '</div>' +

      '<div class="card mt">' +
        '<h4>عملیات</h4>' +
        '<div class="modal-foot">' +
          '<button class="btn" id="d-traffic">افزودن حجم</button>' +
          '<button class="btn ok" id="d-renew">تمدید</button>' +
          '<button class="btn warn" id="d-reset-usage">صفر کردن مصرف</button>' +
          '<button class="btn warn" id="d-reset-link">لینک جدید</button>' +
          '<button class="btn" id="d-toggle">' + (user.enabled ? 'غیرفعال کردن' : 'فعال کردن') + '</button>' +
          '<button class="btn" id="d-edit">ویرایش</button>' +
          '<button class="btn danger" id="d-delete">حذف</button>' +
        '</div>' +
      '</div>' +
    '</div>',
    () => {
      document.querySelectorAll('#overlay [data-copy]').forEach((node) => {
        node.onclick = () => copyText(node.dataset.copy, 'لینک');
      });
      $('d-close').onclick = closeModal;
      $('d-edit').onclick = () => { closeModal(); openUserForm(user); };
      $('d-renew').onclick = () => openRenewForm(user.id);
      const act = async (path, method, message, body) => {
        try {
          await api('/api/users/' + user.id + path, { method: method || 'POST', body: body });
          toast(message, 'ok');
          closeModal();
          await refreshAll();
          openUserDetail(user.id);
        } catch (e) { toast(e.message, 'err'); }
      };
      $('d-reset-usage').onclick = () =>
        confirmAction('همه مصرف و سابقه این کاربر صفر شود؟', () =>
          act('/reset-usage', 'POST', 'مصرف صفر شد'));
      $('d-reset-link').onclick = () =>
        confirmAction('لینک قبلی از کار می‌افتد و لینک جدید ساخته می‌شود. مطمئنی؟', () =>
          act('/reset-link', 'POST', 'لینک جدید ساخته شد'));
      $('d-toggle').onclick = () =>
        act('', 'PATCH', user.enabled ? 'کاربر غیرفعال شد' : 'کاربر فعال شد', { enabled: !user.enabled });
      $('d-traffic').onclick = () => openTrafficForm(user);
      $('d-delete').onclick = () =>
        confirmAction('کاربر «' + user.name + '» کاملاً حذف شود؟', async () => {
          try {
            await api('/api/users/' + user.id, { method: 'DELETE' });
            toast('کاربر حذف شد', 'ok');
            closeModal();
            await refreshAll();
          } catch (e) { toast(e.message, 'err'); }
        });
    }
  );
}

function openTrafficForm(user) {
  openModal(
    '<div class="modal" style="max-width:440px">' +
      '<h3>افزودن حجم — ' + esc(user.name) + '</h3>' +
      '<p class="muted small">حجم فعلی مصرف‌شده: ' + esc(fmtBytes(user.used_bytes)) + '</p>' +
      '<label class="field"><span>مقدار (گیگابایت)</span>' +
        '<input id="t-gb" type="number" step="1" min="1" value="10"></label>' +
      '<div id="t-err" class="err"></div>' +
      '<div class="modal-foot">' +
        '<button class="btn primary" id="t-add">افزودن به مصرف</button>' +
        '<button class="btn" id="t-add-quota">افزودن به حجم کل مجاز</button>' +
        '<button class="btn ghost" id="t-cancel">انصراف</button>' +
      '</div>' +
    '</div>',
    () => {
      $('t-cancel').onclick = closeModal;
      const gb = () => Number($('t-gb').value) || 0;
      $('t-add').onclick = async () => {
        const bytes = Math.round(gb() * 1073741824);
        if (bytes <= 0) { $('t-err').textContent = 'مقدار نامعتبر'; return; }
        try {
          await api('/api/users/' + user.id + '/traffic', { method: 'POST', body: { delta_bytes: bytes } });
          toast('حجم اضافه شد', 'ok');
          closeModal(); await refreshAll(); openUserDetail(user.id);
        } catch (e) { $('t-err').textContent = e.message; }
      };
      $('t-add-quota').onclick = async () => {
        const extra = gb();
        if (extra <= 0) { $('t-err').textContent = 'مقدار نامعتبر'; return; }
        const currentGb = user.unlimited ? 0 : user.quota_bytes / 1073741824;
        const target = user.unlimited ? extra : currentGb + extra;
        try {
          await api('/api/users/' + user.id, { method: 'PATCH', body: { quota_gb: target } });
          toast('حجم کل افزایش یافت', 'ok');
          closeModal(); await refreshAll(); openUserDetail(user.id);
        } catch (e) { $('t-err').textContent = e.message; }
      };
    }
  );
}

function openRenewForm(userId) {
  const user = S.users.find((u) => u.id === userId);
  openModal(
    '<div class="modal" style="max-width:440px">' +
      '<h3>تمدید کانفیگ</h3>' +
      '<label class="field"><span>مدت جدید: <b id="r-days-label">۳۰</b> روز</span>' +
        '<input id="r-days" type="range" min="' + MIN_DAYS + '" max="' + MAX_DAYS + '" value="30">' +
        '<div class="hint">شمارش از همین حالا شروع می‌شود. حجم مصرف‌شده دست‌نخورده می‌ماند.</div></label>' +
      '<label class="check"><input type="checkbox" id="r-reset"><span>مصرف هم صفر شود</span></label>' +
      '<div id="r-err" class="err"></div>' +
      '<div class="modal-foot">' +
        '<button class="btn primary" id="r-save">تمدید</button>' +
        '<button class="btn ghost" id="r-cancel">انصراف</button>' +
      '</div>' +
    '</div>',
    () => {
      $('r-days').oninput = (event) => { $('r-days-label').textContent = fa(event.target.value); };
      $('r-cancel').onclick = closeModal;
      $('r-save').onclick = async () => {
        try {
          await api('/api/users/' + userId + '/renew', {
            method: 'POST',
            body: { days: Number($('r-days').value), reset_usage: $('r-reset').checked },
          });
          toast('تمدید شد', 'ok');
          closeModal(); await refreshAll();
          if (user && S.view === 'dashboard') openUserDetail(userId);
        } catch (e) { $('r-err').textContent = e.message; }
      };
    }
  );
}

/* -------------------------------- servers ------------------------------- */

const PROTOCOL_FIELDS = {
  vless: ['network', 'security', 'path', 'host', 'sni', 'fingerprint', 'public_key', 'short_id', 'flow', 'service_name'],
  vmess: ['network', 'security', 'path', 'host', 'sni', 'fingerprint', 'alter_id', 'service_name'],
  trojan: ['network', 'security', 'path', 'host', 'sni', 'fingerprint', 'service_name'],
  shadowsocks: ['method', 'plugin', 'plugin_opts'],
};

const FIELD_LABELS = {
  network: 'نوع شبکه (tcp / ws / grpc / http)',
  security: 'امنیت (none / tls / reality)',
  path: 'مسیر (path)',
  host: 'هاست هدر / دامنه',
  sni: 'SNI',
  fingerprint: 'اثر انگشت (chrome / firefox / safari)',
  public_key: 'Reality Public Key',
  short_id: 'Reality Short ID',
  spider_x: 'Reality SpiderX',
  flow: 'Flow (مثلاً xtls-rprx-vision)',
  service_name: 'gRPC service name',
  grpc_mode: 'gRPC mode',
  alter_id: 'Alter ID (vmess)',
  method: 'روش رمزنگاری Shadowsocks',
  plugin: 'پلاگین Shadowsocks',
  plugin_opts: 'تنظیمات پلاگین',
  http_header: 'HTTP header type (۱ = بله)',
  cipher: 'رمز vmess (auto)',
  allow_insecure: 'اجازه گواهی نامعتبر (۱ = بله)',
};

function renderServers() {
  const rows = S.servers.map((server) =>
    '<tr>' +
      '<td>' + esc(server.name) + '</td>' +
      '<td><span class="pill">' + esc(server.protocol) + '</span></td>' +
      '<td class="mono small">' + esc(server.address) + ':' + esc(server.port) + '</td>' +
      '<td class="small muted">' + esc(
        [server.params.network, server.params.security,
         server.params.path ? 'path=' + server.params.path : '']
          .filter(Boolean).join(' · ') || '—') + '</td>' +
      '<td>' + (server.enabled
        ? '<span class="badge active">فعال</span>'
        : '<span class="badge disabled">غیرفعال</span>') + '</td>' +
      '<td><div class="actions">' +
        '<button class="btn sm" data-edit-server="' + server.id + '">ویرایش</button>' +
        '<button class="btn sm danger" data-del-server="' + server.id + '">حذف</button>' +
      '</div></td>' +
    '</tr>'
  ).join('');

  $('view-servers').innerHTML =
    '<div class="card">' +
      '<div class="card-head"><h2>سرورها و کانفیگ‌ها</h2><span class="spacer"></span>' +
        '<button class="btn primary" id="new-server">+ سرور جدید</button></div>' +
      '<p class="muted small">هر سرور یک «ورودی» است. پنل از روی این تعریف‌ها لینک واقعی VLESS / VMess / ' +
        'Trojan / Shadowsocks برای هر کاربر می‌سازد.</p>' +
      (S.servers.length
        ? '<div class="table-scroll"><table><thead><tr><th>نام</th><th>پروتکل</th><th>آدرس</th>' +
          '<th>تنظیمات</th><th>وضعیت</th><th>عملیات</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<div class="empty">سروری ثبت نشده است. با «سرور جدید» شروع کن.</div>') +
    '</div>';

  $('new-server').onclick = () => openServerForm(null);
  $('view-servers').querySelectorAll('[data-edit-server]').forEach((node) => {
    node.onclick = () => openServerForm(S.servers.find((s) => s.id === Number(node.dataset.editServer)));
  });
  $('view-servers').querySelectorAll('[data-del-server]').forEach((node) => {
    node.onclick = () => {
      confirmAction('این سرور حذف شود؟ کانفیگ‌های کاربران که به آن وصل بودند دیگر ساخته نمی‌شوند.', async () => {
        await api('/api/servers/' + node.dataset.delServer, { method: 'DELETE' });
        toast('سرور حذف شد', 'ok');
        await refreshAll();
      });
    };
  });
}

function openServerForm(server) {
  const editing = !!server;
  const params = (server && server.params) || {};

  const fieldsFor = (protocol, values) => {
    const keys = PROTOCOL_FIELDS[protocol] || [];
    if (protocol === 'shadowsocks') {
      const methods = ['aes-256-gcm', 'aes-128-gcm', 'chacha20-ietf-poly1305',
        '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', '2022-blake3-chacha20-poly1305'];
      return '<label class="field"><span>' + FIELD_LABELS.method + '</span><select data-param="method">' +
        methods.map((m) => '<option' + (values.method === m || (!values.method && m === 'chacha20-ietf-poly1305') ? ' selected' : '') +
          '>' + m + '</option>').join('') + '</select></label>' +
        extraField('plugin', values) + extraField('plugin_opts', values);
    }
    return keys.map((key) => {
      if (key === 'security') {
        return '<label class="field"><span>' + FIELD_LABELS.security + '</span>' +
          '<select data-param="security">' +
          ['none', 'tls', 'reality'].map((v) => '<option' + (values.security === v ? ' selected' : '') +
            '>' + v + '</option>').join('') + '</select></label>';
      }
      if (key === 'network') {
        return '<label class="field"><span>' + FIELD_LABELS.network + '</span>' +
          '<select data-param="network">' +
          ['tcp', 'ws', 'grpc', 'http'].map((v) => '<option' + (values.network === v ? ' selected' : '') +
            '>' + v + '</option>').join('') + '</select></label>';
      }
      return extraField(key, values);
    }).join('');
  };

  const extraField = (key, values) =>
    '<label class="field"><span>' + esc(FIELD_LABELS[key] || key) + '</span>' +
      '<input data-param="' + esc(key) + '" value="' + esc(values[key] === undefined ? '' : values[key]) + '"></label>';

  openModal(
    '<div class="modal">' +
      '<div class="modal-head"><h3>' + (editing ? 'ویرایش سرور' : 'سرور جدید') + '</h3></div>' +
      '<label class="field"><span>نام (در نام کانفیگ کاربر نمایش داده می‌شود)</span>' +
        '<input id="s-name" value="' + esc(editing ? server.name : '') + '" placeholder="مثلاً: آلمان ۱"></label>' +
      '<label class="field"><span>پروتکل</span>' +
        '<select id="s-protocol">' +
        ['vless', 'vmess', 'trojan', 'shadowsocks'].map((p) =>
          '<option value="' + p + '"' + (editing && server.protocol === p ? ' selected' : '') + '>' + p + '</option>').join('') +
        '</select></label>' +
      '<div class="grid two">' +
        '<label class="field"><span>آدرس (دامنه یا IP)</span>' +
          '<input id="s-address" value="' + esc(editing ? server.address : '') + '" placeholder="example.com"></label>' +
        '<label class="field"><span>پورت</span>' +
          '<input id="s-port" type="number" min="1" max="65535" value="' + (editing ? server.port : 443) + '"></label>' +
      '</div>' +
      '<label class="check"><input type="checkbox" id="s-enabled"' +
        ((!editing || server.enabled) ? ' checked' : '') + '><span>فعال</span></label>' +
      '<h4 class="mt">تنظیمات اتصال</h4>' +
      '<div id="s-params">' + fieldsFor(editing ? server.protocol : 'vless', params) + '</div>' +
      '<div id="s-err" class="err"></div>' +
      '<div class="modal-foot">' +
        '<button class="btn primary" id="s-save">ذخیره</button>' +
        '<button class="btn ghost" id="s-cancel">انصراف</button>' +
      '</div>' +
    '</div>',
    (overlay) => {
      const collected = {};
      overlay.querySelectorAll('[data-param]').forEach((node) => { collected[node.dataset.param] = node.value; });
      $('s-protocol').onchange = (event) => {
        const values = {};
        overlay.querySelectorAll('[data-param]').forEach((node) => { values[node.dataset.param] = node.value; });
        $('s-params').innerHTML = fieldsFor(event.target.value, values);
      };
      $('s-cancel').onclick = closeModal;
      $('s-save').onclick = async () => {
        const body = {
          name: $('s-name').value.trim(),
          protocol: $('s-protocol').value,
          address: $('s-address').value.trim(),
          port: Number($('s-port').value),
          enabled: $('s-enabled').checked,
          params: {},
        };
        overlay.querySelectorAll('[data-param]').forEach((node) => {
          const value = node.value.trim();
          if (value) body.params[node.dataset.param] = value;
        });
        $('s-err').textContent = '';
        try {
          if (editing) await api('/api/servers/' + server.id, { method: 'PUT', body: body });
          else await api('/api/servers', { method: 'POST', body: body });
          toast('سرور ذخیره شد', 'ok');
          closeModal();
          await refreshAll();
        } catch (e) { $('s-err').textContent = e.message; }
      };
    }
  );
}

/* -------------------------------- settings ------------------------------ */

async function renderSettings() {
  let values = {};
  try { values = await api('/api/settings'); } catch (e) { toast(e.message, 'err'); }

  $('view-settings').innerHTML =
    '<div class="card">' +
      '<h2>تنظیمات عمومی</h2>' +
      '<label class="field"><span>نام برند پنل</span><input id="g-brand" value="' + esc(values.brand || '') + '"></label>' +
      '<label class="field"><span>آدرس عمومی پنل (برای ساخت لینک‌ها)</span>' +
        '<input id="g-base" value="' + esc(values.sub_base_url || '') + '" placeholder="https://your-app.up.railway.app">' +
        '<div class="hint">اگر خالی باشد، آدرس از خود درخواست ساخته می‌شود.</div></label>' +
      '<label class="field"><span>لینک پشتیبانی</span>' +
        '<input id="g-support" value="' + esc(values.support_url || '') + '" placeholder="https://t.me/username"></label>' +
      '<div class="grid two">' +
        '<label class="field"><span>مدت پیش‌فرض (۱ تا ۳۰ روز)</span>' +
          '<input id="g-days" type="number" min="1" max="30" value="' + esc(values.default_duration_days || 30) + '"></label>' +
        '<label class="field"><span>حجم پیش‌فرض (گیگابایت، ۰ = بی‌نهایت)</span>' +
          '<input id="g-quota" type="number" min="0" step="1" value="' + esc(values.default_quota_gb || 0) + '"></label>' +
        '<label class="field"><span>محدودیت روزانه پیش‌فرض (گیگابایت، ۰ = بدون سقف)</span>' +
          '<input id="g-daily" type="number" min="0" step="0.1" value="' + esc(values.default_daily_gb || 0) + '"></label>' +
      '</div>' +
      '<div id="g-err" class="err"></div>' +
      '<div class="modal-foot"><button class="btn primary" id="g-save">ذخیره تنظیمات</button></div>' +
    '</div>' +

    '<div class="card mt">' +
      '<h2>API ثبت مصرف</h2>' +
      '<p class="muted small">اسکریپت روی سرور کانفیگ با این کلید مصرف هر کاربر را به پنل می‌فرستد. ' +
        'کلید را جایی امن نگه دار.</p>' +
      '<div class="row wrap">' +
        '<input id="g-key" readonly value="••••••••" style="max-width:420px">' +
        '<button class="btn" id="g-show-key">نمایش کلید</button>' +
        '<button class="btn" id="g-copy-key">کپی</button>' +
        '<button class="btn warn" id="g-rotate-key">ساخت کلید جدید</button>' +
      '</div>' +
      '<pre class="cfg-url" style="margin-top:.8rem">' +
        esc('curl -X POST ' + location.origin + '/api/report \\\n' +
            '  -H "X-API-Key: <KEY>" -H "Content-Type: application/json" \\\n' +
            '  -d \'{"user":"<sub_token|uuid|name>","used_bytes":123456789}\'') +
      '</pre>' +
    '</div>' +

    '<div class="card mt">' +
      '<h2>تغییر رمز عبور مدیر</h2>' +
      '<label class="field"><span>رمز فعلی</span><input id="p-cur" type="password"></label>' +
      '<label class="field"><span>رمز جدید (حداقل ۶ کاراکتر)</span><input id="p-new" type="password"></label>' +
      '<div id="p-err" class="err"></div>' +
      '<div class="modal-foot"><button class="btn primary" id="p-save">تغییر رمز</button></div>' +
    '</div>' +

    '<div class="card mt">' +
      '<h2>پشتیبان‌گیری</h2>' +
      '<p class="muted small">خروجی کامل JSON از کاربران، سرورها و سابقه مصرف.</p>' +
      '<a class="btn" href="/api/export" target="_blank" rel="noopener">دانلود فایل پشتیبان</a>' +
    '</div>';

  $('g-save').onclick = async () => {
    $('g-err').textContent = '';
    try {
      await api('/api/settings', {
        method: 'PUT',
        body: {
          brand: $('g-brand').value,
          sub_base_url: $('g-base').value,
          support_url: $('g-support').value,
          default_duration_days: Number($('g-days').value),
          default_quota_gb: Number($('g-quota').value),
          default_daily_gb: Number($('g-daily').value),
        },
      });
      toast('تنظیمات ذخیره شد', 'ok');
      S.brand = $('g-brand').value || S.brand;
      $('brand-name').textContent = S.brand;
    } catch (e) { $('g-err').textContent = e.message; }
  };

  let apiKeyLoaded = false;
  $('g-show-key').onclick = async () => {
    try {
      const data = await api('/api/settings/api-key');
      $('g-key').value = data.api_key;
      apiKeyLoaded = true;
    } catch (e) { toast(e.message, 'err'); }
  };
  $('g-copy-key').onclick = async () => {
    if (!apiKeyLoaded) { await $('g-show-key').click(); }
    copyText($('g-key').value, 'کلید API');
  };
  $('g-rotate-key').onclick = () =>
    confirmAction('کلید قبلی از کار می‌افتد تا اسکریپت‌ها را به‌روز کنی. مطمئنی؟', async () => {
      const data = await api('/api/settings/api-key/rotate', { method: 'POST' });
      $('g-key').value = data.api_key;
      apiKeyLoaded = true;
      toast('کلید جدید ساخته شد', 'ok');
    });

  $('p-save').onclick = async () => {
    $('p-err').textContent = '';
    try {
      await api('/api/auth/password', {
        method: 'POST',
        body: { current_password: $('p-cur').value, new_password: $('p-new').value },
      });
      toast('رمز تغییر کرد، دوباره وارد شوید', 'ok');
      setTimeout(() => location.reload(), 1200);
    } catch (e) { $('p-err').textContent = e.message; }
  };
}

/* --------------------------------- start -------------------------------- */

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeModal();
});

boot();
