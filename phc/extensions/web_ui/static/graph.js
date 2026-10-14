/* Instantiates a Dygraphs chart in every .graph-panel container rendered by
 * templates/_macros.html's render_panel "graph" branch. Deliberately the
 * only hand-written JS in this extension -- everything else is HTMX; a
 * graph panel's history data doesn't fit HTMX's poll-a-fragment model, so
 * it's fetched as JSON (see extensions/web_ui/server.py's
 * handle_graph_data) and handed to Dygraphs directly.
 *
 * v1 loads once per page view -- no auto-refresh -- to avoid the
 * complexity of preserving a user's zoom/pan state across a live update.
 *
 * Binary series are drawn as stacked on/off lanes rather than sharing a
 * y-axis. The lane offset is applied here only; the server ships raw
 * 0/1 values.
 *
 * Theming: Dygraphs hardcodes light-theme colors, so themeOptions()
 * below supplies the JS-only ones, re-applied on phc:themechange. */

/* A lane is PITCH tall; its trace fills HEIGHT of that, leaving dead
 * space between lanes. */
var LANE_PITCH = 1.0;
var LANE_HEIGHT = 0.7;
var LANE_PAD = 0.15;

/* Lane label gutter: a share of canvas width, capped, so phones get a
 * narrow gutter with shortened labels rather than losing ~40% of chart. */
var LANE_LABEL_FRACTION = 0.28;
var LANE_LABEL_MAX_PX = 110;
/* Estimate at ~11px sans-serif, erring narrow -- Dygraphs crops mid-glyph. */
var LANE_LABEL_PX_PER_CHAR = 6.2;
/* Each lane needs enough height for its own label: the axis font is 14px,
   so anything under ~22px of pitch puts adjacent names within a couple of
   pixels of each other. The CSS height is a floor for a small panel; past
   about eight lanes the canvas has to grow instead, or the labels collide
   however they are wrapped or shortened. */
var LANE_MIN_PITCH_PX = 22;
/* x-axis labels plus the range selector strip, which don't scale with lanes. */
var LANE_CHROME_PX = 75;

/* Grow .graph-canvas to fit `count` lanes, leaving its CSS height as the
   minimum. Set before Dygraphs reads the element, so it measures the final
   size and needs no resize afterwards. */
function fitCanvasToLanes(container, count) {
  var canvas = container.querySelector(".graph-canvas");
  if (!canvas || count < 1) { return; }
  var span = (count - 1) + LANE_HEIGHT + 2 * LANE_PAD;
  var needed = Math.ceil(LANE_MIN_PITCH_PX * span + LANE_CHROME_PX);
  if (needed > canvas.clientHeight) { canvas.style.height = needed + "px"; }
}

function laneLabelWidth(container) {
  var canvas = container.querySelector(".graph-canvas");
  var available = (canvas && canvas.clientWidth) || 0;
  if (!available) { return LANE_LABEL_MAX_PX; }
  return Math.min(available * LANE_LABEL_FRACTION, LANE_LABEL_MAX_PX);
}

/* Series colors, assigned in order and never cycled. Validated for
 * colorblind separation and contrast against each theme's --bs-body-bg;
 * the dark column is the same eight hues re-stepped, not an automatic
 * lightening of the light one. Beyond eight series (in practice binary
 * ones with their own lane label) there's no ninth color. */
var SERIES_COLORS = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500",
         "#d55181", "#008300", "#9085e9", "#e66767"],
};

/* Live charts plus the lane data a restyle/resize needs to rebuild ticks. */
var graphs = [];

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".graph-panel").forEach(initGraph);
});

/* Listen for base.html's event rather than the media query directly --
 * a separate matchMedia listener could fire before data-bs-theme updates. */
window.addEventListener("phc:themechange", function () {
  graphs.forEach(function (entry) {
    restyle(entry, themeOptions());
  });
});

/* Recompute the lane label gutter on resize. Debounced since a
 * drag-resize fires continuously and each update redraws every chart. */
var resizeTimer = null;
window.addEventListener("resize", function () {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function () {
    graphs.forEach(function (entry) {
      if (entry.lanes.count > 0) { restyle(entry, laneAxisOptions(entry)); }
    });
  }, 150);
});

/* Apply `options` to a live chart without resetting the user's zoom/pan. */
function restyle(entry, options) {
  options.dateWindow = entry.graph.xAxisRange();
  entry.graph.updateOptions(options);
}

/* JS-only color options Dygraphs needs, read from computed style so
 * style.css stays the single source of truth. */
function themeOptions() {
  var styles = getComputedStyle(document.documentElement);
  var read = function (token, fallback) {
    return (styles.getPropertyValue(token) || "").trim() || fallback;
  };
  var dark = document.documentElement.getAttribute("data-bs-theme") === "dark";
  var grid = read("--bs-border-color", dark ? "#495057" : "#dee2e6");
  var axis = read("--bs-secondary-color", dark ? "#adb5bd" : "#6c757d");
  return {
    colors: SERIES_COLORS[dark ? "dark" : "light"],
    axisLineColor: axis,
    gridLineColor: grid,
    /* Range selector defaults are invisible against a dark page. */
    rangeSelectorBackgroundStrokeColor: grid,
    rangeSelectorForegroundStrokeColor: axis,
    rangeSelectorPlotFillColor: dark ? "#2c3136" : "#dfe4ea",
    rangeSelectorPlotStrokeColor: dark ? "#5c646c" : "#9aa4b0",
  };
}

function initGraph(container) {
  var graphId = container.dataset.graphId;
  var windowSeconds = Number(container.dataset.window);

  fetch("/api/graph/" + encodeURIComponent(graphId))
    .then(function (response) { return response.json(); })
    .then(function (data) {
      var now = Date.now();
      var options = themeOptions();
      options.labels = data.labels;
      options.ylabel = data.unit || "";
      options.showRangeSelector = true;
      options.dateWindow = [now - windowSeconds * 1000, now];
      /* `series` may be absent if script/server versions mismatch; degrade
       * to a plain single-axis chart rather than throwing. */
      var lanes = assignLanes(data.series || []);
      var rows = buildRows(data.rows, lanes);
      if (lanes.count > 0) {
        fitCanvasToLanes(container, lanes.count);
        applyLaneOptions(options, data, lanes, container);
      }
      graphs.push({
        graph: new Dygraph(container.querySelector(".graph-canvas"), rows, options),
        container: container,
        lanes: lanes,
      });
    });
}

/* Map each series index to its lane, numbered so the first-configured
 * binary series sits at the top. */
function assignLanes(series) {
  var binary = [];
  for (var i = 0; i < series.length; i++) {
    if (series[i].kind === "binary") { binary.push(i); }
  }
  var byIndex = {};
  var byTitle = {};
  for (var n = 0; n < binary.length; n++) {
    var entry = {
      lane: binary.length - 1 - n,
      title: series[binary[n]].title,
      values: series[binary[n]].values,
    };
    byIndex[binary[n]] = entry;
    byTitle[entry.title] = entry;
  }
  return {
    count: binary.length,
    hasNumeric: binary.length < series.length,
    byIndex: byIndex,
    byTitle: byTitle,
    series: series,
  };
}

/* Wire rows -> Dygraphs rows: epoch seconds to Date, and each binary
 * column lifted into its own lane. A null (no data) stays null. */
function buildRows(wireRows, lanes) {
  return wireRows.map(function (row) {
    var out = [new Date(row[0] * 1000)];
    for (var i = 1; i < row.length; i++) {
      var entry = lanes.byIndex[i - 1];
      var value = row[i];
      if (entry && value !== null) {
        value = entry.lane * LANE_PITCH + value * LANE_HEIGHT;
      }
      out.push(value);
    }
    return out;
  });
}

function applyLaneOptions(options, data, lanes, container) {
  var seriesOpts = {};
  for (var i = 0; i < lanes.series.length; i++) {
    var title = lanes.series[i].title;
    if (lanes.byIndex[i]) {
      seriesOpts[title] = {
        axis: "y",
        stepPlot: true,  /* a switch holds its value between samples */
        drawPoints: false,
        highlightCircleSize: 0,
        strokeWidth: 2,
      };
    } else {
      seriesOpts[title] = { axis: "y2" };
    }
  }

  options.series = seriesOpts;
  options.labelsShowZeroValues = true;  /* else a lane resting at 0 drops out of the legend */
  var axes = laneAxisOptions({ container: container, lanes: lanes }).axes;
  options.axes = {
    y: axes.y,
  };
  if (lanes.hasNumeric) {
    options.axes.y2 = { independentTicks: true, drawGrid: false };
    options.y2label = data.unit || "";
    options.ylabel = "";
  } else {
    options.ylabel = "";
  }
}

/* The y-axis block for a laned chart. Rebuilt on demand since the label
 * gutter depends on canvas width. One explicit tick per lane, since
 * Dygraphs' own tick spacing would skip or duplicate lane baselines. */
function laneAxisOptions(entry) {
  var lanes = entry.lanes;
  var width = laneLabelWidth(entry.container);
  var maxChars = Math.max(6, Math.floor(width / LANE_LABEL_PX_PER_CHAR));
  var ticks = [];
  for (var t = 0; t < lanes.series.length; t++) {
    var e = lanes.byIndex[t];
    if (e) {
      ticks.push({ v: e.lane * LANE_PITCH, label: shortenLabel(e.title, maxChars) });
    }
  }
  return {
    axes: {
      y: {
        valueRange: [-LANE_PAD, (lanes.count - 1) * LANE_PITCH + LANE_HEIGHT + LANE_PAD],
        ticker: function () { return ticks; },
        axisLabelWidth: width,
        /* Resolved per axis, not per series, so this branches on seriesName. */
        valueFormatter: function (y, opts, seriesName) {
          return formatLaneValue(y, lanes.byTitle[seriesName]);
        },
      },
    },
  };
}

/* Elide the middle, not the tail: sibling devices often share a long
 * prefix ("Motion Living Room" / "Motion Cellar") that a tail ellipsis
 * would render identically. Full title is still in the hover legend. */
function shortenLabel(title, maxChars) {
  if (title.length <= maxChars) { return title; }
  var keep = maxChars - 1;                       /* the ellipsis itself */
  var head = Math.ceil(keep / 2);
  var tail = Math.floor(keep / 2);
  return title.slice(0, head) + "…" + title.slice(title.length - tail);
}

/* Invert the lane transform and name the state. Under
 * `binary_aggregation: avg`, a value strictly between 0 and 1 is a
 * genuine duty cycle, not a glitch. */
function formatLaneValue(y, entry) {
  if (y === null || isNaN(y)) { return ""; }
  if (!entry) { return y; }
  var v = (y - entry.lane * LANE_PITCH) / LANE_HEIGHT;
  var labels = entry.values || {};
  /* Python True str()-keys as "True"; accept either casing. */
  if (v <= 0.001) { return labels["False"] || labels["false"] || "off"; }
  if (v >= 0.999) { return labels["True"] || labels["true"] || "on"; }
  return Math.round(v * 100) + "% on";
}
