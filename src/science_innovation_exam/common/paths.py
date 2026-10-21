from pathlib import PurePosixPath
from .errors import ContractError


def require_safe_relative(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("%s must be a non-empty POSIX relative path" % field)
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise ContractError("%s is not a normalized safe relative path" % field)
    return path
