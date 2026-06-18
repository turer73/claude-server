"""File manager — safe file CRUD with path traversal prevention."""

from __future__ import annotations

import glob
import os
from datetime import datetime

from app.exceptions import AuthorizationError, NotFoundError


class FileManager:
    def __init__(self, allowed_paths: list[str], max_file_size_mb: int = 10) -> None:
        # NOT: rstrip("/") tek başına "/" kökünü "" yapıp realpath("")=cwd'ye
        # düşürüyordu (allowed_paths=["/"] sessizce cwd'ye iniyordu). "or os.sep"
        # ile kök korunur; ayraç-sınırlı alt-yol kontrolü aşağıda kökü ayrıca ele alır.
        self._allowed = [os.path.realpath(p.rstrip("/") or os.sep) for p in allowed_paths]
        self._max_size = max_file_size_mb * 1024 * 1024

    def validate_path(self, path: str) -> str:
        real = os.path.realpath(path)
        for allowed in self._allowed:
            # GÜVENLIK: salt-prefix BUG'lıydı — /tmp/foo izinliyse /tmp/foobar/secret de
            # geçerdi (sibling-prefix). Tam-eşleşme VEYA ayraç-sınırlı alt-yol şart.
            # Kök ("/"): allowed+os.sep "//" olur, startswith tutmaz → ayrıca ele al.
            if allowed == os.sep or real == allowed or real.startswith(allowed + os.sep):
                return real
        raise AuthorizationError(f"Path {path} not in allowed paths")

    def read_file(self, path: str, offset: int = 0, limit: int = 1000) -> dict:
        path = self.validate_path(path)
        if not os.path.isfile(path):
            raise NotFoundError(f"File not found: {path}")
        with open(path, errors="replace") as f:
            lines = f.readlines()
        selected = lines[offset : offset + limit]
        content = "".join(selected)
        return {
            "path": path,
            "content": content,
            "size": os.path.getsize(path),
            "lines": len(lines),
        }

    def write_file(self, path: str, content: str, mode: str = "write") -> dict:
        path = self.validate_path(path)
        write_mode = "a" if mode == "append" else "w"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, write_mode) as f:
            f.write(content)
        return {"path": path, "size": os.path.getsize(path)}

    def edit_file(self, path: str, old_string: str, new_string: str) -> dict:
        path = self.validate_path(path)
        if not os.path.isfile(path):
            raise NotFoundError(f"File not found: {path}")
        with open(path) as f:
            content = f.read()
        if old_string not in content:
            raise NotFoundError("String not found in file")
        content = content.replace(old_string, new_string, 1)
        with open(path, "w") as f:
            f.write(content)
        return {"path": path, "size": os.path.getsize(path)}

    def delete_file(self, path: str) -> bool:
        path = self.validate_path(path)
        if not os.path.exists(path):
            raise NotFoundError(f"File not found: {path}")
        os.remove(path)
        return True

    def list_directory(self, path: str) -> list[dict]:
        path = self.validate_path(path)
        if not os.path.isdir(path):
            raise NotFoundError(f"Directory not found: {path}")
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                entries.append(
                    {
                        "path": full,
                        "size": st.st_size,
                        "is_dir": os.path.isdir(full),
                        "permissions": oct(st.st_mode)[-3:],
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                        "owner": str(st.st_uid),
                    }
                )
            except OSError:
                continue
        return entries

    def get_file_info(self, path: str) -> dict:
        path = self.validate_path(path)
        if not os.path.exists(path):
            raise NotFoundError(f"Path not found: {path}")
        st = os.stat(path)
        return {
            "path": path,
            "size": st.st_size,
            "is_dir": os.path.isdir(path),
            "permissions": oct(st.st_mode)[-3:],
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "owner": str(st.st_uid),
        }

    def search_files(self, path: str, pattern: str, max_results: int = 50) -> list[str]:
        path = self.validate_path(path)
        results = glob.glob(os.path.join(path, "**", pattern), recursive=True)
        return results[:max_results]
