#!/usr/bin/env python3
"""定期実行のジョブを棚卸しして、黙って壊れる作りになっていないかを当たる。

読むだけで、ジョブの登録・解除・書き換えはしない。標準ライブラリだけで動く。

    python3 scan_jobs.py                     # launchd + cron + ./.github/workflows
    python3 scan_jobs.py --repo ~/src/app    # そのリポの workflows も見る
    python3 scan_jobs.py --json              # 機械で読む形で出す
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

# 通知らしき呼び出し(失敗が人に届く経路があるか)
NOTIFY = re.compile(
    r"webhook|slack|discord|mailx?\b|sendmail|smtplib|notify|osascript .*display|"
    r"pushover|line/v2/bot|telegram|curl .*hooks\.",
    re.I,
)
# 失敗のときだけ動かしている痕跡
ON_FAILURE = re.compile(r"\$\?|exit\s+[1-9]|returncode|CalledProcessError|\|\||if:\s*failure\(\)|trap ", re.I)
# 二度打ちを防ぐ痕跡
IDEMPOTENT = re.compile(r"\.jsonl|ledger|\.lock\b|flock|lockfile|already|dedup|seen_|idempot|concurrency:", re.I)
# 秘密の直書きらしき行
SECRET = re.compile(
    r"(?:api[_-]?key|secret|token|password|passwd|webhook_url)\s*[=:]\s*[\"']?[A-Za-z0-9_\-/+]{16,}",
    re.I,
)
SECRET_SAFE = re.compile(r"\$\{?[A-Z_]+\}?|os\.environ|getenv|secrets\.|\$\(.*\)|<[^>]+>", re.I)


def read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


REF = re.compile(r"[\w./$~{}-]+\.(?:sh|bash|zsh|py|rb|pl)\b")
CODE_SUFFIX = {".sh", ".bash", ".zsh", ".py", ".rb", ".pl", ""}


def _expand(token: str, base: Path) -> Path | None:
    """スクリプトの中に出てきたパスらしき文字列を、読めるファイルに解決する。"""
    t = re.sub(r"\$\{?\w+\}?", "", os.path.expanduser(token)).strip("\"'")
    if not t or t.endswith("/"):
        return None
    for cand in (Path(t), base / t.lstrip("/"), base / Path(t).name):
        if cand.is_file():
            return cand
    return None


def inspect_program(parts: list[str], depth: int = 2) -> dict:
    """ジョブが呼ぶスクリプトと、そこから呼ばれるスクリプトを見て 1・2・3・6・7 に当たる。

    ラッパーの .sh が本体の .py を呼ぶ形が多いので、参照先も 1〜2 段たどって結果を足し合わせる。
    """
    out = {"script": "", "notify": None, "on_failure": None, "idempotent": None,
           "set_e": None, "secret_line": None, "read": []}
    queue = [Path(os.path.expanduser(p)) for p in parts]
    seen: set[Path] = set()
    while queue and len(seen) <= 4:
        cand = queue.pop(0)
        if cand in seen or not cand.is_file() or cand.suffix not in CODE_SUFFIX:
            continue
        body = read(cand)
        if not body:
            continue
        seen.add(cand)
        out["read"].append(str(cand))
        if not out["script"]:
            out["script"] = str(cand)
            out["set_e"] = ("set -e" in body or "set -o errexit" in body) if cand.suffix != ".py" else None
        for key, rx in (("notify", NOTIFY), ("on_failure", ON_FAILURE), ("idempotent", IDEMPOTENT)):
            out[key] = bool(out[key]) or bool(rx.search(body))
        if not out["secret_line"]:
            for line in body.splitlines():
                if SECRET.search(line) and not SECRET_SAFE.search(line):
                    out["secret_line"] = line.strip()[:60] + " …"
                    break
        if len(seen) <= depth:
            for token in REF.findall(body)[:40]:
                nxt = _expand(token, cand.parent)
                if nxt and nxt not in seen:
                    queue.append(nxt)
    return out


def when_launchd(d: dict) -> str:
    if "StartInterval" in d:
        return f"{d['StartInterval']} 秒ごと"
    cal = d.get("StartCalendarInterval")
    if cal is None:
        return "起動時 / 常駐" if d.get("RunAtLoad") or d.get("KeepAlive") else "(引き金なし)"
    items = cal if isinstance(cal, list) else [cal]
    wd = "日月火水木金土"
    outs = []
    for c in items:
        s = ""
        if "Weekday" in c:
            s += f"{wd[c['Weekday'] % 7]}曜 "
        if "Month" in c or "Day" in c:
            s += f"{c.get('Month', '毎月')}/{c.get('Day', '?')} "
        outs.append(s + f"{c.get('Hour', '毎時')}:{str(c.get('Minute', 0)).zfill(2)}")
    return " , ".join(outs[:4]) + (" …" if len(outs) > 4 else "")


def is_vendor(parts: list[str]) -> bool:
    """自分で書いたジョブか、入れたアプリが勝手に置いたジョブかを分ける。"""
    if not parts:
        return True  # 実行するものを書いていない= アプリが独自の鍵で登録している
    joined = " ".join(parts)
    return (parts[0].startswith(("/Applications/", "/Library/", "/System/", "/usr/", "/opt/"))
            or ".app" in joined
            or "/Library/" in joined)   # ~/Library 配下はアプリが置いた常駐が多い


def scan_launchd(include_vendor: bool = False) -> list[dict]:
    jobs = []
    if sys.platform != "darwin":
        return jobs
    codes = {}
    try:
        for line in subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                                   timeout=20).stdout.splitlines()[1:]:
            f = line.split("\t")
            if len(f) >= 3:
                codes[f[2]] = f[1]
    except (OSError, subprocess.SubprocessError):
        pass
    for base in (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents")):
        for plist in sorted(base.glob("*.plist")) if base.is_dir() else []:
            try:
                with plist.open("rb") as fh:
                    d = plistlib.load(fh)
            except (OSError, ValueError):
                continue
            label = d.get("Label", plist.stem)
            parts = d.get("ProgramArguments") or [x for x in (d.get("Program"), d.get("BundleProgram")) if x]
            if not include_vendor and is_vendor(parts):
                continue
            job = {"kind": "launchd", "name": label, "when": when_launchd(d),
                   "runs": " ".join(parts)[:70], "log": d.get("StandardOutPath") or d.get("StandardErrorPath") or "",
                   "last_exit": codes.get(label, "(未登録)")}
            job.update(inspect_program(parts))
            jobs.append(job)
    return jobs


def scan_cron() -> list[dict]:
    jobs = []
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return jobs
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        f = line.split(None, 5)
        if len(f) < 6:
            continue
        cmd = f[5]
        job = {"kind": "cron", "name": cmd.split()[0].split("/")[-1][:30], "when": " ".join(f[:5]),
               "runs": cmd[:70], "log": ">" in cmd and cmd.split(">")[-1].strip() or "",
               "last_exit": "-"}
        job.update(inspect_program([w for w in cmd.split() if "/" in w]))
        jobs.append(job)
    return jobs


def scan_actions(repo: Path) -> list[dict]:
    jobs = []
    wf = repo / ".github/workflows"
    for path in sorted(wf.glob("*.y*ml")) if wf.is_dir() else []:
        body = read(path)
        crons = re.findall(r"cron:\s*[\"']([^\"']+)[\"']", body)
        if not crons:
            continue
        jobs.append({
            "kind": "actions", "name": path.name, "when": " , ".join(crons) + " (UTC)",
            "runs": str(path), "log": "GitHub 上に残る", "last_exit": "-", "script": str(path),
            "notify": bool(NOTIFY.search(body)), "on_failure": bool(ON_FAILURE.search(body)),
            "idempotent": bool(IDEMPOTENT.search(body)), "set_e": None,
            "secret_line": next((l.strip()[:60] + " …" for l in body.splitlines()
                                 if SECRET.search(l) and not SECRET_SAFE.search(l)), None),
        })
    return jobs


def findings(job: dict) -> list[str]:
    out = []
    if job.get("notify") is False:
        out.append("1 失敗が届かない")
    elif job.get("notify") and not job.get("on_failure"):
        out.append("2 通知が失敗時だけか要確認")
    if job.get("idempotent") is False:
        out.append("3 二度打ちの防ぎがない")
    if job.get("secret_line"):
        out.append("6 秘密の直書きの疑い")
    if job["kind"] == "launchd" and job.get("set_e") is False and not job.get("on_failure"):
        out.append("7 落ちたあとも後段が走る")
    if not job.get("log"):
        out.append("9 ログの行き先がない")
    if job.get("last_exit") not in ("0", "-", "(未登録)", None):
        out.append(f"! 直近の終了コード {job['last_exit']}")
    if not job.get("script"):
        out.append("? 実行するものを読めなかった")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="GitHub Actions を見るリポジトリ(既定: カレント)")
    ap.add_argument("--json", action="store_true", help="機械で読む形で出す")
    ap.add_argument("--all", action="store_true",
                    help="アプリが置いた常駐ジョブ(/Applications 配下など)も含める")
    a = ap.parse_args()

    jobs = scan_launchd(a.all) + scan_cron() + scan_actions(Path(os.path.expanduser(a.repo)))
    for j in jobs:
        j["findings"] = findings(j)

    if a.json:
        print(json.dumps(jobs, ensure_ascii=False, indent=2))
        return 0

    if not jobs:
        print("定期ジョブは見つからなかった。--repo でリポジトリを指しているか確かめる。")
        return 0

    w = max((len(j["name"]) for j in jobs), default=10)
    print(f"{'ジョブ'.ljust(w)}  {'いつ'.ljust(22)}  点検")
    print("-" * (w + 40))
    for j in sorted(jobs, key=lambda x: (-len(x["findings"]), x["name"])):
        note = " / ".join(j["findings"]) if j["findings"] else "ひっかかりなし"
        print(f"{j['name'].ljust(w)}  {j['when'][:22].ljust(22)}  {note}")

    flagged = sum(1 for j in jobs if j["findings"])
    print(f"\n{len(jobs)} 件中 {flagged} 件に見るところがある。番号は reference/checklist.md に対応。")
    print("4(出力の鮮度)・5(あるべき一覧との差)・8(人へ渡す経路)・10(端末が寝ていたとき)は目で見る。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
