<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    Chart, LineController, LineElement, PointElement, LinearScale,
    CategoryScale, TimeScale, Tooltip, Legend, Filler,
  } from 'chart.js';

  Chart.register(
    LineController, LineElement, PointElement, LinearScale,
    CategoryScale, TimeScale, Tooltip, Legend, Filler,
  );

  let {
    buckets = [],
    bucketSec = 60,
    tags = [],
    visible = {},
    height = 220,
    showLegend = false,
    mode = 'smooth', // 'smooth' | 'step'
  } = $props();

  let canvas;
  let chart;

  function buildData() {
    const labels = buckets.map(b => b.bucket_start * 1000);
    const stepped = mode === 'step';
    const datasets = tags
      .filter(t => visible[t.id] !== false)
      .map(t => {
        const color = getCSS(t.color);
        return {
          label: t.label,
          data: buckets.map(b => b[t.id] || 0),
          borderColor: color,
          backgroundColor: hexA(color, 0.18),
          fill: true,
          tension: stepped ? 0 : 0.34,
          stepped: stepped ? 'before' : false,
          borderWidth: stepped ? 1.8 : 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: color,
          pointHoverBorderColor: '#0d1117',
          pointHoverBorderWidth: 2,
          spanGaps: true,
        };
      });
    return { labels, datasets };
  }

  function getCSS(v) {
    if (!v) return '#888';
    if (v.startsWith('var(')) {
      const name = v.slice(4, -1).trim();
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
    }
    return v;
  }

  function hexA(hex, a) {
    if (!hex.startsWith('#')) return hex;
    let h = hex.slice(1);
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    const n = parseInt(h, 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return `rgba(${r},${g},${b},${a})`;
  }

  function opts() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 220, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: showLegend,
          position: 'bottom',
          labels: {
            color: getCSS('var(--fg-muted)'),
            font: { size: 11, family: 'inherit' },
            usePointStyle: true,
            boxWidth: 8, boxHeight: 8,
            padding: 14,
          },
        },
        tooltip: {
          backgroundColor: 'rgba(13,17,23,0.96)',
          borderColor: getCSS('var(--line)'),
          borderWidth: 1,
          titleColor: getCSS('var(--fg)'),
          bodyColor: getCSS('var(--fg)'),
          padding: 10,
          displayColors: true,
          boxPadding: 4,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const ts = items[0].label;
              return new Date(+ts).toLocaleString();
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: {
            unit: bucketSec >= 86400 ? 'day' : bucketSec >= 3600 ? 'hour' : 'minute',
            displayFormats: {
              minute: 'HH:mm',
              hour: 'MMM d HH:mm',
              day: 'MMM d',
            },
            tooltipFormat: 'PP HH:mm',
          },
          grid: { display: false },
          border: { display: false },
          ticks: {
            color: getCSS('var(--fg-dim)'),
            font: { size: 10, family: 'inherit' },
            maxRotation: 0,
            autoSkipPadding: 18,
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: getCSS('var(--hairline)'), drawTicks: false },
          border: { display: false },
          ticks: {
            color: getCSS('var(--fg-dim)'),
            font: { size: 10, family: 'inherit' },
            padding: 6,
            maxTicksLimit: 4,
          },
        },
      },
    };
  }

  onMount(async () => {
    // Time adapter (date-fns) — load lazily so it ships in bundle.
    await import('chartjs-adapter-date-fns');
    chart = new Chart(canvas, { type: 'line', data: buildData(), options: opts() });
  });

  $effect(() => {
    // Re-render whenever inputs change.
    void buckets; void visible; void tags; void bucketSec; void mode;
    if (chart) {
      chart.data = buildData();
      chart.options = opts();
      chart.update('none');
    }
  });

  onDestroy(() => chart?.destroy());
</script>

<div class="wrap" style:height={height + 'px'}>
  <canvas bind:this={canvas}></canvas>
</div>

<style>
  .wrap {
    position: relative;
    width: 100%;
  }
  canvas { display: block; }
</style>
