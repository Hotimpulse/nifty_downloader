"""Build a native desktop artifact for the operating system running this file."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
APP_NAME = "yt_downloader"


def build_command() -> tuple[list[str], Path]:
    """Return the native PyInstaller invocation and its expected output."""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
    ]

    if sys.platform == "win32":
        command.append("--onefile")
        icon = PROJECT_ROOT / "app.ico"
        version_file = PROJECT_ROOT / "version_info.txt"
        if icon.is_file():
            command.extend(["--icon", str(icon)])
        if version_file.is_file():
            command.extend(["--version-file", str(version_file)])
        artifact = PROJECT_ROOT / "dist" / f"{APP_NAME}.exe"
    elif sys.platform == "darwin":
        # A one-directory bundle is the most reliable shape for code signing and
        # notarization, and PyInstaller wraps it in a double-clickable .app.
        command.append("--onedir")
        icon = PROJECT_ROOT / "app.icns"
        if icon.is_file():
            command.extend(["--icon", str(icon)])
        artifact = PROJECT_ROOT / "dist" / f"{APP_NAME}.app"
    else:
        raise RuntimeError(
            "Native desktop builds are currently supported on Windows and macOS."
        )

    command.append(str(PROJECT_ROOT / "main.py"))
    return command, artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the build command without running it"
    )
    args = parser.parse_args()

    try:
        command, artifact = build_command()
    except RuntimeError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    print(f"Building native {sys.platform} app…")
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    if args.dry_run:
        return 0

    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    except subprocess.CalledProcessError as error:
        return error.returncode

    print(f"\nBuild complete: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
