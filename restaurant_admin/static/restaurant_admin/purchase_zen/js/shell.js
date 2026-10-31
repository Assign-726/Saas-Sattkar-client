// --- APP SHELL: the bar over every window, with one box (Ctrl+K) to go to any window. It lives apart from the
// purchase bill (app.js) because every window — sale, receipt… — shares it ---

// The entry windows. The purchase bill is app.js; the others are built separately
const WINDOWS = [
    { id: 'purchase', name: 'Purchase', alias: 'bill kharid buy' },
    { id: 'sale', name: 'Sale (POS)', alias: 'pos billing bikri sell' },
    { id: 'receipt', name: 'Receipt', alias: 'money in jama' },
    { id: 'payment', name: 'Payment', alias: 'money out bhugtan pay' },
    { id: 'credit', name: 'Credit Note', alias: 'sales return' },
    { id: 'debit', name: 'Debit Note', alias: 'purchase return wapas' },
];
let currentWindow = 'purchase';
let recentWindows = [];      // most recent first: Ctrl+K, Enter = back to the last window (like Alt+Tab)
let purchaseFocus = null;    // where the cursor was in the bill while another window is open
const windowName = id => WINDOWS.find(w => w.id === id).name;

// --- PAYMENTS TO LINK (data): money given when the goods came, before the bill was entered. To recognise one, the
// operator needs the amount, when it was paid (day and time) and the note — nothing else is shown ---
const paidOn = w => addDays(today(), -w.daysAgo);
function paymentWhen(w) {
    const d = paidOn(w), [h, m] = w.time.split(':').map(Number), mon = MONTHS[d.getMonth()];
    return `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${mon[0].toUpperCase()}${mon.slice(1)} · ${h % 12 || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'am' : 'pm'}`;
}
// What is left of a payment after the bills already saved against it. The saved bills are read once (and again after
// each save) — not on every key, however many bills there are
let savedLinks = null;
const linkedBySaved = () => savedLinks || (savedLinks = savedInvoices().flatMap(i => i.payments || []).filter(p => p.linkedPaymentId)
    .reduce((m, p) => ({ ...m, [p.linkedPaymentId]: round2((m[p.linkedPaymentId] || 0) + p.amount) }), {}));
const leftToLink = w => round2(w.amount - (linkedBySaved()[w.id] || 0));
// Oldest first: the longer a payment waits for its bill, the more it needs attention
const paymentsToLink = (partyId = null) => UNLINKED_PAYMENTS.filter(w => (!partyId || w.partyId === partyId) && leftToLink(w) > 0.004)
    .sort((a, b) => b.daysAgo - a.daysAgo || a.time.localeCompare(b.time));
// As a "Paid from" choice in this supplier's payment
const linkEntry = w => ({
    id: w.id, kind: 'linked', group: 'Paid earlier',
    name: `${formatINR(leftToLink(w))} · ${paymentWhen(w)}`, label: `Paid ${paymentWhen(w)}`, meta: w.note,
    alias: `${Math.round(w.amount)} ${w.mode} ${w.note}`.toLowerCase()
});
const linkEntries = party => party ? paymentsToLink(party.id).map(linkEntry) : [];

// --- COMMAND BOX: a box that reads as a name until it is used — then it works like every list in the app (type,
// ↑ ↓, Enter, Esc). Go (Ctrl+K) in the app bar only goes to a window: a list never mixes places with pieces of work
// (payments waiting for their bill have a drawer of their own, Alt+L) ---
const BOXES = {
    go: { label: () => windowName(currentWindow), options: q => goOptions(q), pick: (o, back) => switchTo(o.id, back) }
};
const boxBack = {};          // where the cursor was when each box was opened

// The windows, most recent first; the purchase bill says when it has unsaved work
function goOptions(query) {
    const q = query.trim().toLowerCase(), draft = !isEmptySnapshot(snapshot());
    const rank = id => { const i = recentWindows.indexOf(id); return i < 0 ? 99 + WINDOWS.findIndex(w => w.id === id) : i; };
    return WINDOWS.filter(w => w.id !== currentWindow && (!q || w.name.toLowerCase().includes(q) || `${w.name} ${w.alias}`.toLowerCase().split(/[\s()]+/).some(x => x.startsWith(q))))
        .sort((a, b) => rank(a.id) - rank(b.id))
        .map(w => ({ id: w.id, kind: 'window', name: w.name, meta: w.id === 'purchase' && draft ? 'unsaved bill' : '' }));
}

function showBoxList(box) {
    Suggest.show(box, box.dataset.typed === '1' ? box.value : '');
    if (Suggest.idx < 0 && Suggest.items.length) { Suggest.idx = 0; Suggest.render(); }
}

// Unused, a box simply reads its name
function restoreBox(box) {
    box.value = BOXES[box.id].label();
    delete box.dataset.typed;
    if (Suggest.input === box) Suggest.close();
}

const usable = el => !!el && el.isConnected && el.offsetParent !== null && !el.closest('[inert]') && !BOXES[el.id];
// Where to carry on in the bill when there is nowhere better to go back to
const resumeField = () => itemsOpen ? firstEmptyItem() : firstField();

// Ctrl+K from anywhere: an open payment, "Save?", the drawer, settings or help close first (nothing in them is lost)
function openBox(id) {
    if (!$('help').hidden) toggleHelp(false);
    if (!$('confirm').hidden) closeConfirm(false);
    if (settleOpen) closeSettle(false);
    if (awaitOpen()) toggleAwait(false);
    toggleSettings(false);
    $(id).focus();
}

function boxLeave(box) {
    const back = boxBack[box.id];
    boxBack[box.id] = null;
    restoreBox(box);
    if (currentWindow !== 'purchase') return box.blur();
    focusField(usable(back) ? back : resumeField());
}

function boxEnter(box) {
    const pick = (Suggest.isOpenFor(box) && Suggest.current()) || BOXES[box.id].options(box.dataset.typed === '1' ? box.value : '')[0];
    Suggest.close();
    if (!pick) { showToast('Nothing matches', true); return showBoxList(box); }
    const back = boxBack[box.id];
    boxBack[box.id] = null;
    restoreBox(box);
    BOXES[box.id].pick(pick, back);
}

// Another window opens in place; the purchase bill stays exactly as it was, cursor and all
function switchTo(id, back = null) {
    if (id === currentWindow) return currentWindow === 'purchase' ? focusField(usable(back) ? back : resumeField()) : $('go').blur();
    if (currentWindow === 'purchase') { purchaseFocus = back; if (settleOpen) closeSettle(false); toggleSettings(false); }
    recentWindows = [currentWindow, ...recentWindows.filter(x => x !== currentWindow && x !== id)];
    currentWindow = id;
    document.body.dataset.window = id;
    document.title = id === 'purchase' ? 'Purchase Bill' : windowName(id);
    const away = id !== 'purchase';
    $('screen').hidden = away;
    $('other-window').hidden = !away;
    $('ow-name').textContent = away ? windowName(id) : '';
    restoreBox($('go'));
    if (away) return $('go').blur();
    const b = purchaseFocus;
    purchaseFocus = null;
    focusField(usable(b) ? b : resumeField());
}

// The app bar's keys, before any window's own: Ctrl+K and Alt+L anywhere; inside the Go box ↑ ↓ Enter Esc like every list
function shellKey(e) {
    const k = e.key.toLowerCase(), at = document.activeElement;
    if ((e.ctrlKey || e.metaKey) && !e.altKey && k === 'k') { e.preventDefault(); at.id === 'go' ? boxLeave(at) : openBox('go'); return true; }
    if (e.altKey && !e.ctrlKey && k === 'l' && currentWindow === 'purchase') { e.preventDefault(); toggleAwait(); return true; }
    if (!BOXES[at.id]) return false;
    if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); boxLeave(at); return true; }
    if (e.key === 'Enter') { e.preventDefault(); boxEnter(at); return true; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (Suggest.isOpenFor(at)) Suggest.move(e.key === 'ArrowDown' ? 1 : -1);
        else showBoxList(at);
        return true;
    }
    return false;
}

document.addEventListener('DOMContentLoaded', () => {
    Object.keys(BOXES).forEach(id => {
        const box = $(id);
        restoreBox(box);
        // Opening a box: its name is selected (typing replaces it) and its whole list shows at once
        box.addEventListener('focus', e => {
            if (!boxBack[id]) boxBack[id] = e.relatedTarget;
            delete box.dataset.typed;
            box.select();
            showBoxList(box);
        });
        box.addEventListener('input', () => { box.dataset.typed = '1'; showBoxList(box); });
        box.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== box) restoreBox(box); }, 0));
    });
    renderAwait();
});
