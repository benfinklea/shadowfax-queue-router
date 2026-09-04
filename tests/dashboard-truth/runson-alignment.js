// Run against the dashboard's real CSS and refreshRunsOn renderer with fixture data.
(async () => {
    await document.fonts.ready;
    const measurements = [];
    const textRect = element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        return range.getBoundingClientRect();
    };
    const rect = element => {
        const r = element.classList.contains('runson-label')
            ? textRect(element) : element.getBoundingClientRect();
        return [r.x, r.y, r.width, r.height];
    };
    for (const count of [0, 12, 123]) {
        window.fetch = async () => ({json: async () => ({
            available: true, deployed: true, live_runners: count,
            runners: [{instance_type: 'a-very-wide-runner-instance-type', age_minutes: 12}],
            gate_shards: 'on'
        })});
        refreshRunsOn();
        await new Promise(resolve => setTimeout(resolve, 0));
        const number = document.querySelector('#runson-card .runson-count');
        const label = document.querySelector('#runson-card .runson-label');
        const details = document.querySelector('#runson-card details');
        for (const open of details ? [false, true] : [false]) {
            if (details) details.open = open;
            // display:contents reconstructs the original flex children so we can
            // prove that the label, dropdown, card, and neighboring facts stay put.
            const baseline = document.createElement('style');
            baseline.textContent = '#runson-card .runson-total{display:contents}'
                + '#runson-card .runson-count{text-align:start}';
            document.head.appendChild(baseline);
            const stationary = [label, document.querySelector('#runson-card'),
                ...document.querySelectorAll('#runson-card .runson-grid > :not(.runson-live)')];
            if (details) stationary.push(details);
            const before = stationary.map(rect);
            baseline.remove();
            const after = stationary.map(rect);
            measurements.push({
                count, open, zeroClass: number.classList.contains('zero'),
                numberRight: textRect(number).right, labelRight: textRect(label).right,
                labelAlign: getComputedStyle(label).textAlign,
                maxStationaryDelta: Math.max(...before.flatMap((r, i) =>
                    r.map((value, j) => Math.abs(value - after[i][j]))))
            });
        }
    }
    const output = document.createElement('pre');
    output.id = 'runson-alignment-result';
    output.textContent = JSON.stringify(measurements);
    document.body.appendChild(output);
})();
