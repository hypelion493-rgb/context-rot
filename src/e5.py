"""E5: 長い会話での記憶と compaction。

使い方:
    python src/e5.py --lang ja --window 400000 [--model claude-haiku-5-5[1m]] [--seed 0]
    python src/e5.py --lang en --window 1000000 --target 700000

同じ引数で再実行すると、続きのターンから再開する。
結果: results/e5/<run_id>/{state.json, turns.jsonl, probes.jsonl}
"""
import argparse
import json
import random
import re
import string
import sys
import uuid

from common import ROOT, RESULTS, UsageLimitError, append_jsonl, compactions, ctx_tokens, run_claude

OVERHEAD = 38_000              # Claude Code 標準のシステムプロンプト＋ツール定義（Phase 0 実測）
CHARS_PER_TOKEN = {"ja": 0.96, "en": 2.9}
CHECKPOINTS = (100_000, 200_000, 400_000, 600_000)

NAMES = ["Kestrel", "Marlin", "Juniper", "Osprey", "Tamarind", "Halcyon", "Basalt",
         "Cobalt", "Quillon", "Sorrel", "Larkspur", "Wren", "Thistle", "Garnet", "Ember"]

TEXT = {
    "ja": {
        "intro": "これから長い資料をいくつか読んでもらいます。その前に、覚えておいてほしい情報と、回答のルールを伝えます。\n\n",
        "rules_head": "【回答のルール】この会話のすべての回答で守ってください。\n",
        "rules": ["回答の冒頭は必ず「【要約】」で始める。", "回答の最後は必ず「（以上）」で終える。",
                  "回答は 3 行以内にする。"],
        "facts_head": "【覚えておいてほしい情報】\n",
        "fact": "プロジェクト {name} の承認コードは {value} です。",
        "var": "サーバー {name} のポート番号は {value} です。",
        "update": "【変更のお知らせ】サーバー {name} のポート番号は {old} から {value} に変更になりました。\n\n",
        "task": "以下の資料を読んで、内容を要約してください。\n\n<資料>\n{doc}\n</資料>",
        "fact_key": "プロジェクト {name} の承認コード",
        "var_key": "サーバー {name} のポート番号",
        "probe": ("確認させてください。ツールは使わず、記憶だけで答えてください。"
                  "この会話の最初のほうで伝えた情報を、覚えている範囲で下の形式に埋めて返してください。"
                  "覚えていない項目は「不明」と書いてください。\n{schema}"),
        "rules_key": "回答のルール",
    },
    "en": {
        "intro": "You will read several long documents. Before that, here is some information to remember and the rules for your replies.\n\n",
        "rules_head": "[Reply rules] Follow these in every reply in this conversation.\n",
        "rules": ["Always begin your reply with \"[Summary]\".", "Always end your reply with \"(end)\".",
                  "Keep your reply to at most 3 lines."],
        "facts_head": "[Information to remember]\n",
        "fact": "The approval code for project {name} is {value}.",
        "var": "The port number of server {name} is {value}.",
        "update": "[Change notice] The port number of server {name} has changed from {old} to {value}.\n\n",
        "task": "Read the following document and summarize it.\n\n<document>\n{doc}\n</document>",
        "fact_key": "approval code of project {name}",
        "var_key": "port number of server {name}",
        "probe": ("Let me check something. Do not use any tools; answer from memory only. "
                  "Fill in the form below with the information given early in this conversation, as far as you remember it. "
                  "Write \"unknown\" for anything you don't remember.\n{schema}"),
        "rules_key": "reply rules",
    },
}


def make_items(seed):
    rng = random.Random(seed)
    names = rng.sample(NAMES, 15)
    code = lambda: f"{''.join(rng.choices(string.ascii_uppercase, k=2))}-{rng.randint(1000, 9999)}"
    facts = [{"key": f"code:{n}", "name": n, "value": code()} for n in names[:10]]
    ports = rng.sample(range(2000, 9999), 10)
    vars_ = [{"key": f"port:{n}", "name": n, "old": ports[i], "value": ports[i + 5]}
             for i, n in enumerate(names[10:])]
    return facts, vars_


def plan_turns(args):
    """ターンごとのメッセージを組み立てる（決定的に再生成できるように seed から作る）。"""
    t = TEXT[args.lang]
    corpus = (ROOT / "corpus" / f"{args.lang}.txt").read_text(encoding="utf-8")
    chunk = int(args.turn_tokens * CHARS_PER_TOKEN[args.lang])
    n_turns = (args.target - OVERHEAD) // args.turn_tokens
    pos = random.Random(args.seed).randrange(len(corpus))
    facts, vars_ = make_items(args.seed)
    update_turn = max(3, n_turns * 2 // 5)         # 更新値は全体の 4 割の地点で変更する

    turns = []
    for i in range(n_turns):
        doc = (corpus + corpus)[pos:pos + chunk]      # 本を頭から読み進める（末尾で折り返す）
        pos = (pos + chunk) % len(corpus)
        head = ""
        if i == 0:
            head = (t["intro"] + t["rules_head"] + "\n".join(f"{k + 1}. {r}" for k, r in enumerate(t["rules"]))
                    + "\n\n" + t["facts_head"] + "\n".join(t["fact"].format(**f) for f in facts[:4]) + "\n\n")
        elif i in (1, 2):                    # 残りの事実と初期値を序盤に分散させる
            fs = facts[4:7] if i == 1 else facts[7:]
            vs = vars_[:3] if i == 1 else vars_[3:]
            head = "\n".join([t["fact"].format(**f) for f in fs]
                             + [t["var"].format(name=v["name"], value=v["old"]) for v in vs]) + "\n\n"
        elif i == update_turn:
            head = "".join(t["update"].format(**v) for v in vars_)
        turns.append(head + t["task"].format(doc=doc))
    return turns, facts, vars_


def label(t, item):
    return t["fact_key" if item["key"].startswith("code:") else "var_key"].format(name=item["name"])


def probe_message(t, facts, vars_):
    schema = {label(t, x): "" for x in facts + vars_}
    schema[t["rules_key"]] = ["..."]
    return t["probe"].format(schema=json.dumps(schema, ensure_ascii=False, indent=1))


def grade_probe(text, facts, vars_, rules_lang):
    t = TEXT[rules_lang]
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        raw = {}
    ans = {x["key"]: raw.get(label(t, x)) for x in facts + vars_}
    ans["rules"] = raw.get(t["rules_key"])
    norm = lambda x: str(x).strip().upper() if x is not None else None
    fact_ok = {f["key"]: norm(ans.get(f["key"])) == f["value"] for f in facts}
    var_res = {}
    for v in vars_:
        a = norm(ans.get(v["key"]))
        var_res[v["key"]] = "new" if a == str(v["value"]) else "old" if a == str(v["old"]) else "wrong"
    rules_txt = json.dumps(ans.get("rules", ""), ensure_ascii=False)
    marks = ["【要約】", "（以上）", "3"] if rules_lang == "ja" else ["[Summary]", "(end)", "3"]
    rules_recalled = [mk in rules_txt for mk in marks]
    return {"parsed": bool(raw), "fact_ok": fact_ok, "var": var_res, "rules_recalled": rules_recalled,
            "fact_acc": sum(fact_ok.values()) / len(facts),
            "var_new": sum(r == "new" for r in var_res.values()) / len(vars_),
            "var_old": sum(r == "old" for r in var_res.values()) / len(vars_)}


def rule_compliance(text, lang):
    s = text.strip()
    start, end = ("【要約】", "（以上）") if lang == "ja" else ("[Summary]", "(end)")
    lines = [l for l in s.splitlines() if l.strip()]
    return [s.startswith(start), s.endswith(end), len(lines) <= 3]


def facts_in_summaries(sid, facts, vars_):
    out = []
    for c in compactions(sid):
        s = c["summary"] or ""
        out.append({"preTokens": c["metadata"].get("preTokens"), "postTokens": c["metadata"].get("postTokens"),
                    "facts_kept": sum(f["value"] in s for f in facts),
                    "var_new_kept": sum(str(v["value"]) in s for v in vars_),
                    "var_old_kept": sum(str(v["old"]) in s for v in vars_)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["ja", "en"], required=True)
    ap.add_argument("--window", type=int, required=True, help="CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    ap.add_argument("--model", default="claude-haiku-5-5[1m]")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--target", type=int, default=700_000, help="論理的な会話長（トークン）")
    ap.add_argument("--turn-tokens", type=int, default=15_000)
    args = ap.parse_args()

    run_id = f"{args.model.replace('[', '_').replace(']', '')}_{args.lang}_w{args.window // 1000}k_s{args.seed}_t{args.target // 1000}k"
    out = RESULTS / "e5" / run_id
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "run_id": run_id, "args": vars(args), "session_id": str(uuid.uuid4()), "done": 0, "probed": []}
    sid, t = state["session_id"], TEXT[args.lang]
    turns, facts, vars_ = plan_turns(args)
    common = dict(model=args.model, effort=args.effort, plain=False, autocompact=args.window)
    print(f"{run_id}: {len(turns)} ターン、{state['done']} 済み、session={sid}")

    def save():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def probe(label):
        # 分岐セッションで compaction が起きると測定対象が変わるため、窓を最大にしツールも外す
        res = run_claude(probe_message(t, facts, vars_), resume=sid, fork=True, no_tools=True,
                         **{**common, "autocompact": 1_000_000})
        g = grade_probe(res["result"], facts, vars_, args.lang)
        append_jsonl(out / "probes.jsonl", {"checkpoint": label, "turn": state["done"], "ctx": ctx_tokens(res),
                                            "answer": res["result"], **g})
        print(f"  probe@{label}: facts={g['fact_acc']:.0%} var_new={g['var_new']:.0%} "
              f"var_old={g['var_old']:.0%} rules={g['rules_recalled']}")
        state["probed"].append(label)
        save()

    try:
        # 前回、ターン後の probe の途中で止まっていた場合の取りこぼしを補う
        for cp in CHECKPOINTS:
            if state["done"] and OVERHEAD + state["done"] * args.turn_tokens >= cp and cp not in state["probed"]:
                probe(cp)
        for i in range(state["done"], len(turns)):
            kw = {"session_id": sid} if i == 0 else {"resume": sid}
            res = run_claude(turns[i], persist=True, **common, **kw)
            logical = OVERHEAD + (i + 1) * args.turn_tokens
            n_comp = len(compactions(sid))
            append_jsonl(out / "turns.jsonl", {
                "turn": i, "logical": logical, "ctx": ctx_tokens(res), "compactions": n_comp,
                "rules": rule_compliance(res["result"], args.lang), "answer": res["result"][:500],
                "output_tokens": res.get("usage", {}).get("output_tokens"),
                "num_turns": res.get("num_turns")})   # compaction やツール使用があると 2 以上になる
            state["done"] = i + 1
            save()
            print(f"turn {i}: logical={logical // 1000}K ctx={ctx_tokens(res) // 1000}K compactions={n_comp}")
            for cp in CHECKPOINTS:
                if logical >= cp and cp not in state["probed"] and logical - args.turn_tokens < cp:
                    probe(cp)
        if "final" not in state["probed"]:
            probe("final")
        state["compaction_summaries"] = facts_in_summaries(sid, facts, vars_)
        save()
        print("完了:", json.dumps(state["compaction_summaries"], ensure_ascii=False))
    except UsageLimitError as e:
        save()
        sys.exit(f"停止（{state['done']} ターン済み）: {e}\n同じコマンドで再開できます。")


if __name__ == "__main__":
    main()
