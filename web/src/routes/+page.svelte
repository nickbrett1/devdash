<script>
	import { onMount } from 'svelte';
	import StatusStrip from '$lib/StatusStrip.svelte';

	// The page is prerendered to a static asset and served by devdash's own
	// Python server, so every number here comes from /api/* at runtime.
	const REFRESH_MS = 30_000;
	const JOB_POLL_MS = 1000;
	// devdash answers 401 for nothing: access is the tailnet bind, so this is
	// only reachable if something new starts gating the API. Kept as a
	// readable message rather than an unexplained blank page.
	const AUTH = 'Not authorised. devdash gates access by the tailnet bind, not by a token.';

	let projects = $state([]);
	let status = $state(null);
	// While the Mac's screen is locked, System Events reports every process
	// with zero windows and no error, so a running project's `window_open` flag
	// is not a fact. Open is still hidden for a running container then.
	let screenLocked = $state(false);

	// Open puts a window on the Mac, so the only thing it can get wrong is
	// adding a second one. A stopped or absent project has no container, so
	// there is nothing to duplicate and the button always stands — including
	// while the screen is locked, which is exactly when opening from a phone is
	// the point. A *running* container is the case to be careful with: hide it
	// if devdash can see the window, and hide it while the screen is locked,
	// when every window reads as closed whether or not it is.
	function canOpen(p) {
		if (p.state !== 'running') return true;
		return !screenLocked && !p.window_open;
	}
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
	// The job log opens as its own full-screen view (a "window" the phone can
	// live in while a build runs) and follows its tail unless the reader has
	// scrolled up — then it stays where they put it until they come back down.
	// `logEl` is the scrolling element; the effect below runs as lines arrive.
	let logEl = $state(null);
	let following = $state(true);

	$effect(() => {
		const length = job?.log?.length ?? 0;
		if (!length || !following || !logEl) return;
		// After the DOM has the new lines — a microtask is enough, and it is
		// available whether or not the page is animating. Guarded: the view may
		// have been dismissed between the effect and the microtask.
		queueMicrotask(() => {
			if (logEl) logEl.scrollTop = logEl.scrollHeight;
		});
	});

	function onLogScroll() {
		if (!logEl) return;
		// 48px of slack: a finger lifting near the bottom should not stop the
		// follow, only a deliberate scroll up should.
		following = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 48;
	}

	// A job can be opened in its own browser window (`?job=<id>`); read that id
	// and adopt it so the new window polls the same job. Guarded for the
	// prerender, where there is no window.
	function initialJobId() {
		if (typeof window === 'undefined') return null;
		return new URLSearchParams(window.location.search).get('job');
	}

	// The Provision picker opens in its own browser window (`?provision=1`), the
	// way a job does, so a phone gets a dedicated screen instead of a disclosure
	// at the foot of the list. Guarded for the prerender, where there is no window.
	function initialProvision() {
		if (typeof window === 'undefined') return false;
		return new URLSearchParams(window.location.search).get('provision') === '1';
	}

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
			screenLocked = !!body.screen_locked;
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
		const id = initialJobId();
		if (id) {
			following = true;
			job = { job_id: id, kind: 'job', target: '', state: 'running', log: [] };
			pollJob();
		}
		if (initialProvision()) {
			pickerOpen = true;
			if (repos === null) fetchRepos();
		}
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
				following = true;
				job = { state: 'failed', target, error: data.error || `HTTP ${res.status}`, log: [] };
				return;
			}
			following = true;
			job = data;
			pollJob();
		} catch (e) {
			following = true;
			job = { state: 'failed', target, error: `Could not reach devdash: ${e}`, log: [] };
		}
	}

	function pollJob() {
		const tick = async () => {
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
		};
		// Poll immediately as well as on the interval: the first line of output
		// should not wait a whole second, and a job named in the URL should show
		// its log the moment the window opens.
		tick();
		pollTimer = setInterval(tick, JOB_POLL_MS);
	}

	function stopPolling() {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	}

	async function fetchRepos() {
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

	// Pull to refresh. iOS Safari in standalone mode has no reload gesture, and
	// this app is mostly read while standing up. Only fires from the very top of
	// the page, so it can never fight an ordinary scroll.
	let pull = $state(0);
	let pullStart = null;

	function onTouchStart(e) {
		if (window.scrollY > 0) {
			pullStart = null;
			return;
		}
		pullStart = e.touches[0].clientY;
	}

	function onTouchMove(e) {
		if (pullStart === null) return;
		const dy = e.touches[0].clientY - pullStart;
		pull = dy > 0 ? Math.min(90, dy / 2) : 0;
	}

	function onTouchEnd() {
		if (pull >= 45) load();
		pull = 0;
		pullStart = null;
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

</script>

<svelte:head>
	<title>devdash</title>
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<meta name="color-scheme" content="light dark" />
</svelte:head>

<main
	ontouchstart={onTouchStart}
	ontouchmove={onTouchMove}
	ontouchend={onTouchEnd}
	style="padding-top: calc(1rem + {pull}px)"
>
	{#if pull > 0}
		<p class="pull" aria-hidden="true">{pull >= 45 ? 'Release to refresh' : 'Pull to refresh'}</p>
	{/if}
	<header>
		<h1>devdash</h1>
		<div class="refresh">
			{#if updated}
				<span class="stamp">{updated.toLocaleTimeString()}</span>
			{/if}
			<button onclick={load} disabled={loading} aria-label="Refresh">↻</button>
		</div>
	</header>

	<a class="provision-top" href="?provision=1" target="_blank" rel="noopener">Provision a repo</a>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if status}
		<div class="strip-slot"><StatusStrip {status} /></div>
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
						<span class="right">
							{#if p.mem_bytes}
								<span class="mem" title="memory this container is using">{bytes(p.mem_bytes)}</span>
							{/if}
							<span class="state {p.state}">{p.state}</span>
						</span>
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

					<!-- How to reach a running container: its tailnet state and the
					     ssh target. This is the step devopen skips when there is no
					     authkey, surfaced where the phone can use it. -->
					{#if p.connection}
						<div class="conn">
							{#if p.connection.state === 'connected'}
								<span class="badge tailnet" title="on the tailnet as {p.connection.host}">tailnet</span>
								{#if p.connection.ssh}
									<code class="ssh">{p.connection.ssh}</code>
								{/if}
							{:else if p.connection.state === 'logged_out'}
								<span class="badge warn-badge" title="the container has tailscale but is not registered">not on tailnet</span>
								<button
									class="pill register"
									disabled={isRunning(p.name)}
									onclick={() => startJob(`/api/projects/${encodeURIComponent(p.name)}/tailscale`, {}, p.name)}
								>
									{isRunning(p.name) ? 'Registering…' : 'Register'}
								</button>
							{:else}
								<span class="badge muted-badge">no tailscale</span>
							{/if}
						</div>
					{/if}

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
							<!-- Close only where there is something to reclaim. On a stopped
							     container it would close a leftover window and nothing else —
							     a button named after RAM, with no RAM to free. -->
							{#if p.state === 'running'}
								<button
									disabled={!!busy[p.name]}
									title={p.live_session ? `live: ${p.live_evidence}` : 'stop the container'}
									onclick={() => act(p.name, 'close')}
								>
									{busy[p.name] === 'close' ? 'Closing…' : 'Close'}
								</button>
							{/if}
							{#if canOpen(p)}
								<button
									disabled={isRunning(p.name)}
									title="reopen / rebuild and open a window"
									onclick={() => startJob(`/api/projects/${encodeURIComponent(p.name)}/open`, {}, p.name)}
								>
									{isRunning(p.name) ? 'Opening…' : 'Open'}
								</button>
							{/if}
						{/if}
						{#if offer[p.name]}
							<button class="warn" disabled={!!busy[p.name]} onclick={() => act(p.name, 'close', { force: true })}>
								Force stop
							</button>
						{/if}
					</div>
					{#if note[p.name]}
						<p class="note" class:bad={!note[p.name].ok} role="status">
							{note[p.name].text}
							{#if note[p.name].url}
								<a class="login-link" href={note[p.name].url} target="_blank" rel="noopener">
									open login ↗
								</a>
							{/if}
						</p>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

</main>

<!-- The Provision picker opens in its own browser window (`?provision=1`), the
     way a job does, so there is a dedicated screen for it on a phone rather than
     a disclosure at the foot of the list. It sits below the job view in z-order:
     starting a provision closes it and the build log takes the screen. -->
{#if pickerOpen}
	<section class="provision-view" aria-label="Provision a repo">
		<div class="job-head">
			<strong>Provision a repo</strong>
			<button onclick={() => (pickerOpen = false)}>Close</button>
		</div>
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
								onclick={() => {
									pickerOpen = false;
									startJob('/api/provision', { repo: r }, r);
								}}
							>
								{isRunning(r) ? 'Cloning…' : 'Provision'}
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</section>
{/if}

<!-- The job is a sibling of <main>, not a child: it is a full-screen view, so
     it must not inherit main's pull-to-refresh touch handlers (a drag on the
     log would otherwise read as a pull), and it takes the whole screen for
     minutes at a time. -->
{#if job}
	<section class="job" class:bad={job.state === 'failed'} aria-label="Job output">
		<div class="job-head">
			<strong>{job.kind || 'job'} {job.target}</strong>
			<span class="state" class:running={job.state === 'done'} class:stopped={job.state === 'running'}>
				{job.state}
			</span>
		</div>
		{#if job.error}
			<p class="note bad">{job.error}</p>
		{/if}
		{#if job.attention}
			<p class="attention" role="alert">
				<span>{job.attention.detail || 'Action needed to continue'}</span>
				{#if job.attention.url}
					<a class="login-link" href={job.attention.url} target="_blank" rel="noopener">
						Authenticate on Tailscale ↗
					</a>
				{/if}
			</p>
		{/if}
		{#if job.state === 'running'}
			<p class="note">
				{#if job.attention}
					Waiting for you to authenticate on the tailnet — the job continues by itself once it is done.
				{:else}
					Running — a first container build takes minutes. The log follows the tail; scroll up to read back.
				{/if}
			</p>
		{/if}
		{#if job.log && job.log.length}
			<pre bind:this={logEl} onscroll={onLogScroll}>{job.log.join('\n')}</pre>
		{:else}
			<p class="muted">Waiting for output…</p>
		{/if}
		<div class="job-actions">
			{#if job.job_id}
				<a class="btn" href={`?job=${job.job_id}`} target="_blank" rel="noopener">New window</a>
			{/if}
			{#if job.state !== 'running'}
				<button onclick={() => (job = null)}>Dismiss</button>
			{/if}
		</div>
	</section>
{/if}

<style>
	:global(body) {
		margin: 0;
		font: 16px/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
		background: #0f172a;
		color: #e2e8f0;
		/* iOS Safari inflates text in landscape otherwise; the sizes here are
		   already chosen for a phone. */
		-webkit-text-size-adjust: 100%;
	}
	main {
		max-width: 42rem;
		margin: 0 auto;
		padding: 1rem;
		padding-bottom: calc(1rem + env(safe-area-inset-bottom));
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
		color: #94a3b8;
		font-variant-numeric: tabular-nums;
	}
	button {
		min-width: 2.75rem;
		min-height: 2.75rem;
		font-size: 1.2rem;
		border: 1px solid #3b4152;
		background: #161923;
		border-radius: 0.6rem;
	}
	button:disabled {
		opacity: 0.5;
	}
	.error {
		background: #3a1518;
		border: 1px solid #7f2d2d;
		color: #fca5a5;
		padding: 0.75rem;
		border-radius: 0.6rem;
	}
	.muted {
		color: #94a3b8;
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
		background: #161923;
		border: 1px solid #3b4152;
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
	.right {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
	}
	.mem {
		font-size: 0.8rem;
		color: #94a3b8;
		font-variant-numeric: tabular-nums;
	}
	.state {
		font-size: 0.75rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		background: #3b4152;
		color: #cbd5e1;
	}
	.state.running {
		background: #17321f;
		color: #5ee08a;
	}
	.state.stopped {
		background: #3a2f10;
		color: #f0c14b;
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
		background: #1f2534;
		color: #cbd5e1;
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.badge.live {
		background: #17321f;
		color: #5ee08a;
	}
	.badge.window {
		background: #17293d;
		color: #60a5fa;
	}
	.badge.muted-badge {
		color: #6b7280;
	}
	.badge.tailnet {
		background: #10241f;
		color: #34d399;
	}
	.badge.warn-badge {
		background: #3a2f10;
		color: #f0c14b;
	}
	/* How to reach a running container. Kept on its own line under the badges
	   so the ssh target can be long without shoving the state pill around. */
	.conn {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.35rem;
		margin-top: 0.4rem;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		font-size: 0.78rem;
		line-height: 1;
		padding: 0.45rem 0.7rem;
		min-height: 2.25rem;
		border-radius: 0.5rem;
		border: 1px solid #3b4152;
		background: #1f2534;
		color: #e2e8f0;
		text-decoration: none;
	}
	.pill.register {
		border: 1px solid #7f6d2d;
		background: #2c2610;
		color: #f0c14b;
	}
	.ssh {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.75rem;
		padding: 0.3rem 0.45rem;
		border-radius: 0.4rem;
		background: #0b0e17;
		color: #cbd5e1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
		/* Selectable: over plain http (the tailnet) there is no clipboard API,
		   so a long-press select is how this gets copied on a phone. */
		user-select: all;
		-webkit-user-select: all;
	}
	.login-link {
		margin-left: 0.4rem;
		color: #93c5fd;
	}
	.strip-slot {
		margin-bottom: 1rem;
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
		border: 1px solid #3b4152;
		background: #161923;
		border-radius: 0.6rem;
		color: inherit;
	}
	.actions button.wide {
		flex: 1;
	}
	.actions button.warn {
		border-color: #7f2d2d;
		background: #3a1518;
		color: #fca5a5;
	}
	.note {
		margin: 0.5rem 0 0;
		font-size: 0.8rem;
		color: #94a3b8;
	}
	.note.bad {
		color: #fca5a5;
	}
	/* A job that is paused on something only the phone can do — the Tailscale
	   login. Louder than a note, because the job does not move until it is
	   done, and the link must be a real tap target, not text in the log. */
	.attention {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin: 0.6rem 0 0;
		padding: 0.6rem 0.7rem;
		font-size: 0.85rem;
		border: 1px solid #7f6d2d;
		background: #2c2610;
		color: #f0c14b;
		border-radius: 0.5rem;
	}
	.attention .login-link {
		margin-left: 0;
		font-weight: 600;
	}
	.pull {
		margin: 0 0 0.5rem;
		text-align: center;
		font-size: 0.8rem;
		color: #94a3b8;
	}
	/* The job view takes the whole screen: a first build is minutes of output
	   and the phone has nothing else to do, so it gets the room rather than
	   being a box below the project list that has to be scrolled to. */
	.job {
		position: fixed;
		inset: 0;
		z-index: 50;
		margin: 0;
		display: flex;
		flex-direction: column;
		background: #161923;
		border: none;
		border-radius: 0;
		padding: calc(0.9rem + env(safe-area-inset-top)) 1rem
			calc(0.9rem + env(safe-area-inset-bottom));
	}
	.job.bad {
		background: #3a1518;
	}
	.job-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		flex: 0 0 auto;
	}
	.job pre {
		flex: 1 1 auto;
		min-height: 6rem;
		margin: 0.6rem 0 0;
		padding: 0.6rem;
		overflow: auto;
		background: #0b0e17;
		color: #e2e8f0;
		border-radius: 0.5rem;
		font-size: 0.8rem;
		line-height: 1.4;
		white-space: pre-wrap;
		word-break: break-word;
		overscroll-behavior: contain;
		-webkit-overflow-scrolling: touch;
	}
	.job-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-top: 0.6rem;
		flex: 0 0 auto;
	}
	.job-actions .btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 4.5rem;
		min-height: 2.75rem;
		padding: 0 0.9rem;
		font-size: 0.95rem;
		border: 1px solid #3b4152;
		background: #161923;
		color: inherit;
		border-radius: 0.6rem;
		text-decoration: none;
	}
	.provision-top {
		display: flex;
		align-items: center;
		justify-content: center;
		box-sizing: border-box;
		width: 100%;
		min-height: 2.75rem;
		margin-bottom: 0.75rem;
		padding: 0 0.9rem;
		font-size: 0.95rem;
		border: 1px solid #3b4152;
		background: #161923;
		color: inherit;
		border-radius: 0.6rem;
		text-decoration: none;
	}
	.provision-view {
		position: fixed;
		inset: 0;
		z-index: 40;
		display: flex;
		flex-direction: column;
		margin: 0;
		padding: calc(0.9rem + env(safe-area-inset-top)) 1rem calc(0.9rem + env(safe-area-inset-bottom));
		background: #161923;
		overflow: auto;
	}
	input[type='search'] {
		width: 100%;
		box-sizing: border-box;
		margin-top: 0.6rem;
		padding: 0.6rem 0.7rem;
		min-height: 2.75rem;
		font-size: 1rem;
		border: 1px solid #3b4152;
		border-radius: 0.6rem;
		background: #161923;
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
		background: #161923;
		border: 1px solid #3b4152;
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
