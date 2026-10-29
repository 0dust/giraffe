"""Theme boot works before CSS under CSP and preserves accessible text contrast."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / "src/giraffe/static"


def test_theme_boot_is_external_and_runs_before_styles():
    html = (STATIC / "index.html").read_text()
    assert '<script src="/static/boot.js"></script>' in html
    assert html.index('/static/boot.js') < html.index('/static/styles.css')
    assert all(not body.strip() for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))


@pytest.mark.parametrize(
    ("stored", "system_light", "expected"),
    [(None, True, "light"), (None, False, "dark"),
     ("dark", True, "dark"), ("light", False, "light"),
     ("invalid", True, "light")],
)
def test_boot_uses_system_theme_until_user_chooses(stored, system_light, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI tests")
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const values = {{'giraffe-theme': {json.dumps(stored)}, 'giraffe-rail': 'collapsed'}};
const classes = new Set();
const document = {{documentElement: {{dataset: {{}}, classList: {{add: value => classes.add(value)}}}}}};
vm.runInNewContext({json.dumps((STATIC / 'boot.js').read_text())}, {{
  document,
  window: {{matchMedia: query => {{assert.equal(query, '(prefers-color-scheme: dark)'); return {{matches: {json.dumps(not system_light)}}};}}}},
  localStorage: {{getItem: key => values[key], setItem: () => {{throw Error('Automatic theme must not persist');}}}}
}});
assert.equal(document.documentElement.dataset.theme, {json.dumps(expected)});
assert(classes.has('rail-collapsed'));
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_theme_boot_survives_blocked_storage():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI tests")
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const document = {{documentElement: {{dataset: {{}}, classList: {{add() {{}}}}}}}};
vm.runInNewContext({json.dumps((STATIC / 'boot.js').read_text())}, {{document,
  window: {{matchMedia: () => ({{matches: false}})}},
  localStorage: {{getItem: () => {{throw Error('blocked');}}}}
}});
assert.equal(document.documentElement.dataset.theme, 'light');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def luminance(hex_color):
    components = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in components]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(foreground, background):
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_meet_aa_on_all_surfaces(theme):
    css = (STATIC / "styles.css").read_text()
    selector = r":root\[data-theme=light\]" if theme == "light" else r":root"
    block = re.search(selector + r"\s*\{([^}]+)\}", css).group(1)
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", block))
    for foreground in ("--text", "--text-2", "--text-3", "--accent", "--accent-2", "--pass", "--fail", "--blocked", "--unknown", "--skip", "--live", "--chart-ttft"):
        for background in ("--bg", "--bg-elev", "--surface", "--surface-2", "--surface-3"):
            ratio = contrast(tokens[foreground], tokens[background])
            assert ratio >= 4.5, f"{theme} {foreground} on {background}: {ratio:.2f}:1"


def test_manual_theme_choice_persists_without_pin_on_initialization():
    from tests.test_ux_results import run_render_checks
    run_render_checks(r"""
const preferences=new Map();localStorage={getItem:key=>preferences.get(key),setItem:(key,value)=>preferences.set(key,value)};
document.documentElement={dataset:{theme:'light'},classList:{contains:()=>false,toggle(){}}};initInterface();assert.equal(preferences.has('giraffe-theme'),false);
setTheme('dark');assert.equal(document.documentElement.dataset.theme,'dark');assert.equal(preferences.get('giraffe-theme'),'dark');
setTheme('light');assert.equal(preferences.get('giraffe-theme'),'light');
""",dom=True)


def test_system_theme_changes_until_manual_choice_and_updates_toggle_label():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI tests")
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const values = {{}};
const attributes = {{}};
const toggle = {{setAttribute: (key, value) => attributes[key] = value}};
const root = {{dataset: {{}}, classList: {{add() {{}}}}}};
let change;
const media = {{matches: false, addEventListener(type, callback) {{assert.equal(type, 'change'); change = callback;}}}};
vm.runInNewContext({json.dumps((STATIC / 'boot.js').read_text())}, {{
  document: {{documentElement: root, querySelector: () => toggle}},
  window: {{matchMedia: query => {{assert.equal(query, '(prefers-color-scheme: dark)'); return media;}}}},
  localStorage: {{getItem: key => values[key]}}
}});
assert.equal(root.dataset.theme, 'light');
assert.equal(typeof change, 'function');
media.matches = true; change(media);
assert.equal(root.dataset.theme, 'dark');
assert.equal(attributes['aria-label'], 'Switch to light theme');
values['giraffe-theme'] = 'light';
change(media);
assert.equal(root.dataset.theme, 'dark', 'A manual choice prevents automatic changes');
delete values['giraffe-theme']; root.dataset.theme = 'light'; root.dataset.themePreference = 'manual';
change(media);
assert.equal(root.dataset.theme, 'light', 'Manual choice survives blocked storage');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_request_evidence_fits_desktop_and_uses_stacked_mobile_cards():
    css = (STATIC / "styles.css").read_text()
    table = re.search(r"\.request-evidence \.request-table\s*\{([^}]+)\}", css).group(1)
    assert "min-width: 0" in table
    assert "width: 100%" in table
    assert "table-layout: fixed" in table
    assert ".request-question" in css and "text-overflow: ellipsis" in css
    mobile = css[css.index("@media (max-width: 599px)"):]
    assert ".request-evidence-row" in mobile and "grid-template-columns" in mobile
    assert "content: attr(data-label)" in mobile
    assert re.search(r"\.request-evidence > \.request-table-scroll\s*\{[^}]*max-height: none;[^}]*overflow: visible", mobile)
    assert 'grid-template-areas: "status condition latency ttft" "question question question question" "answer answer answer answer"' in mobile
    assert re.search(r"\.request-evidence-row td::before\s*\{[^}]*display: none", mobile)
    assert ".request-evidence-row td.has-error" in mobile
    assert ".request-mobile-received" in mobile
    assert re.search(r"\.request-mobile-mismatch > span\s*\{[^}]*flex: 1 1 0;[^}]*min-width: 0;[^}]*text-overflow: ellipsis", mobile)
    assert ".request-evidence-answer" in mobile
    assert re.search(r"\.setup-limits-line\s*\{[^}]*padding: 8px 12px", css)
    assert ".setup-limit-field" in css
    assert ".api-key-row" in css
    assert "#new-run-form { padding-bottom:" in mobile
    assert "@media (hover: none), (pointer: coarse)" in css
    assert "#setup-navigation-hint" in css


def test_request_columns_keep_questions_readable_and_statuses_visible():
    css = (STATIC / "styles.css").read_text()
    assert re.search(r"\.request-evidence \.request-table th:nth-child\(6\)\s*\{[^}]*width: 28ch", css)
    assert re.search(r"\.request-evidence-row \.request-question\s*\{[^}]*min-width: 28ch", css)
    assert re.search(r"\.request-evidence \.request-table th:nth-child\(5\)\s*\{[^}]*white-space: nowrap", css)
    pill = re.search(r"\.request-evidence \.request-table \.status\s*\{([^}]+)\}", css).group(1)
    assert "padding: 0 6px" in pill
    assert "border: 0" not in pill
    assert "background: transparent" not in pill
    assert "border-radius: 0" not in pill
    assert re.search(r"#setup-duration\s*\{[^}]*padding: 8px 12px", css)
