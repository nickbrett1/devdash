import adapter from "@sveltejs/adapter-static";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // adapter-static: the SvelteKit app builds to static assets that the
    // python server serves. It is not a Node server.
    // See https://kit.svelte.dev/docs/adapter-static for more information.
    adapter: adapter(),
  },
};

export default config;
