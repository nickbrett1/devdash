<script>
	// The embed: just the meter strip, for the NAS dashboard to iframe.
	//
	// The dashboard card could have been a `customapi` widget, but that is a
	// fixed list of label/value pairs with no room for the memory bar — and
	// it could not show what the VM has in total, only what is in use. An
	// iframe of the same `meter-strip` block the page renders cannot drift from
	// the page, which is the reason the block exists.
	//
	// The dashboard's `href` still points at `/`: this route is for reading,
	// not for pressing buttons.
	import { onMount } from 'svelte';
	import '../../app.css';
	import MeterStrip from '$lib/components/nb/MeterStrip.svelte';
	import { meterProps } from '$lib/status.js';

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
	<MeterStrip {...meterProps(status)} />
{:else}
	<p class="wait">…</p>
{/if}

<style>
	/* Transparent, so the dashboard card's own background shows through and the
	   tile reads as part of the card rather than a box inside it. Both the
	   variable and the declaration are set on <html>: the theme paints
	   `html, body` with `var(--bg)`, and the root element's background is what
	   the canvas actually takes, so overriding it on <body> alone would only
	   half-work and depend on which stylesheet won the cascade. */
	:global(html) {
		--bg: transparent;
		background: transparent;
	}
	:global(body) {
		background: transparent;
	}
	.wait {
		margin: 0;
		color: var(--ink-faint);
	}
</style>
