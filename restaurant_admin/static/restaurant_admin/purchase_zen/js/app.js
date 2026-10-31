// --- CONSTANTS & STATE ---
const STORAGE_KEY = 'elite-erp-invoices';
const CONFIG_KEY = 'elite-erp-config';
const CONFIG_IDS = ['cfg-skipvch', 'cfg-interest', 'cfg-godown', 'cfg-disc', 'cfg-tax', 'cfg-remark', 'cfg-free', 'cfg-mrp', 'cfg-exp', 'cfg-incl', 'cfg-round'];
// How a bank payment went out (typed by first letter, like any list)
const VIA = [
    { id: 'upi', name: 'UPI', keys: ['u'] },
    { id: 'neft', name: 'NEFT', keys: ['n'] },
    { id: 'rtgs', name: 'RTGS', keys: ['r'] },
    { id: 'imps', name: 'IMPS', keys: ['i'] },
    { id: 'cheque', name: 'Cheque', keys: ['c', 'q', 'chq'] }
];
const payLabel = p => p.mode === 'cash' ? 'Cash' : p.mode === 'linked' ? 'Paid earlier' : (VIA.find(v => v.id === p.mode) || { name: 'Bank' }).name;
const CASH_LIMIT = 10000;      // cash to one supplier in a day above this is not allowed as an expense
const MSME_MAX_DAYS = 45;      // an MSME supplier has to be paid within this
const MAX_FILE_MB = 10;
// One draft per restaurant / branch (and one per bill being edited), so two outlets never share a half-typed bill
const DRAFT_KEY = `elite-erp-draft:${ZEN.draftScope}`;
const THEME_KEY = 'elite-erp-theme';

const TAX_HEADS = ['cgst', 'sgst', 'igst', 'cess'];
// A line says what kind of purchase it is only when it is not kitchen stock
const ITEM_TYPE_TAG = { asset: 'Asset', consumable: 'Consumable', service: 'Expense' };
// The full item list (Space) is grouped by what the things are, in this order
const ITEM_GROUPS = { stock: 'Kitchen & bar stock', consumable: 'Consumables', asset: 'Assets', service: 'Expenses' };
const TYPE_ORDER = Object.keys(ITEM_GROUPS);
// A line typed in the other section is still found — and moved to its own section on Enter
const CROSS = { item: 'charge', charge: 'item' };
const MOVES_KEY = 'elite-erp-moves';   // how many moves this operator has seen: the first few are explained
const THEMES = ['auto', 'dark', 'light'];

// List heads, pinned on top of every list: "Create" hands over to the master form, "End of List" closes a row list
const END_OF_LIST_KINDS = ['item', 'charge', 'payledger'];
const CREATABLE = ['party', 'item', 'charge', 'godown', 'payledger', 'per'];
const KIND_LABEL = { party: 'Supplier', item: 'Item / Expense', charge: 'Charge / Discount', godown: 'Godown', payledger: 'Cash / Bank', per: 'Unit' };

// One number style everywhere: money 2 decimals, quantity 3, percent 2, days whole
const FMT = {
    money: n => n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    qty: n => n.toLocaleString('en-IN', { minimumFractionDigits: 3, maximumFractionDigits: 3 }),
    pct: n => n.toFixed(2),
    int: n => String(Math.round(n))
};

let itemsOpen = false;      // body stays empty until the cursor comes down from the header
let chargesOpen = false;    // charges & discount lines revealed
let settleOpen = false;     // payment popup showing
let paymentSeen = false;    // the payment has been looked at once for this bill (saving shows it first otherwise)
let isSaving = false;
let billTax = {};          // GST heads / round off as printed on the bill — typed only when the paper differs (Alt+T)
let focusBeforePanel = null;
let focusBeforeSettle = null;
let focusBeforeBox = null;
let pointerFocus = false;
let navColumn = null;       // the column of a ↑ ↓ walk, while it lasts: { el, left, right }
let shownGrand = 0;         // the total as last announced (a finished line shows what it added)
let lastBill = null;        // latest computeBill() result
let lastDiag = [];          // likely reasons when the e-invoice total and ours differ
let lineSeq = 0;            // item lines get ids L1, L2… (a charge "on line 2" follows the line, not its position)
let einvoice = null;      // what the e-invoice QR code said
let attachments = [];       // bill copies: { id, name, type, size, blob, url }
let viewerIdx = 0;
let draftTimer = null;
let restoring = false;      // true while a draft is being rebuilt, so it isn't re-saved half-built
let lastRemoved = null;     // last deleted row, for Ctrl+Z
let saveCardTimer = null;
let focusBeforeHelp = null;

const $ = id => document.getElementById(id);
const parseNum = v => parseFloat(String(v).replace(/,/g, '')) || 0;
const num = el => parseNum(el.value);
// A round off can be minus: "-0.40", "− 0.40", "+0.30"
const parseSigned = v => round2(parseFloat(String(v).replace(/[−–]/g, '-').replace(/[^0-9.+-]/g, '')) || 0);
const signed = n => `${n > 0 ? '+ ' : n < 0 ? '− ' : ''}${formatNum(Math.abs(n))}`;
// Half-up to paise via exponent notation: 8.325 -> 8.33 (plain *100 gives 832.4999…)
const round2 = n => Math.sign(n) * Number(Math.round(Math.abs(n) + 'e2') + 'e-2');
function formatField(el) {
    const f = el.dataset && el.dataset.fmt;
    if (f && el.value.trim() !== '') el.value = FMT[f](parseNum(el.value));
}
const isDiscOn = () => document.body.classList.contains('show-disc');
// Packed goods (Settings): free qty, MRP, batch & expiry — off, they are not asked, counted or saved
const freeOn = () => document.body.classList.contains('show-free');
const mrpOn = () => document.body.classList.contains('show-mrp');
const expOn = () => document.body.classList.contains('show-exp');
const formatNum = n => n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const formatINR = n => `₹ ${formatNum(n)}`;
const fmtPct = n => String(+n.toFixed(2));
const unitLabel = u => (UNIT_MASTER[u] || { label: u }).label;
// "payledger" = cash-in-hand + bank accounts, searched together
// A supplier's payments made before the bill (still to link) come first in their "Paid from" list
const ledgerPool = kind => kind === 'payledger' ? [...linkEntries(getParty()), ...LEDGER.cash, ...LEDGER.bank] : LEDGER[kind];
const findLedger = (kind, id) => id ? ledgerPool(kind).find(e => e.id === id) || null : null;
// What a picked entry shows in its box (an earlier payment reads "Paid Thu 28 Oct · 4:15 pm")
const shownName = e => e.label || e.name;
// The bills already saved on the server (sent with the page, the bill being edited left out), plus this session's
const savedInvoices = () => ZEN.saved;
const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const isService = item => !!item && item.type === 'service';

// --- DATES: type it your way (10.1.26, 10/01, 100126, 10 jan, jan 10, t, y, -2) ---
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const today = () => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); };
const isoOf = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const fromIso = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
const fmtDate = d => `${String(d.getDate()).padStart(2, '0')}-${String(d.getMonth() + 1).padStart(2, '0')}-${d.getFullYear()}`;
const addDays = (d, n) => { const r = new Date(d); r.setDate(r.getDate() + n); return r; };
// Same day n months later; the 31st becomes the month's last day
const addMonths = (d, n) => { const r = new Date(d.getFullYear(), d.getMonth() + n, 1); r.setDate(Math.min(d.getDate(), new Date(r.getFullYear(), r.getMonth() + 1, 0).getDate())); return r; };

function parseDateInput(text, { allowFuture = false } = {}) {
    const s = text.trim().toLowerCase().replace(/(\d)(st|nd|rd|th)\b/g, '$1');
    const now = today();
    if (['t', 'today', 'aaj', '.'].includes(s)) return now;
    if (['y', 'yesterday', 'kal'].includes(s)) return addDays(now, -1);
    if (/^[+-]\d{1,3}$/.test(s)) return addDays(now, parseInt(s, 10));

    const monthOf = w => { const i = MONTHS.indexOf(w.slice(0, 3)); return i < 0 ? 0 : i + 1; };
    let d, m, y, mt, dayOnly = false;
    if ((mt = s.match(/^(\d{1,2})[\s.\/-]*([a-z]{3,9})[\s.,\/-]*(\d{2}|\d{4})?$/))) { d = +mt[1]; m = monthOf(mt[2]); y = mt[3]; }
    else if ((mt = s.match(/^([a-z]{3,9})[\s.\/-]*(\d{1,2})(?:[\s.,\/-]+(\d{2}|\d{4}))?$/))) { m = monthOf(mt[1]); d = +mt[2]; y = mt[3]; }
    else if (/^\d{1,4}([\s.\/-]+\d{1,4}){1,2}$/.test(s)) { [d, m, y] = s.split(/[\s.\/-]+/); d = +d; m = +m; }
    else if (/^\d{1,8}$/.test(s)) {
        const n = s.length;
        if (n <= 2) { d = +s; m = now.getMonth() + 1; dayOnly = true; }
        else if (n === 3) { d = +s.slice(0, 2); m = +s.slice(2); if (d > 31) { d = +s[0]; m = +s.slice(1); } }
        else if (n === 4) { d = +s.slice(0, 2); m = +s.slice(2); }
        else if (n === 6 || n === 8) { d = +s.slice(0, 2); m = +s.slice(2, 4); y = s.slice(4); }
        else return null;
    } else return null;
    if (!m || !d) return null;

    const yearGiven = y !== undefined && y !== '';
    const year = yearGiven ? (String(y).length === 2 ? 2000 + +y : +y) : now.getFullYear();
    const make = (yr, mo) => { const dt = new Date(yr, mo - 1, d); return dt.getMonth() === ((mo - 1) % 12 + 12) % 12 && dt.getDate() === d ? dt : null; };
    let date = make(year, m);
    // No year/month typed and the date lands in the future: it means the previous month / year
    if (!yearGiven && !allowFuture && (!date || date > now)) {
        date = dayOnly ? make(m === 1 ? year - 1 : year, m === 1 ? 12 : m - 1) : make(year - 1, m);
    }
    return date;
}

function relativeDay(d) {
    const diff = Math.round((d - today()) / 86400000);
    const rel = diff === 0 ? 'today' : diff === -1 ? 'yesterday' : diff === 1 ? 'tomorrow' : diff < 0 ? `${-diff} days ago` : `in ${diff} days`;
    return `${WEEKDAYS[d.getDay()]} · ${rel}`;
}

const dateOf = el => el && el.dataset.iso ? fromIso(el.dataset.iso) : null;

function setDate(el, d) {
    // Another date: the old-bill mark (if any) was for the one before
    const changed = el.dataset.iso !== isoOf(d);
    el.value = fmtDate(d);
    el.dataset.iso = isoOf(d);
    el.classList.remove('input-error');
    if (changed) el.classList.remove('old-bill');
    const hint = document.querySelector(`.date-hint[data-for="${el.id}"]`);
    if (hint) { hint.textContent = relativeDay(d); if (changed) hint.classList.remove('warn'); }
}

// Parse whatever was typed; returns false (and flags) if unreadable
function commitDate(el, silent = false) {
    const raw = el.value.trim();
    const hint = document.querySelector(`.date-hint[data-for="${el.id}"]`);
    if (!raw) { el.dataset.iso = ''; if (hint) hint.textContent = ''; return true; }
    const d = parseDateInput(raw, { allowFuture: el.dataset.future === '1' });
    if (!d) {
        if (silent) el.classList.add('input-error');
        else flagError(el, `Couldn't read "${raw}" — try 17.1.26, 17/01, 170126 or 17 jan`);
        return false;
    }
    setDate(el, d);
    if (el.id === 'inp-vdate') { updatePurchaseNo(); prefillBillDate(); }
    updateTotals();
    return true;
}

// The bill date is on screen from the start (= our purchase date) and follows it until the operator types another
function prefillBillDate() {
    const bd = $('inp-date'), ours = dateOf($('inp-vdate'));
    if (!ours || (bd.dataset.iso && bd.dataset.auto !== '1')) return;
    setDate(bd, ours);
    bd.dataset.auto = '1';
}

// --- A BILL DATE FAR BEHIND (a 24 typed for 26, a month slip — or a bill really handed over late): asked once,
// next to the date, never blocked. Enter again keeps it; typing replaces it. Counted from the day the goods came ---
const OLD_BILL_DAYS = 60;
// { short: beside the date, long: the whole of it } — or null when the date is not far behind
function oldBillText(el) {
    const bill = dateOf(el), ours = dateOf($('inp-vdate')) || today();
    if (!bill) return null;
    const days = Math.round((ours - bill) / 86400000);
    if (days <= OLD_BILL_DAYS) return null;
    const creditTill = new Date(+('20' + fyOf(bill).slice(3)), 10, 30); // 30 Nov after its financial year
    const lastFy = fyOf(bill) !== fyOf(ours), creditOver = COMPANY.gstCredit && today() > creditTill;
    return {
        short: `${days} days old${lastFy ? ` · FY ${fyOf(bill)}` : ''}${creditOver ? ' · GST credit over' : ''}`,
        long: `Bill dated ${el.value} — ${days} days before the goods came${lastFy ? `, in the last financial year (${fyOf(bill)})` : ''}${creditOver ? ' — its GST credit time is over (s.16(4))' : ''}`
    };
}
// true = go on; false = asked (the cursor stays on the date). Said beside the date and in the key bar, where the eye
// already is; a message only when the screen is too narrow to show the note beside the date
function confirmOldBill(el) {
    const t = oldBillText(el), hint = document.querySelector(`.date-hint[data-for="${el.id}"]`);
    el.classList.toggle('old-bill', !!t);
    if (hint) { hint.classList.toggle('warn', !!t); if (t) { hint.textContent = t.short; hint.title = t.long; } else hint.title = ''; }
    if (!t || el.dataset.oldOk === el.dataset.iso) return true;
    el.dataset.oldOk = el.dataset.iso;
    focusField(el);
    renderHint(el);
    if (!hint || hint.offsetParent === null) showToast(`${t.long}. Right? Enter again keeps it`, 'warn');
    return false;
}

// Supplier bill date <= our purchase date <= today
function checkHeaderDates(el) {
    const bill = $('inp-date').dataset.iso, ours = $('inp-vdate').dataset.iso;
    if (ours && ours > isoOf(today())) return flagError($('inp-vdate'), 'Purchase date cannot be in the future');
    if (bill && ours && bill > ours) return flagError(el, 'Bill date cannot be after our purchase date');
    return true;
}

// --- SHELF LIFE on a line: "45d", "6m", "1y" counted from the bill date (expiry) ---
const DUE_UNITS = [
    { unit: 'd', words: ['d', 'day', 'days', 'din'], one: 'day', many: 'days' },
    { unit: 'w', words: ['w', 'wk', 'wks', 'week', 'weeks', 'hafta', 'hafte'], one: 'week', many: 'weeks' },
    { unit: 'm', words: ['m', 'mo', 'mon', 'mnth', 'month', 'months', 'mahina', 'mahine'], one: 'month', many: 'months' },
    { unit: 'y', words: ['y', 'yr', 'yrs', 'year', 'years', 'saal', 'sal'], one: 'year', many: 'years' }
];

function parseDue(text, from) {
    const s = text.trim().toLowerCase().replace(/\.$/, '');
    if (['0', 'now', 'today', 'turant', 'abhi'].includes(s)) return { date: from, label: 'now' };
    const m = s.match(/^(\d{1,3})\s*([a-z]*)$/);
    if (m) {
        const n = +m[1], u = m[2] ? DUE_UNITS.find(x => x.words.includes(m[2])) : DUE_UNITS[0]; // a bare number = days
        if (u) {
            const date = u.unit === 'd' ? addDays(from, n) : u.unit === 'w' ? addDays(from, 7 * n) : addMonths(from, u.unit === 'm' ? n : 12 * n);
            return { date, label: n === 0 ? 'now' : `${n} ${n === 1 ? u.one : u.many}` };
        }
    }
    const d = parseDateInput(text, { allowFuture: true });
    return d ? { date: d, label: fmtDate(d) } : null;
}

// --- UDHAAR DUE: two boxes, Days and Date. The operator types whichever is at hand (the supplier's terms are in
// days, a bill may print a date) and the other is worked out — counted from the supplier's bill date ---
const dueFrom = () => dateOf($('inp-date')) || dateOf($('inp-vdate')) || today();
const dueDaysOf = v => /^\d{1,4}$/.test(String(v).trim()) ? +String(v).trim() : null;
// Blank days = due on the bill date
const currentDue = () => { const raw = $('inp-due').value.trim(), d = raw === '' ? 0 : dueDaysOf(raw); return d === null ? null : { days: d, date: addDays(dueFrom(), d) }; };

// Days are the truth; the date box shows what they come to (unless it is being typed in)
function syncDueDate() {
    const dd = $('inp-duedate'), due = currentDue();
    if (document.activeElement === dd) return;
    dd.value = due ? fmtDate(due.date) : '';
    dd.dataset.iso = due ? isoOf(due.date) : '';
    dd.classList.remove('input-error');
}

// The date typed: the days follow
function commitDueDate(silent = false) {
    const dd = $('inp-duedate'), raw = dd.value.trim();
    const fail = msg => { if (silent) dd.classList.add('input-error'); else flagError(dd, msg); return false; };
    if (!raw) { syncDueDate(); return true; }
    const d = parseDateInput(raw, { allowFuture: true });
    if (!d) return fail(`Couldn't read "${raw}" — type the due date like 15.11 or 15/11/26`);
    const days = Math.round((d - dueFrom()) / 86400000);
    if (days < 0) return fail('The due date is before the bill date');
    const due = $('inp-due');
    due.value = String(days);
    delete due.dataset.auto;
    dd.value = fmtDate(d);
    dd.dataset.iso = isoOf(d);
    updateTotals();
    return true;
}

// The days typed (a date typed here by habit is taken as the date)
function commitDue(silent = false) {
    const el = $('inp-due'), raw = el.value.trim();
    const fail = msg => { if (silent) el.classList.add('input-error'); else flagError(el, msg); return null; };
    if (raw && dueDaysOf(raw) === null) {
        if (!parseDateInput(raw, { allowFuture: true })) return fail('Type the days (like 15) — or the due date in the next box');
        $('inp-duedate').value = raw;
        el.value = '';
        return commitDueDate(silent) ? currentDue() : null;
    }
    syncDueDate();
    updateTotals();
    return currentDue();
}

// --- EXPIRY on a line: the date as printed, the month / year a packet prints ("11/26", "nov 2026" = its last day),
// or the shelf life from the bill date ("6m", "45d"). A day and month alone ("15.3") is the next one after the bill
// date — an expiry is never in the past by default. An item with a shelf life in its master starts with its estimate ---
const monthEnd = (y, m) => new Date(y, m, 0);
function parseExpiry(text, from) {
    const s = text.trim().toLowerCase();
    let mt;
    if ((mt = s.match(/^(\d{1,2})[\s.\/-]+(\d{4}|\d{2})$/)) && +mt[1] >= 1 && +mt[1] <= 12 && (mt[2].length === 4 || +mt[2] > 12)) {
        return monthEnd(mt[2].length === 2 ? 2000 + +mt[2] : +mt[2], +mt[1]);
    }
    if ((mt = s.match(/^([a-z]{3,9})[\s.,\/-]*(\d{4})$/)) && MONTHS.includes(mt[1].slice(0, 3))) return monthEnd(+mt[2], MONTHS.indexOf(mt[1].slice(0, 3)) + 1);
    const due = parseDue(text, from);
    if (!due || due.label === 'now') return null;
    const dayMonth = /^\d{1,2}[\s.\/-]+\d{1,2}$/.test(s) || /^\d{3,4}$/.test(s) || /^(\d{1,2}[\s.\/-]*[a-z]{3,9}|[a-z]{3,9}[\s.\/-]*\d{1,2})$/.test(s);
    if (dayMonth && due.date < from) return new Date(due.date.getFullYear() + 1, due.date.getMonth(), due.date.getDate());
    return due.date;
}

// Shelf life said the way a packet says it: "5 days", "3 months", "1 year"
function lifeText(days) {
    const n = (v, one) => `${v} ${one}${v === 1 ? '' : 's'}`;
    return days % 365 === 0 ? n(days / 365, 'year') : days % 30 === 0 ? n(days / 30, 'month') : n(days, 'day');
}

// The estimate from the shelf life, counted from the bill date — it follows the bill date until a date is typed
function estimateExpiry(el, item) {
    const d = addDays(dueFrom(), item.life);
    el.value = fmtDate(d);
    el.dataset.iso = isoOf(d);
    el.dataset.auto = '1';
}

function commitExpiry(el, silent = false) {
    const raw = el.value.trim();
    if (!raw) { el.dataset.iso = ''; delete el.dataset.auto; updateTotals(); return true; }
    if (el.dataset.auto === '1') return true; // the estimate, kept as it is
    const d = parseExpiry(raw, dueFrom());
    if (!d) {
        if (silent) el.classList.add('input-error');
        else flagError(el, 'Type the expiry like 15.12.26 or 11/26 (month / year), or the shelf life: 6m, 45d');
        return false;
    }
    el.value = fmtDate(d);
    el.dataset.iso = isoOf(d);
    updateTotals();
    return true;
}

// --- OUR PURCHASE NUMBER: PUR/<FY>/<seq>, Indian financial year (Apr-Mar) ---
function fyOf(d) {
    const start = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
    return `${String(start % 100).padStart(2, '0')}-${String((start + 1) % 100).padStart(2, '0')}`;
}

// The next number of the restaurant's purchase series (Document Sequence settings) — the server confirms it on save
function nextPurchaseNo() {
    return ZEN.nextPurchaseNo;
}

// Auto-numbered until the operator types their own; a typed number is kept
function updatePurchaseNo() {
    const el = $('inp-purno');
    if (el.value.trim() === '' || el.dataset.auto === '1') { el.value = nextPurchaseNo(); el.dataset.auto = '1'; }
}

// Where a new bill starts: our number & date, or straight at the supplier (settings)
const firstField = () => $('cfg-skipvch').checked ? $('inp-supplier') : $('inp-purno');

// --- INITIALIZATION ---
document.addEventListener('DOMContentLoaded', () => {
    setDate($('inp-vdate'), today());
    prefillBillDate();
    updatePurchaseNo();
    loadConfig();

    setTheme(document.documentElement.dataset.theme || 'auto', false);

    addItemRow();
    applyConfig();
    document.querySelector('main').addEventListener('scroll', () => { Suggest.reposition(); Glide.hide(); }, { passive: true });
    window.addEventListener('resize', () => { Suggest.reposition(); fitImage(); });
    $('btn-full').addEventListener('click', toggleFullScreen);
    document.addEventListener('fullscreenchange', () => $('btn-full').classList.toggle('on', !!document.fullscreenElement));
    $('billbox').querySelectorAll('.bill-tax').forEach(inp => inp.addEventListener('input', () => { billTax[inp.dataset.head] = inp.value; updateTotals(); }));
    // Buttons that act on the bill never take the cursor out of it
    $('foot-pay').addEventListener('mousedown', e => e.preventDefault());
    $('await-pill').addEventListener('mousedown', e => e.preventDefault());
    initBillCopy();

    // A power cut or refresh must never cost the operator a half-entered bill
    const restored = restoreDraft();
    if (restored) showToast('Unsaved bill restored — continue, or Alt+N twice for a fresh bill');

    // Start of the bill, or the next empty item row of a restored bill
    setTimeout(() => focusField(restored && itemsOpen ? firstEmptyItem() : firstField()), 100);
});

// Numeric fields accept digits and one dot only (qty / rate / free also take a unit: 15kg, 5b, 29/kg, 6p);
// the qty also takes billed + free, as the bill prints it: 10+1 (one dot in each number)
document.addEventListener('input', e => {
    const el = e.target;
    if (!el.classList || !el.classList.contains('num')) return;
    const isQty = el.classList.contains('item-qty');
    let v = el.value.replace(isQty ? /[^0-9.a-zA-Z/ +]/g : el.dataset.units || el.classList.contains('item-free') ? /[^0-9.a-zA-Z/ ]/g : /[^0-9.]/g, '');
    const plus = v.indexOf('+');
    if (plus >= 0) v = v.slice(0, plus + 1) + v.slice(plus + 1).replace(/\+/g, '');
    v = v.split('+').map(n => { const dot = n.indexOf('.'); return dot >= 0 ? n.slice(0, dot + 1) + n.slice(dot + 1).replace(/\./g, '') : n; }).join('+');
    if (v !== el.value) el.value = v;
}, true);

document.addEventListener('input', e => {
    const el = e.target;
    if (BOXES[el.id]) return; // the Go box runs its own list (shell.js)
    if (el.id === 'aw-q') { awaitIdx = 0; awaitRows = awaitCards(el.value); awaitIdx = firstLinkable(); return renderAwaitList(); }
    // Clear an error highlight as soon as the user edits the field
    el.classList.remove('input-error');
    el.dataset.touched = '1';
    delete el.dataset.auto;
    if (el.classList.contains('item-exp')) el.classList.remove('est'); // typed over the estimate: it reads as typed
    // Ledger fields: typing unbinds the old entry and opens suggestions
    if (el.dataset.ledger) {
        delete el.dataset.id;
        if (el.id === 'inp-supplier') {
            if (einvoice) { einvoice = null; renderEInvoiceTag(); }
            onPartyChange();
        }
        if (el.classList.contains('pay-ledger')) { delete el.closest('.pay-row').dataset.mode; updateTotals(); }
    }
    // Pick-lists keep their last choice until a new one is picked
    if (el.dataset.ledger || el.dataset.pick) {
        if (el.value.trim()) Suggest.show(el);
        else Suggest.close();
    } else if (el.classList.contains('item-qty')) Suggest.showUnits(el);
    if (el.id === 'inp-due' && dueDaysOf(el.value) !== null) syncDueDate();
    if (['inp-due', 'inp-interest', 'inp-latefee'].includes(el.id)) updateTotals();
    // The hint follows the typing: power-line preview
    if (el.closest && el.closest('.item-wrapper')) renderHint(el);
    scheduleDraft();
});
document.addEventListener('change', scheduleDraft);

// A click means "I want to edit here"; keyboard arrival means "I'm passing through"
document.addEventListener('pointerdown', () => { pointerFocus = true; setTimeout(() => pointerFocus = false, 0); });
document.addEventListener('focusin', e => { if (!pointerFocus) e.target.dataset && (e.target.dataset.touched = ''); else e.target.dataset.touched = '1'; });

// Every focus change: close what was opened and not used above it, and show what this field wants
document.addEventListener('focusin', e => {
    Glide.follow(e.target);
    // A look at the Go box or the awaiting-bill drawer leaves the bill exactly as it was (nothing collapses, no blank
    // line is dropped)
    if (BOXES[e.target.id] || (e.target.closest && e.target.closest('#await'))) return renderHint(e.target);
    // Phone: while a line is being typed (on-screen keyboard up) the footer shrinks to the total
    document.body.classList.toggle('typing', !!(e.target.closest && e.target.closest('main')));
    dropBlankCharges(e.target);
    collapseUnused(stepOf(e.target));
    renderHint(e.target);
});

// A blank charge line the cursor has left is dropped, so no empty gaps sit between the charges
function dropBlankCharges(target) {
    document.querySelectorAll('.charge-row').forEach(r => {
        const blank = !r.querySelector('.charge-name').value.trim() && r.querySelector('.charge-rate').value === '' && r.querySelector('.charge-amt').value === '';
        if (blank && !r.contains(target)) r.remove();
    });
}

document.addEventListener('focusout', e => {
    const el = e.target;
    if (!el.dataset) return;
    if (el.dataset.units) commitUnitField(el, true);
    else formatField(el);
    if (el.classList.contains('date-input')) commitDate(el, true);
    if (el.classList.contains('item-exp')) commitExpiry(el, true);
    if (el.classList.contains('item-free')) commitFree(el, true);
    if (el.classList.contains('item-batch')) el.value = el.value.trim().toUpperCase();
    if (el.id === 'inp-due' && el.value.trim()) commitDue(true);
    if (el.id === 'inp-duedate') commitDueDate(true);
    if (Suggest.input === el) Suggest.close();
    // Which lines a charge is for: read as typed (all / a line no. / "1,3" / a name); unreadable = left as it was
    if (el.classList.contains('charge-on')) return commitOn(el, null, true);
    if (!el.dataset.ledger && !el.dataset.pick) return;

    // Pick-list left half-typed: take the best match, or put the last choice back
    if (el.dataset.pick) {
        const bound = boundEntry(el);
        if (bound && shownName(bound) === el.value) return;
        const match = el.value.trim() ? optionsFor(el, el.value)[0] : null;
        if (match) bindField(el, match);
        else el.value = bound ? bound.name : '';
        return;
    }
    // Tabbing/clicking away binds an exact ledger name match
    if (!el.dataset.id && el.value.trim()) {
        const typed = el.value.trim().toLowerCase();
        const exact = searchLedger(el.dataset.ledger, typed).find(x => x.name.toLowerCase() === typed);
        // A line of the other section only moves on Enter, never silently on blur
        if (exact && exact.kind !== CROSS[el.dataset.ledger]) bindLedger(el, exact);
    }
});

// Settings close when the mouse goes back to the bill
document.addEventListener('pointerdown', e => {
    if ($('settings-panel').classList.contains('open') && !e.target.closest('#settings-panel, #btn-settings')) toggleSettings(false);
});

// --- TOAST ---
// A message never holds the operator up: the next key clears it (the field it is about stays marked)
let toastTimer;
function hideToast() { clearTimeout(toastTimer); $('toast').classList.remove('show'); }
// isError: true = red (a slip to fix), 'warn' = amber (worth a look, not wrong), false = green (done)
function showToast(msg, isError = false) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.toggle('error', isError === true);
    t.classList.toggle('warn', isError === 'warn');
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), isError ? 4200 : 2800);
}

// A problem on the bill itself closes the popup first, so the cursor can land on it; a payment problem opens it
function flagError(el, msg) {
    const inPopup = $('settle').contains(el);
    if (settleOpen && !inPopup) closeSettle(false);
    if (!settleOpen && inPopup) openSettle();
    el.classList.add('input-error');
    focusField(el);
    showToast(msg, true);
    return false;
}

// --- LEDGER SEARCH ---
// Hindi words come in many English spellings: majduri = majdoori = mazdoori, cheeni = chini, bhaada = bhada
const sound = s => s.toLowerCase().replace(/ck/g, 'k').replace(/z/g, 'j').replace(/q/g, 'k').replace(/w/g, 'v').replace(/ph/g, 'f')
    .replace(/([bcdgjkpst])h/g, '$1').replace(/ee/g, 'i').replace(/oo/g, 'u').replace(/iy/g, 'i').replace(/(.)\1+/g, '$1');

// The field's own ledger first. Item and charge boxes also find the other section's ledgers (listed apart, moved on
// Enter), so "majduri" typed among the items is never a dead end that ends in a duplicate ledger
function searchLedger(kind, query) {
    const q = query.trim().toLowerCase();
    if (!q) return kind === 'item' ? [...LEDGER.item].sort((a, b) => TYPE_ORDER.indexOf(a.type || 'stock') - TYPE_ORDER.indexOf(b.type || 'stock')) : ledgerPool(kind);
    const qs = sound(q);
    const rank = pool => pool.map(e => {
        const name = e.name.toLowerCase(), alias = (e.alias || '').toLowerCase();
        const words = [...name.split(/[\s(/]+/), ...alias.split(' ')].filter(Boolean);
        const score = name.startsWith(q) || (e.gstin || '').toLowerCase().startsWith(q) ? 0
            : name.split(/[\s(/]+/).some(w => w.startsWith(q)) ? 1
            : alias.split(' ').some(w => w.startsWith(q)) ? 2
            : name.includes(q) || alias.includes(q) ? 3
            : qs.length >= 3 && words.some(w => sound(w).startsWith(qs)) ? 4 : -1;
        return { e, score };
    }).filter(x => x.score >= 0).sort((a, b) => a.score - b.score).map(x => x.e);
    return [...rank(ledgerPool(kind)).slice(0, 8), ...(CROSS[kind] ? rank(LEDGER[CROSS[kind]]).slice(0, 4) : [])];
}

// --- PICK-LISTS: typeable instead of dropdowns (Space or Alt+↓ = full list) ---
const rowItem = el => { const w = el.closest('.item-wrapper'); return w ? findLedger('item', w.querySelector('.item-name').dataset.id) : null; };
// The item lines as they stand; a line's number is its place among them (the S.No. on screen)
const itemLines = () => [...document.querySelectorAll('.item-wrapper')].filter(w => w.querySelector('.item-name').dataset.id);
const PICK = {
    gst: () => GST_RATES.map(r => ({ id: String(r), name: `${r}%` })),
    godown: () => LEDGER.godown,
    via: () => VIA,
    // What the bill's rate is per: the item's units, each with the amount it makes — the right one matches the paper
    per: el => perOptions(el.closest('.item-wrapper')),
    // Which lines a charge is for: all of them, or one picked here ("1,3" or "1-3" typed for several)
    on: () => [{ id: '', name: 'All items' }, ...itemLines().map((w, i) => ({ id: w.dataset.line, name: `${i + 1} · ${w.querySelector('.item-name').value}`, meta: w.querySelector('.item-qty').value }))],
    // GST on a charge: as the goods it is on (the rule for a charge billed with them), none, or its own rate as printed
    cgst: () => [{ id: 'items', name: 'as items' }, { id: 'none', name: 'no GST' }, ...GST_RATES.filter(r => r > 0).map(r => ({ id: String(r), name: `${r}%` }))]
};

// --- A CHARGE FOR SOME LINES ONLY (labour for the 50 katta of atta, not for the masala): '' = all lines,
// otherwise line ids. The box reads "all items", "1 · Wheat Atta" or "lines 1, 3" ---
function onText(ids) {
    const lines = itemLines(), nums = (ids || '').split(',').filter(Boolean).map(id => lines.findIndex(w => w.dataset.line === id) + 1).filter(n => n > 0);
    if (!nums.length) return 'all items';
    return nums.length === 1 ? `${nums[0]} · ${lines[nums[0] - 1].querySelector('.item-name').value}` : `lines ${nums.join(', ')}`;
}

function setOn(el, ids) {
    el.dataset.on = ids;
    el.value = onText(ids);
    el.classList.remove('input-error');
}

// Blank / "all" → every line; "2", "1,3", "1-3" → those lines; a name → the line that has it; a row picked in the list
function commitOn(el, picked = null, silent = false) {
    Suggest.close();
    const lines = itemLines(), raw = el.value.trim().toLowerCase(), was = el.dataset.on || '';
    const fail = msg => { if (silent) setOn(el, was); else flagError(el, msg); return false; };
    let ids;
    if (picked && !picked.special) ids = picked.id;
    else if (raw === onText(was).toLowerCase()) ids = was;
    else if (!raw || /^all/.test(raw)) ids = '';
    else if (/^\d+(\s*[-,]\s*\d+)*$/.test(raw)) {
        const nums = new Set();
        raw.split(/\s*,\s*/).forEach(part => { const [a, b] = part.split(/\s*-\s*/).map(Number); for (let n = a; n <= (b || a); n++) nums.add(n); });
        const bad = [...nums].find(n => n < 1 || n > lines.length);
        if (bad !== undefined) return fail(`There is no line ${bad} — the bill has ${lines.length} line${lines.length === 1 ? '' : 's'}`);
        ids = [...nums].sort((a, b) => a - b).map(n => lines[n - 1].dataset.line).join(',');
    } else {
        const hit = lines.find(w => w.querySelector('.item-name').value.toLowerCase().includes(raw));
        if (!hit) return fail(`No line has "${el.value.trim()}" — type its line no. (2), or several (1,3)`);
        ids = hit.dataset.line;
    }
    if (ids && ids.split(',').length === lines.length) ids = ''; // every line = all
    setOn(el, ids);
    updateTotals();
    return true;
}

// --- UNITS LIVE INSIDE QTY & RATE: "15kg", "5b", "29/kg" ---
const itemUnits = item => item ? item.units : [];
const unitFactor = (item, u) => (itemUnits(item).find(x => x.u === u) || { f: 1 }).f;

// Full name / spelling first, then first letters in this item's own order (so "k" on Atta = Katta, on Tomato = Kg)
function matchUnit(item, typed) {
    const s = typed.toLowerCase();
    const names = u => [u, unitLabel(u).toLowerCase(), ...((UNIT_MASTER[u] || {}).alias || [])];
    const units = itemUnits(item);
    return units.find(x => names(x.u).includes(s)) || units.find(x => names(x.u).some(n => n.startsWith(s))) || null;
}

function splitUnit(el) {
    // "10+1" in the qty while it is typed: the billed part counts (the free part goes to its own box on Enter)
    const text = el.value.replace(/,/g, '').split('+')[0];
    const m = text.match(/^\s*(\d*\.?\d*)\s*\/?\s*([a-zA-Z]*)\s*$/);
    if (!m) return { n: parseNum(text), unit: el.dataset.unit || null, bad: true };
    const n = parseFloat(m[1]) || 0;
    if (!m[2]) return { n, unit: el.dataset.unit || null, bad: false };
    const hit = matchUnit(rowItem(el), m[2]);
    return { n, unit: hit ? hit.u : el.dataset.unit || null, bad: !hit, typed: m[2] };
}

// Base = the item's main unit; amount = qty in base × rate per base
const qtyInBase = el => { const q = splitUnit(el); return q.n * unitFactor(rowItem(el), q.unit); };
const ratePerBase = el => { const r = splitUnit(el); return r.n / unitFactor(rowItem(el), r.unit); };

// The other unit of a two-unit item, for the "(5.000 Bag)" line under the qty
function otherUnit(item, u) {
    const units = itemUnits(item);
    return units.length > 1 ? units.find(x => x.u !== u) : null;
}

// "5b" + Enter → "5.000 Bag" in the same box. The unit is always the one typed (the bill's unit);
// only an item with a single unit takes it without asking. Rate follows the qty unit unless typed for it
function commitUnitField(el, silent = false) {
    if (!el.dataset.units || el.value.trim() === '') return true;
    const item = rowItem(el);
    if (isService(item)) return true;
    const isQty = el.classList.contains('item-qty');
    if (isQty && el.value.includes('+') && !splitFree(el, silent)) return false;
    const r = splitUnit(el);
    const fail = msg => { if (silent) el.classList.add('input-error'); else flagError(el, msg); return false; };
    if (r.bad) return fail(item ? `${item.name} is bought in ${itemUnits(item).map(x => unitLabel(x.u)).join(' / ')} — type the unit with the number, e.g. 10${itemUnits(item)[0].u[0]}` : 'Choose the item first');
    if (isQty && item && !r.unit) {
        fail(`Which unit is on the bill? ${itemUnits(item).map(x => `5${x.u[0]} = 5 ${unitLabel(x.u)}`).join(' · ')}`);
        // Cursor after the number, so the next key ("b") completes it instead of replacing it
        if (!silent) { el.setSelectionRange(el.value.length, el.value.length); el.dataset.touched = '1'; }
        return false;
    }
    const wrapper = el.closest('.item-wrapper');
    const rate = wrapper.querySelector('.item-rate');
    let rateConverted = false;
    if (isQty && r.unit && r.unit !== el.dataset.unit) {
        el.dataset.unit = r.unit;
        // An empty or ledger-filled rate follows the qty unit (ledger rate converted); a typed rate keeps its own unit
        if (!rate.dataset.unitLocked && (rate.dataset.auto === '1' || rate.value === '')) {
            if (rate.dataset.auto === '1' && item) { rate.value = FMT.money(item.lastRate * unitFactor(item, r.unit)); rateConverted = true; }
            rate.dataset.unit = r.unit;
        }
    }
    if (el.classList.contains('item-rate') && r.typed) {
        rate.dataset.unit = r.unit;
        rate.dataset.unitLocked = '1';
    }
    el.value = FMT[el.dataset.fmt](r.n) + (isQty && el.dataset.unit ? ` ${unitLabel(el.dataset.unit)}` : '');
    // The live maths already read "15kg" while typing; only a converted ledger rate needs a fresh amount
    if (rateConverted) wrapper.recalc();
    // The free qty is in the qty's unit unless it says another: shown again for the unit the qty now has
    const free = wrapper.querySelector('.item-free');
    if (isQty && free.value.trim()) commitFree(free, true);
    updateTotals();
    return true;
}

// --- FREE / SCHEME QTY: "10 + 1 free" — not charged, but it comes into the store, so each one costs less ---
// "10+1" in the qty: 10 billed, 1 free (in the qty's unit; "2b+6p" = 2 Box + 6 Pcs free). The Free column comes on
// by itself the first time — the operator said what the bill says, so the screen follows
function splitFree(el, silent) {
    const m = el.value.replace(/,/g, '').match(/^\s*(\d*\.?\d*)\s*([a-zA-Z]*)\s*\+\s*(\d*\.?\d*)\s*([a-zA-Z]*)\s*$/);
    if (!m || !(parseFloat(m[1]) > 0) || !(parseFloat(m[3]) > 0)) {
        if (silent) el.classList.add('input-error');
        else flagError(el, 'Billed + free, as the bill prints it: 10+1 · 2b+6p');
        return false;
    }
    let [, bq, bu, fq, fu] = m;
    if (!bu && fu) { bu = fu; fu = ''; } // "10+1c": both in crates
    if (!$('cfg-free').checked) {
        $('cfg-free').checked = true;
        applyConfig();
        showToast(`${bq} billed + ${fq} free — the Free column is on now · F12 turns it off`);
    }
    el.value = bq + bu;
    el.closest('.item-wrapper').querySelector('.item-free').value = fq + fu;
    return true;
}

// What the Free box holds: { n, unit, base } — its unit is the qty's unless typed ("6p")
function freeOf(w) {
    const f = w.querySelector('.item-free'), item = rowItem(f);
    const m = f.value.replace(/,/g, '').match(/^\s*(\d*\.?\d*)\s*([a-zA-Z]*)\s*$/);
    const none = { n: 0, unit: null, base: 0, bad: !!f.value.trim() && !m };
    if (!item || isService(item) || !m || !(parseFloat(m[1]) > 0)) return none;
    const hit = m[2] ? matchUnit(item, m[2]) : null;
    if (m[2] && !hit) return { ...none, bad: true };
    const unit = hit ? hit.u : w.querySelector('.item-qty').dataset.unit || item.units[0].u, n = parseFloat(m[1]);
    return { n, unit, base: n * unitFactor(item, unit), bad: false };
}
// Only counted while the Free column is on
const freeCounted = w => freeOn() ? freeOf(w) : { n: 0, unit: null, base: 0, bad: false };

function commitFree(el, silent = false) {
    const w = el.closest('.item-wrapper'), item = rowItem(el);
    if (!el.value.trim() || !freeOn()) { updateTotals(); return true; }
    const fr = freeOf(w);
    if (fr.bad || !(fr.n > 0)) {
        const units = itemUnits(item);
        if (silent) el.classList.add('input-error');
        else flagError(el, item && units.length ? `Free qty, like 1${units.length > 1 ? ` or 6${units[units.length - 1].u[0]}` : ''} — ${item.name} comes in ${units.map(x => unitLabel(x.u)).join(' / ')}` : 'Free qty — a number, like 1');
        return false;
    }
    // Shown like the qty; the unit only when it is not the qty's
    const own = fr.unit !== w.querySelector('.item-qty').dataset.unit;
    el.value = FMT.qty(fr.n) + (own ? ` ${unitLabel(fr.unit)}` : '');
    el.classList.remove('input-error');
    updateTotals();
    // More free than billed is almost always the two numbers swapped
    const billed = qtyInBase(w.querySelector('.item-qty'));
    if (!silent && billed > 0 && fr.base > billed) showToast(`More free (${FMT.qty(fr.n)}) than billed — check the two are not swapped`, true);
    return true;
}

// --- RATE PER: the bill may give the rate per another unit than its qty (50 Katta at ₹29 a Kg). The per box after the
// rate says which; its list shows what each unit makes, so the one that matches the paper's amount is plain to see ---
const lineDiscFactor = w => isDiscOn() ? 1 - Math.min(Math.max(num(w.querySelector('.item-disc')), 0), 100) / 100 : 1;
function perOptions(w) {
    const item = rowItem(w.querySelector('.item-name'));
    if (!item || isService(item)) return [];
    const qty = qtyInBase(w.querySelector('.item-qty')), r = splitUnit(w.querySelector('.item-rate')).n;
    return itemUnits(item).map(x => {
        const other = otherUnit(item, x.u), amount = qty > 0 && r > 0 ? round2(qty / x.f * r * lineDiscFactor(w)) : 0;
        return {
            id: x.u, name: unitLabel(x.u), amount,
            meta: amount ? `${FMT.qty(qty / x.f)} ${unitLabel(x.u)} × ${FMT.money(r)} = ${FMT.money(amount)}`
                : other ? `1 ${unitLabel(x.u)} = ${FMT.qty(x.f / other.f)} ${unitLabel(other.u)}` : ''
        };
    });
}

// A unit picked in the per box: a rate the operator typed keeps its number and the amount follows; a rate from the
// ledger, or one worked out from a typed amount, keeps the price — its number is converted. Picked = settled
function applyPer(el, u) {
    const w = el.closest('.item-wrapper'), item = rowItem(el), rate = w.querySelector('.item-rate'), amt = w.querySelector('.item-amt');
    if (!item || isService(item)) return;
    rate.dataset.unitLocked = '1';
    delete el.dataset.guess;
    if (u === rate.dataset.unit) return;
    const keepPrice = rate.dataset.auto === '1' || amt.dataset.typed === '1';
    if (keepPrice && rate.value !== '') rate.value = FMT.money(ratePerBase(rate) * unitFactor(item, u));
    rate.dataset.unit = u;
    if (amt.dataset.typed !== '1') w.recalc();
    updateTotals();
}

// A rate far from what the item cost last time, but right on it in another unit, was given per that unit: Atta was
// ₹1,450 a Katta = ₹29 a Kg, so "29" with the qty in Katta is the rate per Kg. Taken so, said once, and the per box
// shows it marked. A rate typed with its unit (29/kg), a per picked by hand or a typed amount is never second-guessed
function guessPer(w) {
    const item = rowItem(w.querySelector('.item-name')), rate = w.querySelector('.item-rate'), per = w.querySelector('.item-per');
    if (!item || isService(item) || itemUnits(item).length < 2 || !item.lastRate || rate.dataset.unitLocked
        || rate.dataset.auto === '1' || w.querySelector('.item-amt').dataset.typed === '1') return null;
    const n = splitUnit(rate).n, off = u => Math.abs(n / unitFactor(item, u) / item.lastRate - 1);
    if (!(n > 0) || off(rate.dataset.unit) <= 0.5) return null;
    const hit = itemUnits(item).filter(x => x.u !== rate.dataset.unit && off(x.u) <= 0.25).sort((a, b) => off(a.u) - off(b.u))[0];
    if (!hit) return null;
    rate.dataset.unit = hit.u;
    per.dataset.guess = '1';
    w.recalc();
    updateTotals();
    showToast(`₹${FMT.money(n)} taken as the rate per ${unitLabel(hit.u)} — last time it was ₹${FMT.money(item.lastRate * hit.f)} a ${unitLabel(hit.u)} · the per box changes it`);
    return hit.u;
}

function searchPick(el, query) {
    const list = PICK[el.dataset.pick](el);
    const q = query.trim().toLowerCase();
    if (!q) return list;
    const rank = o => o.name.toLowerCase().startsWith(q) || (o.keys || []).some(k => k.startsWith(q)) ? 0
        : o.name.toLowerCase().includes(q) ? 1 : -1;
    return list.map((o, i) => ({ o, r: rank(o), i })).filter(x => x.r >= 0).sort((a, b) => a.r - b.r || a.i - b.i).map(x => x.o);
}

const optionsFor = (el, query) => BOXES[el.id] ? BOXES[el.id].options(query) : el.dataset.ledger ? searchLedger(el.dataset.ledger, query) : searchPick(el, query);

function boundEntry(el) {
    if (!el.dataset.id) return null;
    if (el.dataset.ledger) return findLedger(el.dataset.ledger, el.dataset.id);
    return PICK[el.dataset.pick](el).find(o => o.id === el.dataset.id) || null;
}

function setPick(el, id) {
    const opt = PICK[el.dataset.pick](el).find(o => o.id === String(id));
    if (!opt) return;
    el.value = opt.name;
    el.dataset.id = opt.id;
    el.classList.remove('input-error');
}

function bindField(el, entry) {
    if (el.dataset.ledger) return bindLedger(el, entry);
    setPick(el, entry.id);
    if (el.dataset.pick === 'via') applyVia(el.closest('.pay-row'));
    if (el.dataset.pick === 'per') applyPer(el, entry.id);
    updateTotals();
}

function ledgerMeta(e) {
    if (e.kind === 'party') {
        if (e.type === 'unregistered') return `Unregistered · ${e.state}`;
        return `${e.type === 'composition' ? 'Composition · ' : ''}${e.gstin} · ${e.state}`;
    }
    if (e.kind === 'item') {
        if (e.type === 'service') return `Expense · GST ${e.gst}%${e.lastAmount ? ` · last ₹${e.lastAmount}` : ''}`;
        const kind = e.type === 'asset' ? 'Asset · ' : e.type === 'consumable' ? 'Consumable · ' : '';
        return `${kind}${e.units.map(x => unitLabel(x.u)).join(' / ')} · GST ${e.gst}%${e.cess ? ` + Cess ${e.cess}%` : ''} · last ₹${e.lastRate}/${unitLabel(e.units[0].u)}`;
    }
    if (e.kind === 'charge') {
        const usual = e.rate ? ` · usually ${e.rate}%` : '';
        return e.ctype === 'discount' ? `Discount · ${e.stage === 'before' ? 'before tax' : 'after tax'}${usual}`
            : `Charge${e.gst === 'none' ? ' · no GST' : e.gst === 'items' ? '' : ` · GST ${e.gst}%`}${usual}`;
    }
    if (e.kind === 'cash') return 'Cash';
    if (e.kind === 'bank') return 'Bank account';
    return e.meta || '';
}

// --- SUGGESTION LIST (ledgers and pick-lists) ---
const Suggest = {
    input: null, items: [], idx: 0,
    get el() { return $('suggest'); },

    // Pinned on top of every list: Create (with what was typed), then End of List on the row lists
    show(input, query = input.value) {
        const kind = input.dataset.ledger || input.dataset.pick;
        // In a whole line ("atta 10k 1450", "bhada 200") only the name part is searched
        if (CROSS[input.dataset.ledger]) { const pl = splitPowerLine(query, input.dataset.ledger); if (pl) query = pl.name; }
        const q = query.trim();
        const heads = [];
        if (CREATABLE.includes(kind)) heads.push({ special: 'create', name: q ? `Create "${q}"` : `Create New ${KIND_LABEL[kind]}`, meta: q ? `new ${KIND_LABEL[kind].toLowerCase()} · Alt+C` : 'Alt+C' });
        if (END_OF_LIST_KINDS.includes(input.dataset.ledger)) heads.push({ special: 'end', name: 'End of List', meta: 'blank Enter' });
        const matches = optionsFor(input, query);
        const at = s => heads.findIndex(h => h.special === s);
        this.input = input;
        this.items = [...heads, ...matches];
        this.browsing = !q;
        // Highlight: typed text → best match ("end" → End of List; nothing found → Create);
        // empty → End of List where it exists, else the current choice, else nothing
        if (q) this.idx = at('end') >= 0 && q.length >= 3 && 'end of list'.startsWith(q.toLowerCase()) ? at('end') : matches.length ? heads.length : at('create');
        else this.idx = at('end') >= 0 ? at('end') : this.items.findIndex(o => o.id && o.id === input.dataset.id);
        this.render();
    },

    // Qty box of a two-unit item: the item's own units, read as "5.000 Bag  = 250.000 Kg".
    // Nothing is highlighted until a letter is typed — the unit comes from the bill, not from us
    showUnits(input, pick = false) {
        const item = rowItem(input), units = itemUnits(item);
        if (units.length < 2) return this.close();
        const m = input.value.replace(/,/g, '').match(/^\s*(\d*\.?\d*)\s*([a-zA-Z]*)/);
        const n = m ? parseFloat(m[1]) || 0 : 0, typed = m ? m[2] : '';
        if (!n && !pick) return this.close();
        const hit = typed ? matchUnit(item, typed) : null;
        this.input = input;
        this.items = units.map(x => {
            const other = otherUnit(item, x.u);
            return { id: x.u, unitOpt: true, name: `${FMT.qty(n || 1)} ${unitLabel(x.u)}`, meta: `= ${FMT.qty((n || 1) * x.f / other.f)} ${unitLabel(other.u)}` };
        });
        this.idx = hit ? this.items.findIndex(o => o.id === hit.u) : pick ? 0 : -1;
        this.render();
    },

    // Heads stay pinned while the list scrolls; groups get a small title (the other section's lines say where they go)
    render() {
        const el = this.el, own = this.input.dataset.ledger;
        const heads = document.createElement('div'), list = document.createElement('div');
        heads.className = 'sg-heads';
        list.className = 'sg-list';
        let group = '';
        this.items.forEach((e, i) => {
            const opt = document.createElement('div');
            const cross = !e.special && e.kind === CROSS[own];
            opt.className = 'sg-opt' + (e.special ? ` sg-special sg-${e.special}` : '') + (cross ? ' sg-cross' : '') + (i === this.idx ? ' active' : '');
            opt.setAttribute('role', 'option');
            opt.id = `sg-opt-${i}`;
            opt.setAttribute('aria-selected', String(i === this.idx));
            const name = document.createElement('span');
            name.textContent = e.name;
            const meta = document.createElement('span');
            meta.className = 'sg-meta';
            meta.textContent = e.special ? (e.meta || '') : ledgerMeta(e);
            opt.append(name, meta);
            opt.addEventListener('mousedown', ev => ev.preventDefault()); // keep focus in the input
            opt.addEventListener('click', () => { this.idx = i; handleEnter(this.input); });
            if (e.special) return heads.appendChild(opt);
            const g = groupOf(e, own, this.browsing);
            if (g && g !== group) list.appendChild(Object.assign(document.createElement('div'), { className: 'sg-group' + (cross ? ' cross' : ''), textContent: g }));
            group = g;
            list.appendChild(opt);
        });
        if (!this.items.some(e => !e.special)) {
            const empty = document.createElement('div');
            empty.className = 'sg-empty';
            empty.textContent = own ? 'Not in the ledger — check the spelling, or Create it' : BOXES[this.input.id] ? 'Nothing matches'
                : this.input.classList.contains('charge-on') ? 'Several lines: Enter takes them (1,3 or 1-3)' : 'No match — Space shows the full list';
            list.appendChild(empty);
        }
        el.replaceChildren(...(heads.children.length ? [heads] : []), list);
        el.hidden = false;
        this.reposition();
        const act = el.querySelector('.sg-list .sg-opt.active');
        if (act) act.scrollIntoView({ block: 'nearest' });
        else list.scrollTop = 0;
        // Screen readers: the box is a combobox and announces the highlighted row (WAI-ARIA combobox pattern)
        const box = this.input, on = el.querySelector('.sg-opt.active');
        Object.entries({ role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'suggest', 'aria-autocomplete': 'list' }).forEach(([k, v]) => box.setAttribute(k, v));
        if (on) box.setAttribute('aria-activedescendant', on.id); else box.removeAttribute('aria-activedescendant');
    },

    // Right under the field (it may cover the totals for a moment, never the lines above);
    // above it only when the screen itself ends
    reposition() {
        if (!this.input) return;
        const r = this.input.getBoundingClientRect(), el = this.el;
        // Never wider than the screen (a phone is 360-430 px)
        const wide = this.input.dataset.ledger || BOXES[this.input.id] ? 380 : this.input.dataset.pick === 'per' ? 320 : 220;
        el.style.minWidth = Math.min(Math.max(r.width, wide), innerWidth - 16) + 'px';
        el.style.maxWidth = (innerWidth - 16) + 'px';
        el.style.left = Math.max(8, Math.min(r.left, innerWidth - el.offsetWidth - 8)) + 'px';
        const floor = innerHeight - 28; // key bar
        if (floor - r.bottom < 180 && r.top > 300) {
            el.style.top = '';
            el.style.bottom = (innerHeight - r.top + 4) + 'px';
        } else {
            el.style.bottom = '';
            el.style.top = (r.bottom + 4) + 'px';
        }
    },

    move(step) {
        if (!this.items.length) return;
        this.idx = this.idx < 0 ? (step > 0 ? 0 : this.items.length - 1) : (this.idx + step + this.items.length) % this.items.length;
        this.render();
    },

    close() {
        if (this.input) { this.input.setAttribute('aria-expanded', 'false'); this.input.removeAttribute('aria-activedescendant'); }
        this.el.hidden = true; this.input = null; this.items = [];
    },
    isOpenFor(el) { return this.input === el && !this.el.hidden; },
    current() { return this.items[this.idx] || null; }
};

// Group titles: a line of the other section says where it will go; the full lists (Space) are grouped by kind
function groupOf(e, own, browsing) {
    if (e.group) return e.group;
    if (own === 'item' && e.kind === 'charge') return '↓ goes under Charges & discount';
    if (own === 'charge' && e.kind === 'item') return '↑ goes up into the item lines';
    if (!browsing) return '';
    if (e.kind === 'item') return ITEM_GROUPS[e.type || 'stock'];
    if (e.kind === 'charge') return e.ctype === 'discount' ? 'Discounts' : 'Charges';
    if (own === 'payledger') return e.kind === 'cash' ? 'Cash' : 'Bank';
    return '';
}

// Resolve what the user typed in a ledger / pick-list field: the row highlighted in an open list always wins
// (picked with ↓↑ after Space, the box itself may still be empty or show the old choice)
function resolveField(el) {
    const listed = Suggest.isOpenFor(el) ? Suggest.current() : null;
    if (listed && !listed.special) return listed;
    const bound = boundEntry(el);
    if (bound && shownName(bound) === el.value.trim()) return bound;
    return el.value.trim() ? optionsFor(el, el.value)[0] || null : null;
}

// Create New: hands the typed name to the master form (linked separately) — nothing is created here
function createNew(el) {
    const kind = el.dataset.ledger || el.dataset.pick;
    const name = el.value.trim();
    Suggest.close();
    document.dispatchEvent(new CustomEvent('erp:create-ledger', { detail: { kind, name, field: el } }));
    showToast(`Create New ${KIND_LABEL[kind]}${name ? ` "${name}"` : ''} — the master form opens here`);
    focusField(el);
}

// End of List: this row goes (with undo) and the cursor moves on
function endOfList(el) {
    const row = el.closest('.item-wrapper, .charge-row, .pay-row');
    const isItems = row.classList.contains('item-wrapper'), isPay = row.classList.contains('pay-row');
    const hasData = isItems ? itemRowHasData(row) || !!el.dataset.id
        : isPay ? !!row.dataset.mode || row.querySelector('.pay-amt').value !== ''
        : !!el.dataset.id || row.querySelector('.charge-amt').value !== '' || row.querySelector('.charge-rate').value !== '';
    // A filled row taken out of the middle: the cursor goes to the row that took its place (the next one), so
    // the operator keeps reviewing down the list. Only the last / blank row closes the list and moves on
    let next = row.nextElementSibling;
    while (next && !next.matches('.item-wrapper, .charge-row, .pay-row')) next = next.nextElementSibling;
    if (hasData) removeRow(row, 'Row removed by End of List · Ctrl+Z to undo');
    else if (!isItems || document.querySelectorAll('.item-wrapper').length > 1) { row.remove(); updateTotals(); }

    if (hasData && next && next.isConnected && !isPay) {
        focusField(next.querySelector(isItems ? '.item-name' : '.charge-name') || next.querySelector('.nav-input'));
    } else if (isItems) {
        focusField(firstEmptyCharge());
    } else if (isPay) {
        afterPayments(); // nothing more paid: the rest is udhaar
    } else {
        openSettle();
    }
}

function bindLedger(el, entry) {
    el.value = shownName(entry);
    el.dataset.id = entry.id;
    el.classList.remove('input-error');
    if (entry.kind === 'party') onPartyChange();
    else if (entry.kind === 'item') fillItemRow(el.closest('.item-wrapper'), entry);
    else if (entry.kind === 'charge') fillChargeRow(el.closest('.charge-row'), entry);
    else fillPayRow(el.closest('.pay-row'), entry);
}

// The MRP tracker: the MRP on the latest saved bill that had the item, else the one in its master. Read once and
// kept until the next save (not on every keystroke)
let savedMrp = null;
function lastMrpOf(item) {
    if (!savedMrp) {
        savedMrp = {};
        savedInvoices().forEach(inv => (inv.items || []).forEach(l => { if (l.mrp) savedMrp[l.itemId] = l.mrp; }));
    }
    return savedMrp[item.id] || item.mrp || 0;
}

// Pull unit, GST and last rate from the item ledger. An expense (CA fee, marketing) has no qty or rate —
// just the amount, pre-filled with the last one
function fillItemRow(wrapper, item) {
    const qty = wrapper.querySelector('.item-qty'), rate = wrapper.querySelector('.item-rate'), amt = wrapper.querySelector('.item-amt');
    const service = isService(item);
    const tag = wrapper.querySelector('.type-tag');
    tag.textContent = ITEM_TYPE_TAG[item.type] || '';
    tag.dataset.type = item.type || 'stock';
    wrapper.classList.toggle('is-service', service);
    wrapper.classList.toggle('has-exp', !!item.expiry);
    wrapper.classList.toggle('has-batch', !!item.batch);
    wrapper.classList.toggle('has-mrp', !!item.mrp);
    // MRP: the last one paid (it rarely changes — Enter keeps it), or the one just typed on this bill for the same
    // item; a new MRP is still measured against the last one paid. Another item picked on the line brings its own
    const mrp = wrapper.querySelector('.item-mrp'), lastMrp = item.mrp ? lastMrpOf(item) : 0;
    wrapper.dataset.lastMrp = lastMrp || '';
    const onThisBill = item.mrp ? [...document.querySelectorAll('.item-wrapper')].filter(x => x !== wrapper && x.querySelector('.item-name').dataset.id === item.id)
        .map(x => num(x.querySelector('.item-mrp'))).filter(v => v > 0).pop() : 0;
    if (!item.mrp) { mrp.value = ''; delete mrp.dataset.auto; }
    else if (mrp.value === '' || mrp.dataset.auto === '1') { mrp.value = FMT.money(onThisBill || lastMrp); mrp.dataset.auto = '1'; }
    // Expiry: the estimate from the shelf life until a date is typed; batch only where the item has one
    const exp = wrapper.querySelector('.item-exp');
    if (!item.batch) wrapper.querySelector('.item-batch').value = '';
    if (!item.expiry) { exp.value = ''; exp.dataset.iso = ''; delete exp.dataset.auto; }
    else if (exp.value === '' || exp.dataset.auto === '1') {
        if (item.life) estimateExpiry(exp, item);
        else { exp.value = ''; exp.dataset.iso = ''; delete exp.dataset.auto; }
    }
    qty.disabled = rate.disabled = wrapper.querySelector('.item-free').disabled = service;
    if (service) {
        qty.value = rate.value = '';
        delete qty.dataset.unit;
        delete rate.dataset.unit;
        delete rate.dataset.auto;
        if (amt.value === '' && item.lastAmount) { amt.value = FMT.money(item.lastAmount); amt.dataset.auto = '1'; }
    } else {
        // The ledger rate is per the main unit; the qty unit is left for the operator to type (unless there is only one)
        rate.dataset.unit = item.units[0].u;
        if (item.units.length === 1) qty.dataset.unit = item.units[0].u; else delete qty.dataset.unit;
        delete rate.dataset.unitLocked;
        if (rate.value === '' && amt.value === '') {
            rate.value = FMT.money(item.lastRate);
            rate.dataset.auto = '1';
        }
    }
    setPick(wrapper.querySelector('.item-gst'), item.gst);
    wrapper.recalc();
    updateTotals();
}

// A ledger with a usual rate (commission 6%) comes pre-filled; others wait for the amount.
// Its GST treatment comes from the ledger (as the items, none, or its own rate) and can be changed on the line
function fillChargeRow(row, charge) {
    const rate = row.querySelector('.charge-rate');
    if (rate.value === '' && row.querySelector('.charge-amt').value === '' && charge.rate) {
        rate.value = FMT.pct(charge.rate);
        delete row.dataset.fixedAmt;
    }
    setPick(row.querySelector('.charge-gst'), String(charge.gst));
    updateTotals();
}

// --- SUPPLIER & TAX REGIME ---
function getParty() { return findLedger('party', $('inp-supplier').dataset.id); }

// none: unregistered/composition supplier (or no supplier yet); intra: CGST+SGST; inter: IGST
function taxRegime(party) {
    if (!party || party.type !== 'regular') return 'none';
    return party.state === COMPANY.state ? 'intra' : 'inter';
}

// Ledger balance = opening balance + udhaar of bills saved since. Positive = we owe the supplier
function partyBalance(party) {
    return round2(savedInvoices().filter(i => i.partyId === party.id && i.credit)
        .reduce((s, i) => s + i.credit.amount, party.balance || 0));
}

// A supplier's usual udhaar period, in days (the ledger keeps days only)
const creditTermOf = party => party.creditDays ? `${party.creditDays} day${party.creditDays === 1 ? '' : 's'}` : 'now';

// Under the supplier: what the operator needs — the balance now and after this bill — in plain words.
// The GST type is shown only when it is the unusual one (other state / unregistered / composition)
function renderPartyInfo(bill) {
    const party = bill.party, info = $('party-info'), sub = $('party-sub');
    info.innerHTML = '';
    sub.innerHTML = '';
    if (!party) return;
    if (bill.regime !== 'intra') {
        const c = document.createElement('span');
        c.className = 'chip ' + (bill.regime === 'inter' ? 'inter' : 'none');
        c.textContent = bill.regime === 'inter' ? 'Other state · IGST' : party.type === 'composition' ? 'Composition dealer · no GST' : 'Unregistered · no GST';
        info.appendChild(c);
    }
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
    const owe = (b, box, lower) => {
        const words = b > 0 ? 'You owe' : b < 0 ? 'Advance' : 'Nothing due';
        box.append(lower ? words.toLowerCase() : words);
        if (b) box.append(' ', el('b', '', formatINR(Math.abs(b))), ' ', el('small', '', b > 0 ? 'Cr' : 'Dr'));
        return box;
    };
    const now = partyBalance(party);
    sub.appendChild(owe(now, el('span', 'bal ' + (now > 0 ? 'bal-cr' : now < 0 ? 'bal-dr' : 'bal-nil'))));
    // The udhaar part of this bill lands on the balance (before payment is looked at, a cash supplier is paid on the spot)
    const unpaid = paymentSeen ? Math.max(bill.balance, 0) : party.defaultPay === 'cash' ? 0 : bill.grand;
    if (unpaid > 0) sub.appendChild(owe(round2(now + unpaid), el('span', 'bal-after', 'after this bill: '), true));
    const term = creditTermOf(party), last = savedInvoices().filter(i => i.partyId === party.id).pop();
    sub.appendChild(el('span', 'psub-meta', [term === 'now' ? 'pays on the spot' : `${term} credit`,
        last && `last bill ${fmtDate(fromIso(last.billDate))} · ${formatINR(last.totals.grandTotal)}`].filter(Boolean).join(' · ')));
}

function onPartyChange() {
    const party = getParty();
    // Udhaar terms default from the supplier ledger (until the operator types their own)
    const due = $('inp-due'), interest = $('inp-interest');
    if (due.value === '' || due.dataset.auto === '1') { due.value = party ? String(party.creditDays || 0) : ''; due.dataset.auto = '1'; syncDueDate(); }
    if (interest.value === '' || interest.dataset.auto === '1') { interest.value = party && party.interest ? FMT.pct(party.interest) : ''; interest.dataset.auto = '1'; }
    prefillBillNo();
    updateTotals();
}

// A supplier's bill numbers run in a series (JWM/25-26/118): the series part is filled in, the operator types the number
function lastBillNoOf(party) {
    const last = savedInvoices().filter(i => i.partyId === party.id && i.billNo).pop();
    return last ? last.billNo : party.lastBillNo || '';
}

function prefillBillNo() {
    const inv = $('inp-inv'), party = getParty();
    if (inv.value.trim() !== '' && inv.dataset.auto !== '1') return;
    const last = party ? lastBillNoOf(party) : '';
    const series = last.match(/^(.*?\D)\d+$/);
    inv.value = series ? series[1] : '';
    if (series) inv.dataset.auto = '1'; else delete inv.dataset.auto;
    inv.dataset.last = last;
}

function isDuplicateBill() {
    const party = getParty(), inv = $('inp-inv'), no = inv.value.trim().toLowerCase();
    if (!party || !no || inv.dataset.auto === '1') return null;
    return savedInvoices().find(i => i.partyId === party.id && (i.billNo || '').toLowerCase() === no) || null;
}

const dupText = dup => `Bill ${dup.billNo} of ${dup.supplier} is already entered as ${dup.purchaseNo} on ${fmtDate(fromIso(dup.purchaseDate || dup.receivedDate))}`;

// --- E-INVOICE QR: a 2D scanner (or paste) gives the signed code; it carries the supplier's GSTIN,
// bill no., date and total — not the lines. The signature is checked by the server, not here ---
function decodeEInvoiceQR(text) {
    const parts = String(text).trim().split('.');
    if (parts.length !== 3 || parts[1].length < 40) return null;
    try {
        const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
        const bytes = Uint8Array.from(atob(b64 + '='.repeat((4 - b64.length % 4) % 4)), c => c.charCodeAt(0));
        const payload = JSON.parse(new TextDecoder().decode(bytes));
        const data = typeof payload.data === 'string' ? JSON.parse(payload.data) : payload.data || payload;
        return data && data.SellerGstin && data.DocNo ? data : null;
    } catch (err) {
        return null;
    }
}

function applyEInvoice(data) {
    Suggest.close();
    const sup = $('inp-supplier'), inv = $('inp-inv');
    const party = LEDGER.party.find(p => p.gstin && p.gstin === data.SellerGstin);
    if (party) bindLedger(sup, party);
    else { sup.value = data.SellerGstin; delete sup.dataset.id; onPartyChange(); }
    einvoice = {
        irn: data.Irn || null, irnDate: data.IrnDt || null, sellerGstin: data.SellerGstin, buyerGstin: data.BuyerGstin || null,
        docNo: data.DocNo, docDate: data.DocDt || null, total: +data.TotInvVal || null, itemCount: +data.ItemCnt || null
    };
    inv.value = data.DocNo;
    delete inv.dataset.auto;
    const d = data.DocDt ? parseDateInput(String(data.DocDt), { allowFuture: true }) : null; // "24/09/2026"
    if (d) setDate($('inp-date'), d);
    renderEInvoiceTag();
    updateTotals();
    if (!party) return flagError(sup, `e-invoice read — but supplier GSTIN ${data.SellerGstin} is not in the ledger · Alt+C to create`);
    const dup = isDuplicateBill();
    if (dup) return flagError(inv, dupText(dup));
    openItems();
    focusField(firstEmptyItem());
    showToast(`e-invoice read: ${party.name} · Bill ${data.DocNo}${einvoice.itemCount ? ` · ${einvoice.itemCount} lines` : ''} — now the lines`);
}

function renderEInvoiceTag() {
    const tag = $('einv-tag');
    tag.hidden = !einvoice;
    tag.textContent = '';
    if (!einvoice) return;
    tag.title = einvoice.irn ? `IRN ${einvoice.irn}` : '';
    tag.append(`✓ e-invoice${einvoice.itemCount ? ` · ${einvoice.itemCount} lines on the bill` : ''}`);
    if (einvoice.buyerGstin && einvoice.buyerGstin !== COMPANY.gstin) {
        const bad = document.createElement('span');
        bad.className = 'bad';
        bad.textContent = `· made out to ${einvoice.buyerGstin}, not our GSTIN`;
        tag.append(bad);
    }
}

// --- SETTINGS & SCREEN ---
// Keyboard first like the bill: F12 opens on the first row, ↑↓ move, Space / Enter switch, ←→ theme, Esc closes
const settingControls = () => [...$('settings-panel').querySelectorAll('.set-ctl')];

function toggleSettings(force) {
    const panel = $('settings-panel');
    const open = force !== undefined ? force : !panel.classList.contains('open');
    if (open === panel.classList.contains('open')) return;
    panel.classList.toggle('open', open);
    panel.inert = !open;
    if (open) {
        focusBeforePanel = document.activeElement;
        Suggest.close();
        settingControls()[0].focus();
    } else {
        const back = focusBeforePanel;
        focusBeforePanel = null;
        focusField(back && back.isConnected && !back.closest('[inert]') ? back : firstField());
    }
}

function settingsKey(e) {
    const ctls = settingControls(), n = ctls.length, k = e.key;
    const at = ctls.indexOf(document.activeElement.closest && document.activeElement.closest('.set-ctl'));
    if (k === 'Escape') return toggleSettings(false);
    const step = k === 'ArrowDown' || (k === 'Tab' && !e.shiftKey) ? 1 : k === 'ArrowUp' || k === 'Tab' ? -1 : 0;
    if (step) return ctls[at < 0 ? (step > 0 ? 0 : n - 1) : (at + step + n) % n].focus();
    if (k === 'Home' || k === 'End') return ctls[k === 'Home' ? 0 : n - 1].focus();
    const ctl = ctls[at];
    if (!ctl) return;
    if (ctl.id === 'cfg-theme') {
        if (['ArrowLeft', 'ArrowRight', ' ', 'Enter'].includes(k)) setTheme(THEMES[(THEMES.indexOf(ctl.dataset.value) + (k === 'ArrowLeft' ? -1 : 1) + THEMES.length) % THEMES.length]);
        return;
    }
    if (k === ' ' || k === 'Enter') { ctl.checked = !ctl.checked; applyConfig(); }
}

function toggleFullScreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => showToast('Full screen is blocked here — press F11 in the browser', true));
}

function loadConfig() {
    try {
        const saved = JSON.parse(localStorage.getItem(CONFIG_KEY) || 'null');
        if (saved) CONFIG_IDS.forEach(id => { if (id in saved) $(id).checked = saved[id]; });
    } catch (err) { /* storage unavailable: use defaults */ }
}

function applyConfig() {
    const body = document.body, on = id => $(id).checked;
    body.classList.toggle('skip-vch', on('cfg-skipvch'));
    body.classList.toggle('show-godown', on('cfg-godown'));
    body.classList.toggle('show-disc', on('cfg-disc'));
    body.classList.toggle('show-tax', on('cfg-tax'));
    body.classList.toggle('show-remark', on('cfg-remark'));
    body.classList.toggle('show-free', on('cfg-free'));
    body.classList.toggle('show-mrp', on('cfg-mrp'));
    body.classList.toggle('show-exp', on('cfg-exp'));
    $('terms-extra').hidden = !on('cfg-interest');
    applyGridCols();
    // Discount on/off changes amounts, so recalculate every row
    document.querySelectorAll('.item-wrapper').forEach(w => w.recalc && w.recalc());
    updateTotals();
    try {
        localStorage.setItem(CONFIG_KEY, JSON.stringify(Object.fromEntries(CONFIG_IDS.map(id => [id, $(id).checked]))));
    } catch (err) { /* storage unavailable: settings last for this session only */ }
}

// Name takes the room; the number columns keep fixed widths on the right, like a printed bill
// With three or more extra columns on, the number columns give a little so the name keeps its room on a 1366 screen
function applyGridCols() {
    const on = cls => document.body.classList.contains(cls);
    const tight = ['show-godown', 'show-free', 'show-disc', 'show-tax'].filter(on).length >= 3;
    const cols = [
        `minmax(var(--cols-name-min, ${tight ? 150 : 200}px), 1fr)`, on('show-godown') && (tight ? '120px' : '130px'), tight ? '150px' : '170px',
        on('show-free') && (tight ? '96px' : '104px'), tight ? '136px' : '150px', '78px',
        on('show-disc') && '72px', on('show-tax') && '64px', tight ? '140px' : '150px', on('show-tax') && '110px'
    ].filter(Boolean);
    document.body.style.setProperty('--cols', cols.join(' '));
}

// Alt+T: the cursor goes onto the bill box — GST heads, then round off — to type what the paper prints, when it differs
function focusTaxCheck() {
    const first = [...document.querySelectorAll('.bill-tax')].find(i => !i.closest('[hidden]'));
    if (!$('billbox').contains(document.activeElement)) focusBeforeBox = document.activeElement;
    focusField(first);
}

function leaveTaxCheck() {
    const back = focusBeforeBox;
    focusBeforeBox = null;
    focusField(back && back.isConnected && !back.closest('[inert]') ? back : $('inp-remarks'));
}

// --- HTML TEMPLATE BUILDERS ---
function addItemRow() {
    const row = document.createElement('div');
    row.className = 'item-wrapper'; // Wraps main row and remarks

    row.innerHTML = `
        <div class="data-row item-row">
            <div class="name-cell"><input type="text" placeholder="item or expense…" aria-label="Item or expense" class="nav-input item-name" data-ledger="item" autocomplete="off"><span class="type-tag"></span><button type="button" class="row-del" tabindex="-1" aria-label="Delete line">✕</button></div>
            <div class="col-godown"><input type="text" class="nav-input pick item-godown" data-pick="godown" aria-label="Godown" autocomplete="off"></div>
            <div class="unit-cell"><input type="text" placeholder="qty" aria-label="Quantity with unit" class="nav-input num item-qty" data-fmt="qty" data-units="1" autocomplete="off"><span class="alt-qty"></span></div>
            <div class="col-free free-cell"><input type="text" placeholder="free" aria-label="Free quantity (not charged)" class="nav-input num item-free" autocomplete="off"><span class="free-net"></span></div>
            <div class="rate-cell"><input type="text" placeholder="rate" aria-label="Rate" class="nav-input num item-rate" data-fmt="money" data-units="1" autocomplete="off"><span class="rate-delta" hidden></span></div>
            <div class="per-cell"><input type="text" class="nav-input pick item-per" data-pick="per" aria-label="Rate per (unit)" autocomplete="off" spellcheck="false"></div>
            <div class="col-disc"><input type="text" inputmode="decimal" aria-label="Discount percent" class="nav-input num item-disc" data-fmt="pct" autocomplete="off"></div>
            <div class="col-tax"><input type="text" class="nav-input pick item-gst num" data-pick="gst" aria-label="GST rate" autocomplete="off"></div>
            <div><input type="text" inputmode="decimal" placeholder="amount" aria-label="Amount" class="nav-input num item-amt" data-fmt="money" autocomplete="off"></div>
            <div class="col-tax tax-cell"><span class="item-tax"></span></div>
        </div>
        <!-- Only for items whose master asks, and only while their switch is on (Settings): batch & expiry (dairy,
             sauces, packed drinks) and MRP (sold as bought) -->
        <div class="row-extra">
            <label class="rx rx-batch">Batch <input type="text" class="nav-input item-batch" placeholder="as printed" aria-label="Batch number" maxlength="20" autocomplete="off" autocapitalize="characters" spellcheck="false"></label>
            <label class="rx rx-exp">Expiry <input type="text" class="nav-input item-exp" placeholder="15.12 or 6m" aria-label="Expiry date" autocomplete="off"></label>
            <span class="exp-hint"></span>
            <label class="rx rx-mrp">MRP ₹ / pc <input type="text" inputmode="decimal" class="nav-input num item-mrp" data-fmt="money" aria-label="MRP per piece" autocomplete="off"></label>
            <span class="mrp-hint"></span>
        </div>
        <div class="row-remark">
            <input type="text" placeholder="↳ remark…" aria-label="Line remark" class="nav-input remark-input">
        </div>
    `;
    setPick(row.querySelector('.item-godown'), LEDGER.godown[0].id);

    const qtyInput = row.querySelector('.item-qty');
    const rateInput = row.querySelector('.item-rate');
    const amtInput = row.querySelector('.item-amt');
    const discInput = row.querySelector('.item-disc');

    // Discount only counts while the column is visible, clamped to 0-100
    const discFactor = () => isDiscOn() ? 1 - Math.min(Math.max(num(discInput), 0), 100) / 100 : 1;

    // Forward (qty × rate → amount), unit-aware: 10 Katta × ₹29/Kg works
    const calcForward = (force = false) => {
        const amt = qtyInBase(qtyInput) * ratePerBase(rateInput) * discFactor();
        if (force || document.activeElement !== amtInput) { amtInput.value = amt > 0 ? FMT.money(amt) : ''; delete amtInput.dataset.typed; }
    };

    // Reverse (amount ÷ qty → rate) for bills that print only the amount; the rate stays in its own unit
    const calcReverse = () => {
        const qty = qtyInBase(qtyInput);
        const factor = discFactor();
        if (qty > 0 && factor > 0) {
            const rate = num(amtInput) / (qty * factor) * unitFactor(rowItem(rateInput), rateInput.dataset.unit);
            rateInput.value = rate > 0 ? FMT.money(rate) : '';
            delete rateInput.dataset.auto;
        }
    };

    // Qty/Disc edits: use Rate if present, otherwise keep a typed Amount and derive Rate from it
    const onQtyOrDisc = () => {
        if (rateInput.value === '' && amtInput.value !== '') calcReverse();
        else calcForward();
        updateTotals();
    };

    qtyInput.addEventListener('input', onQtyOrDisc);
    discInput.addEventListener('input', onQtyOrDisc);
    // A rate typed again starts over: a unit the system guessed for the old number goes back to the qty's unit
    rateInput.addEventListener('input', () => {
        const per = row.querySelector('.item-per');
        if (per.dataset.guess) { delete per.dataset.guess; rateInput.dataset.unit = qtyInput.dataset.unit || rateInput.dataset.unit; }
        delete amtInput.dataset.typed;
        calcForward(); updateTotals();
    });
    // A typed amount is the paper's: a unit picked later converts the rate, never this amount
    amtInput.addEventListener('input', () => { amtInput.dataset.typed = '1'; calcReverse(); updateTotals(); });
    row.recalc = () => { if (rateInput.value !== '') calcForward(true); };
    row.querySelector('.item-mrp').addEventListener('input', updateTotals);
    row.querySelector('.item-free').addEventListener('input', updateTotals);
    row.querySelector('.row-del').addEventListener('click', () => deleteRow(row.querySelector('.item-name')));
    // A line keeps its own id, so a charge "on line 2" stays on that line when lines above it come or go
    row.dataset.line = 'L' + (++lineSeq);

    $('items-body').appendChild(row);
    return row.querySelector('.item-name');
}

const firstEmptyItem = () => [...document.querySelectorAll('.item-name')].find(x => !x.value.trim()) || addItemRow();
// Coming down to the charges: a blank line after the ones already there — typing never overwrites a charge
const firstEmptyCharge = () => { openCharges(); return [...document.querySelectorAll('.charge-name')].find(x => !x.value.trim()) || addChargeRow().querySelector('.charge-name'); };

// Charge / discount line in the item columns: Ledger · on (which lines) · Rate · per (%) · [GST] · Amount · [tax]
// A rate means % of the lines it is on; with no rate the amount is taken as typed
function addChargeRow(entry = null, prefill = null) {
    const row = document.createElement('div');
    row.className = 'data-row charge-row';
    row.innerHTML = `
        <div class="charge-cell"><input type="text" placeholder="charge / discount…" aria-label="Charge or discount ledger" class="nav-input charge-name" data-ledger="charge" autocomplete="off"><span class="charge-tag"></span><button type="button" class="row-del" tabindex="-1" aria-label="Delete line">✕</button></div>
        <div class="col-godown"></div>
        <div class="on-cell"><input type="text" class="nav-input pick charge-on" data-pick="on" aria-label="Which lines this is for" autocomplete="off"><span class="charge-base"></span></div>
        <div class="col-free"></div>
        <div><input type="text" inputmode="decimal" class="nav-input num charge-rate" data-fmt="pct" aria-label="Rate in percent" autocomplete="off"></div>
        <div class="per charge-per"></div>
        <div class="col-disc"></div>
        <div class="col-tax"><input type="text" class="nav-input pick charge-gst" data-pick="cgst" aria-label="GST on this charge" autocomplete="off"></div>
        <div class="amt-cell"><span class="sign"></span><input type="text" inputmode="decimal" aria-label="Charge amount" class="nav-input num charge-amt" data-fmt="money" autocomplete="off"></div>
        <div class="col-tax tax-cell"><span class="charge-tax"></span></div>
        <div class="charge-split"></div>
    `;
    const rate = row.querySelector('.charge-rate'), amt = row.querySelector('.charge-amt');
    row.querySelector('.row-del').addEventListener('click', () => deleteRow(row.querySelector('.charge-name')));
    setOn(row.querySelector('.charge-on'), '');
    $('charges-body').appendChild(row);
    // Set values via properties (never interpolate user text into HTML)
    if (entry) bindLedger(row.querySelector('.charge-name'), entry);
    if (prefill) setChargeValue(row, prefill);

    // Typing a rate makes the line a percent; typing the amount makes the amount win (rate shown for reference)
    rate.addEventListener('input', () => { delete row.dataset.fixedAmt; updateTotals(); });
    amt.addEventListener('input', () => {
        row.dataset.fixedAmt = '1';
        const x = lastBill && lastBill.extras.find(e => e.row === row);
        if (rate.value !== '' && x && x.base > 0) rate.value = FMT.pct(num(amt) / x.base * 100);
        updateTotals();
    });
    updateTotals();
    return row;
}

// One number typed with a charge: "%" (or a ledger with a usual rate) makes it a rate, otherwise it is the amount
function chargeValue(entry, text) {
    const n = parseNum(String(text).replace('%', ''));
    return /%$/.test(text) || (entry.rate && n <= 100) ? { rate: n } : { amount: n };
}

function setChargeValue(row, v) {
    const rate = row.querySelector('.charge-rate'), amt = row.querySelector('.charge-amt');
    if (v.rate !== undefined) { rate.value = FMT.pct(v.rate); delete row.dataset.fixedAmt; }
    else { rate.value = ''; amt.value = FMT.money(v.amount); row.dataset.fixedAmt = '1'; }
    updateTotals();
}

const nextChargeName = row => (row.nextElementSibling || addChargeRow()).querySelector('.charge-name');

// Payment row: a cash or bank ledger, picked like any ledger · Amount · (bank only) Via · Ref · Cheque date
function addPayRow(entry = null) {
    const row = document.createElement('div');
    row.className = 'pay-row';
    row.innerHTML = `
        <div class="pf pf-ledger"><span class="pf-label">Paid from</span><input type="text" class="nav-input pay-ledger" data-ledger="payledger" placeholder="cash / bank…" aria-label="Paid from" autocomplete="off"></div>
        <div class="pf"><span class="pf-label">Amount ₹</span><input type="text" inputmode="decimal" class="nav-input num pay-amt" data-fmt="money" aria-label="Payment amount" autocomplete="off"></div>
        <div class="pay-details">
            <div class="pf pay-bank"><span class="pf-label">Via</span><input type="text" class="nav-input pick pay-via" data-pick="via" aria-label="Paid via" autocomplete="off"></div>
            <div class="pf pf-wide pay-bank"><span class="pf-label pay-ref-label">Ref / UTR No.</span><input type="text" class="nav-input pay-ref" aria-label="Reference number" autocomplete="off"></div>
            <div class="pf pay-chq"><span class="pf-label">Cheque Date</span><input type="text" class="nav-input date-input pay-date" data-future="1" aria-label="Cheque date" autocomplete="off"></div>
        </div>
    `;
    // A typed amount is the operator's: it stops following the bill before anything is worked out again
    const amtInput = row.querySelector('.pay-amt');
    amtInput.addEventListener('input', () => { delete amtInput.dataset.auto; updateTotals(); });
    setPick(row.querySelector('.pay-via'), 'upi');
    $('pay-body').appendChild(row);
    if (entry) bindLedger(row.querySelector('.pay-ledger'), entry);
    return row;
}

// Cash needs nothing more; a bank ledger asks how (UPI / NEFT / RTGS / IMPS / Cheque) and the reference;
// an earlier payment needs nothing either — it was recorded when it was made
function fillPayRow(row, ledger) {
    row.dataset.mode = ledger.kind;
    applyVia(row);
    // Amount: what is still unsettled — for an earlier payment, at most what is left of it — and it follows
    // the bill until typed over (refreshAutoPayments)
    const amt = row.querySelector('.pay-amt');
    if (amt.value === '') amt.dataset.auto = '1';
    updateTotals();
}

// A payment made before the bill becomes a "Paid from" line of this bill: an adjustment against money already
// given, never a second payment (the cash book has it once, from the day it was paid)
function linkPayment(w) {
    const rows = [...document.querySelectorAll('.pay-row')];
    if (rows.some(r => r.querySelector('.pay-ledger').dataset.id === w.id)) return;
    const row = rows.find(r => !r.dataset.mode && r.querySelector('.pay-amt').value === '') || addPayRow();
    bindLedger(row.querySelector('.pay-ledger'), linkEntry(w));
}

// Earlier payments linked in this bill, by payment id
const linkedInBill = id => round2([...document.querySelectorAll('.pay-row[data-mode="linked"]')]
    .filter(r => r.querySelector('.pay-ledger').dataset.id === id).reduce((s, r) => s + num(r.querySelector('.pay-amt')), 0));

// --- PAID, AWAITING BILL: money given when the goods came, before their bill (Alt+L, or the pill top right). Where:
// the pill sits at the right of the app bar — out of the bill (it is no field of the paper) and in the corner the eye
// visits last, so it never pulls attention off the line being typed. Its drawer slides in from that same side (the
// hand and the eye go where the control is). Quiet while it doesn't concern this bill; when this bill's supplier has
// such a payment the pill says so with one small nudge — a single onset is seen from the corner of the eye, a
// blinking one is soon ignored. Each card: amount first (what is recognised), then who, when (day, date, time, how
// long ago) and the note. This bill's supplier first, then the oldest — the longer one waits, the more it matters ---
const openPayments = () => paymentsToLink().filter(w => leftToLink(w) - linkedInBill(w.id) > 0.004);
const awaitOpen = () => !$('await').hidden && !$('await').classList.contains('leaving');
let awaitIdx = 0, awaitRows = [], awaitBack = null;

function daysAgoText(w) { return w.daysAgo === 0 ? 'today' : w.daysAgo === 1 ? 'yesterday' : `${w.daysAgo} days ago`; }

// The drawer's cards for what is typed: linked to this bill (to undo), this supplier's, everyone else's
function awaitCards(query) {
    const q = query.trim().toLowerCase(), qs = sound(q), party = getParty();
    const hit = w => { const p = findLedger('party', w.partyId), text = `${p.name} ${w.note} ${Math.round(w.amount)} ${w.mode}`.toLowerCase();
        return !q || text.includes(q) || text.split(/[\s/&()·]+/).some(x => x && (x.startsWith(q) || (qs.length >= 3 && sound(x).startsWith(qs)))); };
    const linked = UNLINKED_PAYMENTS.filter(w => linkedInBill(w.id) > 0.004 && hit(w));
    const open = openPayments().filter(hit);
    const mine = open.filter(w => party && w.partyId === party.id);
    return [
        ...linked.map(w => ({ w, state: 'linked', group: 'Linked to this bill' })),
        ...mine.map(w => ({ w, state: 'mine', group: `${party.name} — this bill's supplier` })),
        ...open.filter(w => !mine.includes(w)).map(w => ({ w, state: party ? 'other' : 'open', group: party ? 'Other suppliers' : 'Oldest first' }))
    ];
}

// The pill: how many wait; this supplier's said in words, once with a nudge
function renderAwait() {
    const pill = $('await-pill');
    if (!pill) return;
    const open = openPayments(), party = getParty(), mine = party ? open.filter(w => w.partyId === party.id).length : 0;
    pill.hidden = !open.length && !awaitOpen();
    $('await-count').textContent = open.length;
    $('await-text').textContent = mine ? `awaiting bill · ${mine} of this supplier` : 'awaiting bill';
    pill.classList.toggle('mine', mine > 0);
    if (mine && pill.dataset.nudged !== party.id) { pill.dataset.nudged = party.id; ping(pill, 'nudge'); }
    if (!mine) delete pill.dataset.nudged;
    if (awaitOpen()) renderAwaitList();
}

function renderAwaitList(stagger = false) {
    const list = $('aw-list'), q = $('aw-q').value;
    awaitRows = awaitCards(q);
    if (awaitIdx >= awaitRows.length) awaitIdx = awaitRows.length - 1;
    const frag = document.createDocumentFragment();
    let group = '';
    awaitRows.forEach((c, i) => {
        if (c.group !== group) { group = c.group; frag.appendChild(Object.assign(document.createElement('div'), { className: 'aw-group', textContent: group })); }
        const w = c.w, p = findLedger('party', w.partyId), left = leftToLink(w), here = linkedInBill(w.id);
        const card = document.createElement('div');
        card.className = `aw-card ${c.state}` + (i === awaitIdx ? ' active' : '') + (w.daysAgo > 7 ? ' old' : '');
        card.id = `aw-${w.id}`;
        card.setAttribute('role', 'option');
        card.setAttribute('aria-selected', String(i === awaitIdx));
        if (stagger && !reduceMotion()) card.style.animationDelay = `${Math.min(i, 8) * 28}ms`;
        const amount = c.state === 'linked' ? here : left - here;
        const el = (tag, cls, text) => Object.assign(document.createElement(tag), { className: cls, textContent: text });
        const top = el('div', 'aw-top', '');
        top.append(el('b', 'aw-amt', formatINR(amount)), el('span', 'aw-mode', w.mode === 'cash' ? 'Cash' : w.mode.toUpperCase()));
        if (left < w.amount - 0.004) top.append(el('span', 'aw-part', `of ${formatINR(w.amount)}`));
        card.append(top, el('div', 'aw-who', p.name), el('div', 'aw-when', `${paymentWhen(w)} · `));
        card.lastChild.append(el('span', 'aw-age', daysAgoText(w)));
        if (w.note) card.append(el('div', 'aw-note', w.note));
        card.append(el('div', 'aw-act', c.state === 'linked' ? '✓ in this bill · Ctrl+Del unlinks' : c.state === 'other' ? `for ${p.name} — not this bill` : 'Enter ↵ link to this bill'));
        card.addEventListener('mousedown', ev => ev.preventDefault()); // the search box keeps the cursor
        card.addEventListener('click', () => { awaitIdx = i; actAwait(); });
        frag.appendChild(card);
    });
    if (!awaitRows.length) frag.appendChild(Object.assign(document.createElement('div'), { className: 'aw-empty', textContent: q.trim() ? 'Nothing matches — try the supplier, the amount or the note' : 'Nothing is waiting for its bill' }));
    list.replaceChildren(frag);
    const act = list.querySelector('.aw-card.active');
    if (act) { act.scrollIntoView({ block: 'nearest' }); $('aw-q').setAttribute('aria-activedescendant', act.id); }
    else $('aw-q').removeAttribute('aria-activedescendant');
}

// The first card worth acting on (this supplier's come first, then the oldest) — never one already linked
const firstLinkable = () => Math.max(awaitRows.findIndex(c => c.state !== 'linked'), 0);

function toggleAwait(force) {
    const d = $('await'), open = force !== undefined ? force : !awaitOpen();
    if (open === awaitOpen()) return;
    if (open) {
        if (!openPayments().length && !UNLINKED_PAYMENTS.some(w => linkedInBill(w.id) > 0.004)) return showToast('No payment is waiting for its bill');
        if (!$('help').hidden) toggleHelp(false);
        if (!$('confirm').hidden) closeConfirm(false);
        if (settleOpen) closeSettle(false);
        toggleSettings(false);
        Suggest.close();
        awaitBack = document.activeElement;
        d.classList.remove('leaving');
        d.hidden = false;
        d.inert = false;
        $('screen').inert = true;
        $('appbar').inert = true;
        $('await-scrim').hidden = false;
        $('await-pill').setAttribute('aria-expanded', 'true');
        $('aw-q').value = '';
        awaitRows = awaitCards('');
        awaitIdx = firstLinkable();
        renderAwaitList(true);
        // A finger opens it to tap a card: no on-screen keyboard for the search box
        if (matchMedia('(pointer: coarse)').matches) d.focus(); else $('aw-q').focus();
        return;
    }
    closeAwait(true);
}

// Closes with a slide; the cursor goes back (or to where the work goes on, when a payment was just linked)
function closeAwait(back) {
    const d = $('await');
    d.inert = true;
    d.classList.add('leaving');
    $('screen').inert = false;
    $('appbar').inert = false;
    $('await-scrim').hidden = true;
    $('await-pill').setAttribute('aria-expanded', 'false');
    setTimeout(() => { if (d.classList.contains('leaving')) { d.hidden = true; d.classList.remove('leaving'); renderAwait(); } }, reduceMotion() ? 0 : 170);
    const b = awaitBack;
    awaitBack = null;
    if (back) focusField(usable(b) ? b : resumeField());
    return b;
}

function awaitKey(e) {
    const k = e.key;
    if (k === 'Escape' || k === 'Tab') { e.preventDefault(); return closeAwait(true); }
    if (k === 'ArrowDown' || k === 'ArrowUp') {
        e.preventDefault();
        if (!awaitRows.length) return;
        awaitIdx = (awaitIdx + (k === 'ArrowDown' ? 1 : -1) + awaitRows.length) % awaitRows.length;
        return renderAwaitList();
    }
    if (k === 'Enter') { e.preventDefault(); return actAwait(); }
    if (k === 'Delete' && (e.ctrlKey || !$('aw-q').value)) {
        const c = awaitRows[awaitIdx];
        if (c && c.state === 'linked') { e.preventDefault(); unlinkPayment(c.w); }
        return;
    }
    // Typing on a touch screen (focus on the drawer itself) goes into the search box
    if (k.length === 1 && document.activeElement !== $('aw-q')) $('aw-q').focus();
}

// Enter / click on a card: link it — an empty bill takes its supplier; a bill of another supplier can't take it
function actAwait() {
    const c = awaitRows[awaitIdx];
    if (!c) return;
    const w = c.w, party = getParty(), payee = findLedger('party', w.partyId);
    if (c.state === 'linked') return showToast('Already linked to this bill — Ctrl+Del unlinks it');
    if (party && party.id !== w.partyId) return showToast(`This bill is for ${party.name} — save it or start a new one (Alt+N), then link ${payee.name}'s payment`, true);
    const card = $(`aw-${w.id}`), back = closeAwait(false); // closed first: the card stays put for its flight
    if (!party) bindLedger($('inp-supplier'), payee);
    linkPayment(w);
    updateTotals();
    // It lands on the bill's payment line (or on the supplier, before any line): where it went is seen, not guessed
    const fp = $('foot-pay');
    flyTo(card, fp.offsetParent !== null ? fp : $('inp-supplier'));
    const inv = $('inp-inv');
    focusField(!inv.value.trim() || inv.dataset.auto === '1' ? inv : usable(back) ? back : resumeField());
    showToast(`Linked to this bill · ${formatINR(linkedInBill(w.id))} · paid ${paymentWhen(w)}`);
}

function unlinkPayment(w) {
    document.querySelectorAll('.pay-row[data-mode="linked"]').forEach(r => { if (r.querySelector('.pay-ledger').dataset.id === w.id) r.remove(); });
    updateTotals();
    awaitIdx = Math.min(awaitIdx, Math.max(awaitCards($('aw-q').value).length - 1, 0));
    renderAwaitList();
    showToast(`Unlinked — ${formatINR(leftToLink(w))} waits for its bill again`);
}

// A card flies from the drawer to where it now belongs, shrinking into it; the place lights up as it lands
function flyTo(card, target) {
    if (!target) return;
    if (!card || reduceMotion()) return ping(target, 'landed-pay');
    const a = card.getBoundingClientRect(), b = target.getBoundingClientRect();
    const ghost = card.cloneNode(true);
    ghost.removeAttribute('id');
    ghost.classList.add('aw-ghost');
    Object.assign(ghost.style, { left: a.left + 'px', top: a.top + 'px', width: a.width + 'px', height: a.height + 'px', animationDelay: '0ms' });
    document.body.appendChild(ghost);
    const dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
    const s = Math.max(0.18, Math.min(1, b.height / a.height));
    ghost.animate([
        { transform: 'translate(0, 0) scale(1)', opacity: 1 },
        { transform: `translate(${dx * 0.45}px, ${dy * 0.45 - 36}px) scale(${(1 + s) / 2}) rotate(-2deg)`, opacity: 0.96, offset: 0.5 },
        { transform: `translate(${dx}px, ${dy}px) scale(${s})`, opacity: 0.2 }
    ], { duration: 520, easing: 'cubic-bezier(.3, .7, .2, 1)', fill: 'forwards' }).onfinish = () => { ghost.remove(); ping(target, 'landed-pay'); };
    setTimeout(() => ghost.remove(), 900);
}

function applyVia(row) {
    const via = row.querySelector('.pay-via').dataset.id;
    row.dataset.via = via;
    row.querySelector('.pay-ref-label').textContent = via === 'cheque' ? 'Cheque No.' : via === 'upi' ? 'UPI Ref No.' : 'UTR No.';
    const date = row.querySelector('.pay-date');
    if (via === 'cheque' && !date.dataset.iso) setDate(date, dateOf($('inp-vdate')) || today());
}

// A row "has data" if any numeric field is filled
function itemRowHasData(wrapper) {
    return ['.item-qty', '.item-free', '.item-rate', '.item-amt'].some(s => wrapper.querySelector(s).value !== '');
}

function removeItemRow(wrapper) {
    wrapper.remove();
    if (!document.querySelector('.item-wrapper')) addItemRow();
    updateTotals();
}

// --- MATH ENGINE: the value of the supply and its GST, built the way the GST law builds it ---
// · Every line is taxed at its own rate on its own value (CGST + SGST within the state, IGST from another state;
//   a 0% line carries no tax). Tax is worked out per line to the paisa and added up per rate, as bills print it.
// · A discount on the invoice cuts the value before tax — of the lines it is given on (CGST Act s.15(3)(a)).
// · A charge billed with the goods (freight, packing, loading, labour on the bill) is part of their value (s.15(2)(c))
//   and goes with them as one supply (s.8(a)): it is shared over the lines it is for, by value, and each share is
//   taxed at that line's rate — its share on atta (0%) carries no tax, its share on masala (5%) carries 5%. When the
//   bill charges GST on it at a rate of its own, the line says so (its GST box) and that is used: the tax must match
//   the paper, because that is what the supplier files.
// · A discount after the invoice (cash discount) cuts what is paid, not the tax.
// · A refundable deposit (crates, water jars, cylinders) is no payment for the goods (s.2(31)): no GST, and not part
//   of their cost — it is money that comes back. It adds to what is paid now.
// · Round off is never shared out or taxed: it sits after the tax, on the total (the GST is worked out to the paisa).
// Each charge keeps how its GST was arrived at (x.split: value and tax per rate), shown under it and saved with it.
function computeBill() {
    const party = getParty(), regime = taxRegime(party);
    const inclusive = $('cfg-incl').checked;
    // From the company's GST setup: does the GST paid come back? It only decides what each line costs
    const itc = !!COMPANY.gstCredit && regime !== 'none';
    const groups = new Map();
    const heads = { cgst: 0, sgst: 0, igst: 0, cess: 0 };

    // GST at one rate on one value — into the heads and that rate's group; returns the tax
    const addTax = (rate, cessRate, taxable) => {
        if (regime === 'none' || !taxable) return 0;
        const t = { cgst: 0, sgst: 0, igst: 0, cess: round2(taxable * cessRate / 100) };
        if (regime === 'intra') t.cgst = t.sgst = round2(taxable * rate / 200);
        else t.igst = round2(taxable * rate / 100);
        const g = groups.get(rate) || { rate, taxable: 0, cgst: 0, sgst: 0, igst: 0, cess: 0 };
        Object.keys(heads).forEach(k => { g[k] += t[k]; heads[k] += t[k]; });
        g.taxable += taxable;
        groups.set(rate, g);
        return t.cgst + t.sgst + t.igst + t.cess;
    };
    // A value shared by weight, to the paisa — the shares add up exactly to the value
    const split = (value, weights) => {
        const total = weights.reduce((s, w) => s + w, 0);
        if (!total) return weights.map(() => 0);
        let left = round2(value);
        return weights.map((w, i) => { if (i === weights.length - 1) return left; const s = round2(value * w / total); left = round2(left - s); return s; });
    };

    // 1. Lines: their value before anything bill-level (an inclusive rate is taken back to its value without GST)
    const lines = [...document.querySelectorAll('.item-wrapper')].map(w => {
        const amount = num(w.querySelector('.item-amt'));
        const item = findLedger('item', w.querySelector('.item-name').dataset.id);
        const rate = parseFloat(w.querySelector('.item-gst').dataset.id) || 0;
        const cessRate = item ? item.cess || 0 : 0;
        const base = inclusive && regime !== 'none' ? round2(amount / (1 + (rate + cessRate) / 100)) : amount;
        return { wrapper: w, id: w.dataset.line, item, amount, rate, cessRate, base, discount: 0, taxable: base, tax: 0, charge: 0, chargeTax: 0, cashDisc: 0, cost: 0 };
    });
    const itemsGross = round2(lines.reduce((s, l) => s + l.base, 0));
    const withItem = lines.filter(l => l.item);

    // 2. Charges & discounts, each on its lines (all of them unless told otherwise); a rate is % of those lines' value
    const extras = [...document.querySelectorAll('.charge-row')].map(r => {
        const ledger = findLedger('charge', r.querySelector('.charge-name').dataset.id);
        const rateEl = r.querySelector('.charge-rate'), rateRaw = rateEl.value.trim(), fixedAmt = r.dataset.fixedAmt === '1';
        const onIds = (r.querySelector('.charge-on').dataset.on || '').split(',').filter(Boolean);
        let on = withItem.filter(l => onIds.includes(l.id));
        const all = !on.length;
        if (all) on = withItem.filter(l => l.base > 0); // every line (also when the chosen lines are gone)
        const how = r.querySelector('.charge-gst').dataset.id || (ledger ? String(ledger.gst) : 'items');
        return {
            row: r, ledger, isDisc: !!ledger && ledger.ctype === 'discount', isDeposit: !!ledger && ledger.ctype === 'deposit', on, all, how,
            rateRaw, rate: num(rateEl), byRate: rateRaw !== '' && !fixedAmt,
            typed: num(r.querySelector('.charge-amt')), base: 0, value: 0, tax: 0, split: []
        };
    });
    // Value and tax per rate, for showing and saving how a charge or discount was taxed
    const byRateOf = (ls, shares, taxOf) => {
        const m = new Map();
        ls.forEach((l, i) => { const g = m.get(l.rate) || { rate: l.rate, value: 0, tax: 0, lines: [] }; g.value = round2(g.value + shares[i]); g.lines.push(l); m.set(l.rate, g); });
        return [...m.values()].map(g => ({ ...g, tax: taxOf ? taxOf(g) : 0 })).sort((a, b) => a.rate - b.rate);
    };
    const valueOf = (x, base) => { x.base = round2(base); return x.byRate ? round2(base * x.rate / 100) : x.typed; };
    const baseOf = ls => ls.reduce((s, l) => s + l.base, 0);

    // 3. Discount on the invoice: cuts the value before tax of its lines (shared by value); then each line's own tax
    let discBefore = 0;
    extras.filter(x => x.isDisc && x.ledger.stage === 'before').forEach(x => {
        x.value = valueOf(x, baseOf(x.on));
        discBefore += x.value;
        const shares = split(x.value, x.on.map(l => l.base));
        shares.forEach((s, i) => { x.on[i].discount += s; });
        // The GST it takes off, rate by rate (worked out exactly on the lines below)
        x.split = regime === 'none' ? [] : byRateOf(x.on, shares, g => round2(g.value * g.rate / 100));
    });
    lines.forEach(l => {
        l.taxable = round2(Math.max(l.base - l.discount, 0));
        if (l.taxable > 0) l.tax = addTax(l.rate, l.cessRate, l.taxable);
    });
    const itemsTaxable = round2(lines.reduce((s, l) => s + l.taxable, 0));

    // 4. Charges billed with the goods: shared over their lines by value and taxed as those lines — or at their own
    // rate when the bill says so — or not at all (an exempt service, like a commission agent's on farm produce)
    extras.filter(x => !x.isDisc && !x.isDeposit).forEach(x => {
        x.value = valueOf(x, baseOf(x.on));
        const shares = split(x.value, x.on.map(l => l.taxable || l.base));
        x.on.forEach((l, i) => { l.charge += shares[i]; });
        if (!x.value || x.how === 'none' || regime === 'none') return;
        if (x.how === 'items') {
            const byRate = new Map();
            x.on.forEach((l, i) => {
                const k = `${l.rate}|${l.cessRate}`, g = byRate.get(k) || { rate: l.rate, cess: l.cessRate, value: 0, parts: [] };
                g.value += shares[i];
                g.parts.push([l, shares[i]]);
                byRate.set(k, g);
            });
            byRate.forEach(g => {
                const t = addTax(g.rate, g.cess, round2(g.value));
                x.tax += t;
                g.tax = t;
                split(t, g.parts.map(([, s]) => s)).forEach((s, i) => { g.parts[i][0].chargeTax += s; });
            });
            x.split = [...byRate.values()].map(g => ({ rate: g.rate, value: round2(g.value), tax: round2(g.tax), lines: g.parts.map(([l]) => l) })).sort((a, b) => a.rate - b.rate);
        } else {
            x.tax = addTax(parseFloat(x.how) || 0, 0, x.value);
            split(x.tax, shares).forEach((s, i) => { x.on[i].chargeTax += s; });
            x.split = [{ rate: parseFloat(x.how) || 0, value: x.value, tax: round2(x.tax), lines: x.on, own: true }];
        }
        x.tax = round2(x.tax);
    });
    const charges = round2(extras.filter(x => !x.isDisc && !x.isDeposit).reduce((s, x) => s + x.value, 0));
    Object.keys(heads).forEach(k => heads[k] = round2(heads[k]));
    const computed = { ...heads };

    // 4. A GST head typed as printed on the bill (Alt+T): within ±₹1 it is their rounding and is taken
    const taxCheck = {};
    const regimeHeads = regime === 'intra' ? ['cgst', 'sgst', 'cess'] : regime === 'inter' ? ['igst', 'cess'] : [];
    regimeHeads.forEach(h => {
        if (billTax[h] === undefined || billTax[h].trim() === '') return;
        const v = parseNum(billTax[h]), diff = round2(v - heads[h]);
        taxCheck[h] = { ok: Math.abs(diff) <= 1, diff, computed: heads[h], bill: v };
        if (Math.abs(diff) <= 1) heads[h] = v;
    });

    const gst = round2(heads.cgst + heads.sgst + heads.igst);
    const beforeAfterDisc = round2(itemsTaxable + charges + gst + heads.cess);

    // 5. After-tax (cash) discount cuts only the payable; its percent is of its lines' total with tax
    let discAfter = 0;
    const lineTotal = l => l.taxable + l.tax + l.charge + l.chargeTax;
    extras.filter(x => x.isDisc && x.ledger.stage === 'after').forEach(x => {
        x.value = valueOf(x, x.all ? beforeAfterDisc : x.on.reduce((s, l) => s + lineTotal(l), 0));
        discAfter += x.value;
        split(x.value, x.on.map(lineTotal)).forEach((s, i) => { x.on[i].cashDisc += s; });
    });
    // 6. Refundable deposits: on top of what is paid — outside the GST and outside the goods' cost
    const deposits = round2(extras.filter(x => x.isDeposit).reduce((s, x) => s + (x.value = valueOf(x, baseOf(x.on))), 0));
    extras.filter(x => !x.ledger).forEach(x => { x.value = x.typed; });
    const gross = round2(beforeAfterDisc - discAfter + deposits);

    // 7. Round off: to the nearest ₹, or as printed on the bill (Alt+T). An e-invoice brings the paper's total
    // with it, so then the round off matches it by itself (within ₹1) and a bigger gap is looked into
    const typedRound = (billTax.round || '').trim() !== '' ? parseSigned(billTax.round) : null;
    const paper = einvoice && einvoice.total;
    let roundOff = $('cfg-round').checked ? round2(Math.round(gross) - gross) : 0, roundBy = 'auto';
    if (typedRound !== null) { roundOff = typedRound; roundBy = 'bill'; }
    else if (paper && Math.abs(paper - gross) <= 1) { roundOff = round2(paper - gross); roundBy = 'einvoice'; }
    const grand = round2(gross + roundOff);
    const match = paper ? { ok: Math.abs(paper - grand) < 0.005, diff: round2(paper - grand) } : null;

    // 8. What each line really costs (for food costing): its value, plus the charges that are for it, less its cash
    // discount, plus the GST when no credit comes back — labour for the atta lands on the atta only. Not the round
    // off (a paisa difference, booked to round off) nor a deposit (it comes back)
    lines.forEach(l => { l.cost = round2(l.taxable + l.charge - l.cashDisc + (itc ? 0 : l.tax + l.chargeTax)); });

    // 9. Settlement
    const payments = [...document.querySelectorAll('.pay-row')].filter(r => r.dataset.mode)
        .map(r => ({ row: r, mode: ['cash', 'linked'].includes(r.dataset.mode) ? r.dataset.mode : r.dataset.via, amount: num(r.querySelector('.pay-amt')) }));
    const paid = round2(payments.reduce((s, p) => s + p.amount, 0));
    const taxGroups = [...groups.values()].filter(g => g.taxable > 0).sort((a, b) => a.rate - b.rate);

    return {
        regime, inclusive, itc, party, lines, extras, itemsGross, discBefore: round2(itemsGross - itemsTaxable),
        discBeforeTyped: round2(discBefore), itemsTaxable, charges, ...heads, computed, gst, taxCheck,
        discAfter: round2(discAfter), deposits, gross, roundOff, roundBy, match, grand,
        payments, paid, balance: round2(grand - paid), groups: taxGroups,
        taxable: round2(taxGroups.reduce((s, g) => s + g.taxable, 0))
    };
}

// How a charge's GST was arrived at, in one line under it — shown while the cursor is on it, so the operator (and the
// CA) can see why the bill box says what it says, without a report: "757 → 725 with Wheat Atta at 0% · 32 with …"
function splitText(x, bill) {
    if (!x.ledger || !x.value) return '';
    const names = ls => ls.length === 1 ? ls[0].item.name : `${ls[0].item.name} +${ls.length - 1}`;
    const pct = r => `${fmtPct(r)}%`, tax = round2(x.split.reduce((s, g) => s + g.tax, 0));
    if (x.isDeposit) return 'Refundable deposit — no GST, and not part of the goods\' cost: the money comes back';
    if (bill.regime === 'none') return 'No GST on this bill — this supplier does not charge it';
    if (x.isDisc && x.ledger.stage === 'after') return 'After the tax: the GST stays as billed — only what is paid is less';
    if (x.isDisc && !x.split.length) return 'Cuts the value before tax of the lines it is on';
    if (x.isDisc) return `Cuts the value before tax: ${x.split.map(g => `${formatNum(g.value)} off ${names(g.lines)} (${pct(g.rate)})`).join(' · ')}${tax ? ` → GST ${formatNum(tax)} less` : ' → no GST on these lines anyway'}`;
    if (x.how === 'none') return 'No GST on it';
    if (!x.on.length) return 'No item lines yet — its GST is worked out once the goods are on the bill';
    if (x.split[0] && x.split[0].own) return `GST ${pct(x.split[0].rate)} on ${formatNum(x.value)} = ${formatNum(x.tax)} — the rate the bill prints on it`;
    if (x.split.every(g => !g.rate)) return `No GST — it goes with ${names(x.on)}, which ${x.on.length === 1 ? 'is' : 'are'} at 0%`;
    if (x.split.length === 1) return `GST as the goods it is for: ${pct(x.split[0].rate)} on ${formatNum(x.value)} = ${formatNum(x.tax)}`;
    return `Shared by value: ${x.split.map(g => `${formatNum(g.value)} with ${names(g.lines)} at ${pct(g.rate)}`).join(' · ')} → GST ${formatNum(x.tax)}`;
}

// Total quantity only means something when every line is in the same unit
function totalQty(bill) {
    const lines = bill.lines.filter(l => l.item && num(l.wrapper.querySelector('.item-qty')) > 0);
    const units = [...new Set(lines.map(l => splitUnit(l.wrapper.querySelector('.item-qty')).unit))];
    if (!lines.length || units.length !== 1) return '';
    return `${FMT.qty(lines.reduce((s, l) => s + splitUnit(l.wrapper.querySelector('.item-qty')).n, 0))} ${unitLabel(units[0])}`;
}

// Expiry under a line: when, and what is wrong with it — expired before it came (send it back), expired since, a short
// life (a third or less of its shelf life left when it came), a year that can't be meant. An estimate says so
const dayWord = n => `${n} day${n === 1 ? '' : 's'}`;
function renderExpiry(l) {
    const exp = l.wrapper.querySelector('.item-exp'), eh = l.wrapper.querySelector('.exp-hint');
    const life = l.item && l.item.expiry ? l.item.life || 0 : 0, est = exp.dataset.auto === '1';
    // The estimate follows the bill date (typed after the lines, or changed)
    if (est && life && document.activeElement !== exp) estimateExpiry(exp, l.item);
    exp.classList.toggle('est', est);
    const ed = exp.dataset.iso ? fromIso(exp.dataset.iso) : null;
    let text = '', cls = '';
    if (ed) {
        const left = Math.round((ed - dueFrom()) / 86400000), inDays = Math.round((ed - today()) / 86400000);
        text = inDays < 0 ? `expired ${dayWord(-inDays)} ago` : inDays === 0 ? 'expires today' : `${WEEKDAYS[ed.getDay()]} · in ${dayWord(inDays)}`;
        if (left < 0) { text = 'expired before it came — send it back?'; cls = 'bad'; }
        else if (inDays < 0) cls = 'bad';
        else if (est) { text = `est. · ${lifeText(life)} shelf life`; cls = 'est'; }
        else if (left > 3 * 365) { text += ' · over 3 years away — check the year'; cls = 'warn'; }
        else if (life ? left < life / 3 : inDays <= 15) { if (life) text += ` · came with ${dayWord(left)} of ${lifeText(life)}`; cls = 'warn'; }
        if (est && cls === 'bad') text = `est. · ${text}`;
    }
    eh.textContent = text;
    eh.className = 'exp-hint' + (cls ? ` ${cls}` : '');
}

// MRP under a line (items sold as bought): what one piece costs with GST, against the MRP (which has every tax in it).
// Above it is a slip on the bill or an old MRP (red); under it, the margin; a new MRP against the last one paid
// (amber: the counter / menu price may need to follow); with free goods, what each one comes to with them in
function renderMrp(l, billed, freeBase, regime) {
    const mh = l.wrapper.querySelector('.mrp-hint');
    const mrp = num(l.wrapper.querySelector('.item-mrp')), was = +l.wrapper.dataset.lastMrp || 0;
    const pcs = l.item && l.item.mrp ? l.item.pcs || 1 : 0, paid = l.taxable + l.tax;
    const each = pcs && billed > 0 && paid > 0 ? paid / (billed * pcs) : 0, withGst = regime === 'none' ? '' : ' with GST';
    const parts = [];
    let cls = '';
    if (mrp && was && Math.abs(mrp - was) >= 0.005) { parts.push(`${mrp > was ? '▲' : '▼'} MRP was ₹${FMT.money(was)} — sale price to change?`); cls = 'warn'; }
    if (each && mrp) {
        if (each > mrp + 0.005) { parts.push(`₹${FMT.money(each)} / pc${withGst} — above the MRP`); cls = 'bad'; }
        else parts.push(`₹${FMT.money(each)} / pc${withGst} · ${Math.round((1 - each / mrp) * 100)}% under MRP`);
        if (freeBase > 0) parts.push(`₹${FMT.money(paid / ((billed + freeBase) * pcs))} with the free ones`);
    }
    mh.textContent = parts.join(' · ');
    mh.className = 'mrp-hint' + (cls ? ` ${cls}` : '');
}

function updateTotals() {
    let bill = computeBill();
    // Payment amounts the system filled follow the bill (worked out again only when one of them changed)
    if (refreshAutoPayments(bill.grand)) bill = computeBill();

    // Lines: the other unit under the qty, "per" next to the rate, rate vs the last purchase
    bill.lines.forEach(l => {
        const cell = l.wrapper.querySelector('.item-tax');
        cell.textContent = bill.regime === 'none' || !l.amount ? '' : formatNum(l.tax);
        l.wrapper.querySelector('.item-gst').disabled = bill.regime === 'none';

        const qtyInput = l.wrapper.querySelector('.item-qty'), rateInput = l.wrapper.querySelector('.item-rate');
        const q = splitUnit(qtyInput), r = splitUnit(rateInput);
        // Per: the unit the rate is per (a box of its own — the bill may price another unit than it counts)
        const per = l.wrapper.querySelector('.item-per'), perUnit = l.item && !isService(l.item) ? r.unit : null;
        per.disabled = !perUnit;
        if (document.activeElement !== per) { per.value = perUnit ? unitLabel(perUnit) : ''; if (perUnit) per.dataset.id = perUnit; else delete per.dataset.id; }
        l.wrapper.classList.toggle('multi-unit', !!l.item && itemUnits(l.item).length > 1);
        const other = l.item && q.n > 0 && q.unit ? otherUnit(l.item, q.unit) : null;
        qtyInput.parentElement.querySelector('.alt-qty').textContent = other ? `(${FMT.qty(qtyInBase(qtyInput) / other.f)} ${unitLabel(other.u)})` : '';

        // Rate vs last purchase, compared per main unit (₹29/Kg on Atta = ₹1,450/Katta).
        // Dearer is a cost leak: amber from 5 %, red from 20 %. Cheaper is good news and stays quiet —
        // unless it is so low (25 %+) that it is more likely a typing slip
        const perBase = l.item && !isService(l.item) && r.n > 0 ? ratePerBase(rateInput) : 0;
        const change = perBase ? (perBase - l.item.lastRate) / l.item.lastRate : 0;
        const level = change > 0.20 ? 'high' : change > 0.05 ? 'up' : change < -0.25 ? 'down' : '';
        rateInput.classList.toggle('rate-warn', level === 'up' || level === 'high');
        rateInput.classList.toggle('rate-high', level === 'high');
        const delta = l.wrapper.querySelector('.rate-delta');
        delta.hidden = !level;
        delta.className = 'rate-delta ' + level;
        const pct = Math.abs(Math.round(change * 100)), lastText = FMT.money(perBase ? l.item.lastRate * unitFactor(l.item, r.unit) : 0);
        delta.textContent = !level ? '' : `${level === 'down' ? '▼' : '▲'} ${pct}% · was ${lastText}${level === 'down' ? ' ?' : ''}`;
        delta.title = !level ? '' : level === 'down' ? 'Much lower than the last purchase — check the rate on the bill' : `Last purchase rate ₹${lastText} — the supplier has raised the price`;

        // Free: what each one really comes to with the free ones in ("net"), per the unit the rate is per
        const fr = l.item && !isService(l.item) ? freeCounted(l.wrapper) : { base: 0 }, billed = qtyInBase(qtyInput);
        const net = l.wrapper.querySelector('.free-net'), netRate = fr.base > 0 && l.amount > 0 && r.unit ? l.amount / ((billed + fr.base) / unitFactor(l.item, r.unit)) : 0;
        net.textContent = netRate ? `net ${FMT.money(netRate)}` : '';
        net.title = netRate ? `₹${FMT.money(netRate)} a ${unitLabel(r.unit)} with the free ones in` : '';
        renderExpiry(l);
        renderMrp(l, billed, fr.base, bill.regime);
    });

    // Charges & discount: computed amount, which lines (and the value a % works on), the sign, its GST and the tag
    bill.extras.forEach(x => {
        const amt = x.row.querySelector('.charge-amt'), on = x.row.querySelector('.charge-on');
        if (x.byRate && document.activeElement !== amt) amt.value = x.value ? FMT.money(x.value) : '';
        if (document.activeElement !== on) on.value = onText(on.dataset.on);
        on.classList.toggle('some', !x.all);
        on.hidden = !x.ledger || x.isDeposit; // a blank line has nothing to put on lines yet; a deposit is on no goods
        x.row.querySelector('.charge-per').textContent = x.rateRaw !== '' ? '%' : '';
        x.row.querySelector('.charge-base').textContent = x.rateRaw !== '' && x.base ? `of ${formatNum(x.base)}` : '';
        x.row.classList.toggle('is-disc', x.isDisc);
        x.row.classList.toggle('is-deposit', x.isDeposit);
        x.row.querySelector('.sign').textContent = x.isDisc ? '−' : '';
        x.row.querySelector('.charge-tax').textContent = bill.regime === 'none' || x.isDisc || x.isDeposit || !x.value ? '' : formatNum(x.tax);
        const gstEl = x.row.querySelector('.charge-gst');
        gstEl.disabled = bill.regime === 'none' || x.isDisc || x.isDeposit;
        // The tag says what is unusual: a discount and when it applies, a deposit, or GST at a rate of its own / none
        x.row.querySelector('.charge-tag').textContent = x.isDisc
            ? `less · ${x.ledger.stage === 'before' ? 'before tax' : 'after tax'}`
            : x.isDeposit ? 'deposit · comes back'
            : bill.regime === 'none' || !x.ledger ? '' : x.how === 'none' ? 'no GST' : x.how !== 'items' ? `GST ${x.how}%` : '';
        x.row.querySelector('.charge-split').textContent = splitText(x, bill);
    });

    // Items total right under the lines (before the charges, as the paper has it): count, qty when the units agree, amount
    const count = bill.lines.filter(l => l.item).length;
    $('items-total').hidden = !count;
    $('st-count').textContent = count ? `· ${count} line${count === 1 ? '' : 's'}` : '';
    $('st-qty').textContent = totalQty(bill);
    $('st-amt').textContent = formatNum(bill.itemsGross);
    $('st-tax').textContent = bill.regime === 'none' ? '' : formatNum(round2(bill.lines.reduce((s, l) => s + l.tax, 0)));

    renderBillBox(bill);
    renderDiag(bill);
    renderSettlement(bill);
    renderPartyInfo(bill);
    renderAwait();
    lastBill = bill;
    scheduleDraft();
    return bill;
}

// Bottom right, only what the system works out: CGST and SGST (or IGST) and Cess head by head with the rate, the
// round off and the total. What the operator typed — items total, charges — is in the body, once; never shown twice.
// A head or the round off printed differently: Alt+T
function renderBillBox(bill) {
    const working = head => bill.groups.filter(g => g[head] > 0).map(g => head === 'cess'
        ? `on ${formatNum(g.taxable)}` : `${fmtPct(head === 'igst' ? g.rate : g.rate / 2)}% on ${formatNum(g.taxable)} = ${formatNum(g[head])}`).join('\n');

    const heads = bill.regime === 'intra' ? ['cgst', 'sgst', 'cess'] : bill.regime === 'inter' ? ['igst', 'cess'] : [];
    TAX_HEADS.forEach(head => {
        const r = $('billbox').querySelector(`.bb-tax[data-head="${head}"]`), inp = r.querySelector('.bill-tax');
        const typed = (billTax[head] || '').trim() !== '';
        // A head with nothing on it (all lines 0%, like vegetables) isn't printed on the paper — so not here either
        const show = heads.includes(head) && (bill.computed[head] > 0 || typed);
        r.hidden = !show;
        if (!show) return;
        const c = bill.taxCheck[head];
        r.classList.toggle('ok', !!c && c.ok);
        r.classList.toggle('bad', !!c && !c.ok);
        r.title = working(head);
        // No rate / "on ₹…" text beside the heads (the working is in the row's hover title); only the check against
        // a tax typed from the supplier's bill (Alt+T) shows here
        r.querySelector('.bb-note').textContent = !c ? ''
            : c.ok ? (c.diff ? `as per bill · ours ${formatNum(c.computed)}` : 'as per bill ✓')
            : `bill ${formatNum(c.bill)} · ours ${formatNum(c.computed)}`;
        inp.classList.toggle('ok', !!c && c.ok);
        inp.classList.toggle('bad', !!c && !c.ok);
        if (document.activeElement !== inp) inp.value = c && !c.ok ? FMT.money(c.bill) : formatNum(bill[head]);
    });

    // Round off: an input too, for a bill rounded another way (or not at all)
    const rr = $('bb-round'), ri = rr.querySelector('input'), roBad = Math.abs(bill.roundOff) > 1;
    rr.classList.toggle('bad', roBad);
    rr.classList.toggle('ok', bill.roundBy !== 'auto' && !roBad);
    rr.querySelector('.bb-note').textContent = roBad ? 'more than ₹1 — a discount (kasar)?' : bill.roundBy === 'bill' ? 'as per bill' : bill.roundBy === 'einvoice' ? 'to match the e-invoice' : '';
    if (document.activeElement !== ri) ri.value = signed(bill.roundOff);
    $('lbl-grand').textContent = formatINR(bill.grand);
}

// When the paper's total is known without typing (e-invoice QR now, the bill reader later) and differs from ours,
// the system looks for the usual reasons and points at the line
function diagnose(bill) {
    if (!bill.match || bill.match.ok) return [];
    const d = round2(einvoice.total - bill.gross); // + : the paper is more than ours
    const near = v => Math.abs(d - v) <= 1;
    const out = [];
    const lines = bill.lines.filter(l => l.item && l.amount > 0);
    const amtOf = l => l.wrapper.querySelector('.item-amt');
    const lineTotal = l => round2(l.amount + l.tax);
    // 1. One line taxed at another GST rate than our item master says
    if (bill.regime !== 'none') lines.forEach(l => GST_RATES.forEach(r => {
        const v = round2(l.taxable * (r - l.rate) / 100);
        if (r !== l.rate && Math.abs(v) >= 1 && near(v)) out.push({ field: l.wrapper.querySelector('.item-gst'), gst: true, text: `${l.item.name} — the bill seems to charge GST ${r}%, we have ${l.rate}%` });
    }));
    // 2. A line entered twice, or one more on the bill
    lines.forEach(l => {
        if (near(-lineTotal(l))) out.push({ field: amtOf(l), text: `${l.item.name} (${formatINR(lineTotal(l))}) is not on the bill — entered twice?` });
        else if (near(lineTotal(l))) out.push({ field: amtOf(l), text: `the bill has one more ${l.item.name} line (${formatINR(lineTotal(l))})?` });
    });
    // 2b. Free goods typed into the billed qty: 10 + 1 free entered as 11 makes ours one piece dearer
    if (d < 0) lines.forEach(l => {
        const qty = l.wrapper.querySelector('.item-qty'), q = splitUnit(qty).n, each = q > 0 ? lineTotal(l) / q : 0;
        const k = each ? Math.round(-d / each) : 0;
        if (q > 1 && k >= 1 && k <= Math.max(1, Math.floor(q / 4)) && near(-k * each)) out.push({ field: qty, text: `${k} of the ${FMT.int(q)} ${l.item.name} may be free — type ${FMT.int(q - k)}+${k} in the qty` });
    });
    // 3. The decimal point slipped on a line
    lines.forEach(l => {
        if (near(-lineTotal(l) * 0.9)) out.push({ field: amtOf(l), text: `${l.item.name} looks 10× too high — check the decimal point` });
        if (near(lineTotal(l) * 9)) out.push({ field: amtOf(l), text: `${l.item.name} looks 10× too low — check the decimal point` });
    });
    // 4. A charge or discount the bill doesn't have
    bill.extras.filter(x => x.ledger && x.value > 0).forEach(x => {
        if (near(x.isDisc ? x.value : -x.value)) out.push({ field: x.row.querySelector('.charge-amt'), text: `${x.ledger.name} (${formatINR(x.value)}) is not on the bill?` });
    });
    // 5. The bill's rates already have GST in them
    const tax = round2(bill.gst + bill.cess);
    if (bill.regime !== 'none' && !bill.inclusive && tax > 1 && near(-tax)) out.push({ text: 'the rates on this bill already include GST — turn on "Rates Include GST" in Settings (F12)' });
    // 6. Two digits swapped somewhere: the difference divides by 9
    const whole = Math.round(Math.abs(d));
    if (!out.length && whole >= 9 && Math.abs(Math.abs(d) - whole) < 0.01 && whole % 9 === 0) out.push({ text: 'the difference divides by 9 — two digits may be swapped in a rate or an amount' });
    return out.slice(0, 2);
}

function renderDiag(bill) {
    const box = $('bill-diag');
    lastDiag = diagnose(bill);
    box.hidden = !(bill.match && !bill.match.ok);
    box.textContent = '';
    if (box.hidden) return;
    const d = bill.match.diff;
    const head = document.createElement('b');
    head.textContent = `The e-invoice total (${formatINR(einvoice.total)}) is ${formatINR(Math.abs(d))} ${d > 0 ? 'more' : 'less'} than this entry.`;
    box.append(head, ' ', lastDiag.length ? `Likely: ${lastDiag.map(h => h.text).join(' — or ')}.` : 'Check the lines against the bill, one by one.');
    if (lastDiag.some(h => h.field)) {
        const go = document.createElement('div');
        go.className = 'dg-go';
        go.textContent = 'Saving takes you to that line';
        box.append(go);
    }
}

// Enter on a total that doesn't match: straight to the line that is most likely wrong
function goToSuspect(hint) {
    if (hint.gst && !$('cfg-tax').checked) { $('cfg-tax').checked = true; applyConfig(); }
    const row = hint.field.closest('.data-row');
    row.classList.remove('suspect-flash');
    void row.offsetWidth; // restart the flash
    row.classList.add('suspect-flash');
    focusField(hint.field);
    showToast(hint.text + (hint.gst ? ' — the GST column is now on' : ''), true);
}

function renderSettlement(bill) {
    $('settle-grand').textContent = formatINR(bill.grand);

    // Udhaar for whatever is not paid now, due on the supplier's terms unless typed otherwise
    const credit = bill.balance > 0.004;
    $('credit-block').hidden = !credit;
    $('lbl-credit').textContent = formatINR(Math.max(bill.balance, 0));
    // The date box follows the days (a new bill date moves it too); the weekday beside it
    syncDueDate();
    const due = currentDue(), days = due ? due.days : null;
    $('due-date').textContent = due ? `${WEEKDAYS[due.date.getDay()]}${days === 0 ? ' · on the bill date' : ''}` : '';
    const interest = $('cfg-interest').checked ? num($('inp-interest')) : 0;
    $('interest-note').textContent = credit && interest > 0 ? `≈ ${formatINR(round2(bill.balance * interest / 100 / 365))} a day after the due date` : '';

    // Warnings in plain words (the rule, not the section number)
    const warns = [];
    const party = bill.party;
    if (credit && party && party.msme && days > MSME_MAX_DAYS) warns.push(`This supplier is MSME — not paid within ${MSME_MAX_DAYS} days, the expense is not allowed in income tax until it is paid.`);
    const cash = bill.payments.filter(p => p.mode === 'cash').reduce((s, p) => s + p.amount, 0);
    if (cash > CASH_LIMIT) warns.push('Cash above ₹10,000 to one supplier in a day is not allowed as a business expense — pay by bank / UPI.');
    bill.payments.filter(p => p.mode === 'cheque').forEach(p => {
        const cd = dateOf(p.row.querySelector('.pay-date'));
        if (cd && cd > today()) warns.push(`Cheque dated ${fmtDate(cd)} — the money goes out on that day.`);
    });
    if (bill.balance < -0.004) warns.push(`Paid ${formatINR(-bill.balance)} more than the bill.`);
    const lineCount = bill.lines.filter(l => l.item).length;
    if (einvoice && einvoice.itemCount && lineCount !== einvoice.itemCount) warns.push(`The e-invoice says ${einvoice.itemCount} lines — this entry has ${lineCount}.`);
    if (bill.itc && einvoice && einvoice.buyerGstin && einvoice.buyerGstin !== COMPANY.gstin) warns.push(`This bill is made out to GSTIN ${einvoice.buyerGstin}, not ours — no GST credit on it.`);
    $('settle-warn').innerHTML = '';
    warns.forEach(w => { const li = document.createElement('li'); li.textContent = w; $('settle-warn').appendChild(li); });

    // Footer: the payment in one line once it has been looked at; before that, what is usual for this supplier
    const fp = $('foot-pay');
    fp.hidden = !party || !bill.lines.some(l => l.item);
    if (fp.hidden) return;
    const paidFrom = bill.payments.map(p => p.mode === 'linked' ? 'earlier payment' : p.row.querySelector('.pay-ledger').value).join(' + ');
    // A payment linked from the drawer counts as looked at: it shows here at once
    const shown = paymentSeen || bill.payments.length > 0;
    const text = !shown ? (party.defaultPay === 'cash' ? 'usually paid on the spot' : `usually udhaar · ${creditTermOf(party)}`)
        : [bill.paid > 0 && `Paid ${formatINR(bill.paid)} · ${paidFrom}`, credit && `Udhaar ${formatINR(bill.balance)}${due ? ` · due ${fmtDate(due.date)} ${WEEKDAYS[due.date.getDay()]}` : ''}`].filter(Boolean).join(' + ') || 'Nothing to pay';
    fp.classList.remove('preview', 'udhaar', 'paid'); // other classes (a landing glow) are left to finish
    fp.classList.add(!shown ? 'preview' : credit ? 'udhaar' : 'paid');
    fp.replaceChildren(Object.assign(document.createElement('span'), { className: 'fp-label', textContent: 'Payment' }), text,
        Object.assign(document.createElement('span'), { className: 'fp-key', textContent: 'Alt+4' }));
}

// --- SECTIONS OPEN ONE AFTER ANOTHER: Lines → Charges & discount → (Payment popup) ---
function setSection(id, open) {
    const el = $(id);
    el.classList.toggle('revealed', open);
    el.inert = !open;
}

// The body stays empty while the header is being filled; lines appear when the cursor comes down
function openItems() {
    if (!itemsOpen) { itemsOpen = true; setSection('items-sec', true); updateTotals(); }
}

function openCharges() {
    openItems();
    if (!chargesOpen) { chargesOpen = true; setSection('zen-charges', true); }
    updateTotals();
}

function closeSections() {
    itemsOpen = chargesOpen = false;
    setSection('items-sec', false);
    setSection('zen-charges', false);
}

// --- PAYMENT: a popup over the bill after the charges. Enter on its last box keeps the payment with the bill and
// the cursor goes on to the bill's Note; saving the bill stays the bill's own step ---
function openSettle() {
    if (settleOpen) return;
    settleOpen = true;
    paymentSeen = true;
    focusBeforeSettle = document.activeElement;
    Suggest.close();
    const party = getParty(), earlier = linkEntries(party).filter(e => !linkedInBill(e.id));
    // A cash supplier starts on the usual counter; everyone else on an empty ledger (End of List = all udhaar).
    // A supplier paid before the bill starts on that earlier payment instead: the same money entered a second time
    // is the costliest slip on this screen (cash book and supplier balance both go wrong)
    if (!document.querySelector('.pay-row')) addPayRow(party && party.defaultPay === 'cash' && !earlier.length ? LEDGER.cash[0] : null);
    $('screen').inert = true;
    $('appbar').inert = true;
    const overlay = $('settle');
    overlay.hidden = false;
    overlay.inert = false;
    const billNo = $('inp-inv').dataset.auto === '1' ? '' : $('inp-inv').value.trim();
    const bill = updateTotals(), count = bill.lines.filter(l => l.item).length;
    $('settle-sub').textContent = [party ? party.name : 'No supplier yet', billNo && `Bill ${billNo}`, `${count} line${count === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
    const first = document.querySelector('.pay-row .pay-ledger');
    focusField(first);
    // Its earlier payments show at once (amount · date and time · note), so nobody pays the same money twice.
    // A cash supplier was most likely paid at delivery: that payment is highlighted. Anyone else most likely paid
    // nothing now: End of List stays highlighted (a habitual Enter never links a payment to the wrong bill)
    if (!first.dataset.id && earlier.length) {
        Suggest.show(first, '');
        if (party.defaultPay === 'cash') { Suggest.idx = Suggest.items.findIndex(o => o.id === earlier[0].id); Suggest.render(); }
    }
}

function closeSettle(focusBack = true) {
    if (!settleOpen) return;
    settleOpen = false;
    Suggest.close();
    $('settle').hidden = true;
    $('settle').inert = true;
    $('screen').inert = false;
    $('appbar').inert = false;
    // An empty payment row isn't worth keeping
    document.querySelectorAll('.pay-row').forEach(r => { if (!r.dataset.mode && r.querySelector('.pay-amt').value === '') r.remove(); });
    updateTotals();
    const back = focusBeforeSettle;
    focusBeforeSettle = null;
    if (!focusBack) return;
    // Back where it was opened from; opened from the blank charge line, that line comes back
    focusField(back && back.isConnected && !back.closest('[inert]') && back.offsetParent !== null ? back : firstEmptyCharge());
}

// Done (Enter on the last box): the payment is checked and kept with the bill — the bill itself is not saved yet
function confirmPayment() {
    if (!collectPayments()) return false;
    focusBeforeSettle = null;
    closeSettle(false);
    ping($('foot-pay'));
    focusField($('inp-remarks'));
    return true;
}

// Payment lines and udhaar, checked; false (with the cursor on the problem) when something is missing
function collectPayments(bill = updateTotals()) {
    const ours = $('inp-vdate'), payments = [];
    for (const r of document.querySelectorAll('.pay-row')) {
        const ledgerEl = r.querySelector('.pay-ledger'), amt = r.querySelector('.pay-amt');
        if (!r.dataset.mode) {
            if (amt.value !== '') return flagError(ledgerEl, 'Choose the cash or bank it was paid from');
            continue;
        }
        if (!(num(amt) > 0)) return flagError(amt, 'Payment amount is missing');
        // An earlier payment: no new money moves — only how much of it this bill takes, never more than is left
        if (r.dataset.mode === 'linked') {
            const w = UNLINKED_PAYMENTS.find(x => x.id === ledgerEl.dataset.id);
            if (linkedInBill(w.id) > leftToLink(w) + 0.004) return flagError(amt, `Only ${formatINR(leftToLink(w))} of that payment is left to link`);
            payments.push({ mode: 'linked', amount: num(amt), linkedPaymentId: w.id, ledgerId: w.id, ledger: ledgerEl.value, ref: null, date: isoOf(paidOn(w)) });
            continue;
        }
        const via = r.dataset.mode === 'cash' ? 'cash' : r.dataset.via;
        const ref = r.querySelector('.pay-ref'), date = r.querySelector('.pay-date');
        if (via === 'cheque' && !/^\d{6}$/.test(ref.value.trim())) return flagError(ref, 'Cheque number must be 6 digits');
        if (via === 'cheque' && (!commitDate(date) || !date.dataset.iso)) return flagError(date, 'Cheque date is required');
        payments.push({
            mode: via, amount: num(amt), ledgerId: ledgerEl.dataset.id, ledger: ledgerEl.value,
            ref: via === 'cash' ? null : ref.value.trim() || null,
            // Cash and transfers are dated our purchase date; a cheque carries its own date
            date: via === 'cheque' ? date.dataset.iso : ours.dataset.iso
        });
    }
    if (bill.balance < -0.004) {
        const last = [...document.querySelectorAll('.pay-row .pay-amt')].pop();
        return flagError(last || $('inp-remarks'), `Paid ${formatINR(-bill.balance)} more than the bill`);
    }
    // Udhaar for the unpaid balance
    let credit = null;
    if (bill.balance > 0.004) {
        // The date box may still hold a typed date that wasn't committed yet
        if (document.activeElement === $('inp-duedate') && !commitDueDate()) return false;
        const due = commitDue();
        if (!due) return false;
        const interestOn = $('cfg-interest').checked;
        credit = {
            amount: bill.balance, dueTerm: `${due.days} days`, dueDate: isoOf(due.date), dueDays: due.days,
            interestPA: interestOn ? num($('inp-interest')) : 0, lateFee: interestOn ? num($('inp-latefee')) : 0
        };
    }
    return { payments, credit };
}

// Amounts the system filled follow the bill until typed over. An earlier payment gives at most what is left of it
// (before any line is entered it shows its whole amount, so it is clear what is being linked). True = one changed
function refreshAutoPayments(grand) {
    let remaining = grand, changed = false;
    document.querySelectorAll('.pay-row').forEach(r => {
        const amt = r.querySelector('.pay-amt');
        if (r.dataset.mode && amt.dataset.auto === '1' && document.activeElement !== amt) {
            const w = r.dataset.mode === 'linked' ? UNLINKED_PAYMENTS.find(x => x.id === r.querySelector('.pay-ledger').dataset.id) : null;
            const v = w ? (grand > 0 ? Math.min(leftToLink(w), Math.max(remaining, 0)) : leftToLink(w)) : Math.max(remaining, 0);
            const text = v > 0 ? FMT.money(v) : '';
            if (amt.value !== text) { amt.value = text; changed = true; }
        }
        remaining = round2(remaining - num(amt));
    });
    return changed;
}

// --- A LINE GOES WHERE IT BELONGS: type every line where the cursor is. A charge typed among the items flies
// below them, an item typed among the charges flies up. The cursor goes with the line at once — typing never
// waits for the animation, and the eye and the cursor end up in the same place ---
function teleportToCharges(itemInput, entry, numText) {
    const wrapper = itemInput.closest('.item-wrapper');
    // "commission 6" / "disc 2%" is a rate, "bhada 200" an amount; an amount already typed in the row is kept
    const prefill = numText !== undefined ? chargeValue(entry, numText)
        : wrapper.querySelector('.item-amt').value ? { amount: num(wrapper.querySelector('.item-amt')) } : null;
    const from = placeOf(wrapper);
    closeGap(wrapper);
    if (!document.querySelector('.item-wrapper')) addItemRow();
    openCharges();
    const row = addChargeRow(entry, prefill);
    // A complete line ("bhada 200") goes straight on to the next line; otherwise the cursor waits for its rate / amount
    focusField(prefill ? nextChargeName(row) : row.querySelector('.charge-rate'), false);
    flyLine(from, row, entry, 'down');
}

function teleportToItems(chargeInput, entry, pl = null) {
    const row = chargeInput.closest('.charge-row');
    const from = placeOf(row);
    closeGap(row);
    openItems();
    const name = firstEmptyItem();
    if (pl) powerLine(name, pl, entry);
    else { bindLedger(name, entry); goNext(name); }
    flyLine(from, name.closest('.item-wrapper'), entry, 'up');
}

// Where a line is, in <main>'s own coordinates — so a flight in progress scrolls together with the lines
function placeOf(row) {
    const main = document.querySelector('main'), m = main.getBoundingClientRect();
    const r = (row.querySelector('.name-cell, .charge-cell') || row).getBoundingClientRect();
    return { x: r.left - m.left + main.scrollLeft, y: r.top - m.top + main.scrollTop, h: r.height };
}

// The line leaves a gap that closes up behind it, so the lines below glide up instead of jumping
function closeGap(row) {
    const gap = document.createElement('div');
    gap.className = 'gap-row';
    gap.style.height = row.offsetHeight + 'px';
    row.replaceWith(gap);
    updateTotals();
    if (reduceMotion()) return gap.remove();
    requestAnimationFrame(() => requestAnimationFrame(() => gap.classList.add('closing')));
    setTimeout(() => gap.remove(), 450);
}

const movesSeen = () => { try { return +localStorage.getItem(MOVES_KEY) || 0; } catch (err) { return 99; } };
const countMove = () => { try { localStorage.setItem(MOVES_KEY, String(movesSeen() + 1)); } catch (err) { /* no count: no lesson */ } };

// The line lifts off as a chip, flies an arc to its new place with a short trail, and lands with a ripple; the
// section and the bill-box line it went to light up for a moment. The first few moves are slower and explained
// (a toast); after that the motion is short — it only has to say where the line went
function flyLine(from, toRow, entry, dir) {
    const learning = movesSeen() < 3;
    countMove();
    const what = dir === 'up' ? 'an item' : entry.ctype === 'discount' ? 'a discount' : 'a charge';
    if (learning) showToast(`"${entry.name}" is ${what} — placed ${dir === 'up' ? 'in the item lines' : 'under Charges & discount'}`);
    toRow.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'nearest' });
    if (reduceMotion()) return land(toRow, dir);

    const main = document.querySelector('main'), to = placeOf(toRow);
    const dx = to.x - from.x, dy = to.y - from.y, bulge = Math.min(160, 50 + Math.abs(dy) * 0.3);
    const ease = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    // A curve, not a slide: out to the right on the way, a slight tilt into the turn, a lift and a soft landing
    const frames = [];
    for (let i = 0; i <= 18; i++) {
        const o = i / 18, t = ease(o), u = 1 - t;
        const x = 2 * u * t * (dx / 2 + bulge) + t * t * dx, y = t * dy;
        const s = o < 0.15 ? 1 + 0.07 * o / 0.15 : o > 0.85 ? 1.07 - 0.1 * (o - 0.85) / 0.15 : 1.07;
        frames.push({ offset: o, transform: `translate(${x}px, ${y}px) rotate(${Math.sin(o * Math.PI) * (dy > 0 ? -2.5 : 2.5)}deg) scale(${s})` });
    }
    const chip = document.createElement('div');
    chip.className = `fly-chip ${dir}`;
    chip.append(Object.assign(document.createElement('b'), { textContent: entry.name }),
        Object.assign(document.createElement('span'), { textContent: dir === 'up' ? '↑ items' : '↓ charges' }));
    Object.assign(chip.style, { left: from.x + 'px', top: from.y + 'px', height: from.h + 'px' });
    const duration = learning ? 680 : 420;
    const trail = [0.3, 0.18, 0.08].slice(0, learning ? 3 : 1).map((op, k) => {
        const c = chip.cloneNode(true);
        c.classList.add('trail');
        main.appendChild(c);
        c.animate(frames.map(f => ({ ...f, opacity: f.offset < 0.9 ? op : 0 })), { duration, delay: (k + 1) * 34, fill: 'both' }).onfinish = () => c.remove();
        return c;
    });
    main.appendChild(chip);
    toRow.classList.add('awaiting');
    chip.animate(frames, { duration, fill: 'forwards' }).onfinish = () => { chip.remove(); land(toRow, dir); };
    // Never leave a chip behind, whatever happens to the page meanwhile
    setTimeout(() => { [chip, ...trail].forEach(c => c.remove()); toRow.classList.remove('awaiting'); }, duration + 400);
}

// Landing: the row ripples, its section title and its bill-box line light up
function land(row, dir) {
    row.classList.remove('awaiting');
    const target = row.classList.contains('item-wrapper') ? row.querySelector('.item-row') : row;
    ping(target, 'landed');
    ping(dir === 'down' ? $('zen-charges').querySelector('.sec-label') : $('items-total'));
}

// --- FOCUS GLIDE: a soft ring travels from the box just left to the box the cursor lands in, then fades. The eye
// follows motion by itself (smooth pursuit), so after any jump — Enter to the next box, a line moved, the charges to
// the payment — it arrives where the cursor is without searching for it. 110-260 ms by distance; never in the way of
// typing (it only draws); off with reduced motion ---
const Glide = {
    el: null, from: null, timer: 0,
    follow(to) {
        if (reduceMotion() || !to || !to.classList || !(to.classList.contains('nav-input') || BOXES[to.id] || to.id === 'aw-q')) return this.hide();
        const from = this.from && this.from.isConnected && this.from !== to ? this.from : null;
        this.from = to;
        requestAnimationFrame(() => {
            if (document.activeElement !== to) return;
            const el = this.el || (this.el = document.body.appendChild(Object.assign(document.createElement('div'), { id: 'glide' })));
            const b = to.getBoundingClientRect(), a = from ? from.getBoundingClientRect() : null;
            if (!b.width) return this.hide();
            const place = r => { el.style.transform = `translate(${r.left - 3}px, ${r.top - 3}px)`; el.style.width = `${r.width + 6}px`; el.style.height = `${r.height + 6}px`; };
            el.style.zIndex = to.closest('#settle') ? 210 : to.closest('#await') ? 340 : 150;
            el.style.transition = 'none';
            place(a && a.width ? a : b);
            el.classList.add('on');
            void el.offsetWidth;
            const ms = Math.round(Math.min(260, 110 + (a ? Math.hypot(b.left - a.left, b.top - a.top) : 0) * 0.12));
            const ease = `${ms}ms cubic-bezier(.2, .8, .2, 1)`;
            el.style.transition = `transform ${ease}, width ${ease}, height ${ease}, opacity .35s ease`;
            place(b);
            clearTimeout(this.timer);
            this.timer = setTimeout(() => el.classList.remove('on'), ms + 240);
        });
    },
    hide() { if (this.el) this.el.classList.remove('on'); }
};

// --- WHAT A LINE ADDED: when a line is finished (or removed) the total says by how much — "+ 14,500.00" rises
// beside it and the total ticks. A changed number is easy to miss; a change that is announced is not ---
function popTotal() {
    const g = lastBill ? lastBill.grand : 0, d = round2(g - shownGrand);
    shownGrand = g;
    if (Math.abs(d) < 0.005 || reduceMotion()) return;
    const tag = document.createElement('span');
    tag.className = 'delta-pop' + (d < 0 ? ' down' : '');
    tag.textContent = `${d > 0 ? '+' : '−'} ${formatNum(Math.abs(d))}`;
    document.querySelectorAll('.delta-pop').forEach(t => t.remove()); // the latest says it; two never overlap
    document.querySelector('.bb-total').appendChild(tag);
    setTimeout(() => tag.remove(), 1300);
    ping($('lbl-grand'), 'tick');
}

// Restartable one-shot highlight
function ping(el, cls = 'ping') {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), 1200);
}

// --- BILL COPY: the photo / PDF is kept with the bill and shown beside the entry ---
// Files live in this browser's IndexedDB (the preview has no server); the bill keeps their ids
const FileStore = {
    db: null,
    open() {
        if (!this.db) this.db = new Promise((resolve, reject) => {
            const req = indexedDB.open('elite-erp-files', 1);
            req.onupgradeneeded = () => req.result.createObjectStore('files');
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
        return this.db;
    },
    run(mode, fn) {
        return this.open().then(db => new Promise((resolve, reject) => {
            const tx = db.transaction('files', mode), req = fn(tx.objectStore('files'));
            tx.oncomplete = () => resolve(req.result);
            tx.onerror = () => reject(tx.error);
        }));
    },
    put(id, blob) { return this.run('readwrite', s => s.put(blob, id)); },
    get(id) { return this.run('readonly', s => s.get(id)); },
    del(id) { return this.run('readwrite', s => s.delete(id)); }
};

const view = { scale: 1, x: 0, y: 0, img: null };

function initBillCopy() {
    const input = $('file-input');
    $('btn-attach').addEventListener('click', () => attachments.length ? showViewer($('viewer').hidden) : input.click());
    input.addEventListener('change', () => { addAttachments([...input.files]); input.value = ''; });
    $('btn-addpage').addEventListener('click', () => input.click());
    $('btn-remove-file').addEventListener('click', removeAttachment);
    $('btn-hide-viewer').addEventListener('click', () => showViewer(false));
    $('btn-read').addEventListener('click', readBill);
    // Buttons and the photo never take the cursor away from the entry
    document.querySelectorAll('#viewer button, #btn-attach').forEach(b => b.addEventListener('mousedown', e => e.preventDefault()));

    // Drop a file anywhere, or paste a screenshot / WhatsApp image
    let depth = 0;
    const hasFiles = e => !!e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    document.addEventListener('dragenter', e => { if (!hasFiles(e)) return; depth++; $('drop-zone').hidden = false; });
    document.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; $('drop-zone').hidden = true; } });
    document.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener('drop', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth = 0;
        $('drop-zone').hidden = true;
        addAttachments([...e.dataTransfer.files]);
    });
    document.addEventListener('paste', e => {
        const cd = e.clipboardData;
        if (!cd) return;
        const files = [...cd.files];
        if (files.length) { e.preventDefault(); addAttachments(files); return; }
        const text = cd.getData('text');
        const qr = text && text.length > 80 ? decodeEInvoiceQR(text) : null;
        if (qr) { e.preventDefault(); applyEInvoice(qr); }
    });

    // The photo: scroll zooms around the pointer, drag moves it, double-click fits the width
    const stage = $('viewer-stage');
    stage.addEventListener('mousedown', e => e.preventDefault());
    stage.addEventListener('wheel', e => {
        if (!view.img) return;
        e.preventDefault();
        const r = stage.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
        const s = Math.min(Math.max(view.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15), 0.05), 12);
        view.x = cx - (cx - view.x) * s / view.scale;
        view.y = cy - (cy - view.y) * s / view.scale;
        view.scale = s;
        applyView();
    }, { passive: false });
    let drag = null;
    stage.addEventListener('pointerdown', e => {
        if (!view.img) return;
        drag = { x: e.clientX - view.x, y: e.clientY - view.y };
        stage.classList.add('dragging');
        stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener('pointermove', e => { if (drag) { view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; applyView(); } });
    const endDrag = () => { drag = null; stage.classList.remove('dragging'); };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
    stage.addEventListener('dblclick', fitImage);
}

function addAttachments(files) {
    const ok = files.filter(f => /^image\//.test(f.type) || f.type === 'application/pdf');
    if (ok.length < files.length) showToast('Only a photo or a PDF of the bill can be attached', true);
    let added = 0;
    ok.forEach(f => {
        if (f.size > MAX_FILE_MB * 1048576) return showToast(`${f.name} is bigger than ${MAX_FILE_MB} MB`, true);
        const a = { id: 'f' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7), name: f.name || 'bill.png', type: f.type, size: f.size, blob: f, url: URL.createObjectURL(f) };
        attachments.push(a);
        added++;
        FileStore.put(a.id, f).catch(() => showToast('Could not keep the file on this computer — it stays only until this page closes', true));
    });
    if (!added) return;
    viewerIdx = attachments.length - 1;
    showViewer(true);
    scheduleDraft();
    showToast(`Bill copy attached${attachments.length > 1 ? ` (${attachments.length} pages)` : ''} — kept with this bill`);
}

function renderAttachments() {
    const n = attachments.length;
    $('btn-attach').classList.toggle('has-files', n > 0);
    $('attach-label').textContent = n ? `Bill copy${n > 1 ? ` · ${n}` : ''}` : 'Attach bill';
    const tabs = $('viewer-tabs');
    tabs.innerHTML = '';
    if (n > 1) attachments.forEach((a, i) => {
        const t = document.createElement('button');
        t.type = 'button';
        t.tabIndex = -1;
        t.className = 'v-tab' + (i === viewerIdx ? ' on' : '');
        t.textContent = i + 1;
        t.title = a.name;
        t.addEventListener('mousedown', e => e.preventDefault());
        t.addEventListener('click', () => { viewerIdx = i; renderAttachments(); renderViewerStage(); });
        tabs.appendChild(t);
    });
}

function showViewer(on) {
    if (on && !attachments.length) return;
    $('viewer').hidden = !on;
    document.body.classList.toggle('viewer-open', on);
    renderAttachments();
    if (on) renderViewerStage();
    Suggest.reposition();
}

function renderViewerStage() {
    const stage = $('viewer-stage'), a = attachments[viewerIdx];
    stage.innerHTML = '';
    view.img = null;
    if (!a) return;
    if (a.type === 'application/pdf') {
        const frame = document.createElement('iframe');
        frame.src = a.url + '#view=FitH';
        frame.title = a.name;
        frame.tabIndex = -1;
        stage.appendChild(frame);
        return;
    }
    const img = document.createElement('img');
    img.alt = a.name;
    img.draggable = false;
    img.onload = fitImage;
    img.src = a.url;
    view.img = img;
    stage.appendChild(img);
}

function fitImage() {
    const img = view.img, stage = $('viewer-stage');
    if (!img || !img.naturalWidth || !stage.clientWidth) return;
    view.scale = stage.clientWidth / img.naturalWidth;
    view.x = 0;
    view.y = 0;
    applyView();
}

const applyView = () => { if (view.img) view.img.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`; };

function removeAttachment() {
    const a = attachments[viewerIdx];
    if (!a) return;
    URL.revokeObjectURL(a.url);
    FileStore.del(a.id).catch(() => { /* already gone */ });
    attachments.splice(viewerIdx, 1);
    viewerIdx = Math.max(0, Math.min(viewerIdx, attachments.length - 1));
    if (attachments.length) { renderAttachments(); renderViewerStage(); }
    else showViewer(false);
    scheduleDraft();
    showToast('Bill copy removed');
}

// Reading the bill (OCR / AI) is a separate service; this hands it the copies — nothing is read here yet
function readBill() {
    document.dispatchEvent(new CustomEvent('erp:read-bill', { detail: { files: attachments.map(a => a.blob) } }));
    showToast('Bill reading is not connected in this preview — the copy is kept with the bill', true);
}

// After save the copies stay (the saved bill points to them); a discarded bill takes its copies with it
function clearAttachments(deleteFiles) {
    attachments.forEach(a => { URL.revokeObjectURL(a.url); if (deleteFiles) FileStore.del(a.id).catch(() => { /* already gone */ }); });
    attachments = [];
    viewerIdx = 0;
    showViewer(false);
}

function restoreAttachments(list) {
    (list || []).forEach(meta => FileStore.get(meta.id).then(blob => {
        if (!blob) return;
        attachments.push({ ...meta, blob, url: URL.createObjectURL(blob) });
        renderAttachments();
    }).catch(() => { /* storage unavailable: the copy is lost with the draft */ }));
}

// --- SAVE & RESET ---
function saveInvoice({ ask = true } = {}) {
    if (isSaving) return;

    const supplier = $('inp-supplier'), billNo = $('inp-inv'), billDate = $('inp-date'), ours = $('inp-vdate');
    const party = getParty(), purNo = $('inp-purno');
    if (!purNo.value.trim()) updatePurchaseNo();
    if (savedInvoices().some(i => (i.purchaseNo || '').toLowerCase() === purNo.value.trim().toLowerCase())) return flagError(purNo, `Purchase No. ${purNo.value.trim()} is already used`);
    if (!party) return flagError(supplier, 'Select the supplier from the ledger');
    if (billNo.dataset.auto === '1') { billNo.value = ''; delete billNo.dataset.auto; }
    // Mandi / unregistered purchases often have no printed bill number
    if (!billNo.value.trim() && party.type !== 'unregistered') { prefillBillNo(); return flagError(billNo, 'Type the bill number printed on the bill'); }
    const dup = isDuplicateBill();
    if (dup) return flagError(billNo, dupText(dup));
    for (const el of [billDate, ours]) if (!commitDate(el)) return false;
    if (!ours.dataset.iso) return flagError(ours, 'Purchase date is required');
    if (!billDate.dataset.iso) setDate(billDate, dateOf(ours));
    if (!checkHeaderDates(billDate)) return false;
    if (!confirmOldBill(billDate)) return false; // Ctrl+S never skips the question

    const bill = updateTotals();
    const items = [];
    for (const line of bill.lines) {
        const w = line.wrapper;
        const name = w.querySelector('.item-name');
        const qty = w.querySelector('.item-qty'), rate = w.querySelector('.item-rate');
        const disc = w.querySelector('.item-disc'), amt = w.querySelector('.item-amt');
        if (!name.value.trim() && !itemRowHasData(w)) continue; // blank row, ignore
        if (!line.item) { openItems(); return flagError(name, 'Select the item from the item ledger'); }
        if (!commitUnitField(qty) || !commitUnitField(rate)) return false;
        const freeEl = w.querySelector('.item-free');
        if (freeOn() && freeEl.value.trim() && !commitFree(freeEl)) return false;
        if (isDiscOn() && num(disc) > 100) return flagError(disc, `Discount must be 0-100% (${line.item.name})`);
        const exp = w.querySelector('.item-exp'), mrpEl = w.querySelector('.item-mrp');
        if (expOn() && line.item.expiry && !commitExpiry(exp)) return false;
        // Free / scheme goods: rate typed as 0 with a quantity, or only a free qty on the line
        const fr = isService(line.item) ? { n: 0, unit: null, base: 0 } : freeCounted(w);
        const isFree = (rate.value !== '' && num(rate) === 0 && num(qty) > 0) || (fr.base > 0 && !(num(qty) > 0));
        if (!(num(amt) > 0) && !isFree) return flagError(amt, `Amount is missing for ${line.item.name}${isService(line.item) ? '' : ' (rate 0 = free item)'}`);
        const r3 = n => Math.round(n * 1000) / 1000;
        const base = isService(line.item) ? 0 : r3(qtyInBase(qty)), stock = r3(base + fr.base);
        const mrp = mrpOn() && line.item.mrp ? num(mrpEl) || null : null, mrpWas = +w.dataset.lastMrp || null;
        items.push({
            itemId: line.item.id, name: line.item.name, type: line.item.type || 'stock', hsn: line.item.hsn || line.item.sac,
            godown: document.body.classList.contains('show-godown') ? w.querySelector('.item-godown').value : null,
            qty: num(qty), unit: qty.dataset.unit || null, qtyInBase: base, baseUnit: isService(line.item) ? null : line.item.units[0].u,
            rate: num(rate), rateUnit: rate.dataset.unit || null,
            disc: isDiscOn() ? num(disc) : 0, amount: num(amt),
            taxable: line.taxable, gstRate: bill.regime === 'none' ? 0 : line.rate, cessRate: line.cessRate, tax: line.tax,
            // Its share of the charges that are for it (and their GST), and what it really cost
            charges: round2(line.charge), chargesTax: round2(line.chargeTax),
            // Free goods come into the store at no charge: the stock is billed + free, each one costs less
            free: fr.n || 0, freeUnit: fr.n ? fr.unit : null, freeInBase: r3(fr.base), stockQty: isService(line.item) ? 0 : stock,
            cost: line.cost, landedRate: stock > 0 ? round2(line.cost / stock) : null,
            // Expiry as typed from the bill / packet, or estimated from the shelf life (said so, for whoever reads it)
            batch: expOn() && line.item.batch ? w.querySelector('.item-batch').value.trim().toUpperCase() || null : null,
            expiry: expOn() && line.item.expiry ? exp.dataset.iso || null : null,
            expiryEstimated: expOn() && line.item.expiry && !!exp.dataset.iso && exp.dataset.auto === '1',
            mrp, mrpWas: mrp && mrpWas && Math.abs(mrp - mrpWas) >= 0.005 ? mrpWas : null,
            remark: w.querySelector('.remark-input').value.trim()
        });
    }
    if (items.length === 0) { openItems(); return flagError(document.querySelector('.item-name'), 'Add at least one line'); }

    const extras = [];
    for (const x of bill.extras) {
        const name = x.row.querySelector('.charge-name'), amt = x.row.querySelector('.charge-amt'), rate = x.row.querySelector('.charge-rate');
        if (!name.value.trim() && amt.value === '' && rate.value === '') continue;
        if (!x.ledger) return flagError(name, 'Select from the charge / discount ledger');
        if (!(x.value > 0)) return flagError(x.byRate ? rate : amt, `Enter the rate % or the amount for ${x.ledger.name}`);
        if (x.isDisc && x.byRate && x.rate > 100) return flagError(rate, 'Discount cannot exceed 100%');
        const lineNo = l => bill.lines.filter(b => b.item).indexOf(l) + 1;
        extras.push({
            ledgerId: x.ledger.id, name: x.ledger.name, type: x.ledger.ctype, stage: x.ledger.stage || null,
            method: x.byRate ? 'percent' : 'fixed', rate: x.byRate ? x.rate : null, base: x.byRate ? x.base : null,
            amount: x.value,
            // Which lines it is for, and how its GST was worked out ('items' = at those lines' rates): value and tax
            // per rate. A deposit is on no goods, carries no GST and is no cost (it is money that comes back)
            appliesTo: x.isDeposit ? null : x.all ? 'all' : x.on.map(l => ({ line: lineNo(l), itemId: l.item.id })),
            gst: bill.regime === 'none' || x.isDisc || x.isDeposit ? 'none' : x.how, tax: x.isDisc || x.isDeposit ? 0 : x.tax,
            split: x.split.map(g => ({ rate: g.rate, value: g.value, tax: g.tax })), inCost: !x.isDeposit
        });
    }
    // A discount bigger than what it is taken on is a typing slip, never a real bill
    const firstDisc = stage => bill.extras.find(x => x.isDisc && x.ledger.stage === stage);
    if (bill.discBeforeTyped > bill.itemsGross + 0.004) return flagError(firstDisc('before').row.querySelector('.charge-amt'), `Discount ${formatINR(bill.discBeforeTyped)} is more than the items ${formatINR(bill.itemsGross)}`);
    if (bill.gross < 0) return flagError(firstDisc('after').row.querySelector('.charge-amt'), 'Discount after tax is more than the bill');

    // An e-invoice tells us the paper's total: a gap is looked into, and saving goes to the likely line
    if (bill.match && !bill.match.ok) {
        const suspect = lastDiag.find(h => h.field);
        if (suspect) { goToSuspect(suspect); return false; }
        return flagError(document.querySelector('.item-name'), `The e-invoice total differs from this entry by ${formatINR(Math.abs(bill.match.diff))} — ${lastDiag.length ? lastDiag[0].text : 'check the lines against the bill'}`);
    }
    // Typed as printed (Alt+T): a GST head more than ₹1 off, or a round off above ₹1, is not rounding
    const badHead = Object.entries(bill.taxCheck).find(([, c]) => !c.ok);
    if (badHead) return flagError($('billbox').querySelector(`.bill-tax[data-head="${badHead[0]}"]`), `${badHead[0].toUpperCase()} on the bill differs from ours by ${formatINR(Math.abs(badHead[1].diff))} — a line's GST rate is probably different on the bill`);
    if (Math.abs(bill.roundOff) > 1) return flagError($('bb-round').querySelector('input'), 'Round off can be at most ₹1 — a bigger gap is a discount (kasar) or a slip in a line');

    // The payment is looked at once before the bill is saved
    if (!paymentSeen) {
        openSettle();
        showToast('First the payment — Enter on its last box, or Ctrl+S to save it as shown');
        return false;
    }
    const pay = collectPayments(bill);
    if (!pay) return false;
    const { payments, credit } = pay;

    const invoice = {
        purchaseNo: purNo.value.trim(), purchaseNoManual: purNo.dataset.auto !== '1', partyId: party.id, supplier: party.name, gstin: party.gstin,
        billNo: billNo.value.trim(), billDate: billDate.dataset.iso, purchaseDate: ours.dataset.iso, receivedDate: ours.dataset.iso,
        items, extras, taxRegime: bill.regime, ratesInclusive: bill.inclusive, itcClaimed: bill.itc ? round2(bill.gst + bill.cess) : 0,
        totals: {
            itemsGross: bill.itemsGross, discountBeforeTax: bill.discBefore, itemsTaxable: bill.itemsTaxable, charges: bill.charges,
            cgst: bill.cgst, sgst: bill.sgst, igst: bill.igst, cess: bill.cess,
            discountAfterTax: bill.discAfter, deposits: bill.deposits, roundOff: bill.roundOff, grandTotal: bill.grand
        },
        taxSummary: bill.groups.map(g => ({ ...g, taxable: round2(g.taxable), cgst: round2(g.cgst), sgst: round2(g.sgst), igst: round2(g.igst), cess: round2(g.cess) })),
        supplierBill: { total: einvoice ? einvoice.total : null, tax: { ...billTax } },
        einvoice, attachments: attachments.map(({ id, name, type, size }) => ({ id, name, type, size })),
        linkedPayments: payments.filter(p => p.linkedPaymentId).map(p => p.linkedPaymentId), payments, paid: bill.paid, credit,
        remarks: $('inp-remarks').value.trim(), savedAt: new Date().toISOString()
    };

    // Everything is in order: one last look before it is saved (not for Ctrl+S, which is never pressed by accident)
    if (ask) { openConfirm(invoice); return false; }

    // To the server: the voucher, the screen state (to reopen it for editing) and the bill copies, in one request
    isSaving = true;
    const form = new FormData();
    form.append('voucher', JSON.stringify(invoice));
    form.append('snapshot', JSON.stringify(snapshot()));
    attachments.forEach(a => form.append('attachments', a.blob, a.name));
    fetch(ZEN.saveUrl, { method: 'POST', body: form, credentials: 'same-origin', headers: { 'X-CSRFToken': ZEN.csrf, 'X-Requested-With': 'XMLHttpRequest' } })
        .then(r => r.json().catch(() => ({ success: false, error: `Server error (${r.status})` })))
        .then(res => {
            if (!res.success) { isSaving = false; return showToast(res.error || 'Could not save the bill', true); }
            invoice.purchaseNo = res.purchase_no || invoice.purchaseNo;
            // Reads that follow the saved bills see this one too; the server already took it from any earlier payment
            ZEN.saved.push({ ...invoice, payments: [], credit: null });
            (invoice.payments || []).filter(p => p.linkedPaymentId).forEach(p => {
                const w = UNLINKED_PAYMENTS.find(x => x.id === p.linkedPaymentId);
                if (w) w.amount = round2(w.amount - p.amount);
            });
            const party = getParty();
            if (party && invoice.credit) party.balance = round2((party.balance || 0) + invoice.credit.amount);
            if (res.next_purchase_no) ZEN.nextPurchaseNo = res.next_purchase_no;
            savedLinks = null; // what is left of each earlier payment is read again
            savedMrp = null;   // and the last MRP of each item
            clearDraft();
            clearAttachments(true);
            closeSettle(false);
            showSaveCard(invoice);
            // Editing a saved bill: back to the purchase register once it is saved
            if (ZEN.editing) { setTimeout(() => { window.location.href = res.redirect_url || ZEN.listUrl; }, 900); return; }
            setTimeout(() => { resetForm(); isSaving = false; }, 700);
        })
        .catch(() => { isSaving = false; showToast('Could not reach the server — the bill is kept as a draft, try Ctrl+S again', true); });
    return true;
}

function resetForm() {
    closeSettle(false);
    const sup = $('inp-supplier'), inv = $('inp-inv');
    sup.value = '';
    delete sup.dataset.id;
    inv.value = '';
    delete inv.dataset.auto;
    delete inv.dataset.last;
    $('inp-date').value = '';
    $('inp-date').dataset.iso = '';
    setDate($('inp-vdate'), today());
    prefillBillDate();
    ['items-body', 'charges-body', 'pay-body'].forEach(id => $(id).innerHTML = '');
    addItemRow();
    ['inp-due', 'inp-interest', 'inp-latefee', 'inp-remarks'].forEach(id => { $(id).value = ''; delete $(id).dataset.auto; });
    billTax = {};
    paymentSeen = false;
    einvoice = null;
    renderEInvoiceTag();
    clearAttachments(false);
    closeSections();
    onPartyChange();
    shownGrand = 0;
    $('inp-purno').dataset.auto = '1';
    updatePurchaseNo();
    ping($('inp-purno'), 'flip'); // the next number rolls in
    focusField(firstField());
}

// --- CONTEXT HINTS: what the focused field wants, shown in the key bar ---
const kb = s => `<kbd>${s}</kbd>`;
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const LIST_HINT = `type a letter · ${kb('Space')} full list`;

// The cost each line really comes to (share of charges, GST when it doesn't come back) is worked out and saved
// with the bill for food costing — it is not shown here: the operator copies the paper, the owner reads the cost
function hintFor(el) {
    if (!el || !el.classList) return '';
    const id = el.id, has = c => el.classList.contains(c);
    const item = rowItem(el);
    if (id === 'go') return `Go to another window — type ${kb('sale')} ${kb('receipt')} … · ${kb('Enter')} go · ${kb('Esc')} back · this bill stays as it is`;
    if (id === 'aw-q') return `Find it by supplier, amount or note · ${kb('↑')}${kb('↓')} choose · ${kb('Enter')} link it to this bill · ${kb('Esc')} back to the bill`;
    if (id === 'inp-purno') return 'Our purchase number — numbered automatically, type over it only if you keep your own series';
    if (id === 'inp-vdate') return `Our purchase date (goods received) · ${kb('t')} today ${kb('y')} yesterday ${kb('-2')} two days ago`;
    if (id === 'inp-supplier') return `Supplier name, Hindi name or GSTIN · ${kb('Space')} full list · an e-invoice QR can be scanned here`;
    if (id === 'inp-inv') return el.dataset.last ? `Last bill from this supplier: <b>${escapeHtml(el.dataset.last)}</b> — type the new number` : 'Bill number exactly as printed · a repeat bill is caught instantly';
    if (id === 'inp-date' && el.classList.contains('old-bill') && el.dataset.oldOk === el.dataset.iso) {
        const t = oldBillText(el);
        if (t) return `<b>${escapeHtml(t.long)}</b> · right? ${kb('Enter')} keeps it`;
    }
    if (id === 'inp-date') return `Any style: ${kb('10.1.26')} ${kb('10/01')} ${kb('10 jan')} · blank ${kb('Enter')} = our purchase date`;
    if (has('item-name')) {
        const pl = splitPowerLine(el.value);
        if (pl) return `${kb('Enter')} fills the whole line: <b>${escapeHtml(pl.name)}</b> · ${escapeHtml(pl.nums.join(' · '))}`;
        return `Anything on the bill — kitchen items, soap, an AC, CA fees · whole line: ${kb('atta 10k 1450')} · blank ${kb('Enter')} = lines done`;
    }
    if (has('item-godown')) return `Godown · ${LIST_HINT}`;
    if (has('item-qty')) {
        const free = freeOn() ? ` · billed + free: ${kb('10+1')}` : '';
        if (!item) return `Quantity · ${kb('Enter')} next`;
        const u = itemUnits(item);
        if (u.length === 1) return `Quantity in ${unitLabel(u[0].u)}${free} · ${kb('Enter')} next`;
        return `Qty + the unit on the bill: ${u.map(x => `${kb(`5${x.u[0]}`)} = 5 ${unitLabel(x.u)}`).join(' · ')}${free} · ${kb('Alt')}+${kb('↓')} shows the units`;
    }
    if (has('item-free')) {
        const u = item ? itemUnits(item) : [];
        return `Free qty on the bill (10 + ${kb('1')} free) — not charged, it lowers what each one costs${u.length > 1 ? ` · another unit: ${kb(`6${u[u.length - 1].u[0]}`)}` : ''} · blank ${kb('Enter')} = none`;
    }
    if (has('item-batch')) return `Batch no. as printed on the bill or packet · blank ${kb('Enter')} if it has none`;
    if (has('item-exp')) {
        const est = el.dataset.auto === '1' && item && item.life;
        return `${est ? `<b>Estimated from the ${escapeHtml(lifeText(item.life))} shelf life</b> — ${kb('Enter')} keeps it · ` : ''}the date as printed ${kb('15.11.26')} · month / year ${kb('11/26')} · or the shelf life ${kb('6m')} ${kb('45d')}`;
    }
    if (has('item-mrp')) {
        const was = +el.closest('.item-wrapper').dataset.lastMrp || 0;
        return `MRP per piece as printed${was ? ` — last time ₹${FMT.money(was)}, ${kb('Enter')} keeps it` : ''} · a price above it is flagged`;
    }
    if (has('item-rate')) return `${item && !isService(item) ? `Last rate ₹${FMT.money(item.lastRate)}/${unitLabel(item.units[0].u)} · ` : ''}the rate as printed — which unit it is per: the next box · no rate on the bill? ${kb('Del')} and type the amount`;
    if (has('item-per')) {
        const opts = perOptions(el.closest('.item-wrapper')).filter(o => o.amount);
        const guess = el.dataset.guess ? `<b>Taken per ${escapeHtml(el.value)} — the last price says so</b> · ` : '';
        return `${guess}What the rate is per${opts.length > 1 ? ` · ${opts.map(o => `per ${o.name} = ${FMT.money(o.amount)}`).join(' · ')} — the one that matches the bill` : ''} · ${kb('Space')} the units`;
    }
    if (has('item-disc')) return 'Line discount in %';
    if (has('item-gst')) return `GST rate from the item ledger · change only if the bill differs · ${LIST_HINT}`;
    if (has('item-amt')) return `${isService(item) ? 'The amount on the bill' : 'Amount as on the bill — typing it works out the rate'} · ${kb('Enter')} next line`;
    if (has('remark-input')) return `Note for this line · ${kb('Enter')} next line`;
    if (has('charge-name')) return `Freight, hamali, commission, discount… · whole line: ${kb('bhada 200')} · for some lines only: ${kb('→')} · blank ${kb('Enter')} = payment`;
    if (has('charge-on')) return `Which lines it is for: ${kb('Enter')} all · a line no. ${kb('1')} · several ${kb('1,3')} ${kb('1-3')} · or a name ${kb('atta')} · ${kb('Space')} the list`;
    if (has('charge-gst')) return `GST on it: ${kb('as items')} = at the rate of the lines it is for (the rule for a charge billed with the goods) · or the rate the bill prints on it`;
    if (has('charge-rate')) return `A rate is % of the lines it is for (${kb('2')} = 2%) · leave it blank for a fixed amount`;
    if (has('charge-amt')) return `Amount as on the bill · ${kb('Enter')} next line`;
    if (has('pay-ledger')) return `Paid from which cash / bank · blank ${kb('Enter')} = nothing paid, rest is udhaar`;
    if (has('pay-amt')) return 'Filled with what is left to pay · type to pay only part';
    if (has('pay-via')) return `${kb('U')} UPI ${kb('N')} NEFT ${kb('R')} RTGS ${kb('I')} IMPS ${kb('C')} Cheque`;
    if (has('pay-ref')) return el.closest('.pay-row').dataset.via === 'cheque' ? '6-digit cheque number' : 'Reference / UTR number (optional)';
    if (has('pay-date')) return `Cheque date · ${kb('+5')} for a post-dated cheque`;
    if (id === 'inp-due') return `Udhaar due in how many days (from the bill date) — the date is worked out · only a date on the bill? ${kb('→')} type it there`;
    if (id === 'inp-duedate') return `The due date, if the bill gives one — the days are worked out · ${kb('15.11')} ${kb('15/11/26')}`;
    if (id === 'btn-accept') return `${kb('Enter')} payment done · ${kb('⌫')} or ${kb('↑')} back to change it`;
    if (id === 'inp-interest') return 'Interest % a year, charged after the due date';
    if (id === 'inp-latefee') return 'One-time late fee in ₹ (optional)';
    if (id === 'inp-remarks') return `A note about this bill (optional) · ${kb('Enter')} saves the bill · ${kb('Alt+4')} payment again`;
    if (has('bill-round')) return `Round off as printed on the bill — ${kb('-0.40')} ${kb('0')} · up to ₹1 · ${kb('Esc')} back`;
    if (has('bill-tax')) return `Type it as printed on the bill · ±₹1 is their rounding · ${kb('Enter')} next · ${kb('Esc')} back`;
    return '';
}

function renderHint(el) {
    $('ctx-hint').innerHTML = hintFor(el) || '';
}

// --- POWER LINE: "atta 10k 1450" in the item box, "bhada 200" in the charge box fills the whole row ---
// Numbers start at the first token beginning with a digit — unless that token is part of the item's own name
// ("tomato ketchup 1kg 2 110", "cold drink 300ml 1 480")
function splitPowerLine(text, kind = 'item') {
    // "pepsi 10 + 1 265" = "pepsi 10+1 265": billed + free stays one number
    const tokens = text.trim().replace(/\s*\+\s*/g, '+').split(/\s+/);
    const first = tokens.findIndex(t => /^[\d.]/.test(t));
    if (first <= 0) return null;
    for (let j = tokens.length - 1; j > first; j--) {
        const name = tokens.slice(0, j).join(' ');
        const hit = searchLedger(kind, name)[0];
        if (!hit) continue;
        const words = hit.name.toLowerCase().split(/[\s()]+/);
        if (tokens.slice(first, j).every(t => words.includes(t.toLowerCase()))) return { name, nums: tokens.slice(j) };
    }
    return { name: tokens.slice(0, first).join(' '), nums: tokens.slice(first) };
}

// "bhada 200" / "commission 6" / "disc 2%" in the charge box: the charge line at once. An item's whole line
// ("aloo 2k 2200") typed here flies up into the item lines, filled
function chargeLine(el, pl) {
    const listed = Suggest.isOpenFor(el) ? Suggest.current() : null;
    const entry = listed && !listed.special ? listed : searchLedger('charge', pl.name)[0];
    Suggest.close();
    if (!entry) return flagError(el, `"${pl.name}" is not in the ledger — Alt+C to create it`);
    if (entry.kind === 'item') return teleportToItems(el, entry, pl);
    const row = el.closest('.charge-row');
    bindLedger(el, entry);
    setChargeValue(row, chargeValue(entry, pl.nums[0]));
    popTotal();
    focusField(nextChargeName(row));
}

function powerLine(el, pl, picked = null) {
    const listed = Suggest.isOpenFor(el) ? Suggest.current() : null;
    const entry = picked || (listed && !listed.special ? listed : searchLedger('item', pl.name)[0]);
    Suggest.close();
    if (!entry) return flagError(el, `"${pl.name}" is not in the item ledger — Alt+C to create it`);
    if (entry.kind === 'charge') return teleportToCharges(el, entry, pl.nums[0]);

    const w = el.closest('.item-wrapper');
    const qty = w.querySelector('.item-qty'), rate = w.querySelector('.item-rate'), amt = w.querySelector('.item-amt');
    bindLedger(el, entry);
    // An expense has just an amount: "ca fee 15000"
    if (isService(entry)) {
        amt.value = pl.nums[0];
        amt.dispatchEvent(new Event('input', { bubbles: true }));
        formatField(amt);
        updateTotals();
        return endOfItemRow(w);
    }
    const [qtyText, rateText, amtText] = pl.nums;
    qty.value = qtyText;
    if (rateText !== undefined) { rate.value = rateText; delete rate.dataset.auto; }
    // No unit typed on a two-unit item: the rest of the line is kept, the cursor waits on the qty
    if (!commitUnitField(qty)) { w.recalc(); updateTotals(); Suggest.showUnits(qty); return; }
    const free = w.querySelector('.item-free');
    if (free.value.trim() && !commitFree(free)) return;
    // A rate typed on the same line is per the qty's unit, as the bill prints it ("masala 10p 320" = ₹320 a packet)
    // — unless it names its own unit ("atta 10k 29/kg")
    if (rateText !== undefined && !/[a-z]/i.test(rateText) && qty.dataset.unit) rate.dataset.unit = qty.dataset.unit;
    if (rateText !== undefined && !commitUnitField(rate)) return;
    w.recalc();
    // …unless the item's last price says that number is per its other unit ("atta 50k 29" = ₹29 a Kg)
    if (rateText !== undefined && amtText === undefined) guessPer(w);
    if (amtText !== undefined) {
        amt.value = amtText;
        amt.dispatchEvent(new Event('input', { bubbles: true })); // works out the rate
        formatField(amt);
    }
    updateTotals();
    // Rate given → the line is done; only qty given → stop on the rate to confirm it
    if (rateText !== undefined) endOfItemRow(w);
    else focusField(rate);
}

// --- WHERE AM I: 1 header · 2 lines · 3 charges · 4 payment (popup) · 5 note ---
function stepOf(el) {
    if (!el || !el.closest) return 0;
    if (el.closest('header') || el.closest('.titlebar')) return 1;
    if (el.closest('#items-body')) return 2;
    if (el.closest('#zen-charges')) return 3;
    if (el.closest('.vfoot')) return 5;
    return 0;
}

function jumpToStep(n) {
    if (n === 4) return openSettle();
    closeSettle(false);
    if (n === 1) return focusField(firstField());
    if (n === 2) { openItems(); return focusField(firstEmptyItem()); }
    if (n === 3) return focusField(firstEmptyCharge());
    focusField($('inp-remarks'));
}

// --- DRAFT AUTOSAVE: survives refresh, crash and power cuts ---
function scheduleDraft() {
    if (restoring) return;
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 400);
}

function snapshot() {
    const v = (root, sel) => { const el = root.querySelector(sel); return el ? el.value : null; };
    const id = (root, sel) => { const el = root.querySelector(sel); return el ? el.dataset.id || null : null; };
    const inv = $('inp-inv');
    return {
        party: { id: $('inp-supplier').dataset.id || null, text: $('inp-supplier').value },
        billNo: inv.dataset.auto === '1' ? '' : inv.value, billDate: $('inp-date').dataset.iso || '', recv: $('inp-vdate').dataset.iso || '',
        purNo: $('inp-purno').dataset.auto === '1' ? null : $('inp-purno').value,
        items: [...document.querySelectorAll('.item-wrapper')].map(w => ({
            line: w.dataset.line, id: id(w, '.item-name'), name: v(w, '.item-name'), godown: id(w, '.item-godown'),
            qty: v(w, '.item-qty'), qtyUnit: w.querySelector('.item-qty').dataset.unit || null,
            rate: v(w, '.item-rate'), rateUnit: w.querySelector('.item-rate').dataset.unit || null,
            rateLocked: !!w.querySelector('.item-rate').dataset.unitLocked, rateAuto: w.querySelector('.item-rate').dataset.auto === '1',
            disc: v(w, '.item-disc'), free: v(w, '.item-free'), batch: v(w, '.item-batch'),
            exp: v(w, '.item-exp'), expAuto: w.querySelector('.item-exp').dataset.auto === '1',
            mrp: v(w, '.item-mrp'), mrpAuto: w.querySelector('.item-mrp').dataset.auto === '1',
            gst: id(w, '.item-gst'), amt: v(w, '.item-amt'), remark: v(w, '.remark-input')
        })),
        itemsOpen, chargesOpen,
        charges: [...document.querySelectorAll('.charge-row')].map(r => ({
            id: id(r, '.charge-name'), name: v(r, '.charge-name'), on: r.querySelector('.charge-on').dataset.on || '',
            gst: id(r, '.charge-gst'), rate: v(r, '.charge-rate'), amt: v(r, '.charge-amt'), fixedAmt: r.dataset.fixedAmt === '1'
        })),
        billTax: { ...billTax }, einvoice, paymentSeen, billDateAuto: $('inp-date').dataset.auto === '1',
        attachments: attachments.map(({ id, name, type, size }) => ({ id, name, type, size })),
        pays: [...document.querySelectorAll('.pay-row')].map(r => ({
            ledger: id(r, '.pay-ledger'), amt: v(r, '.pay-amt'), amtAuto: r.querySelector('.pay-amt').dataset.auto === '1',
            via: id(r, '.pay-via'), ref: v(r, '.pay-ref'), date: r.querySelector('.pay-date').dataset.iso || ''
        })),
        credit: ['inp-due', 'inp-interest', 'inp-latefee'].map(id => ({ id, value: $(id).value, auto: $(id).dataset.auto === '1' })),
        remarks: $('inp-remarks').value
    };
}

const isEmptySnapshot = s => !s.party.text && !s.billNo && !s.charges.length && !(s.attachments || []).length && s.items.every(i => !i.name && !i.qty && !i.amt);

function saveDraft() {
    const s = snapshot(), empty = isEmptySnapshot(s);
    try {
        if (empty) localStorage.removeItem(DRAFT_KEY);
        else localStorage.setItem(DRAFT_KEY, JSON.stringify(s));
        $('draft-state').textContent = empty ? '' : `● Draft saved ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
    } catch (err) { $('draft-state').textContent = ''; /* storage unavailable: no draft */ }
}

function clearDraft() {
    clearTimeout(draftTimer);
    try { localStorage.removeItem(DRAFT_KEY); } catch (err) { /* nothing to clear */ }
    $('draft-state').textContent = '';
}

function restoreDraft() {
    let s = null;
    try { s = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch (err) { /* corrupt or unavailable */ }
    if (!s || isEmptySnapshot(s)) return false;

    restoring = true;
    const sup = $('inp-supplier');
    sup.value = s.party.text;
    if (findLedger('party', s.party.id)) sup.dataset.id = s.party.id;
    $('inp-inv').value = s.billNo;
    if (s.billDate) { setDate($('inp-date'), fromIso(s.billDate)); if (s.billDateAuto) $('inp-date').dataset.auto = '1'; else delete $('inp-date').dataset.auto; }
    if (s.recv) setDate($('inp-vdate'), fromIso(s.recv));
    if (s.purNo) { $('inp-purno').value = s.purNo; delete $('inp-purno').dataset.auto; }

    $('items-body').innerHTML = '';
    s.items.forEach(it => {
        const w = addItemRow().closest('.item-wrapper');
        // The line keeps its id, so the charges on it stay on it
        if (it.line) { w.dataset.line = it.line; lineSeq = Math.max(lineSeq, parseInt(it.line.slice(1), 10) || 0); }
        const item = findLedger('item', it.id);
        if (item) bindLedger(w.querySelector('.item-name'), item);
        const set = (sel, val) => { if (val !== null && val !== undefined) w.querySelector(sel).value = val; };
        ['name', 'qty', 'free', 'rate', 'disc', 'amt', 'batch', 'exp', 'mrp'].forEach(k => set(`.item-${k}`, it[k]));
        // An estimate stays an estimate (it follows the bill date); a typed expiry / MRP stays typed
        const exp = w.querySelector('.item-exp'), mrp = w.querySelector('.item-mrp');
        if (it.expAuto) exp.dataset.auto = '1'; else delete exp.dataset.auto;
        if (it.mrpAuto) mrp.dataset.auto = '1'; else delete mrp.dataset.auto;
        if (it.exp && !it.expAuto) commitExpiry(exp, true);
        set('.remark-input', it.remark);
        [['.item-godown', it.godown], ['.item-gst', it.gst]].forEach(([sel, val]) => { if (val) setPick(w.querySelector(sel), val); });
        const qty = w.querySelector('.item-qty'), rate = w.querySelector('.item-rate');
        if (it.qtyUnit) qty.dataset.unit = it.qtyUnit;
        if (it.rateUnit) rate.dataset.unit = it.rateUnit;
        if (it.rateLocked) rate.dataset.unitLocked = '1';
        if (it.rateAuto) rate.dataset.auto = '1'; else delete rate.dataset.auto;
        delete w.querySelector('.item-amt').dataset.auto;
    });
    if (!s.items.length) addItemRow();

    if (s.itemsOpen || s.items.some(i => i.name)) openItems();
    if (s.chargesOpen) openCharges();
    (s.charges || []).forEach(c => {
        const row = addChargeRow(findLedger('charge', c.id));
        if (!c.id) row.querySelector('.charge-name').value = c.name;
        row.querySelector('.charge-rate').value = c.rate || '';
        row.querySelector('.charge-amt').value = c.amt || '';
        if (c.fixedAmt) row.dataset.fixedAmt = '1'; else delete row.dataset.fixedAmt;
        setOn(row.querySelector('.charge-on'), c.on || '');
        if (c.gst) setPick(row.querySelector('.charge-gst'), c.gst);
    });
    billTax = s.billTax || {};
    paymentSeen = !!s.paymentSeen;
    einvoice = s.einvoice || null;
    renderEInvoiceTag();
    (s.pays || []).forEach(p => {
        const row = addPayRow();
        const amt = row.querySelector('.pay-amt');
        amt.value = p.amt || '';
        if (p.amtAuto) amt.dataset.auto = '1';
        if (p.via) setPick(row.querySelector('.pay-via'), p.via);
        if (p.date) setDate(row.querySelector('.pay-date'), fromIso(p.date));
        row.querySelector('.pay-ref').value = p.ref || '';
        const ledger = findLedger('payledger', p.ledger);
        if (ledger) bindLedger(row.querySelector('.pay-ledger'), ledger);
    });

    onPartyChange();
    if (s.billNo) delete $('inp-inv').dataset.auto;
    (s.credit || []).forEach(c => {
        if (!$(c.id)) return; // a field from an older version
        $(c.id).value = c.value;
        if (c.auto) $(c.id).dataset.auto = '1';
        else delete $(c.id).dataset.auto;
    });
    $('inp-remarks').value = s.remarks || '';
    restoreAttachments(s.attachments);
    restoring = false;
    updatePurchaseNo();
    shownGrand = updateTotals().grand;
    return true;
}

// --- ROW DELETE & UNDO (Ctrl+Del / Ctrl+Z) ---
function removeRow(row, message) {
    const parent = row.parentElement, next = row.nextElementSibling;
    row.remove();
    if (row.classList.contains('item-wrapper') && !parent.querySelector('.item-wrapper')) addItemRow();
    lastRemoved = { row, parent, next, at: Date.now() };
    updateTotals();
    popTotal();
    showToast(message);
    return { parent, next };
}

function deleteRow(el) {
    const row = el.closest('.item-wrapper, .charge-row, .pay-row');
    const list = navList();
    const prevField = list[list.indexOf(row.querySelector('.nav-input')) - 1];
    const { parent, next } = removeRow(row, 'Row deleted · Ctrl+Z to undo');
    const neighbour = next || parent.lastElementChild;
    focusField((neighbour && neighbour.querySelector('.nav-input')) || prevField || $('inp-remarks'));
}

function undoDelete() {
    const { row, parent, next } = lastRemoved;
    lastRemoved = null;
    // Drop the blank placeholder that replaced a deleted only-item row
    if (row.classList.contains('item-wrapper') && parent.children.length === 1) {
        const only = parent.firstElementChild;
        if (!only.querySelector('.item-name').value && !itemRowHasData(only)) only.remove();
    }
    parent.insertBefore(row, next && next.parentElement === parent ? next : null);
    updateTotals();
    popTotal();
    focusField(row.querySelector('.nav-input'));
    showToast('Row restored');
}

// --- SAVE CARD & HELP ---
// How a bill is paid, in one line (the "Save?" check and the saved card say it the same way)
const payText = inv => inv.credit
    ? `${inv.paid ? `Paid ${formatINR(inv.paid)} · ` : ''}Udhaar ${formatINR(inv.credit.amount)} · due ${fmtDate(fromIso(inv.credit.dueDate))} (${inv.credit.dueDays} days)`
    : `Fully paid${inv.payments.length ? ` · ${inv.payments.map(payLabel).join(' + ')}` : ''}`;

// --- SAVE THIS BILL? Asked once, at the end (Enter on the Note, or Save on a touch screen). It shows what will be
// saved — supplier and bill, the total, how it is paid — so the last look checks the paper instead of being a
// habit click. Enter / Y saves · Esc / N goes back · ← → between the two buttons · the mouse works too ---
let focusBeforeConfirm = null;
function openConfirm(inv) {
    const n = inv.items.length;
    $('confirm-what').textContent = `${inv.supplier}${inv.billNo ? ` · Bill ${inv.billNo}` : ''} · ${fmtDate(fromIso(inv.billDate))} · ${n} line${n === 1 ? '' : 's'}`;
    $('confirm-total').textContent = formatINR(inv.totals.grandTotal);
    $('confirm-pay').textContent = payText(inv);
    focusBeforeConfirm = document.activeElement;
    Suggest.close();
    $('confirm').hidden = false;
    $('screen').inert = true;
    $('appbar').inert = true;
    $('btn-confirm-yes').focus();
}

function closeConfirm(back = true) {
    if ($('confirm').hidden) return;
    $('confirm').hidden = true;
    $('screen').inert = false;
    $('appbar').inert = false;
    const b = focusBeforeConfirm;
    focusBeforeConfirm = null;
    if (back) focusField(b && b.isConnected && b.offsetParent !== null ? b : $('inp-remarks'));
}

function confirmYes() { closeConfirm(false); saveInvoice({ ask: false }); }

function confirmKey(e) {
    const yes = $('btn-confirm-yes'), no = $('btn-confirm-no'), k = e.key;
    e.preventDefault();
    if (['Escape', 'n', 'N'].includes(k)) return closeConfirm();
    if (['y', 'Y'].includes(k)) return confirmYes();
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Tab'].includes(k)) return (document.activeElement === yes ? no : yes).focus();
    if (k === 'Enter' || k === ' ') return document.activeElement === no ? closeConfirm() : confirmYes();
}

function showSaveCard(inv) {
    const card = $('save-card');
    const n = inv.items.length;
    card.querySelector('.sc-no').textContent = `${inv.purchaseNo} SAVED`;
    card.querySelector('.sc-party').textContent = `${inv.supplier}${inv.billNo ? ` · Bill ${inv.billNo}` : ''} · ${n} line${n === 1 ? '' : 's'}`;
    card.querySelector('.sc-total').textContent = formatINR(inv.totals.grandTotal);
    card.querySelector('.sc-status').textContent = payText(inv);
    card.classList.remove('leaving');
    card.hidden = false;
    clearTimeout(saveCardTimer);
    saveCardTimer = setTimeout(hideSaveCard, 2000);
}

function hideSaveCard() {
    const card = $('save-card');
    if (card.hidden || card.classList.contains('leaving')) return;
    card.classList.add('leaving');
    setTimeout(() => { card.hidden = true; card.classList.remove('leaving'); }, 220);
}

function toggleHelp(force) {
    const help = $('help');
    const open = force !== undefined ? force : help.hidden;
    if (open && help.hidden) focusBeforeHelp = document.activeElement;
    help.hidden = !open;
    if (!open && focusBeforeHelp) {
        focusField(focusBeforeHelp);
        focusBeforeHelp = null;
    }
}

// Theme: Auto / Dark / Light, all three in sight (← → in Settings, or a click)
function setTheme(theme, save = true) {
    const ctl = $('cfg-theme');
    ctl.dataset.value = theme;
    ctl.querySelectorAll('[data-v]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.v === theme)));
    if (theme === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    if (save) try { localStorage.setItem(THEME_KEY, theme); } catch (err) { /* this session only */ }
}

// --- 100% KEYBOARD HIGHWAY ---
// Focusable fields in visual order: visible, enabled, not inside a hidden (inert) part, not a skipped voucher field
function navList() {
    return Array.from(document.querySelectorAll('.nav-input')).filter(el =>
        el.offsetParent !== null && !el.disabled && !el.closest('[inert]') && !el.closest('[hidden]')
        && !(el.classList.contains('vch-field') && document.body.classList.contains('skip-vch'))
    );
}

// Arriving by keyboard selects the field: typing replaces, Backspace keeps walking back
function focusField(el, scroll = true) {
    if (!el) return;
    navColumn = null; // any move but ↑ ↓ starts a new column
    el.focus({ preventScroll: !scroll });
    el.dataset.touched = '';
    if (el.tagName === 'INPUT' && el.select) el.select();
    // A bill no. with only the supplier's series in it: the cursor waits after it, to type the number
    if (el.id === 'inp-inv' && el.dataset.auto === '1') { el.setSelectionRange(el.value.length, el.value.length); el.dataset.touched = '1'; }
}

// --- ARROW KEYS, like a sheet: ← → along the line, ↑ ↓ straight up / down the column ---
// A box just arrived at (all selected) moves at once; a box being edited moves its caret first and leaves at its
// end. An open list keeps ↑ ↓ for itself. Moving never commits more than leaving a box with the mouse would
function arrowMove(el, key) {
    if (key === 'ArrowUp' || key === 'ArrowDown') return moveSpatial(el, key === 'ArrowDown' ? 1 : -1);
    const isInput = el.tagName === 'INPUT', len = isInput ? el.value.length : 0;
    const editing = isInput && el.dataset.touched === '1';
    if (editing && key === 'ArrowLeft' && !(el.selectionStart === 0 && el.selectionEnd === 0)) return false;
    if (editing && key === 'ArrowRight' && !(el.selectionStart === len && el.selectionEnd === len)) return false;
    const list = navList(), i = list.indexOf(el), next = list[i + (key === 'ArrowRight' ? 1 : -1)];
    if (i < 0) return false;
    if (!next) { bump(el, key === 'ArrowRight' ? 'x' : '-x'); return true; }
    focusField(next);
    return true;
}

// ↑ ↓ keep to the column the walk started in: only a box straight above / below (overlapping that column) counts,
// the nearest one wins. The column is kept through a wide box (the item name) and on — so ↑ ↑ ↑ from a rate climbs
// the rates and stops at the first line; it never turns into the header's Bill Date because that is the nearest box.
// Nothing straight there: the cursor stays and the box gives a small nudge (the key was heard; this is the edge).
// The payment popup is a short form, not a table: there the next row's nearest box is taken (Due date ↓ = Done)
function moveSpatial(el, dir) {
    const r = el.getBoundingClientRect(), cy = r.top + r.height / 2;
    const col = navColumn && navColumn.el === el ? navColumn : { left: r.left, right: r.right };
    const need = Math.min(12, (col.right - col.left) / 3);
    const all = navList().filter(x => x !== el).map(x => {
        const b = x.getBoundingClientRect();
        return { x, b, dy: (b.top + b.height / 2 - cy) * dir, ov: Math.min(b.right, col.right) - Math.max(b.left, col.left), dx: Math.abs(b.left + b.width / 2 - (col.left + col.right) / 2) };
    }).filter(c => c.dy > 6 && c.b.width > 0);
    let cands = all.filter(c => c.ov >= Math.min(need, c.b.width / 3));
    const form = !!el.closest('#settle');
    if (!cands.length && form) cands = all;
    if (!cands.length) { bump(el, dir > 0 ? 'y' : '-y'); return true; }
    const nearest = Math.min(...cands.map(c => c.dy));
    // In the nearest row: the most overlap with the column, then the left-most (a wide box → its first column below)
    const best = cands.filter(c => c.dy <= nearest + 12).sort((a, b) => (form ? a.dx - b.dx : b.ov - a.ov) || a.b.left - b.b.left)[0];
    focusField(best.x);
    // The column narrows to the box landed in (name → qty keeps to the qty's column from there on)
    const left = Math.max(col.left, best.b.left), right = Math.min(col.right, best.b.right);
    navColumn = right > left ? { el: best.x, left, right } : { el: best.x, left: best.b.left, right: best.b.right };
    return true;
}

// The edge: a 2-pixel nudge toward the key that was pressed, gone in a blink
function bump(el, dir) {
    if (reduceMotion()) return;
    el.classList.remove('bump-x', 'bump--x', 'bump-y', 'bump--y');
    void el.offsetWidth;
    el.classList.add(`bump-${dir}`);
    setTimeout(() => el.classList.remove(`bump-${dir}`), 200);
}

// Backspace on the payment's first box goes back to the bill, like Esc
function goPrev(el) {
    const list = navList(), i = list.indexOf(el);
    if (i > 0) focusField(list[i - 1]);
    else if (settleOpen) closeSettle();
}

// Enter visits every visible field, in order; the payment's last stop is its Done button
function goNext(el) {
    const list = navList();
    const next = list[list.indexOf(el) + 1];
    const payRow = el.closest('.pay-row');
    if (payRow && (!next || next.closest('.pay-row') !== payRow)) return endOfPayRow(payRow);
    if (settleOpen && (!next || !$('settle').contains(next))) return focusField($('btn-accept'));
    if (next) focusField(next);
}

// Move to the next existing item row, or create one at the end
function focusNextItemRow(wrapper) {
    popTotal();
    const next = wrapper.nextElementSibling;
    focusField(next && next.classList.contains('item-wrapper') ? next.querySelector('.item-name') : addItemRow());
}

// After the amount: batch / expiry / MRP when the item asks for them, then the remark (if shown), then the next line
function endOfItemRow(wrapper, from = null) {
    popTotal();
    const after = ['.item-batch', '.item-exp', '.item-mrp', '.remark-input'].map(s => wrapper.querySelector(s)).filter(el => el.offsetParent !== null);
    const next = after[from ? after.indexOf(from) + 1 : 0];
    if (next) focusField(next);
    else focusNextItemRow(wrapper);
}

function endOfPayRow(row) {
    const bill = updateTotals();
    if (bill.balance > 0.004) {
        const next = row.nextElementSibling || addPayRow();
        focusField(next.querySelector('.pay-ledger'));
    } else afterPayments();
}

// Nothing more paid: if something is left it is udhaar — the cursor goes to its due; fully paid → Done
function afterPayments() {
    const bill = updateTotals();
    if (bill.balance > 0.004) focusField($('inp-due'));
    else focusField($('btn-accept'));
}

// Going back up closes the parts below that were opened but never used, so the screen stays clean
function collapseUnused(step) {
    if (!step) return;
    const chargesUsed = () => [...document.querySelectorAll('.charge-row')].some(r => r.querySelector('.charge-name').value.trim() || r.querySelector('.charge-amt').value);
    const itemsUsed = () => [...document.querySelectorAll('.item-wrapper')].some(w => w.querySelector('.item-name').value.trim() || itemRowHasData(w));
    let changed = false;
    if (step <= 2 && chargesOpen && !chargesUsed()) {
        $('charges-body').innerHTML = '';
        chargesOpen = false; setSection('zen-charges', false); changed = true;
    }
    if (step === 1 && itemsOpen && !chargesOpen && !itemsUsed()) {
        itemsOpen = false; setSection('items-sec', false); changed = true;
    }
    if (changed) updateTotals();
}

// Alt+N: a fresh bill. If something is entered, the first press asks, the second discards
let discardArmedAt = 0;
function newBill() {
    if (!isEmptySnapshot(snapshot()) && Date.now() - discardArmedAt > 4000) {
        discardArmedAt = Date.now();
        showToast('Press Alt+N again to discard this bill and start a new one', true);
        return;
    }
    discardArmedAt = 0;
    clearDraft();
    clearAttachments(true);
    resetForm();
    showToast('New bill ready');
}

const isAllSelected = el => el.selectionStart === 0 && el.selectionEnd === el.value.length;

document.addEventListener('keydown', (e) => {
    const activeEl = document.activeElement;

    // Any key dismisses the save card and goes straight into the next bill, and clears the last message
    hideSaveCard();
    if (!['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) hideToast();

    // The app bar (Ctrl+K and the Go box) comes first, in every window; the rest belongs to this bill
    if (shellKey(e)) return;
    if (currentWindow !== 'purchase') return;
    // The awaiting-bill drawer and "Save this bill?" own the keyboard while they are open
    if (awaitOpen()) { awaitKey(e); return; }
    if (!$('confirm').hidden) { confirmKey(e); return; }

    // Help overlay owns the keyboard while open
    if (e.key === 'F1') { e.preventDefault(); toggleHelp(); return; }
    if (!$('help').hidden) {
        e.preventDefault();
        if (e.key === 'Escape') toggleHelp(false);
        return;
    }
    if (e.key === 'F11') { e.preventDefault(); toggleFullScreen(); return; }
    if (e.key === 'F12') { e.preventDefault(); toggleSettings(); return; }
    // Settings own the keyboard while open
    if ($('settings-panel').classList.contains('open')) { e.preventDefault(); settingsKey(e); return; }

    if (e.altKey && /^[1-5]$/.test(e.key)) { e.preventDefault(); jumpToStep(+e.key); return; }
    if (e.altKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newBill(); return; }
    // Alt+B: the bill copy — attach one, or show / hide it
    if (e.altKey && e.key.toLowerCase() === 'b') { e.preventDefault(); attachments.length ? showViewer($('viewer').hidden) : $('file-input').click(); return; }
    if (e.ctrlKey && e.key === 'Delete' && activeEl.closest && activeEl.closest('.item-wrapper, .charge-row, .pay-row')) {
        e.preventDefault();
        deleteRow(activeEl);
        return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && lastRemoved && Date.now() - lastRemoved.at < 15000) {
        e.preventDefault();
        undoDelete();
        return;
    }

    // Suggestion list keys
    const listField = activeEl.dataset && (activeEl.dataset.ledger || activeEl.dataset.pick);
    if (Suggest.isOpenFor(activeEl)) {
        if (e.key === 'ArrowDown') { e.preventDefault(); Suggest.move(1); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); Suggest.move(-1); return; }
        if (e.key === 'Escape') { e.preventDefault(); Suggest.close(); return; }
    } else if (activeEl.classList.contains('item-qty') && e.key === 'ArrowDown' && e.altKey && itemUnits(rowItem(activeEl)).length > 1) {
        e.preventDefault();
        Suggest.showUnits(activeEl, true);
        return;
    } else if (listField && ((e.key === 'ArrowDown' && e.altKey) || (e.key === ' ' && (activeEl.value === '' || isAllSelected(activeEl))))) {
        // Space on an untouched / empty list field, or Alt+↓, shows the full list (plain ↓ moves to the box below)
        e.preventDefault();
        Suggest.show(activeEl, '');
        if (Suggest.idx < 0) {
            Suggest.idx = Math.max(Suggest.items.findIndex(o => !o.special), 0);
            Suggest.render();
        }
        return;
    }

    if (e.altKey && e.key.toLowerCase() === 'c' && listField && CREATABLE.includes(listField)) { e.preventDefault(); createNew(activeEl); return; }
    // Ctrl+S (or Ctrl+A) saves from anywhere — a payment not looked at yet is shown first. A shortcut can't be pressed
    // by accident in the flow, so it doesn't ask "Save?" (an Enter at the end, or a tap, does)
    if ((e.ctrlKey || e.metaKey) && ['s', 'a'].includes(e.key.toLowerCase())) { e.preventDefault(); saveInvoice({ ask: false }); return; }
    if (e.altKey && e.key.toLowerCase() === 't') { e.preventDefault(); if (!settleOpen) focusTaxCheck(); return; }

    // Bill box figures typed as printed (Alt+T): GST heads, then the round off
    if (activeEl.classList.contains('bill-tax')) {
        const boxes = [...document.querySelectorAll('.bill-tax')].filter(b => !b.closest('[hidden]')), i = boxes.indexOf(activeEl);
        if (e.key === 'Escape') { leaveTaxCheck(); return; }
        if (e.key === 'Enter') { e.preventDefault(); formatField(activeEl); i < boxes.length - 1 ? focusField(boxes[i + 1]) : leaveTaxCheck(); updateTotals(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const to = boxes[i + (e.key === 'ArrowDown' ? 1 : -1)]; if (to) { formatField(activeEl); focusField(to); updateTotals(); } return; }
        if (e.key === 'Backspace' && (!activeEl.dataset.touched || activeEl.value === '') && i > 0) { e.preventDefault(); focusField(boxes[i - 1]); return; }
        return;
    }

    if (e.key === 'Escape' && settleOpen) { e.preventDefault(); closeSettle(); return; }
    if (e.key === 'Escape' && !$('viewer').hidden) { showViewer(false); return; }
    if (!activeEl.classList.contains('nav-input')) return;

    // Arrows move between boxes (an open list keeps ↑ ↓, and ← → for the typing in it)
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key) && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        if (!Suggest.isOpenFor(activeEl) && arrowMove(activeEl, e.key)) { e.preventDefault(); return; }
        activeEl.dataset.touched = '1'; // the caret moved inside the box: it is being edited now
        return;
    }
    // F2 (or Home / End): edit inside the box — the caret goes in and the arrows move it
    if (e.key === 'F2' && activeEl.setSelectionRange) { e.preventDefault(); activeEl.dataset.touched = '1'; activeEl.setSelectionRange(activeEl.value.length, activeEl.value.length); return; }
    if (['Home', 'End'].includes(e.key)) activeEl.dataset.touched = '1';

    // Backspace: delete while editing; on an untouched or empty field, step back without deleting anything
    if (e.key === 'Backspace' && !e.ctrlKey && !e.altKey && !e.shiftKey) {
        const editable = activeEl.tagName === 'INPUT' && !activeEl.readOnly;
        if (!editable || !activeEl.dataset.touched || activeEl.value === '') {
            e.preventDefault();
            Suggest.close();
            goPrev(activeEl);
        }
        return;
    }

    if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return; // ignore IME (Hindi) composition
    e.preventDefault();
    handleEnter(activeEl);
});

function handleEnter(activeEl) {
    if (BOXES[activeEl.id]) return boxEnter(activeEl); // a row of the Go / to-link list clicked or tapped
    if (activeEl.id === 'btn-accept') return confirmPayment();
    // An e-invoice QR from a scanner arrives as typed text + Enter, in whatever box has the cursor
    const scanned = activeEl.value.trim();
    if (scanned.length > 80 && !activeEl.dataset.units) {
        const qr = decodeEInvoiceQR(scanned);
        if (qr) { activeEl.value = ''; return applyEInvoice(qr); }
    }
    if (!activeEl.dataset.units) formatField(activeEl); // qty / rate are committed with their unit further down
    let raw = activeEl.value.trim();
    const kind = activeEl.dataset.ledger;

    // 0a. List heads chosen: Create / End of List
    const highlighted = Suggest.isOpenFor(activeEl) ? Suggest.current() : null;
    if (highlighted && highlighted.special) {
        Suggest.close();
        return highlighted.special === 'end' ? endOfList(activeEl) : createNew(activeEl);
    }

    // 0b. Whole line in the item / charge box: "atta 10k 1450", "bhada 200"
    if (CROSS[kind]) {
        const pl = splitPowerLine(raw, kind);
        if (pl) return kind === 'item' ? powerLine(activeEl, pl) : chargeLine(activeEl, pl);
    }

    // Which lines a charge is for → then its rate
    if (activeEl.classList.contains('charge-on')) {
        if (!commitOn(activeEl, highlighted)) return;
        return focusField(activeEl.closest('.charge-row').querySelector('.charge-rate'));
    }

    // 0c. Ledger & pick-list fields: what is typed — or the row picked with ↓↑ in the open list — must resolve to an entry
    if ((kind || activeEl.dataset.pick) && (raw !== '' || highlighted)) {
        const entry = resolveField(activeEl);
        Suggest.close();
        if (!entry) return flagError(activeEl, kind ? `"${raw}" is not in the ${KIND_LABEL[kind].toLowerCase()} ledger — Alt+C to create it` : `"${raw}" is not in the list — Space shows all`);

        // A line of the other section (a charge among the items, an item among the charges) moves to its own place
        if (CROSS[kind] && entry.kind === CROSS[kind]) return kind === 'item' ? teleportToCharges(activeEl, entry) : teleportToItems(activeEl, entry);
        bindField(activeEl, entry);
        raw = activeEl.value.trim();
    } else if (activeEl.dataset.pick) {
        Suggest.show(activeEl, ''); // nothing chosen yet: show the choices
        return;
    }

    // 1. Dates: blank = sensible default, anything else is parsed
    if (activeEl.classList.contains('date-input')) {
        if (!raw) setDate(activeEl, activeEl.id === 'inp-date' ? dateOf($('inp-vdate')) || today() : today());
        if (!commitDate(activeEl)) return;
        if ((activeEl.id === 'inp-date' || activeEl.id === 'inp-vdate') && !checkHeaderDates(activeEl)) return;
    }

    // 2. Header, in the order the eye reads the paper: supplier → bill no. → bill date → lines
    if (activeEl.id === 'inp-supplier') {
        if (!getParty()) return flagError(activeEl, 'Type the supplier name — Space shows the full list');
        const dup = isDuplicateBill();
        if (dup) return flagError($('inp-inv'), dupText(dup));
        return focusField($('inp-inv'));
    }
    if (activeEl.id === 'inp-inv') {
        const party = getParty(), onlySeries = activeEl.dataset.auto === '1';
        if ((!raw || onlySeries) && party && party.type !== 'unregistered') return flagError(activeEl, 'Type the bill number printed on the bill');
        if (onlySeries) { activeEl.value = ''; delete activeEl.dataset.auto; }
        const dup = isDuplicateBill();
        if (dup) return flagError(activeEl, dupText(dup));
        return focusField($('inp-date'));
    }
    if (activeEl.id === 'inp-date') {
        if (!confirmOldBill(activeEl)) return;
        openItems();
        return focusField(firstEmptyItem());
    }

    // 3. Blank item name = lines done → charges & discount
    if (activeEl.classList.contains('item-name') && raw === '') {
        const wrapper = activeEl.closest('.item-wrapper');
        // Don't silently destroy a row that already has qty/rate/amount
        if (itemRowHasData(wrapper)) return flagError(activeEl, 'Enter an item name, or clear the row first');
        if (document.querySelectorAll('.item-wrapper').length > 1) removeItemRow(wrapper);
        focusField(firstEmptyCharge());
        return;
    }

    // 4. Blank charge name = charges done → payment
    if (activeEl.classList.contains('charge-name') && raw === '') {
        const row = activeEl.closest('.charge-row');
        if (row.querySelector('.charge-amt').value !== '') return flagError(activeEl, 'Choose a ledger, or clear the amount');
        row.remove();
        updateTotals();
        return openSettle();
    }

    // 6. Blank payment ledger (list closed with Esc) = done paying, same as End of List
    if (activeEl.classList.contains('pay-ledger') && raw === '') return endOfList(activeEl);

    // Qty: a unit picked from the list (↓) goes into the box with the number
    if (activeEl.classList.contains('item-qty') && Suggest.isOpenFor(activeEl)) {
        const opt = Suggest.current();
        if (opt) { Suggest.close(); activeEl.value = `${parseNum(activeEl.value) || 1} ${opt.id}`; }
    }
    // Qty / rate with a unit ("15kg") must make sense for this item before moving on;
    // a qty with no unit keeps its unit list open so the answer is one letter or ↓ away
    const billedFree = activeEl.classList.contains('item-qty') && activeEl.value.includes('+');
    if (activeEl.dataset.units && !commitUnitField(activeEl)) {
        if (activeEl.classList.contains('item-qty')) Suggest.showUnits(activeEl);
        return;
    }
    if (Suggest.isOpenFor(activeEl)) Suggest.close();
    // "10+1": the free qty is in — past the Free box, to the rate
    if (billedFree) {
        const w = activeEl.closest('.item-wrapper');
        if (!commitFree(w.querySelector('.item-free'))) return;
        return focusField(w.querySelector('.item-rate'));
    }
    if (activeEl.classList.contains('item-free') && !commitFree(activeEl)) return;
    // A rate that only makes sense per another unit (29 for a Katta of atta) is taken per that unit — the per box is next
    if (activeEl.classList.contains('item-rate') && guessPer(activeEl.closest('.item-wrapper'))) ping(activeEl.closest('.item-wrapper').querySelector('.item-per'), 'guessed');

    // 7. Udhaar due: the days (the date box is worked out and skipped) or the date (the days are worked out) → Done
    if (activeEl.id === 'inp-due') {
        if (!commitDue()) return;
        return goNext($('inp-duedate'));
    }
    if (activeEl.id === 'inp-duedate') {
        if (!commitDueDate()) return;
        return goNext(activeEl);
    }

    // 8. The bill's note → save the bill
    if (activeEl.id === 'inp-remarks') { saveInvoice(); return; }

    // Batch / expiry / MRP under the line (only items that ask): Enter goes on along the line
    if (activeEl.classList.contains('item-batch')) { activeEl.value = activeEl.value.trim().toUpperCase(); return endOfItemRow(activeEl.closest('.item-wrapper'), activeEl); }
    if (activeEl.classList.contains('item-exp')) { if (!commitExpiry(activeEl)) return; return endOfItemRow(activeEl.closest('.item-wrapper'), activeEl); }
    if (activeEl.classList.contains('item-mrp')) return endOfItemRow(activeEl.closest('.item-wrapper'), activeEl);

    // 9. End of an item row: Amount -> Expiry / MRP / Remark (if shown) -> next row
    if (activeEl.classList.contains('item-amt')) { endOfItemRow(activeEl.closest('.item-wrapper')); return; }
    if (activeEl.classList.contains('remark-input')) { focusNextItemRow(activeEl.closest('.item-wrapper')); return; }

    // End of a charge row: next charge row, or a new one
    if (activeEl.classList.contains('charge-amt')) {
        popTotal();
        const next = activeEl.closest('.charge-row').nextElementSibling;
        focusField((next || addChargeRow()).querySelector('.charge-name'));
        return;
    }

    // A charge picked: straight to its rate — it is for all the lines unless told otherwise (→ to the "on" box)
    if (activeEl.classList.contains('charge-name')) return focusField(activeEl.closest('.charge-row').querySelector('.charge-rate'));

    // 10. Standard flow
    goNext(activeEl);
}
