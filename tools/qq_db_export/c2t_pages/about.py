"""页：关于（版本、数据目录、许可）。"""

from __future__ import annotations


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["about"] = page
    try:
        from todoseine import __version__ as ver
    except Exception:  # noqa: BLE001
        ver = "—"
    for icon, title, sub in (
        ("◈", "TodoSeine", f"版本 {ver} · 本地优先的聊天记录待办提取器"),
        ("💾", "数据目录", str(ctx.root)),
        ("📄", "许可", "MIT License"),
    ):
        app._card_row(p, page, icon, title, sub)
