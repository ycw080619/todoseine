"""桌面悬浮看板（tkinter，标准库实现，零第三方依赖）。

功能（对齐主流待办 App 的常见交互）：
- 顶部筛选标签：待办 / 待定 / 今日 / 完成
- 内联快速添加：底部输入框回车即入库，不用弹窗
- 滚轮翻页列表，Ctrl+滚轮调透明度
- 迷你模式：收起成小时钟胶囊，点一下展开
- 置顶开关、撤销上一步（Ctrl+Z）、右键菜单（完成 / 非待办 / 恢复 / 看原文）
- 到期提醒：快到期 / 刚过期的条目在右下角弹卡片，点击唤出看板（[dash] remind 可关）

设计约束（与方案第五章一致）：
- 只读数据库；勾选/清空时才写回状态，不改原文；
- 置顶常显，但不做任何键鼠模拟，也不主动抢焦点；
- 无图形环境时给出 E3001 与建议，不抛栈。

代码组织（原单文件 dashboard.py 拆分而来）：
- app.py    窗口搭建与渲染（run / _build）、外观配置与热重载
- data.py   从库里挑要显示的条目（load_items）
- files.py  时间格式化、识别并定位 QQ 下载的文件
- geom.py   多显示器工作区 / 钳位 / 吸附 / 锚点（纯函数，可单测）
- win32.py  单实例、全局热键、窗口层级、毛玻璃（ctypes 直调）
"""

from .app import (ACCENT, ACCENTS, ACTIVE, ANIM_ON, BLUR_ON, BORDER, BORDER_ON,
                  BORDER_W, FIELD, FOOT, H, H_MINI, HOVER_ON, KEY, LINE, PAD,
                  PANEL, RADIUS, RESIZE_MARGIN, ROW, SHOW_CLOCK, SNAP_ON, SUB,
                  TABS, TEXT, TOP, W, W_MAX, W_MIN, W_MINI, _apply_ui_config,
                  _build, _config_mtime, _draw_bg, _pos_file, _rounded,
                  _ui_config, load_pos, run, save_pos)
from .data import _is_today_due, due_remindable, filter_items, load_items
from .files import (_FILE_EXTS, _documents_dir, _extract_filenames,
                    _find_downloaded_file, _fmt_ts)
from .geom import (SNAP_PX, _anchor_of, _apply_anchor, _clamp_to_rects,
                   _ease_out_cubic, _monitor_work_areas, _slide_offset,
                   _snap_to_edges)
from .win32 import (_apply_blur, _hwnd_of, _set_layer, _single_instance,
                    _start_hotkey)
