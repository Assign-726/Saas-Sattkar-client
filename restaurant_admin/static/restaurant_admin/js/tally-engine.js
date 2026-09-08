/**
 * TALLY FORM ENGINE - Universal JavaScript Module for Web Applications
 * Brings authentic Tally ERP / Tally Prime desktop experience to web forms.
 */
class TallyFormEngine {
    /**
     * @param {Object} config Configuration options
     * @param {string|HTMLElement} config.container CSS selector or DOM element of the voucher container
     * @param {string} [config.fieldSelector] Optional CSS selector to find focusable fields
     * @param {Object.<string, Array<string>>} [config.autocompleteData] Key-value dictionary mapping field IDs or classes to list arrays
     * @param {Function} [config.onCalculate] Optional calculation callback when table inputs change
     * @param {boolean} [config.enableDynamicRows=false] Automatically add new rows on last field enter
     */
    constructor(config = {}) {
        this.container = typeof config.container === 'string' ? document.querySelector(config.container) : config.container || document.body;
        this.fieldSelector = config.fieldSelector || null;
        this.autocompleteData = config.autocompleteData || {};
        this.onCalculate = config.onCalculate || null;
        this.enableDynamicRows = config.enableDynamicRows === true;

        this.blinkTimer = null;
        this.isMouseDown = false;

        this._initGlobalMouseTrackers();
        this.bindEvents();
    }

    _initGlobalMouseTrackers() {
        document.addEventListener('mousedown', () => { this.isMouseDown = true; });
        document.addEventListener('mouseup', () => { setTimeout(() => { this.isMouseDown = false; }, 60); });
    }

    isElementVisible(el) {
        if (!el) return false;
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }

    getCaretStatus(el) {
        if (!el) {
            return { start: null, end: null, hasSelection: false, allSelected: false, atStart: true };
        }
        try {
            if (typeof el.selectionStart === 'number' && typeof el.selectionEnd === 'number') {
                const val = el.value || '';
                return {
                    start: el.selectionStart,
                    end: el.selectionEnd,
                    hasSelection: el.selectionStart !== el.selectionEnd,
                    allSelected: (el.selectionStart === 0 && el.selectionEnd === val.length),
                    atStart: (el.selectionStart === 0)
                };
            }
        } catch (e) {
            // HTML5 input type="number", "email", etc. throw DOMException on selectionStart
        }
        return { start: null, end: null, hasSelection: false, allSelected: false, atStart: true };
    }

    safeSelect(el) {
        if (!el) return;
        try {
            if (typeof el.select === 'function') {
                el.select();
            }
        } catch(e) {}
    }

    getActiveContainer(contextElement) {
        if (contextElement && typeof contextElement.closest === 'function') {
            const activeTabPane = contextElement.closest('.settings-tab-pane.active, .tab-pane.active');
            if (activeTabPane) return activeTabPane;
        }
        if (this.container) {
            const activeTabPane = this.container.querySelector('.settings-tab-pane.active, .tab-pane.active');
            if (activeTabPane) return activeTabPane;
        }
        return this.container || document.body;
    }

    getFocusableElements(contextElement) {
        const root = this.getActiveContainer(contextElement);
        if (!root) return [];
        
        let selector = this.fieldSelector;
        if (!selector) {
            const hasExplicitTally = root.querySelectorAll('.tally-input').length > 0;
            if (hasExplicitTally) {
                selector = '.tally-input:not([readonly]):not([disabled])';
            } else {
                selector = 'input:not([type="hidden"]):not([readonly]):not([disabled]), select:not([readonly]):not([disabled]), textarea:not([readonly]):not([disabled]), button[type="submit"]:not([disabled]), button.btn-primary:not([disabled])';
            }
        }

        const candidates = Array.from(root.querySelectorAll(selector));
        return candidates.filter(el => {
            if (el.disabled || el.readOnly || el.type === 'hidden') return false;
            // Exclude elements that are inside inactive sub-tabs within the container
            const inactivePane = el.closest('.settings-tab-pane:not(.active), .tab-pane:not(.active)');
            if (inactivePane && inactivePane !== root) return false;
            return this.isElementVisible(el);
        });
    }

    stopBlinking() {
        if (this.blinkTimer) {
            clearInterval(this.blinkTimer);
            this.blinkTimer = null;
        }
    }

    startBlinking(inputElement) {
        this.stopBlinking();
        if (!inputElement || inputElement.readOnly || inputElement.disabled) return;

        // Number inputs and unsupported types don't support setSelectionRange in modern browsers
        if (inputElement.type === 'number') {
            this.safeSelect(inputElement);
            return;
        }

        const isTextInput = ['text', 'tel', 'email', 'search', 'url', 'password'].includes(inputElement.type || 'text');
        if (!isTextInput || typeof inputElement.setSelectionRange !== 'function') {
            inputElement.dataset.isEditing = 'true';
            return;
        }

        if (inputElement.value.length === 0) {
            inputElement.dataset.isEditing = 'true';
            return;
        }

        let toggle = true;
        try {
            inputElement.setSelectionRange(0, 1);
        } catch(e) {}

        this.blinkTimer = setInterval(() => {
            if (document.activeElement !== inputElement || inputElement.dataset.isEditing === 'true') {
                this.stopBlinking();
                return;
            }
            try {
                if (toggle) {
                    inputElement.setSelectionRange(0, 0);
                } else {
                    inputElement.setSelectionRange(0, 1);
                }
            } catch(e) {}
            toggle = !toggle;
        }, 530);
    }

    moveToNextField(currentElement) {
        const elements = this.getFocusableElements(currentElement);
        const currentIndex = elements.indexOf(currentElement);

        if (currentIndex > -1 && currentIndex < elements.length - 1) {
            const nextEl = elements[currentIndex + 1];
            nextEl.focus();
            if (nextEl.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit'].includes(nextEl.type)) {
                this.safeSelect(nextEl);
            }
        }
    }

    moveToPrevField(currentElement) {
        const elements = this.getFocusableElements(currentElement);
        const currentIndex = elements.indexOf(currentElement);
        if (currentIndex > 0) {
            const prevEl = elements[currentIndex - 1];
            prevEl.focus();
            if (prevEl.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit'].includes(prevEl.type)) {
                this.safeSelect(prevEl);
            }
        }
    }

    bindEventsToContainer(targetContainer) {
        if (!targetContainer) return;
        
        let selector = this.fieldSelector;
        if (!selector) {
            const hasExplicitTally = targetContainer.querySelectorAll('.tally-input').length > 0;
            if (hasExplicitTally) {
                selector = '.tally-input';
            } else {
                selector = 'input:not([type="hidden"]), select, textarea, button[type="submit"]';
            }
        }

        const inputs = targetContainer.querySelectorAll(selector);
        inputs.forEach(input => this.attachFieldEvents(input));
    }

    attachFieldEvents(input) {
        if (!input || input.dataset.tallyBound === 'true') return;
        input.dataset.tallyBound = 'true';
        
        const self = this;
        const isTextInput = ['text', 'number', 'tel', 'email', 'search', 'url', 'password'].includes(input.type || 'text');

        input.addEventListener('focus', function () {
            if (!self.isMouseDown) {
                if (isTextInput && this.value && this.value.length > 0) {
                    this.dataset.isEditing = 'false';
                    self.safeSelect(this);
                    setTimeout(() => { self.startBlinking(this); }, 10);
                } else {
                    this.dataset.isEditing = 'true';
                    if (isTextInput) {
                        self.safeSelect(this);
                    }
                }
            } else {
                this.dataset.isEditing = 'true';
                self.stopBlinking();
            }
        });

        input.addEventListener('blur', function () {
            self.stopBlinking();
        });

        input.addEventListener('mousedown', function () {
            this.dataset.isEditing = 'true';
            self.stopBlinking();
        });

        input.addEventListener('click', function () {
            this.dataset.isEditing = 'true';
            self.stopBlinking();
        });

        input.addEventListener('keydown', function (e) {
            const isEditing = (this.dataset.isEditing === 'true');

            // Skip default tally navigation if field has custom navigation handling or is combobox
            if (this.getAttribute('role') === 'combobox' || this.dataset.tallyCustom === 'true' || this.classList.contains('no-tally-enter')) {
                return;
            }

            // 1. FIELD NAVIGATION (ENTER / SHIFT+ENTER / ARROWS)
            if (e.key === 'Enter') {
                if (e.shiftKey) {
                    // Shift + Enter navigates backward
                    e.preventDefault();
                    self.moveToPrevField(this);
                    return;
                }
                if (this.tagName === 'BUTTON' || this.type === 'submit') {
                    return; // Let standard button click / form submission happen
                }
                if (this.tagName === 'TEXTAREA' && !e.ctrlKey) {
                    return; // Allow newlines in textarea unless ctrl+enter
                }
                e.preventDefault();
                self.moveToNextField(this);
            }
            else if (e.key === 'ArrowUp') {
                if (this.tagName === 'SELECT') {
                    // Let select change options
                    return;
                }
                e.preventDefault();
                self.moveToPrevField(this);
            }
            else if (e.key === 'ArrowDown') {
                if (this.tagName === 'SELECT') {
                    // Let select change options
                    return;
                }
                e.preventDefault();
                self.moveToNextField(this);
            }
            else if (e.key === 'Backspace') {
                // Non-text elements (select, button, checkbox, radio) always stroll backward
                if (this.tagName === 'SELECT' || this.tagName === 'BUTTON' || this.type === 'submit' || this.type === 'checkbox' || this.type === 'radio') {
                    e.preventDefault();
                    self.moveToPrevField(this);
                    return;
                }

                // Text / number inputs:
                const caret = self.getCaretStatus(this);
                const val = (this.value !== null && typeof this.value !== 'undefined') ? String(this.value) : '';
                const isEmpty = (val === '');

                if (!isEditing || isEmpty || caret.allSelected || (caret.start === 0 && caret.end === 0)) {
                    e.preventDefault();
                    self.moveToPrevField(this);
                }
            }
            // 2. EDIT MODE SWITCHING (LEFT, RIGHT, HOME, END)
            else if (['ArrowRight', 'ArrowLeft', 'End', 'Home'].includes(e.key)) {
                this.dataset.isEditing = 'true';
                self.stopBlinking();

                if (e.key === 'End' && typeof this.setSelectionRange === 'function') {
                    try {
                        e.preventDefault();
                        this.setSelectionRange(this.value.length, this.value.length);
                    } catch(ex) {}
                } else if (e.key === 'Home' && typeof this.setSelectionRange === 'function') {
                    try {
                        e.preventDefault();
                        this.setSelectionRange(0, 0);
                    } catch(ex) {}
                }
            }
            // 3. TYPING IN FIELD
            else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey && this.tagName !== 'SELECT' && isTextInput) {
                if (!isEditing) {
                    self.stopBlinking();
                    this.dataset.isEditing = 'true';
                }
            }
        });

        input.addEventListener('input', () => {
            input.dataset.isEditing = 'true';
            if (typeof self.onCalculate === 'function') {
                self.onCalculate(self.container);
            }
        });
    }

    bindEvents() {
        this.bindEventsToContainer(this.container);
    }
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = TallyFormEngine;
} else {
    window.TallyFormEngine = TallyFormEngine;
}
