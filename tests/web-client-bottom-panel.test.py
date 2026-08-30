from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src" / "web-client.ts").read_text()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


check("id=\"bottom-panel\"" in SOURCE, "bottom panel remains present")
check("id=\"btn-panel-toggle\"" in SOURCE, "bottom panel has a toggle control")
check("function toggleBottomPanel()" in SOURCE, "bottom panel toggle behavior exists")
check("#bottom-panel.collapsed #transcript { display: none; }" in SOURCE, "collapsed state hides transcript")
check("PERSIST_KEY_BOTTOM_PANEL" in SOURCE, "panel state is persisted")
check("max-height: 30vh" in SOURCE, "transcript height is bounded")
print("web-client bottom-panel tests passed")
