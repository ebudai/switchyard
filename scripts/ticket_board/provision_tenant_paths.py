"""The packet lines that change paths a tenant controls: one shape, through `scripts/tenant_paths.py`.

Every generated root operation on a directory or file inside a tenant's home,
or a role account's home, is written as

    sudo /usr/bin/python3 -I "$switchyard_tenant_paths" --root '<home>' <op> ...

and never as `install -d`, `setfacl`, `find -exec`, `chown` or `chmod` on the
path, which all act on whatever a link there points at (SYRD-605). The packet
defines the variable once, at its top, as the helper inside the release it was
rendered from -- the same release whose scripts it already runs as root -- and
`repair-boundary` recognises the shape and runs the same helper in-process.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

TENANT_PATHS_VARIABLE = "switchyard_tenant_paths"
TENANT_PATHS_PREFIX = f'sudo /usr/bin/python3 -I "${TENANT_PATHS_VARIABLE}" '


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def tenant_paths_definition(source_repo: str) -> str:
    """The packet line that names the helper the rest of the packet runs."""
    helper = str(PurePosixPath(str(source_repo)) / "scripts" / "tenant_paths.py")
    return f"{TENANT_PATHS_VARIABLE}={_quote(helper)}"


def _inside(root: str, path: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(root))
    except ValueError:
        return False
    return True


def tenant_path_line(root: str, operation: str, *arguments: str, paths: tuple[str, ...] = ()) -> str:
    """The helper invocation(s) for these paths, each rooted at `root` when it is inside it.

    A path elsewhere -- a checkout, worktree base or control repository kept
    outside the home -- is its own root. That is as safe: the helper reaches
    any root from `/` by descriptor and refuses a link nobody but root made.
    """
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(str(root) if _inside(root, path) else str(path), []).append(str(path))
    return "\n".join(
        TENANT_PATHS_PREFIX + " ".join(_quote(word) for word in ["--root", anchor, operation, *arguments, *members])
        for anchor, members in groups.items()
    )


def directory_line(root: str, mode: str, owner: str, group: str, *paths: str) -> str:
    return tenant_path_line(root, "dir", "--mode", mode, "--owner", owner, "--group", group, paths=paths)


def acl_line(root: str, spec: str, *paths: str, remove: bool = False, recursive: bool = False,
             directories_only: bool = False, if_exists: bool = False, if_group: str = "") -> str:
    arguments = ["--remove" if remove else "--modify", spec]
    if recursive:
        arguments.append("--recursive")
    if directories_only:
        arguments.append("--directories-only")
    if if_exists:
        arguments.append("--if-exists")
    if if_group:
        arguments.extend(["--if-group", if_group])
    return tenant_path_line(root, "acl", *arguments, paths=paths)


def attrs_line(root: str, *paths: str, mode: str = "", owner: str = "", group: str = "",
               if_exists: bool = False) -> str:
    arguments: list[str] = []
    if mode:
        arguments.extend(["--mode", mode])
    if owner:
        arguments.extend(["--owner", owner])
    if group:
        arguments.extend(["--group", group])
    if if_exists:
        arguments.append("--if-exists")
    return tenant_path_line(root, "attrs", *arguments, paths=paths)


def close_children_line(root: str, path: str) -> str:
    return tenant_path_line(root, "close-children", paths=(path,))


def chown_tree_line(root: str, owner: str, path: str, *, if_exists: bool = False) -> str:
    arguments = ["--owner", owner, *(["--if-exists"] if if_exists else [])]
    return tenant_path_line(root, "chown-tree", *arguments, paths=(path,))


def install_line(root: str, mode: str, owner: str, group: str, source: str, path: str) -> str:
    """`source` is root's staged file, written as the packet spells it (it may be a shell expression)."""
    if not _inside(root, path):
        root = str(PurePosixPath(path).parent)
    return (
        TENANT_PATHS_PREFIX
        + " ".join(_quote(word) for word in ("--root", root, "install", "--mode", mode, "--owner", owner,
                                              "--group", group))
        + f" {source} {_quote(path)}"
    )


def helper_path() -> str:
    """The helper in the release this code runs from -- what root already trusts to execute."""
    return str(Path(__file__).resolve().parents[1] / "tenant_paths.py")


def tenant_paths_argv(root: str, operation: str, *arguments: str) -> list[str]:
    """The helper as an argv, for root code that runs it directly rather than through a packet."""
    return [sys.executable or "/usr/bin/python3", "-I", helper_path(), "--root", str(root), operation,
            *(str(argument) for argument in arguments)]
