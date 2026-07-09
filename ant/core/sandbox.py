"""Security sandbox for agent tools — filesystem, command, and network isolation.

The sandbox is the primary security boundary between an agent and the host.
It is NOT a decorator or wrapper — each tool explicitly calls the relevant
validate_* method before performing its operation.  Violations raise
SandboxViolation, which ToolRegistry catches and returns as a clean error
string (no crash, no stack trace leaked to the LLM).

Design: layered defence
  1. PathSandbox   — restrict file read/write/edit/ingest to allowed directories
  2. CommandSandbox — restrict shell commands (dangerous patterns, timeout, output)
  3. NetworkSandbox — restrict outbound HTTP(S) (SSRF prevention, domain control)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class SandboxViolation(Exception):
    """Raised when a sandbox check blocks an operation.

    Caught by ToolRegistry and returned as an error string to the LLM —
    never allowed to propagate as an unhandled exception.
    """

    def __init__(
        self,
        message: str,
        violation_type: str = "generic",
        detail: str = "",
    ) -> None:
        self.violation_type = violation_type  # "path" | "command" | "network"
        self.detail = detail
        super().__init__(message)


# ---------------------------------------------------------------------------
# Path sandbox
# ---------------------------------------------------------------------------

# Glob patterns that are always blocked inside allowed directories.
# The default set protects configuration files (API keys), internal state,
# and long-term memory stores from accidental or malicious access by tools.
_DEFAULT_BLOCKED_GLOBS: list[str] = [
    "config.user.yaml",
    "config.runtime.yaml",
    ".history/**",
    ".events/**",
    ".memory/**",
    ".logs/**",
    "**/*.key",
    "**/*.pem",
    "**/.env*",
]


class PathSandbox:
    """Restrict file read/write/edit/ingest to a set of allowed directories."""

    def __init__(self, config: "SandboxConfig", workspace: Path) -> None:  # noqa: F821
        self._workspace = workspace.resolve()
        self._enabled = config.path.enabled and config.enabled
        self._allow_all = config.path.allow_all

        # Build allowed roots: workspace + any extras from config
        allowed: list[Path] = [self._workspace]
        for raw in config.path.allowed_dirs:
            p = Path(raw)
            if not p.is_absolute():
                p = self._workspace / p
            allowed.append(p.resolve())
        self._allowed_dirs = allowed

        # Blocked globs: user overrides or built-in defaults
        if config.path.blocked_patterns is not None:
            self._blocked_globs = list(config.path.blocked_patterns)
        else:
            self._blocked_globs = list(_DEFAULT_BLOCKED_GLOBS)

    # -- public API ----------------------------------------------------------

    def validate_read(self, path: str) -> None:
        """Raise SandboxViolation if *path* may not be read."""
        self._validate(path, "read")

    def validate_write(self, path: str) -> None:
        """Raise SandboxViolation if *path* may not be written."""
        self._validate(path, "write")

    def validate_ingest(self, path: str) -> None:
        """Raise SandboxViolation if *path* may not be ingested into memory."""
        self._validate(path, "ingest")

    # -- internals -----------------------------------------------------------

    def _validate(self, raw: str, operation: str) -> None:
        """Core check shared by all path-validating methods."""
        if not self._enabled or self._allow_all:
            return

        try:
            resolved = Path(raw).resolve()
        except (OSError, ValueError) as exc:
            raise SandboxViolation(
                f"Cannot resolve path: {raw}",
                violation_type="path",
                detail=str(exc),
            )

        # 1. Must be inside an allowed directory
        if not any(self._is_subpath(resolved, root) for root in self._allowed_dirs):
            raise SandboxViolation(
                f"Access denied: '{raw}' resolves outside allowed directories. "
                f"Only paths within the workspace are permitted.",
                violation_type="path",
                detail=f"resolved={resolved}",
            )

        # 2. Must not match any blocked glob
        for glob in self._blocked_globs:
            # Try matching against the resolved path relative to each root
            for root in self._allowed_dirs:
                try:
                    rel = resolved.relative_to(root)
                except ValueError:
                    continue
                if self._glob_match(rel, glob):
                    raise SandboxViolation(
                        f"Access denied: '{raw}' matches blocked pattern '{glob}'. "
                        f"This path contains sensitive or internal data.",
                        violation_type="path",
                        detail=f"resolved={resolved}, glob={glob}",
                    )
                # Also check the resolved path against the glob directly
                # This catches patterns like ".history/**" when the path
                # isn't relative to workspace (e.g. allowed_dirs extras)
                if self._glob_match(resolved, f"**/{glob}"):
                    raise SandboxViolation(
                        f"Access denied: '{raw}' matches blocked pattern '{glob}'.",
                        violation_type="path",
                        detail=f"resolved={resolved}, glob={glob}",
                    )

    @staticmethod
    def _is_subpath(child: Path, parent: Path) -> bool:
        """True when *child* is equal to or a descendant of *parent*."""
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _glob_match(path: Path, pattern: str) -> bool:
        """Match a path against a glob pattern using fnmatch on string parts.

        Supports ** for recursive matching, e.g. '.history/**' matches
        '.history/foo/bar.json'.
        """
        # Use Path.match which supports basic glob but not ** recursively.
        # fnmatch on the string representation handles ** correctly when
        # we use the right pattern format.
        path_str = str(path).replace("\\", "/")
        return fnmatch(path_str, pattern)


# ---------------------------------------------------------------------------
# Command sandbox
# ---------------------------------------------------------------------------

# Regex patterns that block shell commands.  Compiled at import time so
# the cost is paid once.  All matching is case-insensitive.
_DEFAULT_BLOCKED_COMMAND_PATTERNS: list[str] = [
    # Unix / cross-platform dangerous patterns
    r"rm\s+(-rf?|--recursive)\s+/",          # rm -rf /
    r"sudo\s+",                                # privilege escalation
    r"chmod\s+777",                            # world-writable permissions
    r"chown\s+",                               # ownership changes
    r"dd\s+if=",                               # raw disk writes
    r"mkfs\.",                                 # filesystem creation
    r":\(\)\s*\{",                             # fork bomb
    r"\|\s*(sh|bash|zsh|pwsh|cmd)\b",          # pipe to shell
    r"curl\s+.*\|",                            # curl pipe (often curl | sh)
    r"wget\s+.*\|",                            # wget pipe
    r">\s*/dev/sda",                           # overwrite block device
    r"apt(-get)?\s+(install|remove|purge)",    # system package manager
    r"yum\s+(install|remove)",                 # RHEL package manager
    r"pip\s+(install|uninstall)",              # Python packages
    r"npm\s+(install|publish|run\s+.*:)",      # Node packages
    # Windows-specific dangerous patterns
    r"del\s+/[fF]\s+/[sS]",                    # del /f /s (force recursive delete)
    r"format\s+[cCdD]:",                       # format C: / D:
    r"netsh\s+",                               # network config changes
    r"reg\s+delete",                           # registry deletion
    r"rmdir\s+/[sS]",                          # rmdir /s (recursive delete)
]


class CommandSandbox:
    """Restrict shell command execution.

    Four layers of protection:
    1. Command pattern validation (dangerous commands blocked)
    2. File-path validation (shell cannot bypass PathSandbox via cat/head/etc.)
    3. Execution timeout (runaway processes killed)
    4. Output size truncation (response flooding prevented)
    """

    # Commands whose file arguments should be validated against PathSandbox.
    # Without this, an agent could use `cat config.user.yaml` to bypass the
    # file-tool restrictions entirely.
    _FILE_READING_COMMANDS: set[str] = {
        "cat", "head", "tail", "less", "more", "wc",
        "grep", "awk", "sed", "cut", "sort", "uniq",
        "find", "ls", "file", "stat", "readlink",
        "cp", "mv", "ln",
        # Windows equivalents
        "type", "findstr", "dir",
    }

    # Regex to extract file-path candidates from a command string.
    # Matches tokens that look like paths (contain /, \, or common extensions)
    # and are not obviously flags (starting with -).
    _PATH_CANDIDATE_RE: re.Pattern[str] = re.compile(
        r"""(?x)
        (?<=[\s"'])      # preceded by whitespace or quote
        (?!-)            # not a flag
        (                # capture the path candidate
            [/.\w-]*     # path characters
            (?:/|\.[a-z]{1,6})  # path separator or common extension
            [/.\w-]*     # rest of path
        )
        (?=[\s"']|$)     # followed by whitespace, quote, or end
        """
    )

    def __init__(
        self,
        config: "SandboxConfig",       # noqa: F821
        workspace: Path,
        path_sandbox: "PathSandbox | None" = None,  # noqa: F821
    ) -> None:
        self._enabled = config.command.enabled and config.enabled
        self._timeout = config.command.default_timeout
        self._max_output = config.command.max_output_size
        self._working_dir = str(workspace.resolve())

        if config.command.working_dir is not None:
            wd = Path(config.command.working_dir)
            if not wd.is_absolute():
                wd = workspace / wd
            self._working_dir = str(wd.resolve())

        # Compile blocked patterns
        raw_patterns: list[str]
        if config.command.blocked_patterns is not None:
            raw_patterns = list(config.command.blocked_patterns)
        else:
            raw_patterns = list(_DEFAULT_BLOCKED_COMMAND_PATTERNS)

        self._blocked: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in raw_patterns
        ]
        self._allowed: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in config.command.allowed_patterns
        ]

        # Path sandbox reference for file-argument validation.
        # When set, file path candidates in commands are validated against it
        # to prevent bypasses like 'cat config.user.yaml'.
        self._path_sandbox = path_sandbox

    # -- public API ----------------------------------------------------------

    @property
    def timeout(self) -> int:
        return self._timeout

    @property
    def working_dir(self) -> str:
        return self._working_dir

    def validate_command(self, command: str) -> None:
        """Raise SandboxViolation if *command* is dangerous or accesses blocked files."""
        if not self._enabled:
            return

        # Allowed patterns override blocked (explicit allowlist)
        for pattern in self._allowed:
            if pattern.search(command):
                return  # explicitly allowed

        for pattern in self._blocked:
            if pattern.search(command):
                raise SandboxViolation(
                    f"Command blocked by safety policy: matched pattern '{pattern.pattern}'",
                    violation_type="command",
                    detail=command[:200],
                )

        # Validate file path arguments against PathSandbox.
        # This prevents shell from being used as an escape hatch to read
        # blocked files (e.g. 'cat config.user.yaml' when read_file is denied).
        if self._path_sandbox is not None:
            self._validate_file_args(command)

    def _validate_file_args(self, command: str) -> None:
        """Extract file path candidates from *command* and validate each.

        This is a best-effort heuristic — shell command parsing is inherently
        ambiguous.  It catches the common bypass patterns without claiming
        to be a full parser.
        """
        candidates = self._PATH_CANDIDATE_RE.findall(command)
        for candidate in candidates:
            # Skip candidates that are clearly shell commands, not paths
            if candidate.lower() in self._FILE_READING_COMMANDS:
                continue
            # Only validate if it looks like a real path (has /, \, or .)
            if "/" not in candidate and "\\" not in candidate and "." not in candidate:
                continue
            try:
                # Resolve relative to the bash working directory
                p = Path(candidate)
                if not p.is_absolute():
                    p = Path(self._working_dir) / p
                self._path_sandbox._validate(str(p), "read")
            except SandboxViolation:
                raise SandboxViolation(
                    f"Command blocked: '{candidate}' in '{command[:80]}...' "
                    f"would access a restricted file. Use approved tools instead.",
                    violation_type="command",
                    detail=command[:200],
                )

    def validate_output(self, output: str) -> str:
        """Truncate output to max_output_size characters.

        Returns the original string if within limits, otherwise a truncated
        version with a size notice appended.
        """
        if not self._enabled or len(output) <= self._max_output:
            return output

        truncated = output[: self._max_output]
        return (
            f"{truncated}\n\n"
            f"[Truncated — original output was {len(output):,} chars, "
            f"limit is {self._max_output:,} chars]"
        )


# ---------------------------------------------------------------------------
# Network sandbox
# ---------------------------------------------------------------------------

class NetworkSandbox:
    """Restrict outbound HTTP(S) requests.

    Primary goal: prevent Server-Side Request Forgery (SSRF) and
    access to internal/private network resources.
    """

    def __init__(self, config: "SandboxConfig") -> None:  # noqa: F821
        self._enabled = config.network.enabled and config.enabled
        self._block_private = config.network.block_private_ips
        self._block_file = config.network.block_file_urls
        self._allowed_domains = list(config.network.allowed_domains)
        self._denied_domains = list(config.network.denied_domains)

    # -- public API ----------------------------------------------------------

    def validate_url(self, url: str) -> None:
        """Raise SandboxViolation if *url* is not allowed."""
        if not self._enabled:
            return

        try:
            parsed = urlparse(url)
        except Exception as exc:
            raise SandboxViolation(
                f"Cannot parse URL: {url}",
                violation_type="network",
                detail=str(exc),
            )

        # Scheme check: only http/https
        if parsed.scheme not in ("http", "https"):
            raise SandboxViolation(
                f"URL scheme '{parsed.scheme}' is not allowed. Only http:// and https:// are permitted.",
                violation_type="network",
                detail=url,
            )

        # File URL block (explicit check even though scheme check catches most)
        if self._block_file and parsed.scheme == "file":
            raise SandboxViolation(
                "file:// URLs are not allowed.",
                violation_type="network",
                detail=url,
            )

        host = (parsed.hostname or "").lower()
        if not host:
            raise SandboxViolation(
                f"URL has no hostname: {url}",
                violation_type="network",
            )

        # Domain denylist check (before allowlist — denied domains are always blocked)
        if self._domain_is_denied(host):
            raise SandboxViolation(
                f"Access denied: '{host}' is on the domain denylist.",
                violation_type="network",
                detail=url,
            )

        # Domain allowlist check (if non-empty, host must match)
        if self._allowed_domains and not self._domain_is_allowed(host):
            raise SandboxViolation(
                f"Access denied: '{host}' is not on the domain allowlist.",
                violation_type="network",
                detail=url,
            )

        # Private IP check (after domain checks to avoid DNS for denied hosts)
        if self._block_private:
            self._check_private_ip(host, url)

    # -- internals -----------------------------------------------------------

    def _domain_is_denied(self, host: str) -> bool:
        for pattern in self._denied_domains:
            if self._domain_matches(host, pattern):
                return True
        return False

    def _domain_is_allowed(self, host: str) -> bool:
        for pattern in self._allowed_domains:
            if self._domain_matches(host, pattern):
                return True
        return False

    @staticmethod
    def _domain_matches(host: str, pattern: str) -> bool:
        """Match host against a pattern supporting * wildcards.

        Examples:
          *.example.com  matches  sub.example.com
          example.com    matches  example.com (exact)
        """
        return fnmatch(host, pattern)

    @staticmethod
    def _check_private_ip(host: str, url: str) -> None:
        """Raise SandboxViolation if *host* resolves to a private/loopback/link-local IP."""
        # If host is already an IP literal, check it directly
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise SandboxViolation(
                    f"Access denied: '{host}' is a private/internal IP address.",
                    violation_type="network",
                    detail=url,
                )
            return
        except ValueError:
            pass  # Not an IP literal — resolve via DNS below

        # DNS resolution — best-effort.  If it fails we still allow (the HTTP
        # client will get the error).  This avoids breaking legitimate requests
        # when no DNS is available.
        try:
            import socket
            info = socket.getaddrinfo(host, None)
            for _, _, _, _, sockaddr in info:
                addr = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(addr)
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        raise SandboxViolation(
                            f"Access denied: '{host}' resolves to private IP '{addr}'.",
                            violation_type="network",
                            detail=url,
                        )
                except ValueError:
                    continue
        except SandboxViolation:
            raise
        except Exception:
            # DNS failure — allow the request through; the HTTP layer will
            # surface any real connectivity problems.
            pass


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class Sandbox:
    """Aggregate facade over PathSandbox, CommandSandbox, and NetworkSandbox.

    Instantiated once in SharedContext and accessed by tools via
    ``session.shared_context.sandbox``.
    """

    def __init__(self, config: "SandboxConfig", workspace: Path) -> None:  # noqa: F821
        self.path = PathSandbox(config, workspace)
        self.command = CommandSandbox(config, workspace, path_sandbox=self.path)
        self.network = NetworkSandbox(config)
