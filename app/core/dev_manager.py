"""Dev manager — git operations, package management."""

from __future__ import annotations

import subprocess

from app.exceptions import ShellExecutionError


class DevManager:
    def __init__(self, base_path: str = "/home") -> None:
        self._base = base_path

    def _run_git(self, cwd: str, *args: str) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result
        except subprocess.TimeoutExpired:
            raise ShellExecutionError("Git command timed out")
        except FileNotFoundError:
            raise ShellExecutionError("Git not installed")

    def git_status(self, cwd: str) -> dict:
        result = self._run_git(cwd, "status", "--porcelain=v1", "-b")
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

        branch = "unknown"
        staged, modified, untracked = [], [], []

        for line in lines:
            if line.startswith("##"):
                branch = line[3:].split("...")[0]
            elif line.startswith("??"):
                untracked.append(line[3:])
            elif line[0] != " " and line[0] != "?":
                staged.append(line[3:])
            elif line[1] == "M":
                modified.append(line[3:])

        clean = len(staged) == 0 and len(modified) == 0 and len(untracked) == 0
        return {
            "branch": branch,
            "clean": clean,
            "staged": staged,
            "modified": modified,
            "untracked": untracked,
        }

    def git_log(self, cwd: str, limit: int = 10) -> list[dict]:
        result = self._run_git(cwd, "log", f"--max-count={limit}", "--format=%H|||%an|||%ai|||%s")
        entries = []
        for line in result.stdout.strip().split("\n"):
            if "|||" in line:
                parts = line.split("|||")
                entries.append(
                    {
                        "hash": parts[0][:8],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3],
                    }
                )
        return entries

    def git_diff(self, cwd: str) -> str:
        result = self._run_git(cwd, "diff")
        return result.stdout

    def git_add(self, cwd: str, files: list[str]) -> bool:
        self._run_git(cwd, "add", *files)
        return True

    def git_commit(self, cwd: str, message: str) -> bool:
        result = self._run_git(cwd, "commit", "-m", message)
        if result.returncode != 0:
            raise ShellExecutionError(f"Git commit failed: {result.stderr}")
        return True

    def git_branches(self, cwd: str) -> list[str]:
        result = self._run_git(cwd, "branch", "--format=%(refname:short)")
        return [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
