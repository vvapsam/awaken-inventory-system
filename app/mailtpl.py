"""The little language the email templates are written in.

Four constructs, and no more, because every one of them is something somebody
editing an email at eleven at night has to hold in their head:

    ${record.first_name}                a value, escaped
    ${block.facts}                      a block the system builds
    ${block.note}  …  ${/block.note}    a block wrapped around your own words
    ${if record.deadline} … ${/if}      drop the lot when there is nothing to say

Values are escaped; blocks are trusted, because we wrote them. Anything that
isn't in the spec for the template being saved is refused by name rather than
posted to thirty people as literal text — a placeholder typo does not raise an
error on its own, it just quietly ships.
"""
import html
import re
from difflib import get_close_matches

#: ${name}, ${name "arg" "arg"}, ${/name}. The name is dotted; the arguments
#: are double-quoted and optional.
#: An argument is either a quoted string (a label you typed) or a bare dotted
#: name (what ${if} tests). Both, so ${if record.deadline} and
#: ${block.button "Confirm" "Takes a minute"} are the same shape of thing.
TOKEN = re.compile(
    r'\$\{(/?)([a-z][a-z0-9_.]*)((?:\s+(?:"[^"]*"|[a-z][a-z0-9_.]*))*)\s*\}', re.I)
ARG = re.compile(r'"([^"]*)"|([a-z][a-z0-9_.]*)', re.I)


def _args(raw):
    return [q if q else bare for q, bare in ARG.findall(raw or "")]


class TemplateError(ValueError):
    """Something in the source we will not save. Carries a readable reason."""


def tokens(src: str):
    """Every token in order, as (close?, name, args, whole match, position)."""
    for m in TOKEN.finditer(src or ""):
        yield (bool(m.group(1)), m.group(2), _args(m.group(3)),
               m.group(0), m.start())


def check(src: str, spec: dict) -> None:
    """Refuse a template we could not render. Raises TemplateError.

    `spec` is {"values": {...}, "blocks": {...}, "pairs": {...}} — the names
    this particular email is allowed to use. It differs per email because
    ${block.qr} means nothing on an invitation and ${record.deadline} means
    nothing on a receipt-chaser.
    """
    known = set(spec["values"]) | set(spec["blocks"]) | set(spec["pairs"])
    stack = []
    for close, name, tok_args, whole, _at in tokens(src):
        if name == "if":
            if close:
                if not stack or stack[-1][0] != "if":
                    raise TemplateError(
                        "%s closes an ${if} that was never opened." % whole)
                stack.pop()
                continue
            cond = tok_args
            if not cond:
                raise TemplateError(
                    "%s doesn't say what to test. Try ${if record.deadline}."
                    % whole)
            if cond[0] not in known:
                near = get_close_matches(cond[0], known, n=1, cutoff=0.6)
                raise TemplateError(
                    "${if %s} tests something we don't know.%s" % (
                        cond[0],
                        " Did you mean ${if %s}?" % near[0] if near else ""))
            stack.append(("if", whole))
            continue
        if close:
            if name not in spec["pairs"]:
                raise TemplateError(
                    "%s closes something that isn't a block you can open." % whole)
            if not stack or stack[-1][0] != name:
                raise TemplateError(
                    "%s closes a block that was never opened." % whole)
            stack.pop()
            continue
        if name in spec["pairs"]:
            stack.append((name, whole))
            continue
        if name not in known:
            near = get_close_matches(name, known, n=1, cutoff=0.6)
            raise TemplateError(
                "${%s} isn't something we know.%s" % (
                    name,
                    " Did you mean ${%s}?" % near[0] if near else ""))
    if stack:
        kind, whole = stack[-1]
        raise TemplateError(
            "%s was opened and never closed. Add ${/%s}."
            % (whole, "if" if kind == "if" else kind))


def _close_of(src, name, start):
    """Where the matching ${/name} is, honouring nesting of the same name."""
    depth = 0
    for close, tok, _a, whole, at in tokens(src):
        if at < start or tok != name:
            continue
        if close:
            if depth == 0:
                return at, at + len(whole)
            depth -= 1
        else:
            depth += 1
    return None, None


def _ifs(src, values):
    """Resolve ${if x} … ${/if}, innermost first so nesting works."""
    while True:
        opens = [(at, whole, a) for close, name, a, whole, at in tokens(src)
                 if name == "if" and not close]
        if not opens:
            return src
        at, whole, args = opens[-1]                 # innermost opener
        m = re.compile(r"\$\{/if\s*\}").search(src, at)
        if not m:
            return src                              # check() already refused this
        keep = bool(str(values.get(args[0], "")).strip()) if args else False
        inner = src[at + len(whole):m.start()]
        src = src[:at] + (inner if keep else "") + src[m.end():]


def render(src: str, spec: dict, values: dict, blocks: dict, pairs: dict) -> str:
    """Source in, HTML out. Assumes check() has already passed."""
    src = _ifs(src or "", values)

    # Paired blocks, outermost first — the inner text is rendered before it is
    # handed over, so ${record.x} inside a note still resolves.
    while True:
        hit = next(((at, name, args, whole) for close, name, args, whole, at
                    in tokens(src)
                    if not close and name in pairs), None)
        if not hit:
            break
        at, name, args, whole = hit
        c_at, c_end = _close_of(src, name, at + len(whole))
        if c_at is None:
            break
        inner = render(src[at + len(whole):c_at], spec, values, blocks, pairs)
        src = src[:at] + (pairs[name](inner, *args) or "") + src[c_end:]

    out, last = [], 0
    for close, name, args, whole, at in tokens(src):
        out.append(src[last:at])
        last = at + len(whole)
        if close:
            continue
        if name in blocks:
            out.append(blocks[name](*args) or "")
        elif name in values:
            out.append(html.escape(str(values.get(name) or "")))
        # Anything else was refused at save time; drop it rather than ship it.
    out.append(src[last:])
    return "".join(out)


#: Tags no mail client will run, and that nobody editing a gym's newsletter
#: means to include. Stripped rather than refused: the point is that a paste
#: from somewhere else cannot carry anything live into somebody's inbox.
_STRIP_TAG = re.compile(r"<\s*(script|iframe|object|embed|link|meta)\b[^>]*>"
                        r"(?:.*?<\s*/\s*\1\s*>)?", re.I | re.S)
_STRIP_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_STRIP_JS = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)


def sanitise(src: str) -> tuple:
    """Take the live things out. Returns (clean, what_was_removed)."""
    removed = []
    out, n = _STRIP_TAG.subn("", src or "")
    if n:
        removed.append("%d script or embed tag%s" % (n, "" if n == 1 else "s"))
    out, n = _STRIP_ATTR.subn("", out)
    if n:
        removed.append("%d inline handler%s" % (n, "" if n == 1 else "s"))
    out, n = _STRIP_JS.subn(r'\1=\2#\2', out)
    if n:
        removed.append("%d javascript: link%s" % (n, "" if n == 1 else "s"))
    return out, removed


def unclosed(src: str) -> int:
    """A rough count of HTML tags left open. Advisory only.

    Mail clients close these themselves and render anyway, so this is a warning
    and never a wall — refusing to save over it would stop somebody fixing a
    typo at the one moment they need to.
    """
    void = {"br", "img", "hr", "input", "meta", "link", "source", "col"}
    stack = []
    for m in re.finditer(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)\s*>", src or ""):
        closing, tag, selfshut = m.group(1), m.group(2).lower(), m.group(3)
        if tag in void or selfshut:
            continue
        if closing:
            if tag in stack:
                while stack and stack.pop() != tag:
                    pass
        else:
            stack.append(tag)
    return len(stack)
