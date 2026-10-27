"""The service registry (services.yaml) and the dependency graph built from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = ROOT / "services.yaml"


@dataclass(frozen=True)
class Service:
    name: str
    path: str
    port: int
    depends_on: tuple[str, ...] = ()
    owner: str = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class Registry:
    services: dict[str, Service]
    shared_paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_REGISTRY) -> Registry:
        raw = yaml.safe_load(Path(path).read_text())
        services = {
            name: Service(name=name, path=s["path"], port=int(s["port"]),
                          depends_on=tuple(s.get("depends_on") or ()), owner=s.get("owner", ""))
            for name, s in raw["services"].items()
        }
        reg = cls(services=services, shared_paths=list(raw.get("shared_paths") or []))
        reg.validate()
        return reg

    def validate(self) -> None:
        for svc in self.services.values():
            unknown = set(svc.depends_on) - set(self.services)
            if unknown:
                raise ValueError(f"{svc.name} depends on unknown services: {sorted(unknown)}")
        self.deploy_order()  # raises on cycles

    def __getitem__(self, name: str) -> Service:
        return self.services[name]

    def __iter__(self):
        return iter(self.services.values())

    @cached_property
    def _callers(self) -> dict[str, set[str]]:
        callers: dict[str, set[str]] = {name: set() for name in self.services}
        for svc in self.services.values():
            for dep in svc.depends_on:
                callers[dep].add(svc.name)
        return callers

    def callers_of(self, name: str, transitive: bool = True) -> set[str]:
        """Services that call `name`, directly or (by default) through others.
        A change to `name` can break any of them."""
        direct = self._callers[name]
        if not transitive:
            return set(direct)
        seen: set[str] = set()
        stack = list(direct)
        while stack:
            s = stack.pop()
            if s not in seen:
                seen.add(s)
                stack.extend(self._callers[s])
        return seen

    def dependencies_of(self, name: str, transitive: bool = True) -> set[str]:
        direct = set(self.services[name].depends_on)
        if not transitive:
            return direct
        seen: set[str] = set()
        stack = list(direct)
        while stack:
            s = stack.pop()
            if s not in seen:
                seen.add(s)
                stack.extend(self.services[s].depends_on)
        return seen

    def deploy_order(self) -> list[str]:
        """Dependencies before their callers, so a caller is never deployed
        against a dependency version it hasn't been tested with. Ties are
        broken alphabetically for a stable order."""
        remaining = {n: set(s.depends_on) for n, s in self.services.items()}
        order: list[str] = []
        while remaining:
            ready = sorted(n for n, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError(f"dependency cycle among {sorted(remaining)}")
            for n in ready:
                order.append(n)
                del remaining[n]
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    def service_for_port(self, port: int) -> str | None:
        return next((s.name for s in self.services.values() if s.port == port), None)

    def service_for_path(self, path: str) -> str | None:
        for svc in self.services.values():
            if path == svc.path or path.startswith(svc.path.rstrip("/") + "/"):
                return svc.name
        return None

    def is_shared_path(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.shared_paths)
