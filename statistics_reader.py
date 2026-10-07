"""Read the configured backend's lightweight statistics without importing its runtime."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from datetime import date
from pathlib import Path

from oas_client import UpstreamError, account_identifier, statistics_date


class LocalStatisticsReader:
    def __init__(self, backend_root: Path):
        root = backend_root.resolve()
        module_path = root / "module" / "server" / "log_stats.py"
        if module_path.resolve().parent != (root / "module" / "server").resolve() or not module_path.is_file():
            raise RuntimeError("Configured backend statistics module is missing")
        self.root = root
        self.module_path = module_path
        self._lock = threading.RLock()
        self._signature = None
        self._module_name = f"_oas_panel_readonly_log_statistics_{id(self):x}"
        self.input_error = ValueError
        self._reload_if_changed()

    def _reload_if_changed(self):
        # OAS and the panel are independent processes. An updated parser must
        # also replace the panel's service, including already parsed log caches.
        info = self.module_path.stat()
        signature = (info.st_mtime_ns, info.st_size, info.st_ino)
        if signature == self._signature:
            return
        source = self.module_path.read_bytes()
        after = self.module_path.stat()
        if (after.st_mtime_ns, after.st_size, after.st_ino) != signature:
            raise OSError("Statistics parser changed while being read")
        # A standalone module name avoids module.server.__init__, which loads
        # the OAS logger. The file itself has only standard-library imports.
        name = self._module_name
        spec = importlib.util.spec_from_file_location(name, self.module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the statistics reader")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(name)
        sys.modules[name] = module
        try:
            # Compile these exact bytes rather than accepting a .pyc built for
            # a same-sized edit made within the same timestamp second.
            exec(compile(source, str(self.module_path), "exec"), module.__dict__)
            service = module.LogStatsService(self.root)
        except BaseException:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
            raise
        self.service = service
        self.input_error = module.StatisticsInputError
        self._signature = signature

    def _read_sync(self, method: str, *args):
        with self._lock:
            try:
                self._reload_if_changed()
                return getattr(self.service, method)(*args)
            except (self.input_error, OSError, SyntaxError, ImportError) as exc:
                raise UpstreamError("无法读取此配置的日志统计，请刷新重试") from exc

    async def _read(self, method: str, *args):
        return await asyncio.to_thread(self._read_sync, method, *args)

    async def statistics_dates(self, account: str) -> dict:
        return await self._read("list_available_dates", account_identifier(account))

    async def statistics_day(self, account: str, day: str) -> dict:
        return await self._read("build_stats", account_identifier(account), date.fromisoformat(statistics_date(day)))
