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
 * Binary series (see phc/extensions/web_ui/widgets.py's infer_series_kind)
 * are drawn as stacked on/off lanes rather than sharing a y-axis, where
 * they would otherwise sit on top of each other -- or be flattened onto
 * the baseline by an unrelated series' magnitude. The lane offset is
 * applied here only; the server ships raw 0/1 values. */

/* A lane is PITCH tall; its trace fills HEIGHT of that, leaving dead
 * space between lanes. */
var LANE_PITCH = 1.0;
var LANE_HEIGHT = 0.7;
var LANE_PAD = 0.15;

/* Lane labels sit in the y-axis gutter; Dygraphs crops an over-wide
 * label mid-glyph, so a longer title is shortened instead. */
var LANE_LABEL_WIDTH = 150;
var LANE_LABEL_MAX_CHARS = 20;

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".graph-panel").forEach(initGraph);
});

function initGraph(container) {
  var graphId = container.dataset.graphId;
  var windowSeconds = Number(container.dataset.window);

  fetch("/api/graph/" + encodeURIComponent(graphId))
    .then(function (response) { return response.json(); })
    .then(function (data) {
      var now = Date.now();
      var options = {
        labels: data.labels,
        ylabel: data.unit || "",
        showRangeSelector: true,
        dateWindow: [now - windowSeconds * 1000, now],
      };
      /* `series` is absent when a cached copy of this script meets an
       * older server (or vice versa); degrade to the pre-lane chart. */
      var lanes = assignLanes(data.series || []);
      var rows = buildRows(data.rows, lanes);
      if (lanes.count > 0) {
        applyLaneOptions(options, data, lanes);
      }
      new Dygraph(container.querySelector(".graph-canvas"), rows, options);
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

/* Lanes on the left axis, numeric series on their own right-hand axis so
 * an unrelated magnitude can't flatten them. Dygraphs' own tick spacing
 * would skip or duplicate lane baselines, so each lane gets one explicit
 * tick carrying its series name; a tick with an explicit label bypasses
 * axisLabelFormatter. */
function applyLaneOptions(options, data, lanes) {
  var seriesOpts = {};
  for (var i = 0; i < lanes.series.length; i++) {
    var title = lanes.series[i].title;
    if (lanes.byIndex[i]) {
      seriesOpts[title] = {
        axis: "y",
        /* A switch holds its value between samples; interpolating would
         * draw a ramp through states it was never in. */
        stepPlot: true,
        drawPoints: false,
        highlightCircleSize: 0,
        strokeWidth: 2,
      };
    } else {
      seriesOpts[title] = { axis: "y2" };
    }
  }

  var ticks = [];
  for (var t = 0; t < lanes.series.length; t++) {
    var e = lanes.byIndex[t];
    if (e) {
      ticks.push({ v: e.lane * LANE_PITCH, label: shortenLabel(e.title, LANE_LABEL_MAX_CHARS) });
    }
  }

  options.series = seriesOpts;
  options.labelsShowZeroValues = true;  /* else a lane resting at 0 drops out of the legend */
  options.axes = {
    y: {
      valueRange: [-LANE_PAD, (lanes.count - 1) * LANE_PITCH + LANE_HEIGHT + LANE_PAD],
      ticker: function () { return ticks; },
      axisLabelWidth: LANE_LABEL_WIDTH,
      /* Dygraphs resolves valueFormatter per *axis*, not per series, so
       * this one function serves every lane and branches on the series
       * name it is handed. */
      valueFormatter: function (y, opts, seriesName) {
        return formatLaneValue(y, lanes.byTitle[seriesName]);
      },
    },
  };
  if (lanes.hasNumeric) {
    options.axes.y2 = { independentTicks: true, drawGrid: false };
    options.y2label = data.unit || "";
  }
  options.ylabel = "";
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

/* Invert the lane transform and name the state. Decimation averages a
 * bucket's samples (see LogDb.get_decimated), so a value strictly
 * between off and on is a duty cycle over that bucket, not a glitch. */
function formatLaneValue(y, entry) {
  if (y === null || isNaN(y)) { return ""; }
  if (!entry) { return y; }
  var v = (y - entry.lane * LANE_PITCH) / LANE_HEIGHT;
  var labels = entry.values || {};
  /* describe_endpoint str()-keys these, so a Python True arrives as
   * "True"; accept either casing. */
  if (v <= 0.001) { return labels["False"] || labels["false"] || "off"; }
  if (v >= 0.999) { return labels["True"] || labels["true"] || "on"; }
  return Math.round(v * 100) + "% on";
}
