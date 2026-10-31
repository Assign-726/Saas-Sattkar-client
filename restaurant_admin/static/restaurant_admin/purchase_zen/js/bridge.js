// --- BRIDGE: ties the Elite Zen voucher (app.js + shell.js, unchanged in how they work) to the restaurant admin ---

// Go (Ctrl+K): the windows that exist in the admin open there; the purchase register is one more window, so the
// keyboard way back to the list is the same box. Windows not built yet stay as the prototype shows them
WINDOWS.push({ id: 'register', name: 'Purchase Register', alias: 'list back admin dashboard purchases kharid' });
const shellSwitchTo = switchTo;
// eslint-disable-next-line no-global-assign
switchTo = function (id, back = null) {
    const url = ZEN.windows && ZEN.windows[id];
    if (url && id !== 'purchase') {
        // The draft is saved first, so coming back to Purchase brings the bill back as it was
        if (!isEmptySnapshot(snapshot())) saveDraft();
        window.location.href = url;
        return;
    }
    return shellSwitchTo(id, back);
};

// "Create" in a list: a supplier, charge / discount, godown or cash / bank ledger is made by the name typed, and
// picked straight away. Items have units, GST and HSN to set, so they open in the inventory master
document.addEventListener('erp:create-ledger', e => {
    const { kind, name, field } = e.detail;
    if (kind === 'item') { window.open(ZEN.itemCreateUrl, '_blank', 'noopener'); return; }
    if (!['party', 'charge', 'godown', 'payledger'].includes(kind)) return;
    if (!name) return showToast('Type the name first, then Create', true);
    fetch(ZEN.createUrl, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': ZEN.csrf, 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ kind, name })
    }).then(r => r.json()).then(res => {
        if (!res.success) return showToast(res.error || 'Could not create it', true);
        const entry = { ...res.entry, kind: res.kind };
        if (!LEDGER[res.kind].some(x => x.id === entry.id)) LEDGER[res.kind].push(entry);
        if (kind === 'godown') setPick(field, entry.id);
        else bindLedger(field, entry);
        updateTotals();
        showToast(`${KIND_LABEL[kind]} "${entry.name}" created${kind === 'party' ? ' — set its GSTIN & credit days in Parties' : ''}`);
        focusField(field);
    }).catch(() => showToast('Could not reach the server', true));
});

// Back to the admin: the half-typed bill is saved as the draft first, so it is there on coming back
document.getElementById('btn-back').addEventListener('click', () => { if (!isEmptySnapshot(snapshot())) saveDraft(); });

// "Unregistered · no GST" / "Other state · IGST" comes from the supplier's ledger (GSTIN, GST type, state). Clicking
// the tag opens the supplier, so a supplier who does charge GST can be given its GSTIN — then reload this bill
document.getElementById('party-info').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    const party = getParty();
    if (!chip || !party || !ZEN.partyUrl) return;
    window.open(ZEN.partyUrl.replace('/0/', `/${party.id.slice(1)}/`), '_blank', 'noopener');
    showToast('Set the GSTIN & GST type in the supplier, then reload this bill (the draft is kept)');
});
document.getElementById('party-info').title = 'Click to open the supplier and set its GSTIN / GST type';

// Admin menu drawer: the arrow on the left edge (or Alt+M) slides the admin sidebar in over the bill, so the rest of
// the admin is one click away from the full-screen voucher. The sidebar is the real one (same permissions, theme and
// groups), loaded once in the background so it opens at once. Opening a menu link keeps this bill as the draft.
(function () {
    const nav = document.getElementById('zen-nav'), frame = document.getElementById('zen-nav-frame');
    const handle = document.getElementById('zen-nav-handle'), backdrop = document.getElementById('zen-nav-backdrop');
    if (!nav || !frame || !handle) return;
    let returnFocus = null;
    const load = () => { if (!frame.getAttribute('src')) frame.setAttribute('src', nav.dataset.src); };
    const isOpen = () => nav.classList.contains('open');
    function openNav() {
        load();
        returnFocus = document.activeElement;
        Suggest.close();
        nav.classList.add('open');
        nav.setAttribute('aria-hidden', 'false');
        handle.setAttribute('aria-expanded', 'true');
        backdrop.hidden = false;
        document.body.classList.add('zen-nav-open');
        frame.focus();
    }
    function closeNav() {
        if (!isOpen()) return;
        nav.classList.remove('open');
        nav.setAttribute('aria-hidden', 'true');
        handle.setAttribute('aria-expanded', 'false');
        backdrop.hidden = true;
        document.body.classList.remove('zen-nav-open');
        if (returnFocus && returnFocus.isConnected && returnFocus !== document.body) returnFocus.focus();
    }
    const keepDraft = () => { if (!isEmptySnapshot(snapshot())) saveDraft(); };

    handle.addEventListener('mousedown', e => e.preventDefault()); // the field being typed in keeps the cursor
    handle.addEventListener('click', openNav);
    document.getElementById('zen-nav-close').addEventListener('click', closeNav);
    backdrop.addEventListener('click', closeNav);
    // Captured before the voucher's own keys: Esc closes the menu (not the bill's lists), Alt+M toggles it
    window.addEventListener('keydown', e => {
        if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'm') { e.preventDefault(); e.stopPropagation(); isOpen() ? closeNav() : openNav(); return; }
        if (e.key === 'Escape' && isOpen()) { e.preventDefault(); e.stopPropagation(); closeNav(); }
    }, true);
    // Inside the menu: a link or form leaving for another page saves the bill as the draft first; Esc / Alt+M close
    frame.addEventListener('load', () => {
        let doc;
        try { doc = frame.contentDocument; } catch (_) { return; }
        if (!doc) return;
        doc.addEventListener('click', e => { const a = e.target.closest && e.target.closest('a[href]'); if (a && !a.getAttribute('href').startsWith('#')) keepDraft(); }, true);
        doc.addEventListener('submit', keepDraft, true);
        doc.addEventListener('keydown', e => {
            if (e.key === 'Escape' || (e.altKey && e.key.toLowerCase() === 'm')) { e.preventDefault(); closeNav(); }
        });
    });
    // Loaded quietly once the bill is up, so the first open is instant
    const idle = window.requestIdleCallback || (fn => setTimeout(fn, 1500));
    window.addEventListener('load', () => idle(load, { timeout: 4000 }));
})();
