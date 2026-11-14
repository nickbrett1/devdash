<script>
  /**
   * LogView — a job's output, in a panel with its state and its controls.
   *
   * Lifted from devdash, where `open`, `rebuild` and `provision` all take minutes
   * on a first build and answer with a job id to poll. The log is the page you
   * live in while that happens, so it is a full screen on a phone, and it is the
   * one place in that app where a `pre` is the right element.
   *
   * `element` is `$bindable` on purpose: the interesting behaviour — follow the
   * tail until the reader scrolls up, then stay where they put it — needs the
   * scrolling node, and it belongs to the app, not the block. A block that owned
   * it would have to own a scroll position, a "following" flag and the rules for
   * leaving it, which is three opinions about a page it cannot see.
   *
   * So: the block owns the frame (head, state pill, notice, the monospace pane,
   * the action row) and hands back the node. `onscroll` is passed straight
   * through for the same reason.
   */
  let {
    title = "Output",
    /** the job's state, e.g. running | done | failed */
    state = "",
    tone = "",
    lines = [],
    emptyMessage = "Waiting for output…",
    /** bindable: the <pre>, for a caller that follows the tail */
    element = $bindable(),
    onscroll,
    actions,
    notice,
  } = $props();
</script>

<section class="card log-view" aria-label={title}>
  <div class="head">
    <strong class="title">{title}</strong>
    {#if state}
      <span class="pill {tone}">{state}</span>
    {/if}
  </div>

  {#if notice}
    {@render notice()}
  {/if}

  {#if lines.length > 0}
    <pre bind:this={element} {onscroll}>{lines.join("\n")}</pre>
  {:else}
    <p class="hint waiting">{emptyMessage}</p>
  {/if}

  {#if actions}
    <div class="actions">{@render actions()}</div>
  {/if}
</section>

<style>
  .log-view {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }

  .title {
    overflow-wrap: anywhere;
  }

  .pill.running {
    background: var(--warn-bg);
    color: var(--warn);
  }

  .pill.done {
    background: var(--ok-bg);
    color: var(--ok);
  }

  .pill.failed {
    background: var(--bad-bg);
    color: var(--bad);
  }

  pre {
    margin: 0;
    padding: 10px 12px;
    border-radius: 6px;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.45;
    color: var(--ink-dim);
    /* The pane, not the page, is what scrolls: the reader must be able to sit
       at the bottom of a build log without losing the head and the buttons. */
    max-height: min(60vh, 34rem);
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    -webkit-overflow-scrolling: touch;
  }

  .waiting {
    margin: 0;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .actions :global(button),
  .actions :global(a) {
    min-height: var(--tap);
    padding: 0 12px;
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: 13px;
    color: var(--ink);
    background: var(--bg-raised);
    border: 1px solid var(--line-strong);
    border-radius: 6px;
    text-decoration: none;
    cursor: pointer;
  }

  .actions :global(button:hover),
  .actions :global(a:hover) {
    background: var(--bg-hover);
  }
</style>
