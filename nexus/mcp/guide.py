# nexus/mcp/guide.py
"""The public setup guide for AI apps, served at ``/help/ai-apps`` with no sign-in.

Every directory InfoJoy GTM is listed in (Anthropic, OpenAI, Microsoft, the MCP Registry) asks for
a documentation and a support address that a reviewer, and an automated check, can open. The SPA
would answer such an address with an empty shell, so this page is rendered on the server from one
source, ``help/ai-apps.md``, which the listing repository's README points at.

``render`` handles exactly the Markdown the guide uses: headings, paragraphs, flat lists, tables,
fenced code, inline code, bold and links. Standard library only. The source is ours, and every
character of it is escaped anyway; a link may only be https, mailto, an anchor or a path here, and
anything else fails the test that renders the guide rather than reaching the page.
"""
from __future__ import annotations

import html
import pathlib
import re
from functools import lru_cache

SOURCE = pathlib.Path(__file__).parent / "help" / "ai-apps.md"
PATH = "/help/ai-apps"

_HEADING = re.compile(r"(#{1,4}) (.+)")
_LIST_ITEM = re.compile(r"(- |\d+\. )(.*)")
_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)")
_SAFE_HREF = re.compile(r"(https://|mailto:|#|/)\S*")


def _text(value: str) -> str:
    return html.escape(value, quote=False)


def _href(value: str) -> str:
    if not _SAFE_HREF.fullmatch(value):
        raise ValueError(f"link not allowed on the public guide: {value!r}")
    return html.escape(value, quote=True)


def _inline(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(_text(text[pos:m.start()]))
        code, bold, label, href = m.groups()
        if code is not None:
            out.append(f"<code>{_text(code)}</code>")
        elif bold is not None:
            out.append(f"<strong>{_inline(bold)}</strong>")
        else:
            out.append(f'<a href="{_href(href)}">{_inline(label)}</a>')
        pos = m.end()
    out.append(_text(text[pos:]))
    return "".join(out)


def slug(heading: str) -> str:
    """The anchor a heading gets: ``## Support`` is ``#support``."""
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def _starts_block(line: str) -> bool:
    return bool(line.startswith(("```", "|")) or _HEADING.match(line) or _LIST_ITEM.match(line))


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    head, body = cells[0], [r for r in cells[1:] if not all(set(c) <= set("-: ") for c in r)]
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return (f'<div class="table"><table><thead><tr>{th}</tr></thead>'
            f"<tbody>{trs}</tbody></table></div>")


def render(markdown: str) -> str:
    """HTML for the guide's Markdown subset. Raises on an unclosed code block or a bad link."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("```")), None)
            if end is None:
                raise ValueError(f"unclosed code block at line {i + 1}")
            out.append(f"<pre><code>{_text(chr(10).join(lines[i + 1:end]))}</code></pre>")
            i = end + 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2).strip()
            out.append(f'<h{level} id="{slug(text)}">{_inline(text)}</h{level}>')
            i += 1
            continue
        if line.startswith("|"):
            start = i
            while i < len(lines) and lines[i].startswith("|"):
                i += 1
            out.append(_table(lines[start:i]))
            continue
        item = _LIST_ITEM.match(line)
        if item:
            ordered = item.group(1)[0].isdigit()
            items: list[str] = []
            while i < len(lines) and lines[i].strip():
                item = _LIST_ITEM.match(lines[i])
                if item and item.group(1)[0].isdigit() == ordered:
                    items.append(item.group(2))
                elif lines[i].startswith(" ") and items:
                    items[-1] += " " + lines[i].strip()  # a wrapped line of the same item
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
            continue
        para: list[str] = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


_STYLE = """
:root { --bg: #ffffff; --fg: #1b1530; --muted: #5b5670; --line: #e6e3ef; --code: #f5f3fa;
  --link: #5b2bd6; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14121c; --fg: #ece9f5; --muted: #a9a4bd; --line: #2c2840; --code: #1e1b2a;
    --link: #b7a2ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header, main, footer { max-width: 760px; margin: 0 auto; padding: 0 16px; }
header { display: flex; align-items: center; gap: 10px; padding-top: 24px; font-weight: 600; }
header img { width: 32px; height: 32px; }
main { padding-bottom: 32px; }
h1 { font-size: 1.9rem; line-height: 1.25; margin: 24px 0 12px; }
h2 { font-size: 1.35rem; margin: 36px 0 8px; padding-top: 12px;
  border-top: 1px solid var(--line); }
h3 { font-size: 1.1rem; margin: 24px 0 6px; }
a { color: var(--link); }
code { font: 0.92em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--code); padding: 1px 4px; border-radius: 4px; overflow-wrap: anywhere; }
pre { background: var(--code); padding: 12px 14px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; overflow-wrap: normal; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.95rem; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px;
  border-bottom: 1px solid var(--line); }
footer { color: var(--muted); font-size: 0.9rem; padding-top: 16px; padding-bottom: 32px;
  border-top: 1px solid var(--line); }
footer a { margin-right: 16px; }
"""


@lru_cache(maxsize=1)
def ai_apps_page() -> str:
    """The whole page, rendered once per process."""
    source = SOURCE.read_text(encoding="utf-8")
    title = next(line[2:].strip() for line in source.splitlines() if line.startswith("# "))
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_text(title)}</title>\n"
        '<meta name="description" content="Connect InfoJoy GTM to Claude, ChatGPT, Microsoft '
        'Copilot, Gemini, Cursor, VS Code and model APIs: setup, permissions, data and support.">\n'
        '<link rel="icon" type="image/png" href="/favicon-32.png">\n'
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        '<header><img src="/infojoy-mark.png" alt="" width="32" height="32">InfoJoy GTM</header>\n'
        f"<main>\n{render(source)}\n</main>\n"
        '<footer><a href="https://infojoy.com/legal.html#privacy">Privacy</a>'
        '<a href="https://infojoy.com/legal.html#terms">Terms</a>'
        '<a href="#support">Support</a><a href="/">Open InfoJoy GTM</a></footer>\n'
        "</body>\n</html>\n"
    )
