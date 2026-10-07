/**
 * Unified Theme Manager (Light / Dark / System Preference)
 * Supports Restaurant Admin, Event Management, Hotel PMS, and Super Admin.
 */
(function () {
    'use strict';

    const STORAGE_KEY = 'app-theme-preference';

    // Get stored preference or default to 'system'
    function getStoredPreference() {
        return localStorage.getItem(STORAGE_KEY) || 'system';
    }

    // Determine current system OS preference
    function getSystemTheme() {
        return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    // Resolve effective theme ('dark' or 'light')
    function getEffectiveTheme(preference) {
        if (preference === 'system') {
            return getSystemTheme();
        }
        return preference === 'dark' ? 'dark' : 'light';
    }

    // Apply theme to document
    function applyTheme(preference, save = true) {
        if (save) {
            try {
                localStorage.setItem(STORAGE_KEY, preference);
            } catch (e) {
                console.warn('Unable to persist theme to localStorage', e);
            }
        }

        const effective = getEffectiveTheme(preference);

        // Apply HTML root attributes
        document.documentElement.setAttribute('data-bs-theme', effective);
        document.documentElement.setAttribute('data-theme', effective);

        if (effective === 'dark') {
            document.documentElement.classList.add('dark-theme');
            if (document.body) {
                document.body.classList.add('dark-mode');
            }
        } else {
            document.documentElement.classList.remove('dark-theme');
            if (document.body) {
                document.body.classList.remove('dark-mode');
            }
        }

        // Update UI controls on the page
        updateUI(preference, effective);

        // Dispatch custom event for charts, maps, or external listeners
        window.dispatchEvent(new CustomEvent('appThemeChanged', {
            detail: { preference: preference, effective: effective }
        }));
    }

    // Update icons, active states, and labels across all toggle buttons
    function updateUI(preference, effective) {
        // Icon classes map
        const iconClasses = {
            light: { fa: 'fa-sun text-warning', bi: 'bi-sun-fill text-warning' },
            dark: { fa: 'fa-moon text-primary', bi: 'bi-moon-stars-fill text-primary' },
            system: { fa: 'fa-desktop text-secondary', bi: 'bi-display text-secondary' }
        };

        const labels = {
            light: 'Light',
            dark: 'Dark',
            system: 'System'
        };

        // Update indicator icons on trigger buttons
        document.querySelectorAll('.theme-icon-indicator').forEach(function (iconEl) {
            // Check if font-awesome or bootstrap icon
            const isFa = iconEl.classList.contains('fa') || iconEl.classList.contains('fa-solid') || iconEl.classList.contains('fas');
            if (isFa) {
                iconEl.className = 'theme-icon-indicator fa-solid ' + (iconClasses[preference] ? iconClasses[preference].fa : iconClasses.system.fa);
            } else {
                iconEl.className = 'theme-icon-indicator bi ' + (iconClasses[preference] ? iconClasses[preference].bi : iconClasses.system.bi);
            }
        });

        // Update indicator text
        document.querySelectorAll('.theme-text-indicator').forEach(function (textEl) {
            textEl.textContent = labels[preference] || 'Theme';
        });

        // Update segmented control buttons
        document.querySelectorAll('.theme-seg-btn').forEach(function (btn) {
            const btnVal = btn.getAttribute('data-theme-val');
            if (btnVal === preference) {
                btn.classList.add('active');
                btn.setAttribute('aria-pressed', 'true');
            } else {
                btn.classList.remove('active');
                btn.setAttribute('aria-pressed', 'false');
            }
        });

        // Update dropdown option items
        document.querySelectorAll('.theme-opt-btn').forEach(function (btn) {
            const btnVal = btn.getAttribute('data-theme-val');
            const checkIcon = btn.querySelector('.theme-check-icon');
            if (btnVal === preference) {
                btn.classList.add('active');
                if (checkIcon) checkIcon.classList.remove('d-none');
            } else {
                btn.classList.remove('active');
                if (checkIcon) checkIcon.classList.add('d-none');
            }
        });
    }

    // Initialize toggle button and dropdown interactions
    function initInteractions() {
        // Click handlers for segmented buttons & dropdown options
        document.addEventListener('click', function (e) {
            const segBtn = e.target.closest('.theme-seg-btn');
            if (segBtn) {
                e.preventDefault();
                e.stopPropagation();
                const selectedVal = segBtn.getAttribute('data-theme-val');
                if (selectedVal) {
                    applyTheme(selectedVal, true);
                }
                return;
            }

            const optBtn = e.target.closest('.theme-opt-btn');
            if (optBtn) {
                e.preventDefault();
                e.stopPropagation();
                const selectedVal = optBtn.getAttribute('data-theme-val');
                if (selectedVal) {
                    applyTheme(selectedVal, true);
                    // Close any open dropdown menu
                    const menu = optBtn.closest('.theme-menu, .dropdown-menu');
                    if (menu) {
                        menu.classList.remove('show');
                        const toggleBtn = menu.closest('.dropdown, .theme-toggle-dropdown')?.querySelector('.theme-toggle-btn');
                        if (toggleBtn) {
                            toggleBtn.setAttribute('aria-expanded', 'false');
                            toggleBtn.classList.remove('show');
                        }
                    }
                }
                return;
            }

            // Fallback dropdown toggle for pages without Bootstrap dropdown JS
            const toggleBtn = e.target.closest('.theme-toggle-btn');
            if (toggleBtn) {
                const parent = toggleBtn.closest('.dropdown, .theme-toggle-dropdown');
                if (parent) {
                    const menu = parent.querySelector('.theme-menu, .dropdown-menu');
                    if (menu && !window.bootstrap?.Dropdown) {
                        e.preventDefault();
                        const isShown = menu.classList.contains('show');
                        // Close other dropdowns
                        document.querySelectorAll('.theme-menu.show').forEach(m => m.classList.remove('show'));
                        if (!isShown) {
                            menu.classList.add('show');
                            toggleBtn.classList.add('show');
                            toggleBtn.setAttribute('aria-expanded', 'true');
                        } else {
                            menu.classList.remove('show');
                            toggleBtn.classList.remove('show');
                            toggleBtn.setAttribute('aria-expanded', 'false');
                        }
                    }
                }
                return;
            }

            // Click outside closes non-bootstrap dropdowns
            if (!e.target.closest('.theme-toggle-dropdown, .dropdown')) {
                document.querySelectorAll('.theme-toggle-dropdown .theme-menu.show').forEach(function (menu) {
                    menu.classList.remove('show');
                    const btn = menu.closest('.theme-toggle-dropdown')?.querySelector('.theme-toggle-btn');
                    if (btn) {
                        btn.classList.remove('show');
                        btn.setAttribute('aria-expanded', 'false');
                    }
                });
            }
        });

        // Fast cycle toggle for standalone 1-click theme switch buttons
        document.querySelectorAll('.theme-cycle-btn').forEach(function (cycleBtn) {
            cycleBtn.addEventListener('click', function (e) {
                e.preventDefault();
                const current = getStoredPreference();
                let next = 'dark';
                if (current === 'light') next = 'dark';
                else if (current === 'dark') next = 'system';
                else next = 'light';
                applyTheme(next, true);
            });
        });

        // Update UI initially
        updateUI(getStoredPreference(), getEffectiveTheme(getStoredPreference()));
    }

    // Media query listener for OS dark mode changes
    if (window.matchMedia) {
        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        const handleOSChange = function () {
            if (getStoredPreference() === 'system') {
                applyTheme('system', false);
            }
        };
        if (mediaQuery.addEventListener) {
            mediaQuery.addEventListener('change', handleOSChange);
        } else if (mediaQuery.addListener) {
            mediaQuery.addListener(handleOSChange);
        }
    }

    // Cross-tab synchronization
    window.addEventListener('storage', function (e) {
        if (e.key === STORAGE_KEY) {
            applyTheme(e.newValue || 'system', false);
        }
    });

    // Run on script load (immediate)
    applyTheme(getStoredPreference(), false);

    // Run on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initInteractions);
    } else {
        initInteractions();
    }

    // Expose global API
    window.ThemeManager = {
        applyTheme: applyTheme,
        getStoredPreference: getStoredPreference,
        getEffectiveTheme: function () {
            return getEffectiveTheme(getStoredPreference());
        }
    };
})();
