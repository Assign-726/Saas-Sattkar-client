/**
 * Smart Select — type-to-search for long <select> dropdowns, without replacing them.
 *
 * The native <select> stays in the page (so form validation, onchange handlers, server
 * rendering and the Tally Enter-to-next engine all keep working); we only swap its popup
 * for a searchable panel.
 *
 * Opens on: click, Space, Alt+↓ / F4, Enter (except on Tally forms, where Enter moves to the next field),
 * or simply typing a letter while the select is focused. Touch-only devices keep the native picker.
 * In the panel: type to filter (every word must match name, SKU, GSTIN…), ↑ ↓ PgUp PgDn Home End
 * to move, Enter to pick and jump to the next field, Tab to pick and move on, Esc to close.
 *
 * Applied to:
 *   - every select with data-search="on", anywhere;
 *   - on pages that call SmartSelect.auto(), every select with at least `minOptions` options
 *     (opt out with data-search="off"). Rows added later (line editors) are picked up too.
 */
(function (window, document) {
  'use strict';

  var bound = new WeakSet();
  var panel = null;       // the one open panel: {root, input, list, select, items, active}
  var MAX_ROWS = 300;

  function norm(s) {
    return (s || '').toString().toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim();
  }

  function esc(s) {
    return (s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function highlight(text, words) {
    if (!words.length) return esc(text);
    var lower = norm(text), marks = [];
    words.forEach(function (w) {
      var i = lower.indexOf(w);
      if (i > -1) marks.push([i, i + w.length]);
    });
    if (!marks.length || lower.length !== text.length) return esc(text);
    marks.sort(function (a, b) { return a[0] - b[0]; });
    var out = '', pos = 0;
    marks.forEach(function (m) {
      if (m[0] < pos) return;
      out += esc(text.slice(pos, m[0])) + '<mark>' + esc(text.slice(m[0], m[1])) + '</mark>';
      pos = m[1];
    });
    return out + esc(text.slice(pos));
  }

  function readOptions(select) {
    var out = [];
    Array.prototype.forEach.call(select.options, function (opt, index) {
      var group = opt.parentNode && opt.parentNode.tagName === 'OPTGROUP' ? opt.parentNode.label : '';
      var label = opt.textContent.replace(/\s+/g, ' ').trim();
      var extra = Object.keys(opt.dataset).map(function (k) { return opt.dataset[k]; }).join(' ');
      out.push({ index: index, value: opt.value, label: label, group: group, disabled: opt.disabled || opt.hidden,
                 placeholder: opt.value === '', hay: norm(label + ' ' + group + ' ' + extra) });
    });
    return out;
  }

  function score(item, words, q) {
    if (!words.length) return 0;
    for (var i = 0; i < words.length; i++) {
      if (item.hay.indexOf(words[i]) === -1) return -1;
    }
    var label = norm(item.label);
    if (label === q) return 0;
    if (label.indexOf(q) === 0) return 1;
    if ((' ' + label).indexOf(' ' + words[0]) > -1) return 2;
    return 3;
  }

  // ------------------------------------------------------------------ panel

  function build(select) {
    var root = document.createElement('div');
    root.className = 'ss-panel';
    root.setAttribute('role', 'dialog');
    root.innerHTML =
      '<div class="ss-search"><i class="fas fa-search" aria-hidden="true"></i>' +
      '<input type="text" class="ss-input" autocomplete="off" spellcheck="false" role="combobox" aria-autocomplete="list" aria-expanded="true">' +
      '<kbd class="ss-kbd">Esc</kbd></div>' +
      '<ul class="ss-list" role="listbox"></ul>' +
      '<div class="ss-foot"><span class="ss-count"></span><span class="ss-hint">↑↓ move · Enter pick · Esc close</span></div>';
    var id = 'ss-list-' + Math.random().toString(36).slice(2, 8);
    var input = root.querySelector('.ss-input');
    var list = root.querySelector('.ss-list');
    list.id = id;
    input.setAttribute('aria-controls', id);
    var label = select.labels && select.labels[0] ? select.labels[0].textContent.trim() : '';
    input.placeholder = 'Search ' + (label ? label.toLowerCase() : 'options') + '…';
    input.setAttribute('aria-label', input.placeholder);
    // Inside a Bootstrap modal the panel must live in the modal, or its focus trap steals focus.
    (select.closest('.modal, dialog, .offcanvas') || document.body).appendChild(root);
    return { root: root, input: input, list: list, select: select, items: readOptions(select), shown: [], active: -1 };
  }

  function render() {
    var p = panel, q = norm(p.input.value), words = q ? q.split(' ') : [];
    var matches = [];
    p.items.forEach(function (it) {
      if (it.disabled) return;
      if (it.placeholder && words.length) return;
      var s = score(it, words, q);
      if (s > -1) matches.push({ it: it, s: s });
    });
    if (words.length) matches.sort(function (a, b) { return a.s - b.s || a.it.index - b.it.index; });
    var total = matches.length;
    matches = matches.slice(0, MAX_ROWS);
    p.shown = matches.map(function (m) { return m.it; });

    var html = '', lastGroup = null, current = p.select.value;
    p.shown.forEach(function (it, i) {
      if (!words.length && it.group !== lastGroup && it.group) html += '<li class="ss-group" role="presentation">' + esc(it.group) + '</li>';
      lastGroup = it.group;
      var sel = it.value === current && !it.placeholder;
      html += '<li class="ss-opt' + (it.placeholder ? ' ss-placeholder' : '') + (sel ? ' is-selected' : '') +
        '" role="option" id="' + p.list.id + '-' + i + '" data-i="' + i + '" aria-selected="' + sel + '">' +
        '<span class="ss-label">' + highlight(it.label, words) + '</span>' +
        (words.length && it.group ? '<span class="ss-tag">' + esc(it.group) + '</span>' : '') +
        (sel ? '<i class="fas fa-check ss-tick" aria-hidden="true"></i>' : '') + '</li>';
    });
    if (!p.shown.length) html = '<li class="ss-empty">No match for “' + esc(p.input.value) + '”</li>';
    p.list.innerHTML = html;
    p.root.querySelector('.ss-count').textContent = total > MAX_ROWS
      ? 'Showing ' + MAX_ROWS + ' of ' + total + ' — keep typing'
      : (words.length ? total + ' match' + (total === 1 ? '' : 'es') : total + ' option' + (total === 1 ? '' : 's'));

    var start = -1;
    if (!words.length) {
      p.shown.forEach(function (it, i) { if (start < 0 && it.value === current && !it.placeholder) start = i; });
    }
    if (start < 0) start = p.shown.length && p.shown[0].placeholder && p.shown.length > 1 && words.length === 0 && !current ? 1 : 0;
    move(p.shown.length ? start : -1, true);
  }

  function move(i, center) {
    var p = panel;
    var rows = p.list.querySelectorAll('.ss-opt');
    if (!rows.length) { p.active = -1; p.input.removeAttribute('aria-activedescendant'); return; }
    i = Math.max(0, Math.min(rows.length - 1, i));
    if (p.active > -1 && rows[p.active]) rows[p.active].classList.remove('is-active');
    p.active = i;
    rows[i].classList.add('is-active');
    p.input.setAttribute('aria-activedescendant', rows[i].id);
    var r = rows[i], top = r.offsetTop, bottom = top + r.offsetHeight;
    if (center) p.list.scrollTop = Math.max(0, top - p.list.clientHeight / 2 + r.offsetHeight / 2);
    else if (top < p.list.scrollTop) p.list.scrollTop = top;
    else if (bottom > p.list.scrollTop + p.list.clientHeight) p.list.scrollTop = bottom - p.list.clientHeight;
  }

  function place() {
    if (!panel) return;
    var r = panel.select.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    if (r.bottom < 0 || r.top > vh) { close(false); return; }
    var w = Math.min(Math.max(r.width, 300), vw - 16);
    var left = Math.min(Math.max(8, r.left), vw - w - 8);
    var below = vh - r.bottom - 8, above = r.top - 8;
    var maxH = Math.min(380, Math.max(below, above));
    panel.root.style.width = w + 'px';
    panel.root.style.left = left + 'px';
    panel.list.style.maxHeight = Math.max(120, maxH - 84) + 'px';
    if (below < 260 && above > below) {
      panel.root.style.top = '';
      panel.root.style.bottom = (vh - r.top + 4) + 'px';
      panel.root.classList.add('is-up');
    } else {
      panel.root.style.bottom = '';
      panel.root.style.top = (r.bottom + 4) + 'px';
      panel.root.classList.remove('is-up');
    }
  }

  function open(select, seed) {
    if (select.disabled || select.hasAttribute('readonly')) return;
    if (panel && panel.select === select) return;
    close(false);
    panel = build(select);
    select.classList.add('ss-open');
    select.setAttribute('aria-expanded', 'true');
    panel.input.value = seed || '';
    render();
    place();
    panel.input.focus();
    var len = panel.input.value.length;
    panel.input.setSelectionRange(len, len);

    panel.input.addEventListener('input', render);
    panel.input.addEventListener('keydown', onPanelKey);
    panel.list.addEventListener('mousemove', function (e) {
      var li = e.target.closest('.ss-opt');
      if (li && +li.dataset.i !== panel.active) move(+li.dataset.i);
    });
    panel.list.addEventListener('mousedown', function (e) { e.preventDefault(); });  // keep focus in the search box
    panel.list.addEventListener('click', function (e) {
      var li = e.target.closest('.ss-opt');
      if (li) choose(+li.dataset.i, 'click');
    });
  }

  function close(refocus) {
    if (!panel) return;
    var p = panel;
    panel = null;
    p.root.remove();
    p.select.classList.remove('ss-open');
    p.select.setAttribute('aria-expanded', 'false');
    if (refocus) p.select.focus({ preventScroll: true });
  }

  function choose(i, how) {
    var p = panel, it = p.shown[i];
    if (!it) return;
    var changed = p.select.selectedIndex !== it.index;
    p.select.selectedIndex = it.index;
    close(true);
    if (changed) {
      p.select.dispatchEvent(new Event('input', { bubbles: true }));
      p.select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (how === 'enter') {
      // Same as Tally: Enter picks and moves on. Pages using the Tally engine listen for this.
      p.select.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    }
  }

  function onPanelKey(e) {
    var p = panel, page = Math.max(1, Math.floor(p.list.clientHeight / 34));
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); move(p.active + 1); break;
      case 'ArrowUp': e.preventDefault(); move(p.active - 1); break;
      case 'PageDown': e.preventDefault(); move(p.active + page); break;
      case 'PageUp': e.preventDefault(); move(p.active - page); break;
      case 'Home': if (!p.input.value) { e.preventDefault(); move(0); } break;
      case 'End': if (!p.input.value) { e.preventDefault(); move(p.shown.length - 1); } break;
      case 'Enter': e.preventDefault(); e.stopPropagation(); if (p.active > -1) choose(p.active, 'enter'); break;
      case 'Tab':  // focus goes back to the select first, so the browser's Tab then moves on from there
        if (!e.shiftKey && p.active > -1 && p.input.value) choose(p.active, 'tab'); else close(true);
        break;
      case 'Escape': e.preventDefault(); e.stopPropagation(); close(true); break;
    }
  }

  // ------------------------------------------------------------------ binding

  function onSelectKey(e) {
    var s = e.currentTarget;
    if (!e.isTrusted || e.ctrlKey || e.metaKey) return;  // ignore our own synthetic Enter
    var printable = e.key && e.key.length === 1 && e.key !== ' ' && !e.altKey;
    var opener = e.key === ' ' || e.key === 'F4' || (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) ||
      (e.key === 'Enter' && !e.shiftKey && s.dataset.tallyBound !== 'true');  // on Tally forms Enter moves to the next field
    if (printable || opener) {
      e.preventDefault();
      e.stopImmediatePropagation();
      open(s, printable ? e.key : '');
    }
  }

  function onSelectMouseDown(e) {
    if (e.button !== 0) return;
    var s = e.currentTarget;
    e.preventDefault();
    if (panel && panel.select === s) { close(true); return; }
    s.focus({ preventScroll: true });
    open(s, '');
  }

  // Phones and tablets keep their native picker — it is already searchable-by-scroll and full screen.
  var touchOnly = window.matchMedia && window.matchMedia('(hover: none) and (pointer: coarse)').matches;

  function enhance(select) {
    if (touchOnly || bound.has(select) || select.multiple || (select.size && select.size > 1)) return;
    bound.add(select);
    select.classList.add('ss-enhanced');
    select.setAttribute('aria-haspopup', 'listbox');
    select.setAttribute('aria-expanded', 'false');
    if (!select.title) select.title = 'Click or start typing to search';
    // Capture phase so we run before other key handlers bound to the same select (Tally engine).
    select.addEventListener('keydown', onSelectKey, true);
    select.addEventListener('mousedown', onSelectMouseDown, true);
  }

  function qualifies(select, min) {
    if (select.dataset.search === 'off') return false;
    if (select.dataset.search === 'on') return true;
    return min !== null && select.options.length >= min;
  }

  function scan(root, min) {
    (root.querySelectorAll ? root : document).querySelectorAll('select').forEach(function (s) {
      if (qualifies(s, min)) enhance(s);
    });
    if (root.tagName === 'SELECT' && qualifies(root, min)) enhance(root);
  }

  var autoMin = null;

  function watch() {
    new MutationObserver(function (records) {
      records.forEach(function (rec) {
        rec.addedNodes.forEach(function (n) {
          if (n.nodeType === 1 && !n.classList.contains('ss-panel')) scan(n, autoMin);
        });
        // Options filled in later (e.g. by fetch) can push a select over the threshold.
        if (rec.target.tagName === 'SELECT' && qualifies(rec.target, autoMin)) enhance(rec.target);
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  document.addEventListener('mousedown', function (e) {
    if (panel && !panel.root.contains(e.target) && e.target !== panel.select) close(false);
  });
  window.addEventListener('resize', place);
  document.addEventListener('scroll', function (e) {
    if (panel && !panel.root.contains(e.target)) place();
  }, true);

  window.SmartSelect = {
    /** Enhance one select, or every qualifying select inside a container. */
    enhance: function (el) { if (el && el.tagName === 'SELECT') enhance(el); else scan(el || document, null); },
    /** Searchable dropdowns for every select with at least `minOptions` options on this page. */
    auto: function (minOptions) { autoMin = minOptions || 8; scan(document, autoMin); },
    close: function () { close(false); }
  };

  function boot() {
    scan(document, null);  // data-search="on" anywhere
    var cfg = document.body.dataset.smartSelect;
    if (cfg) window.SmartSelect.auto(parseInt(cfg, 10) || 8);
    watch();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})(window, document);
