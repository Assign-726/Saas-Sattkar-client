// Mac keyboard support. Loaded before the voucher's own scripts; does nothing on Windows / Linux.
//  - Option (⌥) works as Alt: on a Mac, Option+N types "˜" instead of "n", so the shortcut checks
//    (e.key === 'n') never match. The key is re-read from the physical key (e.code) instead.
//  - Command (⌘) works as Ctrl: Cmd+S, Cmd+K, Cmd+Z ... and Cmd+Backspace stands for Ctrl+Delete.
//  - The on-screen hints say ⌥ / ⌘ instead of Alt / Ctrl.
(function () {
    const isMac = /Mac|iPhone|iPad|iPod/i.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent);
    if (!isMac) return;
    window.IS_MAC = true;

    const set = (e, prop, value) => { try { Object.defineProperty(e, prop, { value, configurable: true }); } catch (_) { /* read-only: leave as is */ } };

    // Registered first, in the capture phase, so every later keydown listener sees the normalised event
    window.addEventListener('keydown', e => {
        if (e.altKey && e.code) {
            const m = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(e.code);
            if (m) set(e, 'key', m[1] ? (e.shiftKey ? m[1] : m[1].toLowerCase()) : m[2]);
        }
        if (e.metaKey && !e.ctrlKey) {
            set(e, 'ctrlKey', true);
            if (e.key === 'Backspace') set(e, 'key', 'Delete');
        }
    }, true);

    // Hints: "Alt" → ⌥, "Ctrl" → ⌘, in the page and in anything added later (status bar, toasts, lists)
    const WORDS = /\b(Alt|Ctrl)\b/g, SWAP = { Alt: '⌥', Ctrl: '⌘' };
    const fix = node => {
        if (node.nodeType === 3) {
            if (WORDS.test(node.nodeValue)) node.nodeValue = node.nodeValue.replace(WORDS, w => SWAP[w]);
            WORDS.lastIndex = 0;
            return;
        }
        if (node.nodeType !== 1 || /^(SCRIPT|STYLE|INPUT|TEXTAREA)$/.test(node.nodeName)) return;
        if (node.title) node.title = node.title.replace(WORDS, w => SWAP[w]);
        node.childNodes.forEach(fix);
    };
    const start = () => {
        fix(document.body);
        new MutationObserver(list => list.forEach(m => {
            if (m.type === 'characterData') fix(m.target);
            else m.addedNodes.forEach(fix);
        })).observe(document.body, { childList: true, subtree: true, characterData: true });
    };
    document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();
