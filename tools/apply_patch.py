#!/usr/bin/env python3
"""Apply a V4A ("*** Begin Patch") patch to files on disk.

## Why this exists

The revision models (gpt-oss:120b-cloud, glm-5.2:cloud) are trained on OpenAI's codex
`apply_patch` tool and reach for it unprompted. Across the 2026-08-21 emperor cycles the
model ran it as a SHELL command through `exec` eight times, every one of them dying with

    /bin/sh: 1: apply_patch: not found
    [exit code 127]

and each failure cost a tool-call round trip inside REVISION_TIMEOUT before the model
fell back to re-emitting the whole file through `write_file`. The eighth emperor pass's
response was a line in REVISION_SYSTEM_PROMPT telling the model not to use it; this is
the other half of that fix -- the tool itself, so the reflex lands instead of failing.

`write_file` can already do everything this does. The win is tokens: three of those eight
attempts were two-line import additions to a 300-line main.py, which `write_file` can
only express by re-emitting all 300 lines.

## The format, as the models actually emit it

    *** Begin Patch
    *** Update File: /opt/strategies/clone_x/main.py
    @@
    -from price_feed import get_price
    +from price_feed import get_price
    +from regime import regime as get_regime
    *** End Patch

Every one of the five distinct patches recovered from v/emperor_logs/ (the corpus this
was written against) looks like that, and two details of it drove the parser:

  - **No context lines.** Canonical V4A hunks carry ' '-prefixed context around the
    change. Not one hunk in the corpus has any: they are pure runs of '-' followed by
    '+'. So the located block is whatever the '-' lines say it is, and there is nothing
    else to disambiguate with.
  - **Bare `@@`.** No enclosing-scope text after the marker. It is supported below as a
    search hint because the format allows it, but nothing in the corpus supplies one.

Paths arrive both absolute (/opt/strategies/seed_x/main.py) and bare-relative (main.py),
so relative paths resolve against the cwd of whoever called -- which for `exec` is the
cwd master-agent.py was started in.

## Why it fails closed

A half-applied patch is worse here than no patch at all. monitor.py's
revision_changed_anything only asks whether the strategy directory changed; a main.py
with hunk 1 of 3 applied is "changed", so it goes on to the smoke test and burns the
revision slot on a file the model never intended to exist. Therefore:

  - a hunk that matches its target zero times is an error;
  - a hunk that matches MORE than once is an error, not a first-match guess;
  - nothing is written until every hunk of every file in the patch has been resolved,
    so a patch touching two files cannot leave one of them edited.

Errors name `write_file` as the fallback, because that is what the model should do next
and it will not think of it on its own.
"""

import os
import sys

# Unicode the model retypes into what were ASCII characters in the file it is quoting.
# This is not hypothetical: patch04 of the corpus quotes a comment reading "pull-back"
# with U+2011 NON-BREAKING HYPHEN, and several patches carry en dashes in '-' lines that
# are plain '-' on disk. The model is reconstructing those lines from its context window,
# not copying bytes, so an exact-match-only applier rejects a patch whose intent is
# perfectly clear. Exact matching is still tried first; this is the second pass.
_PUNCT_FOLD = {
    0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-', 0x2014: '-', 0x2015: '-',
    0x2018: "'", 0x2019: "'", 0x201a: "'", 0x201b: "'",
    0x201c: '"', 0x201d: '"', 0x201e: '"', 0x201f: '"',
    0x00a0: ' ', 0x2007: ' ', 0x202f: ' ', 0x2009: ' ',
    0x2026: '...',
}


def _fold(line: str) -> str:
    """Normalise a line for the fuzzy second pass: fold punctuation, drop trailing space.

    Deliberately does NOT touch leading whitespace or collapse internal runs of it. This
    patches Python, where indentation is syntax: a fold that let 4 spaces match 8 would
    happily splice a hunk into the wrong block.
    """
    return line.translate(_PUNCT_FOLD).rstrip()


class PatchError(Exception):
    """A patch that could not be parsed or could not be applied. Message goes to the model."""


class _Hunk:
    """One `@@` block: an ordered list of (kind, text) with kind in ' ', '-', '+'."""

    def __init__(self, anchor: str):
        self.anchor = anchor
        self.lines = []

    def before(self):
        """The lines this hunk expects to find on disk, in order (context + removed)."""
        return [text for kind, text in self.lines if kind in ' -']

    def counts(self):
        added = sum(1 for kind, _ in self.lines if kind == '+')
        removed = sum(1 for kind, _ in self.lines if kind == '-')
        return added, removed


class _FileOp:
    def __init__(self, action: str, path: str):
        self.action = action          # 'update' | 'add' | 'delete'
        self.path = path
        self.move_to = None
        self.hunks = []


def parse_patch(text: str):
    """Parse V4A patch text into a list of _FileOp. Raises PatchError."""
    if not text or not text.strip():
        raise PatchError('empty patch')

    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    # split('\n') on text ending in a newline leaves a final '' that is the end of the
    # last line, not a line of its own. Inside a hunk an empty string parses as a blank
    # CONTEXT line (see the blank-line note below), so leaving it there appends a
    # phantom '' to the last hunk's expected block and the hunk then matches nothing.
    # Invisible when the '*** End Patch' marker is present, because the envelope slice
    # below cuts it off anyway -- it only bites on a patch that omits the marker.
    if lines and lines[-1] == '':
        lines.pop()

    # Take what is between the envelope markers when they are present, but do not insist
    # on them. The heredoc the model writes sometimes picks up a stray blank line or a
    # shell prompt echo at one end, and rejecting an otherwise valid patch over that is
    # a round trip spent on nothing.
    start = next((i for i, l in enumerate(lines) if l.strip() == '*** Begin Patch'), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == '*** End Patch'), None)
    if start is not None:
        lines = lines[start + 1:end if end is not None and end > start else len(lines)]
    elif not any(l.startswith('*** ') for l in lines):
        raise PatchError(
            'not a patch: expected a "*** Begin Patch" envelope or at least one '
            '"*** Update File: <path>" header')

    ops = []
    hunk = None

    for raw in lines:
        line = raw.rstrip('\n')
        stripped = line.strip()

        if stripped.startswith('*** '):
            hunk = None
            directive = stripped[4:]
            for prefix, action in (('Update File:', 'update'),
                                   ('Add File:', 'add'),
                                   ('Delete File:', 'delete')):
                if directive.startswith(prefix):
                    ops.append(_FileOp(action, directive[len(prefix):].strip()))
                    break
            else:
                if directive.startswith('Move to:'):
                    if not ops:
                        raise PatchError('"*** Move to:" with no preceding file header')
                    ops[-1].move_to = directive[len('Move to:'):].strip()
                elif stripped in ('*** Begin Patch', '*** End Patch'):
                    pass
                else:
                    raise PatchError(f'unrecognised directive: {stripped!r}')
            continue

        if not ops:
            # Prose before the first header: the model narrating. Ignore it.
            continue

        op = ops[-1]

        if line.startswith('@@'):
            hunk = _Hunk(line[2:].strip())
            op.hunks.append(hunk)
            continue

        if op.action == 'add':
            # An Add File body is all '+' lines; tolerate a model that forgets the sign.
            if not op.hunks:
                op.hunks.append(_Hunk(''))
            op.hunks[0].lines.append(('+', line[1:] if line.startswith('+') else line))
            continue

        if op.action == 'delete':
            continue

        if hunk is None:
            # Body lines before any '@@'. Every corpus patch opens its hunk with '@@',
            # but the marker is redundant when there is only one hunk and a model that
            # omits it means exactly what a model that includes it means.
            hunk = _Hunk('')
            op.hunks.append(hunk)

        if line.startswith('+'):
            hunk.lines.append(('+', line[1:]))
        elif line.startswith('-'):
            hunk.lines.append(('-', line[1:]))
        elif line.startswith(' '):
            hunk.lines.append((' ', line[1:]))
        elif line == '':
            # A blank line inside a hunk is a context line whose content is empty -- the
            # ' ' that would mark it is itself stripped by every editor and by the model.
            hunk.lines.append((' ', ''))
        else:
            raise PatchError(
                f'line in a hunk has no +/-/space prefix: {line!r} '
                '(every line inside an @@ block must start with "+", "-" or a space)')

    if not ops:
        raise PatchError('patch contains no file headers ("*** Update File: <path>")')
    return ops


def _find_all(haystack, needle, key=lambda s: s):
    """Every index where `needle` occurs as a contiguous run of lines in `haystack`."""
    if not needle:
        return []
    hay = [key(l) for l in haystack]
    ned = [key(l) for l in needle]
    n = len(ned)
    return [i for i in range(len(hay) - n + 1) if hay[i:i + n] == ned]


def _locate(file_lines, hunk, path, index):
    """Index in file_lines where this hunk's `before` block starts. Raises PatchError.

    Exact match first, punctuation-folded match second, and an ambiguous result is an
    error either way -- see the module docstring on failing closed.
    """
    before = hunk.before()

    if not before:
        # Nothing to match on: a pure insertion. Only locatable if the `@@` carried an
        # anchor line, which nothing in the corpus does, so this is a clear error rather
        # than a guess at where the model meant.
        if hunk.anchor:
            anchors = _find_all(file_lines, [hunk.anchor], _fold)
            if len(anchors) == 1:
                return anchors[0] + 1
        raise PatchError(
            f'hunk {index} of {path} adds lines but has no context or removed lines to '
            'position them by. Give the hunk at least one unchanged line of context '
            '(prefixed with a space) or one "-" line, or use write_file.')

    for key, how in ((lambda s: s, 'exact'), (_fold, 'fuzzy')):
        matches = _find_all(file_lines, before, key)

        # An `@@ <text>` anchor names the enclosing scope; matches below it are the ones
        # the model meant. Used only to break a tie -- never to turn a found match into a
        # not-found one, so a wrong anchor cannot fail an otherwise applicable hunk.
        if len(matches) > 1 and hunk.anchor:
            anchors = _find_all(file_lines, [hunk.anchor], _fold)
            if anchors:
                narrowed = [m for m in matches if m >= anchors[0]]
                if len(narrowed) == 1:
                    matches = narrowed

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise PatchError(
                f'hunk {index} of {path} matches in {len(matches)} places '
                f'(lines {", ".join(str(m + 1) for m in matches)}) -- it is ambiguous, so '
                'nothing was written. Include more surrounding context in the hunk to '
                'pin down which one you mean.')
        if how == 'fuzzy':
            first = before[0]
            raise PatchError(
                f'hunk {index} of {path} does not match the file: no run of lines there '
                f'starts with {first!r}. The file on disk is not what the hunk expects -- '
                'read_file it again and rebuild the hunk from the current contents, or '
                'use write_file with the complete new file.')


def _apply_hunks(file_lines, hunks, path):
    """Return the new list of lines. Raises PatchError without touching disk."""
    out = list(file_lines)
    for index, hunk in enumerate(hunks, 1):
        start = _locate(out, hunk, path, index)
        before = hunk.before()
        replacement = []
        cursor = start
        for kind, text in hunk.lines:
            if kind == ' ':
                # Take the line from the FILE, not from the patch. Under a fuzzy match
                # the model's retyped context differs from disk (that is what made it
                # fuzzy), and writing its version back would silently rewrite unrelated
                # characters -- turning an ASCII hyphen in a comment into U+2011 because
                # the model quoted it that way.
                replacement.append(out[cursor])
                cursor += 1
            elif kind == '-':
                cursor += 1
            else:
                replacement.append(text)
        out[start:start + len(before)] = replacement
    return out


def apply_patch(patch: str) -> str:
    """Apply V4A patch text. Returns a summary; raises PatchError having written nothing.

    Every file's new contents are computed first and written only once the whole patch
    has resolved, so a patch that touches two files cannot leave one edited and the
    other not.
    """
    ops = parse_patch(patch)

    pending = []     # (op, resolved_path, new_text_or_None)
    summary = []

    for op in ops:
        path = os.path.abspath(os.path.expanduser(op.path))

        if op.action == 'delete':
            if not os.path.exists(path):
                raise PatchError(f'cannot delete {path}: no such file')
            pending.append((op, path, None))
            summary.append(f'deleted {path}')
            continue

        if op.action == 'add':
            if os.path.exists(path):
                raise PatchError(
                    f'cannot add {path}: it already exists. Use "*** Update File:" to '
                    'change a file that is already there.')
            body = [text for hunk in op.hunks for _, text in hunk.lines]
            text = '\n'.join(body)
            if text and not text.endswith('\n'):
                text += '\n'
            pending.append((op, path, text))
            summary.append(f'added {path} ({len(body)} lines)')
            continue

        if not os.path.isfile(path):
            raise PatchError(
                f'cannot update {path}: no such file. Check the path -- a bare relative '
                f'path resolves against {os.getcwd()}.')
        if not op.hunks:
            raise PatchError(f'"*** Update File: {op.path}" has no @@ hunks after it')

        with open(path) as f:
            original = f.read()
        # splitlines() then rejoin, so a file with no trailing newline keeps not having
        # one and a file that has one keeps exactly one.
        lines = original.splitlines()
        new_lines = _apply_hunks(lines, op.hunks, path)

        added = sum(h.counts()[0] for h in op.hunks)
        removed = sum(h.counts()[1] for h in op.hunks)
        new_text = '\n'.join(new_lines)
        if original.endswith('\n') or not original:
            new_text += '\n'

        if new_text == original:
            # Not an error -- the corpus contains a hunk whose '-' and '+' lines are
            # identical -- but the model must hear it, because monitor.py will score this
            # revision as having changed nothing and discard it.
            summary.append(f'{path}: patch applied but the file is byte-identical '
                           f'(the hunks were no-ops); NOTHING CHANGED')
        else:
            summary.append(f'updated {path} ({len(op.hunks)} hunk(s), '
                           f'+{added} -{removed} lines)')
        pending.append((op, path, new_text))

    # Everything resolved. Now write.
    for op, path, text in pending:
        if text is None:
            os.remove(path)
            continue
        with open(path, 'w') as f:
            f.write(text)
        if op.move_to:
            dest = os.path.abspath(os.path.expanduser(op.move_to))
            os.rename(path, dest)
            summary.append(f'moved {path} -> {dest}')

    return '\n'.join(summary)


def main(argv=None) -> int:
    """CLI entry point: read the patch on stdin, the way the models invoke it.

    All eight logged attempts were `apply_patch <<'PATCH' ... PATCH` through the `exec`
    tool, not a tool call, so this half is the one that catches the reflex. Exits 1 on
    failure so `exec` reports a non-zero exit code to the model alongside the message.
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('-h', '--help'):
        print('usage: apply_patch < patch    (V4A "*** Begin Patch" format, on stdin)')
        return 0
    try:
        print(apply_patch(sys.stdin.read()))
    except PatchError as e:
        print(f'apply_patch: {e}', file=sys.stderr)
        return 1
    except Exception as e:                                    # noqa: BLE001
        print(f'apply_patch: {type(e).__name__}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
