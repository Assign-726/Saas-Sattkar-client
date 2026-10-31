// --- LEDGER: the live masters of this restaurant / branch, sent by the server (purchase_create view) in the
// #zen-boot JSON block. Same shapes as the Elite Zen prototype, so app.js works on them unchanged ---
const ZEN = JSON.parse(document.getElementById('zen-boot').textContent);

// gstCredit comes from the company's GST setup: true = input GST comes back (18% restaurant), false = it is part of
// the cost (5% restaurant). It only changes the cost the system saves for each line — the screen is the same
const COMPANY = ZEN.company;
// Unit master: label + extra spellings the operator may type (prefixes like "k", "b" are matched per item)
const UNIT_MASTER = {
    kg: { label: 'Kg', alias: ['kgs', 'kilo'] },
    gm: { label: 'Gm', alias: ['g', 'gms', 'gram'] },
    ltr: { label: 'Ltr', alias: ['l', 'litre', 'liter'] },
    ml: { label: 'Ml', alias: [] },
    pcs: { label: 'Pcs', alias: ['pc', 'nag', 'piece', 'pieces'] },
    dozen: { label: 'Dozen', alias: ['dz', 'doz'] },
    box: { label: 'Box', alias: ['boxes'] },
    bag: { label: 'Bag', alias: ['bags'] },
    katta: { label: 'Katta', alias: ['katte'] },
    tin: { label: 'Tin', alias: ['tins'] },
    crate: { label: 'Crate', alias: ['crates'] },
    packet: { label: 'Packet', alias: ['pkt', 'pack'] },
    bundle: { label: 'Bundle', alias: ['gaddi'] },
    tray: { label: 'Tray', alias: ['trays'] },
    bottle: { label: 'Bottle', alias: ['btl', 'bottles'] },
};
// Any other unit used in this restaurant's item master is known too (label = as written in the master)
(ZEN.extraUnits || []).forEach(u => { if (!UNIT_MASTER[u.u]) UNIT_MASTER[u.u] = { label: u.label, alias: [] }; });
// GST rates in force since 22 Sep 2025 (56th GST Council): 5% and 18%, 40% for sin / luxury goods (aerated drinks),
// 0% for exempt goods. A rate still set on an item master (12 / 28) stays pickable so old masters keep working
const GST_RATES = [...new Set([0, 5, 18, 40, ...(ZEN.extraRates || [])])].sort((a, b) => a - b);

// type: regular | composition | unregistered · defaultPay: credit / cash · creditDays, interest (% p.a.), msme
// balance: positive = we owe them (Cr), negative = advance with them (Dr) · lastBillNo: their last bill number
const LEDGER = {
    party: ZEN.ledger.party,
    // units: first = stock unit (lastRate is per this unit), others = alternate units; f = stock units in one of it
    item: ZEN.ledger.item,
    // ctype: charge | discount | deposit · stage before / after tax · gst: 'items' | 'none' | a rate · rate: usual %
    charge: ZEN.ledger.charge,
    cash: ZEN.ledger.cash,
    bank: ZEN.ledger.bank,
    godown: ZEN.ledger.godown
};
Object.entries(LEDGER).forEach(([kind, list]) => list.forEach(e => e.kind = kind));

// Money given to a supplier before the bill was entered (VendorAdvance), with only what is still left to link
const UNLINKED_PAYMENTS = ZEN.unlinked;
