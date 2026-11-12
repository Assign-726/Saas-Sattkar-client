// Stop the mouse wheel from silently changing a focused number field (rates, qty, tax %).
// The field is blurred on wheel so the page scrolls as usual and the value stays as typed.
(function () {
  document.addEventListener('wheel', function (e) {
    var el = e.target;
    if (el && el.tagName === 'INPUT' && el.type === 'number' && document.activeElement === el) {
      el.blur();
    }
  }, { passive: true, capture: true });
})();
