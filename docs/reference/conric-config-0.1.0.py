"""Explicit YAML composition; validation and provider policy belong to callers."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, TypeVar, cast

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, DocumentStartEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

KeyPath: TypeAlias = tuple[str | int, ...]
_T = TypeVar("_T")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_FLOAT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_TAG = "tag:yaml.org,2002:"
_FILE_BYTES = 1024 * 1024
_TOTAL_BYTES = 8 * _FILE_BYTES
_VISITS = 64
_DEPTH = 64


@dataclass(frozen=True)
class Source:
    """Resolved file and one-based line/column of a configuration node."""

    file: Path
    line: int
    column: int


@dataclass(frozen=True, repr=False)
class SecretRef:
    """An unresolved environment reference. No environment value is ever read."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ENV.fullmatch(self.name):
            raise ValueError("invalid environment reference name")

    def __repr__(self) -> str:
        return "SecretRef(<redacted>)"


Value: TypeAlias = None | bool | int | float | str | SecretRef | list["Value"] | dict[str, "Value"]


def _path(value: KeyPath) -> KeyPath:
    if not isinstance(value, tuple) or any(
        type(part) not in (str, int) or (type(part) is int and part < 0) for part in value
    ):
        raise ValueError("path must be a tuple of string keys and nonnegative indices")
    return value


class SchemaError(ValueError):
    """Leaf rejection with a relative path and a nonsecret snake_case reason token."""

    def __init__(self, reason: str = "invalid_value", *, path: KeyPath = ()) -> None:
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise ValueError("schema reason must be a nonsecret snake_case token")
        self.path = _path(path)
        self.reason = reason
        super().__init__(reason)


class ConfigError(ValueError):
    """Stable category and source location, without source text or scalar values."""

    def __init__(
        self,
        code: str,
        *,
        path: KeyPath = (),
        source: Source | None = None,
        reason: str | None = None,
    ) -> None:
        self.code, self.path, self.source, self.reason = code, path, source, reason
        location = f"{source.file}:{source.line}:{source.column}: " if source else ""
        detail = f" ({reason})" if reason else ""
        super().__init__(f"{location}{code}{detail}; path={path!r}")


def _redact(value: Value) -> Value:
    if isinstance(value, SecretRef):
        return "<redacted>"
    if isinstance(value, dict):
        return {key: _redact(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


@dataclass(frozen=True, repr=False)
class Config:
    """A loaded document. Public data access returns detached copies."""

    _data: dict[str, Value] = field(default_factory=dict)
    _sources: dict[KeyPath, Source] = field(default_factory=dict)

    @property
    def data(self) -> dict[str, Value]:
        return deepcopy(self._data)

    def source(self, path: KeyPath = ()) -> Source | None:
        """Return the exact winning location, or None for an absent path."""
        return self._sources.get(_path(path))

    def _nearest(self, path: KeyPath) -> Source | None:
        while path and path not in self._sources:
            path = path[:-1]
        return self._sources.get(path)

    def redacted(self) -> dict[str, Value]:
        """Detached inspection data. Only explicitly tagged references are redacted."""
        return cast(dict[str, Value], _redact(self._data))

    def __repr__(self) -> str:
        return f"Config({self.redacted()!r})"

    def validate(self, validator: Callable[[Value], _T], *, path: KeyPath = ()) -> _T:
        """Apply a trusted leaf callable to a detached subtree, attributing failures."""
        try:
            _path(path)
        except ValueError:
            raise ConfigError("input") from None
        value: Value = self.data
        for index, part in enumerate(path):
            if isinstance(value, dict) and isinstance(part, str) and part in value:
                value = value[part]
            elif isinstance(value, list) and type(part) is int and part < len(value):
                value = value[part]
            else:
                raise ConfigError(
                    "missing_path", path=path, source=self._nearest(path[: index + 1])
                ) from None
        try:
            return validator(value)
        except SchemaError as exc:
            target = path + exc.path
            raise ConfigError(
                "validation", path=target, source=self._nearest(target), reason=exc.reason
            ) from None
        except Exception:
            # Context remains available programmatically; displayed diagnostics omit it.
            raise ConfigError("validation", path=path, source=self._nearest(path)) from None


class _Loader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict[str | None, list[tuple[str, re.Pattern[str]]]] = {}


_Loader.add_implicit_resolver(_TAG + "null", re.compile(r"null\Z"), ["n"])
_Loader.add_implicit_resolver(_TAG + "bool", re.compile(r"(?:true|false)\Z"), ["t", "f"])
_Loader.add_implicit_resolver(_TAG + "int", _INT, list("-0123456789"))
_Loader.add_implicit_resolver(_TAG + "float", _FLOAT, list("-0123456789"))


def _source(file: Path, mark: object) -> Source:
    return Source(file, getattr(mark, "line", 0) + 1, getattr(mark, "column", 0) + 1)


def _scalar(node: ScalarNode, source: Source, path: KeyPath) -> Value:
    text, tag = cast(str, node.value), node.tag
    if tag == _TAG + "str":
        return text
    if tag == "!env":
        if _ENV.fullmatch(text):
            return SecretRef(text)
    elif tag == _TAG + "null":
        if text == "null":
            return None
    elif tag == _TAG + "bool":
        if text in ("true", "false"):
            return text == "true"
    elif tag == _TAG + "int":
        if _INT.fullmatch(text):
            try:
                return int(text)
            except ValueError:
                pass
    elif tag == _TAG + "float":
        if text.lower() in (".inf", "-.inf", "+.inf", ".nan"):
            raise ConfigError("non_finite", path=path, source=source)
        if _FLOAT.fullmatch(text):
            value = float(text)
            if not math.isfinite(value):
                raise ConfigError("non_finite", path=path, source=source)
            return value
    else:
        raise ConfigError("unsupported_tag", path=path, source=source)
    raise ConfigError("invalid_scalar", path=path, source=source)


def _decode(node: Node, file: Path, path: KeyPath, sources: dict[KeyPath, Source]) -> Value:
    source = _source(file, node.start_mark)
    sources[path] = source
    if isinstance(node, ScalarNode):
        return _scalar(node, source, path)
    if isinstance(node, SequenceNode) and node.tag == _TAG + "seq":
        return [_decode(child, file, path + (i,), sources) for i, child in enumerate(node.value)]
    if isinstance(node, MappingNode) and node.tag == _TAG + "map":
        result: dict[str, Value] = {}
        for key, child in node.value:
            if not isinstance(key, ScalarNode) or key.tag != _TAG + "str":
                raise ConfigError("invalid_scalar", path=path, source=_source(file, key.start_mark))
            if key.value == "<<":
                raise ConfigError("merge_key", path=path, source=_source(file, key.start_mark))
            if key.value in result:
                raise ConfigError("duplicate_key", path=path, source=_source(file, key.start_mark))
            result[key.value] = _decode(child, file, path + (key.value,), sources)
        return result
    raise ConfigError("unsupported_tag", path=path, source=source)


def _parse(raw: bytes, file: Path) -> Config:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ConfigError("invalid_utf8", source=Source(file, 1, 1)) from None
    try:
        depth = 0
        document_source = Source(file, 1, 1)
        for event in yaml.parse(text, Loader=_Loader):
            source = _source(file, event.start_mark)
            if isinstance(event, DocumentStartEvent):
                document_source = source
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
                raise ConfigError("alias", source=source)
            if isinstance(event, CollectionStartEvent):
                depth += 1
                if depth > _DEPTH:
                    raise ConfigError("too_deep", source=source)
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
        node = yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ConfigError(
            "yaml", source=_source(file, getattr(exc, "problem_mark", None))
        ) from None
    except RecursionError:
        raise ConfigError("too_deep", source=Source(file, 1, 1)) from None
    if not isinstance(node, MappingNode):
        raise ConfigError("not_mapping", source=Source(file, 1, 1))
    sources: dict[KeyPath, Source] = {}
    value = _decode(node, file, (), sources)
    sources[()] = document_source
    return Config(cast(dict[str, Value], value), sources)


def compose(*configs: Config) -> Config:
    """Merge mappings recursively in order; replace all other values, including null."""
    data: dict[str, Value] = {}
    sources: dict[KeyPath, Source] = {}

    def subtree(value: Value, path: KeyPath) -> Iterator[KeyPath]:
        yield path
        children = (
            value.items()
            if isinstance(value, dict)
            else enumerate(value)
            if isinstance(value, list)
            else ()
        )
        for key, child in children:
            yield from subtree(child, path + (key,))

    def merge(
        left: dict[str, Value],
        right: dict[str, Value],
        path: KeyPath,
        incoming: dict[KeyPath, Source],
    ) -> None:
        if path in incoming:
            sources[path] = incoming[path]
        for key, value in right.items():
            target = path + (key,)
            existing = left.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merge(existing, value, target, incoming)
            else:
                if key in left:
                    for old in subtree(existing, target):
                        sources.pop(old, None)
                left[key] = deepcopy(value)
                for current in subtree(value, target):
                    if current in incoming:
                        sources[current] = incoming[current]

    for config in configs:
        if not isinstance(config, Config):
            raise ConfigError("input")
        merge(data, config._data, (), config._sources)
    return Config(data, sources)


def load(paths: Sequence[str | Path], *, root: str | Path | None = None) -> Config:
    """Load explicit confined files, applying included bases before each file's own data."""
    if isinstance(paths, (str, bytes, Path)) or not paths:
        raise ConfigError("input")
    try:
        files = [Path(file).resolve() for file in paths]
        boundary = Path(root).resolve() if root is not None else files[0].parent
    except (OSError, TypeError, ValueError, RuntimeError):
        raise ConfigError("input") from None
    if not boundary.is_dir():
        raise ConfigError("root", source=Source(boundary, 1, 1))
    visits, total_bytes = 0, 0

    def visit(file: Path, stack: tuple[Path, ...], reference: Source | None = None) -> Config:
        nonlocal visits, total_bytes
        try:
            file = file.resolve()
        except (OSError, ValueError, RuntimeError):
            raise ConfigError("io", source=reference) from None
        location = reference or Source(file, 1, 1)
        if not file.is_relative_to(boundary):
            raise ConfigError("include_escape", source=location)
        if file in stack:
            raise ConfigError("include_cycle", source=location)
        visits += 1
        if visits > _VISITS:
            raise ConfigError("too_many_files", source=location)
        try:
            with file.open("rb") as handle:
                raw = handle.read(_FILE_BYTES + 1)
        except (OSError, ValueError):
            raise ConfigError("io", source=location) from None
        total_bytes += len(raw)
        if len(raw) > _FILE_BYTES or total_bytes > _TOTAL_BYTES:
            raise ConfigError("too_large", source=location)
        document = _parse(raw, file)
        own = document.data
        includes = own.pop("include", [])
        include_source = document.source(("include",))
        if not isinstance(includes, list):
            raise ConfigError("include_invalid", path=("include",), source=include_source)
        bases: list[Config] = []
        for index, name in enumerate(includes):
            source = document.source(("include", index))
            if not isinstance(name, str) or not name or Path(name).is_absolute():
                raise ConfigError("include_invalid", path=("include", index), source=source)
            bases.append(visit(file.parent / name, stack + (file,), source))
        sources = {p: s for p, s in document._sources.items() if not p or p[0] != "include"}
        return compose(*bases, Config(own, sources))

    return compose(*(visit(file, ()) for file in files))
