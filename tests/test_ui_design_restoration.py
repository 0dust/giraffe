"""The reference design survives CSP-compatible script loading."""

import re
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parents[1] / "src/giraffe/static"


class ScriptPolicy(HTMLParser):
    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        assert not any(name.lower().startswith("on") for name in attrs), (tag, attrs)
        if tag == "script":
            assert attrs.get("src"), "Scripts must load from external files"


def test_index_and_app_have_no_inline_scripts_or_handlers():
    html = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()
    ScriptPolicy().feed(html)
    for source in (html, app):
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", source, re.I)
        assert not re.search(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", source, re.I)
        assert all(not body.strip() for body in re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.S | re.I))


def test_reference_coat_stepper_mosaic_and_quick_fill_are_present():
    app = (STATIC / "app.js").read_text()
    css = (STATIC / "styles.css").read_text()
    for name in ("coatGeometry", "coatPolygons", "reticulateCells", "initReticulate", "renderHeroCoat",
                 "request-mosaic", "conditionGeometry", "spotGlyph", "handleConditionChange", "jumpConditionPage"):
        assert name in app
    for token in ("--coat-line", "--coat-spot", ".hero-coat"):
        assert token in css
    for text in ("setup-stepper", "Connect", "Checks", "Launch", "Point Giraffe at an endpoint.", "Quick fill", "Load from file"):
        assert text in app
