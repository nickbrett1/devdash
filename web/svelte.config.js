import adapter from "@sveltejs/adapter-static";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // adapter-static: the SvelteKit app builds to static assets that the
    // python server serves. It is not a Node server.
    // See https://kit.svelte.dev/docs/adapter-static for more information.
    //
    // Output to dist/ (not adapter-static's default build/) because that is
    // what devdash's config.web_dir — "web/dist" — is resolved against, and an
    // install that builds one directory and serves another produces a working
    // API next to a 404 front page.
    adapter: adapter({ pages: "dist", assets: "dist" }),
  },
};

export default config;
