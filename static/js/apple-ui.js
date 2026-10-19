/**
 * Apple UI — list behaviour shared by every admin shell.
 *
 * On phones, apple-ui.css renders tables marked `.ui-stack` as a stack of
 * rounded rows (title, label/value pairs, actions). This script marks the
 * tables and copies each column header into its cells as `data-label`.
 *
 * Opt out per table or per container with `data-ui-no-stack`.
 */
(function () {
    'use strict';

    var SELECTOR = 'table.table, table.pms-table, .table-container table';
    var MIN_COLUMNS = 3;

    function headerLabels(table) {
        var head = table.tHead;
        if (!head || !head.rows.length) return null;
        // Use the last header row (handles grouped two-row headers)
        var row = head.rows[head.rows.length - 1];
        var labels = [];
        Array.prototype.forEach.call(row.cells, function (th) {
            var text = (th.innerText || th.textContent || '').replace(/\s+/g, ' ').trim();
            var span = th.colSpan || 1;
            for (var i = 0; i < span; i++) labels.push(text);
        });
        return labels;
    }

    function isActionCell(td) {
        if (td.querySelector('input:not([type="hidden"]), select, textarea')) return false;
        var controls = td.querySelectorAll('.btn, button, .action-btn, .dropdown-toggle');
        if (!controls.length) return false;
        // Only buttons/links/icons in the cell — no other meaningful text
        var clone = td.cloneNode(true);
        Array.prototype.forEach.call(clone.querySelectorAll('.btn, button, a, .action-btn, .dropdown-menu, form, i'), function (el) {
            el.remove();
        });
        return (clone.textContent || '').trim() === '';
    }

    function labelRow(tr, labels) {
        var col = 0;
        var cells = tr.cells;
        for (var i = 0; i < cells.length; i++) {
            var cell = cells[i];
            var span = cell.colSpan || 1;
            if (span === 1 && !cell.hasAttribute('data-label')) {
                cell.setAttribute('data-label', labels[col] || '');
            }
            col += span;
        }
        if (!cells.length) return;
        var first = cells[0];
        // A leading select-checkbox column becomes a corner control; the next cell is the title
        if (first.querySelector('input[type="checkbox"]') && !(first.textContent || '').trim()) {
            first.classList.add('ui-cell-select');
            first.setAttribute('data-label', '');
            first = cells[1] || first;
        }
        if (first.colSpan === 1 && !isActionCell(first) && !first.classList.contains('ui-cell-select')) first.classList.add('ui-cell-primary');
        var last = cells[cells.length - 1];
        if (cells.length > 1 && last.colSpan === 1 && isActionCell(last)) last.classList.add('ui-cell-actions');
    }

    function enhance(table) {
        if (table.dataset.uiStackReady) return;
        if (table.closest('[data-ui-no-stack], .modal, .dropdown-menu, .offcanvas')) return;
        // Editable line-item grids (purchases, POS cart) keep their columns and scroll sideways
        if (table.querySelector('tbody input:not([type="hidden"]):not([type="checkbox"]), tbody select, tbody textarea')) return;
        var labels = headerLabels(table);
        if (!labels || labels.length < MIN_COLUMNS) return;

        table.dataset.uiStackReady = '1';
        table.classList.add('ui-stack');
        Array.prototype.forEach.call(table.tBodies, function (tbody) {
            Array.prototype.forEach.call(tbody.rows, function (tr) { labelRow(tr, labels); });
        });
    }

    function enhanceAll(root) {
        Array.prototype.forEach.call((root || document).querySelectorAll(SELECTOR), enhance);
    }

    // Re-label rows that are added later (AJAX lists, Alpine x-for, live orders)
    function watch() {
        if (!('MutationObserver' in window)) return;
        var pending = false;
        new MutationObserver(function (mutations) {
            if (pending) return;
            var relevant = mutations.some(function (m) {
                return m.addedNodes.length && (m.target.closest ? m.target.closest('table, .main-content, .pms-content, .main') : true);
            });
            if (!relevant) return;
            pending = true;
            requestAnimationFrame(function () {
                pending = false;
                Array.prototype.forEach.call(document.querySelectorAll('table.ui-stack'), function (table) {
                    var labels = headerLabels(table);
                    if (!labels) return;
                    Array.prototype.forEach.call(table.tBodies, function (tbody) {
                        Array.prototype.forEach.call(tbody.rows, function (tr) {
                            if (!tr.dataset.uiLabelled) { labelRow(tr, labels); tr.dataset.uiLabelled = '1'; }
                        });
                    });
                });
                enhanceAll();
            });
        }).observe(document.body, { childList: true, subtree: true });
    }

    /* ------------------------------------------------------------------
       Collapsible sidebar (all shells). Restaurant admin already owns the
       state logic behind #desktopSidebarCollapseBtn, so we drive that
       button; Hotel and Super Admin use the same html class + storage key.
       ------------------------------------------------------------------ */
    var KEY = 'sidebar_collapsed';
    var html = document.documentElement;

    function isTyping(el) {
        return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    }

    function initRail() {
        var sidebar = document.querySelector('.pms-sidebar') || document.querySelector('aside.sidebar') || document.querySelector('.sidebar');
        if (!sidebar) return;
        var legacyBtn = document.getElementById('desktopSidebarCollapseBtn');

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ui-rail-toggle';
        btn.innerHTML = '<i class="fa-solid fa-chevron-left"></i>';
        document.body.appendChild(btn);

        function collapsed() { return html.classList.contains('sidebar-collapsed'); }

        function labelLinks() {
            sidebar.querySelectorAll('a.nav-link, .sb-link, .nav a, .sidebar-group-header').forEach(function (a) {
                if (!a.dataset.uiTitled && !a.title) {
                    a.title = (a.innerText || '').replace(/\s+/g, ' ').trim();
                    a.dataset.uiTitled = '1';
                }
            });
        }

        function place() {
            var r = sidebar.getBoundingClientRect();
            btn.style.left = Math.round(r.right - 13) + 'px';
            var c = collapsed();
            btn.firstChild.className = 'fa-solid ' + (c ? 'fa-chevron-right' : 'fa-chevron-left');
            btn.title = (c ? 'Expand' : 'Collapse') + ' sidebar  [';
            btn.setAttribute('aria-label', btn.title);
            btn.setAttribute('aria-expanded', String(!c));
        }

        function set(c) {
            if (legacyBtn) {
                if (collapsed() !== c) legacyBtn.click();
            } else {
                html.classList.toggle('sidebar-collapsed', c);
                document.body.classList.toggle('sidebar-collapsed', c);
                try { localStorage.setItem(KEY, c ? 'true' : 'false'); } catch (e) { /* private mode */ }
            }
            place();
            setTimeout(place, 300);
            window.dispatchEvent(new Event('resize')); // let charts / floor plans re-measure
        }

        btn.addEventListener('click', function () { set(!collapsed()); });
        document.addEventListener('keydown', function (e) {
            if (e.key !== '[' || e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
            if (window.innerWidth < 992) return;
            e.preventDefault();
            set(!collapsed());
        });

        if ('ResizeObserver' in window) new ResizeObserver(place).observe(sidebar);
        window.addEventListener('resize', place);
        sidebar.addEventListener('transitionend', place);
        labelLinks();
        place();
        setTimeout(place, 400); // after any width transition settles
    }

    /* ------------------------------------------------------------------
       Focus mode: hides the app chrome and requests browser full screen.
       Any element with [data-ui-fullscreen] toggles it (POS terminal).
       Remembered per page for the tab session, so a POS reload stays clean.
       ------------------------------------------------------------------ */
    var FOCUS_KEY = 'ui_focus:' + location.pathname;

    function fsElement() { return document.fullscreenElement || document.webkitFullscreenElement; }

    function setFocus(on, fromUser) {
        html.classList.toggle('ui-focus', on);
        try { sessionStorage.setItem(FOCUS_KEY, on ? '1' : '0'); } catch (e) { /* ignore */ }
        if (fromUser) {
            var el = document.documentElement;
            try {
                if (on && !fsElement()) {
                    var req = el.requestFullscreen || el.webkitRequestFullscreen;
                    var res = req && req.call(el);
                    if (res && res.catch) res.catch(function () { /* iPad Safari: chrome-less mode only */ });
                } else if (!on && fsElement()) {
                    var exit = document.exitFullscreen || document.webkitExitFullscreen;
                    if (exit) exit.call(document);
                }
            } catch (e) { /* ignore */ }
        }
        window.dispatchEvent(new Event('resize'));
    }

    function initFocus() {
        var saved = null;
        try { saved = sessionStorage.getItem(FOCUS_KEY); } catch (e) { /* ignore */ }
        if (saved === '1' && document.querySelector('[data-ui-fullscreen]')) setFocus(true, false);

        document.addEventListener('click', function (e) {
            var t = e.target.closest && e.target.closest('[data-ui-fullscreen]');
            if (!t) return;
            e.preventDefault();
            setFocus(!html.classList.contains('ui-focus'), true);
        });
        ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (ev) {
            document.addEventListener(ev, function () {
                // Leaving browser full screen (Esc) also leaves focus mode
                if (!fsElement() && html.classList.contains('ui-focus')) setFocus(false, false);
            });
        });
    }

    /* ------------------------------------------------------------------
       Charts: one typographic + color system for every Chart.js chart.
       Legacy Tailwind colors in page scripts are mapped to system colors.
       ------------------------------------------------------------------ */
    var CHART_MAP = {
        light: {
            '79,70,229': '0,122,255', '99,102,241': '0,122,255', '37,99,235': '0,122,255', '59,130,246': '0,122,255',
            '94,114,228': '0,122,255', '67,56,202': '0,122,255', '47,62,158': '0,122,255', '242,108,13': '255,149,0',
            '16,185,129': '52,199,89', '5,150,105': '52,199,89', '34,197,94': '52,199,89', '22,163,74': '52,199,89',
            '245,158,11': '255,149,0', '217,119,6': '255,149,0', '249,115,22': '255,149,0', '234,88,12': '255,149,0',
            '239,68,68': '255,59,48', '220,38,38': '255,59,48', '244,63,94': '255,45,85',
            '139,92,246': '175,82,222', '124,58,237': '175,82,222', '168,85,247': '175,82,222',
            '6,182,212': '48,176,199', '14,165,233': '48,176,199', '2,132,199': '48,176,199',
            '100,116,139': '142,142,147', '148,163,184': '142,142,147'
        },
        dark: {
            '0,122,255': '10,132,255', '52,199,89': '48,209,88', '255,149,0': '255,159,10', '255,59,48': '255,69,58',
            '255,45,85': '255,55,95', '175,82,222': '191,90,242', '48,176,199': '64,203,224'
        }
    };

    function hexToRgb(h) {
        h = h.replace('#', '');
        if (h.length === 3) h = h.replace(/./g, '$&$&');
        var n = parseInt(h, 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(',');
    }

    function mapColor(c) {
        if (typeof c !== 'string') return c;
        var m, rgb, a = null;
        if ((m = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i))) rgb = hexToRgb(m[0]);
        else if ((m = c.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/i))) { rgb = [m[1], m[2], m[3]].join(','); a = m[4]; }
        else return c;
        var mapped = CHART_MAP.light[rgb] || rgb;
        if (html.getAttribute('data-theme') === 'dark') mapped = CHART_MAP.dark[mapped] || mapped;
        if (mapped === rgb) return c;
        return a !== null && a !== undefined ? 'rgba(' + mapped + ',' + a + ')' : 'rgb(' + mapped + ')';
    }

    function css(name) { return getComputedStyle(html).getPropertyValue(name).trim(); }

    function chartTheme() {
        var Chart = window.Chart;
        if (!Chart || !Chart.defaults) return;
        var d = Chart.defaults;
        d.font.family = css('--ui-font') || d.font.family;
        d.font.size = 12;
        d.color = css('--ui-label-2');
        d.borderColor = css('--ui-separator');
        if (d.plugins && d.plugins.legend) {
            d.plugins.legend.labels.usePointStyle = true;
            d.plugins.legend.labels.boxWidth = 8;
            d.plugins.legend.labels.boxHeight = 8;
        }
        if (d.plugins && d.plugins.tooltip) {
            var t = d.plugins.tooltip;
            t.backgroundColor = 'rgba(30,30,32,0.92)';
            t.titleColor = '#fff';
            t.bodyColor = 'rgba(235,235,245,0.85)';
            t.cornerRadius = 10;
            t.padding = 10;
            t.boxPadding = 4;
            t.usePointStyle = true;
        }
        if (d.elements) {
            if (d.elements.bar) d.elements.bar.borderRadius = 6;
            if (d.elements.line) d.elements.line.tension = 0.35;
            if (d.elements.point) { d.elements.point.radius = 0; d.elements.point.hoverRadius = 4; }
            if (d.elements.arc) d.elements.arc.borderWidth = 0;
        }
    }

    function initCharts() {
        var Chart = window.Chart;
        if (!Chart || !Chart.register || Chart.__appleUI) return;
        Chart.__appleUI = true;
        chartTheme();
        Chart.register({
            id: 'appleUI',
            beforeUpdate: function (chart) {
                (chart.data.datasets || []).forEach(function (ds) {
                    ['backgroundColor', 'borderColor', 'pointBackgroundColor', 'pointBorderColor', 'hoverBackgroundColor'].forEach(function (k) {
                        if (Array.isArray(ds[k])) ds[k] = ds[k].map(mapColor);
                        else if (ds[k]) ds[k] = mapColor(ds[k]);
                    });
                });
                var scales = chart.options && chart.options.scales;
                if (scales) Object.keys(scales).forEach(function (k) {
                    var s = scales[k];
                    if (s.grid) { s.grid.color = css('--ui-separator'); s.grid.drawBorder = false; }
                    if (s.border) s.border.display = false;
                    if (s.ticks) s.ticks.color = css('--ui-label-2');
                });
            }
        });
        window.addEventListener('appThemeChanged', function () {
            chartTheme();
            Object.values(Chart.instances || {}).forEach(function (c) { try { c.update('none'); } catch (e) { /* ignore */ } });
        });
    }

    // Chart defaults must exist before page scripts build charts on DOMContentLoaded
    initCharts();

    function init() {
        enhanceAll();
        watch();
        initRail();
        initFocus();
        initCharts();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.AppleUI = { enhanceTables: enhanceAll, setFocus: function (on) { setFocus(on, true); } };
})();
