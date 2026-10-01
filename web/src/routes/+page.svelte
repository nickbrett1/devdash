<script>
	import { onMount } from 'svelte';

	// The page is prerendered to a static asset and served by devdash's own
	// Python server, so every number here comes from /api/* at runtime and
	// the cookie the server set on the first ?token= visit authenticates it.
	const REFRESH_MS = 30_000;
	const JOB_POLL_MS = 1000;
	const AUTH = 'Not authorised. Open this page once with ?token=… and the browser will keep a cookie.';

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
	// The one long-running job this phone started. `open` and `provision` both
	// take minutes on a first build, so they answer with a job id and the log
	// is polled from there.
	let job = $state(null);
	let pollTimer = null;

	// The Provision picker. `repos === null` means "not asked yet", which is
	// deliberately distinct from "asked and got nothing".
	let repos = $state(null);
	let reposError = $state('');
	let repoFilter = $state('');
	let pickerOpen = $state(false);

	async function load() {
		try {
			const [p, s] = await Promise.all([
				fetch('/api/projects', { credentials: 'same-origin' }),
				fetch('/api/status', { credentials: 'same-origin' })
			]);
			if (p.status === 401 || s.status === 401) {
				error = AUTH;
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
		return () => {
			clearInterval(timer);
			stopPolling();
		};
	});

	async function startJob(url, body, target) {
		stopPolling();
		try {
			const res = await fetch(url, {
				method: 'POST',
				credentials: 'same-origin',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(body || {})
			});
			const data = await res.json().catch(() => ({}));
			if (res.status === 401) {
				error = AUTH;
				return;
			}
			if (!res.ok) {
				// 409 is the ordinary "already running" answer and carries the
				// job id of the job in the way; either way the message is the
				// server's to write.
				job = { state: 'failed', target, error: data.error || `HTTP ${res.status}`, log: [] };
				return;
			}
			job = data;
			pollJob();
		} catch (e) {
			job = { state: 'failed', target, error: `Could not reach devdash: ${e}`, log: [] };
		}
	}

	function pollJob() {
		pollTimer = setInterval(async () => {
			try {
				const res = await fetch(`/api/jobs/${job.job_id}`, { credentials: 'same-origin' });
				if (!res.ok) return;
				job = await res.json();
			} catch {
				return; // a dropped poll is not a failed job; the next one may land
			}
			if (job.state !== 'running') {
				stopPolling();
				load();
			}
		}, JOB_POLL_MS);
	}

	function stopPolling() {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	}

	async function loadRepos() {
		pickerOpen = !pickerOpen;
		if (!pickerOpen || repos !== null) return;
		try {
			const res = await fetch('/api/repos', { credentials: 'same-origin' });
			const data = await res.json().catch(() => ({}));
			repos = data.repos || [];
			reposError = data.error || '';
		} catch (e) {
			repos = [];
			reposError = `Could not reach devdash: ${e}`;
		}
	}

	// "This phone already asked for this and it has not finished" — the button
	// that started the job is the one that must show it. The server refuses a
	// duplicate anyway; this is so nobody has to find that out.
	function isRunning(target) {
		return !!job && job.state === 'running' && job.target === target;
	}

	const filteredRepos = $derived(
		repos === null
			? []
			: repos.filter((r) => r.toLowerCase().includes(repoFilter.trim().toLowerCase()))
	);

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
				error = AUTH;
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
								disabled={isRunning(p.name)}
								onclick={() => startJob(`/api/projects/${encodeURIComponent(p.name)}/open`, {}, p.name)}
							>
								{isRunning(p.name) ? 'Opening…' : 'Open'}
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
								disabled={isRunning(p.name)}
								title="reopen / rebuild and open a window"
								onclick={() => startJob(`/api/projects/${encodeURIComponent(p.name)}/open`, {}, p.name)}
							>
								{isRunning(p.name) ? 'Opening…' : 'Open'}
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

	{#if job}
		<section class="job" class:bad={job.state === 'failed'}>
			<div class="job-head">
				<strong>{job.kind || 'job'} {job.target}</strong>
				<span class="state" class:running={job.state === 'done'} class:stopped={job.state === 'running'}>
					{job.state}
				</span>
			</div>
			{#if job.error}
				<p class="note bad">{job.error}</p>
			{/if}
			{#if job.state === 'running'}
				<p class="note">Running — a first container build takes minutes. The log updates once a second.</p>
			{/if}
			{#if job.log && job.log.length}
				<pre>{job.log.join('\n')}</pre>
			{/if}
			{#if job.state !== 'running'}
				<button onclick={() => (job = null)}>Dismiss</button>
			{/if}
		</section>
	{/if}

	<section class="provision">
		<button class="disclose" aria-expanded={pickerOpen} onclick={loadRepos}>
			{pickerOpen ? '▾' : '▸'} Provision a repo
		</button>
		{#if pickerOpen}
			{#if reposError}
				<p class="note bad">{reposError}</p>
			{/if}
			{#if repos === null}
				<p class="muted">Loading repositories…</p>
			{:else}
				<input type="search" bind:value={repoFilter} placeholder="Filter repositories…" />
				{#if filteredRepos.length === 0}
					<p class="muted">Nothing here that has no workspace yet.</p>
				{:else}
					<ul class="repos">
						{#each filteredRepos as r (r)}
							<li>
								<span class="repo-name">{r}</span>
								<button
									disabled={isRunning(r)}
									onclick={() => startJob('/api/provision', { repo: r }, r)}
								>
									{isRunning(r) ? 'Cloning…' : 'Provision'}
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		{/if}
	</section>
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
	.job {
		margin-top: 1rem;
		background: #fff;
		border: 1px solid #e5e5ea;
		border-radius: 0.75rem;
		padding: 0.9rem 1rem;
	}
	.job.bad {
		border-color: #ffc9c4;
		background: #fff1f0;
	}
	.job-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.job pre {
		margin: 0.6rem 0 0;
		padding: 0.6rem;
		max-height: 18rem;
		overflow: auto;
		background: #1d1d1f;
		color: #f2f2f7;
		border-radius: 0.5rem;
		font-size: 0.75rem;
		line-height: 1.35;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.provision {
		margin-top: 1rem;
	}
	.disclose {
		width: 100%;
		text-align: left;
		font-size: 0.95rem;
		padding: 0 0.9rem;
	}
	input[type='search'] {
		width: 100%;
		box-sizing: border-box;
		margin-top: 0.6rem;
		padding: 0.6rem 0.7rem;
		min-height: 2.75rem;
		font-size: 1rem;
		border: 1px solid #d2d2d7;
		border-radius: 0.6rem;
		background: #fff;
		color: inherit;
	}
	.repos {
		list-style: none;
		margin: 0.6rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.repos li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.6rem;
		background: #fff;
		border: 1px solid #e5e5ea;
		border-radius: 0.6rem;
		padding: 0.5rem 0.6rem;
	}
	.repo-name {
		font-size: 0.9rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.repos button {
		min-width: 5.5rem;
		min-height: 2.75rem;
		font-size: 0.9rem;
	}
</style>
