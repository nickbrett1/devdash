// The VM status, as the `meter-strip` block takes it.
//
// Shared by the page and the /tile embed for the reason the old StatusStrip
// existed: the NAS dashboard iframes the strip, and two copies of the same
// figures drift until the dashboard quietly disagrees with the page it
// summarises. Only the *mapping* is shared now — the meter itself is the block,
// so a change to how a capacity bar looks reaches both at once.

/**
 * Bytes, in the units these numbers are memorised in.
 *
 * 8_000_000_000 must read "8 GB", not "8.0 GB": a trailing .0 makes two
 * readings harder to compare at a glance, and comparing them at a glance is
 * the entire job of a strip on a phone.
 */
export function bytes(n) {
	if (n === null || n === undefined) return '—';
	const units = ['B', 'kB', 'MB', 'GB', 'TB'];
	let i = 0;
	while (n >= 1000 && i < units.length - 1) {
		n /= 1000;
		i += 1;
	}
	const value = i === 0 || Number.isInteger(n) ? Math.round(n) : n.toFixed(1);
	return `${value} ${units[i]}`;
}

/**
 * `meter-strip` props for a `/api/status` body.
 *
 * `used`/`total` stay numbers because the bar needs the ratio; the labels are
 * formatted here because this app knows the units and the block does not.
 */
export function meterProps(status) {
	const s = status ?? {};
	return {
		label: 'VM memory',
		used: s.vm_mem_used ?? 0,
		total: s.vm_mem_total ?? 0,
		usedLabel: bytes(s.vm_mem_used),
		totalLabel: bytes(s.vm_mem_total),
		figures: [
			{
				value: bytes(s.devcontainer_footprint),
				label: 'devcontainers',
				hint: 'memory the devcontainers are holding'
			},
			{ value: String(s.running_count ?? 0), label: 'running' },
			{ value: String(s.container_count ?? 0), label: 'containers' }
		]
	};
}
