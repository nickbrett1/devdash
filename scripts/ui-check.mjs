#!/usr/bin/env node
/**
 * ui-check.mjs — cheap, deterministic UI verdict for a running preview server
 * (point UI_BASE_URL at a dev server to check that instead).
 *
 * Replaces "screenshot the page and ask a vision model" with a text verdict:
 * a few hundred tokens of JSON instead of a ~25k-token image call.
 *
 * Two tiers, cheapest first:
 *   Tier 1 — plain `fetch` of the URL, assert on the server-rendered HTML.
 *            No browser, no dependencies, runs anywhere Node 22 does.
 *   Tier 2 — if a shared Chrome DevTools Protocol endpoint answers, run the
 *            page in that browser and collect real runtime truth: uncaught
 *            exceptions, console errors, failed requests, element visibility,
 *            horizontal overflow.
 *
 * THE OUTPUT ALWAYS NAMES THE TIER THAT ACTUALLY RAN. That is the point of the
 * script: "the console was clean" must never be a lie caused by an unreachable
 * browser. A degraded run says it degraded, and says why.
 *
 * Zero dependencies. Node 22+ (uses the global fetch and WebSocket).
 *
 * Usage:
 *   node scripts/ui-check.mjs [options]
 *
 *   --url <url>                Page URL to check        (env UI_BASE_URL, default http://127.0.0.1:4173)
 *   --browser-url <url>        Where the browser should  (env UI_CHECK_BROWSER_URL)
 *                              load the page. Different
 *                              from --url; see below.
 *   --cdp <url>                CDP base endpoint        (env UI_CDP_URL)
 *   --expect-text <string>     Assert this text appears (repeatable)
 *   --expect-selector <css>    Assert this element exists/visible (repeatable)
 *   --expect-status <code>     Expected HTTP status     (default 200)
 *   --no-cdp                   Force tier 1 only
 *   --timeout <ms>             Overall budget           (default 15000)
 *   --strict                   Treat failed subresource requests as errors
 *   --json                     Print the JSON verdict only
 *   -h, --help                 This text
 *
 * Exit codes: 0 pass, 1 check failed, 2 usage/config error.
 *
 * WHY --url AND --browser-url ARE SEPARATE (do not "simplify" this away)
 * ---------------------------------------------------------------------
 * Tier 1 runs *inside the devcontainer*, so 127.0.0.1:<port> is the dev server.
 * Tier 2 runs the page in a browser that lives on the *host*. A container's
 * 127.0.0.1 is meaningless to that browser. The page is only reachable from the
 * host because VS Code's SharedProcessTunnelService auto-forwards the
 * container's port — i.e. it works while VS Code is attached and nowhere else.
 * So tier 2 takes a browser-reachable URL and defaults to the page URL only
 * with that caveat recorded. Headless CI has no such forward: tier 2 is a
 * devcontainer-local capability, and this script says so instead of failing.
 */

const DEFAULT_TIMEOUT_MS = 15000;
const DEFAULT_PAGE_URL = 'http://127.0.0.1:4173';

// Resolution order for the CDP endpoint. The OrbStack host alias is a DEFAULT,
// not a hardcode: host.docker.internal does not resolve inside these containers
// (`getent hosts` exits 2, Node dns gives ENOTFOUND; lookups go to the Tailscale
// MagicDNS nameserver at 100.100.100.100). So 0.250.250.254 is the default
// because it works here, and UI_CDP_URL overrides both.
const DEFAULT_CDP_CANDIDATES = ['http://0.250.250.254:9222', 'http://host.docker.internal:9222'];

const OVERFLOW_TOLERANCE_PX = 2;

// A CDP command that never gets a response must not be allowed to outlive the
// overall budget: Page.navigate in particular stays pending for as long as the
// loader is unresolved (a blackholed host, or a container-loopback URL the host
// browser cannot route to). This is the per-command ceiling; `CdpSession` also
// clamps it to whatever remains of --timeout, so --timeout bounds the whole run.
const DEFAULT_CDP_COMMAND_TIMEOUT_MS = 10000;

// A cleanup command (today only Target.closeTarget) must NOT inherit the run's
// verdict deadline. The deadline exists to bound *producing a verdict*, and the
// paths that exhaust it — a stalled navigation, a page that never fires load, a
// slow Runtime.evaluate — are exactly the paths where a browser target was
// created and has to be closed. `send()` clamps to whatever remains of the
// deadline, so after an overrun its budget collapses to the 1ms floor and the
// close rejects; the caller swallows that reject, so the tab leaks silently.
// Hence a small, fixed allowance that ignores the deadline. This is not a second
// clock: it bounds one cleanup round-trip, not the run. Cost: wall-clock may
// exceed --timeout by up to this much, and only when the target was created.
const CDP_CLEANUP_TIMEOUT_MS = 2000;

// When tier 2 is about to load a loopback URL in a *host* browser with no
// --browser-url, the odds are known-bad: the host's 127.0.0.1 is the host, not
// the container. It still works under VS Code's port auto-forward, so it is
// attempted rather than skipped — but with a short cap, so the common failing
// case degrades in ~3s instead of burning the whole budget to learn it.
//
// THE TRADE, STATED SO NOBODY "FIXES" THIS UPWARD WITHOUT READING IT:
// 3s is shorter than the 15s default overall budget on purpose, and that means
// a loopback page that is slow-but-working — most often a *cold* port forward
// that needs more than 3s to establish on the first navigation — is demoted to
// tier 1's verdict on that run even though the page itself is fine. That is
// accepted: the alternative (the old behaviour) was to hang with no verdict at
// all, which is strictly worse. Do not raise this toward the overall budget to
// "avoid the demotion": that reintroduces the hang for the common no-forward
// case, which is the case this script exists to survive. A caller who needs a
// genuinely slow loopback page checked must pass --browser-url (a host-reachable
// URL, no leash) rather than widen this.
const LOOPBACK_NAVIGATION_TIMEOUT_MS = 3000;

const isLoopbackUrl = (url) => /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])[:/]/.test(url);

// ---------------------------------------------------------------------------
// args
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const opts = {
    url: process.env.UI_BASE_URL || DEFAULT_PAGE_URL,
    browserUrl: process.env.UI_CHECK_BROWSER_URL || null,
    cdp: process.env.UI_CDP_URL || null,
    expectText: [],
    expectSelector: [],
    expectStatus: 200,
    cdpEnabled: true,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    strict: false,
    jsonOnly: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const v = argv[i + 1];
      if (v === undefined) throw new Error(`missing value for ${arg}`);
      i += 1;
      return v;
    };
    switch (arg) {
      case '--url': opts.url = next(); break;
      case '--browser-url': opts.browserUrl = next(); break;
      case '--cdp': opts.cdp = next(); break;
      case '--expect-text': opts.expectText.push(next()); break;
      case '--expect-selector': opts.expectSelector.push(next()); break;
      case '--expect-status': opts.expectStatus = Number(next()); break;
      case '--no-cdp': opts.cdpEnabled = false; break;
      case '--timeout': opts.timeoutMs = Number(next()); break;
      case '--strict': opts.strict = true; break;
      case '--json': opts.jsonOnly = true; break;
      case '-h': case '--help': opts.help = true; break;
      default: throw new Error(`unknown argument: ${arg}`);
    }
  }

  if (!Number.isFinite(opts.timeoutMs) || opts.timeoutMs <= 0) throw new Error('--timeout must be a positive number');
  if (!Number.isFinite(opts.expectStatus)) throw new Error('--expect-status must be a number');
  return opts;
}

const HELP = `ui-check.mjs — cheap UI verdict for a running preview server.

  node scripts/ui-check.mjs [--url URL] [--expect-text S] [--expect-selector CSS]

  --url <url>              page URL (env UI_BASE_URL, default ${DEFAULT_PAGE_URL})
  --browser-url <url>      URL tier 2's browser loads (env UI_CHECK_BROWSER_URL)
  --cdp <url>              CDP endpoint (env UI_CDP_URL)
  --expect-text <string>   assert text appears (repeatable)
  --expect-selector <css>  assert element exists/visible (repeatable)
  --expect-status <code>   expected status (default 200)
  --no-cdp                 force tier 1
  --timeout <ms>           overall budget (default ${DEFAULT_TIMEOUT_MS})
  --strict                 failed subresources are errors
  --json                   JSON verdict only

Exit 0 pass, 1 failed, 2 usage error.
`;

// ---------------------------------------------------------------------------
// verdict
// ---------------------------------------------------------------------------

class Verdict {
  constructor() {
    this.tier = null;
    this.tiersAttempted = [];
    this.errors = [];
    this.warnings = [];
    this.evidence = {};
    this.tier2SkipReason = null;
  }

  error(code, message) { this.errors.push({ code, message }); }
  warn(code, message) { this.warnings.push({ code, message }); }
}

// ---------------------------------------------------------------------------
// tier 1 — fetch and read the server-rendered HTML
// ---------------------------------------------------------------------------

function stripTags(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Minimal selector support for tier 1: #id, .class, tag, [attr=value], and
 * tag.class combinations. Tier 1 has no DOM, so anything fancier falls back to
 * a raw substring match on the HTML — which is deliberately conservative: it
 * can produce a false PASS for a complex selector, never a false FAIL. Tier 2
 * uses a real querySelector when a browser is available.
 */
function selectorLooksSatisfied(html, selector) {
  const sel = selector.trim();
  let m = /^#([\w-]+)$/.exec(sel);
  if (m) return new RegExp(`id\\s*=\\s*["']${m[1]}["']`).test(html);
  m = /^\.([\w-]+)$/.exec(sel);
  if (m) return new RegExp(`class\\s*=\\s*["'][^"']*\\b${m[1]}\\b`).test(html);
  m = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(sel);
  if (m) {
    return m[2] === undefined
      ? new RegExp(`\\b${m[1]}\\s*=`).test(html)
      : new RegExp(`\\b${m[1]}\\s*=\\s*["']${m[2]}["']`).test(html);
  }
  m = /^([a-z][\w-]*)\.([\w-]+)$/i.exec(sel);
  if (m) return new RegExp(`<${m[1]}\\b[^>]*class\\s*=\\s*["'][^"']*\\b${m[2]}\\b`, 'i').test(html);
  m = /^([a-z][\w-]*)$/i.exec(sel);
  if (m) return new RegExp(`<${m[1]}\\b`, 'i').test(html);
  return html.includes(sel);
}

function detectErrorPage(html) {
  if (/data-sveltekit-error/i.test(html)) return 'data-sveltekit-error marker present';
  const title = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  if (title && /^(internal error|error|500)\b/i.test(title[1].trim())) return `error-like title "${title[1].trim()}"`;
  if (/<h1[^>]*>\s*(internal error|something went wrong|application error)\s*<\/h1>/i.test(html)) return 'error heading in body';
  return null;
}

async function runTier1(opts, verdict, deadline) {
  verdict.tiersAttempted.push(1);

  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    verdict.error('timeout', 'no time budget left for tier 1');
    return;
  }

  let res;
  let html;
  try {
    res = await fetch(opts.url, {
      redirect: 'follow',
      signal: AbortSignal.timeout(remaining),
      headers: { 'user-agent': 'ui-check/1 (genproj)' },
    });
    html = await res.text();
  } catch (err) {
    verdict.error(
      'unreachable',
      `no server answered at ${opts.url} (${err.message}); "npm run preview" serves ${DEFAULT_PAGE_URL}, and UI_BASE_URL points this check at a dev server (vite dev defaults to 5173)`,
    );
    return;
  }

  verdict.tier = 1;
  verdict.evidence.status = res.status;
  verdict.evidence.bytes = html.length;

  if (res.status !== opts.expectStatus) {
    verdict.error('bad-status', `expected HTTP ${opts.expectStatus}, got ${res.status}`);
  }

  const errorPage = detectErrorPage(html);
  if (errorPage) verdict.error('sveltekit-error', `server returned an error page: ${errorPage}`);

  const title = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  if (title) verdict.evidence.title = title[1].trim();
  const h1 = /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
  if (h1) verdict.evidence.h1 = stripTags(h1[1]).slice(0, 200);

  const text = stripTags(html);
  verdict.evidence.textLength = text.length;

  for (const needle of opts.expectText) {
    if (!html.includes(needle) && !text.includes(needle)) {
      verdict.error('missing-text', `expected text not found: ${JSON.stringify(needle)}`);
    }
  }

  for (const selector of opts.expectSelector) {
    if (!selectorLooksSatisfied(html, selector)) {
      verdict.error('missing-selector', `expected element not found in HTML: ${selector}`);
    }
  }

  // An empty mount point is a classic SvelteKit symptom: SSR produced a shell
  // with nothing in it. Reported as a warning, not an error, because a static
  // page with an image or canvas legitimately has no text.
  if (text.length < 20 && !/<(img|canvas|svg|video)\b/i.test(html)) {
    verdict.warn('empty-shell', `page has almost no text after tag-stripping (${text.length} chars) — is the app shell empty?`);
  }

  // Hydration marker: SvelteKit inlines a script defining a __sveltekit_ symbol.
  // Absence is only meaningful when the page *is* SvelteKit, which tier 1 cannot
  // know for certain, so this is a warning.
  if (!/__sveltekit_/.test(html)) {
    verdict.warn('no-hydration-marker', 'no __sveltekit_ hydration marker — expected for SvelteKit, ignorable for static or plain Svelte (CSR) pages');
  }
}

// ---------------------------------------------------------------------------
// minimal CDP client over the global WebSocket
// ---------------------------------------------------------------------------

class CdpSession {
  /**
   * @param ws        an open WebSocket
   * @param deadline  epoch ms after which no command may still be pending
   */
  constructor(ws, deadline = Number.POSITIVE_INFINITY) {
    this.ws = ws;
    this.deadline = deadline;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener('message', (event) => this._onMessage(event));
  }

  _onMessage(event) {
    let msg;
    try {
      msg = JSON.parse(typeof event.data === 'string' ? event.data : Buffer.from(event.data).toString('utf8'));
    } catch {
      return;
    }
    if (msg.id !== undefined && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(timer);
      if (msg.error) reject(new Error(`${msg.error.message}${msg.error.data ? ` (${msg.error.data})` : ''}`));
      else resolve(msg.result);
      return;
    }
    if (msg.method) for (const fn of this.listeners) fn(msg);
  }

  on(fn) { this.listeners.push(fn); }

  /**
   * Send a CDP command and settle on its response, its send error, or the
   * command timeout — never on silence. The timeout is the smaller of the
   * per-command ceiling and whatever remains of the overall deadline, so a
   * stalled command cannot make the run outlive --timeout.
   */
  send(method, params = {}, sessionId = undefined, timeoutMs = undefined) {
    const budget = Math.max(
      1,
      Math.min(timeoutMs ?? DEFAULT_CDP_COMMAND_TIMEOUT_MS, this.deadline - Date.now()),
    );
    return this._dispatch(method, params, sessionId, budget);
  }

  /**
   * Like send(), but with a fixed budget that deliberately ignores the overall
   * deadline. Only for cleanup that must still happen once the verdict budget
   * is spent — see CDP_CLEANUP_TIMEOUT_MS for why inheriting the deadline here
   * makes the close a silent no-op on exactly the paths that need it.
   */
  sendCleanup(method, params = {}, sessionId = undefined) {
    return this._dispatch(method, params, sessionId, CDP_CLEANUP_TIMEOUT_MS);
  }

  _dispatch(method, params, sessionId, budget) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        reject(new Error(`CDP command ${method} did not respond within ${budget}ms`));
      }, budget);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.ws.send(JSON.stringify(payload));
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  close() {
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      clearTimeout(pending.timer);
      pending.reject(new Error('CDP session closed before the command responded'));
    }
    try { this.ws.close(); } catch { /* already gone */ }
  }
}

function openWebSocket(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (typeof WebSocket !== 'function') {
      reject(new Error('this Node has no global WebSocket (need Node 22+)'));
      return;
    }
    const ws = new WebSocket(url);
    const timer = setTimeout(() => {
      try { ws.close(); } catch { /* ignore */ }
      reject(new Error(`timed out opening ${url}`));
    }, timeoutMs);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(ws); }, { once: true });
    ws.addEventListener('error', () => {
      clearTimeout(timer);
      reject(new Error(`could not open ${url}`));
    }, { once: true });
  });
}

async function resolveCdpBase(opts, deadline) {
  const candidates = opts.cdp ? [opts.cdp] : DEFAULT_CDP_CANDIDATES;
  const tried = [];

  for (const base of candidates) {
    const budget = Math.min(1500, deadline - Date.now());
    if (budget <= 0) return { skipReason: 'no time budget left to probe the browser endpoint', tried };
    try {
      const res = await fetch(`${base}/json/version`, { signal: AbortSignal.timeout(budget) });
      if (!res.ok) { tried.push(`${base} → HTTP ${res.status}`); continue; }
      const version = await res.json();
      if (!version.webSocketDebuggerUrl) { tried.push(`${base} → no webSocketDebuggerUrl`); continue; }
      // The WS URL is composed from the Host header of the request that asked
      // for it, so it must be dialled on the same address it was discovered on.
      return { base, version, wsUrl: version.webSocketDebuggerUrl, tried };
    } catch (err) {
      tried.push(`${base} → ${err.message}`);
    }
  }
  return { skipReason: 'no CDP endpoint answered', tried };
}

// ---------------------------------------------------------------------------
// tier 2 — run the page in the shared browser
// ---------------------------------------------------------------------------

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * Decide whether tier 2 looked at a DIFFERENT document than tier 1, and so
 * whether its runtime findings must not be attributed to the app.
 *
 * This is the misattribution guard. When the preview server is not the one the
 * check thinks it is (a squatter on the port, a stale tab, a proxy), tier 2 can
 * load a foreign error page — a same-origin 404 document, for example — and
 * report *its* console error as the app's. Tier 1 already fetched the real page
 * and got `expectStatus` cleanly, so the honest outcome is to keep tier 1's
 * verdict and say tier 2 stopped early. It is the same degradation contract as
 * an unreachable browser: demote, never pass.
 *
 * The trigger is the main-frame DOCUMENT status and nothing else. A client-side
 * title change after hydration is legitimate; title may corroborate a message,
 * never fire the demotion. All of the following are required:
 *   - tier 1 saw exactly `expectStatus`, with no errors;
 *   - tier 2's document status is >= 400 and differs from tier 1's;
 *   - tier 2's page origin is the same origin as `opts.url` (a cross-origin
 *     page is a deliberate --browser-url, not a squatter).
 */
function shouldDemoteTier2OnDocumentStatus({
  expectStatus,
  tier1Status,
  tier1ErrorCount,
  documentStatus,
  pageUrl,
  url,
}) {
  if (tier1Status === null || tier1Status === undefined) return false;
  if (tier1Status !== expectStatus || tier1ErrorCount !== 0) return false;
  if (documentStatus === null || documentStatus === undefined) return false;
  if (documentStatus < 400) return false;
  if (documentStatus === tier1Status) return false;
  const pageOrigin = originOf(pageUrl);
  if (pageOrigin === null || pageOrigin !== originOf(url)) return false;
  return true;
}

async function runTier2(opts, verdict, deadline) {
  verdict.tiersAttempted.push(2);

  const cdp = await resolveCdpBase(opts, deadline);
  if (!cdp.base) {
    verdict.tier2SkipReason = cdp.skipReason;
    if (cdp.tried.length) verdict.evidence.cdpProbes = cdp.tried;
    return;
  }

  verdict.evidence.browserUrl = cdp.base;
  verdict.evidence.browserVersion = cdp.version.Browser || cdp.version['User-Agent'] || null;

  let ws;
  try {
    ws = await openWebSocket(cdp.wsUrl, Math.min(5000, deadline - Date.now()));
  } catch (err) {
    verdict.tier2SkipReason = `CDP websocket failed: ${err.message}`;
    return;
  }

  const session = new CdpSession(ws, deadline);
  let targetId = null;

  try {
    const { targetId: created } = await session.send('Target.createTarget', { url: 'about:blank' });
    targetId = created;

    const { sessionId } = await session.send('Target.attachToTarget', { targetId, flatten: true });

    const exceptions = [];
    const consoleErrors = [];
    const failedRequests = [];
    let loadFired = false;
    // The main-frame document response, as the browser saw it.
    // `responseReceived` fires again for each hop in a redirect chain and once
    // for the final response, so the last Document entry is the document
    // actually rendered. A 404 *document* is not a Network.loadingFailed — which
    // is why a foreign error page used to be attributed to the app instead of
    // ending the check.
    let documentStatus = null;
    let documentUrl = null;
    let documentMimeType = null;

    session.on((msg) => {
      if (!msg.method) return;
      const params = msg.params || {};
      switch (msg.method) {
        case 'Runtime.exceptionThrown': {
          const d = params.exceptionDetails || {};
          const desc = (d.exception && (d.exception.description || d.exception.value)) || d.text || 'uncaught exception';
          exceptions.push(String(desc).split('\n')[0].slice(0, 300));
          break;
        }
        case 'Log.entryAdded': {
          const e = params.entry || {};
          if (e.level === 'error') consoleErrors.push(`${e.source || 'log'}: ${String(e.text || '').slice(0, 300)}`);
          break;
        }
        case 'Runtime.consoleAPICalled': {
          if (params.type !== 'error') break;
          const text = (params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
          consoleErrors.push(`console: ${String(text).slice(0, 300)}`);
          break;
        }
        case 'Network.responseReceived': {
          // Last Document wins, so a redirect chain resolves to the document
          // the browser actually rendered.
          if (params.type !== 'Document' || !params.response) break;
          documentStatus = params.response.status;
          documentUrl = params.response.url || null;
          documentMimeType = params.response.mimeType || null;
          break;
        }
        case 'Network.loadingFailed': {
          failedRequests.push({
            url: params.url || params.requestId || '(unknown)',
            error: params.errorText || 'failed',
            type: params.type || null,
          });
          break;
        }
        case 'Page.loadEventFired': loadFired = true; break;
        default: break;
      }
    });

    // Domains BEFORE navigation — creating the target already navigated to
    // about:blank, and everything thrown during the real load must be captured.
    await session.send('Runtime.enable', {}, sessionId);
    await session.send('Log.enable', {}, sessionId);
    await session.send('Network.enable', {}, sessionId);
    await session.send('Page.enable', {}, sessionId);

    const pageUrl = opts.browserUrl || opts.url;
    // When no browser URL is supplied and the page is loopback, tier 2 loads
    // the container's own 127.0.0.1 in a *host* browser. That cannot work
    // unless VS Code auto-forwards the port, so it is warned about and then
    // attempted with a short leash: a bounded attempt that degrades is worth
    // one cheap try, because the auto-forward is exactly the interactive
    // devcontainer case this script is written for. See LOOPBACK_NAVIGATION_TIMEOUT_MS.
    const navigatingLoopbackInHostBrowser = !opts.browserUrl && isLoopbackUrl(opts.url);
    if (navigatingLoopbackInHostBrowser) {
      verdict.warn(
        'browser-url-is-loopback',
        `tier 2 loaded ${pageUrl} in a host browser. That only works while VS Code forwards the container port, so this run falls back to tier 1 if the host browser cannot reach it; set --browser-url to a host-reachable URL for headless runs.`,
      );
    }

    let nav;
    try {
      nav = await session.send(
        'Page.navigate',
        { url: pageUrl },
        sessionId,
        navigatingLoopbackInHostBrowser ? LOOPBACK_NAVIGATION_TIMEOUT_MS : undefined,
      );
    } catch (err) {
      // The browser accepted Page.navigate but never answered. In practice this
      // is the loopback/no-port-forward case: the navigation never settles and
      // the page is simply not reachable from the host browser. That is an
      // infrastructure gap, not a defect in the page — tier 1 already judged
      // the page itself, so keep its verdict and say why tier 2 stopped.
      verdict.tier2SkipReason = `page did not load in the host browser (navigation to ${pageUrl} did not complete: ${err.message})`;
      verdict.evidence.tier2StoppedEarly = true;
      return;
    }
    if (nav && nav.errorText) {
      verdict.tier2SkipReason = `browser could not load the page: ${nav.errorText}`;
      verdict.evidence.tier2StoppedEarly = true;
      return;
    }

    const loadDeadline = Math.min(Date.now() + 10000, deadline);
    while (!loadFired && Date.now() < loadDeadline) await new Promise((r) => setTimeout(r, 50));

    if (!loadFired && failedRequests.some((r) => r.type === 'Document')) {
      // The page never loaded in the browser. That is an infrastructure gap
      // (no port forward), not a defect in the page — tier 1 already judged the
      // page itself. Say so rather than inventing a failure.
      verdict.tier2SkipReason = 'page did not load in the host browser (likely no container→host port forward)';
      verdict.evidence.tier2StoppedEarly = true;
      return;
    }

    // Record what the browser actually loaded. Kept even when tier 2 proceeds:
    // the document status is the primary evidence that tier 1 and tier 2 looked
    // at the same page.
    if (documentStatus !== null) {
      verdict.evidence.documentStatus = documentStatus;
      verdict.evidence.documentUrl = documentUrl;
      verdict.evidence.documentMimeType = documentMimeType;
    }

    // Misattribution guard: if the browser loaded a foreign (same-origin 4xx/5xx)
    // document, its console errors belong to that page, not the app. Keep tier 1's
    // verdict and stop BEFORE verdict.tier = 2 and before emitting any tier-2
    // finding, so the foreign document cannot overwrite evidence.title/h1 either.
    // Title and h1 never participate in this decision: a client-side title change
    // after hydration is legitimate.
    if (
      shouldDemoteTier2OnDocumentStatus({
        expectStatus: opts.expectStatus,
        tier1Status: verdict.evidence.status,
        tier1ErrorCount: verdict.errors.length,
        documentStatus,
        pageUrl,
        url: opts.url,
      })
    ) {
      verdict.tier2SkipReason =
        `tier 2 loaded a different document (HTTP ${documentStatus} from ${documentUrl || pageUrl}) ` +
        `than tier 1 (HTTP ${verdict.evidence.status} from ${opts.url}); ` +
        'its console/network findings are not attributed to the app';
      verdict.evidence.tier2StoppedEarly = true;
      return;
    }

    // Give late runtime errors a moment to surface.
    await new Promise((r) => setTimeout(r, 250));

    const probe = `(() => {
      const out = { title: document.title, url: location.href, selectors: [], overflow: null };
      out.scrollWidth = document.documentElement.scrollWidth;
      out.clientWidth = document.documentElement.clientWidth;
      for (const sel of ${JSON.stringify(opts.expectSelector)}) {
        let found = null;
        try {
          const el = document.querySelector(sel);
          if (el) {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            found = {
              selector: sel,
              present: true,
              width: Math.round(r.width),
              height: Math.round(r.height),
              display: cs.display,
              visibility: cs.visibility,
              opacity: cs.opacity,
              visible: r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity) > 0,
            };
          }
        } catch (err) {
          found = { selector: sel, present: false, invalid: String(err && err.message) };
        }
        out.selectors.push(found || { selector: sel, present: false });
      }
      return out;
    })()`;

    const evaluated = await session.send('Runtime.evaluate', { expression: probe, returnByValue: true, awaitPromise: true }, sessionId);
    const value = evaluated.result && evaluated.result.value;

    verdict.tier = 2;

    if (value) {
      verdict.evidence.title = value.title || verdict.evidence.title;
      verdict.evidence.browserPageUrl = value.url || pageUrl;
      verdict.evidence.overflow = {
        scrollWidth: value.scrollWidth,
        clientWidth: value.clientWidth,
        horizontal: value.scrollWidth - value.clientWidth,
      };

      if (value.scrollWidth - value.clientWidth > OVERFLOW_TOLERANCE_PX) {
        verdict.error(
          'overflow',
          `horizontal overflow: scrollWidth ${value.scrollWidth} > clientWidth ${value.clientWidth}`,
        );
      }

      for (const sel of value.selectors || []) {
        if (sel.invalid) {
          verdict.error('bad-selector', `"${sel.selector}" is not a valid CSS selector: ${sel.invalid}`);
        } else if (!sel.present) {
          verdict.error('missing-selector', `element not present after hydration: ${sel.selector}`);
        } else if (!sel.visible) {
          verdict.error(
            'not-visible',
            `${sel.selector} is in the DOM but not rendered (${sel.width}x${sel.height}, display:${sel.display}, visibility:${sel.visibility}, opacity:${sel.opacity})`,
          );
        }
      }
    }

    for (const message of exceptions) verdict.error('uncaught-exception', message);
    for (const message of consoleErrors) verdict.error('console-error', message);

    const documentFailures = failedRequests.filter((r) => r.type === 'Document');
    for (const failure of documentFailures) verdict.error('request-failed', `document request failed: ${failure.url} (${failure.error})`);

    const subresourceFailures = failedRequests.filter((r) => r.type !== 'Document');
    if (subresourceFailures.length) {
      const sample = subresourceFailures.slice(0, 5).map((r) => `${r.type || 'resource'} ${r.url} (${r.error})`).join('; ');
      if (opts.strict) verdict.error('subresource-failed', sample);
      else verdict.warn('subresource-failed', `${subresourceFailures.length} subresource request(s) failed: ${sample}`);
    }

    verdict.evidence.consoleErrors = consoleErrors.length;
    verdict.evidence.exceptions = exceptions.length;
    verdict.evidence.failedRequests = failedRequests.length;
  } catch (err) {
    // Anything thrown while driving CDP is our problem, not the page's. If tier 1
    // already produced a verdict, keep it and record why tier 2 stopped.
    if (verdict.tier === 1) verdict.tier2SkipReason = `tier 2 aborted: ${err.message}`;
    else verdict.error('cdp-failure', `could not complete the browser pass: ${err.message}`);
  } finally {
    if (targetId) {
      // sendCleanup, not send: by the time we get here the deadline may already
      // be spent (that is often why we are here), and send() would clamp the
      // close to 1ms and reject into the catch below — leaving the tab behind
      // with no evidence. See CDP_CLEANUP_TIMEOUT_MS.
      try { await session.sendCleanup('Target.closeTarget', { targetId }); } catch { /* browser may be gone */ }
    }
    session.close();
  }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  const startedAt = Date.now();
  const opts = parseArgs(process.argv.slice(2));

  if (opts.help) {
    process.stdout.write(HELP);
    return 0;
  }

  const verdict = new Verdict();
  const deadline = startedAt + opts.timeoutMs;

  await runTier1(opts, verdict, deadline);
  if (opts.cdpEnabled) await runTier2(opts, verdict, deadline);

  const ok = verdict.errors.length === 0;
  const tier = verdict.tier;

  const report = {
    ok,
    tier,
    tiersAttempted: verdict.tiersAttempted,
    url: opts.url,
    browserUrl: verdict.evidence.browserUrl || null,
    durationMs: Date.now() - startedAt,
    summary: ok
      ? `tier ${tier === null ? '(none)' : tier} passed${verdict.tier2SkipReason ? ` — browser pass skipped: ${verdict.tier2SkipReason}` : ''}`
      : `tier ${tier === null ? '(none)' : tier} failed with ${verdict.errors.length} error(s)`,
    tier2SkipReason: verdict.tier2SkipReason,
    errors: verdict.errors,
    warnings: verdict.warnings,
    evidence: verdict.evidence,
  };

  if (opts.jsonOnly) {
    process.stdout.write(`${JSON.stringify(report)}\n`);
  } else {
    const lines = [];
    lines.push(`${ok ? 'PASS' : 'FAIL'}  tier ${tier === null ? 'none' : tier}  ${opts.url}  (${report.durationMs}ms)`);
    if (verdict.evidence.title) lines.push(`  title: ${verdict.evidence.title}`);
    for (const e of verdict.errors) lines.push(`  ERROR  ${e.code}: ${e.message}`);
    for (const w of verdict.warnings) lines.push(`  warn   ${w.code}: ${w.message}`);
    if (verdict.tier2SkipReason) lines.push(`  note   browser pass skipped: ${verdict.tier2SkipReason}`);
    lines.push(`  ${JSON.stringify(report)}`);
    process.stdout.write(`${lines.join('\n')}\n`);
  }

  return ok ? 0 : 1;
}

main()
  .then((code) => { process.exitCode = code; })
  .catch((err) => {
    process.stderr.write(`ui-check: ${err.message}\n`);
    process.exitCode = 2;
  });
