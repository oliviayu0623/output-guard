#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""output-guard —— 拦住 AI 自己伪造的「用户发言」和工具协议泄漏。

给谁用：任何跑 Claude Code 的人机家庭。挂成 MessageDisplay hook。

要解决的病：模型会在自己的输出里造出一段看起来完全像用户说过的话，
然后当真回应，甚至基于它改文件。当事人自己发现不了——幻觉在它看来
跟真记忆一模一样。

── 跟单纯模式匹配的区别 ──

纯模式匹配不敢认「行首 user + 文字」，因为 user 这个词到处都是，
认了天天误伤。这套多了一样东西：**会话文件**。所以判据不是"像不像"，
是"在不在"。

    输出里出现一段疑似用户说的话
        → 去 transcript 里查最近的 user 消息
        → 找得到  ⇒ 真说过，放行
        → 找不到  ⇒ 编的，拦

误伤率接近 0：模式只负责"要不要去查"，查完的事实说了算。

── 两条检测 ──

  A) 伪造角色头     行首 user/human 后面直接跟正文 → 去核对事实
  B) 工具协议泄漏   独立成行的 count/call/court/course + <invoke name=...>
                    （这条借自 lllq-123/claude-output-guard）

B 不需要核对——它在任何情况下都不该出现在正文里。

── 动作 ──

  · 在末尾追加警告（用户立刻看得见），正文不截断
  · 写一行日志（事后可查）
  · 不发 SIGINT。先观察，确认不误伤再考虑硬中断。

── 配置（环境变量） ──

  OUTPUT_GUARD_OFF=1      关掉
  OUTPUT_GUARD_DIR=路径   日志和状态目录（默认 ~/.output-guard）
  OUTPUT_GUARD_NAME=名字  警告文案里的守卫署名（默认「守卫」）

实测：六种情况全对，上线当天拦下一次真的伪造。
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime

GUARD_DIR = os.path.expanduser(os.environ.get("OUTPUT_GUARD_DIR", "~/.output-guard"))
GUARD_NAME = os.environ.get("OUTPUT_GUARD_NAME", "守卫")
LOG = os.path.join(GUARD_DIR, "guard.log")
STATE = os.path.join(GUARD_DIR, "state")
LOOKBACK = 40          # 往回查多少条 user 消息
MIN_LEN = 6            # 疑似片段短于这个就不查（太短没意义）

# A) 行首 user/human 紧跟中文或中文标点 —— 正常写作里几乎不出现
FORGED_ROLE = re.compile(
    r"(?m)^[ \t]*(?:user|human|assistant)"
    r"[ \t]*[:：]?[ \t]*"
    r"([一-鿿（【「""'].{0,120})"
)

# B) 工具协议泄漏（抄自 claude-output-guard，含它列的四个变体）
TOOL_LEAK = re.compile(
    r"(?im)^[ \t]*(?:call|count|court|course)[ \t]*\r?\n[ \t]*"
    r"<(?:invoke|call)[ \t]+name[ \t]*=",
)

WARN_FORGED = (
    f"\n\n⚠️ **{GUARD_NAME}：拦下一段伪造的用户发言**\n\n"
    "上面那段以 `user` 开头的话，会话文件里查不到——"
    "**不是用户说的，是模型自己编的。**\n"
    "已从显示中截断。别顺着它往下接。\n"
)
WARN_TOOL = (
    f"\n\n⚠️ **{GUARD_NAME}：工具协议泄漏**\n\n"
    "上面出现了不该进正文的工具标记（`count` / `<invoke>`）。已截断。\n"
)


def log(kind, detail):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"{ts}\t{kind}\t{detail[:200]}\n")
    except Exception:
        pass


def recent_user_texts(path, n=LOOKBACK):
    """从会话文件尾部取最近 n 条真实 user 消息的文本。

    只读尾部 2MB，避免每次输出都扫整个几十 MB 的文件。
    """
    if not path or not os.path.exists(path):
        return None                      # 读不到 → 不做事实判断
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
                fh.readline()            # 丢掉半行
            tail = fh.read().decode("utf-8", "ignore")
    except Exception:
        return None

    out = []
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        c = d.get("message", {}).get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    out.append(b.get("text", ""))
    return out[-n:]


def norm(s):
    return re.sub(r"\s+", "", s)


def check(text, transcript):
    """返回 (截断位置, 警告文本) 或 None。"""
    m = TOOL_LEAK.search(text)
    if m:
        return m.start(), WARN_TOOL

    for m in FORGED_ROLE.finditer(text):
        frag = m.group(1).strip()
        if len(frag) < MIN_LEN:
            continue
        # 事实核对：这段话在会话文件里找得到吗
        reals = recent_user_texts(transcript)
        if reals is None:
            continue                     # 查不了就不拦，宁可漏
        needle = norm(frag)[:40]
        if any(needle in norm(r) for r in reals):
            continue                     # 她真说过 → 放行
        log("forged_role", frag)
        return m.start(), WARN_FORGED
    return None


def _state_file(payload):
    key = "{}|{}|{}".format(payload.get("session_id", ""),
                            payload.get("turn_id", ""),
                            payload.get("message_id", ""))
    return os.path.join(STATE, hashlib.md5(key.encode()).hexdigest())


def main():
    """流式安全版。

    MessageDisplay 是按 delta 调用的，一条消息会触发很多次。
    第一版每个 delta 都单独匹配、命中就替换掉那一片 —— 替换流中间的
    一片，后面的显示就接不上了。2026-08-19 实测：那可能就是"哑巴"的
    来源之一（用户那边一片空白）。

    现在：
      · 非 final 的片段一律原样放过（不输出 = 不干预显示）
      · 只在 final 时，用累积的全文做一次检查
      · 命中也不截断 —— 追加警告就好。截断反而让她看不到我编了什么
    """
    if os.environ.get("OUTPUT_GUARD_OFF") == "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if str(payload.get("hook_event_name") or "") != "MessageDisplay":
        return

    delta = str(payload.get("delta") or "")
    final = bool(payload.get("final"))
    sp = _state_file(payload)

    prev = ""
    try:
        if os.path.exists(sp):
            prev = open(sp, encoding="utf-8").read()
    except Exception:
        pass
    full = prev + delta

    if not final:
        try:
            os.makedirs(STATE, exist_ok=True)
            open(sp, "w", encoding="utf-8").write(full[-20000:])
        except Exception:
            pass
        return                      # 流中间一律不干预

    try:
        os.path.exists(sp) and os.unlink(sp)
    except Exception:
        pass

    hit = check(full, str(payload.get("transcript_path") or ""))
    if not hit:
        return
    _, warn = hit
    print(json.dumps(
        {"hookSpecificOutput": {"hookEventName": "MessageDisplay",
                                "displayContent": delta + warn}},
        ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
