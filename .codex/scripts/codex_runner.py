"""One owner for `codex exec`: which binary, which policy, which bounds."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import model_usage
from process_control import (
    ProcessTreeCleanupError,
    ProcessTreeTimeout,
    run_with_tree_timeout,
)


MEMORY_MODEL = "gpt-5.6-terra"
MEMORY_REASONING = "medium"
_CHILD_ENV_ALLOWLIST = (
    "APPDATA",
    "CODEX_HOME",
    "CODEX_CA_CERTIFICATE",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _child_environment() -> dict[str, str]:
    environment = {
        name: value
        for name in _CHILD_ENV_ALLOWLIST
        if (value := os.environ.get(name))
    }
    # The child runs in a temporary/staging directory, so preserve path meaning.
    for name in ("CODEX_HOME", "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE"):
        if name in environment:
            environment[name] = str(Path(environment[name]).expanduser().absolute())
    environment["BEYIN_INVOKED_BY"] = "beyin-scripts"
    return environment


def find_codex() -> str:
    configured = os.environ.get("CODEX_CLI_PATH", "").strip()
    if configured:
        # A configured path that does not resolve is an operator error, not a
        # reason to run some other binary: falling back would hide the typo
        # behind a wrapper that appears healthy in doctor.
        if not Path(configured).is_file():
            raise FileNotFoundError(f"codex-cli-path-invalid: {configured}")
        return str(Path(configured))

    if os.name == 'nt' and (local_app_data := os.environ.get('LOCALAPPDATA')):
        # The desktop runtime understands the App's config schema; an older npm
        # CLI can reject newer feature tables before it even starts a worker.
        desktop = Path(local_app_data) / 'OpenAI/Codex/bin'
        candidates = [path for path in desktop.glob('*/codex.exe') if path.is_file()]
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))

    wrapper = shutil.which("codex")
    if os.name == "nt" and wrapper:
        native = (
            Path(wrapper).parent
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        if native.is_file():
            return str(native)
    if wrapper:
        return wrapper
    raise FileNotFoundError("codex-cli-missing")


def _exec_argv(output_path: Path, *, sandbox: str, stage: Path | None) -> list[str]:
    command = [
        "exec",
        "--model",
        MEMORY_MODEL,
        "--ephemeral",
        "--skip-git-repo-check",
        "--disable",
        "hooks",
    ]
    if stage is not None:
        command += ["--cd", str(stage)]
    command += [
        "--sandbox",
        sandbox,
        "-c",
        'approval_policy="never"',
        "-c",
        f'model_reasoning_effort="{MEMORY_REASONING}"',
        "--output-last-message",
        str(output_path),
    ]
    return command


def _within(path: Path, root: Path) -> bool:
    resolved_root = str(root.resolve())
    try:
        return os.path.commonpath([path, resolved_root]) == resolved_root
    except ValueError:
        return False


def run_exec(
    prompt: str,
    *,
    sandbox: str,
    timeout: float,
    stage: Path | None = None,
    forbidden_root: Path | None = None,
    propagate_cleanup_error: bool = False,
    usage_state_dir: Path | None = None,
    purpose: str = "unknown",
) -> tuple[str | None, str | None]:
    """Run one bounded, sandboxed, hook-disabled `codex exec`.

    Returns ``(text, reason_code)``. ``text`` is the ``--output-last-message``
    body when it could be read; ``reason_code`` names the failure otherwise.
    ``(None, None)`` means codex succeeded but wrote no readable last message —
    a caller that needs the text turns that into its own error.

    ``stage`` both adds ``--cd`` and becomes the working directory; without it
    the run happens in the private temporary directory that holds the output
    file. ``forbidden_root`` refuses to run when that temporary directory
    landed inside a tree the sandbox must not be able to write.
    Queue adapters set ``propagate_cleanup_error`` so an unverified child tree
    reaches the durable worker fence instead of becoming a retryable reason.
    ``usage_state_dir`` verildiğinde çağrı ``model_usage``'a kaydedilir; kayıt
    hatası asıl çağrının sonucunu asla değiştirmez.
    """
    start = time.monotonic()

    def note(outcome: str, result_chars: int = 0) -> None:
        if usage_state_dir is None:
            return
        try:
            model_usage.record(
                usage_state_dir,
                purpose=purpose,
                prompt_chars=len(prompt),
                duration_ms=int((time.monotonic() - start) * 1000),
                outcome=outcome,
                result_chars=result_chars,
            )
        except (OSError, ValueError):
            pass

    try:
        text, reason = _bounded_exec(
            prompt,
            sandbox=sandbox,
            timeout=timeout,
            stage=stage,
            forbidden_root=forbidden_root,
            propagate_cleanup_error=propagate_cleanup_error,
        )
    except ProcessTreeCleanupError:
        note("codex-cleanup-error")
        raise
    note(reason or ("ok" if text is not None else "output-missing"), len(text or ""))
    return text, reason


def _bounded_exec(
    prompt: str,
    *,
    sandbox: str,
    timeout: float,
    stage: Path | None = None,
    forbidden_root: Path | None = None,
    propagate_cleanup_error: bool = False,
) -> tuple[str | None, str | None]:
    try:
        codex = find_codex()
    except FileNotFoundError as exc:
        reason = str(exc).split(":", 1)[0]
        return None, (
            "codex-cli-path-invalid"
            if reason == "codex-cli-path-invalid"
            else "codex-cli-missing"
        )

    environment = _child_environment()
    try:
        with tempfile.TemporaryDirectory(
            prefix="beyin-codex-",
            ignore_cleanup_errors=True,
        ) as temporary:
            output_dir = Path(temporary).resolve()
            if forbidden_root is not None and _within(output_dir, forbidden_root):
                return None, "temporary-directory-inside-vault"
            output_path = output_dir / "last-message.md"
            result = run_with_tree_timeout(
                [
                    codex,
                    *_exec_argv(output_path, sandbox=sandbox, stage=stage),
                    "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=stage or output_dir,
                env=environment,
                timeout=timeout,
                input=prompt.encode("utf-8"),
            )
            if result.returncode != 0:
                return None, f"codex-exit-{result.returncode}"
            try:
                return output_path.read_text(encoding="utf-8").strip(), None
            except OSError:
                return None, None
    except ProcessTreeCleanupError:
        if propagate_cleanup_error:
            raise
        return None, "codex-cleanup-error"
    except ProcessTreeTimeout:
        return None, "codex-timeout"
    except OSError:
        return None, "codex-exec-error"
