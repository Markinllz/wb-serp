(products) => {
    // Discover the currently loaded WB module; no compiled module IDs are pinned.
    let display = window.__wbSerpDeliveryDisplay;
    if (!display) {
        const name = Object.keys(window).find(k => /(?:rspack|webpack)Chunkwebsite$/.test(k));
        if (!name || !Array.isArray(window[name])) return null;
        let require;
        window[name].push([['wb_serp_delivery_' + Date.now()], {}, r => { require = r; }]);
        if (!require?.m) return null;
        for (const id of Object.keys(require.m)) {
            if (!require.m[id].toString().includes('getDeliveryDisplay')) continue;
            const exports = require(id);
            if (typeof exports?.getDeliveryDisplay === 'function') {
                display = exports.getDeliveryDisplay;
                window.__wbSerpDeliveryDisplay = display;
                break;
            }
        }
    }
    if (!display) return null;
    const now = new Date();
    const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/Moscow'}).format(now);
    return {today, products:products.map(p => {
        try {
            if (typeof p.time1 !== 'number' || typeof p.time2 !== 'number') return null;
            const hours = p.time1 + p.time2;
            if (!Number.isFinite(hours) || hours <= 0 || hours > 8760) return null;
            const dtype = p.sizes?.[0]?.dtype ?? p.dtype;
            // WB's published dtype flags: DBS=2, EDBS=64. BigInt preserves high bits.
            const isDbs = dtype != null && (BigInt(dtype) & 66n) !== 0n;
            const result = display({deliveryHours:hours, whDtype:dtype, isDbs,
                                    intervals:p.intervals, fastestStore:p.wh});
            return result?.text || null;
        } catch (_) { return null; }
    })};
}
