// Applied in <head> before first paint, so the page never flashes the wrong theme
(function () {
    try {
        const theme = localStorage.getItem('elite-erp-theme');
        if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
    } catch (err) { /* storage unavailable: follow the system theme */ }
})();
