<script>
	// The status strip, shared by the full page and the /tile embed.
	//
	// It lives in its own component because the NAS dashboard iframes it: two
	// copies of the same four figures and the same meter would drift, and the
	// dashboard would quietly start disagreeing with the page.
	let { status, windowTitles = null, windowError = '', screenLocked = false } = $props();

	function bytes(n) {
		if (n === null || n === undefined) return '—';
		const units = ['B', 'kB', 'MB', 'GB', 'TB'];
		let i = 0;
		while (n >= 1000 && i < units.length - 1) {
			n /= 1000;
			i += 1;
		}
		// 8_000_000_000 must read "8 GB", not "8.0 GB" — these are memorised
		// numbers, and a trailing .0 makes them harder to compare at a glance.
		const value = i === 0 || Number.isInteger(n) ? Math.round(n) : n.toFixed(1);
		return `${value} ${units[i]}`;
	}

	const usedPct = $derived(
		status && status.vm_mem_total ? Math.min(100, (status.vm_mem_used / status.vm_mem_total) * 100) : 0
	);
</script>

<section class="strip" aria-label="Memory">
	<div class="meter">
		<div class="meter-label">
			<span>VM memory</span>
			<span>{bytes(status.vm_mem_used)} / {bytes(status.vm_mem_total)}</span>
		</div>
		<div class="bar"><span style="width:{usedPct}%"></span></div>
	</div>
	<div class="figures">
		<div class="figure">
			<span class="n">{bytes(status.devcontainer_footprint)}</span>
			<span class="k" title="memory the devcontainers are holding">devcontainers</span>
		</div>
		<div class="figure">
			<span class="n">{status.running_count}</span>
			<span class="k">running</span>
		</div>
		<div class="figure">
			<span class="n">{status.container_count}</span>
			<span class="k">containers</span>
		</div>
		<div class="figure">
			<span class="n">{screenLocked ? '?' : (windowTitles ?? '—')}</span>
			{#if screenLocked}
				<span
					class="k locked"
					title="The Mac's screen is locked. While it is, System Events reports zero windows for every app without an error, so the window count cannot be trusted."
				>
					windows hidden — screen locked
				</span>
			{:else}
				<span class="k" title={windowError || 'windows System Events can see'}>vscode windows</span>
			{/if}
		</div>
	</div>
</section>

<style>
	.strip {
		background: #161923;
		border: 1px solid #3b4152;
		border-radius: 0.75rem;
		padding: 0.9rem 1rem;
	}
	.meter-label {
		display: flex;
		justify-content: space-between;
		font-size: 0.85rem;
		color: #94a3b8;
		margin-bottom: 0.35rem;
	}
	.bar {
		height: 0.5rem;
		background: #3b4152;
		border-radius: 0.25rem;
		overflow: hidden;
	}
	.bar span {
		display: block;
		height: 100%;
		background: #34d399;
	}
	.figures {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr));
		gap: 0.5rem;
		margin-top: 0.9rem;
	}
	.figure {
		display: flex;
		flex-direction: column;
	}
	.figure .n {
		font-size: 1.05rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.figure .k {
		font-size: 0.72rem;
		color: #94a3b8;
	}
	/* Not an error — the page is fine — just a figure that cannot be read as
	   fact right now. Amber sets it apart from the ordinary grey labels. */
	.figure .k.locked {
		color: #f0c14b;
	}
</style>
