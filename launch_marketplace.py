#!/usr/bin/env python3
"""
Launcher (.exe): opens PyCharm with the project and starts Flask in a separate console.

The .exe itself contains NO project code. It only:
  1) launches PyCharm pointing at the project folder
  2) opens a new console window running .\.venv\Scripts\python.exe run.py

Templates, .env, DB, migrations, .venv all live on disk.
PyCharm and Flask read them directly.

Closing this .exe does NOT stop PyCharm or Flask - they run independently.
"""
import os
import subprocess
import sys


PYCHARM_EXE = r"C:\Program Files\JetBrains\PyCharm Community Edition 2023.3.5\bin\pycharm64.exe"

# Console code page: force UTF-8 so Cyrillic paths in cmd output don't break.
# 65001 = CP_UTF8.
CMD_UTF8 = "chcp 65001 >NUL"


def find_project_dir() -> str:
    """Project root = folder containing app/, run.py, .venv."""
    if getattr(sys, "frozen", False):
        candidate = os.path.dirname(os.path.abspath(sys.executable))
    else:
        candidate = os.path.dirname(os.path.abspath(__file__))

    if os.path.isdir(os.path.join(candidate, "app")) and os.path.isfile(
        os.path.join(candidate, "run.py")
    ):
        return candidate

    parent = os.path.dirname(candidate)
    if os.path.isdir(os.path.join(parent, "app")) and os.path.isfile(
        os.path.join(parent, "run.py")
    ):
        return parent

    print(f"[launcher] Project not found near: {candidate}")
    print("[launcher] Put Marketplace.exe into C:\\Users\\ronni\\PycharmProjects\\Marketplace")
    sys.exit(1)


def open_pycharm(project_dir: str) -> None:
    if not os.path.isfile(PYCHARM_EXE):
        print(f"[launcher] PyCharm not found: {PYCHARM_EXE}")
        return
    try:
        subprocess.Popen([PYCHARM_EXE, project_dir], close_fds=False)
        print(f"[launcher] PyCharm opened: {project_dir}")
    except OSError as e:
        print(f"[launcher] Failed to open PyCharm: {e}")


def start_flask_console(project_dir: str) -> None:
    """Opens a new console window and runs Flask via the project's venv."""
    venv_python = os.path.join(project_dir, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(venv_python):
        print(f"[launcher] venv Python not found: {venv_python}")
        print("[launcher] Flask NOT started. Run manually: .\\.venv\\Scripts\\python.exe run.py")
        return

    # Build a small .bat script in TEMP and run it via cmd /K.
    # Why .bat: passing complex commands through cmd /K with quoted Cyrillic
    # paths is fragile (parser errors, encoding issues). A .bat side-steps all
    # of that - cmd.exe reads it as-is, no quoting games.
    bat_path = os.path.join(os.environ.get("TEMP", "."), "marketplace_run.bat")
    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            f.write(f"{CMD_UTF8}\r\n")
            f.write(f'cd /d "{project_dir}"\r\n')
            f.write(f'"{venv_python}" run.py\r\n')
            f.write("echo.\r\n")
            f.write("echo [marketplace_run] Server stopped. Press any key to close.\r\n")
            f.write("pause >NUL\r\n")
    except OSError as e:
        print(f"[launcher] Failed to write helper .bat: {e}")
        return

    try:
        # CREATE_NEW_CONSOLE (0x00000010) - separate window.
        subprocess.Popen(
            ["cmd.exe", "/K", bat_path],
            creationflags=0x00000010,
            close_fds=False,
            cwd=project_dir,
        )
        print(f"[launcher] Flask console opened (helper: {bat_path})")
    except OSError as e:
        print(f"[launcher] Failed to open Flask console: {e}")


def main() -> int:
    project_dir = find_project_dir()
    print(f"[launcher] Project: {project_dir}")
    open_pycharm(project_dir)
    start_flask_console(project_dir)
    print("[launcher] Done. You can close this window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
