<script>
	import { onMount } from 'svelte';
	import '../app.css';
	import ActionList from '$lib/components/nb/ActionList.svelte';
	import LogView from '$lib/components/nb/LogView.svelte';
	import MeterStrip from '$lib/components/nb/MeterStrip.svelte';
	import { bytes, meterProps } from '$lib/status.js';

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
	// `answers[name]` is the last answer, including a refusal; `offer[name]` is
	// a refusal the user may override. (Not `note`: that is the name of the
	// `action-list` slot it fills, and a snippet of the same name would shadow
	// the state inside itself.)
	let busy = $state({});
	let answers = $state({});
	let offer = $state({});
	// The one long-running job this phone started. `open` and `provision` both
	// take minutes on a first build, so they answer with a job id and the log
	// is polled from there.
	let job = $state(null);
	let pollTimer = null;
	// The job log opens as its own full-screen view (a "window" the phone can
	// live in while a build runs) and follows its tail unless the reader has
	// scrolled up — then it stays where they put it until they come back down.
	// `logEl` is the scrolling node *inside* the block: `log-view` hands it back
	// bound rather than owning the follow, because following is this page's
	// opinion about a pane it cannot see.
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

	// The row shape `action-list` renders. Each row carries the project it came
	// from, so a snippet can still reach the fields the list does not know about
	// (`connection`, `live_session`) without the block learning about projects.
	const TONE = { running: 'ok', stopped: 'warn', absent: '' };

	const rows = $derived(
		projects.map((p) => ({
			...p,
			// A stopped or absent container is using nothing, and "—" beside a
			// state that already says "absent" would be noise.
			meta: p.mem_bytes ? bytes(p.mem_bytes) : '',
			tone: TONE[p.state] ?? '',
			badges: [
				...(p.live_session ? [{ label: 'live session', tone: 'ok', title: p.live_evidence }] : []),
				...(p.window_open ? [{ label: 'window open', tone: 'info' }] : []),
				p.last_build
					? {
							label:
								days(p.last_human_build_days) === null
									? p.last_build
									: `${p.last_build} · ${days(p.last_human_build_days)} ago`,
							title: p.last_build
						}
					: { label: 'no human build' }
			]
		}))
	);

	const repoRows = $derived(filteredRepos.map((name) => ({ name })));

	async function act(name, verb, extra = {}) {
		busy = { ...busy, [name]: verb };
		answers = { ...answers, [name]: null };
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
				answers = { ...answers, [name]: { ok: false, text: body.error || `HTTP ${res.status}` } };
				return;
			}
			// A refusal arrives as a 200: the request succeeded, it just
			// declined to act. Offer the override rather than re-deciding for
			// the user.
			if (body.refused) {
				answers = { ...answers, [name]: { ok: false, text: body.detail } };
				offer = { ...offer, [name]: true };
				return;
			}
			const bits = [];
			if ('stopped' in body) bits.push(body.stopped ? 'container stopped' : 'nothing to stop');
			if ('window_closed' in body) bits.push(body.window_closed ? 'window closed' : 'window left open');
			if (body.uri) bits.push('opened in VS Code');
			answers = { ...answers, [name]: { ok: true, text: bits.join(' · ') || body.detail || 'done' } };
			await load();
		} catch (e) {
			answers = { ...answers, [name]: { ok: false, text: `Could not reach devdash: ${e}` } };
		} finally {
			busy = { ...busy, [name]: null };
		}
	}

	function openProject(p) {
		startJob(`/api/projects/${encodeURIComponent(p.name)}/open`, {}, p.name);
	}

	// Register is a job like the others: it drives the tailscale login, waits
	// for the tailnet, then starts the agent — so it lands in the job view,
	// whose attention link is the tap target the phone needs.
	function openTailscale(name) {
		startJob(`/api/projects/${encodeURIComponent(name)}/tailscale`, {}, name);
	}

	// Rebuild pulls origin, then recreates the container. devopen's
	// clone_or_update already runs `git fetch` + `git pull --ff-only` on an
	// existing workspace, so this is "pull before reprovisioning" — the fetch
	// and the rebuild are the same call, and there is no separate pull to
	// expose. Confirmed, because it tears down any running container.
	function rebuild(name) {
		const ok = window.confirm(
			`Rebuild ${name}?\n\nPulls the latest from origin, then recreates the devcontainer. ` +
				'Any running container for the project is stopped and replaced.'
		);
		if (!ok) return;
		startJob(`/api/projects/${encodeURIComponent(name)}/open`, { fresh: true }, name);
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
				<span class="stamp num">{updated.toLocaleTimeString()}</span>
			{/if}
			<button class="btn icon" onclick={load} disabled={loading} aria-label="Refresh">↻</button>
		</div>
	</header>

	<a class="btn provision-top" href="?provision=1" target="_blank" rel="noopener">
		Provision a repo
	</a>

	{#if error}
		<p class="notice bad" role="alert">{error}</p>
	{/if}

	{#if status}
		<div class="strip-slot"><MeterStrip {...meterProps(status)} /></div>
	{/if}

	{#if loading && projects.length === 0}
		<p class="muted">Loading…</p>
	{:else}
		<ActionList label="Projects" items={rows} emptyMessage="No projects.">
			<!-- How to reach a running container: its tailnet state and the ssh
			     target. This is the step devopen skips when there is no authkey,
			     surfaced where the phone can use it. Any running container that
			     is not yet connected gets a Register button — not only one the
			     tailnet probe already resolved — because a freshly built
			     container can be up before its tailscale state is readable, and
			     that first launch is exactly when the phone needs a way to
			     register. -->
			{#snippet aside(p)}
				{#if p.connection || p.state === 'running'}
					<div class="conn">
						{#if p.connection && p.connection.state === 'connected'}
							<span class="pill info" title="on the tailnet as {p.connection.host}">tailnet</span>
							{#if p.connection.ssh}
								<code class="ssh">{p.connection.ssh}</code>
							{/if}
						{:else}
							{#if p.connection && p.connection.state === 'logged_out'}
								<span class="pill warn" title="the container has tailscale but is not registered">
									not on tailnet
								</span>
							{:else if p.connection && p.connection.state === 'absent'}
								<!-- "absent" is both "no tailscale here" and "the daemon
								     is not up yet"; Register starts it, so the badge
								     must not read as a dead end. -->
								<span class="pill" title="tailscale is not running in this container">
									tailscale off
								</span>
							{/if}
							<button class="btn" disabled={isRunning(p.name)} onclick={() => openTailscale(p.name)}>
								{isRunning(p.name) ? 'Registering…' : 'Register'}
							</button>
						{/if}
					</div>
				{/if}
			{/snippet}

			{#snippet actions(p)}
				{#if p.state === 'absent'}
					<button disabled={isRunning(p.name)} onclick={() => openProject(p)}>
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
							title="reopen and open a VS Code window (reuses a running container)"
							onclick={() => openProject(p)}
						>
							{isRunning(p.name) ? 'Opening…' : 'Open'}
						</button>
					{/if}
					<!-- Pull origin and recreate the container, so a fix already
					     pushed to GitHub lands in the running devcontainer. Shown
					     even while the window is open, which is exactly when you
					     need it. -->
					<button
						disabled={isRunning(p.name)}
						title="pull origin, then recreate the container"
						onclick={() => rebuild(p.name)}
					>
						{isRunning(p.name) ? 'Rebuilding…' : 'Rebuild'}
					</button>
				{/if}
				{#if offer[p.name]}
					<button class="warn" disabled={!!busy[p.name]} onclick={() => act(p.name, 'close', { force: true })}>
						Force stop
					</button>
				{/if}
			{/snippet}

			{#snippet note(p)}
				{#if answers[p.name]}
					<p class="note" class:bad={!answers[p.name].ok} role="status">
						{answers[p.name].text}
						{#if answers[p.name].url}
							<a href={answers[p.name].url} target="_blank" rel="noopener">open login ↗</a>
						{/if}
					</p>
				{/if}
			{/snippet}
		</ActionList>
	{/if}
</main>

<!-- The Provision picker opens in its own browser window (`?provision=1`), the
     way a job does, so there is a dedicated screen for it on a phone rather than
     a disclosure at the foot of the list. It sits below the job view in z-order:
     starting a provision closes it and the build log takes the screen. -->
{#if pickerOpen}
	<section class="provision-view" aria-label="Provision a repo">
		<div class="sheet-head">
			<strong>Provision a repo</strong>
			<button class="btn" onclick={() => (pickerOpen = false)}>Close</button>
		</div>
		{#if reposError}
			<p class="notice bad">{reposError}</p>
		{/if}
		{#if repos === null}
			<p class="muted">Loading repositories…</p>
		{:else}
			<input type="search" bind:value={repoFilter} placeholder="Filter repositories…" />
			<ActionList items={repoRows} emptyMessage="Nothing here that has no workspace yet.">
				{#snippet actions(row)}
					<button
						disabled={isRunning(row.name)}
						onclick={() => {
							pickerOpen = false;
							startJob('/api/provision', { repo: row.name }, row.name);
						}}
					>
						{isRunning(row.name) ? 'Cloning…' : 'Provision'}
					</button>
				{/snippet}
			</ActionList>
		{/if}
	</section>
{/if}

<!-- The job is a sibling of <main>, not a child: it is a full-screen view, so
     it must not inherit main's pull-to-refresh touch handlers (a drag on the
     log would otherwise read as a pull), and it takes the whole screen for
     minutes at a time. The pane, its head and its action row come from
     `log-view`; the full-screen frame is this page's, because whether output
     takes the screen is a decision about a phone. -->
{#if job}
	<section class="job" class:bad={job.state === 'failed'} aria-label="Job output">
		<LogView
			title={`${job.kind || 'job'} ${job.target ?? ''}`.trim()}
			state={job.state}
			tone={job.state}
			lines={job.log ?? []}
			emptyMessage="Waiting for output…"
			bind:element={logEl}
			onscroll={onLogScroll}
		>
			{#snippet notice()}
				{#if job.error}
					<p class="notice bad">{job.error}</p>
				{/if}
				{#if job.attention}
					<!-- A job paused on something only the phone can do — the
					     Tailscale login. Louder than a note, because the job does
					     not move until it is done, and the link must be a real tap
					     target, not text in the log. -->
					<p class="notice warn" role="alert">
						<span>{job.attention.detail || 'Action needed to continue'}</span>
						{#if job.attention.url}
							<a class="login-link" href={job.attention.url} target="_blank" rel="noopener">
								Authenticate on Tailscale ↗
							</a>
						{/if}
					</p>
				{/if}
				{#if job.state === 'running'}
					{#if job.attention}
						<p class="hint">
							Waiting for you to authenticate on the tailnet — the job continues by itself once
							it is done.
						</p>
					{:else}
						<p class="hint">
							Running — a first container build takes minutes. The log follows the tail; scroll
							up to read back.
						</p>
					{/if}
				{/if}
			{/snippet}

			{#snippet actions()}
				{#if job.job_id}
					<a class="btn" href={`?job=${job.job_id}`} target="_blank" rel="noopener">New window</a>
				{/if}
				{#if job.state !== 'running'}
					<button onclick={() => (job = null)}>Dismiss</button>
				{/if}
			{/snippet}
		</LogView>
	</section>
{/if}

<style>
	/* The theme owns colour, type and the base elements now (web/src/app.css).
	   What is left here is this page's layout: a phone column, the fixed views,
	   and the two places the app's own controls need the house --tap. */
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
		color: var(--ink-muted);
	}
	/* The two controls that are the page's own rather than a block's: the
	   refresh button and the picker's Close. Both are pressed with a thumb. */
	.icon {
		min-width: var(--tap);
		min-height: var(--tap);
		font-size: 1.2rem;
	}
	.provision-top {
		display: flex;
		align-items: center;
		justify-content: center;
		box-sizing: border-box;
		width: 100%;
		min-height: var(--tap);
		margin-bottom: 0.75rem;
		font-size: 0.95rem;
	}
	.strip-slot {
		margin-bottom: 1rem;
	}
	/* The ssh target, selectable: over plain http (the tailnet) there is no
	   clipboard API, so a long-press select is how this gets copied on a phone. */
	.ssh {
		font-size: 0.75rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
		user-select: all;
		-webkit-user-select: all;
	}
	.conn {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.35rem;
	}
	.conn :global(.btn) {
		min-height: var(--tap);
	}
	.note {
		margin: 0;
		font-size: 0.8rem;
		color: var(--ink-muted);
	}
	.note.bad {
		color: var(--bad);
	}
	.login-link {
		margin-left: 0.4rem;
		font-weight: 600;
	}
	.pull {
		margin: 0 0 0.5rem;
		text-align: center;
		font-size: 0.8rem;
		color: var(--ink-muted);
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
		gap: 0;
		background: var(--bg);
		border: none;
		border-radius: 0;
		padding: calc(0.9rem + env(safe-area-inset-top)) 1rem
			calc(0.9rem + env(safe-area-inset-bottom));
	}
	.job.bad {
		background: var(--bad-bg);
	}
	.provision-view {
		position: fixed;
		inset: 0;
		z-index: 40;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		margin: 0;
		padding: calc(0.9rem + env(safe-area-inset-top)) 1rem
			calc(0.9rem + env(safe-area-inset-bottom));
		background: var(--bg);
		overflow: auto;
	}
	.sheet-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.sheet-head :global(.btn) {
		min-height: var(--tap);
	}
	input[type='search'] {
		width: 100%;
		box-sizing: border-box;
		min-height: var(--tap);
	}
</style>
