// Replay the captured endpoint response through refreshModelServing. Fixtures
// deliberately disagree with the lifetime record and exercise refresh transitions.
(async function () {
    const rows = [];
    let response;
    window.fetch = async path => {
        if (path !== '/api/model_serving') throw new Error('unexpected endpoint ' + path);
        return {json: async () => response};
    };
    const host = document.createElement('div');
    // Use only these cards, so a captured page's existing IDs cannot shadow them.
    document.body.replaceChildren(host);
    for (const [box, id] of Object.entries(TPS_DIAL_BOXES)) {
        host.insertAdjacentHTML('beforeend', '<div id="card-' + box + '" data-identity-ok="true">'
            + renderGauge(null, 0, 1, 'TOK/S', '', true, 0.7, true, id)
            + '<div id="serving-' + box + '"></div>'
            + '<div id="tps-fill-now-' + box + '"></div><div id="tps-fill-peak-' + box + '"></div></div>');
    }
    const base = {available: true, tps_now: 60.5, tps_avg_today: 80, tps_max_today: 120.5,
        tps_record: 3283.2, requests: {hour: 0, day: 0, week: 0}, serving_minutes_today: 1};
    const cases = [
        ['live', null],
        ['fractional', base],
        ['at-peak', {...base, tps_now: 120.5, tps_avg_today: 120.5}],
        ['above-peak', {...base, tps_now: 150}],
        ['zero', {...base, tps_now: 0, tps_avg_today: 0, tps_max_today: 0}],
        ['unavailable', {...base, available: false}],
        ['record-zero', {...base, tps_record: 0}],
        ['identity-unverified', base],
    ];
    for (const [name, fixture] of cases) {
        response = fixture ? Object.fromEntries(Object.keys(TPS_DIAL_BOXES).map(box => [box, fixture])) : servingSnapshot;
        for (const box of Object.keys(TPS_DIAL_BOXES)) {
            document.getElementById('card-' + box).dataset.identityOk = String(name !== 'identity-unverified');
        }
        refreshModelServing();
        await new Promise(resolve => setTimeout(resolve, 0));
        for (const [box, id] of Object.entries(TPS_DIAL_BOXES)) {
            const payload = response[box];
            const gauge = document.getElementById(id);
            const angle = Number(gauge.querySelector('.gauge-needle').style.transform.match(/rotate\(([-\d.]+)deg\)/)[1]);
            const percentage = Math.round((angle + 90) / 1.8);
            const label = gauge.querySelector('.gauge-value').textContent;
            const serving = document.getElementById('serving-' + box);
            const peakLabel = serving.querySelectorAll('.tps')[1]?.textContent ?? null;
            const available = payload?.available && name !== 'identity-unverified';
            const peak = payload?.tps_max_today;
            const expected = {
                percentage: available && peak > 0 ? Math.round(Math.max(0, Math.min(100, 100 * payload.tps_now / peak))) : 0,
                label: !available ? '--' : peak === 0 ? 'no serving yet today' : Math.round(payload.tps_now) + '/' + Math.round(peak),
                peakLabel: available ? String(Math.round(peak)) : null,
            };
            const rendered = {percentage, label, peakLabel};
            rows.push({case: name, box, payload, rendered, expected,
                ok: JSON.stringify(rendered) === JSON.stringify(expected)
                    && (!available || Number(gauge.dataset.max) === peak)
                    && !document.getElementById(id + '-peak')});
        }
    }
    const output = document.createElement('pre');
    output.id = 'tps-dial-result';
    output.textContent = JSON.stringify(rows);
    document.body.appendChild(output);
})();
