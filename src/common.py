"""claude -p を呼び出す共通処理。"""
import json
import random
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
RESULTS = ROOT / "results"
PROJECTS = Path.home() / ".claude" / "projects"

CLAUDE = shutil.which("claude") or "claude"

# 単発試行で Claude Code 本来のシステムプロンプトの代わりに使う短い指示
PLAIN_SYSTEM = "You answer questions about the text the user provides. Answer concisely."


class UsageLimitError(RuntimeError):
    """利用上限や認証エラーなど、続行すべきでない失敗。"""


def run_claude(prompt, stdin_text="", *, model="haiku", effort="low",
               plain=True, session_id=None, resume=None, fork=False,
               persist=False, autocompact=None, timeout=1800):
    """claude -p を 1 回実行し、JSON 結果を dict で返す。"""
    args = [CLAUDE, "-p", prompt, "--safe-mode", "--output-format", "json",
            "--model", model, "--effort", effort]
    if plain:
        args += ["--tools", "", "--system-prompt", PLAIN_SYSTEM]
    if session_id:
        args += ["--session-id", session_id]
    if resume:
        args += ["--resume", resume]
    if fork:
        args += ["--fork-session"]
    if not persist:
        args += ["--no-session-persistence"]
    if autocompact:
        args += ["--autocompact", str(autocompact)]

    WORK.mkdir(exist_ok=True)
    proc = subprocess.run(args, input=stdin_text.encode("utf-8"), cwd=WORK,
                          capture_output=True, timeout=timeout)
    out = _decode(proc.stdout).strip()
    try:
        res = json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError(f"JSON を解釈できません: {out[:500]} / {proc.stderr[:500]!r}")
    if res.get("is_error"):
        raise UsageLimitError(res.get("result", "unknown error"))
    return res


def _decode(b):
    # Windows では claude -p の出力がコンソールのコードページ（cp932）になることがある
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp932", errors="replace")


def ctx_tokens(res):
    """実コンテキスト長（キャッシュ分を含む入力トークン）。"""
    u = res.get("usage", {})
    return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0))


def session_log(session_id):
    """セッション ID に対応するログファイルを探す。"""
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def compactions(session_id):
    """セッションログから compact_boundary の行と、その直後の要約文を取り出す。"""
    path = session_log(session_id)
    if not path:
        return []
    found, pending = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            pending = {"metadata": rec.get("compactMetadata", {}), "summary": None}
            found.append(pending)
        elif pending and rec.get("type") == "user" and pending["summary"] is None:
            content = rec.get("message", {}).get("content")
            pending["summary"] = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            pending = None
    return found


# --- コーパスが揃うまでの仮のフィラー文 ---

_JA = ["朝の市場には", "古い港町では", "山あいの村で", "川沿いの道を", "冬の終わりに",
       "人々が集まり", "静かに雨が降り", "商人たちは", "子どもたちが", "旅人は"]
_JA2 = ["野菜を並べていた。", "灯りがともった。", "昔話を語り合った。", "遠くの鐘を聞いた。",
        "新しい道を探した。", "季節の移ろいを感じた。", "手紙を書いた。", "橋を渡った。"]
_EN = ["In the morning market", "Along the old harbor", "In the mountain village",
       "By the quiet river", "At the end of winter", "The merchants", "The travelers"]
_EN2 = ["arranged their goods.", "lit the lanterns.", "told old stories.",
        "heard the distant bells.", "searched for a new road.", "wrote long letters.",
        "crossed the stone bridge."]


def filler(lang, n_chars, seed=0):
    """指定文字数前後の仮フィラー文を生成する。"""
    rng = random.Random(seed)
    a, b, sep = (_JA, _JA2, "") if lang == "ja" else (_EN, _EN2, " ")
    parts, size = [], 0
    while size < n_chars:
        s = f"{rng.choice(a)}{'' if lang == 'ja' else ' '}{rng.choice(b)}"
        parts.append(s)
        size += len(s) + len(sep)
    return sep.join(parts)


def append_jsonl(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
