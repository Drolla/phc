/* Instantiates a Dygraphs chart in every .graph-panel container rendered by
 * templates/_macros.html's render_panel "graph" branch. Deliberately the
 * only hand-written JS in this extension -- everything else is HTMX; a
 * graph panel's history data doesn't fit HTMX's poll-a-fragment model, so
 * it's fetched as JSON (see extensions/web_ui/server.py's
 * handle_graph_data) and handed to Dygraphs directly.
 *
 * v1 loads once per page view -- no auto-refresh -- to avoid the
 * complexity of preserving a user's zoom/pan state across a live update. */

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".graph-panel").forEach(initGraph);
});

function initGraph(container) {
  var graphId = container.dataset.graphId;
  var windowSeconds = Number(container.dataset.window);

  fetch("/api/graph/" + encodeURIComponent(graphId))
    .then(function (response) { return response.json(); })
    .then(function (data) {
      var rows = data.rows.map(function (row) {
        return [new Date(row[0] * 1000)].concat(row.slice(1));
      });
      var now = Date.now();
      new Dygraph(container.querySelector(".graph-canvas"), rows, {
        labels: data.labels,
        ylabel: data.unit || "",
        showRangeSelector: true,
        dateWindow: [now - windowSeconds * 1000, now],
      });
    });
}
