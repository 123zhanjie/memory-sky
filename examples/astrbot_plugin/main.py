"""AstrBot 插件骨架：把 Memory StarMap 挂进来。

这份是「示范写法」，不是能直接装进别人机器的成品——数据源、路径、截图后端都得按你的环境改。
三个要点（README「接入 AstrBot 插件」有展开）：
  1) 构建放后台，别塞进消息处理路径；
  2) 前端与 starmap.json 必须同源，一起放 views/starmap/；
  3) 重算判据是 fingerprint 变了，不是「执行成功」。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, MessageChain, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).parent
WEB_DIR = PLUGIN_DIR / "views" / "starmap"
DATA_FILE = WEB_DIR / "starmap.json"
STATE_FILE = PLUGIN_DIR / "build_state.json"
BUILDER = Path(os.environ.get("STARMAP_BUILDER", PLUGIN_DIR / "builder.py"))
SOURCES = Path(os.environ.get("STARMAP_SOURCES", PLUGIN_DIR / "sources.json"))
BUILD_TIMEOUT = int(os.environ.get("STARMAP_BUILD_TIMEOUT", "900"))


def read_fingerprint() -> str:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f).get("fingerprint") or ""
    except (OSError, ValueError):
        return ""


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@register("memory_starmap", "记忆星图", "把长期记忆渲染成可漫游的三维星图", "0.1.0", "")
class MemoryStarMap(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None

    # ---------------- 构建 ----------------

    async def _build(self, force: bool = False) -> tuple[bool, str]:
        """返回 (是否有变化, 说明)。真构建在子进程里跑，不阻塞事件循环。"""
        if self._task and not self._task.done():
            return False, "上一轮还在跑"
        if not force and DATA_FILE.exists():
            age = time.time() - DATA_FILE.stat().st_mtime
            if age < 3600:
                return False, f"产物 {int(age // 60)} 分钟前刚建过"

        before = read_fingerprint()
        self._task = asyncio.create_task(self._run_subprocess())
        await self._task

        after = read_fingerprint()
        changed = bool(after) and after != before
        if changed:
            state = read_state()
            state.update({"fingerprint": after, "built_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            write_state(state)
            return True, "星图已更新"
        return False, "内容没变化，沿用上一版"

    async def _run_subprocess(self) -> None:
        cmd = [sys.executable, str(BUILDER), "--sources", str(SOURCES), "--out", str(DATA_FILE)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=BUILD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[starmap] 构建超时 %ss，已中止", BUILD_TIMEOUT)
            return
        tail = (out or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
        logger.info("[starmap] 构建结束 rc=%s %s", proc.returncode, " | ".join(tail))

    # ---------------- 对外命令 ----------------

    @filter.command("星图")
    async def starmap_rebuild(self, event: AstrMessageEvent):
        """重建并回一张预览图。"""
        yield event.plain_result("在算了，几千个点要一两分钟，别催。")
        changed, msg = await self._build(force=True)
        shot = await self._screenshot()
        if shot:
            yield event.chain_result([Image(file=shot), Plain(msg)])
        else:
            yield event.plain_result(msg)

    @filter.command("星图更新")
    async def starmap_soft(self, event: AstrMessageEvent):
        """只在内容真的变了才说一声。"""
        changed, msg = await self._build(force=False)
        yield event.plain_result(msg if changed or "跑过" in msg else "没变化")

    @filter.command("星图状态")
    async def starmap_status(self, event: AstrMessageEvent):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            st = d.get("stats", {})
            yield event.plain_result(
                f"节点 {st.get('nodes')} · 连线 {st.get('links')} · 天数 {st.get('days')}\n"
                f"布局 {d.get('layout_by')} · 指纹 {d.get('fingerprint')}\n"
                f"构建 {d.get('generated_at')}（{d.get('build_seconds')}s）")
        except (OSError, ValueError):
            yield event.plain_result("还没有产物，先用 /星图 建一份")

    # ---------------- 截图（按你的环境替换） ----------------

    async def _screenshot(self) -> str:
        """用 CDP 打开本地页面截图。返回路径或空串。

        这里只写调用姿势，不打包截图后端：可用的方案有 playwright、
        或者直接连一个带 --remote-debugging-port 的浏览器。
        """
        out = PLUGIN_DIR / "preview.png"
        url = os.environ.get("STARMAP_PREVIEW_URL", "http://127.0.0.1:8101/plugins/memory_starmap/")
        script = Path(os.environ.get("STARMAP_SHOT_SCRIPT", ""))
        if script.is_file():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), url, str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        return str(out) if out.exists() else ""

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
