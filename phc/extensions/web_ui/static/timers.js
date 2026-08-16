/* Drives the "timers" panel's add/edit form (templates/_timers.html):
 * swaps the value control to match the selected target endpoint's own
 * metadata (mirroring extensions/web_ui/widgets.py's infer_widget_kind,
 * since the browser has no server round-trip to ask), and fills the form
 * from a row's data-* attributes when its "Edit" button is clicked.
 *
 * Listeners are delegated at the document level rather than bound per
 * element, because htmx replaces the whole panel -- including the form --
 * on every poll/submit (hx-swap="outerHTML" on .timers-panel), which would
 * otherwise leave per-element bindings stale after the first swap. */

function timerPanelOf(el) {
  return el.closest(".timers-panel");
}

function timerTargets(panel) {
  var script = panel.querySelector(".timer-targets-data");
  return script ? JSON.parse(script.textContent) : [];
}

function findTarget(targets, ref) {
  return targets.find(function (t) { return (t.device + "/" + t.endpoint) === ref; });
}

function rebuildValueControl(form) {
  var panel = timerPanelOf(form);
  var targets = timerTargets(panel);
  var targetSelect = form.querySelector(".timer-form-target");
  var actionSelect = form.querySelector(".timer-form-action");
  var target = findTarget(targets, targetSelect.value);
  var action = actionSelect.value;
  var wrap = form.querySelector(".timer-form-value-wrap");
  var old = form.querySelector(".timer-form-value");
  var current = old ? old.value : "";

  if (action !== "set" || !target) {
    wrap.style.display = "none";
    if (old) old.disabled = true;
    return;
  }
  wrap.style.display = "";

  var input;
  if (target.value_type === "bool" && !target.values) {
    input = document.createElement("select");
    ["true", "false"].forEach(function (v) {
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      input.appendChild(opt);
    });
    if (current) input.value = current;
  } else if (target.values) {
    input = document.createElement("select");
    Object.keys(target.values).forEach(function (raw) {
      var opt = document.createElement("option");
      opt.value = target.values[raw];
      opt.textContent = target.values[raw];
      input.appendChild(opt);
    });
    if (current) input.value = current;
  } else if ((target.value_type === "int" || target.value_type === "float") &&
             target.min !== null && target.max !== null) {
    input = document.createElement("input");
    input.type = "range";
    input.min = target.min;
    input.max = target.max;
    input.step = target.value_type === "int" ? "1" : "any";
    input.value = current !== "" ? current : target.min;
  } else if (target.value_type === "int" || target.value_type === "float") {
    input = document.createElement("input");
    input.type = "number";
    input.step = target.value_type === "int" ? "1" : "any";
    input.value = current;
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = current;
  }
  input.name = "value";
  input.className = "timer-form-value form-control";
  old.replaceWith(input);
}

document.addEventListener("change", function (event) {
  if (event.target.matches(".timer-form-target, .timer-form-action")) {
    rebuildValueControl(event.target.closest("form"));
  }
});

document.addEventListener("click", function (event) {
  var editBtn = event.target.closest(".timer-edit-btn");
  if (editBtn) {
    var row = editBtn.closest(".timer-row");
    var form = timerPanelOf(row).querySelector(".timers-form");
    form.querySelector(".timer-form-id").value = row.dataset.id;
    form.querySelector(".timer-form-target").value = row.dataset.target;
    form.querySelector(".timer-form-action").value = row.dataset.action;
    form.querySelector(".timer-form-time").value = row.dataset.time;
    form.querySelector(".timer-form-repeat").value = row.dataset.repeat;
    form.querySelector(".timer-form-description").value = row.dataset.description;
    form.querySelector(".timer-form-enabled").checked = row.dataset.enabled === "true";
    rebuildValueControl(form);
    var valueInput = form.querySelector(".timer-form-value");
    if (valueInput && !valueInput.disabled) {
      valueInput.value = row.dataset.value;
    }
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  var cancelBtn = event.target.closest(".timer-cancel-btn");
  if (cancelBtn) {
    var cancelForm = cancelBtn.closest("form");
    cancelForm.reset();
    cancelForm.querySelector(".timer-form-id").value = "";
    rebuildValueControl(cancelForm);
  }
});

document.addEventListener("htmx:afterSwap", function (event) {
  if (event.target.classList && event.target.classList.contains("timers-panel")) {
    event.target.querySelectorAll(".timers-form").forEach(rebuildValueControl);
  }
});

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".timers-form").forEach(rebuildValueControl);
});
