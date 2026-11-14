<script>
  /**
   * MeterStrip — a capacity bar over a row of figures.
   *
   * Lifted from devdash, where it is the strip the NAS dashboard iframes: the VM's
   * memory as "used / total" with a bar, then three counts underneath. The tile
   * and the page render the same component for the reason a block exists at all —
   * two copies of the same figures drift, and the dashboard quietly starts
   * disagreeing with the page it summarises.
   *
   * Why not `stat-strip` with a bar bolted on: a stat is a number with a sub-line,
   * and the interesting number here is a *ratio against a ceiling*. The bar is
   * the fact — "2.1 / 8 GB" as a filled bar is a glance, as a string it is a
   * calculation. So the ratio is required and the figures are the annotation.
   *
   * `used`/`total` are numbers (the bar needs them). The printed labels are
   * separate strings because the caller's formatter knows the units and this
   * block does not: devdash writes "2.1 GB", not "2.1".
   */
  let {
    label = "",
    used = 0,
    total = 0,
    usedLabel = "",
    totalLabel = "",
    figures = [],
    /** ok | warn | bad — the bar's colour, i.e. how close to the ceiling it is. */
    tone = "ok",
  } = $props();

  const pct = $derived(total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0);
</script>

<section class="card meter-strip" aria-label={label || "capacity"}>
  <div class="meter-label">
    <span class="micro">{label}</span>
    <span class="num reading">{usedLabel || used} / {totalLabel || total}</span>
  </div>
  <div
    class="bar"
    role="progressbar"
    aria-valuenow={used}
    aria-valuemin="0"
    aria-valuemax={total}
    aria-label={label || "capacity"}
  >
    <span class={tone} style="width: {pct}%"></span>
  </div>
  {#if figures.length > 0}
    <div class="figures" style="--fig-cols: {figures.length}">
      {#each figures as figure}
        <div class="figure">
          <span class="n num">{figure.value}</span>
          <span class="k micro" title={figure.hint ?? ""}>{figure.label}</span>
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .meter-strip {
    padding: 14px 16px;
  }

  .meter-label {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 6px;
  }

  .reading {
    font-size: 13px;
    color: var(--ink-dim);
  }

  .bar {
    height: 8px;
    border-radius: 4px;
    background: var(--bg-sunken);
    overflow: hidden;
  }

  .bar span {
    display: block;
    height: 100%;
    border-radius: 4px;
    background: var(--ok);
    transition: width 240ms ease;
  }

  .bar span.warn {
    background: var(--warn);
  }

  .bar span.bad {
    background: var(--bad);
  }

  .figures {
    display: grid;
    grid-template-columns: repeat(var(--fig-cols, 3), 1fr);
    gap: 10px;
    margin-top: 14px;
  }

  .figure {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .n {
    font-size: 17px;
    font-weight: 700;
    line-height: 1.2;
    color: var(--ink);
  }

  .k {
    margin: 0;
  }
</style>
