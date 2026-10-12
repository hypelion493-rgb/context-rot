"""corpus/raw/ のダウンロード物から、注記を除いた本文テキストを作る。

    python -I src/prep_corpus.py

出力: corpus/ja.txt, corpus/en.txt（作品ごとに区切り行を入れて連結）
"""
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "corpus" / "raw"


def aozora_body(text):
    lines = text.splitlines()
    title = lines[0].strip()
    # 冒頭の記号説明ブロック（-------- で挟まれた部分）を除く
    dashes = [i for i, l in enumerate(lines) if l.startswith("-----")]
    if len(dashes) >= 2:
        lines = lines[dashes[1] + 1:]
    body = "\n".join(lines)
    body = body.split("底本：")[0]           # 末尾の底本情報を除く
    body = re.sub(r"《[^》]*》", "", body)    # ルビ
    body = re.sub(r"［＃[^］]*］", "", body)  # 入力者注記
    body = body.replace("｜", "")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title, body.strip()


def gutenberg_body(text):
    title = re.search(r"Title: (.+)", text).group(1).strip()
    start = re.search(r"\*\*\* ?START OF.*?\*\*\*", text)
    end = re.search(r"\*\*\* ?END OF", text)
    body = text[start.end():end.start()] if start and end else text
    body = re.sub(r"\r\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title, body.strip()


def main():
    ja = []
    for z in sorted((RAW / "ja").glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".txt"))
            title, body = aozora_body(zf.read(name).decode("cp932", errors="replace"))
        ja.append((title, body))
    en = [gutenberg_body(p.read_text(encoding="utf-8-sig")) for p in sorted((RAW / "en").glob("*.txt"))]

    for lang, works in (("ja", ja), ("en", en)):
        out = "\n\n".join(body for _, body in works)
        (ROOT / "corpus" / f"{lang}.txt").write_text(out, encoding="utf-8")
        for title, body in works:
            print(f"{lang} {title}: {len(body):,} 字")
        print(f"{lang} 合計: {len(out):,} 字\n")


if __name__ == "__main__":
    main()
