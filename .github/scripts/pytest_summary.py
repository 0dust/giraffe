"""Render pytest's JUnit results in the Actions job summary."""

import sys
from html import escape
from pathlib import Path
from xml.etree import ElementTree


def summarize(path):
    print("## pytest results\n")
    if not path.exists():
        print("No test-results file was produced. Check the setup and pytest step logs; coverage is unknown.")
        return
    cases = ElementTree.parse(path).findall(".//testcase")
    counts = {kind: sum(case.find(kind) is not None for case in cases)
              for kind in ("failure", "error", "skipped")}
    print(f"{len(cases)} cases · {counts['failure']} failed · {counts['error']} errors · {counts['skipped']} skipped\n")
    for kind, title in (("failure", "Failures"), ("error", "Errors"), ("skipped", "Skipped coverage")):
        if not counts[kind]:
            continue
        print(f"### {title}\n\n<pre>")
        for case in cases:
            detail = case.find(kind)
            if detail is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                print(escape(name))
                print(escape(detail.get("message", "See pytest logs")[:1000]))
        print("</pre>\n")


if __name__ == "__main__":
    summarize(Path(sys.argv[1]))
