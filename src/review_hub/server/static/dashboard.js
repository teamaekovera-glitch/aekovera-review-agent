/* Aekovera Review Hub dashboard — minimal vanilla JS polling layer.
 *
 * The server-rendered pages adapt to the JSON API (routes.py); this layer
 * re-fetches the same endpoints and re-renders, so what polling shows is
 * exactly what the API says. No frameworks, no build step, no external
 * assets — works on localhost without internet.
 *
 * Rendering uses createElement + textContent throughout: store data
 * (company names, prompts, pastes) is never trusted as HTML.
 */
(function () {
  "use strict";

  var page = document.body.dataset.page;
  var runId = document.body.dataset.runId || "";

  // ---- tiny DOM helpers ------------------------------------------------
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, shown) { node.hidden = !shown; }

  function emptyRow(colspan, message) {
    var tr = el("tr");
    var td = el("td", "empty", message);
    td.setAttribute("colspan", String(colspan));
    tr.appendChild(td);
    return tr;
  }
  function recordLink(recordId) {
    var link = el("a", null, recordId);
    link.href = "/dashboard/audit/" + encodeURIComponent(recordId || "");
    return link;
  }
  function badgeClass(value, prefix) {
    return prefix + String(value || "").toLowerCase().replace(/[^a-z0-9_]+/g, "_");
  }
  function statusBadge(status) {
    return el("span", "badge badge-" + status, status);
  }
  function fmt(value) {
    return value === undefined || value === null || value === "" ? "—" : value;
  }

  // ---- fetch helpers ---------------------------------------------------
  function getJSON(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error(r.status + " for " + url);
      return r.json();
    });
  }
  function sendJSON(url, method, body) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (bodyJSON) {
        return { ok: r.ok, status: r.status, body: bodyJSON };
      });
    });
  }

  // ---- polling ---------------------------------------------------------
  function poll(url, intervalMs, fn) {
    var busy = false;
    function tick() {
      if (busy) return;
      busy = true;
      getJSON(url).then(fn, function () {
        /* Transient poll errors (server restarting, navigation away) stay
           quiet: the next tick re-reads the store, the source of truth. */
      }).finally(function () { busy = false; });
    }
    tick();
    return setInterval(tick, intervalMs);
  }

  // ---- overview --------------------------------------------------------
  function renderRuns(runs) {
    var body = byId("runs-table").querySelector("tbody");
    clear(body);
    if (!runs.length) {
      body.appendChild(emptyRow(6, "No runs yet — start one above."));
      return;
    }
    runs.forEach(function (r) {
      var tr = el("tr");
      var idCell = el("td");
      idCell.appendChild(recordLink(r.run_id));
      tr.appendChild(idCell);
      tr.appendChild(el("td", null, r.mode));
      var st = el("td");
      st.appendChild(statusBadge(r.status));
      tr.appendChild(st);
      tr.appendChild(el("td", null, r.processed + " / " + r.run_count + " records"));
      tr.appendChild(el("td", "muted", fmt(r.status_reason)));
      var worker = "—";
      if (r.thread_alive) worker = "live";
      else if (r.adopted) worker = "adopted";
      tr.appendChild(el("td", "muted", worker));
      body.appendChild(tr);
    });
  }

  function initOverview() {
    var form = byId("create-run");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var err = byId("create-run-error");
      show(err, false);
      sendJSON("/api/runs", "POST", {
        mode: form.mode.value,
        backend: form.backend.value,
        limit: parseInt(form.limit.value, 10) || 1,
      }).then(function (res) {
        if (!res.ok) {
          err.textContent = res.body.detail || "could not start the run";
          show(err, true);
          return;
        }
        window.location.href = "/dashboard/runs/" + encodeURIComponent(res.body.run_id);
      });
    });
    poll("/api/runs", 3000, function (data) { renderRuns(data.runs); });
    function refreshCounts() {
      getJSON("/api/queues/manual-review").then(function (d) {
        byId("count-manual").textContent = d.holds.length;
      }).catch(function () {});
      getJSON("/api/queues/held").then(function (d) {
        byId("count-held").textContent = d.holds.length;
      }).catch(function () {});
    }
    refreshCounts();
    setInterval(refreshCounts, 4000);
  }

  // ---- run monitor -----------------------------------------------------
  function renderPasteBox(request) {
    var box = byId("paste-box");
    var pending = !!(request && request.pending);
    show(box, pending);
    if (!pending) return;
    byId("paste-record").textContent = fmt(request.record_id);
    byId("paste-company").textContent = fmt(request.company_name);
    byId("paste-prompt").textContent = request.prompt || "";
    show(byId("paste-error"), !!request.error);
    if (request.error) byId("paste-error-text").textContent = " " + request.error;
    // A fresh re-park (new request id) means the invalid paste consumed the
    // old submission — clear the textarea so the operator pastes anew.
    if (box.dataset.requestId !== String(request.request_id)) {
      byId("paste-input").value = "";
      byId("paste-status").textContent = "";
      byId("paste-submit").disabled = false;
      box.dataset.requestId = String(request.request_id);
    }
  }

  function renderRun(run) {
    var statusNode = byId("run-status");
    statusNode.className = "badge badge-" + run.status;
    statusNode.textContent = run.status;
    byId("run-reason").textContent = run.status_reason || "";
    byId("run-worker").textContent = run.thread_alive ? "worker live"
      : (run.adopted ? "adopted (no live worker)" : "");
    byId("progress-text").textContent = run.processed + " / " + run.run_count + " records";
    var pct = run.run_count ? Math.floor((run.processed / run.run_count) * 100) : 0;
    byId("progress-bar").style.width = Math.max(0, Math.min(100, pct)) + "%";

    var tally = byId("run-tally");
    clear(tally);
    Object.keys(run.decision_tally || {}).forEach(function (key, i) {
      if (i) tally.appendChild(document.createTextNode(" · "));
      tally.appendChild(el("strong", null, key + " " + run.decision_tally[key]));
    });

    var body = byId("records-body");
    clear(body);
    if (!run.records.length) body.appendChild(emptyRow(7, "No records yet."));
    run.records.forEach(function (rec) {
      var tr = el("tr");
      var idCell = el("td");
      idCell.appendChild(recordLink(rec.record_id));
      tr.appendChild(idCell);
      tr.appendChild(el("td", null, fmt(rec.company_name)));
      tr.appendChild(el("td", null, fmt(rec.kind)));
      var dec = el("td");
      dec.appendChild(el("span", badgeClass(rec.decision, "badge-decision-"), fmt(rec.decision)));
      tr.appendChild(dec);
      tr.appendChild(el("td", null, rec.finalized ? "✓" : "…"));
      tr.appendChild(el("td", null, fmt(rec.processed_after)));
      tr.appendChild(el("td", "muted", fmt(rec.recorded_at)));
      body.appendChild(tr);
    });

    var events = byId("events-list");
    clear(events);
    if (!run.status_events.length) events.appendChild(el("li", "empty", "No events."));
    run.status_events.forEach(function (evd) {
      var li = el("li");
      li.appendChild(statusBadge(evd.status));
      li.appendChild(el("span", "muted", evd.created_at));
      li.appendChild(document.createTextNode(evd.reason || ""));
      events.appendChild(li);
    });

    renderPasteBox(run.manual_request);
  }

  function initRun() {
    var url = "/api/runs/" + encodeURIComponent(runId);

    var controls = byId("run-controls");
    controls.addEventListener("click", function (ev) {
      var cmd = ev.target && ev.target.dataset && ev.target.dataset.cmd;
      if (!cmd) return;
      ev.target.disabled = true;
      sendJSON(url + "/" + cmd, "POST", {}).then(function () {
        ev.target.disabled = false;
      }, function () { ev.target.disabled = false; });
    });

    var form = byId("paste-form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var input = byId("paste-input");
      var status = byId("paste-status");
      var submit = byId("paste-submit");
      if (!input.value.trim()) return;
      submit.disabled = true;
      status.textContent = "Submitting — the run resumes when the response is accepted…";
      sendJSON(url + "/manual-response", "POST", { raw_response: input.value }).then(
        function (res) {
          if (res.ok) {
            input.value = "";
            status.textContent = "Response accepted — resuming…";
          } else {
            status.textContent = res.body.detail || "paste refused";
            submit.disabled = false;
          }
          // The polling render reports the outcome either way: a valid paste
          // flips the run out of awaiting_manual, an invalid one re-parks
          // with the error banner and a fresh box.
        },
        function () {
          status.textContent = "network error — try again";
          submit.disabled = false;
        }
      );
    });

    byId("copy-prompt").addEventListener("click", function () {
      var text = byId("paste-prompt").textContent || "";
      var done = function () {
        byId("copy-prompt").textContent = "Copied!";
        setTimeout(function () { byId("copy-prompt").textContent = "Copy prompt"; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        // Fallback: select the prompt so Ctrl+C works (non-secure contexts).
        var range = document.createRange();
        range.selectNodeContents(byId("paste-prompt"));
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        done();
      }
    });

    poll(url, 2000, renderRun);
  }

  // ---- queues ----------------------------------------------------------
  function resolveForm(queue, hold) {
    var form = el("form", "row-form resolve-form");

    var outLabel = el("label");
    outLabel.appendChild(document.createTextNode("Outcome"));
    var outSelect = el("select");
    ["", "accepted", "rejected", "needs_work"].forEach(function (v) {
      var opt = el("option", null, v === "" ? "(none)" : v);
      opt.value = v;
      outSelect.appendChild(opt);
    });
    outLabel.appendChild(outSelect);
    form.appendChild(outLabel);

    var noteLabel = el("label", "grow");
    noteLabel.appendChild(document.createTextNode("Note"));
    var noteInput = el("input");
    noteInput.type = "text";
    noteInput.placeholder = "Why this is resolved...";
    noteLabel.appendChild(noteInput);
    form.appendChild(noteLabel);

    var btn = el("button", "btn btn-primary", "Resolve");
    btn.type = "submit";
    form.appendChild(btn);

    var err = el("span", "form-error");
    err.hidden = true;
    form.appendChild(err);

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      err.hidden = true;
      sendJSON("/api/queues/" + queue + "/" + hold.hold_id + "/resolve", "POST", {
        outcome: outSelect.value,
        note: noteInput.value,
      }).then(function (res) {
        if (!res.ok) {
          err.textContent = res.body.detail || "could not resolve";
          err.hidden = false;
        }
      });
    });
    return form;
  }

  function renderHolds(queue, holds) {
    var list = document.querySelector('[data-queue="' + queue + '"][data-kind-holds]');
    if (!list) return;
    clear(list);
    if (!holds.length) {
      list.appendChild(el("p", "empty", "Nothing waiting — this queue is clear."));
      return;
    }
    holds.forEach(function (hold) {
      var card = el("article", "hold");
      card.dataset.holdId = hold.hold_id;

      var head = el("header");
      head.appendChild(el("strong", null, hold.company_name));
      head.appendChild(el("span", "muted", "record " + (hold.record_id || "")));
      head.appendChild(el("span", "muted", hold.created_at || ""));
      card.appendChild(head);

      if (hold.kind === "manual_review" && hold.detail) {
        var reason = el("p", "hold-reason");
        reason.appendChild(el("span", "reason-label", "Reason: "));
        reason.appendChild(document.createTextNode(hold.detail));
        card.appendChild(reason);
      }
      if (hold.kind === "field_hold" && hold.detail) {
        var chips = el("div", "hold-reason");
        chips.appendChild(el("span", "reason-label", "Held fields: "));
        (hold.detail.needs_clear || []).forEach(function (item) {
          chips.appendChild(el("span", "chip chip-warn", item[0] + ": " + item[1] + " (needs clear)"));
        });
        (hold.detail.needs_review || []).forEach(function (item) {
          chips.appendChild(el("span", "chip", item[0] + ": " + item[1] + " (needs review)"));
        });
        (hold.detail.identity_renamed || []).forEach(function (item) {
          chips.appendChild(el("span", "chip chip-warn", item[0] + ": " + item[1] + " (renamed)"));
        });
        card.appendChild(chips);
      }

      card.appendChild(resolveForm(queue, hold));
      list.appendChild(card);
    });
  }

  function initQueues() {
    function refresh() {
      getJSON("/api/queues/manual-review").then(function (d) {
        renderHolds("manual-review", d.holds);
      }).catch(function () {});
      getJSON("/api/queues/held").then(function (d) {
        renderHolds("held", d.holds);
      }).catch(function () {});
    }
    refresh();
    setInterval(refresh, 3000);
  }

  // ---- history / accepted (poll keeps them fresh across runs) ----------
  function initHistory() {
    poll("/api/history", 5000, function (data) {
      var body = byId("history-body");
      clear(body);
      if (!data.history.length) {
        body.appendChild(emptyRow(6, "No history yet — finalize a record first."));
      }
      data.history.forEach(function (row) {
        var tr = el("tr");
        tr.appendChild(el("td", null, row.company_name));
        var idCell = el("td");
        idCell.appendChild(recordLink(row.record_id));
        tr.appendChild(idCell);
        var out = el("td");
        out.appendChild(el("span", badgeClass(row.outcome_view, "badge-decision-"), fmt(row.outcome_view)));
        tr.appendChild(out);
        tr.appendChild(el("td", null, fmt(row.times_seen)));
        tr.appendChild(el("td", "muted", fmt(row.first_seen)));
        tr.appendChild(el("td", "muted", fmt(row.last_seen)));
        body.appendChild(tr);
      });
    });
  }

  function initAccepted() {
    poll("/api/accepted-companies", 5000, function (data) {
      var body = byId("accepted-body");
      clear(body);
      if (!data.accepted.length) {
        body.appendChild(emptyRow(7, "No accepted companies yet."));
      }
      data.accepted.forEach(function (row) {
        var tr = el("tr");
        var name = el("td");
        name.appendChild(el("strong", null, row.company_name));
        tr.appendChild(name);
        var idCell = el("td");
        idCell.appendChild(recordLink(row.record_id));
        tr.appendChild(idCell);
        var vs = el("td");
        vs.appendChild(el("span", "badge badge-decision-accept", row.verdict_status));
        tr.appendChild(vs);
        var edit = el("td", null, row.edit_status);
        if (row.fields_failed) edit.appendChild(el("span", "muted", " (" + row.fields_failed + " failed)"));
        tr.appendChild(edit);
        tr.appendChild(el("td", "muted", fmt(row.confirmed_by)));
        tr.appendChild(el("td", null, fmt(row.times_accepted)));
        tr.appendChild(el("td", "muted", fmt(row.first_accepted)));
        body.appendChild(tr);
      });
    });
  }

  // ---- settings --------------------------------------------------------
  function initSettings() {
    var form = byId("settings-form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var status = byId("settings-status");
      var updates = {};
      form.querySelectorAll("tr[data-key]").forEach(function (tr) {
        var key = tr.dataset.key;
        var input = tr.querySelector("input, select");
        if (!input) return; // secret rows carry no editable input
        if (input.type === "checkbox") updates[key] = input.checked;
        else if (input.type === "number") updates[key] = parseFloat(input.value);
        else updates[key] = input.value;
      });
      sendJSON("/api/settings", "PUT", updates).then(function (res) {
        if (res.ok) {
          status.textContent = "Saved. Applies to new runs immediately; persists across restarts.";
        } else {
          status.textContent = res.body.detail || "update refused";
        }
      });
    });
  }

  // ---- boot ------------------------------------------------------------
  var inits = {
    overview: initOverview,
    run: initRun,
    queues: initQueues,
    history: initHistory,
    accepted: initAccepted,
    settings: initSettings,
  };
  if (inits[page]) inits[page]();
})();
