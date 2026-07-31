/**
 * Garmin dashboard — the latest day's intraday curves (stress, Body Battery,
 * heart rate).
 *
 * window.vitalsGarminIntraday = { "<series_type>": [{ ts, value }, ...], ... }
 * (ts is a local wall-clock ISO string; the server already converted from
 * Garmin's UTC epoch ms).
 *
 * Unlike the custom-chart builder in charts.js, this is a *within-day* view: the
 * x-axis is minutes since midnight, not dates, and the series never land in the
 * chart registry (which groups by date).
 *
 * A *linear minute* axis, deliberately, not a category axis over the union of
 * every series' timestamps: the curves are sampled on their own clocks (stress
 * and Body Battery every ~3 min out of one payload, heart rate every ~2 min out
 * of another), so a shared label list leaves each series null at most positions.
 * With spanGaps off — and it must stay off, a gap means the watch measured
 * nothing — that draws them as isolated points, i.e. invisible at pointRadius 0.
 * Minutes also make time proportional, so a gap reads as the hour it really was
 * (same call as the night chart in garmin_sleep.js).
 *
 * Stress and Body Battery are both 0–100 scores, so they share the left axis and
 * stay directly comparable — the whole point of drawing them together is seeing a
 * stress spike drain the battery. Heart rate is bpm, so it gets its own
 * right-hand axis rather than being squashed into 0–100.
 */
function initGarminIntradayChart() {
    const canvas = document.getElementById('garminIntradayChart');
    if (!canvas) return;

    const data = window.vitalsGarminIntraday || {};
    const C = (window.vitalsChartTheme && window.vitalsChartTheme()) || {};

    const SERIES = [
        { key: 'stress', labelKey: 'garmin.series.stress', color: C.bad, fallback: 'Stress', axis: 'y' },
        { key: 'body_battery', labelKey: 'garmin.series.body_battery', color: C.good, fallback: 'Body Battery', axis: 'y' },
        { key: 'heart_rate', labelKey: 'garmin.series.heart_rate', color: C.violet, fallback: 'Heart rate', axis: 'y1' },
    ];

    // "2026-07-30T08:33:00" → 513. Read off the string rather than through Date:
    // the value is already local wall clock, and parsing it would re-read it in
    // the browser's own zone.
    const minuteOfDay = ts => {
        const hh = Number((ts || '').slice(11, 13));
        const mm = Number((ts || '').slice(14, 16));
        return Number.isFinite(hh) && Number.isFinite(mm) ? hh * 60 + mm : null;
    };
    // Round to the whole minute *first*: a tick can land on 179.6, and formatting
    // the parts separately would render that as "02:60".
    const clock = m => {
        const total = Math.round(m);
        return ('0' + Math.floor(total / 60)).slice(-2) + ':' + ('0' + (total % 60)).slice(-2);
    };

    const present = SERIES.filter(s => (data[s.key] || []).length);
    if (!present.length) return;

    let lastMinute = 0;
    const datasets = present.map(s => {
        const points = (data[s.key] || [])
            .map(p => ({ x: minuteOfDay(p.ts), y: p.value }))
            .filter(p => p.x !== null);
        points.forEach(p => { if (p.x > lastMinute) lastMinute = p.x; });
        return {
            label: (window.t ? window.t(s.labelKey) : s.fallback),
            data: points,
            borderColor: s.color,
            backgroundColor: 'transparent',
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
            tension: 0.25,
            yAxisID: s.axis,
            // false, not true: a gap here means the watch recorded nothing (taken
            // off, or a sentinel reading the parser dropped). Bridging it would
            // draw a straight line through hours that were never measured.
            spanGaps: false,
        };
    });

    // Whole hours, ~8 ticks across whatever the day has so far, so the labels read
    // 00:00 / 03:00 / … instead of the linear scale's own 01:57 / 04:42. The axis
    // ends on a whole step too: against a ragged max (23:59) Chart.js lays the
    // ticks out backwards from it and every label after the middle drifts a minute.
    const tickStep = Math.max(30, Math.ceil(lastMinute / 8 / 60) * 60);
    const axisEnd = Math.ceil(lastMinute / tickStep) * tickStep;

    if (canvas._vitalsChart) canvas._vitalsChart.destroy();
    canvas._vitalsChart = new Chart(canvas, {
        type: 'line',
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: window.devicePixelRatio || 2,
            // Registered by charts.js — 'index' would pair the series by array
            // position, which is a different minute in each of them.
            interaction: { mode: 'nearestByTime', intersect: false },
            plugins: {
                legend: { position: 'bottom', labels: { color: C.muted, font: { family: 'Inter', size: 10 }, boxWidth: 12 } },
                tooltip: {
                    backgroundColor: C.surface, borderColor: C.line2, borderWidth: 1,
                    titleColor: C.accent2, titleFont: { family: 'Inter', size: 11 },
                    bodyColor: C.fg, bodyFont: { family: 'Inter', size: 10 }, padding: 8,
                    callbacks: {
                        // The raw x is a minute offset — nobody wants to read "513".
                        title: items => (items.length ? clock(items[0].parsed.x) : ''),
                    },
                },
            },
            scales: {
                x: {
                    type: 'linear',
                    min: 0,
                    max: axisEnd,
                    grid: { color: C.grid, drawTicks: false },
                    border: { color: C.axisLine },
                    ticks: {
                        color: C.muted, maxRotation: 0, stepSize: tickStep,
                        font: { family: 'Inter', size: 9 },
                        callback: value => clock(value),
                    },
                },
                y: {
                    min: 0,
                    max: 100,
                    grid: { color: C.grid, drawTicks: false },
                    border: { color: C.axisLine },
                    ticks: { color: C.muted, stepSize: 25, font: { family: 'Inter', size: 9 } },
                },
                y1: {
                    // Hidden when the day has no heart-rate curve (an old sync, or
                    // the watch was off) — an empty right axis is just clutter.
                    display: present.some(s => s.axis === 'y1'),
                    position: 'right',
                    grid: { drawOnChartArea: false },
                    border: { color: C.axisLine },
                    ticks: { color: C.muted, font: { family: 'Inter', size: 9 } },
                },
            },
        },
    });
}

function initGarminIntradayChartSafe() {
    // A throw here must not bubble out of an htmx:afterSettle handler and abort
    // the rest of the swap (same guard as the other chart scripts).
    try { initGarminIntradayChart(); } catch (e) { console.error('garminIntraday init failed', e); }
}

if (document.readyState !== 'loading') {
    initGarminIntradayChartSafe();
} else {
    document.addEventListener('DOMContentLoaded', initGarminIntradayChartSafe);
}

// Registered once: this file lives in <head>, so it does NOT re-execute on a
// boosted navigation — these hooks are what redraw the chart after an hx-boost
// swap into /garmin and after browser back/forward.
if (!window.__garminIntradayBound) {
    window.__garminIntradayBound = true;
    document.addEventListener('htmx:afterSettle', initGarminIntradayChartSafe);
    document.addEventListener('htmx:historyRestore', initGarminIntradayChartSafe);
}
