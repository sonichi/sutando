#!/usr/bin/env python3
"""Focused regression coverage for inferred-workstream grouping in the web UI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def _helper_source() -> str:
    start_marker = "// ─── Workstream-grouped task display helpers"
    end_marker = "// ─── End workstream-grouped task display helpers"
    start = SOURCE.index(start_marker)
    end = SOURCE.index(end_marker, start)
    return SOURCE[start:end]


def _visibility_policy_source() -> str:
    marker = "function isOwnerVisibleTask"
    assert marker in SOURCE, "owner task history has no internal-task visibility policy"
    start = SOURCE.index(marker)
    end = SOURCE.index("\n}", start) + 2
    return SOURCE[start:end]


def _run_helper_probe() -> dict:
    # Execute the exact browser helper source. A tiny DOM-free esc stand-in is
    # sufficient here because its contract is the same text-to-HTML escaping
    # used by the page's DOM-based esc function.
    probe = r"""
let taskWorkstreamNames = Object.create(null);
const collapsedTaskWorkstreams = new Set();
const seenTaskWorkstreams = new Set();
function persistTaskWorkstreamDisplayState() {}
function esc(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
""" + _visibility_policy_source() + _helper_source() + r"""
const rows = [
  ['a1', {time: new Date(10), status: 'done', workstream_id: 'a', workstream_name: 'Alpha <img src=x onerror=boom>'}],
  ['b1', {time: new Date(20), status: 'done', workstream_id: 'b', workstream_name: 'Mission B'}],
  ['a2', {time: new Date(30), status: 'done', workstream_id: 'a', workstream_name: 'Alpha <img src=x onerror=boom>'}],
  ['b2', {time: new Date(40), status: 'working', workstream_id: 'b', workstream_name: 'Mission B'}],
  ['u1', {time: new Date(5), status: 'done'}],
];
const grouped = groupedTaskDisplay(rows);
const groupedHtml = renderTaskWorkstreamGroups(grouped, function(entry, index) {
  return '<i>' + (index + 1) + ':' + entry[0] + '</i>';
});
const flat = groupedTaskDisplay([
  ['old', {time: new Date(1)}],
  ['new', {time: new Date(2)}],
]);
const flatHtml = renderTaskWorkstreamGroups(flat, function(entry, index) {
  return '<i>' + (index + 1) + ':' + entry[0] + '</i>';
});
process.stdout.write(JSON.stringify({
  grouped: grouped.grouped,
  groupIds: grouped.groups.map(function(group) { return group.id; }),
  groupEntries: grouped.groups.map(function(group) {
    return group.entries.map(function(entry) { return entry[0]; });
  }),
  displayEntries: grouped.entries.map(function(entry) { return entry[0]; }),
  groupedHtml: groupedHtml,
  collapsedWorkstreams: Array.from(collapsedTaskWorkstreams),
  flatGrouped: flat.grouped,
  flatEntries: flat.entries.map(function(entry) { return entry[0]; }),
  flatHtml: flatHtml,
}));
"""
    result = subprocess.run(
        ["node", "-e", probe],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _run_visibility_probe() -> dict:
    probe = r"""
let taskWorkstreamNames = Object.create(null);
const collapsedTaskWorkstreams = new Set();
const seenTaskWorkstreams = new Set();
function persistTaskWorkstreamDisplayState() {}
function esc(value) { return String(value); }
""" + _visibility_policy_source() + _helper_source() + r"""
const display = groupedTaskDisplay([
  ['task-owner-message', {time: new Date(60), status: 'working', source: 'discord'}],
  ['task-owner-api', {time: new Date(50), status: 'done', source: 'api'}],
  ['task-cron-main-loop-123', {time: new Date(40), status: 'working', source: 'cron'}],
  ['task-cron-daily-brief-456', {time: new Date(30), status: 'done', source: 'cron'}],
  ['task-health-789', {time: new Date(20), status: 'working', source: 'health-check'}],
  ['task-discord-e2e-012', {time: new Date(10), status: 'done', source: 'discord'}],
]);
process.stdout.write(JSON.stringify({
  entries: display.entries.map(function(entry) { return entry[0]; }),
}));
"""
    result = subprocess.run(
        ["node", "-e", probe],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_internal_system_tasks_are_hidden_from_owner_history() -> None:
    data = _run_visibility_probe()
    assert data["entries"] == ["task-owner-message", "task-owner-api"]


def test_group_order_alphabetical_numbering_and_escaping() -> None:
    data = _run_helper_probe()

    # Groups are alphabetical by display name; tasks remain newest-first within
    # each group. Ungrouped tasks remain a final explicit bucket.
    assert data["groupIds"] == ["a", "b", "__ungrouped__"]
    assert data["groupEntries"] == [["a2", "a1"], ["b2", "b1"], ["u1"]]
    assert data["displayEntries"] == ["a2", "a1", "b2", "b1", "u1"]

    # The renderer numbers the flattened display globally and escapes inferred
    # names before inserting them into HTML.
    assert "1:a2" in data["groupedHtml"] and "5:u1" in data["groupedHtml"]
    assert "&lt;img src=x onerror=boom&gt;" in data["groupedHtml"]
    assert "<img src=x onerror=boom>" not in data["groupedHtml"]
    assert data["collapsedWorkstreams"] == ["a", "__ungrouped__"]
    assert 'data-workstream-id="a" aria-expanded="false"' in data["groupedHtml"]
    assert 'data-workstream-id="b" aria-expanded="true"' in data["groupedHtml"]


def test_all_ungrouped_keeps_flat_appearance() -> None:
    data = _run_helper_probe()
    assert data["flatGrouped"] is False
    assert data["flatEntries"] == ["new", "old"]
    assert "task-workstream-header" not in data["flatHtml"]


def test_all_three_display_paths_share_order_and_history_is_quiet() -> None:
    # Voice expand:N, the hidden list, and the visible Tasks tab must all call
    # the same helper; independently sorted paths caused prior targeting bugs.
    voice = SOURCE[SOURCE.index("new MutationObserver"):SOURCE.index("function toggleResult")]
    hidden = SOURCE[SOURCE.index("function renderTasks"):SOURCE.index("// ─── Toast notifications")]
    visible = SOURCE[SOURCE.index("} else if (tab === 'tasks')"):SOURCE.index("} else if (tab === 'notes')")]
    assert "groupedTaskDisplay(" in voice
    assert "groupedTaskDisplay(" in hidden
    assert "groupedTaskDisplay(" in visible
    assert "esc(displayText)" in hidden
    assert "esc(displayText)" in visible

    # History hydration seeds knownTaskIds before merging, so old tasks cannot
    # trigger the active poll's "Context received" toast. It never triggers
    # inference itself: the always-on agent API owns that maintenance loop.
    hydrate = SOURCE[SOURCE.index("async function hydrateTaskHistory"):SOURCE.index("// ─── Poll agent API")]
    assert "fetch('/api/task-history')" in hydrate
    assert "fetch('/api/task-workstreams/infer'" not in hydrate
    assert hydrate.index("knownTaskIds.add(row.id)") < hydrate.index("taskMap[row.id] = mergeTaskRow")
    assert "data.inference && data.inference.pending" in hydrate
    assert "scheduleTaskHistoryHydration(10000)" in hydrate
    assert "setTimeout(function()" in SOURCE[SOURCE.index("function scheduleTaskHistoryHydration"):SOURCE.index("async function hydrateTaskHistory")]

    # Both live update paths preserve the additive workstream metadata.
    load = SOURCE[SOURCE.index("function loadPersistedTaskMap"):SOURCE.index("function loadPersistedExpanded")]
    update = SOURCE[SOURCE.index("function updateTask"):SOURCE.index("const expandedTasks")]
    poll = SOURCE[SOURCE.index("function startTaskPolling"):SOURCE.index("function stopTaskPolling")]
    assert "!isOwnerVisibleTask(taskId, parsed[taskId])" in load
    assert "if (!isOwnerVisibleTask(taskId, null)) return" in update
    assert "!isOwnerVisibleTask(row.id, row)" in hydrate
    assert poll.index("!isOwnerVisibleTask(t.id, t)") < poll.index("apiTasks.add(t.id)")
    assert "Object.assign({}, existing" in update
    assert "taskMap[t.id] = mergeTaskRow(existing, t)" in poll
    assert "if (taskHistoryInitialLoadComplete)" in poll
    assert "else if (!existing.workstream_id" in poll
    assert "scheduleTaskHistoryHydration(1000)" in poll
    assert "workstream_id:" in SOURCE and "workstream_name:" in SOURCE
    assert "PERSIST_KEY_WORKSTREAM_DISPLAY" in SOURCE
    assert "task-workstream--collapsed" in SOURCE
    assert "toggleTaskWorkstream(workstreamHeader.dataset.workstreamId)" in SOURCE

    proxy = SOURCE[SOURCE.index("Task history carries owner prompts"):SOURCE.index("if (url.pathname === BROWSER_TRANSPORT_ROUTE)")]
    assert "SUTANDO_API_TOKEN" in proxy
    assert "isLoopbackAddress(req.socket.remoteAddress)" in proxy
    assert "task history is local-only" in proxy


def main() -> None:
    tests = [
        test_internal_system_tasks_are_hidden_from_owner_history,
        test_group_order_alphabetical_numbering_and_escaping,
        test_all_ungrouped_keeps_flat_appearance,
        test_all_three_display_paths_share_order_and_history_is_quiet,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print("web-client inferred-workstream grouping tests passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise
