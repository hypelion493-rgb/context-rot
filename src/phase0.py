"""Phase 0: 動作確認と校正。

使い方:
    python src/phase0.py auth       # ログイン状態の確認
    python src/phase0.py smoke      # 短い needle 1 件
    python src/phase0.py calib      # 日英の文字数→トークン数の比率
    python src/phase0.py bigctx     # 約 300K トークンの入力が通るか
    python src/phase0.py compact    # headless で autocompact が発火するか（ゲート）
"""
import json
import subprocess
import sys
import time
import uuid

from common import (CLAUDE, RESULTS, UsageLimitError, append_jsonl, compactions,
                    ctx_tokens, filler, run_claude, session_log)

OUT = RESULTS / "phase0.jsonl"


def needle_doc(lang, n_chars, code, depth=0.5, seed=0):
    body = filler(lang, n_chars, seed)
    needle = (f"なお、灯台守の好きな数字は {code} だった。" if lang == "ja"
              else f"By the way, the lighthouse keeper's favorite number was {code}. ")
    cut = int(len(body) * depth)
    return body[:cut] + needle + body[cut:]


def ask_code(lang):
    return "灯台守の好きな数字は？数字のみで答えて。" if lang == "ja" else "What was the lighthouse keeper's favorite number? Reply with the number only."


def auth():
    out = subprocess.run([CLAUDE, "auth", "status"], capture_output=True, text=True)
    print(out.stdout or out.stderr)
    return '"loggedIn": true' in out.stdout


def smoke(model="haiku"):
    res = run_claude(ask_code("ja"), needle_doc("ja", 2000, "7319"), model=model)
    ok = "7319" in res["result"]
    print(f"answer={res['result']!r} ok={ok} ctx={ctx_tokens(res)} model_usage={list(res.get('modelUsage', {}))}")
    append_jsonl(OUT, {"step": "smoke", "ok": ok, "ctx": ctx_tokens(res), "res": res})


def calib(model="haiku"):
    for lang in ("ja", "en"):
        for n_chars in (5_000, 50_000):
            t0 = time.time()
            res = run_claude(ask_code(lang), needle_doc(lang, n_chars, "5582"), model=model)
            ctx = ctx_tokens(res)
            rec = {"step": "calib", "lang": lang, "chars": n_chars, "ctx": ctx,
                   "chars_per_token": round(n_chars / max(ctx, 1), 3),
                   "ok": "5582" in res["result"], "sec": round(time.time() - t0, 1)}
            print(rec)
            append_jsonl(OUT, {**rec, "res": res})


def bigctx(model="haiku", target_tokens=300_000):
    # calib の結果があれば比率を使い、なければ控えめに 1.5 文字/トークンで見積もる
    ratio = 1.5
    if OUT.exists():
        rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines()]
        r = [x["chars_per_token"] for x in rows if x.get("step") == "calib" and x.get("lang") == "en"]
        ratio = r[-1] if r else ratio
    n_chars = int(target_tokens * ratio)
    t0 = time.time()
    try:
        res = run_claude(ask_code("en"), needle_doc("en", n_chars, "9046"), model=model)
    except UsageLimitError as e:
        print(f"失敗: {e}")
        append_jsonl(OUT, {"step": "bigctx", "model": model, "chars": n_chars, "error": str(e)})
        return
    rec = {"step": "bigctx", "model": model, "chars": n_chars, "ctx": ctx_tokens(res),
           "ok": "9046" in res["result"], "sec": round(time.time() - t0, 1)}
    print(rec)
    append_jsonl(OUT, {**rec, "res": res})


def compact(model="haiku", turns=4, chars_per_turn=40_000):
    """--autocompact 100000 で会話を伸ばし、compact_boundary が出るか確かめる。"""
    sid = str(uuid.uuid4())
    fact = "最初に伝えておきます。プロジェクト Kestrel の承認コードは QX-4817 です。\n\n"
    for i in range(turns):
        msg = ((fact if i == 0 else "") + "以下の資料を 1 行で要約してください。\n\n<資料>\n"
               + filler("ja", chars_per_turn, seed=i) + "\n</資料>")
        kw = {"session_id": sid} if i == 0 else {"resume": sid}
        res = run_claude(msg, model=model,
                         plain=False, persist=True, autocompact=100_000, **kw)
        print(f"turn {i}: ctx={ctx_tokens(res)} compactions={len(compactions(sid))}")
    probe = run_claude("プロジェクト Kestrel の承認コードは？コードのみ。", resume=sid,
                       fork=True, model=model, plain=False, autocompact=100_000)
    comps = compactions(sid)
    rec = {"step": "compact", "session_id": sid, "log": str(session_log(sid)),
           "n_compactions": len(comps),
           "metadata": [c["metadata"] for c in comps],
           "fact_in_summary": any("QX-4817" in (c["summary"] or "") for c in comps),
           "probe_answer": probe["result"], "probe_ok": "QX-4817" in probe["result"]}
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    append_jsonl(OUT, rec)
    if not comps:
        print("compaction が発火しませんでした。E5 の駆動方式を見直す必要があります。")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "auth"
    model = sys.argv[2] if len(sys.argv) > 2 else "claude-haiku-5-5"
    if step != "auth" and not auth():
        sys.exit("CLI がログインしていません。`claude auth login` を実行してください。")
    try:
        {"auth": lambda m: auth(), "smoke": smoke, "calib": calib,
         "bigctx": bigctx, "compact": compact}[step](model)
    except UsageLimitError as e:
        sys.exit(f"停止: {e}")
