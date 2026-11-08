#!/usr/bin/env python3
"""把本机 DSH 会话记录（~/.dsh/sessions/**/session.v4.jsonl.zstd）转成
memory-starmap 的 jsonl 数据源。

只取两类内容：
  - user/message   用户自己说过的话（kind=think，星图上最亮的一层）
  - assistant/message 里的纯文本回复（kind=answer，去掉思维链与工具调用）

用法：
  python3 tools/dsh_to_jsonl.py                       # 输出 data/dsh-memories.jsonl
  python3 tools/dsh_to_jsonl.py --since 2026-09-01    # 只看某天之后
  python3 tools/dsh_to_jsonl.py --out /tmp/x.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from zoneinfo import ZoneInfo

try:
    import zstandard as zstd
except ImportError:
    sys.exit("需要 zstandard：python3 -m pip install zstandard")

SESSIONS = os.path.expanduser("~/.dsh/sessions")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
TZ = ZoneInfo("Asia/Shanghai")

# 明显不是「记忆」的壳：图片占位、压缩标记、定时任务壳、系统提醒
DROP_PAT = re.compile(
    r"\[Image:|\[图片|\[图[:：]|<system_reminder>|\[压缩备忘|<image_caption>|"
    r"^\s*\[CronJob\]|/clear\b|^\s*$"
)
CODE_FENCE = re.compile(r"```")
CJK = re.compile(r"[\u4e00-\u9fff]")


def day_of(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


def text_blocks(content) -> str:
    """只拼接 type=text 的块，跳过 reasoning / tool_use / image。"""
    if isinstance(content, str):
        return content
    out = []
    for blk in content or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            out.append(blk.get("text") or "")
    return "\n".join(out)


def accept(text: str, role: str) -> bool:
    t = text.strip()
    if len(t) < (24 if role == "user" else 60):
        return False
    if DROP_PAT.search(t):
        return False
    if len(CODE_FENCE.findall(t)) >= 2:      # 整段代码，不进星图
        return False
    ascii_ratio = sum(1 for c in t if ord(c) < 128) / max(len(t), 1)
    if ascii_ratio > 0.85 and not CJK.search(t):   # 纯英文日志/报错
        return False
    return True


def clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/dsh-memories.jsonl")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD，只要这之后的消息")
    ap.add_argument("--max-per-session", type=int, default=400)
    ap.add_argument("--no-claude", dest="claude", action="store_false",
                    help="只读 DSH 会话，不读 ~/.claude/projects")
    args = ap.parse_args()

    since = dt.date.fromisoformat(args.since) if args.since else None
    dctx = zstd.ZstdDecompressor()
    seen: set[str] = set()
    rows: list[dict] = []
    kinds: Counter = Counter()

    for root_dir in sorted(os.listdir(SESSIONS)):
        walk_root = os.path.join(SESSIONS, root_dir)
        if not os.path.isdir(walk_root):
            continue
        for dirpath, _dirnames, filenames in os.walk(walk_root):
            for fn in filenames:
                if not fn.endswith(".jsonl.zstd"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "rb") as fh:
                        raw = dctx.stream_reader(fh).read()
                except Exception as exc:            # 坏档直接跳过，别让整次构建失败
                    print(f"[skip] {path}: {exc}", file=sys.stderr)
                    continue
                kept = 0
                for line in raw.decode("utf-8", "replace").splitlines():
                    if kept >= args.max_per_session:
                        break
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    etype = ev.get("type")
                    if etype not in ("user/message", "assistant/message"):
                        continue
                    data = ev.get("data") or {}
                    msg = data.get("message") if etype == "assistant/message" else data
                    if not isinstance(msg, dict):
                        continue
                    if etype == "user/message":
                        src = (data.get("source") or {}).get("kind")
                        if src not in (None, "user"):   # 排除 tool-result / system 注入
                            continue
                        role, kind, weight = "user", "msg", 0.88
                    else:
                        role, kind, weight = "assistant", "fact", 0.62
                    text = text_blocks(msg.get("content"))
                    if not accept(text, role):
                        continue
                    day = day_of(ev.get("time") or 0)
                    if since and dt.date.fromisoformat(day) < since:
                        continue
                    full = clip(text, 400)
                    norm = re.sub(r"\W+", "", full)[:80]
                    if norm in seen:
                        continue
                    seen.add(norm)
                    sid = (temp := os.path.basename(dirpath))[:24]
                    rows.append(
                        {
                            "id": f"{sid}-{ev.get('seq')}",
                            "day": day,
                            "kind": kind,
                            "text": full,
                            "evidence": clip(text, 120),
                            "weight": round(weight + min(len(full), 400) / 4000, 3),
                        }
                    )
                    kinds[kind] += 1
                    kept += 1

    # ---- 第二来源：Claude Code 的历史 transcript（同一批人的另一段记忆）----
    if args.claude and os.path.isdir(CLAUDE_PROJECTS):
        for dirpath, _dirnames, filenames in os.walk(CLAUDE_PROJECTS):
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                kept = 0
                try:
                    fh = open(path, encoding="utf-8", errors="replace")
                except OSError as exc:
                    print(f"[skip] {path}: {exc}", file=sys.stderr)
                    continue
                with fh:
                    for line in fh:
                        if kept >= args.max_per_session:
                            break
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        etype = ev.get("type")
                        if etype not in ("user", "assistant") or ev.get("isMeta"):
                            continue
                        if ev.get("isSidechain"):
                            continue
                        msg = ev.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            if any(
                                isinstance(b, dict) and b.get("type") in ("tool_result", "image")
                                for b in content
                            ):
                                continue
                        if etype == "user":
                            role, kind, weight = "user", "msg", 0.88
                        else:
                            role, kind, weight = "assistant", "fact", 0.62
                        text = text_blocks(content)
                        if not accept(text, role):
                            continue
                        ts = ev.get("timestamp") or ""
                        try:
                            day = dt.datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            ).astimezone(TZ).strftime("%Y-%m-%d")
                        except ValueError:
                            continue
                        if since and dt.date.fromisoformat(day) < since:
                            continue
                        full = clip(text, 400)
                        norm = re.sub(r"\W+", "", full)[:80]
                        if norm in seen:
                            continue
                        seen.add(norm)
                        rows.append(
                            {
                                "id": f"cc-{os.path.basename(path)[:8]}-{kept}",
                                "day": day,
                                "kind": kind,
                                "text": full,
                                "evidence": clip(text, 120),
                                "weight": round(weight + min(len(full), 400) / 4000, 3),
                            }
                        )
                        kinds[kind] += 1
                        kept += 1

    rows.sort(key=lambda r: (r["day"], r["id"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    days = len({r["day"] for r in rows})
    print(f"wrote {len(rows)} memories -> {args.out}  ({days} 天, {dict(kinds)})")
    if rows:
        print(f"day range: {rows[0]['day']} .. {rows[-1]['day']}")


if __name__ == "__main__":
    main()
