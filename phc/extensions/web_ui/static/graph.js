/* Instantiates a Dygraphs chart in every .graph-panel container rendered by
 * templates/_macros.html's render_panel "graph" branch. Deliberately the
 * only hand-written JS in this extension -- everything else is HTMX; a
 * graph panel's history data doesn't fit HTMX's poll-a-fragment model, so
 * it's fetched as JSON (see extensions/web_ui/server.py's
 * handle_graph_data) and handed to Dygraphs directly.
 *
 * v1 loads once per graph-panel -- on page load if already visible,
 * otherwise on first expand of its collapsed section -- no auto-refresh,
 * to avoid the complexity of preserving a user's zoom/pan state across a
 * live update.
 *
 * Binary series are drawn as stacked on/off lanes rather than sharing a
 * y-axis. The lane offset is applied here only; the server ships raw
 * 0/1 values.
 *
 * When there are no binary lanes, numeric series spanning more than one
 * declared `unit:` are split across the left/right axes too (see
 * assignUnitAxes) -- otherwise, say, a 0-100% humidity trace shares a
 * scale with a 0-30C temperature trace and reads as a flat line. Only
 * two axes exist, so with 3+ distinct units the extras fall back onto
 * the second axis. A laned chart's y-axis is already lane baselines, so
 * this split only ever applies to y/y2 when lanes.count is 0.
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

/* A graph inside a collapsed <details class="section"> (see
 * _macros.html's render_section) defers its /api/graph/* fetch until
 * that section is actually opened, rather than loading data for every
 * collapsed history section on every page view. */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".graph-panel").forEach(function (container) {
    var section = container.closest("details.section");
    if (!section || section.open) {
      initGraph(container);
    } else {
      section.addEventListener("toggle", function onToggle() {
        if (section.open) {
          section.removeEventListener("toggle", onToggle);
          initGraph(container);
        }
      });
    }
  });
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
      if (entry.lanes.count > 0) {
        restyle(entry, laneAxisOptions(entry));
      }
    });
  }, 150);
});

/* A chart's legend box: a block above the canvas, so it never covers
 * the plot. `tall` reserves room for a lane chart's many entries. */
function createLegend(container, tall) {
  var canvas = container.querySelector(".graph-canvas");
  var legend = document.createElement("div");
  legend.className = "graph-legend" + (tall ? " graph-legend-tall" : "");
  canvas.parentNode.insertBefore(legend, canvas);
  return legend;
}

/* Dygraphs plugin: a thin vertical line at the selected time, so a tap
 * or hover shows where in the plot the legend's values come from (lane
 * traces have no highlight dot to show it). A redraw drops Dygraphs'
 * selection without a deselect event, so it hides the line too. */
function selectionLine() {
  var line = null;
  var hide = function () { line.style.display = "none"; };
  return {
    activate: function (graph) {
      line = document.createElement("div");
      line.className = "graph-selection-line";
      graph.graphDiv.appendChild(line);
      return {
        select: function (event) {
          var area = graph.getArea();
          var x = graph.toDomXCoord(event.selectedX);
          if (x === null || x < area.x || x > area.x + area.w) { hide(); return; }
          line.style.left = Math.round(x) + "px";
          line.style.top = area.y + "px";
          line.style.height = area.h + "px";
          line.style.display = "block";
        },
        deselect: hide,
        didDrawChart: hide,
      };
    },
  };
}

/* A touch that moves less than this is a tap, not a drag. */
var TAP_SLOP_PX = 10;

/* One-finger touch on the plot. With the range selector on, Dygraphs
 * defaults to a mouse-only model, so without this a chart can neither be
 * panned nor read by touch. Handled here, not through Dygraphs'
 * interaction model, whose touch helpers this build doesn't expose.
 *  - A tap selects the nearest row, so the legend shows its values as a
 *    hover does on desktop; a touch anywhere off the plot clears it.
 *  - A horizontal drag pans (horizontal only: a lane chart's y-range is
 *    fixed).
 *  - A vertical drag scrolls the page, left to the browser through
 *    .graph-canvas's touch-action (see style.css).
 * Touches outside the plot, i.e. on the range selector, are left to
 * Dygraphs. */
function enableTouch(graph, element) {
  var start = null;
  var inPlot = function (touch) {
    var box = element.getBoundingClientRect();
    var area = graph.getArea();
    var x = touch.clientX - box.left;
    var y = touch.clientY - box.top;
    return x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h;
  };
  element.addEventListener("touchstart", function (event) {
    start = null;
    if (event.touches.length === 1 && inPlot(event.touches[0])) {
      var touch = event.touches[0];
      start = { x: touch.clientX, y: touch.clientY, range: graph.xAxisRange(), mode: null };
    }
  }, { passive: true });
  element.addEventListener("touchmove", function (event) {
    if (!start || event.touches.length !== 1) { start = null; return; }
    if (start.mode === null) {
      var dx = event.touches[0].clientX - start.x;
      var dy = event.touches[0].clientY - start.y;
      if (Math.max(Math.abs(dx), Math.abs(dy)) < TAP_SLOP_PX) { return; }
      start.mode = Math.abs(dx) > Math.abs(dy) ? "pan" : "scroll";
      if (start.mode === "pan") { graph.clearSelection(); }
    }
    if (start.mode !== "pan") { return; }
    if (event.cancelable) { event.preventDefault(); }
    var span = start.range[1] - start.range[0];
    var shift = -(event.touches[0].clientX - start.x) / graph.getArea().w * span;
    var extremes = graph.xAxisExtremes();
    shift = Math.max(extremes[0] - start.range[0], Math.min(extremes[1] - start.range[1], shift));
    graph.updateOptions({ dateWindow: [start.range[0] + shift, start.range[1] + shift] });
  }, { passive: false });
  element.addEventListener("touchend", function (event) {
    if (start && start.mode === null) {
      event.preventDefault();  /* no emulated mouse events or click after the tap */
      var row = graph.findClosestRow(start.x - element.getBoundingClientRect().left);
      if (row >= 0) { graph.setSelection(row); }
    }
    start = null;
  });
  element.addEventListener("touchcancel", function () { start = null; });
  document.addEventListener("touchstart", function (event) {
    var onPlot = element.contains(event.target) && inPlot(event.touches[0]);
    if (!onPlot) { graph.clearSelection(); }
  }, { passive: true });
}

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
      options.strokeWidth = 2;  /* Dygraphs' default is 1 */
      options.dateWindow = [now - windowSeconds * 1000, now];
      /* `series` may be absent if script/server versions mismatch; degrade
       * to a plain single-axis chart rather than throwing. */
      var lanes = assignLanes(data.series || []);
      var rows = buildRows(data.rows, lanes);
      var legend = createLegend(container, lanes.count > 0);
      options.labelsDiv = legend;
      options.legend = "always";  /* shows the latest values when not hovering */
      options.plugins = [selectionLine()];
      if (lanes.count > 0) {
        applyLaneOptions(options, data, lanes, container);
      } else {
        applyUnitAxisOptions(options, data.series || [], data.unit);
      }
      var canvas = container.querySelector(".graph-canvas");
      var entry = {
        graph: new Dygraph(canvas, rows, options),
        container: container,
        lanes: lanes,
        legend: legend,
      };
      graphs.push(entry);
      enableTouch(entry.graph, canvas);
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

/* Group numeric series by their declared unit, in first-appearance order,
 * and assign the first group to `y` and every later group to `y2`. Only
 * meaningful when there are no binary lanes (those already own `y`).
 * `null`/missing units count as one group of their own -- so a ratio like
 * COP, with no unit, doesn't get silently merged into a temperature axis
 * just because both happen to be unitless-looking numbers. */
function assignUnitAxes(series, declaredUnit) {
  var order = [];
  var byUnit = {};
  for (var i = 0; i < series.length; i++) {
    var entry = series[i];
    if (entry.kind !== "numeric") { continue; }
    var unit = entry.unit === undefined ? null : entry.unit;
    if (!(unit in byUnit)) {
      byUnit[unit] = { unit: unit, titles: [] };
      order.push(unit);
    }
    byUnit[unit].titles.push(entry.title);
  }
  /* The panel's own `unit:` (if set) names the axis the author built the
   * chart around; put it first rather than whichever unit happens to sort
   * first among the endpoints. Series order otherwise follows
   * resolve_selectors' alphabetical-by-endpoint sort (see panels.py), which
   * has no relation to a selector's position in the panel's own config. */
  if (declaredUnit) {
    var preferred = order.indexOf(declaredUnit);
    if (preferred > 0) {
      order.splice(preferred, 1);
      order.unshift(declaredUnit);
    }
  }
  var axisByTitle = {};
  var labels = { y: null, y2: null };
  order.forEach(function (unit, idx) {
    var axis = idx === 0 ? "y" : "y2";
    if (labels[axis] === null) { labels[axis] = unit || ""; }
    byUnit[unit].titles.forEach(function (title) { axisByTitle[title] = axis; });
  });
  return { split: order.length > 1, axisByTitle: axisByTitle, labels: labels };
}

/* Apply the y/y2 split from assignUnitAxes, if it found more than one
 * unit among the (lane-less) series. A single-unit chart is left exactly
 * as it was before this existed: one shared axis, labelled from the
 * panel's own `unit:` (data.unit), not from assignUnitAxes' grouping. */
function applyUnitAxisOptions(options, series, declaredUnit) {
  var grouping = assignUnitAxes(series, declaredUnit);
  if (!grouping.split) { return; }
  var seriesOpts = {};
  series.forEach(function (entry) {
    if (entry.kind === "numeric") {
      seriesOpts[entry.title] = { axis: grouping.axisByTitle[entry.title] };
    }
  });
  options.series = seriesOpts;
  options.ylabel = grouping.labels.y || "";
  options.y2label = grouping.labels.y2 || "";
  options.axes = { y2: { independentTicks: true, drawGrid: false } };
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
        strokeWidth: 3,
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
