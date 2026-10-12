"""E6: 単発の長文入力（compaction なし）で、長さ 1M 付近まで性能が落ちるかを測る。

1 回の入力に次を仕込み、文章の後でまとめて質問する。
- 点検番号 ×20: 深さ 2.5%〜97.5% に均等配置（位置ごとの想起を見る）
- 在庫数 ×5: 前半（10〜30%）で初出、後半（60〜90%）で改訂（最新値を答えられるか）
- 紫色の風船の一文: ランダムな位置に K 回（数える問題）

使い方:
    python src/e6.py --model "claude-opus-5-5[1m]" --lengths 100000 300000 600000 900000
結果: results/e6/<model>_<lang>_<長さ>k_s<seed>.json（既にあればスキップ）
"""
import argparse
import json
import random
import re
import string
import sys
import time

from common import ROOT, RESULTS, UsageLimitError, ctx_tokens, run_claude

CHARS_PER_TOKEN = {"ja": 0.97, "en": 2.85}   # E5 の Opus 実測から
NAMES = ["Alder", "Birch", "Cedar", "Dunmore", "Elmstead", "Fenwick", "Galloway", "Hartley", "Ingram",
         "Jarrow", "Kingsley", "Lowell", "Marston", "Norwood", "Oakham", "Penrith", "Quarry", "Redcliff",
         "Selby", "Thornton", "Upton", "Verney", "Whitby", "Yarrow", "Zennor"]

TEXT = {
    "ja": {
        "code": "（記録によれば、{name}灯台の点検番号は {value} であった。）",
        "stock0": "（帳簿によれば、{name}倉庫の在庫数は {value} 個であった。）",
        "stock1": "（のちに帳簿が改められ、{name}倉庫の在庫数は {value} 個に訂正された。）",
        "balloon": "（その日、紫色の風船がひとつ空へ上がっていった。）",
        "code_key": "{name}灯台の点検番号", "stock_key": "{name}倉庫の在庫数（最新）",
        "count_key": "紫色の風船が空へ上がった回数",
        "question": ("上の文章には、括弧書きの記録がところどころに挿入されています。文章だけを根拠に、"
                     "下の形式の JSON を埋めて返してください。在庫数は訂正があれば訂正後の値を答えてください。"
                     "見つからない項目は「不明」と書いてください。JSON 以外は出力しないでください。\n{schema}"),
        "sep": "。",
    },
    "en": {
        "code": " (According to the records, the inspection number of {name} Lighthouse was {value}.) ",
        "stock0": " (According to the ledger, the stock count of {name} Warehouse was {value}.) ",
        "stock1": " (The ledger was later corrected: the stock count of {name} Warehouse was revised to {value}.) ",
        "balloon": " (That day, a purple balloon floated up into the sky.) ",
        "code_key": "inspection number of {name} Lighthouse", "stock_key": "latest stock count of {name} Warehouse",
        "count_key": "number of times a purple balloon floated up",
        "question": ("Parenthetical records have been inserted here and there in the text above. Using only the text, "
                     "fill in the JSON below. For stock counts, give the corrected value if there was a correction. "
                     "Write \"unknown\" for anything you cannot find. Output only the JSON.\n{schema}"),
        "sep": ". ",
    },
}


def build(lang, length, seed):
    t = TEXT[lang]
    rng = random.Random(f"{lang}-{length}-{seed}")
    corpus = (ROOT / "corpus" / f"{lang}.txt").read_text(encoding="utf-8")
    n_chars = int(length * CHARS_PER_TOKEN[lang])
    if n_chars >= len(corpus):
        raise ValueError(f"コーパス不足: {lang} {n_chars:,} 字 > {len(corpus):,} 字")
    start = rng.randrange(len(corpus) - n_chars)
    body = corpus[start:start + n_chars]

    names = rng.sample(NAMES, 25)
    code = lambda: f"{''.join(rng.choices(string.ascii_uppercase, k=2))}-{rng.randint(1000, 9999)}"
    codes = [{"name": n, "value": code(), "depth": 0.025 + 0.95 * i / 19} for i, n in enumerate(names[:20])]
    stocks = []
    for n in names[20:]:
        v0, v1 = rng.sample(range(100, 999), 2)
        stocks.append({"name": n, "old": v0, "value": v1, "d0": rng.uniform(0.10, 0.30), "d1": rng.uniform(0.60, 0.90)})
    k = rng.randint(7, 15)
    balloons = sorted(rng.uniform(0.02, 0.98) for _ in range(k))

    inserts = ([(c["depth"], t["code"].format(**c)) for c in codes]
               + [(s["d0"], t["stock0"].format(name=s["name"], value=s["old"])) for s in stocks]
               + [(s["d1"], t["stock1"].format(**s)) for s in stocks]
               + [(d, t["balloon"]) for d in balloons])
    # 後ろから挿入して位置がずれないようにする。挿入点は直後の文末に合わせる
    for depth, text in sorted(inserts, reverse=True):
        p = int(len(body) * depth)
        q = body.find(t["sep"], p)
        p = q + len(t["sep"]) if 0 <= q < p + 2000 else p
        body = body[:p] + text + body[p:]

    schema = {t["code_key"].format(name=c["name"]): "" for c in codes}
    schema.update({t["stock_key"].format(name=s["name"]): "" for s in stocks})
    schema[t["count_key"]] = ""
    question = t["question"].format(schema=json.dumps(schema, ensure_ascii=False, indent=1))
    return body, question, {"codes": codes, "stocks": stocks, "balloons": k}


def grade(text, lang, truth):
    t = TEXT[lang]
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        raw = {}
    first = lambda x, pat: (re.search(pat, str(x).upper()) or [None])[0] if x is not None else None
    codes = [{"depth": round(c["depth"], 3),
              "ok": first(raw.get(t["code_key"].format(name=c["name"])), r"[A-Z]{2}-\d{4}") == c["value"]}
             for c in truth["codes"]]
    stocks = []
    for s in truth["stocks"]:
        a = first(raw.get(t["stock_key"].format(name=s["name"])), r"\d{3}")
        stocks.append("new" if a == str(s["value"]) else "old" if a == str(s["old"]) else "wrong")
    cnt = first(raw.get(t["count_key"]), r"\d+")
    return {"parsed": bool(raw), "codes": codes, "code_acc": sum(c["ok"] for c in codes) / len(codes),
            "stocks": stocks, "stock_new": stocks.count("new") / len(stocks), "stock_old": stocks.count("old") / len(stocks),
            "count_answer": int(cnt) if cnt else None, "count_truth": truth["balloons"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5-5[1m]")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--langs", nargs="+", default=["ja", "en"])
    ap.add_argument("--lengths", nargs="+", type=int, default=[100_000, 300_000, 600_000, 900_000])
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    out_dir = RESULTS / "e6"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.model.replace("[", "_").replace("]", "")
    for length in sorted(args.lengths):          # 短い順に実行する
        for lang in args.langs:
            path = out_dir / f"{tag}_{lang}_{length // 1000}k_s{args.seed}.json"
            if path.exists():
                continue
            body, question, truth = build(lang, length, args.seed)
            t0 = time.time()
            try:
                res = run_claude(question, body, model=args.model, effort=args.effort, timeout=3600)
            except UsageLimitError as e:
                sys.exit(f"停止: {e}\n同じコマンドで再開できます。")
            g = grade(res["result"], lang, truth)
            rec = {"model": args.model, "lang": lang, "length": length, "seed": args.seed, "ctx": ctx_tokens(res),
                   "sec": round(time.time() - t0, 1), "answer": res["result"], "truth": truth, **g,
                   "usage": res.get("usage")}
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{lang} {length // 1000}K: ctx={rec['ctx'] // 1000}K codes={g['code_acc']:.0%} "
                  f"stock_new={g['stock_new']:.0%} stock_old={g['stock_old']:.0%} "
                  f"count={g['count_answer']}/{g['count_truth']} ({rec['sec']}s)")


if __name__ == "__main__":
    main()
