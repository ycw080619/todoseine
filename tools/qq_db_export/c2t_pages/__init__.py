"""控制台分页（从 console.py 拆出）。

每个模块只暴露 `build(app, ctx)`：
- app：Console 实例，提供 `_pal / _card_row / _pill / _new_page / write` 等界面与动作；
- ctx：主程序注入的助手函数与常量（读配置、写配置、路径等）。

页面模块不 import console，避免循环导入；所有对主程序状态的访问都走 ctx，
因此测试里替换 CONFIG / ROOT 依旧生效（函数仍在 console 模块里取值）。
"""
