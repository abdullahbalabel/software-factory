"""Write the owner's usage guide and command list into README.md from the supervisor's own text (the same text
`/help` posts), between the markers <!-- usage:start --> and <!-- usage:end -->.

Usage: python infra/gen-readme-usage.py          (rewrite README.md)
       python infra/gen-readme-usage.py --check  (exit 1 if README.md is out of date; run before every commit that
                                                  touches supervisor/factory_supervisor/commands.py or config.yml)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "supervisor"))
from factory_supervisor import commands  # noqa: E402

START, END = "<!-- usage:start -->", "<!-- usage:end -->"


def section() -> str:
    cfg = yaml.safe_load((ROOT / "supervisor" / "config.yml").read_text(encoding="utf-8"))
    body = commands.help_text(cfg["default_product"], cfg["attachments"])
    note = ("_Generated from `supervisor/factory_supervisor/commands.py` by `infra/gen-readme-usage.py`; "
            "the same text is posted by `/help` in Zulip. Do not edit by hand._")
    return f"{START}\n## How to use the factory (from Zulip)\n\n{note}\n\n{body}\n{END}"


def main() -> int:
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    new_section = section()
    if START in text:
        head, rest = text.split(START, 1)
        tail = rest.split(END, 1)[1]
        new = head + new_section + tail
    else:
        anchor = "## Read in this order"
        new = text.replace(anchor, new_section + "\n\n" + anchor, 1)
    if "--check" in sys.argv:
        if new != text:
            print("README.md usage section is out of date: run python infra/gen-readme-usage.py", file=sys.stderr)
            return 1
        print("README.md usage section is current")
        return 0
    readme.write_text(new, encoding="utf-8", newline="\n")
    print("README.md usage section written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
