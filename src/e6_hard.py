"""E6-hard: 1M 付近での劣化を、より難しい課題で探す（日本語・単発）。

仕込むもの:
- 言い換え ×8: 記録と質問で語彙をずらす
- ディストラクタ ×5: 似た名前・別の建物・改番前の旧番号を混ぜる
- multi-hop ×5: 「人→組」（前半）と「組→集合場所」（後半）を組み合わせる
- 複数回更新 ×3: 4 回更新される値の最新値と最初の値
- 条件つき数え上げ: 紫色の風船（赤い風船・紫色の凧が紛れる）

使い方:
    python src/e6_hard.py --length 900000 [--model "claude-opus-5-5[1m]"] [--seed 0]
結果: results/e6/hard_<model>_ja_<長さ>k_s<seed>.json
"""
import argparse
import json
import random
import re
import string
import sys
import time

from common import ROOT, RESULTS, UsageLimitError, ctx_tokens, run_claude

CHARS_PER_TOKEN = 0.97

SEMANTIC = [
    ("（港の古い測候所に勤めていた男は、毎朝きまって {n} 段の石段をのぼって仕事場へ向かった。）",
     "海辺の気象観測施設の職員が毎朝の通勤で登った段数"),
    ("（町はずれの製本所では、ひと月に {n} 冊の帳面が綴じられていたという。）",
     "郊外の本を綴じる工房が一か月に仕上げたノートの数"),
    ("（祖母の家の柱時計は、一日に {n} 秒ずつ遅れるくせがあった。）",
     "おばあさんの住まいにあった壁掛けの時計が毎日ずれる秒数"),
    ("（峠の茶屋の主人は、若いころに {n} 匹の鯉を池で育てていた。）",
     "山道の休憩所を営む人物がかつて飼っていた魚の数"),
    ("（その寺の鐘は、鋳造されてから {n} 年のあいだ一度もひびが入らなかった。）",
     "寺院の釣り鐘が無傷のまま保たれた年数"),
    ("（川向こうの染物屋は、藍甕を {n} 個も並べて仕事をしていた。）",
     "対岸の布を染める店が使っていた染料の容器の数"),
    ("（学校の裏山には、明治の初めに植えられた杉が {n} 本残っていた。）",
     "校舎の背後の丘に残っている古い針葉樹の本数"),
    ("（鍛冶屋の親方は、生涯で {n} 本の鎌を打ったと自慢していた。）",
     "金属を鍛える職人の頭が一生のうちに作った草刈り道具の数"),
]
DISTRACT = [("ハートリー", "ハートレー"), ("ウィットビー", "ウィットベー"), ("ペンリス", "ペンリズ"),
            ("セルビー", "セルベー"), ("ソーントン", "ソートン")]
PEOPLE = ["杉浦", "小野寺", "丹羽", "早瀬", "久保田"]
GROUPS = ["青", "緑", "橙", "灰", "白"]
PLACES = ["北門", "東の倉庫前", "旧講堂", "給水塔の下", "船着き場"]
CARS = ["三号車", "七号車", "九号車"]


def build(length, seed):
    rng = random.Random(f"hard-{length}-{seed}")
    corpus = (ROOT / "corpus" / "ja.txt").read_text(encoding="utf-8")
    n_chars = int(length * CHARS_PER_TOKEN)
    start = rng.randrange(len(corpus) - n_chars)
    body = corpus[start:start + n_chars]
    code = lambda: f"{''.join(rng.choices(string.ascii_uppercase, k=2))}-{rng.randint(1000, 9999)}"

    ins, truth, schema = [], {}, {}
    nums = rng.sample(range(100, 999), len(SEMANTIC))
    for i, ((rec, q), n) in enumerate(zip(SEMANTIC, nums)):
        ins.append((0.05 + 0.9 * i / (len(SEMANTIC) - 1), rec.format(n=n)))
        truth[q] = {"type": "semantic", "answer": str(n)}

    for name, near in DISTRACT:
        real, fake_near, fake_obj, old = code(), code(), code(), code()
        ins += [(rng.uniform(0.02, 0.98), f"（記録によれば、{name}灯台の点検番号は {real} であった。）"),
                (rng.uniform(0.02, 0.98), f"（記録によれば、{near}灯台の点検番号は {fake_near} であった。）"),
                (rng.uniform(0.02, 0.98), f"（記録によれば、{name}倉庫の点検番号は {fake_obj} であった。）"),
                (rng.uniform(0.02, 0.98), f"（{name}灯台の点検番号は、改番される前は {old} であった。）")]
        truth[f"{name}灯台の現在の点検番号"] = {"type": "distractor", "answer": real,
                                         "traps": {"near": fake_near, "obj": fake_obj, "old": old}}

    groups, places = rng.sample(GROUPS, 5), rng.sample(PLACES, 5)
    for p, g, pl in zip(PEOPLE, groups, places):
        ins += [(rng.uniform(0.05, 0.35), f"（名簿によれば、{p}さんは{g}組に属していた。）"),
                (rng.uniform(0.65, 0.95), f"（{g}組の集合場所は{pl}と定められていた。）")]
        truth[f"{p}さんが集合すべき場所"] = {"type": "multihop", "answer": pl}

    for car in CARS:
        vals = sorted(rng.sample(range(1000, 9999), 4))
        ds = sorted(rng.uniform(0.05, 0.95) for _ in range(4))
        ins += [(d, f"（{car}の走行距離計は {v} キロを示していた。）") for d, v in zip(ds, vals)]
        truth[f"{car}の走行距離計の最新の値"] = {"type": "latest", "answer": str(vals[-1]), "first": str(vals[0])}
        truth[f"{car}の走行距離計の最初の記録の値"] = {"type": "first", "answer": str(vals[0])}

    k, m, j = rng.randint(5, 12), rng.randint(5, 12), rng.randint(5, 12)
    ins += [(rng.uniform(0.02, 0.98), "（その日、紫色の風船がひとつ空へ上がっていった。）") for _ in range(k)]
    ins += [(rng.uniform(0.02, 0.98), "（その日、赤い風船がひとつ空へ上がっていった。）") for _ in range(m)]
    ins += [(rng.uniform(0.02, 0.98), "（その日、紫色の凧がひとつ空へ上がっていった。）") for _ in range(j)]
    truth["紫色の風船が空へ上がった回数"] = {"type": "count", "answer": str(k), "red": m, "kite": j}

    for depth, text in sorted(ins, reverse=True):
        p = int(len(body) * depth)
        q = body.find("。", p)
        p = q + 1 if 0 <= q < p + 2000 else p
        body = body[:p] + text + body[p:]

    keys = list(truth)
    rng.shuffle(keys)
    schema = {k_: "" for k_ in keys}
    question = ("上の文章には、括弧書きの記録がところどころに挿入されています。文章だけを根拠に、"
                "下の形式の JSON を埋めて返してください。質問の言葉づかいは記録と違うことがあります。"
                "見つからない項目は「不明」と書いてください。JSON 以外は出力しないでください。\n"
                + json.dumps(schema, ensure_ascii=False, indent=1))
    return body, question, truth


def grade(text, truth):
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        raw = {}
    items = {}
    for key, t in truth.items():
        a = str(raw.get(key, ""))
        if t["type"] == "multihop":
            ok = t["answer"] in a
        elif t["type"] == "distractor":
            got = (re.search(r"[A-Z]{2}-\d{4}", a.upper()) or [None])[0]
            ok = got == t["answer"]
            trap = next((k for k, v in t["traps"].items() if v == got), None)
            items[key] = {"type": t["type"], "ok": ok, "got": got, "trap": trap}
            continue
        else:
            got = (re.search(r"\d+", a) or [None])[0]
            ok = got == t["answer"]
        items[key] = {"type": t["type"], "ok": ok, "got": a}
    by_type = {}
    for v in items.values():
        by_type.setdefault(v["type"], []).append(v["ok"])
    return {"parsed": bool(raw), "items": items,
            "by_type": {k: f"{sum(v)}/{len(v)}" for k, v in by_type.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5-5[1m]")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--length", type=int, default=900_000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    tag = args.model.replace("[", "_").replace("]", "")
    path = RESULTS / "e6" / f"hard_{tag}_ja_{args.length // 1000}k_s{args.seed}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        sys.exit(f"既にあります: {path}")
    body, question, truth = build(args.length, args.seed)
    t0 = time.time()
    try:
        res = run_claude(question, body, model=args.model, effort=args.effort, timeout=3600)
    except UsageLimitError as e:
        sys.exit(f"停止: {e}")
    g = grade(res["result"], truth)
    rec = {"model": args.model, "length": args.length, "seed": args.seed, "ctx": ctx_tokens(res),
           "sec": round(time.time() - t0, 1), "answer": res["result"], "truth": truth, **g, "usage": res.get("usage")}
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ctx={rec['ctx'] // 1000}K ({rec['sec']}s) {g['by_type']}")
    for k, v in g["items"].items():
        if not v["ok"]:
            print("  NG", k, v)


if __name__ == "__main__":
    main()
