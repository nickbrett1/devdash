<script>
	// The embed: just the status strip, for the NAS dashboard to iframe.
	//
	// The dashboard card could have been a `customapi` widget, but that is a
	// fixed list of label/value pairs with no room for the memory meter — and
	// it could not show what the VM has in total, only what is in use. An
	// iframe of the real component renders the same numbers the page does,
	// including the used/total bar, and cannot drift from it.
	//
	// The dashboard's `href` still points at `/`: this route is for reading,
	// not for pressing buttons.
	import { onMount } from 'svelte';
	import StatusStrip from '$lib/StatusStrip.svelte';

	const REFRESH_MS = 15_000;

	let status = $state(null);

	async function load() {
		try {
			const s = await fetch('/api/status');
			if (!s.ok) return;
			status = await s.json();
		} catch {
			// Keep the last good numbers rather than blanking the tile: the
			// dashboard shows the previous sample until the next poll.
		}
	}

	onMount(() => {
		load();
		const timer = setInterval(load, REFRESH_MS);
		return () => clearInterval(timer);
	});
</script>

<svelte:head>
	<title>devdash</title>
	<meta name="color-scheme" content="dark" />
</svelte:head>

{#if status}
	<StatusStrip {status} />
{:else}
	<p class="wait">…</p>
{/if}

<style>
	/* Transparent, so the dashboard card's own background shows through and the
	   tile reads as part of the card rather than a box inside it. */
	:global(body) {
		background: transparent;
		margin: 0;
		font-family:
			-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
		color: #e2e8f0;
	}
	.wait {
		margin: 0;
		color: #6b7280;
	}
</style>
