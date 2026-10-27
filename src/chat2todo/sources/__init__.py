"""可选数据源：目前只有数据库只读通道。"""

from .db import (  # noqa: F401
    BuiltinBackend,
    CommunityBackend,
    DBInfo,
    fetch_group_names,
    get_backend,
    inspect,
    load_messages,
    locate,
    resolve_key,
)
