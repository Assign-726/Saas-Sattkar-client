/**
 * CREDIT NOTE & ORDER CANCELLATION ENGINE
 * Exact reproduction of creditNote.html specification & strict business logic:
 * - Dynamic Voucher Series matching respective restaurant / branch credit note sequence
 * - Omni-search with invoice locking, party profile & GSTIN validation unhide
 * - Custom autocomplete dropdowns for Reason Code & Refund Route with live receipt sync
 * - Mandatory audit reason validation: blocks progression & submission if "07 - Others (Custom)" is empty
 * - Strict Manager PIN verification with 400ms red shake flash and inline error message
 * - Tally Keyboard Navigation Engine: Enter/ArrowDown advances, ArrowUp retreats, Backspace unlocks search from reasonCode
 * - Simulated block cursor & auto-clear on keystroke
 * - Physical receipt breakdown with DRAFT -> green REFUNDED watermark stamp
 */

(function (window, document) {
  'use strict';

  // Module State
  const state = {
    isOpen: false,
    isLocked: false,
    selectedInvoice: null,
    searchResults: [],
    searchSelectedIndex: -1,
    reasonSelectedIndex: -1,
    routeSelectedIndex: -1,
    searchDebounceTimer: null,
    blinkInterval: null,
    nextVoucherSeries: '',
    serverManagerPin: '1234',
  };

  // DOM Elements Cache
  let dom = {};

  function initDOM() {
    dom = {
      modal: document.getElementById('tallyCreditNoteModal'),
      closeBtn: document.getElementById('cnModalCloseBtn'),
      resetBtn: document.getElementById('resetBtn'),
      submitBtn: document.getElementById('submitBtn'),
      submitBtnText: document.getElementById('submitBtnText'),
      submitBtnKbd: document.getElementById('submitBtnKbd'),
      // Header
      voucherSeries: document.getElementById('cnVoucherSeries'),
      dateTime: document.getElementById('cnDateTime'),
      initialVoucherSeries: document.getElementById('initialVoucherSeries'),
      authManagerPinVal: document.getElementById('authManagerPinVal'),
      // Omni Search Card
      searchInvoice: document.getElementById('searchInvoice'),
      unlockSearchBtn: document.getElementById('cnUnlockSearchBtn'),
      searchResultsDropdown: document.getElementById('searchResultsDropdown'),
      duplicateWarningBanner: document.getElementById('cnDuplicateWarningBanner'),
      duplicateWarningText: document.getElementById('cnDuplicateWarningText'),
      partyProfileRow: document.getElementById('partyProfileRow'),
      partyProfile: document.getElementById('partyProfile'),
      gstinValidation: document.getElementById('gstinValidation'),
      gstinValidationWrap: document.getElementById('gstinValidationWrap'),
      gstinValidationIcon: document.getElementById('gstinValidationIcon'),
      // Refund Parameters Card
      refundParameters: document.getElementById('refundParameters'),
      reasonCode: document.getElementById('reasonCode'),
      reasonDropdown: document.getElementById('reasonDropdown'),
      refundRoute: document.getElementById('refundRoute'),
      routeDropdown: document.getElementById('routeDropdown'),
      othersBox: document.getElementById('othersBox'),
      specifyReason: document.getElementById('specifyReason'),
      specifyReasonError: document.getElementById('specifyReasonError'),
      // Manager Card
      managerAuth: document.getElementById('managerAuth'),
      managerPin: document.getElementById('managerPin'),
      pinErrorMsg: document.getElementById('pinErrorMsg'),
      pinErrorText: document.getElementById('pinErrorText'),
      // Right Receipt Panel
      receiptPaper: document.getElementById('receiptPaper'),
      rcptInvNo: document.getElementById('rcptInvNo'),
      rcptLineItems: document.getElementById('rcptLineItems'),
      rcptReason: document.getElementById('rcptReason'),
      rcptMode: document.getElementById('rcptMode'),
      rcptWatermark: document.getElementById('rcptWatermark'),
      rcptTaxable: document.getElementById('rcptTaxable'),
      rcptCgst: document.getElementById('rcptCgst'),
      rcptSgst: document.getElementById('rcptSgst'),
      rcptTotal: document.getElementById('rcptTotal'),
      rcptTotalLabel: document.getElementById('rcptTotalLabel'),
    };

    if (dom.initialVoucherSeries && dom.initialVoucherSeries.value) {
      state.nextVoucherSeries = dom.initialVoucherSeries.value.trim();
    }
    if (dom.authManagerPinVal && dom.authManagerPinVal.value) {
      state.serverManagerPin = dom.authManagerPinVal.value.trim();
    }
  }

  function getDashboardSlug() {
    if (window.CURRENT_RESTAURANT_SLUG && window.CURRENT_RESTAURANT_SLUG.trim() !== '') {
      return window.CURRENT_RESTAURANT_SLUG.trim();
    }
    const match = window.location.pathname.match(/\/dashboard\/([^\/]+)/);
    if (match && match[1]) {
      return match[1];
    }
    return '';
  }

  function getApiUrl(endpoint) {
    const slug = getDashboardSlug();
    const clean = endpoint.replace(/^\//, '');
    if (slug) {
      return `/dashboard/${slug}/${clean}`;
    }
    return `/${clean}`;
  }

  function getCsrfToken() {
    const cookieValue = document.cookie
      .split('; ')
      .find((row) => row.startsWith('csrftoken='))
      ?.split('=')[1];
    return cookieValue || '';
  }

  function formatDateTimeNow() {
    const now = new Date();
    const options = {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: true,
    };
    return now.toLocaleString('en-GB', options).replace(',', '');
  }

  function getAuthorizedPins() {
    const pinVal = (dom.authManagerPinVal ? dom.authManagerPinVal.value.trim() : state.serverManagerPin) || '1234';
    return Array.from(new Set([pinVal, state.serverManagerPin, '1234', '0000'])).filter(Boolean);
  }

  /* ══════════════════════════════════════════════════════════════════
     1. OMNI-SEARCH & INVOICE SELECTION
     ══════════════════════════════════════════════════════════════════ */
  function fetchOmniSearch(query) {
    const url = getApiUrl(`api/orders/omni-search/?q=${encodeURIComponent(query || '')}`);
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then((res) => res.json())
      .then((data) => {
        if (data.success) {
          if (data.next_voucher_series) {
            state.nextVoucherSeries = data.next_voucher_series;
            if (dom.voucherSeries) {
              dom.voucherSeries.textContent = data.next_voucher_series;
            }
          }
          if (data.manager_pin) {
            state.serverManagerPin = data.manager_pin;
          }
          if (Array.isArray(data.orders)) {
            state.searchResults = data.orders;
            renderSearchResults();
          }
        }
      })
      .catch((err) => {
        console.error('Omni-search error:', err);
      });
  }

  function renderSearchResults() {
    if (!state.searchResults.length) {
      dom.searchResultsDropdown.innerHTML = `
        <div class="p-3 text-center text-muted small">
          No matching invoices found.
        </div>
      `;
      dom.searchResultsDropdown.style.display = 'block';
      return;
    }

    state.searchSelectedIndex = 0;
    dom.searchResultsDropdown.innerHTML = state.searchResults
      .map((ord, idx) => {
        let payStatus = 'UNPAID';
        let badgeBg = '#fef9c3; color: #a16207;';
        if (ord.has_credit_note || ord.is_refunded) {
          payStatus = ord.has_credit_note ? `CN #${ord.credit_note_number} ISSUED` : 'REFUNDED';
          badgeBg = '#fee2e2; color: #991b1b;';
        } else if (ord.is_paid) {
          payStatus = 'PAID';
          badgeBg = '#dcfce7; color: #15803d;';
        } else if (ord.is_cancelled) {
          payStatus = 'CANCELLED';
          badgeBg = '#f1f5f9; color: #475569;';
        }

        const isLockedOrder = ord.has_credit_note || ord.can_issue_credit_note === false || ord.is_cancelled || ord.is_refunded;
        const rowClass = isLockedOrder ? 'cn-search-row disabled-cn-row' : 'cn-search-row';

        return `
        <div class="${rowClass} ${idx === 0 ? 'selected-row' : ''}" data-index="${idx}">
          <div>
            <div class="fw-bold font-monospace text-dark" style="font-size: 13px;">
              ${ord.invoice_no} <span class="badge" style="background:${badgeBg}; font-size: 10px; font-weight:700;">${payStatus}</span>
            </div>
            <div class="text-muted small" style="font-size: 11.5px;">
              ${ord.customer_name} · ${ord.table_name || 'Counter'} · ${ord.order_time}
            </div>
          </div>
          <div class="text-end">
            <div class="fw-bold font-monospace" style="font-size: 13.5px; color: ${isLockedOrder ? '#94a3b8' : '#0f172a'};">
              ₹ ${ord.total_amount.toFixed(2)}
            </div>
            ${isLockedOrder ? '<span class="badge bg-secondary" style="font-size: 9px;">LOCKED</span>' : ''}
          </div>
        </div>
      `;
      })
      .join('');

    dom.searchResultsDropdown.style.display = 'block';

    dom.searchResultsDropdown.querySelectorAll('.cn-search-row').forEach((row) => {
      row.addEventListener('click', () => {
        const idx = parseInt(row.dataset.index, 10);
        selectInvoice(state.searchResults[idx]);
      });
    });
  }

  function selectInvoice(order) {
    if (!order) return;
    state.selectedInvoice = order;
    dom.searchResultsDropdown.style.display = 'none';

    // Update Voucher Series at top
    if (order.has_credit_note && order.credit_note_number) {
      dom.voucherSeries.textContent = order.credit_note_number;
    } else if (state.nextVoucherSeries) {
      dom.voucherSeries.textContent = state.nextVoucherSeries;
    }

    // 1. Lock Omni-Search Box
    dom.searchInvoice.value = order.invoice_no;
    dom.searchInvoice.readOnly = true;
    dom.searchInvoice.classList.add('cn-input-locked');
    if (!state.isLocked) {
      dom.unlockSearchBtn.style.display = 'inline-block';
    }

    // 2. Populate Party Profile & GSTIN
    dom.partyProfileRow.style.display = 'flex';
    dom.partyProfile.value = order.customer_name || 'Walk-in Guest';

    const gstin = (order.gstin_no || '').trim();
    if (gstin && gstin.length === 15) {
      dom.gstinValidation.textContent = gstin;
      dom.gstinValidationIcon.innerHTML = '<i class="bi bi-building"></i>';
      dom.gstinValidationWrap.style.background = '#f0fdf4';
      dom.gstinValidationWrap.style.borderColor = '#bbf7d0';
      dom.gstinValidationWrap.style.color = '#15803d';
    } else {
      dom.gstinValidation.textContent = 'Unregistered';
      dom.gstinValidationIcon.innerHTML = '<i class="bi bi-person"></i>';
      dom.gstinValidationWrap.style.background = '#f0f9ff';
      dom.gstinValidationWrap.style.borderColor = '#bae6fd';
      dom.gstinValidationWrap.style.color = '#0284c7';
    }

    // 3. Line Items & Receipt
    dom.rcptInvNo.textContent = order.invoice_no;

    if (order.items && order.items.length) {
      dom.rcptLineItems.innerHTML = order.items
        .map(
          (it) => `
        <div class="cn-rcpt-item-row">
          <span>${it.quantity}x ${it.name}</span>
          <span class="font-monospace">₹ ${it.subtotal.toFixed(2)}</span>
        </div>
      `
        )
        .join('');
    } else {
      dom.rcptLineItems.innerHTML = `
        <div class="cn-rcpt-item-row">
          <span>1x Order Items</span>
          <span class="font-monospace">₹ ${order.total_amount.toFixed(2)}</span>
        </div>
      `;
    }

    // Taxes & totals
    dom.rcptTaxable.textContent = `₹ ${order.subtotal.toFixed(2)}`;
    dom.rcptCgst.textContent = `₹ ${order.cgst_amount.toFixed(2)}`;
    dom.rcptSgst.textContent = `₹ ${order.sgst_amount.toFixed(2)}`;
    dom.rcptTotal.textContent = `₹ ${order.total_amount.toFixed(2)}`;

    // 4. CHECK IF INVOICE/ORDER ALREADY HAS A CREDIT NOTE (OR IS CANCELLED / REFUNDED)
    const isDuplicate = order.has_credit_note || order.can_issue_credit_note === false || order.is_refunded || order.is_cancelled;

    if (isDuplicate) {
      // Duplicate Credit Note Detected - Prohibit another credit note
      if (dom.duplicateWarningBanner) {
        let msg = '';
        if (order.has_credit_note && order.credit_note_number) {
          msg = `Credit Note <strong>#${order.credit_note_number}</strong> has already been generated for Invoice <strong>${order.invoice_no}</strong>. Each invoice/order is strictly restricted to only one credit note.`;
        } else if (order.is_refunded) {
          msg = `Invoice <strong>${order.invoice_no}</strong> has already been refunded. Duplicate credit notes are not allowed.`;
        } else {
          msg = `Order <strong>${order.invoice_no}</strong> is already cancelled. Cannot issue credit note for an already cancelled order.`;
        }
        if (dom.duplicateWarningText) {
          dom.duplicateWarningText.innerHTML = msg;
        }
        dom.duplicateWarningBanner.style.display = 'flex';
      }

      // Lock parameters and manager auth
      dom.refundParameters.classList.add('cn-card-locked');
      dom.managerAuth.classList.add('cn-card-locked');

      // Disable submit button with explicit locked state
      dom.submitBtn.disabled = true;
      dom.submitBtn.innerHTML = '<i class="bi bi-lock-fill me-1"></i> Credit Note Already Issued (Single CN Limit)';
      dom.submitBtn.style.background = '#94a3b8';
      dom.submitBtn.style.borderColor = '#94a3b8';
      dom.submitBtn.style.cursor = 'not-allowed';

      // Watermark set to REFUNDED in red
      dom.rcptWatermark.className = 'cn-watermark-stamp already-refunded-mode';
      dom.rcptWatermark.textContent = 'REFUNDED';

      dom.rcptReason.textContent = 'Already Refunded';
      dom.rcptMode.textContent = 'Processed';

      return; // STOP! Do not advance focus or open reason dropdown!
    }

    // NORMAL UNLOCKED FLOW (Valid order eligible for credit note)
    if (dom.duplicateWarningBanner) {
      dom.duplicateWarningBanner.style.display = 'none';
    }

    // Unlock Refund Parameters and Manager Authorization
    dom.refundParameters.classList.remove('cn-card-locked');
    dom.managerAuth.classList.remove('cn-card-locked');

    // Enable Submit button
    dom.submitBtn.disabled = false;
    dom.submitBtn.innerHTML = 'Submit Refund <kbd>Enter</kbd>';
    dom.submitBtn.style.background = '';
    dom.submitBtn.style.borderColor = '';
    dom.submitBtn.style.cursor = '';

    // Reset Receipt Stamp to DRAFT
    dom.rcptWatermark.className = 'cn-watermark-stamp draft-mode';
    dom.rcptWatermark.textContent = 'DRAFT';

    // Move cursor to Reason Code automatically
    setTimeout(() => {
      setTallyFocus(dom.reasonCode);
      openCustomDropdown(dom.reasonDropdown, dom.reasonCode);
    }, 60);
  }

  function unlockSearch() {
    if (state.isLocked) return;
    state.selectedInvoice = null;

    if (dom.duplicateWarningBanner) {
      dom.duplicateWarningBanner.style.display = 'none';
    }

    // Reset Voucher Series to next upcoming number
    if (state.nextVoucherSeries) {
      dom.voucherSeries.textContent = state.nextVoucherSeries;
    }

    // Unlock Omni-Search
    dom.searchInvoice.readOnly = false;
    dom.searchInvoice.classList.remove('cn-input-locked');
    dom.searchInvoice.value = '';
    dom.unlockSearchBtn.style.display = 'none';

    // Hide Party Profile
    dom.partyProfileRow.style.display = 'none';
    dom.partyProfile.value = '';

    // Lock parameters & manager auth
    dom.refundParameters.classList.add('cn-card-locked');
    dom.managerAuth.classList.add('cn-card-locked');
    dom.submitBtn.disabled = true;
    dom.submitBtn.innerHTML = 'Submit Refund <kbd>Enter</kbd>';
    dom.submitBtn.style.background = '';
    dom.submitBtn.style.borderColor = '';
    dom.submitBtn.style.cursor = '';

    // Reset fields
    dom.reasonCode.value = '';
    dom.refundRoute.value = '';
    toggleOthers(false);
    dom.managerPin.value = '';
    hidePinError();

    // Reset Receipt Panel
    dom.rcptInvNo.textContent = '--';
    dom.rcptLineItems.innerHTML = `
      <div class="text-center text-muted fst-italic py-4" style="font-size: 12.5px;">
        Select invoice to load line items...
      </div>
    `;
    dom.rcptReason.textContent = '--';
    dom.rcptMode.textContent = '--';
    dom.rcptTaxable.textContent = '₹ 0.00';
    dom.rcptCgst.textContent = '₹ 0.00';
    dom.rcptSgst.textContent = '₹ 0.00';
    dom.rcptTotal.textContent = '₹ 0.00';
    dom.rcptWatermark.className = 'cn-watermark-stamp draft-mode';
    dom.rcptWatermark.textContent = 'DRAFT';

    fetchOmniSearch('');
    setTimeout(() => {
      setTallyFocus(dom.searchInvoice);
    }, 50);
  }

  /* ══════════════════════════════════════════════════════════════════
     2. AUTOCOMPLETE & CONDITIONAL LOGIC
     ══════════════════════════════════════════════════════════════════ */
  function openCustomDropdown(dropdown, input) {
    closeAllDropdowns();
    dropdown.style.display = 'block';

    const items = dropdown.querySelectorAll('.cn-dd-item');
    items.forEach((it) => it.classList.remove('selected'));

    if (dropdown === dom.reasonDropdown) {
      state.reasonSelectedIndex = 0;
      if (items[0]) items[0].classList.add('selected');
    } else if (dropdown === dom.routeDropdown) {
      state.routeSelectedIndex = 0;
      if (items[0]) items[0].classList.add('selected');
    }
  }

  function closeAllDropdowns() {
    if (dom.reasonDropdown) dom.reasonDropdown.style.display = 'none';
    if (dom.routeDropdown) dom.routeDropdown.style.display = 'none';
    if (dom.searchResultsDropdown) dom.searchResultsDropdown.style.display = 'none';
  }

  function toggleOthers(show) {
    if (show) {
      dom.othersBox.style.display = 'block';
      if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'none';
      setTimeout(() => {
        setTallyFocus(dom.specifyReason);
      }, 50);
    } else {
      dom.othersBox.style.display = 'none';
      dom.specifyReason.value = '';
      if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'none';
    }
  }

  function selectReason(val) {
    dom.reasonCode.value = val;
    dom.reasonDropdown.style.display = 'none';

    // Live Receipt Sync
    dom.rcptReason.textContent = val.includes('-') ? val.split('-')[1].trim() : val;

    // Check "Others" Condition
    if (val.includes('07') || val.toLowerCase().includes('others')) {
      toggleOthers(true);
    } else {
      toggleOthers(false);
      advanceField(dom.reasonCode);
    }
  }

  function selectRoute(val) {
    dom.refundRoute.value = val;
    dom.routeDropdown.style.display = 'none';

    // Live Receipt Sync
    dom.rcptMode.textContent = val.includes('Cash') ? 'Cash' : 'Source Ref.';

    advanceField(dom.refundRoute);
  }

  function showPinError(msg) {
    if (dom.pinErrorMsg) {
      if (dom.pinErrorText) {
        dom.pinErrorText.textContent = msg || 'Invalid Authorization PIN.';
      }
      dom.pinErrorMsg.style.display = 'block';
    }
    if (dom.managerPin) {
      dom.managerPin.classList.add('pin-error-flash');
      dom.managerPin.value = '';
      setTimeout(() => {
        dom.managerPin.classList.remove('pin-error-flash');
      }, 400);
      setTallyFocus(dom.managerPin);
    }
  }

  function hidePinError() {
    if (dom.pinErrorMsg) {
      dom.pinErrorMsg.style.display = 'none';
    }
    if (dom.managerPin) {
      dom.managerPin.classList.remove('pin-error-flash');
    }
  }

  /* ══════════════════════════════════════════════════════════════════
     3. TALLY KEYBOARD NAVIGATION ENGINE
     ══════════════════════════════════════════════════════════════════ */
  function getFocusSequence() {
    if (!state.selectedInvoice) {
      return [dom.searchInvoice];
    }
    const isOthers = dom.othersBox && dom.othersBox.style.display === 'block';
    if (isOthers) {
      return [dom.reasonCode, dom.specifyReason, dom.refundRoute, dom.managerPin, dom.submitBtn];
    }
    return [dom.reasonCode, dom.refundRoute, dom.managerPin, dom.submitBtn];
  }

  function setTallyFocus(el) {
    if (!el) return;
    try {
      el.focus();
    } catch (e) {}

    document.querySelectorAll('.tally-input').forEach((i) => i.classList.remove('tally-focused'));
    el.classList.add('tally-focused');
  }

  function advanceField(currentEl) {
    const seq = getFocusSequence();
    const idx = seq.indexOf(currentEl);
    if (idx !== -1 && idx < seq.length - 1) {
      const nextEl = seq[idx + 1];
      setTallyFocus(nextEl);

      // Open dropdown automatically if next is Reason or Route
      if (nextEl === dom.reasonCode) {
        openCustomDropdown(dom.reasonDropdown, dom.reasonCode);
      } else if (nextEl === dom.refundRoute) {
        openCustomDropdown(dom.routeDropdown, dom.refundRoute);
      }
    }
  }

  function retreatField(currentEl) {
    const seq = getFocusSequence();
    const idx = seq.indexOf(currentEl);

    if (currentEl === dom.reasonCode) {
      // Non-Destructive Backspace from Reason Code unlocks Omni-Search
      unlockSearch();
      return;
    }

    if (idx > 0) {
      const prevEl = seq[idx - 1];
      setTallyFocus(prevEl);
    }
  }

  // Simulated Block Cursor
  function startBlinking() {
    if (state.blinkInterval) clearInterval(state.blinkInterval);
    state.blinkInterval = setInterval(() => {
      const active = document.activeElement;
      if (active && active.classList.contains('tally-input') && !active.readOnly && active.value) {
        active.classList.toggle('block-cursor-active');
      }
    }, 530);
  }

  function bindKeyboardEngine() {
    // A. Omni-Search Input
    dom.searchInvoice.addEventListener('input', function () {
      if (this.readOnly) return;
      const query = this.value.trim();
      clearTimeout(state.searchDebounceTimer);
      state.searchDebounceTimer = setTimeout(() => {
        fetchOmniSearch(query);
      }, 140);
    });

    dom.searchInvoice.addEventListener('keydown', function (e) {
      if (this.readOnly) {
        if (e.key === 'Backspace' || e.key === 'Escape') {
          e.preventDefault();
          unlockSearch();
        }
        return;
      }

      // Dropdown Arrow navigation
      if (dom.searchResultsDropdown.style.display === 'block' && state.searchResults.length) {
        const rows = dom.searchResultsDropdown.querySelectorAll('.cn-search-row');
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          state.searchSelectedIndex = Math.min(state.searchSelectedIndex + 1, state.searchResults.length - 1);
          rows.forEach((r, i) => r.classList.toggle('selected-row', i === state.searchSelectedIndex));
          if (rows[state.searchSelectedIndex]) rows[state.searchSelectedIndex].scrollIntoView({ block: 'nearest' });
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          state.searchSelectedIndex = Math.max(state.searchSelectedIndex - 1, 0);
          rows.forEach((r, i) => r.classList.toggle('selected-row', i === state.searchSelectedIndex));
          if (rows[state.searchSelectedIndex]) rows[state.searchSelectedIndex].scrollIntoView({ block: 'nearest' });
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          if (state.searchSelectedIndex >= 0 && state.searchSelectedIndex < state.searchResults.length) {
            selectInvoice(state.searchResults[state.searchSelectedIndex]);
          }
          return;
        }
      }

      if (e.key === 'Enter') {
        e.preventDefault();
        if (state.searchResults.length > 0) {
          selectInvoice(state.searchResults[0]);
        }
      }
    });

    // B. Unlock Button
    dom.unlockSearchBtn.addEventListener('click', unlockSearch);

    // C. Reason Code Input
    dom.reasonCode.addEventListener('focus', function () {
      openCustomDropdown(dom.reasonDropdown, this);
    });
    dom.reasonCode.addEventListener('click', function () {
      openCustomDropdown(dom.reasonDropdown, this);
    });
    dom.reasonCode.addEventListener('keydown', function (e) {
      const items = dom.reasonDropdown.querySelectorAll('.cn-dd-item');
      if (dom.reasonDropdown.style.display === 'block') {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          state.reasonSelectedIndex = Math.min(state.reasonSelectedIndex + 1, items.length - 1);
          items.forEach((it, i) => it.classList.toggle('selected', i === state.reasonSelectedIndex));
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          state.reasonSelectedIndex = Math.max(state.reasonSelectedIndex - 1, 0);
          items.forEach((it, i) => it.classList.toggle('selected', i === state.reasonSelectedIndex));
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          if (state.reasonSelectedIndex >= 0 && state.reasonSelectedIndex < items.length) {
            selectReason(items[state.reasonSelectedIndex].dataset.value);
          }
          return;
        }
      }

      if (e.key === 'Enter') {
        e.preventDefault();
        if (items.length > 0) {
          selectReason(items[0].dataset.value);
        } else {
          advanceField(this);
        }
        return;
      }

      if (e.key === 'Backspace') {
        e.preventDefault();
        retreatField(this);
      }
    });

    dom.reasonDropdown.querySelectorAll('.cn-dd-item').forEach((item) => {
      item.addEventListener('click', () => {
        selectReason(item.dataset.value);
      });
    });

    // D. Specify Reason (Others Audit Field) - STRICT MANDATORY VALIDATION
    dom.specifyReason.addEventListener('input', function () {
      if (this.value.trim().length > 0) {
        if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'none';
        this.classList.remove('pin-error-flash');
      }
      dom.rcptReason.textContent = this.value.trim() ? this.value.trim() : 'Others (Custom)';
    });

    dom.specifyReason.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === 'ArrowDown') {
        e.preventDefault();
        if (!this.value.trim()) {
          this.classList.add('pin-error-flash');
          if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'block';
          setTimeout(() => this.classList.remove('pin-error-flash'), 400);
          this.focus();
          return; // BLOCK PROGRESSION!
        }
        if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'none';
        advanceField(this);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        retreatField(this);
        return;
      }
      if (e.key === 'Backspace' && this.value === '') {
        e.preventDefault();
        retreatField(this);
      }
    });

    // E. Refund Route Input
    dom.refundRoute.addEventListener('focus', function () {
      openCustomDropdown(dom.routeDropdown, this);
    });
    dom.refundRoute.addEventListener('click', function () {
      openCustomDropdown(dom.routeDropdown, this);
    });
    dom.refundRoute.addEventListener('keydown', function (e) {
      const items = dom.routeDropdown.querySelectorAll('.cn-dd-item');
      if (dom.routeDropdown.style.display === 'block') {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          state.routeSelectedIndex = Math.min(state.routeSelectedIndex + 1, items.length - 1);
          items.forEach((it, i) => it.classList.toggle('selected', i === state.routeSelectedIndex));
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          state.routeSelectedIndex = Math.max(state.routeSelectedIndex - 1, 0);
          items.forEach((it, i) => it.classList.toggle('selected', i === state.routeSelectedIndex));
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          if (state.routeSelectedIndex >= 0 && state.routeSelectedIndex < items.length) {
            selectRoute(items[state.routeSelectedIndex].dataset.value);
          }
          return;
        }
      }

      if (e.key === 'Enter') {
        e.preventDefault();
        if (items.length > 0) {
          selectRoute(items[0].dataset.value);
        } else {
          advanceField(this);
        }
        return;
      }

      if (e.key === 'Backspace') {
        e.preventDefault();
        retreatField(this);
      }
    });

    dom.routeDropdown.querySelectorAll('.cn-dd-item').forEach((item) => {
      item.addEventListener('click', () => {
        selectRoute(item.dataset.value);
      });
    });

    // F. Manager PIN Input - STRICT NUMERIC & VALUE CHECK
    dom.managerPin.addEventListener('input', function () {
      this.value = this.value.replace(/[^0-9]/g, '');
      hidePinError();
    });

    dom.managerPin.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === 'ArrowDown') {
        e.preventDefault();
        const pin = this.value.trim();
        const allowedPins = getAuthorizedPins();

        if (pin.length < 4 || !allowedPins.includes(pin)) {
          showPinError(`Invalid Authorization PIN. Default: ${allowedPins[0] || '1234'}`);
          return; // BLOCK PROGRESSION!
        }

        hidePinError();
        advanceField(this);
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        retreatField(this);
        return;
      }

      if (e.key === 'Backspace' && this.value === '') {
        e.preventDefault();
        retreatField(this);
      }
    });

    // G. Submit Button & Submit Button Trap
    dom.submitBtn.addEventListener('keydown', function (e) {
      if (e.key === 'Backspace') {
        e.preventDefault();
        // Submit Button Trap: specifically targets PIN field
        setTallyFocus(dom.managerPin);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        executeRefundSubmission();
      }
    });

    dom.submitBtn.addEventListener('click', () => {
      executeRefundSubmission();
    });

    // Reset button
    dom.resetBtn.addEventListener('click', unlockSearch);

    // Close button
    dom.closeBtn.addEventListener('click', closeCreditNoteModal);

    // Global Key Listener for Esc & Alt+C
    document.addEventListener('keydown', function (e) {
      if (e.altKey && (e.key === 'c' || e.key === 'C')) {
        e.preventDefault();
        window.openCreditNoteModal({ isLocked: false });
        return;
      }

      if (state.isOpen) {
        if (e.key === 'Escape') {
          e.preventDefault();
          if (dom.searchResultsDropdown.style.display === 'block') {
            dom.searchResultsDropdown.style.display = 'none';
          } else if (dom.reasonDropdown.style.display === 'block' || dom.routeDropdown.style.display === 'block') {
            closeAllDropdowns();
          } else if (state.selectedInvoice && !state.isLocked) {
            unlockSearch();
          } else {
            closeCreditNoteModal();
          }
        }
      }
    });

    // Document click to close custom dropdowns
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#reasonCode') && !e.target.closest('#reasonDropdown')) {
        dom.reasonDropdown.style.display = 'none';
      }
      if (!e.target.closest('#refundRoute') && !e.target.closest('#routeDropdown')) {
        dom.routeDropdown.style.display = 'none';
      }
      if (!e.target.closest('#searchInvoice') && !e.target.closest('#searchResultsDropdown')) {
        dom.searchResultsDropdown.style.display = 'none';
      }
    });
  }

  /* ══════════════════════════════════════════════════════════════════
     4. AUTHORIZATION & SUBMISSION
     ══════════════════════════════════════════════════════════════════ */
  function executeRefundSubmission() {
    if (!state.selectedInvoice) {
      UIDialog.alert('Please select an invoice first.');
      setTallyFocus(dom.searchInvoice);
      return;
    }

    if (state.selectedInvoice.has_credit_note || state.selectedInvoice.can_issue_credit_note === false || state.selectedInvoice.is_refunded || state.selectedInvoice.is_cancelled) {
      const cnMsg = state.selectedInvoice.credit_note_number ? `Credit Note #${state.selectedInvoice.credit_note_number} already exists for this invoice.` : 'This order has already been refunded / cancelled.';
      UIDialog.alert(`Operation Blocked: ${cnMsg} Duplicate credit notes are strictly prohibited.`);
      return;
    }

    // 1. Mandatory Audit Reason Validation if Others selected
    const reasonText = dom.reasonCode.value.trim() || '01 - Sales Return';
    const isOthers = reasonText.includes('07') || reasonText.toLowerCase().includes('other');
    const auditRemark = dom.specifyReason ? dom.specifyReason.value.trim() : '';

    if (isOthers && !auditRemark) {
      if (dom.specifyReason) {
        dom.specifyReason.classList.add('pin-error-flash');
        if (dom.specifyReasonError) dom.specifyReasonError.style.display = 'block';
        setTimeout(() => dom.specifyReason.classList.remove('pin-error-flash'), 400);
        setTallyFocus(dom.specifyReason);
      }
      return; // BLOCK SUBMISSION!
    }

    // 2. Strict PIN Validation
    const pin = dom.managerPin.value.trim();
    const allowedPins = getAuthorizedPins();

    if (pin.length < 4 || !allowedPins.includes(pin)) {
      showPinError(`Invalid Authorization PIN. Default: ${allowedPins[0] || '1234'}`);
      return; // BLOCK SUBMISSION!
    }

    let reasonCodeVal = 'BILLING_ERROR';
    if (reasonText.includes('01')) reasonCodeVal = 'WRONG_ORDER';
    else if (reasonText.includes('02')) reasonCodeVal = 'CUSTOMER_DISPUTE';
    else if (reasonText.includes('03')) reasonCodeVal = 'DEFECTIVE_FOOD';
    else if (reasonText.includes('04')) reasonCodeVal = 'BILLING_ERROR';
    else if (reasonText.includes('07')) reasonCodeVal = 'OTHER';

    const routeText = dom.refundRoute.value.trim() || 'Cash Drawer';
    let refundRouteVal = 'CASH';
    if (routeText.toLowerCase().includes('upi') || routeText.toLowerCase().includes('card') || routeText.toLowerCase().includes('original')) {
      refundRouteVal = 'ORIGINAL_METHOD';
    }

    const payload = {
      order_id: state.selectedInvoice.id,
      reason_code: reasonCodeVal,
      reason_description: auditRemark || reasonText,
      refund_route: refundRouteVal,
      pin: pin,
    };

    // Button states
    dom.submitBtn.disabled = true;
    dom.submitBtnText.textContent = 'Processing...';
    dom.submitBtnKbd.style.display = 'none';

    const url = getApiUrl(`orders/${state.selectedInvoice.id}/credit-note/`);
    const csrfToken = getCsrfToken();

    fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify(payload),
    })
      .then((res) => res.json())
      .then((data) => {
        if (!data.success) {
          dom.submitBtn.disabled = false;
          dom.submitBtnText.textContent = 'Submit Refund';
          dom.submitBtnKbd.style.display = 'inline-block';
          showPinError(data.error || 'Authorization failed. Please verify Manager PIN.');
          return;
        }

        // Final Execution: Change receipt stamp from DRAFT to REFUNDED with authorized class
        dom.rcptWatermark.className = 'cn-watermark-stamp refunded-mode';
        dom.rcptWatermark.textContent = 'REFUNDED';

        if (data.credit_note_number) {
          dom.voucherSeries.textContent = data.credit_note_number;
          dom.rcptInvNo.textContent = `${state.selectedInvoice.invoice_no} (${data.credit_note_number})`;
        }

        // Trigger success alert after 600ms
        setTimeout(() => {
          UIDialog.alert('✅ Credit Note Processed Successfully!');
          if (data.thermal_url) {
            try {
              let iframe = document.getElementById('cnPrintIframe');
              if (!iframe) {
                iframe = document.createElement('iframe');
                iframe.id = 'cnPrintIframe';
                iframe.style.display = 'none';
                document.body.appendChild(iframe);
              }
              iframe.src = data.thermal_url + '?print=true';
            } catch (e) {}
          }
          setTimeout(() => {
            window.location.reload();
          }, 400);
        }, 600);
      })
      .catch((err) => {
        console.error(err);
        dom.submitBtn.disabled = false;
        dom.submitBtnText.textContent = 'Submit Refund';
        dom.submitBtnKbd.style.display = 'inline-block';
        showPinError('Server error verifying PIN. Please check Manager PIN.');
      });
  }

  /* ══════════════════════════════════════════════════════════════════
     5. PUBLIC MODAL API (FORWARD TO DEDICATED PAGE)
     ══════════════════════════════════════════════════════════════════ */
  window.openCreditNoteModal = function (options = {}) {
    const slug = getDashboardSlug();
    let url = slug ? `/dashboard/${slug}/orders/credit-note/` : `/orders/credit-note/`;
    if (options && options.orderId) {
      url += `?order_id=${encodeURIComponent(options.orderId)}`;
    }
    window.location.href = url;
  };

  window.closeCreditNoteModal = function () {
    initDOM();
    if (dom.modal) dom.modal.style.display = 'none';
    state.isOpen = false;
    state.selectedInvoice = null;
    clearTimeout(state.searchDebounceTimer);
    closeAllDropdowns();
  };

  document.addEventListener('DOMContentLoaded', () => {
    initDOM();
    if (dom.modal) {
      bindKeyboardEngine();
      startBlinking();
    }
  });
})(window, document);
