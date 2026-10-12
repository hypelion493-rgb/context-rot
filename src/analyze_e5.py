"""E5 の結果を集計して Markdown の表にする。

    python src/analyze_e5.py [run_id の部分文字列]   # 標準出力に表示し、reports/e5_summary.md に保存
"""
import json
import sys

from common import ROOT, RESULTS


def load(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def pct(x):
    return f"{x:.0%}"


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    lines = ["# E5 結果サマリ", ""]
    for d in sorted((RESULTS / "e5").iterdir()):
        if not d.is_dir() or key not in d.name or not (d / "state.json").exists():
            continue
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        turns, probes = load(d / "turns.jsonl"), load(d / "probes.jsonl")
        a = state["args"]
        lines += [f"## {d.name}", "",
                  f"- モデル: `{a['model']}` / 言語: {a['lang']} / compaction 窓: {a['window']:,} / "
                  f"ターン: {state['done']} / compaction 回数: {turns[-1]['compactions'] if turns else 0}", ""]

        lines += ["| 時点 | ターン | 実コンテキスト | 承認コード正答 | ポート（新値） | ポート（古い値） | ルール想起 |",
                  "|---|---|---|---|---|---|---|"]
        for p in probes:
            lines.append(f"| {p['checkpoint']} | {p['turn']} | {p['ctx'] // 1000}K | {pct(p['fact_acc'])} | "
                         f"{pct(p['var_new'])} | {pct(p['var_old'])} | {sum(p['rules_recalled'])}/3 |")
        lines.append("")

        # compaction の前後でのルール遵守率
        if turns:
            first = next((t["turn"] for t in turns if t["compactions"] > 0), None)
            seg = {"compaction 前": [t for t in turns if first is None or t["turn"] < first],
                   "compaction 後": [t for t in turns if first is not None and t["turn"] >= first]}
            lines += ["| 区間 | ターン数 | 冒頭ルール | 末尾ルール | 3行以内 |", "|---|---|---|---|---|"]
            for name, ts in seg.items():
                if ts:
                    r = [sum(t["rules"][k] for t in ts) / len(ts) for k in range(3)]
                    lines.append(f"| {name} | {len(ts)} | {pct(r[0])} | {pct(r[1])} | {pct(r[2])} |")
            lines.append("")

        if state.get("compaction_summaries"):
            lines += ["| compaction | 前→後 | 要約に残った承認コード | ポート新値 | ポート古い値 |", "|---|---|---|---|---|"]
            for i, c in enumerate(state["compaction_summaries"], 1):
                lines.append(f"| {i} | {c['preTokens'] // 1000}K→{c['postTokens'] // 1000}K | {c['facts_kept']}/10 | "
                             f"{c['var_new_kept']}/5 | {c['var_old_kept']}/5 |")
            lines.append("")

    text = "\n".join(lines)
    print(text)
    out = ROOT / "reports" / "e5_summary.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
