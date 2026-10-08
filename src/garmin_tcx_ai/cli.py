"""CLI module for the Garmin TCX AI tool."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import date
from pathlib import Path

from garmin_tcx_ai import garmin_auth
from garmin_tcx_ai.ai_text import DENSITY_CHOICES
from garmin_tcx_ai.importers import (
    GarminConnectImportConfig,
    download_tcx_activities,
)
from garmin_tcx_ai.sync_config import load_sync_config
from garmin_tcx_ai.sync_job import EXIT_CODES, run_sync
from garmin_tcx_ai.pipeline import (
    BundleRunConfig,
    BundleRunResult,
    run_bundle,
)


def _print_text_outputs(result: BundleRunResult) -> None:
    """Print the AI text output paths when they were written."""
    if result.summary_txt_path is not None:
        print(f"Summary text: {result.summary_txt_path.resolve()}")
    if result.all_in_one_txt_path is not None:
        print(f"All-in-one text: {result.all_in_one_txt_path.resolve()}")
    if result.run_txt_paths:
        print(f"Run text files: {len(result.run_txt_paths)}")


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    """Register output-format options shared by both sub-commands."""
    parser.add_argument(
        "--gps-policy",
        default="redact_start_end",
        choices=["keep", "remove", "redact_start_end"],
        help=(
            "GPS privacy policy for legacy/atomic outputs "
            "(default: redact_start_end). Text outputs never contain GPS."
        ),
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Taipei",
        help="Timezone name for local times (default: Asia/Taipei).",
    )
    parser.add_argument(
        "--max-gap-minutes",
        type=int,
        default=30,
        help=(
            "Max gap minutes between adjacent activities for "
            "session candidate grouping (default: 30)."
        ),
    )
    parser.add_argument(
        "--trackpoint-density",
        default="standard",
        choices=list(DENSITY_CHOICES),
        help=(
            "Trackpoint sampling density in the per-run text files "
            "(default: standard)."
        ),
    )
    parser.add_argument(
        "--no-ai-text",
        action="store_true",
        help="Do not write summary.txt / all_in_one.txt / runs/*.txt.",
    )
    parser.add_argument(
        "--write-session-bundle",
        action="store_true",
        help=(
            "Additionally write the legacy session_bundle JSON/Markdown "
            "outputs."
        ),
    )
    parser.add_argument(
        "--write-atomic",
        action="store_true",
        help="Additionally write per-activity debug/audit artifacts.",
    )
    parser.add_argument(
        "--write-coach-handoff",
        action="store_true",
        help=(
            "Additionally write coach handoff Markdown report "
            "(implies --write-session-bundle)."
        ),
    )


def _parse_date(value: str | None) -> date | None:
    """Parse ``YYYY-MM-DD`` leniently; ``None`` when absent or invalid."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _bundle_config(
    args: argparse.Namespace,
    input_path: Path,
) -> BundleRunConfig:
    """Build a BundleRunConfig from parsed CLI arguments.

    For ``import-garminconnect`` the requested date range is passed on
    so the summary can flag partial weeks.
    """
    return BundleRunConfig(
        coverage_start=_parse_date(getattr(args, "start_date", None)),
        coverage_end=_parse_date(getattr(args, "end_date", None)),
        input_path=input_path,
        output_dir=Path(args.output),
        gps_policy=args.gps_policy,
        timezone_name=args.timezone,
        max_gap_minutes=args.max_gap_minutes,
        write_atomic=args.write_atomic,
        write_coach_handoff=args.write_coach_handoff,
        write_ai_text=not args.no_ai_text,
        write_session_bundle=args.write_session_bundle,
        trackpoint_density=args.trackpoint_density,
    )


def _run_bundle(args: argparse.Namespace) -> int:
    """Orchestrate parsing, normalizing, and exporting of TCX files.

    Returns:
        0 on success, 1 on failure.
    """
    config = _bundle_config(args, Path(args.input))

    result = run_bundle(config)

    for warning in result.warning_messages:
        print(warning, file=sys.stderr)

    if not result.success:
        if result.error_message:
            print(result.error_message, file=sys.stderr)
        return 1

    print(f"Successfully processed {result.activity_count} activities.")
    print(f"Output folder: {result.output_dir.resolve()}")
    _print_text_outputs(result)
    return 0


def _run_import_garminconnect(args: argparse.Namespace) -> int:
    """Download Garmin Connect TCX files and run the bundle pipeline.

    Returns:
        0 on success, 1 on failure.
    """
    import_config = GarminConnectImportConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        activity_type=args.activity_type,
        download_dir=Path(args.download_dir),
        email=args.email,
        limit=args.limit,
        overwrite=args.overwrite,
    )
    import_result = download_tcx_activities(import_config)

    for warning in import_result.warning_messages:
        print(warning, file=sys.stderr)

    if not import_result.success:
        if import_result.error_message:
            print(import_result.error_message, file=sys.stderr)
        return 1

    bundle_config = _bundle_config(args, import_result.download_dir)
    bundle_result = run_bundle(bundle_config)

    for warning in bundle_result.warning_messages:
        print(warning, file=sys.stderr)

    if not bundle_result.success:
        if bundle_result.error_message:
            print(bundle_result.error_message, file=sys.stderr)
        return 1

    print(f"Downloaded activities: {import_result.downloaded_count}")
    print(f"Skipped activities: {import_result.skipped_count}")
    print(f"Failed activities: {import_result.failed_count}")
    print(f"Download folder: {import_result.download_dir.resolve()}")
    print(f"Processed activities: {bundle_result.activity_count}")
    print(f"Output folder: {bundle_result.output_dir.resolve()}")
    _print_text_outputs(bundle_result)
    return 0


def _run_garmin_login(args: argparse.Namespace) -> int:
    """Save a Garmin Connect login as tokens, or check a saved one.

    The password is read from an interactive terminal only, so it never
    appears in shell history, scripts or logs.

    Returns:
        0 on success, 1 when the login failed, 2 for a usage problem.
    """
    token_dir_text = args.token_dir or os.environ.get(
        garmin_auth.TOKEN_DIR_ENV
    )
    if not token_dir_text:
        print(
            "Error: set --token-dir or the "
            f"{garmin_auth.TOKEN_DIR_ENV} environment variable.",
            file=sys.stderr,
        )
        return 2
    token_dir = Path(token_dir_text)

    try:
        if args.check:
            garmin_auth.create_client_from_tokens(token_dir)
            print("Saved Garmin login is valid.")
            return 0

        if not sys.stdin.isatty():
            print(
                "Error: garmin-login needs an interactive terminal.",
                file=sys.stderr,
            )
            return 2
        email = args.email or input("Garmin email: ")
        password = getpass.getpass("Garmin password: ")
        garmin_auth.login_with_credentials(
            token_dir,
            email=email,
            password=password,
            mfa_prompt=lambda: input("Garmin MFA code: "),
        )
        # Prove that the saved tokens work on their own.
        garmin_auth.create_client_from_tokens(token_dir)
    except garmin_auth.GarminAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # optional dependency missing, etc.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Garmin login saved. No password was stored.")
    return 0


def _run_sync(args: argparse.Namespace) -> int:
    """Run one NAS sync with settings from environment variables.

    Returns:
        0 on success, 1 on failure, 2 for a usage problem, 3 when the
        Garmin login must be renewed, 4 when a sync is already running.
    """
    try:
        config = load_sync_config()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    result = run_sync(config)
    if result.outcome == "ok":
        print(
            f"Sync finished: {result.new_count} new, "
            f"{result.activities} activities in total."
        )
    else:
        print(f"Sync {result.outcome}: {result.message}", file=sys.stderr)
    return EXIT_CODES[result.outcome]


def main(argv: list[str] | None = None) -> int:
    """Run the CLI entry point for garmin-tcx-ai."""
    parser = argparse.ArgumentParser(
        prog="garmin-tcx-ai",
        description="Garmin TCX AI session bundle CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bundle_parser = subparsers.add_parser(
        "bundle",
        help="Convert Garmin TCX files to AI-readable text files.",
    )
    bundle_parser.add_argument(
        "--input",
        required=True,
        help="Path to a single .tcx file or a directory containing .tcx.",
    )
    bundle_parser.add_argument(
        "--output",
        required=True,
        help="Output directory for the processed files.",
    )
    _add_output_options(bundle_parser)

    import_parser = subparsers.add_parser(
        "import-garminconnect",
        help=(
            "Download local Garmin Connect TCX files and process a "
            "session bundle."
        ),
    )
    import_parser.add_argument(
        "--start-date",
        required=True,
        help="Start date for Garmin Connect activity import (YYYY-MM-DD).",
    )
    import_parser.add_argument(
        "--end-date",
        required=True,
        help="End date for Garmin Connect activity import (YYYY-MM-DD).",
    )
    import_parser.add_argument(
        "--activity-type",
        default="running",
        help="Garmin Connect activity type to import (default: running).",
    )
    import_parser.add_argument(
        "--download-dir",
        default="data/raw/garminconnect",
        help=(
            "Local folder for downloaded TCX files "
            "(default: data/raw/garminconnect)."
        ),
    )
    import_parser.add_argument(
        "--email",
        help="Garmin Connect email. If omitted, the CLI prompts for it.",
    )
    import_parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum number of activities to download.",
    )
    import_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing TCX files in the download folder.",
    )
    import_parser.add_argument(
        "--output",
        required=True,
        help="Output directory for the processed files.",
    )
    _add_output_options(import_parser)

    login_parser = subparsers.add_parser(
        "garmin-login",
        help=(
            "Log in to Garmin Connect once and save tokens "
            "(the password is not stored)."
        ),
    )
    login_parser.add_argument(
        "--token-dir",
        help=(
            "Folder for the token file. Defaults to the "
            f"{garmin_auth.TOKEN_DIR_ENV} environment variable."
        ),
    )
    login_parser.add_argument(
        "--email",
        help="Garmin Connect email. If omitted, the CLI prompts for it.",
    )
    login_parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify the saved login; do not ask for a password.",
    )

    subparsers.add_parser(
        "sync",
        help=(
            "Download new Garmin activities and rewrite the current/ "
            "folder (settings come from SYNC_* environment variables)."
        ),
    )

    args = parser.parse_args(argv)

    if args.command == "bundle":
        return _run_bundle(args)
    if args.command == "import-garminconnect":
        return _run_import_garminconnect(args)
    if args.command == "garmin-login":
        return _run_garmin_login(args)
    if args.command == "sync":
        return _run_sync(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
