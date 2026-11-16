/*!
 * UIDialog — themed replacement for window.alert / confirm / prompt.
 *
 * JS API (all return Promises):
 *   await UIDialog.confirm('Delete this?', { title, confirmText, cancelText, tone })   -> true | false
 *   await UIDialog.alert('Saved.', { title, tone })                                    -> undefined
 *   await UIDialog.prompt('Reason?', { title, defaultValue, placeholder, required })   -> string | null
 *
 *   tone: 'danger' | 'warning' | 'primary' | 'success' | 'info'
 *   (confirm guesses 'danger' when the message mentions delete/remove/cancel/etc.)
 *
 * Declarative (no JS needed):
 *   <form data-confirm="Remove this rule?" ...>                 confirm before submit
 *   <button type="submit" data-confirm="Cancel this booking?">  confirm before this button submits
 *   <a href="..." data-confirm="Leave?">                        confirm before navigating
 *   Optional: data-confirm-title, data-confirm-ok, data-confirm-cancel, data-confirm-tone
 *
 *   <form data-prompt="Why skip it?" data-prompt-field="notes" data-prompt-default="Do not disturb">
 *   Optional: data-prompt-title, data-prompt-ok, data-prompt-placeholder, data-prompt-required
 *   (also works on a submit <button>, writing into the button's form)
 */
(function () {
    'use strict';
    if (window.UIDialog) return;

    var CSS = '' +
        '.uid-backdrop{position:fixed;inset:0;z-index:20000;display:flex;align-items:center;justify-content:center;padding:16px;' +
        'background:rgba(15,23,42,.45);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);opacity:0;transition:opacity .16s ease}' +
        '.uid-backdrop.uid-in{opacity:1}' +
        '.uid-box{width:100%;max-width:420px;background:var(--ui-surface,#fff);color:var(--ui-label,#0f172a);border-radius:18px;' +
        'border:1px solid var(--ui-separator,rgba(0,0,0,.08));box-shadow:0 24px 60px rgba(0,0,0,.28);padding:24px 24px 20px;text-align:center;' +
        'font-family:var(--ui-font,inherit);transform:translateY(8px) scale(.97);transition:transform .18s cubic-bezier(.2,.9,.3,1.2)}' +
        '.uid-backdrop.uid-in .uid-box{transform:none}' +
        '.uid-icon{width:56px;height:56px;border-radius:50%;margin:0 auto 14px;display:flex;align-items:center;justify-content:center;font-size:1.5rem}' +
        '.uid-title{font-size:1.08rem;font-weight:700;margin:0 0 6px;color:var(--ui-label,#0f172a)}' +
        '.uid-msg{font-size:.9rem;line-height:1.5;color:var(--ui-label-2,#475569);margin:0;white-space:pre-line;word-break:break-word}' +
        '.uid-input{width:100%;margin-top:14px;padding:.6rem .8rem;border-radius:10px;font-size:.9rem;font-family:inherit;' +
        'border:1px solid var(--ui-separator-strong,#cbd5e1);background:var(--ui-surface-2,#f8fafc);color:var(--ui-label,#0f172a);outline:none}' +
        '.uid-input:focus{border-color:var(--ui-accent,#2563eb);box-shadow:0 0 0 3px rgba(var(--ui-accent-rgb,37,99,235),.15)}' +
        '.uid-input.uid-err{border-color:var(--ui-red,#dc2626);box-shadow:0 0 0 3px rgba(var(--ui-red-rgb,220,38,38),.15)}' +
        '.uid-actions{display:flex;gap:10px;justify-content:center;margin-top:20px}' +
        '.uid-btn{flex:1;max-width:170px;padding:.6rem 1rem;border-radius:10px;font-weight:600;font-size:.9rem;cursor:pointer;' +
        'border:1px solid transparent;font-family:inherit;transition:filter .12s,background .12s}' +
        '.uid-btn:focus-visible{outline:2px solid var(--ui-accent,#2563eb);outline-offset:2px}' +
        '.uid-cancel{background:var(--ui-surface-2,#f1f5f9);border-color:var(--ui-separator,#e2e8f0);color:var(--ui-label-2,#475569)}' +
        '.uid-cancel:hover{background:var(--ui-surface-3,#e2e8f0)}' +
        '.uid-ok{color:#fff}.uid-ok:hover{filter:brightness(1.08)}' +
        '.uid-t-danger .uid-icon{background:rgba(var(--ui-red-rgb,220,38,38),.12);color:var(--ui-red-text,#dc2626)}' +
        '.uid-t-danger .uid-ok{background:var(--ui-red,#dc2626)}' +
        '.uid-t-warning .uid-icon{background:rgba(var(--ui-orange-rgb,234,88,12),.14);color:var(--ui-orange-text,#c2410c)}' +
        '.uid-t-warning .uid-ok{background:var(--ui-orange,#ea580c)}' +
        '.uid-t-primary .uid-icon,.uid-t-info .uid-icon{background:rgba(var(--ui-accent-rgb,37,99,235),.12);color:var(--ui-accent-text,#2563eb)}' +
        '.uid-t-primary .uid-ok,.uid-t-info .uid-ok{background:var(--ui-accent,#2563eb)}' +
        '.uid-t-success .uid-icon{background:rgba(var(--ui-green-rgb,22,163,74),.12);color:var(--ui-green-text,#16a34a)}' +
        '.uid-t-success .uid-ok{background:var(--ui-green,#16a34a)}' +
        '@media (prefers-reduced-motion:reduce){.uid-backdrop,.uid-box{transition:none}}';

    var ICONS = {
        danger: 'fa-solid fa-trash-can',
        warning: 'fa-solid fa-triangle-exclamation',
        primary: 'fa-solid fa-circle-question',
        info: 'fa-solid fa-circle-info',
        success: 'fa-solid fa-circle-check'
    };
    var WARN_RE = /\b(error|fail(ed)?|could ?not|cannot|can.t|denied|not found|maximum|max|too large|limit|locked|invalid|please)\b/i;
    var DANGER_RE = /\b(delet|remov|cancel|revers|discard|dispos|unlock|clear|reset|permanent|cannot be undone|can.t be undone)/i;

    function injectCss() {
        if (document.getElementById('uid-style')) return;
        var s = document.createElement('style');
        s.id = 'uid-style';
        s.textContent = CSS;
        document.head.appendChild(s);
    }

    // Only one dialog at a time; later calls wait their turn.
    var queue = Promise.resolve();

    function open(kind, message, opts) {
        opts = opts || {};
        var run = function () { return show(kind, message, opts); };
        var p = queue.then(run, run);
        queue = p.then(function () {}, function () {});
        return p;
    }

    function show(kind, message, opts) {
        injectCss();
        return new Promise(function (resolve) {
            var tone = opts.tone ||
                (kind === 'confirm' ? (DANGER_RE.test(message) ? 'danger' : 'primary') :
                 kind === 'prompt' ? 'primary' : (WARN_RE.test(message) ? 'warning' : 'info'));
            var title = opts.title ||
                (kind === 'alert' ? (tone === 'danger' || tone === 'warning' ? 'Heads up' : 'Notice') :
                 kind === 'prompt' ? 'Details needed' :
                 tone === 'danger' ? 'Are you sure?' : 'Please confirm');
            var okText = opts.confirmText || (kind === 'alert' ? 'OK' : kind === 'prompt' ? 'Continue' :
                tone === 'danger' ? 'Yes, continue' : 'Confirm');
            var cancelText = opts.cancelText || 'Cancel';

            var prevFocus = document.activeElement;
            var back = document.createElement('div');
            back.className = 'uid-backdrop uid-t-' + tone;
            var box = document.createElement('div');
            box.className = 'uid-box';
            box.setAttribute('role', kind === 'alert' ? 'alertdialog' : 'dialog');
            box.setAttribute('aria-modal', 'true');
            box.setAttribute('aria-labelledby', 'uid-title');
            box.setAttribute('aria-describedby', 'uid-msg');

            var icon = document.createElement('div');
            icon.className = 'uid-icon';
            icon.innerHTML = '<i class="' + (opts.icon || ICONS[tone] || ICONS.primary) + '" aria-hidden="true"></i>';
            var h = document.createElement('h5');
            h.className = 'uid-title'; h.id = 'uid-title'; h.textContent = title;
            var p = document.createElement('p');
            p.className = 'uid-msg'; p.id = 'uid-msg'; p.textContent = message == null ? '' : String(message);
            box.appendChild(icon); box.appendChild(h); box.appendChild(p);

            var input = null;
            if (kind === 'prompt') {
                input = document.createElement('input');
                input.type = 'text';
                input.className = 'uid-input';
                input.value = opts.defaultValue == null ? '' : String(opts.defaultValue);
                if (opts.placeholder) input.placeholder = opts.placeholder;
                input.setAttribute('aria-label', title);
                box.appendChild(input);
            }

            var actions = document.createElement('div');
            actions.className = 'uid-actions';
            var cancelBtn = null;
            if (kind !== 'alert') {
                cancelBtn = document.createElement('button');
                cancelBtn.type = 'button'; cancelBtn.className = 'uid-btn uid-cancel'; cancelBtn.textContent = cancelText;
                actions.appendChild(cancelBtn);
            }
            var okBtn = document.createElement('button');
            okBtn.type = 'button'; okBtn.className = 'uid-btn uid-ok'; okBtn.textContent = okText;
            actions.appendChild(okBtn);
            box.appendChild(actions);
            back.appendChild(box);
            document.body.appendChild(back);

            var done = false;
            function close(result) {
                if (done) return;
                done = true;
                document.removeEventListener('keydown', onKey, true);
                back.classList.remove('uid-in');
                setTimeout(function () { back.remove(); }, 160);
                try { if (prevFocus && prevFocus.focus) prevFocus.focus({ preventScroll: true }); } catch (e) {}
                resolve(result);
            }
            function accept() {
                if (kind === 'prompt') {
                    if (opts.required && !input.value.trim()) {
                        input.classList.add('uid-err'); input.focus(); return;
                    }
                    close(input.value);
                } else {
                    close(kind === 'confirm' ? true : undefined);
                }
            }
            function dismiss() { close(kind === 'confirm' ? false : kind === 'prompt' ? null : undefined); }

            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); dismiss(); }
                else if (e.key === 'Enter' && (e.target === input || e.target === okBtn || !box.contains(e.target))) {
                    e.preventDefault(); e.stopPropagation(); accept();
                } else if (e.key === 'Tab') {
                    var f = [input, cancelBtn, okBtn].filter(Boolean);
                    var i = f.indexOf(document.activeElement);
                    e.preventDefault();
                    f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
                }
            }
            document.addEventListener('keydown', onKey, true);
            okBtn.addEventListener('click', accept);
            if (cancelBtn) cancelBtn.addEventListener('click', dismiss);
            if (input) input.addEventListener('input', function () { input.classList.remove('uid-err'); });
            back.addEventListener('mousedown', function (e) { if (e.target === back) dismiss(); });

            requestAnimationFrame(function () {
                back.classList.add('uid-in');
                if (input) { input.focus(); input.select(); }
                // Danger confirms focus Cancel so a stray Enter/Space isn't destructive.
                else if (cancelBtn && tone === 'danger') cancelBtn.focus();
                else okBtn.focus();
            });
        });
    }

    var UIDialog = {
        confirm: function (message, opts) { return open('confirm', message, opts); },
        alert: function (message, opts) { return open('alert', message, opts); },
        prompt: function (message, opts) { return open('prompt', message, opts); }
    };
    window.UIDialog = UIDialog;

    // ---------- Declarative wiring ----------
    function confirmOpts(el) {
        var d = el.dataset;
        return { title: d.confirmTitle, confirmText: d.confirmOk, cancelText: d.confirmCancel, tone: d.confirmTone };
    }
    function promptOpts(el) {
        var d = el.dataset;
        return {
            title: d.promptTitle, confirmText: d.promptOk, placeholder: d.promptPlaceholder,
            defaultValue: d.promptDefault || '', required: d.promptRequired !== undefined && d.promptRequired !== 'false'
        };
    }
    // Runs the confirm/prompt step for el; resolves true when the action should proceed.
    function gate(el, form) {
        if (el.dataset.prompt !== undefined) {
            return UIDialog.prompt(el.dataset.prompt, promptOpts(el)).then(function (val) {
                if (val === null) return false;
                var name = el.dataset.promptField;
                var target = form && name ? form.elements[name] : null;
                if (target) target.value = val;
                return true;
            });
        }
        return UIDialog.confirm(el.dataset.confirm, confirmOpts(el));
    }
    function submitForm(form, submitter) {
        form.__uidPass = true;
        try {
            if (form.requestSubmit) {
                try { form.requestSubmit(submitter || undefined); return; } catch (e) {}
            }
            form.submit();
        } finally {
            // requestSubmit dispatches synchronously; clear the flag in case native
            // validation blocked the submit so the next attempt is gated again.
            setTimeout(function () { form.__uidPass = false; }, 0);
        }
    }

    // Buttons / links carrying their own data-confirm or data-prompt.
    document.addEventListener('click', function (e) {
        var el = e.target.closest && e.target.closest(
            'button[data-confirm],button[data-prompt],a[data-confirm],input[type=submit][data-confirm],input[type=submit][data-prompt]');
        if (!el || el.disabled) return;
        if (el.__uidPass) { el.__uidPass = false; return; }
        e.preventDefault();
        e.stopImmediatePropagation();
        var form = el.form || null;
        gate(el, form).then(function (ok) {
            if (!ok) return;
            if (el.tagName === 'A') {
                if (el.target && el.target !== '_self') window.open(el.href, el.target);
                else window.location.href = el.href;
            } else if (form && (el.type === 'submit' || el.getAttribute('type') == null)) {
                // Skip the form-level gate too; the user already confirmed.
                submitForm(form, el);
            } else {
                el.__uidPass = true;
                el.click();
            }
        });
    }, true);

    // Forms carrying data-confirm or data-prompt.
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        if (form.__uidPass) { form.__uidPass = false; return; }
        if (form.dataset.confirm === undefined && form.dataset.prompt === undefined) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        var submitter = e.submitter;
        gate(form, form).then(function (ok) { if (ok) submitForm(form, submitter); });
    }, true);
})();
