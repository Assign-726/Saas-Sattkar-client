/**
 * TALLY FORM ENGINE - Universal JavaScript Module for Web Applications
 * Brings authentic Tally ERP / Tally Prime desktop experience to web forms.
 */
class TallyFormEngine {
    /**
     * @param {Object} config Configuration options
     * @param {string|HTMLElement} config.container CSS selector or DOM element of the voucher container
     * @param {Object.<string, Array<string>>} [config.autocompleteData] Key-value dictionary mapping field IDs or classes to list arrays
     * @param {Function} [config.onCalculate] Optional calculation callback when table inputs change
     * @param {boolean} [config.enableDynamicRows=false] Automatically add new rows on last field enter
     */
    constructor(config = {}) {
        this.container = typeof config.container === 'string' ? document.querySelector(config.container) : config.container || document.body;
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
        document.addEventListener('mouseup', () => { this.isMouseDown = false; });
    }

    getFocusableElements() {
        if (!this.container) return [];
        return Array.from(this.container.querySelectorAll('.tally-input:not([readonly]):not([disabled])'));
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
            elements[currentIndex + 1].focus();
        }
    }

    moveToPrevField(currentElement) {
        const elements = this.getFocusableElements();
        const currentIndex = elements.indexOf(currentElement);
        if (currentIndex > 0) {
            elements[currentIndex - 1].focus();
        }
    }

    bindEventsToContainer(targetContainer) {
        const inputs = targetContainer.querySelectorAll('.tally-input');
        inputs.forEach(input => this.attachFieldEvents(input));
    }

    attachFieldEvents(input) {
        const self = this;

        input.addEventListener('focus', function () {
            if (!self.isMouseDown) {
                if (this.value.length > 0) {
                    this.dataset.isEditing = 'false';
                    setTimeout(() => { self.startBlinking(this); }, 10);
                } else {
                    this.dataset.isEditing = 'true';
                }
            } else {
                this.dataset.isEditing = 'true';
                self.stopBlinking();
            }
        });

        input.addEventListener('blur', function () {
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
                e.preventDefault();
                self.moveToNextField(this);
            }
            else if (e.key === 'ArrowUp') {
                e.preventDefault();
                self.moveToPrevField(this);
            }
            else if (e.key === 'Backspace') {
                if (!isEditing || this.value === '' || (this.selectionStart === 0 && this.selectionEnd === 0)) {
                    e.preventDefault();
                    self.moveToPrevField(this);
                }
            }
            // 2. EDIT MODE SWITCHING (LEFT, RIGHT, HOME, END)
            else if (['ArrowRight', 'ArrowLeft', 'End', 'Home'].includes(e.key)) {
                this.dataset.isEditing = 'true';
                self.stopBlinking();

                if (e.key === 'End') {
                    try {
                        e.preventDefault();
                        this.setSelectionRange(this.value.length, this.value.length);
                    } catch(ex) {}
                } else if (e.key === 'Home') {
                    try {
                        e.preventDefault();
                        this.setSelectionRange(0, 0);
                    } catch(ex) {}
                }
            }
            // 3. OVERWRITE TYPING MODE
            else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
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
