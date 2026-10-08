"""Unit tests for token-based Garmin login (no network, no library)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from garmin_tcx_ai import cli, garmin_auth

PASSWORD = "S3cret-Passw0rd!"
EMAIL = "runner@example.com"
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permissions only"
)


class GarminConnectAuthenticationError(Exception):
    """Same name as the library's class; recognised by name."""


class GarminConnectTooManyRequestsError(Exception):
    """Same name as the library's class; recognised by name."""


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class HttpError(Exception):
    """Carries a response like ``requests.HTTPError`` does."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _Response(status_code)


class FakeGarmin:
    """Stand-in for ``garminconnect.Garmin``."""

    mode = "ok"
    logins: list[tuple[str | None, str | None, str]] = []
    mfa_codes: list[str] = []

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        prompt_mfa=None,
    ) -> None:
        self.email = email
        self.password = password
        self.prompt_mfa = prompt_mfa

    def login(self, tokenstore: str) -> None:
        FakeGarmin.logins.append((self.email, self.password, tokenstore))
        token = Path(tokenstore) / garmin_auth.TOKEN_FILE_NAME
        if FakeGarmin.mode == "reject":
            raise GarminConnectAuthenticationError(
                f"bad login for {self.email} password={self.password}"
            )
        if FakeGarmin.mode == "ratelimit":
            raise GarminConnectTooManyRequestsError("429")
        if FakeGarmin.mode == "network":
            raise ConnectionError(f"timeout token=abc123 {self.password}")
        if FakeGarmin.mode == "auth_wrapping_network":
            # What the real library does when the profile call fails.
            try:
                raise ConnectionError("proxy tunnel failed")
            except ConnectionError as cause:
                raise GarminConnectAuthenticationError(
                    "Failed to retrieve social profile"
                ) from cause
        if FakeGarmin.mode == "http401":
            try:
                raise HttpError(401)
            except HttpError as cause:
                raise GarminConnectAuthenticationError(
                    "Failed to retrieve social profile"
                ) from cause
        if FakeGarmin.mode == "http403":
            try:
                raise HttpError(403)
            except HttpError as cause:
                raise GarminConnectAuthenticationError(
                    "Failed to retrieve social profile"
                ) from cause
        if self.password is not None:
            if self.prompt_mfa is not None:
                FakeGarmin.mfa_codes.append(self.prompt_mfa())
            if FakeGarmin.mode != "no_write":
                token.write_text('{"di_token": "x"}', encoding="utf-8")
        elif not token.exists():
            raise GarminConnectAuthenticationError("Username and password")


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeGarmin.mode = "ok"
    FakeGarmin.logins = []
    FakeGarmin.mfa_codes = []


def _login(token_dir: Path, **overrides) -> None:
    garmin_auth.login_with_credentials(
        token_dir,
        email=overrides.get("email", EMAIL),
        password=overrides.get("password", PASSWORD),
        mfa_prompt=lambda: "123456",
        garmin_cls=FakeGarmin,
    )


# ---------------------------------------------------------------------------
# login_with_credentials
# ---------------------------------------------------------------------------


def test_login_saves_token_and_passes_mfa_callback(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    assert garmin_auth.has_token(token_dir)
    assert FakeGarmin.logins == [(EMAIL, PASSWORD, str(token_dir))]
    assert FakeGarmin.mfa_codes == ["123456"]


def test_login_never_writes_the_password_to_disk(tmp_path: Path) -> None:
    _login(tmp_path / "tokens")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("email", "password"), [("", PASSWORD), (EMAIL, "")])
def test_login_requires_email_and_password(
    tmp_path: Path, email: str, password: str
) -> None:
    with pytest.raises(garmin_auth.AuthRequiredError):
        _login(tmp_path / "t", email=email, password=password)
    assert FakeGarmin.logins == []


def test_rejected_login_is_auth_required_without_secrets(
    tmp_path: Path,
) -> None:
    FakeGarmin.mode = "reject"
    with pytest.raises(garmin_auth.AuthRequiredError) as caught:
        _login(tmp_path / "t")
    message = str(caught.value)
    assert PASSWORD not in message
    assert EMAIL not in message
    assert caught.value.__suppress_context__


def test_rate_limit_is_a_service_error(tmp_path: Path) -> None:
    FakeGarmin.mode = "ratelimit"
    with pytest.raises(garmin_auth.GarminServiceError):
        _login(tmp_path / "t")


def test_network_error_message_is_redacted(tmp_path: Path) -> None:
    FakeGarmin.mode = "network"
    with pytest.raises(garmin_auth.GarminServiceError) as caught:
        _login(tmp_path / "t")
    assert PASSWORD not in str(caught.value)
    assert "abc123" not in str(caught.value)


def _token_only_error(tmp_path: Path, mode: str) -> Exception:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    FakeGarmin.mode = mode
    with pytest.raises(garmin_auth.GarminAuthError) as caught:
        garmin_auth.create_client_from_tokens(token_dir, FakeGarmin)
    return caught.value


def test_network_outage_wrapped_as_auth_error_is_not_auth_required(
    tmp_path: Path,
) -> None:
    error = _token_only_error(tmp_path, "auth_wrapping_network")
    assert isinstance(error, garmin_auth.GarminServiceError)
    assert not isinstance(error, garmin_auth.AuthRequiredError)
    assert "network" in str(error)


def test_http_401_is_auth_required(tmp_path: Path) -> None:
    error = _token_only_error(tmp_path, "http401")
    assert isinstance(error, garmin_auth.AuthRequiredError)


def test_http_403_block_is_a_service_error(tmp_path: Path) -> None:
    error = _token_only_error(tmp_path, "http403")
    assert isinstance(error, garmin_auth.GarminServiceError)


def test_missing_token_file_after_login_is_detected(tmp_path: Path) -> None:
    FakeGarmin.mode = "no_write"
    with pytest.raises(garmin_auth.TokenStoreError, match="no token file"):
        _login(tmp_path / "t")


@posix_only
def test_login_leaves_owner_only_permissions(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    token = garmin_auth.token_file(token_dir)
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# create_client_from_tokens
# ---------------------------------------------------------------------------


def test_token_only_login_never_sends_a_password(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    FakeGarmin.logins.clear()
    client = garmin_auth.create_client_from_tokens(token_dir, FakeGarmin)
    assert isinstance(client, FakeGarmin)
    assert FakeGarmin.logins == [(None, None, str(token_dir))]


def test_no_saved_login_requires_login_and_skips_garmin(
    tmp_path: Path,
) -> None:
    with pytest.raises(garmin_auth.AuthRequiredError, match="garmin-login"):
        garmin_auth.create_client_from_tokens(tmp_path / "x", FakeGarmin)
    assert FakeGarmin.logins == []


def test_empty_token_file_counts_as_no_login(tmp_path: Path) -> None:
    garmin_auth.ensure_token_dir(tmp_path)
    garmin_auth.token_file(tmp_path).write_text("", encoding="utf-8")
    with pytest.raises(garmin_auth.AuthRequiredError):
        garmin_auth.create_client_from_tokens(tmp_path, FakeGarmin)


def test_rejected_tokens_require_a_new_login_not_a_password_retry(
    tmp_path: Path,
) -> None:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    FakeGarmin.logins.clear()
    FakeGarmin.mode = "reject"
    with pytest.raises(garmin_auth.AuthRequiredError):
        garmin_auth.create_client_from_tokens(token_dir, FakeGarmin)
    assert all(password is None for _, password, _ in FakeGarmin.logins)


def test_temporary_failure_is_not_reported_as_auth_required(
    tmp_path: Path,
) -> None:
    token_dir = tmp_path / "tokens"
    _login(token_dir)
    FakeGarmin.mode = "network"
    with pytest.raises(garmin_auth.GarminServiceError):
        garmin_auth.create_client_from_tokens(token_dir, FakeGarmin)


@posix_only
def test_loose_permissions_on_a_copied_token_are_tightened(
    tmp_path: Path,
) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(mode=0o755)
    token = garmin_auth.token_file(token_dir)
    token.write_text('{"di_token": "x"}', encoding="utf-8")
    token.chmod(0o644)
    token_dir.chmod(0o755)

    garmin_auth.create_client_from_tokens(token_dir, FakeGarmin)

    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Token folder safety
# ---------------------------------------------------------------------------


@posix_only
def test_symlinked_token_folder_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(garmin_auth.TokenStoreError, match="symlink"):
        garmin_auth.ensure_token_dir(link)


@posix_only
def test_symlinked_token_file_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    garmin_auth.token_file(token_dir).symlink_to(other)
    with pytest.raises(garmin_auth.TokenStoreError, match="symlink"):
        garmin_auth.ensure_token_dir(token_dir)


def test_token_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(garmin_auth.TokenStoreError, match="not a folder"):
        garmin_auth.ensure_token_dir(path)


def test_token_folder_inside_synced_output_is_refused(
    tmp_path: Path,
) -> None:
    output = tmp_path / "RunningData"
    with pytest.raises(garmin_auth.TokenStoreError, match="synced"):
        garmin_auth.ensure_not_inside(output / "tokens", output)
    with pytest.raises(garmin_auth.TokenStoreError):
        garmin_auth.ensure_not_inside(output, output)


def test_token_folder_beside_synced_output_is_allowed(
    tmp_path: Path,
) -> None:
    garmin_auth.ensure_not_inside(
        tmp_path / "tokens", tmp_path / "RunningData"
    )
    garmin_auth.ensure_not_inside(
        tmp_path / "RunningData-tokens", tmp_path / "RunningData"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_requires_a_token_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(garmin_auth.TOKEN_DIR_ENV, raising=False)
    assert cli.main(["garmin-login"]) == 2
    assert garmin_auth.TOKEN_DIR_ENV in capsys.readouterr().err


def test_cli_refuses_to_run_without_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    code = cli.main(["garmin-login", "--token-dir", str(tmp_path / "t")])
    assert code == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert not (tmp_path / "t").exists()


def test_cli_login_uses_env_token_dir_and_prints_no_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(garmin_auth.TOKEN_DIR_ENV, str(tmp_path / "t"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p: PASSWORD)
    monkeypatch.setattr("builtins.input", lambda _p="": "123456")
    monkeypatch.setattr(garmin_auth, "_load_garmin_class", lambda: FakeGarmin)

    code = cli.main(["garmin-login", "--email", EMAIL])

    out = capsys.readouterr()
    assert code == 0
    assert garmin_auth.has_token(tmp_path / "t")
    assert PASSWORD not in out.out + out.err
    assert "No password was stored" in out.out
    # The final proof login used tokens only.
    assert FakeGarmin.logins[-1][:2] == (None, None)


def test_cli_login_failure_returns_1_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeGarmin.mode = "reject"
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p: PASSWORD)
    monkeypatch.setattr(garmin_auth, "_load_garmin_class", lambda: FakeGarmin)

    code = cli.main(
        ["garmin-login", "--token-dir", str(tmp_path / "t"), "--email", EMAIL]
    )

    err = capsys.readouterr().err
    assert code == 1
    assert PASSWORD not in err
    assert EMAIL not in err


def test_cli_check_reports_valid_and_missing_logins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(garmin_auth, "_load_garmin_class", lambda: FakeGarmin)
    token_dir = tmp_path / "t"
    check = ["garmin-login", "--check", "--token-dir", str(token_dir)]
    assert cli.main(check) == 1
    assert "garmin-login" in capsys.readouterr().err

    _login(token_dir)
    assert cli.main(check) == 0
    assert "valid" in capsys.readouterr().out
