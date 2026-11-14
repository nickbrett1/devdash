<script>
  /**
   * ActionList — one row per thing you can act on.
   *
   * Lifted from devdash, whose whole page is this list: a devcontainer per row,
   * with its state, what it is holding (memory, a live session, an open window),
   * how to reach it, and the two or three buttons that are valid *for that row
   * right now*. The old page had that logic inline and 970 lines of markup
   * around it; the logic stays in the app (it is the app's truth) and the row
   * shape comes from here.
   *
   * Why not `result-list`: a result is something you read and rank. This is
   * something you *operate* — the row's job is to put the right actions next to
   * the state they act on, and to make an unavailable action visible-but-disabled
   * rather than absent, because "you cannot close this" is information.
   *
   * Three slots, all per item, because a row's content is not a string field:
   *   `actions(item)`  the buttons for this row
   *   `note(item)`     the last answer for this row (including a refusal)
   *   `aside(item)`    whatever else belongs beside the name (a connect target)
   */
  let {
    items = [],
    emptyMessage = "Nothing here.",
    label = "",
    actions,
    note,
    aside,
  } = $props();
</script>

<section class="action-list" aria-label={label || undefined}>
  {#if items.length === 0}
    <p class="card empty hint">{emptyMessage}</p>
  {:else}
    <ul class="card rows">
      {#each items as item, index (item.name)}
        <li class="row" class:border-top={index > 0}>
          <div class="head">
            <span class="name">{item.name}</span>
            <span class="tail">
              {#if item.meta}
                <span class="meta num" title={item.metaTitle ?? ""}>{item.meta}</span>
              {/if}
              {#if item.state}
                <span class="pill {item.tone ?? ''}">{item.state}</span>
              {/if}
            </span>
          </div>

          {#if aside}
            {@render aside(item)}
          {/if}

          {#if item.badges?.length}
            <div class="badges">
              {#each item.badges as badge}
                <span class="badge {badge.tone ?? ''}" title={badge.title ?? ""}>{badge.label}</span>
              {/each}
            </div>
          {/if}

          {#if actions}
            <div class="actions">{@render actions(item)}</div>
          {/if}

          {#if note}
            {@render note(item)}
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .row {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .border-top {
    border-top: 1px solid var(--line);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }

  .name {
    font-weight: 600;
    color: var(--ink);
    overflow-wrap: anywhere;
  }

  .tail {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-shrink: 0;
  }

  .meta {
    font-size: 12px;
    color: var(--ink-muted);
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .badge {
    font-size: 10px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--bg-sunken);
    color: var(--ink-faint);
    white-space: nowrap;
  }

  .badge.ok {
    background: var(--ok-bg);
    color: var(--ok);
  }
  .badge.warn {
    background: var(--warn-bg);
    color: var(--warn);
  }
  .badge.bad {
    background: var(--bad-bg);
    color: var(--bad);
  }
  .badge.info {
    background: var(--accent-soft);
    color: var(--accent);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .actions :global(button),
  .actions :global(a.btn) {
    min-height: var(--tap);
    padding: 0 12px;
    font: inherit;
    font-size: 13px;
    color: var(--ink);
    background: var(--bg-raised);
    border: 1px solid var(--line-strong);
    border-radius: 6px;
    cursor: pointer;
  }

  .actions :global(button:hover:not(:disabled)),
  .actions :global(a.btn:hover) {
    background: var(--bg-hover);
  }

  .actions :global(button:disabled) {
    opacity: 0.45;
    cursor: default;
  }

  .actions :global(button.warn) {
    color: var(--warn);
    border-color: var(--warn-bg);
  }

  .empty {
    padding: 14px;
    margin: 0;
  }
</style>
