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

    getFocusableElements() {
        if (!this.container) return [];
        
        let selector = this.fieldSelector;
        if (!selector) {
            // Check if container uses explicit .tally-input
            const hasExplicitTally = this.container.querySelectorAll('.tally-input').length > 0;
            if (hasExplicitTally) {
                selector = '.tally-input:not([readonly]):not([disabled])';
            } else {
                selector = 'input:not([type="hidden"]):not([readonly]):not([disabled]), select:not([readonly]):not([disabled]), textarea:not([readonly]):not([disabled]), button[type="submit"]:not([disabled])';
            }
        }

        const candidates = Array.from(this.container.querySelectorAll(selector));
        return candidates.filter(el => {
            if (el.disabled || el.readOnly || el.type === 'hidden') return false;
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

        const isTextInput = ['text', 'number', 'tel', 'email', 'search', 'url', 'password'].includes(inputElement.type || 'text');
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
        const elements = this.getFocusableElements();
        const currentIndex = elements.indexOf(currentElement);

        if (currentIndex > -1 && currentIndex < elements.length - 1) {
            const nextEl = elements[currentIndex + 1];
            nextEl.focus();
            if (typeof nextEl.select === 'function' && nextEl.tagName === 'INPUT' && !['checkbox', 'radio', 'button'].includes(nextEl.type)) {
                try { nextEl.select(); } catch(e){}
            }
        }
    }

    moveToPrevField(currentElement) {
        const elements = this.getFocusableElements();
        const currentIndex = elements.indexOf(currentElement);
        if (currentIndex > 0) {
            const prevEl = elements[currentIndex - 1];
            prevEl.focus();
            if (typeof prevEl.select === 'function' && prevEl.tagName === 'INPUT' && !['checkbox', 'radio', 'button'].includes(prevEl.type)) {
                try { prevEl.select(); } catch(e){}
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
                selector = 'input:not([type="hidden"]), select, textarea';
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
                if (isTextInput && this.value.length > 0) {
                    this.dataset.isEditing = 'false';
                    try { this.select(); } catch(e){}
                    setTimeout(() => { self.startBlinking(this); }, 10);
                } else {
                    this.dataset.isEditing = 'true';
                    if (isTextInput) {
                        try { this.select(); } catch(e){}
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

            // 1. FIELD NAVIGATION (ENTER & ARROWS)
            if (e.key === 'Enter') {
                if (this.tagName === 'BUTTON' || this.type === 'submit') {
                    return; // Let standard button click happen
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
            else if (e.key === 'Backspace') {
                if (this.tagName === 'SELECT' || this.type === 'checkbox' || this.type === 'radio') {
                    e.preventDefault();
                    self.moveToPrevField(this);
                    return;
                }
                if (!isEditing || this.value === '' || (this.selectionStart === 0 && this.selectionEnd === this.value.length) || (this.selectionStart === 0 && this.selectionEnd === 0)) {
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
            // 3. OVERWRITE TYPING MODE
            else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey && this.tagName !== 'SELECT' && isTextInput) {
                if (!isEditing) {
                    self.stopBlinking();
                    this.value = '';
                    this.dataset.isEditing = 'true';
                }
            }
        });

        input.addEventListener('input', () => {
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
