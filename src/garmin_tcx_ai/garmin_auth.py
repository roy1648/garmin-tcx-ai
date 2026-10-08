"""Token-based Garmin Connect login for unattended (NAS) use.

The password is typed once, by the user, in an interactive terminal. It
is passed to Garmin Connect and then dropped; only the login tokens are
written to a dedicated token folder. Every later run loads those tokens
(the library refreshes them and saves the refreshed copy) and never sees
a password. When the tokens stop working the caller gets
:class:`AuthRequiredError` and must ask the user to log in again; there
is no fallback to a stored password.

The ``garminconnect`` package is optional and is imported lazily. The
library already writes its token file with owner-only permissions and
refuses symlinks; this module adds the checks the library does not make
(see :func:`harden_token_store`, :func:`ensure_not_inside`) and makes
sure a token file really exists after login, because the library hides
write errors.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from garmin_tcx_ai.credentials import redact_sensitive_text
from garmin_tcx_ai.importers.garminconnect_importer import (
    _load_garmin_class,
)

TOKEN_FILE_NAME = "garmin_tokens.json"
TOKEN_DIR_ENV = "GARMIN_TOKEN_DIR"
_MAX_MESSAGE_LENGTH = 300


class GarminAuthError(RuntimeError):
    """Base class. The message never contains credentials."""


class AuthRequiredError(GarminAuthError):
    """The saved login is missing or rejected; the user must log in."""


class GarminServiceError(GarminAuthError):
    """Garmin Connect could not be reached or refused for now.

    Retrying later may help; the saved login is not known to be bad.
    """


class TokenStoreError(GarminAuthError):
    """The token folder is unsafe or cannot be used."""


# ---------------------------------------------------------------------------
# Token folder
# ---------------------------------------------------------------------------


def token_file(token_dir: Path) -> Path:
    """Return the token file path inside *token_dir*."""
    return Path(token_dir) / TOKEN_FILE_NAME


def has_token(token_dir: Path) -> bool:
    """Return ``True`` when a non-empty token file exists."""
    path = token_file(token_dir)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def ensure_token_dir(token_dir: Path) -> Path:
    """Create the token folder (owner-only) and return it.

    Args:
        token_dir: Folder that holds the token file.

    Returns:
        The folder path.

    Raises:
        TokenStoreError: If the folder or token file is a symlink, or the
            path exists but is not a folder.
    """
    path = Path(token_dir)
    for candidate in (path, token_file(path)):
        if candidate.is_symlink():
            raise TokenStoreError(
                f"Token path must not be a symlink: {candidate.name}"
            )
    if path.exists() and not path.is_dir():
        raise TokenStoreError("Token path exists but is not a folder.")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise TokenStoreError(
            f"Cannot create token folder: {exc.strerror or exc}"
        ) from None
    return path


def harden_token_store(token_dir: Path) -> None:
    """Restrict the token folder and file to the owner.

    A token file copied in from another machine (for example uploaded
    through a file manager) usually arrives with loose permissions, so
    this is applied before every use. It does nothing on Windows, where
    POSIX permissions do not apply.

    Args:
        token_dir: Folder that holds the token file.

    Raises:
        TokenStoreError: If permissions are still too open afterwards.
    """
    if os.name == "nt":
        return
    targets = [(Path(token_dir), 0o700)]
    if token_file(token_dir).exists():
        targets.append((token_file(token_dir), 0o600))
    for path, mode in targets:
        try:
            path.chmod(mode)
            loose = stat.S_IMODE(path.stat().st_mode) & 0o077
        except OSError as exc:
            raise TokenStoreError(
                f"Cannot restrict permissions of {path.name}: "
                f"{exc.strerror or exc}"
            ) from None
        if loose:
            raise TokenStoreError(
                f"{path.name} is accessible to other users and could not "
                "be restricted."
            )


def ensure_not_inside(token_dir: Path, protected_dir: Path) -> None:
    """Refuse a token folder located inside *protected_dir*.

    The output folder is uploaded to cloud storage by a sync tool, so
    the tokens must never live under it.

    Args:
        token_dir: The token folder.
        protected_dir: A folder that is synced elsewhere.

    Raises:
        TokenStoreError: If *token_dir* is *protected_dir* or below it.
    """
    token = Path(token_dir).resolve()
    protected = Path(protected_dir).resolve()
    if token == protected or protected in token.parents:
        raise TokenStoreError(
            "Token folder must not be inside the synced output folder."
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login_with_credentials(
    token_dir: Path,
    *,
    email: str,
    password: str,
    mfa_prompt: Callable[[], str],
    garmin_cls: type[Any] | None = None,
) -> None:
    """Log in once with a password and save the tokens.

    The password is used for this call only and is never written.

    Args:
        token_dir: Folder that receives the token file.
        email: Garmin Connect account email.
        password: Garmin Connect password.
        mfa_prompt: Called when a verification code is needed.
        garmin_cls: The ``Garmin`` class; defaults to the real library.
            Tests pass a fake.

    Raises:
        AuthRequiredError: If Garmin rejected the credentials.
        GarminServiceError: If Garmin could not be reached.
        TokenStoreError: If the token folder is unsafe or no token file
            was written.
    """
    if not email or not password:
        raise AuthRequiredError("Email and password are required.")
    directory = ensure_token_dir(token_dir)
    cls = garmin_cls or _load_garmin_class()
    try:
        client = cls(email, password, prompt_mfa=mfa_prompt)
        client.login(str(directory))
    except Exception as exc:
        raise _translate(exc, (password, email)) from None
    harden_token_store(directory)
    if not has_token(directory):
        # The library swallows write errors, so a "successful" login can
        # leave nothing behind.
        raise TokenStoreError(
            "Login succeeded but no token file was saved. Check that the "
            "token folder is writable."
        )


def create_client_from_tokens(
    token_dir: Path,
    garmin_cls: type[Any] | None = None,
) -> Any:
    """Return a logged-in client built from the saved tokens only.

    The library refreshes expiring tokens and saves the new copy back
    into *token_dir*, so the folder must be writable.

    Args:
        token_dir: Folder that holds the token file.
        garmin_cls: The ``Garmin`` class; defaults to the real library.

    Returns:
        The authenticated client.

    Raises:
        AuthRequiredError: If there is no saved login or Garmin rejects
            it.
        GarminServiceError: If Garmin could not be reached.
        TokenStoreError: If the token folder is unsafe.
    """
    directory = Path(token_dir)
    if not has_token(directory):
        raise AuthRequiredError(
            "No saved Garmin login. Run 'garmin-tcx-ai garmin-login'."
        )
    ensure_token_dir(directory)
    harden_token_store(directory)
    cls = garmin_cls or _load_garmin_class()
    try:
        client = cls()
        client.login(str(directory))
    except Exception as exc:
        raise _translate(exc, ()) from None
    return client


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _translate(
    exc: BaseException,
    secrets: tuple[str, ...],
) -> GarminAuthError:
    """Map a library exception to a credential-free :class:`GarminAuthError`.

    The library reports *any* failure to fetch the user profile,
    including a plain network outage, as an authentication error. Going
    by the class name alone would tell the user to log in again whenever
    the NAS is offline, so the whole cause chain is inspected and
    "log in again" is only reported when there is no sign of a network
    problem. The library's classes are recognised by name so this module
    does not need the optional package to be installed.
    """
    if isinstance(exc, GarminAuthError):
        return exc
    chain = _cause_chain(exc)
    names = {cls.__name__ for item in chain for cls in type(item).__mro__}
    status = _http_status(chain)
    detail = _safe_text(str(exc), secrets)

    if status == 401:
        return AuthRequiredError(
            f"Garmin rejected the saved or entered login. {detail}".strip()
        )
    if status == 429 or any("TooManyRequests" in n for n in names):
        return GarminServiceError(
            f"Garmin Connect is rate limiting requests. {detail}".strip()
        )
    if status is not None:
        # Only 401 proves the login is bad. A 403 is often a bot
        # challenge and 5xx is Garmin's side, so asking the user to log
        # in again would not help.
        return GarminServiceError(
            f"Garmin Connect answered with HTTP {status}. {detail}".strip()
        )
    if _is_network_problem(chain, names):
        return GarminServiceError(
            "Could not reach Garmin Connect (network problem). "
            f"{detail}".strip()
        )
    if any("AuthenticationError" in name for name in names):
        return AuthRequiredError(
            f"Garmin rejected the saved or entered login. {detail}".strip()
        )
    return GarminServiceError(
        f"Could not complete the Garmin Connect request. {detail}".strip()
    )


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """Return *exc* followed by its causes (bounded, cycle-safe)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _http_status(chain: list[BaseException]) -> int | None:
    """Return the first HTTP status code carried by the chain."""
    for item in chain:
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _is_network_problem(
    chain: list[BaseException],
    names: set[str],
) -> bool:
    """Return ``True`` when the chain looks like a connectivity failure."""
    if any(isinstance(item, OSError) for item in chain):
        return True
    markers = ("Timeout", "Proxy", "SSL", "MaxRetry", "ConnectionError")
    return any(marker in name for name in names for marker in markers)


def _safe_text(text: str, secrets: tuple[str, ...]) -> str:
    """Remove known secrets and secret-looking patterns from *text*."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = redact_sensitive_text(text).strip()
    if len(text) > _MAX_MESSAGE_LENGTH:
        text = text[:_MAX_MESSAGE_LENGTH] + "..."
    return text


__all__ = [
    "TOKEN_DIR_ENV",
    "TOKEN_FILE_NAME",
    "AuthRequiredError",
    "GarminAuthError",
    "GarminServiceError",
    "TokenStoreError",
    "create_client_from_tokens",
    "ensure_not_inside",
    "ensure_token_dir",
    "harden_token_store",
    "has_token",
    "login_with_credentials",
    "token_file",
]
