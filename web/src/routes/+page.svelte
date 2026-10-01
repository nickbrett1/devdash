<script>
	import { onMount } from 'svelte';

	// The page is prerendered to a static asset and served by devdash's own
	// Python server, so every number here comes from /api/* at runtime and
	// the cookie the server set on the first ?token= visit authenticates it.
	const REFRESH_MS = 30_000;

	let projects = $state([]);
	let status = $state(null);
	let windowTitles = $state(null);
	let windowError = $state('');
	let error = $state('');
	let loading = $state(true);
	let updated = $state(null);
	// Per-project action state, keyed by name. `busy[name]` is the verb in
	// flight (so both buttons can disable and only the tapped one spins);
	// `note[name]` is the last answer, including a refusal; `offer[name]` is a
	// refusal the user may override.
	let busy = $state({});
	let note = $state({});
	let offer = $state({});

	async function load() {
		try {
			const [p, s] = await Promise.all([
				fetch('/api/projects', { credentials: 'same-origin' }),
				fetch('/api/status', { credentials: 'same-origin' })
			]);
			if (p.status === 401 || s.status === 401) {
				error = 'Not authorised. Open this page once with ?token=… and the browser will keep a cookie.';
				return;
			}
			if (!p.ok || !s.ok) {
				error = `devdash answered ${p.status}/${s.status}.`;
				return;
			}
			const body = await p.json();
			projects = body.projects || [];
			windowTitles = typeof body.window_titles === 'number' ? body.window_titles : null;
			windowError = body.window_error || '';
			status = await s.json();
			error = '';
			updated = new Date();
		} catch (e) {
			error = `Could not reach devdash: ${e}`;
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		const timer = setInterval(load, REFRESH_MS);
		return () => clearInterval(timer);
	});

	async function act(name, verb, extra = {}) {
		busy = { ...busy, [name]: verb };
		note = { ...note, [name]: null };
		offer = { ...offer, [name]: false };
		try {
			const res = await fetch(`/api/projects/${encodeURIComponent(name)}/${verb}`, {
				method: 'POST',
				credentials: 'same-origin',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(extra)
			});
			const body = await res.json().catch(() => ({}));
			if (res.status === 401) {
				error = 'Not authorised. Open this page once with ?token=… and the browser will keep a cookie.';
				return;
			}
			if (!res.ok) {
				note = { ...note, [name]: { ok: false, text: body.error || `HTTP ${res.status}` } };
				return;
			}
			// A refusal arrives as a 200: the request succeeded, it just
			// declined to act. Offer the override rather than re-deciding for
			// the user.
			if (body.refused) {
				note = { ...note, [name]: { ok: false, text: body.detail } };
				offer = { ...offer, [name]: true };
				return;
			}
			const bits = [];
			if ('stopped' in body) bits.push(body.stopped ? 'container stopped' : 'nothing to stop');
			if ('window_closed' in body) bits.push(body.window_closed ? 'window closed' : 'window left open');
			if (body.uri) bits.push('opened in VS Code');
			note = { ...note, [name]: { ok: true, text: bits.join(' · ') || body.detail || 'done' } };
			await load();
		} catch (e) {
			note = { ...note, [name]: { ok: false, text: `Could not reach devdash: ${e}` } };
		} finally {
			busy = { ...busy, [name]: null };
		}
	}

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

	function days(d) {
		if (d === null || d === undefined) return null;
		if (d < 1) return `${Math.round(d * 24)}h`;
		return `${d.toFixed(1)}d`;
	}

	const usedPct = $derived(
		status && status.vm_mem_total ? Math.min(100, (status.vm_mem_used / status.vm_mem_total) * 100) : 0
	);
</script>

<svelte:head>
	<title>devdash</title>
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<meta name="color-scheme" content="light dark" />
</svelte:head>

<main>
	<header>
		<h1>devdash</h1>
		<div class="refresh">
			{#if updated}
				<span class="stamp">{updated.toLocaleTimeString()}</span>
			{/if}
			<button onclick={load} disabled={loading} aria-label="Refresh">↻</button>
		</div>
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if status}
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
					<span class="k">devcontainers</span>
				</div>
				<div class="figure">
					<span class="n">{status.running_count}</span>
					<span class="k">running</span>
				</div>
				<div class="figure">
					<span class="n">{status.container_count}</span>
					<span class="k">devcontainers</span>
				</div>
				<div class="figure">
					<span class="n">{windowTitles ?? '—'}</span>
					<span class="k" title={windowError || 'windows System Events can see'}>vscode windows</span>
				</div>
			</div>
		</section>
	{/if}

	{#if loading && projects.length === 0}
		<p class="muted">Loading…</p>
	{:else if projects.length === 0}
		<p class="muted">No projects.</p>
	{:else}
		<ul class="projects">
			{#each projects as p (p.name)}
				<li class="project">
					<div class="top">
						<span class="name">{p.name}</span>
						<span class="state {p.state}">{p.state}</span>
					</div>
					<div class="badges">
						{#if p.live_session}
							<span class="badge live" title={p.live_evidence}>live session</span>
						{/if}
						{#if p.window_open}
							<span class="badge window">window open</span>
						{/if}
						{#if p.last_build}
							<span class="badge build" title={p.last_build}>
								{p.last_build}
								{#if days(p.last_human_build_days) !== null}&middot; {days(p.last_human_build_days)} ago{/if}
							</span>
						{:else}
							<span class="badge muted-badge">no human build</span>
						{/if}
					</div>

					<div class="actions">
						{#if p.state === 'absent'}
							<button
								class="wide"
								disabled={!!busy[p.name]}
								onclick={() => act(p.name, 'open')}
							>
								{busy[p.name] === 'open' ? 'Opening…' : 'Open'}
							</button>
						{:else}
							<button
								disabled={!!busy[p.name]}
								title={p.live_session ? `live: ${p.live_evidence}` : 'stop the container'}
								onclick={() => act(p.name, 'close')}
							>
								{busy[p.name] === 'close' ? 'Closing…' : 'Close'}
							</button>
							<button
								disabled={!!busy[p.name]}
								title="reopen / rebuild and open a window"
								onclick={() => act(p.name, 'open')}
							>
								{busy[p.name] === 'open' ? 'Opening…' : 'Open'}
							</button>
						{/if}
						{#if offer[p.name]}
							<button class="warn" disabled={!!busy[p.name]} onclick={() => act(p.name, 'close', { force: true })}>
								Force stop
							</button>
						{/if}
					</div>
					{#if note[p.name]}
						<p class="note" class:bad={!note[p.name].ok} role="status">{note[p.name].text}</p>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</main>

<style>
	:global(body) {
		margin: 0;
		font: 16px/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
		background: #f5f5f7;
		color: #1d1d1f;
	}
	main {
		max-width: 42rem;
		margin: 0 auto;
		padding: 1rem;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 0.75rem;
	}
	h1 {
		font-size: 1.4rem;
		margin: 0;
		letter-spacing: -0.01em;
	}
	.refresh {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.stamp {
		font-size: 0.8rem;
		color: #6e6e73;
		font-variant-numeric: tabular-nums;
	}
	button {
		min-width: 2.75rem;
		min-height: 2.75rem;
		font-size: 1.2rem;
		border: 1px solid #d2d2d7;
		background: #fff;
		border-radius: 0.6rem;
	}
	button:disabled {
		opacity: 0.5;
	}
	.error {
		background: #fff1f0;
		border: 1px solid #ffc9c4;
		color: #a1221b;
		padding: 0.75rem;
		border-radius: 0.6rem;
	}
	.muted {
		color: #6e6e73;
	}
	.strip {
		background: #fff;
		border: 1px solid #e5e5ea;
		border-radius: 0.75rem;
		padding: 0.9rem 1rem;
		margin-bottom: 1rem;
	}
	.meter-label {
		display: flex;
		justify-content: space-between;
		font-size: 0.85rem;
		color: #6e6e73;
		margin-bottom: 0.35rem;
	}
	.bar {
		height: 0.5rem;
		background: #e5e5ea;
		border-radius: 0.25rem;
		overflow: hidden;
	}
	.bar span {
		display: block;
		height: 100%;
		background: #34c759;
	}
	.figures {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
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
		color: #6e6e73;
	}
	.projects {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.project {
		background: #fff;
		border: 1px solid #e5e5ea;
		border-radius: 0.75rem;
		padding: 0.75rem 0.9rem;
		/* M2 turns each of these into a tap target; reserve the height now. */
		min-height: 2.75rem;
	}
	.top {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.name {
		font-weight: 600;
	}
	.state {
		font-size: 0.75rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		background: #e5e5ea;
		color: #48484a;
	}
	.state.running {
		background: #d7f5dd;
		color: #1b7a35;
	}
	.state.stopped {
		background: #fdeccd;
		color: #8a5a00;
	}
	.badges {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
		margin-top: 0.4rem;
	}
	.badge {
		font-size: 0.72rem;
		padding: 0.12rem 0.45rem;
		border-radius: 0.35rem;
		background: #f0f0f3;
		color: #48484a;
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.badge.live {
		background: #d7f5dd;
		color: #1b7a35;
	}
	.badge.window {
		background: #dce8ff;
		color: #1b4fa8;
	}
	.badge.muted-badge {
		color: #8e8e93;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-top: 0.6rem;
	}
	.actions button {
		min-width: 4.5rem;
		min-height: 2.75rem;
		padding: 0 0.9rem;
		font-size: 0.95rem;
		border: 1px solid #d2d2d7;
		background: #fff;
		border-radius: 0.6rem;
		color: inherit;
	}
	.actions button.wide {
		flex: 1;
	}
	.actions button.warn {
		border-color: #ffc9c4;
		background: #fff1f0;
		color: #a1221b;
	}
	.note {
		margin: 0.5rem 0 0;
		font-size: 0.8rem;
		color: #6e6e73;
	}
	.note.bad {
		color: #a1221b;
	}
</style>
