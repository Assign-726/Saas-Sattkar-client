/**
 * TALLY-INSPIRED CREDIT NOTE & ORDER CANCELLATION ENGINE
 * High-speed, keyboard-driven POS module with DOM navigation,
 * block-cursor auto-overwrite, non-destructive backspace, and live thermal sync.
 */

(function (window, document) {
  'use strict';

  // Module State
  const state = {
    isOpen: false,
    isLocked: false,
    selectedOrder: null,
    searchResults: [],
    searchSelectedIndex: -1,
    activeFieldIndex: 0,
    searchDebounceTimer: null,
  };

  // DOM Elements Cache
  let dom = {};

  function initDOM() {
    dom = {
      modal: document.getElementById('tallyCreditNoteModal'),
      closeBtn: document.getElementById('cnModalCloseBtn'),
      closeFooterBtn: document.getElementById('cnCloseFooterBtn'),
      modeBadge: document.getElementById('cnModeBadge'),
      omniSearch: document.getElementById('cnOmniSearch'),
      searchWrap: document.getElementById('cnSearchWrap'),
      searchResults: document.getElementById('cnSearchResults'),
      unlockSearchBtn: document.getElementById('cnUnlockSearchBtn'),
      searchStatusCue: document.getElementById('cnSearchStatusCue'),
      orderSummaryCard: document.getElementById('cnOrderSummaryCard'),
      sumOrderNum: document.getElementById('cnSumOrderNum'),
      sumCustomer: document.getElementById('cnSumCustomer'),
      sumTotal: document.getElementById('cnSumTotal'),
      sumPayBadge: document.getElementById('cnSumPayBadge'),
      sumSubtotal: document.getElementById('cnSumSubtotal'),
      sumCgst: document.getElementById('cnSumCgst'),
      sumSgst: document.getElementById('cnSumSgst'),
      sumDiscWrap: document.getElementById('cnSumDiscWrap'),
      sumDisc: document.getElementById('cnSumDisc'),
      flowBanner: document.getElementById('cnFlowBanner'),
      flowIcon: document.getElementById('cnFlowIcon'),
      flowText: document.getElementById('cnFlowText'),
      reasonCode: document.getElementById('cnReasonCode'),
      reasonDesc: document.getElementById('cnReasonDesc'),
      refundRouteGroup: document.getElementById('cnRefundRouteGroup'),
      refundRoute: document.getElementById('cnRefundRoute'),
      authPin: document.getElementById('cnAuthPin'),
      pinError: document.getElementById('cnPinError'),
      submitBtn: document.getElementById('cnSubmitBtn'),
      submitBtnLabel: document.getElementById('cnSubmitBtnLabel'),
      // Thermal Preview Elements
      thBranchName: document.getElementById('thBranchName'),
      thGstin: document.getElementById('thGstin'),
      thDocTitle: document.getElementById('thDocTitle'),
      thDocNo: document.getElementById('thDocNo'),
      thDate: document.getElementById('thDate'),
      thOrigInv: document.getElementById('thOrigInv'),
      thCustomer: document.getElementById('thCustomer'),
      thTable: document.getElementById('thTable'),
      thItemsList: document.getElementById('thItemsList'),
      thSubtotalAdj: document.getElementById('thSubtotalAdj'),
      thDiscRow: document.getElementById('thDiscRow'),
      thDiscAdj: document.getElementById('thDiscAdj'),
      thCgstRate: document.getElementById('thCgstRate'),
      thCgstAdj: document.getElementById('thCgstAdj'),
      thSgstRate: document.getElementById('thSgstRate'),
      thSgstAdj: document.getElementById('thSgstAdj'),
      thVatRow: document.getElementById('thVatRow'),
      thVatAdj: document.getElementById('thVatAdj'),
      thServiceRow: document.getElementById('thServiceRow'),
      thServiceAdj: document.getElementById('thServiceAdj'),
      thTotalLabel: document.getElementById('thTotalLabel'),
      thTotalAmount: document.getElementById('thTotalAmount'),
      thRouteRow: document.getElementById('thRouteRow'),
      thRouteVal: document.getElementById('thRouteVal'),
      thReasonVal: document.getElementById('thReasonVal'),
      thFooterNotice: document.getElementById('thFooterNotice'),
      printSuccessBar: document.getElementById('cnPrintSuccessBar'),
      printThermalLink: document.getElementById('cnPrintThermalLink'),
      successMsg: document.getElementById('cnSuccessMsg'),
    };
  }

  /**
   * Get active logical navigation sequence based on whether order is paid or unpaid
   */
  function getFocusSequence() {
    if (!state.selectedOrder) {
      return [dom.omniSearch];
    }
    const isPaid = state.selectedOrder.is_paid;
    if (state.isLocked) {
      // Locked contextual entry: search is skipped
      return isPaid
        ? [dom.reasonCode, dom.reasonDesc, dom.refundRoute, dom.authPin, dom.submitBtn]
        : [dom.reasonCode, dom.reasonDesc, dom.authPin, dom.submitBtn];
    } else {
      // Direct entry: search is step 0
      return isPaid
        ? [dom.omniSearch, dom.reasonCode, dom.reasonDesc, dom.refundRoute, dom.authPin, dom.submitBtn]
        : [dom.omniSearch, dom.reasonCode, dom.reasonDesc, dom.authPin, dom.submitBtn];
    }
  }

  /**
   * Move DOM focus to a field with Tally Block-Cursor selection
   */
  function setTallyFocus(element, isAutoOverwrite = true) {
    if (!element) return;
    element.focus();

    // Visual Block-Cursor highlight
    element.classList.add('tally-block-cursor');

    // Auto-select entire text if text/search/pin input
    if (['text', 'search'].includes(element.type) && typeof element.select === 'function') {
      try {
        element.select();
      } catch (e) {}
    }

    if (isAutoOverwrite) {
      element.dataset.overwriteReady = 'true';
    }
  }

  /**
   * Step focus forward to the next logical element in the chain
   */
  function advanceField(currentElement) {
    const seq = getFocusSequence();
    const idx = seq.indexOf(currentElement);
    if (idx !== -1 && idx < seq.length - 1) {
      const nextEl = seq[idx + 1];
      setTallyFocus(nextEl, true);
    }
  }

  /**
   * Step focus backward to previous logical element without losing data
   */
  function retreatField(currentElement) {
    const seq = getFocusSequence();
    const idx = seq.indexOf(currentElement);

    if (currentElement === dom.reasonCode && state.isLocked) {
      // If contextual locked mode, cannot step back past reason code
      return;
    }

    if (currentElement === dom.reasonCode && !state.isLocked) {
      // Stepping back from Reason Code unlocks / refocuses OmniSearch
      dom.omniSearch.readOnly = false;
      dom.unlockSearchBtn.classList.add('d-none');
      setTallyFocus(dom.omniSearch, true);
      return;
    }

    if (idx > 0) {
      const prevEl = seq[idx - 1];
      setTallyFocus(prevEl, true);
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

  /**
   * Live Omni-Search Fetch
   */
  function fetchOmniSearch(query) {
    const url = getApiUrl(`api/orders/omni-search/?q=${encodeURIComponent(query || '')}`);
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then((res) => res.json())
      .then((data) => {
        if (data.success && Array.isArray(data.orders)) {
          state.searchResults = data.orders;
          renderSearchResults();
        }
      })
      .catch((err) => {
        console.error('Omni-search error:', err);
      });
  }

  /**
   * Render Search Results Dropdown
   */
  function renderSearchResults() {
    if (!state.searchResults.length) {
      dom.searchResults.innerHTML = `
        <div class="p-3 text-center text-muted small">
          <i class="bi bi-search me-1"></i> No matching orders found
        </div>
      `;
      dom.searchResults.style.display = 'block';
      return;
    }

    state.searchSelectedIndex = 0;
    const html = state.searchResults
      .map((ord, idx) => {
        const isPaid = ord.is_paid;
        const isCancelled = ord.is_cancelled;
        const payBadge = isPaid
          ? '<span class="badge bg-success-subtle text-success border border-success-subtle px-1.5">PAID</span>'
          : '<span class="badge bg-warning-subtle text-warning border border-warning-subtle px-1.5">UNPAID</span>';
        const cancelBadge = isCancelled
          ? '<span class="badge bg-danger-subtle text-danger border border-danger-subtle px-1.5 ms-1">CANCELLED</span>'
          : '';

        return `
        <div class="tally-search-item ${idx === 0 ? 'tally-selected-row' : ''}" data-index="${idx}">
          <div>
            <div class="fw-bold font-monospace text-dark" style="font-size: 0.85rem;">
              ${ord.invoice_no} <span class="text-muted fw-normal">(${ord.order_number})</span>
            </div>
            <div class="text-muted small" style="font-size: 0.76rem;">
              ${ord.customer_name} · ${ord.table_name} · ${ord.order_time}
            </div>
          </div>
          <div class="text-end">
            <div class="fw-bold font-monospace text-dark" style="font-size: 0.88rem;">₹${ord.total_amount.toFixed(2)}</div>
            <div class="mt-0.5">${payBadge}${cancelBadge}</div>
          </div>
        </div>
      `;
      })
      .join('');

    dom.searchResults.innerHTML = html;
    dom.searchResults.style.display = 'block';

    // Click binding on dropdown items
    dom.searchResults.querySelectorAll('.tally-search-item').forEach((item) => {
      item.addEventListener('click', () => {
        const idx = parseInt(item.dataset.index, 10);
        selectOrder(state.searchResults[idx]);
      });
    });
  }

  /**
   * Select an Order and Populate Form & Live Thermal Sync
   */
  function selectOrder(order) {
    if (!order) return;
    state.selectedOrder = order;
    dom.searchResults.style.display = 'none';

    // Pre-fill OmniSearch input with Invoice Number
    dom.omniSearch.value = `${order.invoice_no} (${order.order_number}) - ${order.customer_name}`;
    dom.omniSearch.readOnly = true;
    dom.unlockSearchBtn.classList.remove('d-none');

    // Populate Order Summary Card
    dom.sumOrderNum.textContent = `${order.invoice_no} (${order.order_number})`;
    dom.sumCustomer.textContent = `${order.customer_name} · ${order.table_name} · ${order.order_type}`;
    dom.sumTotal.textContent = `₹${order.total_amount.toFixed(2)}`;
    dom.sumSubtotal.textContent = `₹${order.subtotal.toFixed(2)}`;
    dom.sumCgst.textContent = `₹${order.cgst_amount.toFixed(2)}`;
    dom.sumSgst.textContent = `₹${order.sgst_amount.toFixed(2)}`;

    if (order.discount_amount > 0) {
      dom.sumDisc.textContent = `-₹${order.discount_amount.toFixed(2)}`;
      dom.sumDiscWrap.style.display = 'inline';
    } else {
      dom.sumDiscWrap.style.display = 'none';
    }

    if (order.is_paid) {
      dom.sumPayBadge.className = 'badge bg-success-subtle text-success border border-success-subtle px-2 py-0.5';
      dom.sumPayBadge.textContent = `PAID (${order.payment_method_display || 'Settled'})`;
    } else {
      dom.sumPayBadge.className = 'badge bg-warning-subtle text-warning border border-warning-subtle px-2 py-0.5';
      dom.sumPayBadge.textContent = `UNPAID (${order.payment_status_display})`;
    }
    dom.orderSummaryCard.style.display = 'block';

    // Route UI state based on Paid vs Unpaid
    applyOrderStateRouting(order);

    // Live Sync Thermal Receipt Preview
    syncThermalReceiptPreview();

    // Automatically drop DOM focus into Reason Code
    setTimeout(() => {
      setTallyFocus(dom.reasonCode, true);
    }, 50);
  }

  /**
   * Apply routing logic (Paid -> Credit Note & Refund vs Unpaid -> Void / Cancel)
   */
  function applyOrderStateRouting(order) {
    if (order.is_cancelled) {
      dom.flowBanner.className = 'alert alert-danger py-2 px-3 mb-3 d-flex align-items-center gap-2';
      dom.flowIcon.className = 'bi bi-x-circle-fill text-danger fs-6';
      dom.flowText.innerHTML = `<strong>Attention:</strong> This order has already been cancelled${
        order.has_credit_note ? ` (Credit Note: ${order.credit_note_number})` : ''
      }.`;
      dom.submitBtn.disabled = true;
      dom.submitBtnLabel.textContent = 'Order Already Cancelled';
      return;
    }

    dom.submitBtn.disabled = false;

    if (order.is_paid) {
      // 1. Paid Invoices -> Full Credit Note & Refund Flow
      dom.modeBadge.className = 'badge bg-danger-subtle text-danger border border-danger-subtle px-2 py-0.5 font-monospace';
      dom.modeBadge.textContent = 'PAID · CREDIT NOTE & REFUND';

      dom.flowBanner.className = 'alert alert-warning py-2 px-3 mb-3 d-flex align-items-center gap-2';
      dom.flowIcon.className = 'bi bi-cash-coin fs-6 text-warning';
      dom.flowText.innerHTML = `<strong>Paid Order:</strong> Generating full Credit Note with split CGST/SGST tax adjustments & customer refund.`;

      dom.refundRouteGroup.style.display = 'block';
      dom.submitBtnLabel.textContent = 'Cancel & Refund (Credit Note)';
      dom.submitBtn.className = 'btn btn-danger fw-bold px-4 shadow-sm tally-input';
    } else {
      // 2. Unpaid Invoices -> Void / Cancel Flow (No refund required)
      dom.modeBadge.className = 'badge bg-warning-subtle text-warning border border-warning-subtle px-2 py-0.5 font-monospace';
      dom.modeBadge.textContent = 'UNPAID · VOID ORDER ONLY';

      dom.flowBanner.className = 'alert alert-info py-2 px-3 mb-3 d-flex align-items-center gap-2';
      dom.flowIcon.className = 'bi bi-slash-circle-fill fs-6 text-info';
      dom.flowText.innerHTML = `<strong>Unpaid Order:</strong> Bypassing refund disbursement. Order will be voided and inventory released.`;

      dom.refundRouteGroup.style.display = 'none';
      dom.submitBtnLabel.textContent = 'Cancel / Void Order';
      dom.submitBtn.className = 'btn btn-warning fw-bold px-4 shadow-sm tally-input text-dark';
    }
  }

  /**
   * Live Sync Thermal Receipt Preview (80mm)
   */
  function syncThermalReceiptPreview() {
    const ord = state.selectedOrder;
    if (!ord) return;

    const isPaid = ord.is_paid;
    const reasonText = dom.reasonCode.options[dom.reasonCode.selectedIndex]?.text || 'Billing Error';
    const routeText = isPaid ? dom.refundRoute.options[dom.refundRoute.selectedIndex]?.text : 'N/A (Void)';

    dom.thDocTitle.textContent = isPaid ? '** CREDIT NOTE / REFUND **' : '** VOID ORDER SLIP **';
    dom.thDocNo.textContent = isPaid ? 'CN-PENDING' : 'VOID-SLIP';
    dom.thDate.textContent = new Date().toLocaleDateString('en-GB', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
    dom.thOrigInv.textContent = ord.invoice_no;
    dom.thCustomer.textContent = ord.customer_name || 'Walk-in Guest';
    dom.thTable.textContent = ord.table_name || 'Counter';

    // Render items
    if (ord.items && ord.items.length) {
      dom.thItemsList.innerHTML = ord.items
        .map(
          (it) => `
        <div class="d-flex justify-content-between my-0.5">
          <span style="width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${it.name}</span>
          <span style="width: 15%; text-align: center;">-${it.quantity}</span>
          <span style="width: 15%; text-align: right;">${it.unit_price.toFixed(2)}</span>
          <span style="width: 20%; text-align: right;">-${it.subtotal.toFixed(2)}</span>
        </div>
      `
        )
        .join('');
    } else {
      dom.thItemsList.innerHTML = '<div class="text-center text-muted py-1">Item list not available</div>';
    }

    // Split adjustments
    dom.thSubtotalAdj.textContent = `- ₹${ord.subtotal.toFixed(2)}`;
    if (ord.discount_amount > 0) {
      dom.thDiscRow.style.display = 'flex';
      dom.thDiscAdj.textContent = `+ ₹${ord.discount_amount.toFixed(2)}`;
    } else {
      dom.thDiscRow.style.display = 'none';
    }

    dom.thCgstRate.textContent = `${ord.cgst_percentage || 2.5}%`;
    dom.thCgstAdj.textContent = `- ₹${ord.cgst_amount.toFixed(2)}`;
    dom.thSgstRate.textContent = `${ord.sgst_percentage || 2.5}%`;
    dom.thSgstAdj.textContent = `- ₹${ord.sgst_amount.toFixed(2)}`;

    if (ord.vat_amount > 0) {
      dom.thVatRow.style.display = 'flex';
      dom.thVatAdj.textContent = `- ₹${ord.vat_amount.toFixed(2)}`;
    } else {
      dom.thVatRow.style.display = 'none';
    }

    if (ord.service_charge > 0) {
      dom.thServiceRow.style.display = 'flex';
      dom.thServiceAdj.textContent = `- ₹${ord.service_charge.toFixed(2)}`;
    } else {
      dom.thServiceRow.style.display = 'none';
    }

    if (isPaid) {
      dom.thTotalLabel.textContent = 'TOTAL REFUND:';
      dom.thTotalAmount.textContent = `₹${ord.total_amount.toFixed(2)}`;
      dom.thRouteRow.style.display = 'flex';
      dom.thRouteVal.textContent = routeText;
    } else {
      dom.thTotalLabel.textContent = 'VOID AMOUNT:';
      dom.thTotalAmount.textContent = `₹${ord.total_amount.toFixed(2)}`;
      dom.thRouteRow.style.display = 'none';
    }

    dom.thReasonVal.textContent = reasonText;
    dom.thFooterNotice.textContent = isPaid
      ? 'GST Credit Note generated per Rule 54 of CGST Rules.'
      : 'Unpaid order voided. Stock released.';
  }

  /**
   * Bind Tally Keyboard Engine Event Listeners
   */
  function bindKeyboardEngine() {
    // 1. Omni-Search Input Events
    dom.omniSearch.addEventListener('input', function (e) {
      if (dom.omniSearch.readOnly) return;
      const query = this.value.trim();
      clearTimeout(state.searchDebounceTimer);
      state.searchDebounceTimer = setTimeout(() => {
        fetchOmniSearch(query);
      }, 150);
    });

    dom.omniSearch.addEventListener('keydown', function (e) {
      // Overwrite check on first keystroke
      if (this.dataset.overwriteReady === 'true' && e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
        this.value = '';
        this.dataset.overwriteReady = 'false';
      }

      if (e.key === 'Tab') {
        e.preventDefault();
        advanceField(this);
        return;
      }

      // Suggestions dropdown arrow navigation
      if (dom.searchResults.style.display === 'block' && state.searchResults.length) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          state.searchSelectedIndex = Math.min(state.searchSelectedIndex + 1, state.searchResults.length - 1);
          highlightSearchSelection();
          return;
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          state.searchSelectedIndex = Math.max(state.searchSelectedIndex - 1, 0);
          highlightSearchSelection();
          return;
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (state.searchSelectedIndex >= 0 && state.searchSelectedIndex < state.searchResults.length) {
            selectOrder(state.searchResults[state.searchSelectedIndex]);
          }
          return;
        }
      }

      if (e.key === 'Enter' || e.key === 'ArrowDown') {
        e.preventDefault();
        if (state.selectedOrder) {
          advanceField(this);
        } else if (state.searchResults.length > 0) {
          selectOrder(state.searchResults[0]);
        }
      }
    });

    // 2. Unlock Search Button
    dom.unlockSearchBtn.addEventListener('click', () => {
      dom.omniSearch.readOnly = false;
      dom.unlockSearchBtn.classList.add('d-none');
      setTallyFocus(dom.omniSearch, true);
    });

    // 3. Reason Code Dropdown
    dom.reasonCode.addEventListener('change', syncThermalReceiptPreview);
    dom.reasonCode.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') {
        e.preventDefault();
        advanceField(this);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        // Strict dropdown validation: must have valid selection
        if (!this.value) {
          this.focus();
          return;
        }
        advanceField(this);
      } else if (e.key === 'Backspace') {
        e.preventDefault();
        retreatField(this);
      }
    });

    // 4. Reason Description
    dom.reasonDesc.addEventListener('keydown', function (e) {
      if (this.dataset.overwriteReady === 'true' && e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
        this.value = '';
        this.dataset.overwriteReady = 'false';
      }
      if (e.key === 'Tab') {
        e.preventDefault();
        advanceField(this);
        return;
      }
      if (e.key === 'Enter' || e.key === 'ArrowDown') {
        e.preventDefault();
        advanceField(this);
      } else if (e.key === 'Backspace') {
        e.preventDefault();
        retreatField(this);
      }
    });

    // 5. Refund Route Dropdown
    dom.refundRoute.addEventListener('change', syncThermalReceiptPreview);
    dom.refundRoute.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') {
        e.preventDefault();
        advanceField(this);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        if (!this.value) {
          this.focus();
          return;
        }
        advanceField(this);
      } else if (e.key === 'Backspace') {
        e.preventDefault();
        retreatField(this);
      }
    });

    // 6. Authorization PIN Input
    dom.authPin.addEventListener('input', function () {
      dom.pinError.style.display = 'none';
      this.dataset.overwriteReady = 'false';
    });

    dom.authPin.addEventListener('keydown', function (e) {
      // Block cursor auto-overwrite logic
      if (this.dataset.overwriteReady === 'true' && e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
        this.value = '';
        this.dataset.overwriteReady = 'false';
      }

      if (e.key === 'Tab') {
        e.preventDefault();
        advanceField(this);
        return;
      }

      if (e.key === 'Enter' || e.key === 'ArrowDown') {
        e.preventDefault();
        const pinVal = this.value.trim();
        if (pinVal.length >= 4) {
          // Move focus directly to Submit button
          setTallyFocus(dom.submitBtn, false);
        } else {
          // Highlight requirement
          this.focus();
          dom.pinError.textContent = 'Please enter a 4-digit Manager PIN.';
          dom.pinError.style.display = 'block';
        }
      } else if (e.key === 'Backspace') {
        // Non-destructive Backspace: does NOT delete data; steps back to previous field
        e.preventDefault();
        retreatField(this);
      }
    });

    // 7. Submit Action Button
    dom.submitBtn.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') {
        e.preventDefault();
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        executeCancellation();
      } else if (e.key === 'Backspace') {
        // Pressing Backspace on submit button returns focus to PIN field without deleting PIN
        e.preventDefault();
        setTallyFocus(dom.authPin, false);
      }
    });

    dom.submitBtn.addEventListener('click', () => {
      executeCancellation();
    });

    // Close buttons
    dom.closeBtn.addEventListener('click', closeCreditNoteModal);
    dom.closeFooterBtn.addEventListener('click', closeCreditNoteModal);

    // Global Key Listener for Esc & Alt+C
    document.addEventListener('keydown', function (e) {
      // Alt+C hotkey to open modal from anywhere
      if (e.altKey && (e.key === 'c' || e.key === 'C')) {
        e.preventDefault();
        window.openCreditNoteModal({ isLocked: false });
        return;
      }

      if (state.isOpen) {
        if (e.key === 'Escape') {
          e.preventDefault();
          closeCreditNoteModal();
        }
      }
    });

    // Focus / Blur styling helpers
    document.querySelectorAll('.tally-input').forEach((input) => {
      input.addEventListener('focus', function () {
        this.classList.add('tally-focused');
      });
      input.addEventListener('blur', function () {
        this.classList.remove('tally-focused', 'tally-block-cursor');
        this.dataset.overwriteReady = 'false';
      });
    });
  }

  function highlightSearchSelection() {
    const items = dom.searchResults.querySelectorAll('.tally-search-item');
    items.forEach((it, i) => {
      if (i === state.searchSelectedIndex) {
        it.classList.add('tally-selected-row');
        it.scrollIntoView({ block: 'nearest' });
      } else {
        it.classList.remove('tally-selected-row');
      }
    });
  }

  /**
   * Execute Backend Cancellation / Credit Note Payload
   */
  function executeCancellation() {
    if (!state.selectedOrder) {
      alert('Please select an order to cancel.');
      setTallyFocus(dom.omniSearch, true);
      return;
    }

    const pin = dom.authPin.value.trim();
    if (!pin) {
      dom.pinError.textContent = 'Please enter a 4-digit Manager Authorization PIN.';
      dom.pinError.style.display = 'block';
      setTallyFocus(dom.authPin, true);
      return;
    }

    const payload = {
      order_id: state.selectedOrder.id,
      reason_code: dom.reasonCode.value,
      reason_description: dom.reasonDesc.value.trim(),
      refund_route: dom.refundRoute.value,
      pin: pin,
    };

    dom.submitBtn.disabled = true;
    dom.submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Processing...';

    const url = getApiUrl(`orders/${state.selectedOrder.id}/credit-note/`);

    // CSRF token
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
        dom.submitBtn.disabled = false;

        if (!data.success) {
          dom.pinError.textContent = data.error || 'Operation failed. Please try again.';
          dom.pinError.style.display = 'block';
          dom.submitBtn.innerHTML = '<i class="bi bi-check2-circle me-1"></i> ' + dom.submitBtnLabel.textContent;
          setTallyFocus(dom.authPin, true);
          return;
        }

        // Successful execution!
        handleSuccess(data);
      })
      .catch((err) => {
        dom.submitBtn.disabled = false;
        dom.submitBtn.innerHTML = '<i class="bi bi-check2-circle me-1"></i> ' + dom.submitBtnLabel.textContent;
        alert('Network error communicating with server.');
        console.error(err);
      });
  }

  function handleSuccess(data) {
    dom.submitBtn.style.display = 'none';
    dom.pinError.style.display = 'none';

    if (data.mode === 'credit_note') {
      dom.successMsg.textContent = data.message || `Credit Note #${data.credit_note_number} generated!`;
      dom.printThermalLink.href = data.thermal_url;
      dom.printSuccessBar.style.display = 'block';

      // Update thermal preview with final Credit Note number
      dom.thDocNo.textContent = data.credit_note_number;

      // Auto-trigger printing in background iframe if desired
      printReceiptIframe(data.thermal_url);
    } else {
      dom.successMsg.textContent = data.message || 'Order voided and stock released successfully!';
      dom.printThermalLink.style.display = 'none';
      dom.printSuccessBar.style.display = 'block';
    }

    // Refresh page or broadcast update after 2.5 seconds if on order details page
    if (state.isLocked) {
      setTimeout(() => {
        window.location.reload();
      }, 2500);
    }
  }

  function printReceiptIframe(url) {
    try {
      let iframe = document.getElementById('cnPrintIframe');
      if (!iframe) {
        iframe = document.createElement('iframe');
        iframe.id = 'cnPrintIframe';
        iframe.style.display = 'none';
        document.body.appendChild(iframe);
      }
      iframe.src = url + '?print=true';
    } catch (e) {
      console.warn('Iframe print trigger failed:', e);
    }
  }

  function getCsrfToken() {
    const cookieValue = document.cookie
      .split('; ')
      .find((row) => row.startsWith('csrftoken='))
      ?.split('=')[1];
    return cookieValue || '';
  }

  /**
   * Public API: Open Credit Note Modal
   * @param {Object} options
   * @param {number|string} [options.orderId] Pre-filled order ID for Contextual Entry
   * @param {boolean} [options.isLocked=false] Whether invoice field is locked
   */
  window.openCreditNoteModal = function (options = {}) {
    initDOM();
    state.isOpen = true;
    state.isLocked = !!options.isLocked;
    state.selectedOrder = null;

    // Reset UI
    dom.modal.style.display = 'flex';
    dom.omniSearch.value = '';
    dom.omniSearch.readOnly = false;
    dom.unlockSearchBtn.classList.add('d-none');
    dom.searchResults.style.display = 'none';
    dom.orderSummaryCard.style.display = 'none';
    dom.refundRouteGroup.style.display = 'block';
    dom.authPin.value = '';
    dom.pinError.style.display = 'none';
    dom.printSuccessBar.style.display = 'none';
    dom.submitBtn.style.display = 'inline-block';
    dom.submitBtn.disabled = false;
    dom.reasonDesc.value = '';

    // Entry point handling
    if (options.orderId) {
      // 1. Contextual Entry: Pre-fill and lock
      dom.modeBadge.className = 'badge bg-primary-subtle text-primary border border-primary-subtle px-2 py-0.5 font-monospace';
      dom.modeBadge.textContent = 'CONTEXTUAL ENTRY';
      dom.omniSearch.value = `Loading Order #${options.orderId}...`;
      dom.omniSearch.readOnly = true;
      dom.unlockSearchBtn.classList.remove('d-none');

      // Fetch order details by ID
      const url = getApiUrl(`api/orders/omni-search/?order_id=${encodeURIComponent(options.orderId)}`);

      fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then((res) => res.json())
        .then((data) => {
          if (data.success && data.orders && data.orders.length) {
            const matched = data.orders.find((o) => o.id === parseInt(options.orderId, 10)) || data.orders[0];
            selectOrder(matched);
          } else {
            alert(`Could not load order #${options.orderId}`);
            closeCreditNoteModal();
          }
        })
        .catch((err) => {
          console.error(err);
          alert('Error fetching order details.');
        });
    } else {
      // 2. Direct Entry: Focus OmniSearch
      dom.modeBadge.className = 'badge bg-secondary-subtle text-secondary border px-2 py-0.5 font-monospace';
      dom.modeBadge.textContent = 'DIRECT ENTRY';
      fetchOmniSearch(''); // Load recent 15 orders immediately
      setTimeout(() => {
        setTallyFocus(dom.omniSearch, true);
      }, 50);
    }
  };

  /**
   * Public API: Close Modal
   */
  window.closeCreditNoteModal = function () {
    if (!dom.modal) initDOM();
    dom.modal.style.display = 'none';
    state.isOpen = false;
    state.selectedOrder = null;
    clearTimeout(state.searchDebounceTimer);
  };

  // Initialize once DOM is ready
  document.addEventListener('DOMContentLoaded', () => {
    initDOM();
    bindKeyboardEngine();
  });
})(window, document);
