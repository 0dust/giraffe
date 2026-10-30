"""External theme boot preserves the pre-restoration dark default under CSP."""

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
    [(None, True, "dark"), (None, False, "dark"),
     ("dark", True, "dark"), ("light", False, "light"),
     ("invalid", True, "dark")],
)
def test_boot_is_dark_unless_stored_theme_is_light(stored, system_light, expected):
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
  window: {{matchMedia: () => {{throw Error('System preference must not override dark default');}}}},
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
assert.equal(document.documentElement.dataset.theme, undefined);
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
