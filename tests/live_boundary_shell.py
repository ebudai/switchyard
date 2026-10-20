"""What a shell script would run, as far as the test boundary can know without running it (SYRD-627).

`tests/live_boundary.py` refuses a write under a live root before the command
that would make it runs. A script reaches its writes through the shell, so its
commands are read here the way a shell reads them:
- split at separators outside quotes, `$(...)` and backticks;
- heredoc bodies kept as data, unless a shell reads them or the shell expands
  them;
- `if`, `while`, `case`, groups and functions read past their keywords;
- variables expanded from what the script and its environment assign;
- command substitutions run as nested scripts, and the few whose output can be
  known (mktemp, `cd X && pwd`, dirname ...) evaluated;
- sourced files and eval read inline; functions read when called, and traps read
  at the end, with every value their variables were ever given.

A value the reader cannot know is None. The guard refuses a write to such a
target: it fails closed rather than let an unread value reach a live root.
"""

from __future__ import annotations

import itertools
import os
import pwd
import re
import shlex
import shutil
from typing import Callable

#: Words a shell reads before the command itself: reserved words, and what opens a group.
KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{", "(", "time"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
DECLARATIONS = frozenset({"export", "local", "readonly", "declare", "typeset"})
#: How deep scripts within scripts are read; the guard refuses anything deeper rather than run it unread.
DEPTH = 8
#: More candidate values than this for one word, and the word is taken as unknowable.
CANDIDATES = 32

ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_PARAMETER = re.compile(r"\$\{(?P<braced>[A-Za-z_]\w*(?:\[0\])?|[0-9@*#?$!])(?P<rest>[^}]*)\}"
                        r"|\$(?P<bare>[A-Za-z_]\w*|[0-9@*#?$!])")
#: Commands that only drop or alter lines of the files they read.
_FILTERS = frozenset({"sed", "cat", "grep", "head", "tail", "cut", "tr"})
_FUNCTION_HEAD = re.compile(r"^([A-Za-z_][\w.:-]*)\(\)$")


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


def closing(text: str, start: int) -> int:
    """The index of the `)` closing a `$(` whose body starts at `start` (or the end of `text`)."""
    depth, quote, index = 1, "", start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return index
        index += 1
    return len(text)


def spans(text: str) -> list[tuple[int, int, str]]:
    """Where the command substitutions a shell would run in `text` are -- `$(...)` and backticks, outside single
    quotes (inside double quotes too) -- as (start, end, body). Nested ones are found when each body is read."""
    found, index, single, double = [], 0, False, False
    while index < len(text):
        char = text[index]
        if single:
            single = char != "'"
        elif char == "\\":
            index += 1
        elif char == "'" and not double:
            single = True
        elif char == '"':
            double = not double
        elif text.startswith("$((", index):
            index += 2  # arithmetic; a substitution inside it is still found
        elif text.startswith("$(", index):
            end = closing(text, index + 2)
            found.append((index, end + 1, text[index + 2:end]))
            index = end
        elif char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            found.append((index, end + 1, text[index + 1:end].replace("\\`", "`")))
            index = end
        index += 1
    return found


def substitutions(text: str) -> list[str]:
    """The bodies of the command substitutions a shell would run in `text`."""
    return [body for _start, _end, body in spans(text)]


def split(text: str) -> list[str]:
    """`text` split into words as the shell splits them, quotes removed -- a command substitution kept whole, as
    written, inside the word it is part of."""
    found = spans(text)
    protected, last = [], 0
    for number, (start, end, _body) in enumerate(found):
        protected += [text[last:start], f"\x00{number}\x00"]
        last = end
    protected.append(text[last:])
    words = shlex.split("".join(protected))
    return [re.sub(r"\x00(\d+)\x00", lambda m: text[found[int(m.group(1))][0]:found[int(m.group(1))][1]], word)
            for word in words]


def commands(script: str) -> list[tuple[list[str], str | None, bool, str]]:
    """A script's simple commands, in order: their words, the body of a heredoc if one has one, whether that
    heredoc's delimiter was quoted (its body then is not expanded), and the command's own text.

    Separators (`;`, `&`, `|`, `&&`, `||`, newlines) split commands only outside quotes, `$(...)` and backticks; a
    backslash-newline continues a line; comments are dropped; a heredoc's body is not read as commands. Text the
    shell itself could not parse is not a command."""
    found: list[list] = []
    current: list[str] = []
    pending: list[tuple[int, str, bool, bool]] = []
    quote, depth, tick, index, size = "", 0, False, 0, len(script)
    test = ""  # inside [[ ... ]] or (( ... )), where && and || are operators: the closing delimiter

    def finish() -> None:
        text = "".join(current).strip()
        current.clear()
        if not text:
            return
        try:
            words = split(text)
        except ValueError:
            return
        quoted = bool(re.search(r"<<-?\s*['\"\\]", text))
        kept, at = [], 0
        while at < len(words):
            word = words[at]
            if word.startswith("<<") and not word.startswith("<<<"):
                marker = word[2:]
                strip = marker.startswith("-")
                marker = marker[1:] if strip else marker
                if not marker and at + 1 < len(words):
                    at += 1
                    marker = words[at]
                pending.append((len(found), marker, strip, quoted))
            else:
                kept.append(word)
            at += 1
        found.append([kept, None, False, text])

    while index < size:
        char = script[index]
        following = script[index + 1] if index + 1 < size else ""
        if quote == "'":
            current.append(char)
            quote = "" if char == "'" else quote
            index += 1
            continue
        if quote == '"':
            current.append(char)
            if char == "\\" and following:
                current.append(following)
                index += 2
                continue
            quote = "" if char == '"' else quote
            index += 1
            continue
        if char == "\\" and following:
            if following != "\n":
                current.extend((char, following))
            index += 2
            continue
        if char in "'\"":
            quote = char
        elif char == "`":
            tick = not tick
        elif char == "$" and following == "(":
            depth += 1
            current.extend((char, following))
            index += 2
            continue
        elif char == ")" and depth:
            depth -= 1
        elif not test and not depth and not tick and script.startswith(("[[", "((" ), index) \
                and (not current or current[-1].isspace() or current[-1] in "!"):
            test = "]]" if char == "[" else "))"
            current.extend((char, following))
            index += 2
            continue
        elif test and script.startswith(test, index):
            test = ""
            current.extend((char, following))
            index += 2
            continue
        elif char == "#" and not depth and not tick and (not current or current[-1].isspace()):
            while index < size and script[index] != "\n":
                index += 1
            continue
        elif char in ";&|\n" and not depth and not tick and not (test and char in "&|"):
            previous = current[-1] if current else ""
            if (char == "&" and (following == ">" or previous in "<>")) or (char == "|" and previous == ">"):
                current.append(char)
                index += 1
                continue
            finish()
            index += 2 if char in "&|" and following == char else 1
            if char == "\n" and pending:
                for slot, marker, strip, quoted in pending:
                    body = []
                    while index < size:
                        end = script.find("\n", index)
                        line = script[index:] if end < 0 else script[index:end]
                        index = size if end < 0 else end + 1
                        if (line.lstrip("\t") if strip else line) == marker:
                            break
                        body.append(line)
                    found[slot][1], found[slot][2] = "\n".join(body), quoted
                pending.clear()
            continue
        current.append(char)
        index += 1
    finish()
    return [(words, body, quoted, raw) for words, body, quoted, raw in found]


# --------------------------------------------------------------------------
# expansion
# --------------------------------------------------------------------------


def _combine(parts: list[list[str]]) -> list[str] | None:
    results = [""]
    for part in parts:
        results = [before + piece for before in results for piece in part]
        if len(results) > CANDIDATES:
            return None
    return results


def substituted(body: str, variables: dict, cwd: str | None) -> list[str] | None:
    """What a command substitution prints, for the few the guard can know without running anything: mktemp (a fresh
    path in its directory), `cd X && pwd`, pwd, dirname, basename, realpath, readlink -f, `command -v` and which.
    None otherwise."""
    here, value = cwd, None
    for command in re.split(r"\s*(?:&&|;)\s*", body.strip()):
        try:
            words = split(command)
        except ValueError:
            return None
        if not words:
            continue
        name, expanded = words[0], [expand(word, variables, here) for word in words[1:]]
        if any(candidates is None or len(candidates) != 1 for candidates in expanded):
            return None
        arguments = [candidates[0] for candidates in expanded]
        operands = [argument for argument in arguments if not argument.startswith("-")]
        if name == "cd" and operands:
            if not operands[0].startswith("/") and here is None:
                return None
            here, value = os.path.normpath(os.path.join(here or "/", operands[0])), None
        elif name == "pwd":
            value = here
        elif name in ("dirname", "basename") and operands:
            value = (os.path.dirname(operands[0]) or ".") if name == "dirname" else os.path.basename(operands[0])
        elif name in ("realpath", "readlink") and operands:
            if name == "readlink" and not {"-f", "-e", "-m"} & set(arguments):
                return None
            if not operands[0].startswith("/") and here is None:
                return None
            value = os.path.normpath(os.path.join(here or "/", operands[0]))
        elif (name == "command" and arguments[:1] == ["-v"] and len(operands) == 1) or (name == "which" and operands):
            value = shutil.which(operands[0], path=_first(variables.get("PATH"))) or ""
        elif name == "mktemp":
            value = _mktemp(arguments, variables, here)
            if value is None:
                return None
        else:
            return None
    return None if value is None else [value]


def _first(candidates: list[str] | None) -> str | None:
    return candidates[0] if candidates else None


def _mktemp(arguments: list[str], variables: dict, here: str | None) -> str | None:
    directory, template, skip = None, None, False
    for index, argument in enumerate(arguments):
        if skip:
            skip = False
        elif argument in ("-p", "--tmpdir") and index + 1 < len(arguments):
            directory, skip = arguments[index + 1], True
        elif argument.startswith("--tmpdir="):
            directory = argument.split("=", 1)[1]
        elif argument == "-t":
            directory = directory or _first(variables.get("TMPDIR")) or "/tmp"
        elif not argument.startswith("-"):
            template = argument
    if template and "/" in template and directory is None:
        directory, template = os.path.dirname(template), os.path.basename(template)
    if directory is None:
        directory = (_first(variables.get("TMPDIR")) or "/tmp") if not template else here
    if directory is None:
        return None
    return os.path.join(directory, (template or "tmp.XXXXXXXXXX").replace("X", "q"))


def expand(word: str, variables: dict, cwd: str | None = None) -> list[str] | None:
    """Every value `word` could take as the shell expands it, from what the script and its environment assigned:
    `~`, `~user`, `$NAME`, `${NAME}`, `${NAME:-default}`, `$1`, `"$@"` and the substitutions `substituted` knows.
    An unset variable is empty, as in the shell. None when a value cannot be known: a variable the script reads at
    run time, an unknown positional parameter, any other substitution, a parameter operator the guard does not
    evaluate."""
    if word.startswith("~"):
        head, slash, tail = word.partition("/")
        if head == "~":
            homes = variables.get("HOME") or []
        else:
            try:
                homes = [pwd.getpwnam(head[1:]).pw_dir]
            except KeyError:
                homes = []
        rest = expand(slash + tail, variables, cwd)
        return None if not homes or rest is None else _combine([homes, rest])
    parts: list[list[str]] = []
    index = 0
    found = sorted([(start, end, "substitution", body) for start, end, body in spans(word)]
                   + [(m.start(), m.end(), "parameter", m) for m in _PARAMETER.finditer(word)
                      if not any(s <= m.start() < e for s, e, _b in spans(word))], key=lambda item: item[0])
    for start, end, kind, item in found:
        if start < index:
            continue
        parts.append([word[index:start]])
        if kind == "substitution":
            value = substituted(item, variables, cwd)
        else:
            value = _parameter(item, variables, cwd)
        if value is None:
            return None
        parts.append(value)
        index = end
    parts.append([word[index:]])
    return _combine(parts)


_UNSET = object()


def _parameter(match: re.Match, variables: dict, cwd: str | None) -> list[str] | None:
    """The values one parameter expansion could give: unset is empty, unknown (None) stays unknown, and the default
    and alternative operators are followed. Other operators (`%`, `#`, `/`, ...) are not evaluated: None."""
    name = match.group("braced") or match.group("bare")
    rest = match.group("rest") or ""
    if name in ("#", "?", "$", "!"):
        return ["0"]  # a number or a length: no path in it
    positional = name in ("@", "*") or name.isdigit()
    value = variables.get(name, None if positional else _UNSET)
    if not rest:
        return [""] if value is _UNSET else value
    operator = re.match(r"^(:?)([-=+?])(.*)$", rest, re.S)
    if not operator:
        return None
    colon, kind, word = operator.groups()
    alternative = expand(word, variables, cwd)
    if value is None or alternative is None:
        return None
    unset = value is _UNSET
    empty = not unset and "" in value
    if kind == "+":
        if unset or (colon and value == [""]):
            return [""]
        return alternative + ([""] if colon and empty else [])
    if kind == "?":
        return [""] if unset else value
    if unset:
        return alternative
    return sorted((set(value) - {""}) | set(alternative)) if colon and empty else value


# --------------------------------------------------------------------------
# reading a script
# --------------------------------------------------------------------------


Check = Callable[[list[str], "str | None", dict, int], list[str]]


class Shell:
    """What one shell process knows while it reads its scripts: every value each variable has been given (None
    once one could not be known), its functions, the functions it calls and its traps."""

    def __init__(self, environ, positional: list[str] | None = None) -> None:
        self.variables: dict[str, list[str] | None] = {key: [value] for key, value in environ.items()}
        self.set_positional(positional)
        self.functions: dict[str, list[tuple[list[str], str | None, bool, str, str | None]]] = {}
        self.called: set[str] = set()
        self.every_function = False
        self.traps: list[tuple[str, str | None]] = []

    def set_positional(self, positional: list[str] | None) -> None:
        for key in ("@", "*", *map(str, range(1, 10))):
            self.variables.pop(key, None)
        if positional is not None:
            self.variables["@"] = list(positional) or [""]
            self.variables["*"] = [" ".join(positional)]
            for number, value in enumerate(positional[:9], start=1):
                self.variables[str(number)] = [value]
            for number in range(len(positional) + 1, 10):
                self.variables[str(number)] = [""]

    def assign(self, name: str, values: list[str] | None) -> None:
        known = self.variables.get(name, [])
        self.variables[name] = None if values is None or known is None else sorted(set(known) | set(values))


class Reader:
    """Reads a script's commands for `check(words, cwd, variables, depth)`, which refuses a command the boundary
    will not let run and returns the names it runs. `read_file(path)` gives a file's text, or None."""

    def __init__(self, check: Check, read_file: Callable[[str], str | None], refuse: Callable[[str], None]) -> None:
        self.check, self.read_file, self.refuse = check, read_file, refuse
        self.calling: list[str] = []  # functions being read at a call, innermost last
        self.read_functions: set[str] = set()

    def script(self, script: str, shell: Shell, cwd: str | None, depth: int, source: str | None = None,
               *, finish: bool = True) -> None:
        if depth >= DEPTH:
            self.refuse("a script nested too deeply to read")
        if source:
            shell.assign("0", [source])
            shell.variables["BASH_SOURCE"] = shell.variables["BASH_SOURCE[0]"] = [source]
        here, groups = cwd, []
        capturing: list[tuple[str, int]] = []  # (function, group depth its body opened at)
        pending: str | None = None
        for words, body, quoted, raw in commands(script):
            words = list(itertools.dropwhile(lambda w: w in KEYWORDS and w not in ("(", "{"), words))
            head = _FUNCTION_HEAD.match(words[0]) if words else None
            if words[:1] == ["function"] and len(words) > 1:
                pending, words = words[1].rstrip("()"), words[2:]
            elif head:
                pending, words = head.group(1), words[1:]
            if pending and words[:1] in (["{"], ["("]):
                capturing.append((pending, len(groups)))
                shell.functions.setdefault(pending, [])
                pending = None
            opened = 0
            while words[:1] in (["("], ["{"]):
                groups.append(here)
                opened += 1
                words = list(itertools.dropwhile(lambda w: w in KEYWORDS and w not in ("(", "{"), words[1:]))
            closes = 0
            while words and words[-1] in (")", "}"):
                closes += 1
                words = words[:-1]
            if capturing and (len(groups) > capturing[-1][1] or opened):
                shell.functions[capturing[-1][0]].append((words, body, quoted, raw, here))
            else:
                here = self._one(words, body, quoted, raw, shell, here, depth)
            for _ in range(closes):
                here = groups.pop() if groups else here
                if capturing and len(groups) <= capturing[-1][1]:
                    capturing.pop()
        if finish:
            self._finish(shell, cwd, depth)

    def _finish(self, shell: Shell, cwd: str | None, depth: int) -> None:
        """Called functions (every function, once a command's name could not be known) and traps, read with every
        value the script ever gave its variables."""
        traps, shell.traps = shell.traps, []
        for text, here in traps:
            self.script(text, shell, here, depth + 1, finish=False)
        while True:
            todo = [name for name in shell.functions if name not in self.read_functions
                    and (shell.every_function or name in shell.called)]
            if not todo:
                break
            for name in todo:
                self._call(name, None, shell, depth)  # reached some other way: its arguments are not known
            if shell.traps:
                traps, shell.traps = shell.traps, []
                for text, here in traps:
                    self.script(text, shell, here, depth + 1, finish=False)

    def _filtered(self, command: str, shell: Shell, here: str | None, depth: int) -> tuple[str | None, str | None]:
        """What `source <(filter FILE...)` sources, for a filter that only drops or alters lines of the files it
        reads (sed, cat, grep, head, tail, cut, tr): those files whole. The filter itself is checked as a command."""
        self.script(command, shell, here, depth + 1, finish=False)
        try:
            words = split(command)
        except ValueError:
            return None, None
        if not words or words[0] not in _FILTERS:
            return None, None
        files = []
        for word in reversed(words[1:]):
            if word.startswith("-"):
                break
            values = expand(word, shell.variables, here)
            if not values or len(values) != 1:
                return None, None
            path = values[0] if values[0].startswith("/") else (os.path.join(here, values[0]) if here else None)
            text = self.read_file(path) if path else None
            if text is None:
                break
            files.insert(0, (path, text))
        if not files:
            return None, None
        return "\n".join(text for _path, text in files), files[0][0]

    def _call(self, name: str, arguments: list[str] | None, shell: Shell, depth: int) -> None:
        """A function's body, read as it runs: with these arguments as its positional parameters (None: unknown).
        A function already being read further up is not read again."""
        if name in self.calling:
            return
        if depth >= DEPTH:
            self.refuse(f"a function nested too deeply to read: {name}")
        self.read_functions.add(name)
        saved = {key: shell.variables.get(key) for key in ("@", "*", *map(str, range(1, 10))) if key in shell.variables}
        shell.set_positional(arguments)
        self.calling.append(name)
        try:
            for words, body, quoted, raw, defined_here in shell.functions[name]:
                self._one(words, body, quoted, raw, shell, defined_here, depth + 1)
        finally:
            self.calling.pop()
            shell.set_positional(None)
            shell.variables.update(saved)

    def _one(self, words: list[str], body: str | None, quoted: bool, raw: str, shell: Shell, here: str | None,
             depth: int) -> str | None:
        """One command; returns where the script is afterwards."""
        for nested in substitutions(raw):
            self.script(nested, shell, here, depth + 1, finish=False)
        if words[:1] == ["case"]:
            words = words[words.index("in") + 1:] if "in" in words else []
            words = list(itertools.dropwhile(lambda w: w.endswith(")") and not w.startswith("$("), words))
        if words and all(ASSIGNMENT.match(w) for w in words):
            for assignment in words:
                key, value = ASSIGNMENT.match(assignment).groups()
                shell.assign(key, expand(value, shell.variables, here))
            return here
        if words[:1] and words[0] in DECLARATIONS:
            for assignment in words[1:]:
                match = ASSIGNMENT.match(assignment)
                if match:
                    shell.assign(match.group(1), expand(match.group(2), shell.variables, here))
            return here
        if words[:1] == ["for"] and len(words) > 1:
            items = words[words.index("in") + 1:] if "in" in words else None
            values = None if items is None else _combine_all([expand(item, shell.variables, here) for item in items])
            shell.assign(words[1], values)
            return here
        if words[:1] in (["read"], ["select"]) and len(words) > 1:
            for key in [w for w in words[1:] if not w.startswith("-")]:
                shell.assign(key, None)  # a value the script takes at run time: unknown
            return here
        if words[:1] == ["cd"]:
            targets = expand(words[1], shell.variables, here) if len(words) > 1 else shell.variables.get("HOME")
            if not targets or len(targets) != 1 or (here is None and not targets[0].startswith("/")):
                return None  # where later relative paths lead is no longer known
            return os.path.normpath(os.path.join(here or "/", targets[0]))
        if words[:1] == ["trap"] and len(words) > 2:
            shell.traps.append((words[1], here))
            return here
        if words[:1] == ["eval"]:
            self.script(" ".join(words[1:]), shell, here, depth + 1, finish=False)
            return here
        if words[:1] in (["."], ["source"]) and len(words) > 1:
            path = expand(words[1], shell.variables, here) if not words[1].startswith("<(") else None
            text = resolved = None
            if words[1].startswith("<(") and words[1].endswith(")"):
                text, resolved = self._filtered(words[1][2:-1], shell, here, depth)
            elif path and len(path) == 1:
                resolved = path[0] if path[0].startswith("/") else (os.path.join(here, path[0]) if here else None)
                text = self.read_file(resolved) if resolved else None
            if text is None:
                self.refuse(f"a sourced file the guard cannot read: {words[1]}")
                return here
            self.script(text, shell, here, depth + 1, source=resolved, finish=False)
            return here
        if words:
            names = self.check(words, here, shell.variables, depth)
            for name in names:
                if name in shell.functions:
                    shell.called.add(name)
            program = expand(words[0], shell.variables, here) if not ASSIGNMENT.match(words[0]) else [""]
            if program is None and words[0] not in ('"$@"', "$@", "$*", "${@}"):
                shell.every_function = True  # a command named by a value unknown here may be any function
            if words[0] in shell.functions:
                arguments = [expand(word, shell.variables, here) for word in words[1:]]
                known = None if any(v is None or len(v) != 1 for v in arguments) else [v[0] for v in arguments]
                self._call(words[0], known, shell, depth)
                if len(words) > 1:
                    # A function may run its arguments ("$@"): they are checked as a command too.
                    self.check(words[1:], here, shell.variables, depth)
        if body is not None:
            reader = [w for w in words if not ASSIGNMENT.match(w)]
            if reader and os.path.basename(reader[0]) in SHELLS and "-c" not in reader:
                self.script(body, shell, here, depth + 1, finish=False)
            elif not quoted:
                for nested in substitutions(body):
                    self.script(nested, shell, here, depth + 1, finish=False)
        return here


def _combine_all(candidates: list[list[str] | None]) -> list[str] | None:
    if any(values is None for values in candidates):
        return None
    return sorted({value for values in candidates for value in values}) or [""]
