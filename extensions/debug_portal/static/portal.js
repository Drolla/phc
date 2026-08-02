/* Live-patches the debug portal's three tables from one JSON snapshot per
 * scheduler tick, delivered over Server-Sent Events (see server.py's
 * handle_events). Deliberately no framework: the row set is close to
 * static (the endpoint table is fixed at page load; only the task queue
 * ever adds/removes rows, on create_task/kill_task), so a keyed Map of
 * <tr> elements plus per-cell textContent writes is simpler and lighter
 * than a virtual-DOM diff.
 *
 * Any cell whose value changed this tick gets a ".changed" class (CSS
 * makes it flash red, see portal.css) -- toggled on/off every render, so
 * it is visible for exactly one tick period. The countdown-style columns
 * (due/cooldown/age) drift every tick by construction, so they use a
 * different rule: highlight only on a RESET (due/cooldown increasing,
 * age decreasing), which is exactly the tick the underlying thing fired.
 */

(function () {
  "use strict";

  var endpointRows = new Map(); // "device/endpoint" -> <tr>, from the server-rendered skeleton
  document.querySelectorAll("#endpoints tbody tr").forEach(function (tr) {
    endpointRows.set(tr.dataset.key, tr);
  });

  var taskRows = new Map(); // tag -> <tr>, built entirely client-side
  var deviceRows = new Map(); // device id -> <tr>, built entirely client-side

  var paused = false;
  var filterText = "";
  var eventsOnly = false;
  // First render (and the first render after a reconnect) has no prior
  // value to diff against -- every cell would otherwise look "changed"
  // relative to the skeleton's placeholder text, flashing the whole page.
  var haveRenderedOnce = false;

  var pauseBtn = document.getElementById("pause");
  var filterInput = document.getElementById("filter");
  var eventsOnlyInput = document.getElementById("events-only");
  var connDot = document.getElementById("conn");
  var tickEl = document.getElementById("tick");
  var hbEl = document.getElementById("heartbeat");
  var clockEl = document.getElementById("clock");
  var taskCountEl = document.getElementById("task-count");
  var deviceCountEl = document.getElementById("device-count");

  pauseBtn.addEventListener("click", function () {
    paused = !paused;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
  });

  filterInput.addEventListener("input", function () {
    filterText = filterInput.value.trim().toLowerCase();
    applyFilter();
  });

  eventsOnlyInput.addEventListener("change", function () {
    eventsOnly = eventsOnlyInput.checked;
    applyFilter();
  });

  function applyFilter() {
    endpointRows.forEach(function (tr) {
      var haystack = (tr.dataset.device + "/" + tr.dataset.endpoint).toLowerCase();
      var matchesFilter = !filterText || haystack.indexOf(filterText) !== -1;
      var matchesEvents = !eventsOnly || tr.classList.contains("has-event");
      tr.classList.toggle("hidden", !(matchesFilter && matchesEvents));
    });
  }

  // toggle(), not add/remove: a cell that keeps changing every tick stays
  // red continuously (toggling true->true is a no-op), and only starts its
  // CSS fade-back once a tick passes with no change.
  function setCell(cell, text, changed) {
    if (cell.textContent !== text) {
      cell.textContent = text;
    }
    cell.classList.toggle("changed", !!changed);
  }

  // Reads back a previously-stored numeric dataset value, treating both
  // "missing" (never rendered) and "" (explicitly null last time) as the
  // same "no previous value" case -- Number("") is 0 in JS, which would
  // otherwise be misread as a real previous due_in/age of zero.
  function prevNumber(tr, key) {
    var raw = tr.dataset[key];
    return raw === undefined || raw === "" ? null : Number(raw);
  }

  function formatDueIn(mode, dueIn) {
    if (mode === "cond") return "cond";
    if (dueIn === null) return "never";
    return dueIn.toFixed(1) + "s";
  }

  function renderEndpoints(rows) {
    rows.forEach(function (row) {
      var tr = endpointRows.get(row.key);
      if (!tr) return; // endpoint set is fixed at load time (server-rendered skeleton)

      var prevState = tr.dataset.state;
      var prevLastValid = tr.dataset.lastValid;
      var hasEvent = row.event !== null;

      setCell(tr.querySelector(".c-state"), row.state,
              haveRenderedOnce && row.state !== prevState);
      setCell(tr.querySelector(".c-last-valid"), row.last_valid,
              haveRenderedOnce && row.last_valid !== prevLastValid);
      setCell(tr.querySelector(".c-event"), hasEvent ? row.event : "—",
              haveRenderedOnce && hasEvent);

      var prevAge = prevNumber(tr, "age");
      var ageReset = haveRenderedOnce && prevAge !== null && row.age !== null && row.age < prevAge;
      setCell(tr.querySelector(".c-age"), row.age === null ? "—" : row.age.toFixed(1) + "s",
              ageReset);

      tr.dataset.state = row.state;
      tr.dataset.lastValid = row.last_valid;
      tr.dataset.age = row.age === null ? "" : String(row.age);
      tr.classList.toggle("has-event", hasEvent);
    });
    applyFilter();
  }

  function renderTasks(tasks) {
    var tbody = document.querySelector("#tasks tbody");
    var seen = new Set();
    tasks.forEach(function (task) {
      seen.add(task.tag);
      var tr = taskRows.get(task.tag);
      if (!tr) {
        tr = document.createElement("tr");
        tr.innerHTML = "<td class=\"c-due\"></td><td class=\"c-tag\"></td>" +
          "<td class=\"c-mode\"></td><td class=\"c-repeat\"></td><td class=\"c-cooldown\"></td>";
        taskRows.set(task.tag, tr);
      }

      var prevDue = prevNumber(tr, "dueIn");
      var dueReset = haveRenderedOnce && task.due_in !== null && prevDue !== null &&
        task.due_in > prevDue;
      setCell(tr.querySelector(".c-due"), formatDueIn(task.mode, task.due_in), dueReset);
      setCell(tr.querySelector(".c-tag"), task.tag, false);
      setCell(tr.querySelector(".c-mode"), task.mode, false);
      setCell(tr.querySelector(".c-repeat"), task.repeat === null ? "—" : task.repeat + "s",
              false);

      var prevCooldown = prevNumber(tr, "cooldown");
      var cooldownReset = haveRenderedOnce && prevCooldown !== null &&
        task.cooldown > prevCooldown;
      setCell(tr.querySelector(".c-cooldown"), task.cooldown > 0 ? task.cooldown.toFixed(1) + "s" : "—",
              cooldownReset);

      tr.dataset.dueIn = task.due_in === null ? "" : String(task.due_in);
      tr.dataset.cooldown = String(task.cooldown);
      tbody.appendChild(tr); // re-append moves it to the end -- keeps DOM order == snapshot order
    });
    taskRows.forEach(function (tr, tag) {
      if (!seen.has(tag)) {
        tr.remove();
        taskRows.delete(tag);
      }
    });
    taskCountEl.textContent = String(tasks.length);
  }

  function renderDevices(devices) {
    var tbody = document.querySelector("#devices tbody");
    var seen = new Set();
    devices.forEach(function (device) {
      seen.add(device.id);
      var tr = deviceRows.get(device.id);
      if (!tr) {
        tr = document.createElement("tr");
        tr.innerHTML = "<td class=\"c-due\"></td><td class=\"c-id\"></td><td class=\"c-interval\"></td>";
        deviceRows.set(device.id, tr);
      }

      var prevDue = prevNumber(tr, "dueIn");
      var dueReset = haveRenderedOnce && prevDue !== null && device.due_in > prevDue;
      setCell(tr.querySelector(".c-due"), device.due_in.toFixed(1) + "s", dueReset);
      setCell(tr.querySelector(".c-id"), device.id, false);
      setCell(tr.querySelector(".c-interval"), device.interval + "s", false);

      tr.dataset.dueIn = String(device.due_in);
      tbody.appendChild(tr);
    });
    deviceRows.forEach(function (tr, id) {
      if (!seen.has(id)) {
        tr.remove();
        deviceRows.delete(id);
      }
    });
    deviceCountEl.textContent = String(devices.length);
  }

  function render(snapshot) {
    tickEl.textContent = "tick " + snapshot.tick;
    hbEl.textContent = "hb " + snapshot.heartbeat.toFixed(3) + "s";
    clockEl.textContent = new Date(snapshot.time * 1000).toLocaleTimeString();
    renderTasks(snapshot.tasks);
    renderDevices(snapshot.devices);
    renderEndpoints(snapshot.endpoints);
    haveRenderedOnce = true;
  }

  function connect() {
    var source = new EventSource("/events");
    source.onopen = function () {
      connDot.className = "conn-dot conn-on";
      connDot.title = "live";
    };
    source.onerror = function () {
      connDot.className = "conn-dot conn-off";
      connDot.title = "disconnected -- retrying";
      haveRenderedOnce = false;
    };
    source.onmessage = function (event) {
      if (paused) return;
      render(JSON.parse(event.data));
    };
  }

  connect();
})();
