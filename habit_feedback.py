#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
habit_feedback.py — agent-agnostic language-habit feedback pipeline.

Reads the user's own messages from four coding agents (Codex, Claude Code, DSH,
OMP), counts recurring filler words (口癖), and maintains the desktop
「语言习惯观察」document. The deterministic parts (extraction + 口癖 counting) run
without any LLM; the semantic parts (language-habit diagnosis + rewrite
suggestions) are handed to an LLM through `digest` + `prompts.md`.

Subcommands:
  extract   pull user messages since each agent's watermark into a fresh view
  stats     count filler words in new messages, accumulate, rewrite the doc table
  digest    emit the new user messages as markdown for the LLM step
  run       extract + stats (one-shot daily entry point)

State (in ./state):
  watermark.json   per-agent last-seen timestamp (ms)
  counts.json      cumulative filler-word counts (seeded from the existing doc)
  digest.json      last digest timestamp (ms)
"""
import datetime
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
STATE = os.path.join(HERE, "state")

DEFAULT_CONFIG = {
    "doc_habits": "~/Desktop/语言习惯观察.md",
    "doc_agents_codex": "~/.codex/AGENTS.md",
    "doc_agents_claude": "~/.claude/CLAUDE.md",
    "doc_reflect": "~/Desktop/每日思考复盘.md",
    "initial_watermark_ms": 0,
    "top_n": 15,
    "filler_words": [
        "一下", "一个", "现在", "新的", "可以", "比如", "可能", "那个", "就是", "然后",
        "好像", "应该", "你看一下", "另外", "甚至", "其实", "感觉", "大概", "比较",
        "非常", "真的", "直接", "只是", "还是", "或者", "还有", "因为", "所以",
        "但是", "不过", "这个", "这种", "那种", "反正", "基本上", "特别", "挺",
        "蛮", "有点", "先", "再", "目前", "弄", "搞", "咱们", "这边", "那边",
    ],
}


def _load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            cfg.update(json.load(open(p, encoding="utf-8")))
        except Exception:
            pass
    for k in ("doc_habits", "doc_agents_codex", "doc_agents_claude", "doc_reflect"):
        if isinstance(cfg.get(k), str) and cfg[k]:
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


CONFIG = _load_config()

os.makedirs(STATE, exist_ok=True)


# --------------------------------------------------------------------------- #
# state helpers
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def watermark():
    p = os.path.join(STATE, "watermark.json")
    default = {a: CONFIG["initial_watermark_ms"] for a in ("codex", "claude", "omp", "dsh")}
    return load_json(p, default)


def save_watermark(w):
    save_json(os.path.join(STATE, "watermark.json"), w)


def iso_to_ms(s):
    """ISO timestamp (with Z or offset) -> epoch ms."""
    if not s:
        return 0
    s = s.strip()
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def text_blocks(content):
    """Yield text strings from a Claude/OMP/DSH-style content list (or str)."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                yield b
            elif isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"):
                yield b.get("text", "")


def _is_noise(text):
    """Automation kickoff prompts are recorded as 'user' messages; drop them."""
    head = text[:300]
    return "Automation ID:" in head or head.startswith("Automation:")


# --------------------------------------------------------------------------- #
# adapters: each yields (ts_ms, session_label, text)
# --------------------------------------------------------------------------- #
CODX_SKIP = ("<", "# AGENTS.md", "# Files mentioned", "codex-clipboard")


def iter_codex():
    files = sorted(glob.glob(os.path.join(HOME, ".codex", "sessions", "**", "*.jsonl"), recursive=True))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("type") != "response_item":
                    continue
                p = j.get("payload", {})
                if p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
                    continue
                role = p["role"]
                for t in text_blocks(p.get("content", [])):
                    t = t.strip()
                    if not t:
                        continue
                    if role == "user":
                        if t.startswith(CODX_SKIP) or any(k in t[:40] for k in CODX_SKIP[1:]):
                            continue
                        if _is_noise(t):
                            continue
                    yield iso_to_ms(j.get("timestamp")), os.path.basename(f), role, t


def iter_claude():
    patterns = [
        os.path.join(HOME, ".claude", "projects", "**", "*.jsonl"),
        os.path.join(HOME, ".claude", "sessions", "**", "*.jsonl"),
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                t = j.get("type")
                if t == "user":
                    if j.get("isSidechain"):
                        continue
                    if (j.get("origin") or {}).get("kind") != "human":
                        continue
                    role = "user"
                elif t == "assistant":
                    role = "assistant"
                else:
                    continue
                for txt in text_blocks((j.get("message") or {}).get("content", [])):
                    txt = txt.strip()
                    if not txt:
                        continue
                    if role == "user" and ("<system-reminder>" in txt or _is_noise(txt)):
                        continue
                    yield iso_to_ms(j.get("timestamp")), os.path.basename(f), role, txt


def iter_omp():
    files = sorted(glob.glob(os.path.join(HOME, ".omp", "agent", "sessions", "**", "*.jsonl"), recursive=True))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("type") != "message":
                    continue
                m = j.get("message")
                if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                    continue
                role = m["role"]
                for t in text_blocks(m.get("content", [])):
                    t = t.strip()
                    if not t:
                        continue
                    if role == "user" and _is_noise(t):
                        continue
                    yield iso_to_ms(j.get("timestamp") or m.get("timestamp")), os.path.basename(f), role, t


def _dsh_stream(path):
    import zstandard
    d = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        return d.stream_reader(fh).read().decode("utf-8", "replace")


def iter_dsh():
    files = sorted(glob.glob(os.path.join(HOME, ".dsh", "sessions", "**", "*.zstd"), recursive=True))
    for f in files:
        try:
            txt = _dsh_stream(f)
        except Exception:
            continue
        for line in txt.splitlines():
            try:
                j = json.loads(line)
            except Exception:
                continue
            if not isinstance(j, dict):
                continue
            data = j.get("data") or {}
            if j.get("type") == "user/message":
                if data.get("role") != "user":
                    continue
                if ((data.get("source") or {}).get("kind") or "user") != "user":
                    continue
                role = "user"
                content = data.get("content")
            elif j.get("type") == "assistant/message":
                msg = data.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                role = "assistant"
                content = msg.get("content")
            else:
                continue
            for txt in text_blocks(content or []):
                txt = txt.strip()
                if not txt:
                    continue
                if role == "user" and _is_noise(txt):
                    continue
                yield int(j.get("time") or 0), os.path.basename(f), role, txt


ADAPTERS = {"codex": iter_codex, "claude": iter_claude, "omp": iter_omp, "dsh": iter_dsh}


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #
def extract(collect):
    """Collect every user message newer than the per-agent watermark.

    `collect` is called with (agent, ts_ms, session, text) for each new message.
    Returns the updated watermark dict.
    """
    w = watermark()
    last_seen = {}  # (agent, text) -> ts, for a 60s duplicate window (codex rollout re-echo)
    for agent, fn in ADAPTERS.items():
        lo = w.get(agent, CONFIG["initial_watermark_ms"])
        hi = lo
        for ts, sess, role, text in fn():
            if role != "user":
                continue
            if ts <= lo:
                continue
            key = (agent, text)
            if ts - last_seen.get(key, -1_000_000_000) < 60_000:
                continue
            last_seen[key] = ts
            hi = max(hi, ts)
            collect(agent, ts, sess, text)
        w[agent] = hi
    return w


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def parse_doc_counts(doc_text):
    counts = {}  # word -> [count, context]
    in_table = False
    for line in doc_text.splitlines():
        if line.startswith("## 高频口癖统计"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if in_table and line.startswith("|") and "---" not in line and "口癖" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                num = int(re.sub(r"[^\d]", "", cells[1]) or 0)
                ctx = cells[2] if len(cells) > 2 else "—"
                counts[cells[0]] = [num, ctx]
    return counts


def counts_state():
    return load_json(os.path.join(STATE, "counts.json"), {"counts": {}}).get("counts", {})


def save_counts(c):
    save_json(os.path.join(STATE, "counts.json"), {"counts": c})


def count_fillers(text, words):
    hits = {}
    for w in words:
        n = text.count(w)
        if n:
            hits[w] = hits.get(w, 0) + n
    return hits


def rewrite_doc(new_msgs_count, filler_hits, top_words, seed_contexts):
    path = CONFIG["doc_habits"]
    text = open(path, encoding="utf-8", newline="").read()
    eol = "\r\n" if "\r\n" in text else "\n"

    # build the new table
    rows = []
    for w in top_words:
        rows.append(f"| {w} | {filler_hits.get(w, 0)} | {seed_contexts.get(w, '—')} |")

    note = (f"（累计统计，覆盖 Codex / Claude Code / DSH / OMP 四个 agent 的用户发言；"
            f"本次新增 {new_msgs_count} 条消息）")

    # replace the section between "## 高频口癖统计" and the next "## "
    lines = text.splitlines()
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("## 高频口癖统计"):
            out.append(line)
            out.append("")
            out.append(note)
            out.append("")
            out.append("| 口癖 | 出现次数 | 典型语境 |")
            out.append("|---|---|---|")
            out.extend(rows)
            i += 1
            # skip until next "## "
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            replaced = True
            continue
        out.append(line)
        i += 1

    new_text = eol.join(out)
    if replaced and new_msgs_count:
        today = datetime.date.today().isoformat()
        log_line = f"- {today}：通用管线首次运行，覆盖四 agent，累计 {new_msgs_count} 条新消息，刷新口癖统计表。"
        new_text = _append_log(new_text, log_line)

    open(path, "w", encoding="utf-8", newline="").write(new_text)


def _append_log(text, line):
    lines = text.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 维护日志"):
            idx = i
            break
    if idx is None:
        return text.rstrip() + "\n\n" + line + "\n"
    last_bullet = -1
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("- "):
            last_bullet = j
    if last_bullet >= 0:
        lines.insert(last_bullet + 1, line)
    else:
        lines.insert(idx + 1, line)
    eol = "\r\n" if "\r\n" in text else "\n"
    return eol.join(lines)


def stats():
    words = CONFIG["filler_words"]
    doc = open(CONFIG["doc_habits"], encoding="utf-8").read()

    # seed cumulative counts from the doc's current table, once
    counts = counts_state()
    seeded = False
    if not counts:
        counts = {w: n for w, (n, _c) in parse_doc_counts(doc).items()}
        seeded = True
    seed_contexts = {w: c for w, (_n, c) in parse_doc_counts(doc).items()}

    hits = {w: counts.get(w, 0) for w in words}
    n_new = 0

    def collect(agent, ts, sess, text):
        nonlocal n_new
        n_new += 1
        for w, c in count_fillers(text, words).items():
            hits[w] = hits.get(w, 0) + c

    w = extract(collect)
    save_watermark(w)
    save_counts(hits)

    top = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    top = [w for w, c in top if c > 0][: CONFIG["top_n"]]

    rewrite_doc(n_new, hits, top, seed_contexts)
    print(f"[stats] new_messages={n_new} seeded={seeded} top_words={len(top)}")
    for w in top:
        print(f"   {w}: {hits[w]}")


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #
def digest():
    since = load_json(os.path.join(STATE, "digest.json"), {"ts": CONFIG["initial_watermark_ms"]})["ts"]
    buckets = {a: [] for a in ADAPTERS}

    # reuse the adapters but only for messages after `since`
    last_seen = {}
    for agent, fn in ADAPTERS.items():
        for ts, sess, role, text in fn():
            if role != "user":
                continue
            if ts > since:
                key = (agent, text)
                if ts - last_seen.get(key, -1_000_000_000) < 60_000:
                    continue
                last_seen[key] = ts
                buckets[agent].append((ts, text))

    out = []
    out.append("# 新用户消息摘要（供 LLM 语言习惯分析）\n")
    total = 0
    for agent, items in buckets.items():
        if not items:
            continue
        items.sort()
        out.append(f"\n## {agent}（{len(items)} 条）\n")
        for ts, text in items:
            total += 1
            day = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
            out.append(f"- [{day}] {text[:300]}")
    out.insert(1, f"共 {total} 条，时间范围：2026-10-16 22:10 之后。\n")

    path = os.path.join(STATE, "digest.md")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    save_json(os.path.join(STATE, "digest.json"), {"ts": int(max([ts for a in buckets.values() for ts, _ in a] + [since]))})
    print(f"[digest] wrote {path} ({total} messages)")


# --------------------------------------------------------------------------- #
# LLM (DeepSeek, OpenAI-compatible) — semantic analysis
# --------------------------------------------------------------------------- #
import urllib.request
import urllib.error

DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def deepseek_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k
    cred = os.path.join(HOME, ".dsh", ".credentials.yaml")
    if os.path.exists(cred):
        for line in open(cred, encoding="utf-8", errors="replace"):
            m = re.match(r'^\s*DEEPSEEK_API_KEY:\s*["\']?([^\s"\']+)', line)
            if m:
                return m.group(1)
    return None


def chat(messages, max_tokens=3000, temperature=0.2):
    key = deepseek_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未找到（环境变量或 ~/.dsh/.credentials.yaml 都没有）")
    url = DEEPSEEK_BASE.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]


def _extract_fence(text):
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _split_habits_suggestions(body):
    """Split LLM output into (habits, suggestions) at the 改进建议 marker."""
    m = re.search(r"(?m)^#{1,4}\s*改进建议\s*$", body)
    if m:
        habits = body[:m.start()].strip()
        suggestions = body[m.end():].strip()
    else:
        habits = body.strip()
        suggestions = ""
    habits = re.sub(r"(?m)^#{1,4}\s*需要注意的语言习惯\s*$", "", habits).strip()
    return habits, suggestions


def _is_empty(text):
    t = (text or "").strip().strip("-").strip()
    return not t or t.startswith("无")


def _prepend_dated(doc, section, date, content):
    """Insert a dated block right after a section heading (newest first)."""
    if _is_empty(content):
        return doc
    eol = "\r\n" if "\r\n" in doc else "\n"
    block = "### %s\n\n%s" % (date, content.replace("\r\n", "\n").strip())
    lines = doc.splitlines()
    out = []
    i = 0
    inserted = False
    while i < len(lines):
        if lines[i].strip().startswith("## %s" % section) and not inserted:
            out.append(lines[i])
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                out.append(lines[j])
                j += 1
            out.extend(block.split("\n"))
            out.append("")
            inserted = True
            i = j
            continue
        out.append(lines[i])
        i += 1
    if not inserted:
        out.append("## %s（按日期，新的在上）" % section)
        out.append("")
        out.extend(block.split("\n"))
    return eol.join(out)


def _read_digest():
    p = os.path.join(STATE, "digest.md")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read()
    return ""


def _has_messages(dig):
    m = re.search(r"共\s*(\d+)\s*条", dig)
    return bool(m) and int(m.group(1)) > 0


HABITS_SYS = (
    "你是用户「语言习惯观察」文档的维护者。下面提供本次新增的用户消息（只含用户本人发言，agent 回复已剔除）。"
    "你的任务：只基于**本次新增**的消息，提炼今天值得记录的表达问题与改进建议（不要重复历史内容）。\n"
    "要求：\n"
    "1. 输出两部分，中间用单独一行「### 改进建议」分隔：前半是「需要注意的语言习惯」，后半是「改进建议」。\n"
    "2. 「需要注意的语言习惯」：只记录今天新消息里反复出现（≥2 次）或明显值得注意的表达问题，附真实例句；今天没有就写「无」。\n"
    "3. 「改进建议」：针对上述问题给出「原句 → 建议改写」对照；没有就写「无」。\n"
    "4. 整体包在一个 ```markdown 代码块里，不要输出任何其他文字。"
)


def llm_habits():
    dig = _read_digest()
    if not _has_messages(dig):
        print("[llm:habits] no new messages, skip")
        return
    user = f"# 本次新增用户消息\n{dig}\n\n请按提示词要求输出今天的新观察。"
    try:
        content = chat([{"role": "system", "content": HABITS_SYS}, {"role": "user", "content": user}])
    except Exception as e:
        print(f"[llm:habits] LLM call failed: {e}")
        return
    body = _extract_fence(content)
    if not body:
        print("[llm:habits] no parseable output")
        return
    habits, suggestions = _split_habits_suggestions(body)
    if _is_empty(habits) and _is_empty(suggestions):
        print("[llm:habits] nothing new today")
        return
    today = datetime.date.today().isoformat()
    doc = open(CONFIG["doc_habits"], encoding="utf-8").read()
    doc = _prepend_dated(doc, "需要注意的语言习惯", today, habits)
    doc = _prepend_dated(doc, "改进建议", today, suggestions)
    doc = _append_log(doc, f"- {today}：语义分析，新增当天「需要注意的语言习惯」与「改进建议」。")
    open(CONFIG["doc_habits"], "w", encoding="utf-8", newline="").write(doc)
    print("[llm:habits] updated")


AGENTS_SYS = (
    "你是 agent 全局记忆文件的维护者。用户用中文与多个 coding agent 交流，"
    "下面提供本次新增的用户消息（只含用户本人发言）。"
    "你的任务：从中提炼反复出现、有长期参考价值的共性，更新文件的「用户偏好」与「常见问题与纠正」小节。\n"
    "要求：\n"
    "1. 只写有重复证据的长期模式，偶发一次性问题不写。\n"
    "2. 已有条目仍准确则保留；新的补充进去；保持面向 agent 的指令式、简洁。\n"
    "3. 若没有新发现，原样输出当前文件内容（不要为空）。\n"
    "4. 若本次有新增或更新，在「维护日志」末尾追加一行「日期：摘要」；无变化则不追加。\n"
    "5. 只输出该文件的完整 markdown，包在一个 ```markdown 代码块里，不要输出任何其他文字。"
)


def llm_agents():
    dig = _read_digest()
    if not _has_messages(dig):
        print("[llm:agents] no new messages, skip")
        return
    for path, label in [(CONFIG["doc_agents_codex"], "AGENTS.md"),
                        (CONFIG["doc_agents_claude"], "CLAUDE.md")]:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else \
            "（文件尚不存在，请从零创建，含「用户偏好」「常见问题与纠正」「维护日志」三个小节）"
        user = f"# 本次新增用户消息\n{dig}\n\n# 当前 {label} 内容\n{cur}\n\n请输出更新后的 {label} 完整内容。"
        try:
            content = chat([{"role": "system", "content": AGENTS_SYS}, {"role": "user", "content": user}])
        except Exception as e:
            print(f"[llm:agents] {label} LLM call failed: {e}")
            continue
        new = _extract_fence(content)
        if not new or new == cur.strip():
            print(f"[llm:agents] {label} no change")
            continue
        open(path, "w", encoding="utf-8", newline="").write(new)
        print(f"[llm:agents] {label} updated")


def llm():
    llm_habits()
    llm_agents()


# --------------------------------------------------------------------------- #
# reflect — daily thinking retrospective
# --------------------------------------------------------------------------- #
REFLECT_DOC = CONFIG.get("doc_reflect", os.path.join(HOME, "Desktop", "每日思考复盘.md"))
REFLECT_SYS_PATH = os.path.join(HERE, "reflect_prompt.md")


def _split_heading(text, heading):
    idx = text.find(heading)
    if idx == -1:
        return text, None
    return text[:idx].strip(), text[idx:].strip()


def _remove_dated_section(doc, heading):
    lines = doc.splitlines()
    out = []
    skip = False
    for ln in lines:
        s = ln.strip()
        if s == heading:
            skip = True
            continue
        if skip and s.startswith("## "):
            skip = False
        if not skip:
            out.append(ln)
    eol = "\r\n" if "\r\n" in doc else "\n"
    return eol.join(out)


def reflect():
    wp = os.path.join(STATE, "reflect_watermark.json")
    since = load_json(wp, {"ts": CONFIG["initial_watermark_ms"]})["ts"]
    entries = {a: [] for a in ADAPTERS}
    hi = since
    last_seen = {}
    for agent, fn in ADAPTERS.items():
        for ts, sess, role, text in fn():
            if ts <= since:
                continue
            key = (agent, role, text[:60])
            if ts - last_seen.get(key, -1_000_000_000) < 60_000:
                continue
            last_seen[key] = ts
            entries[agent].append((ts, role, text))
            hi = max(hi, ts)

    parts = []
    total = 0
    active = []
    for agent, items in entries.items():
        if not items:
            continue
        items.sort()
        items = items[-100:]
        active.append(agent)
        parts.append(f"## {agent}")
        # collapse repeated identical assistant replies (heartbeat/polling noise)
        counter = {}
        for ts, role, text in items:
            if role == "assistant":
                k = text[:60].strip()
                if k:
                    counter[k] = counter.get(k, 0) + 1
        emitted = set()
        for ts, role, text in items:
            if role == "assistant":
                k = text[:60].strip()
                if k in emitted:
                    continue
                emitted.add(k)
                if counter.get(k, 0) > 1:
                    text = text[:200] + f"（×{counter[k]}）"
            total += 1
            tag = "用户" if role == "user" else "agent"
            parts.append(f"[{tag}] {text[:210]}")
    if total == 0:
        print("[reflect] no new activity, skip")
        return

    transcript = "\n".join(parts)
    sys_prompt = open(REFLECT_SYS_PATH, encoding="utf-8").read()
    user = f"# 今日对话（按 agent 分组，时间顺序）\n{transcript}\n\n请按提示词要求输出今日复盘。"
    try:
        content = chat([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
                       max_tokens=3000, temperature=0.3)
    except Exception as e:
        print(f"[reflect] LLM call failed: {e}")
        return
    body = _extract_fence(content)
    if not body:
        print("[reflect] no parseable output")
        return
    body, imp = _split_heading(body, "## 复盘脚本改进建议")
    if imp and imp.strip() not in ("无", "无。", "## 复盘脚本改进建议\n- 无", "## 复盘脚本改进建议\n无"):
        _append_improvements(imp)

    _write_reflect_doc(body, total, active)
    save_json(wp, {"ts": hi})
    print(f"[reflect] updated ({total} messages across {len(active)} agents)")


def _append_improvements(imp):
    p = os.path.join(STATE, "reflect_improvements.md")
    today = datetime.date.today().isoformat()
    entry = f"\n\n## {today}\n{imp.strip()}"
    with open(p, "a", encoding="utf-8", newline="") as fh:
        fh.write(entry)
    print("[reflect] self-improvement notes appended")


def _write_reflect_doc(body, total, active):
    path = REFLECT_DOC
    today = datetime.date.today().isoformat()
    heading = f"## {today} 复盘"
    intro = "本文件由每日定时任务自动维护，复盘用户在各 coding agent 中的思考过程（提问方式、绕路、遗漏、误区等）。"
    if os.path.exists(path):
        doc = open(path, encoding="utf-8", newline="").read()
    else:
        doc = f"# 每日思考复盘（每日自动维护）\n\n{intro}\n\n## 维护日志\n"
    doc = _remove_dated_section(doc, heading)
    section = f"{heading}\n\n{body.strip()}"
    if intro in doc:
        doc = doc.replace(intro, intro + "\n\n" + section, 1)
    else:
        doc = doc.rstrip() + "\n\n" + section
    doc = _append_log(doc, f"- {today}：复盘 {total} 条消息（覆盖 {', '.join(active) or '无'}）。")
    open(path, "w", encoding="utf-8", newline="").write(doc)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "extract":
        n = 0

        def _c(agent, ts, sess, text):
            nonlocal n
            n += 1

        save_watermark(extract(_c))
        print(f"[extract] new_messages={n}")
    elif cmd == "stats":
        stats()
    elif cmd == "digest":
        digest()
    elif cmd == "llm":
        llm()
    elif cmd == "reflect":
        reflect()
    elif cmd == "run":
        stats()
        digest()
        try:
            llm()
        except Exception as e:
            print(f"[run] llm step skipped: {e}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
