import re
from typing import Any, Mapping, Sequence

from .errors import ContractError


SECRET_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|auth|credential|password|private[_-]?key|secret|token)([_-]|$)",
    re.IGNORECASE,
)


def is_secret_name(value: str) -> bool:
    return bool(SECRET_KEY.search(value))


def reject_secret_keys(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key)
            if SECRET_KEY.search(label):
                raise ContractError("secret-bearing field is forbidden in persisted config: %s.%s" % (path, label))
            reject_secret_keys(item, "%s.%s" % (path, label))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            reject_secret_keys(item, "%s[%d]" % (path, index))
