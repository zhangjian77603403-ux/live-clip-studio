#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live Clip Studio · 本地工作台

只读浏览：扫描「视频项目」下符合该模式的项目（20_ / 21_ / 22_ …），
汇总成片、定妆照、八镜抽帧、一致性指标与审核记录。

只监听 127.0.0.1，不修改任何项目文件，不做生成。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # 随包资源
BASE = Path(sys.executable).resolve().parent if FROZEN else RES        # 可写目录
WEB = RES / "web"
CACHE = BASE / "cache"
_cfg = BASE / "config.json"
if not _cfg.is_file():
    _cfg = RES / "config.json"
try:
    CFG = json.loads(_cfg.read_text(encoding="utf-8"))
except (OSError, ValueError):
    CFG = {}                              # 没有配置文件也能起，全部走默认值
PORT = int(CFG.get("port", 8801))
# project_root 留空时回落到「程序目录/视频项目」，首次运行自动建，保证开箱能起
_root_cfg = (CFG.get("project_root") or "").strip()
ROOT = Path(_root_cfg) if _root_cfg else (BASE / "视频项目")
try:
    ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
CFG_PATH = BASE / "config.json"          # 运行时可写的那份
# 被「删除」的项目只记在这里 —— 只从工作台列表里移走，磁盘文件一个都不动
HIDDEN: set[str] = {str(x).lower() for x in (CFG.get("hidden") or []) if str(x).strip()}

VIDEO_EXT = {".mp4", ".mov"}
_probe_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- 子进程隐藏
# GUI 版没有控制台，拉起 ffprobe/ffmpeg 时 Windows 会给每个子进程配一个控制台窗口，
# 表现就是屏幕上黑窗一闪一闪。必须显式隐藏。
_CREATE_NO_WINDOW = 0x08000000


def _hidden() -> dict:
    kw: dict = {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
        kw["creationflags"] = _CREATE_NO_WINDOW
    except Exception:
        pass
    return kw


# ---------------------------------------------------------------- 列表移除
def _save_hidden() -> None:
    d = dict(CFG)
    d["hidden"] = sorted(HIDDEN)
    try:
        CFG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[warn] 写 config.json 失败：{e}")


def set_hidden(name: str, on: bool) -> None:
    """把项目目录名加进/移出隐藏名单。不碰磁盘上的任何文件。"""
    with _lock:
        if on:
            HIDDEN.add(name.lower())
        else:
            HIDDEN.discard(name.lower())
        _save_hidden()


# ---------------------------------------------------------------- 媒体探测
def probe(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_size, int(st.st_mtime))
    with _lock:
        if key in _probe_cache:
            return _probe_cache[key]
    info: dict = {}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate:format=duration,size",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=8,
            **_hidden(),
        )
        d = json.loads(r.stdout or "{}")
        fmt = d.get("format", {})
        if fmt.get("duration"):
            info["duration"] = round(float(fmt["duration"]), 2)
        v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
        if v:
            info["width"] = v.get("width")
            info["height"] = v.get("height")
            fr = v.get("r_frame_rate") or ""
            if "/" in fr:
                a, b = fr.split("/")[:2]
                if float(b or 1):
                    info["fps"] = round(float(a) / float(b or 1), 2)
        info["audio"] = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
    except Exception:
        pass
    info["size"] = st.st_size
    with _lock:
        _probe_cache[key] = info
    return info


# ---------------------------------------------------------------- 抽帧
def grab(video: Path, at: float = 1.0, width: int = 420) -> Path | None:
    """抽一帧缓存成 jpg；带并发去重，避免同时抽同一张。"""
    try:
        st = video.stat()
    except OSError:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{video}|{st.st_size}|{int(st.st_mtime)}|{at}|{width}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return out
    for ss in (f"{at:.2f}", "0"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(video),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)],
                capture_output=True, timeout=30, **_hidden(),
            )
            if out.is_file() and out.stat().st_size > 0:
                return out
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 指标提取
def _dig(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def read_metrics(proj: Path) -> dict:
    """从 05_审核记录 里提取可展示的指标，键名跨版本容错。"""
    out: dict = {}
    d = proj / "05_审核记录"
    if not d.is_dir():
        return out

    # 1) 一致性报告（F0 / 质心 / 逐字）
    cr = d / "consistency_report.json"
    if cr.is_file():
        try:
            j = json.loads(cr.read_text(encoding="utf-8"))
            f0 = _dig(j, "checks", "voice_f0") or {}
            ce = _dig(j, "checks", "voice_centroid") or {}
            out["f0_spread"] = f0.get("spread_pct")
            out["f0_range"] = (f0.get("min_hz"), f0.get("max_hz")) if f0.get("min_hz") else None
            out["cen_spread"] = ce.get("spread_pct")
            em = _dig(j, "checks", "exact_script_match")
            if isinstance(em, dict):
                out["asr_hit"] = f"{em.get('matched')}/{em.get('total')}"
            elif isinstance(em, str):
                out["asr_hit"] = em
        except Exception:
            pass

    # 2) 审核记录（响度 / TP / 结论 / 已知问题）
    cands = sorted(d.glob("审核*.json"), key=lambda p: -p.stat().st_mtime)
    for p in cands:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        dv = j.get("deliverable") or {}
        if dv.get("lufs") is not None and "lufs" not in out:
            out["lufs"] = dv.get("lufs")
            out["tp"] = dv.get("true_peak_db")
        if j.get("verdict") and "verdict" not in out:
            out["verdict"] = j["verdict"]
        if j.get("known_issues") and "issues" not in out:
            out["issues"] = j["known_issues"]
        out.setdefault("audit_file", str(p))
        if len(out) > 6:
            break
    return out


def read_image_consistency(proj: Path) -> dict:
    d = proj / "05_审核记录"
    for p in sorted(d.glob("审核*.json"), key=lambda x: -x.stat().st_mtime) if d.is_dir() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            ic = j.get("image_consistency")
            if isinstance(ic, dict) and ic:
                return ic
        except Exception:
            continue
    return {}


# ---------------------------------------------------------------- 资产定位
def pick_videos(proj: Path) -> list[Path]:
    d = proj / "11_成片"
    if not d.is_dir():
        return []
    return sorted([f for f in d.iterdir() if f.suffix.lower() in VIDEO_EXT],
                  key=lambda x: (0 if "字幕" in x.name else 1, x.name))


def pick_ref(proj: Path) -> Path | None:
    r = proj / "04_分镜参考"
    if not r.is_dir():
        return None
    main = r / "host_scene_ref.png"
    if main.is_file():
        return main
    cand = sorted((r / "cand").glob("*.png")) if (r / "cand").is_dir() else []
    return cand[0] if cand else None


def pick_raw_dir(proj: Path) -> Path | None:
    """挑一套完整的八镜：优先 raw，其次文件名排序最后的 raw*。"""
    g = proj / "02_生成源"
    if not g.is_dir():
        return None
    cands = [x for x in g.iterdir() if x.is_dir() and re.match(r"^raw(_.*)?$", x.name)]
    if not cands:
        return None
    def score(x: Path):
        n = len(list(x.glob("S*.mp4")))
        pref = 1 if x.name == "raw" else 0
        return (n, pref, x.stat().st_mtime)
    return max(cands, key=score)


def pick_shots(proj: Path) -> list[dict]:
    d = pick_raw_dir(proj)
    if not d:
        return []
    out = []
    for f in sorted(d.glob("S*.mp4")):
        m = probe(f)
        out.append({"id": f.stem, "path": str(f), "duration": m.get("duration")})
    return out


def pick_product(proj: Path) -> Path | None:
    d = proj / "03_商品素材"
    if not d.is_dir():
        return None
    for name in ("bottle_front.png", "candle_front.png", "product.png"):
        if (d / name).is_file():
            return d / name
    imgs = sorted([f for f in d.iterdir() if f.suffix.lower() in {".png", ".jpg", ".webp"}])
    return imgs[0] if imgs else None


def read_task(proj: Path) -> dict:
    p = proj / "task.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 新建任务
TALENT_ROOT = ROOT / "00_常驻演员库"
_TALENT_PAT = re.compile(r"^([A-Za-z]{2}\d{2})_([^_]+).*\.(png|jpg|jpeg|webp)$", re.I)


def talents() -> list[dict]:
    """从常驻演员库扒可用主播。文件名形如 CN01_林悦_ZImage_v1.png。

    同一个编号可能散落在多个批次里（身份照 / 三视图 / ZImage 定妆），
    优先取 ZImage 定装照，其次取路径里带「定装/定妆」的，最后才是其它。
    """
    best: dict[str, tuple[int, dict]] = {}
    if not TALENT_ROOT.is_dir():
        return []
    for f in sorted(TALENT_ROOT.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        m = _TALENT_PAT.match(f.name)
        if not m:
            continue
        tid = m.group(1).upper()
        score = 0
        if "zimage" in f.name.lower():
            score += 3
        if "定装" in str(f) or "定妆" in str(f):
            score += 2
        if tid in best and best[tid][0] >= score:
            continue
        best[tid] = (score, {"id": tid, "name": f"{tid} {m.group(2)}", "path": str(f)})

    # 另一批素材按人建目录：…/CN071_谭映瑶/01_全身正面.png
    dpat = re.compile(r"^((?:CN|INT)\d{2,3})_(.+)$", re.I)
    for d in sorted(TALENT_ROOT.rglob("*")):
        if not d.is_dir():
            continue
        m = dpat.match(d.name)
        if not m:
            continue
        tid = m.group(1).upper()
        if tid in best:
            continue
        img = None
        for cand in ("01_全身正面.png", "01_正面.png", "00_三视图源图.png"):
            if (d / cand).is_file():
                img = d / cand
                break
        if img is None:
            pngs = sorted(f for f in d.glob("*.png") if f.is_file())
            img = pngs[0] if pngs else None
        if img is not None:
            best[tid] = (1, {"id": tid, "name": f"{tid} {m.group(2)}", "path": str(img)})

    return sorted((v[1] for v in best.values()), key=lambda x: x["id"])


def next_code() -> str:
    used = []
    if ROOT.is_dir():
        for d in ROOT.iterdir():
            if d.is_dir() and re.match(r"^\d{2}_", d.name):
                used.append(int(d.name[:2]))
    return f"{(max(used) + 1) if used else 1:02d}"


# 分辨率 × 画幅 → 实际像素（H3 要求 8 的倍数，这里直接写死常用档）
SIZE_TABLE = {
    ("480P", "9:16"): (480, 854), ("480P", "16:9"): (854, 480), ("480P", "1:1"): (480, 480),
    ("720P", "9:16"): (720, 1280), ("720P", "16:9"): (1280, 720), ("720P", "1:1"): (720, 720),
    ("1080P", "9:16"): (1080, 1920), ("1080P", "16:9"): (1920, 1080), ("1080P", "1:1"): (1080, 1080),
}


def frame_size(res: str, aspect: str) -> tuple[int, int]:
    return SIZE_TABLE.get(((res or "480P").upper(), aspect or "9:16"), (480, 854))


# 新建任务时就落盘的提示词骨架，界面里四个英文描述 + 每镜台词直接填
DEFAULT_ROOM_EN = (
    "a calm minimal home-study corner in daylight, she sits behind a pale oak desk, a smooth "
    "warm grey plaster wall behind her, a sheer white curtain softly diffused at frame right, "
    "nothing else on the desk"
)


def talent_defaults(talent: str) -> dict:
    """从常驻演员库的人物档案里取 appearance_en / wardrobe_en 预填提示词。"""
    tid = (talent or "").split(" ")[0].strip().upper()
    if not tid or not TALENT_ROOT.is_dir():
        return {}
    for f in TALENT_ROOT.rglob("人物档案.json"):
        if not f.parent.name.upper().startswith(tid):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        snap = d.get("generation_actor_snapshot") or d
        host = (snap.get("appearance_en") or d.get("appearance_en") or "").strip()
        ward = (snap.get("wardrobe_en") or d.get("wardrobe_en") or "").strip()
        if host:
            return {"host_en": host, "wardrobe_en": ward}
    return {}


def prompts_template(task: dict) -> dict:
    n = max(1, int(task.get("shots") or 8))
    auto = talent_defaults(task.get("talent", ""))
    return {
        "product_en": "",
        "host_en": auto.get("host_en", ""),
        "wardrobe_en": auto.get("wardrobe_en", ""),
        "room_en": DEFAULT_ROOM_EN,
        "product_seed": 2026111300,
        "seeds": [2026111301, 2026111302, 2026111303, 2026111304],
        "shots": [{"voice": "", "action_en": ""} for _ in range(n)],
    }


PROMPT_KEYS = ("product_en", "host_en", "wardrobe_en", "room_en")


def read_prompts(proj: Path) -> dict:
    p = proj / "01_方案" / "提示词.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    else:
        d = {}
    base = {"product_en": "", "host_en": "", "wardrobe_en": "", "room_en": "",
            "product_seed": 2026111300,
            "seeds": [2026111301, 2026111302, 2026111303, 2026111304],
            "shots": [{"voice": "", "action_en": ""}
                      for _ in range(max(1, int((read_task(proj) or {}).get("shots") or 8)))]}
    for k in PROMPT_KEYS:
        base[k] = d.get(k, "") or ""
    base["product_seed"] = d.get("product_seed", base["product_seed"])
    base["seeds"] = d.get("seeds") or base["seeds"]
    raw = d.get("shots") or []
    if raw:
        base["shots"] = raw
    base["_exists"] = p.is_file()
    return base


def save_prompts(proj: Path, payload: dict) -> dict:
    cur = read_prompts(proj)
    for k in PROMPT_KEYS:
        if k in payload:
            cur[k] = (payload.get(k) or "").strip()
    shots = payload.get("shots")
    if isinstance(shots, list):
        cur["shots"] = [
            {"voice": (s.get("voice") or "").strip(),
             "action_en": (s.get("action_en") or "").strip()}
            for s in shots[:40]]
    cur.pop("_exists", None)
    p = proj / "01_方案" / "提示词.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(p), "prompts": read_prompts(proj)}


def prompts_state(proj: Path) -> dict:
    """能不能跑②：四段描述齐 + 台词条数够。"""
    d = read_prompts(proj)
    task = read_task(proj) or {}
    n = max(1, int(task.get("shots") or 8))
    desc_ok = all((d.get(k) or "").strip() for k in ("product_en", "host_en", "room_en"))
    shots = d.get("shots") or []
    voices = [s for s in shots if (s.get("voice") or "").strip()]
    return {
        "desc_ok": desc_ok,
        "voices_ok": len(voices) >= n,
        "voices_have": min(len(voices), n),
        "need": n,
    }


def raw_shots(proj: Path) -> tuple[int, int]:
    d = proj / "02_生成源" / "raw"
    n = max(1, int((read_task(proj) or {}).get("shots") or 8))
    done = len(list(d.glob("S*.mp4"))) if d.is_dir() else 0
    return done, n


def final_video(proj: Path) -> dict | None:
    d = proj / "11_成片"
    if not d.is_dir():
        return None
    vids = sorted([f for f in d.glob("*.mp4")],
                  key=lambda f: f.stat().st_mtime, reverse=True)
    subbed = [f for f in vids if "字幕" in f.name]
    f = (subbed or vids or [None])[0]
    return {"name": f.name, "path": str(f)} if f else None


def create_task(product: str, talent: str, seconds: int, shots: int,
                resolution: str = "480P", aspect: str = "9:16") -> dict:
    """只建目录骨架 + task.json，不跑 GPU、不动任何已有文件。"""
    product = re.sub(r'[\\/:*?"<>|\r\n\t]+', "", product or "").strip()[:40]
    if not product:
        return {"ok": False, "error": "商品名不能为空"}
    if not ROOT.is_dir():
        return {"ok": False, "error": "项目根目录不存在"}
    resolution = (resolution or "480P").upper()
    if resolution not in {"480P", "720P", "1080P"}:
        resolution = "480P"
    aspect = aspect if aspect in {"9:16", "16:9", "1:1"} else "9:16"
    w, h = frame_size(resolution, aspect)
    code = next_code()
    name = f"{code}_{product}_直播切片_{seconds}秒"
    proj = ROOT / name
    if proj.exists():
        return {"ok": False, "error": f"目录已存在：{name}"}
    try:
        for sub in ("01_方案", "02_生成源", "03_商品素材", "04_分镜参考", "05_审核记录", "11_成片"):
            (proj / sub).mkdir(parents=True, exist_ok=True)
        task = {
            "code": code, "product": product, "talent": talent or "",
            "seconds": seconds, "shots": shots, "mode": "直播切片",
            "resolution": resolution, "aspect": aspect, "width": w, "height": h,
            "status": "待生成",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (proj / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        # 分镜台词骨架：新建即写盘，界面上直接可编辑，不用再手搓 JSON
        (proj / "01_方案" / "提示词.json").write_text(
            json.dumps(prompts_template(task), ensure_ascii=False, indent=2),
            encoding="utf-8")
        (proj / "01_方案" / "任务说明.md").write_text(
            f"# {product} · 直播切片 {seconds} 秒\n\n"
            f"- 编号：{code}\n"
            f"- 主播：{talent or '待定'}\n"
            f"- 时长：{seconds} 秒 / {shots} 镜（每镜 {round(seconds / max(1, shots), 2)} 秒）\n"
            f"- 规格：{resolution} · {aspect} · {w}×{h} · 24fps\n"
            f"- 模式：首尾同图（每镜 first=last=同一张定妆照）\n"
            f"- 音频：H3 原生生成 / -16 LUFS\n"
            f"- 状态：待生成\n", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"创建失败：{e}"}
    return {"ok": True, "path": str(proj), "name": name, "task": task}


# ---------------------------------------------------------------- 执行（调用本地引擎）
RUN_PROCS: dict = {}


def _python() -> str:
    for c in ("python", "python3"):
        w = shutil.which(c)
        if w:
            return w
    return "python"


def _set_status(proj: Path, status: str) -> None:
    d = read_task(proj)
    if not d:
        return
    d["status"] = status
    try:
        (proj / "task.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_progress(proj: Path) -> dict:
    p = proj / "02_生成源" / "progress.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def candidates(proj: Path) -> list[str]:
    d = proj / "04_分镜参考" / "cand"
    if not d.is_dir():
        return []
    return [str(f) for f in sorted(d.glob("host_*.png"))]


def start_stage(proj: Path, stage: str, regen: bool = False) -> dict:
    """在项目目录里拉起 pipeline/run_stage.py，日志写到 02_生成源/run.log。"""
    key = str(proj)
    p = RUN_PROCS.get(key)
    if p is not None and p.poll() is None:
        return {"ok": False, "error": "这个任务已有进程在跑"}
    script = RES / "pipeline" / "run_stage.py"
    if not script.is_file():
        return {"ok": False, "error": f"缺少执行脚本：{script}"}
    log = proj / "02_生成源" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8")
    fh.write(f"\n===== {time.strftime('%H:%M:%S')} stage={stage}"
             f"{' regen' if regen else ''} =====\n")
    fh.flush()
    cmd = [_python(), "-u", str(script), "--proj", str(proj), "--stage", stage]
    if regen and stage == "assets":
        cmd.append("--regen")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(proj), stdout=fh, stderr=subprocess.STDOUT, **_hidden())
    except Exception as e:
        fh.close()
        return {"ok": False, "error": f"启动失败：{e}"}
    RUN_PROCS[key] = proc
    _set_status(proj, "生成中" if stage != "final" else "合成中")
    return {"ok": True, "pid": proc.pid}


def commit_ref(proj: Path, src: Path) -> dict:
    dst = proj / "04_分镜参考" / "host_scene_ref.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    _set_status(proj, "定妆照已定")
    return {"ok": True, "ref": str(dst)}


# ---------------------------------------------------------------- 项目发现
def parse_name(name: str) -> dict:
    m = re.match(r"^(\d{2})_(.+)$", name)
    if not m:
        return {"code": "", "product": name, "mode": ""}
    code, rest = m.group(1), m.group(2)
    parts = rest.split("_")
    return {"code": code, "product": parts[0], "mode": "_".join(parts[1:])}


def build(proj: Path) -> dict:
    vids = []
    for v in pick_videos(proj):
        m = probe(v)
        vids.append({
            "name": v.name, "path": str(v), "size": m.get("size", 0),
            "duration": m.get("duration"), "width": m.get("width"),
            "height": m.get("height"), "fps": m.get("fps"),
        })
    ref = pick_ref(proj)
    prod = pick_product(proj)
    shots = pick_shots(proj)
    metrics = read_metrics(proj)
    icons = read_image_consistency(proj)
    info = parse_name(proj.name)

    files = []
    for sub in ("01_方案", "05_审核记录", "11_成片"):
        dd = proj / sub
        if dd.is_dir():
            for f in sorted(dd.iterdir()):
                if f.is_file():
                    files.append({"rel": f"{sub}/{f.name}", "path": str(f)})

    ts = [v["path"] for v in vids]
    mtimes = []
    for p in ts:
        try:
            mtimes.append(Path(p).stat().st_mtime)
        except OSError:
            pass
    if ref:
        mtimes.append(ref.stat().st_mtime)

    return {
        "id": hashlib.md5(str(proj).encode()).hexdigest()[:10],
        "code": info["code"],
        "name": proj.name,
        "product": info["product"],
        "mode": info["mode"],
        "path": str(proj),
        "task": read_task(proj),
        "videos": vids,
        "ref": str(ref) if ref else None,
        "product_img": str(prod) if prod else None,
        "shots": shots,
        "metrics": metrics,
        "image_consistency": icons,
        "files": files,
        "mtime": max(mtimes) if mtimes else proj.stat().st_mtime,
        "duration": round(max([v["duration"] for v in vids if v["duration"]], default=0), 2),
        "raw_dir": (str(pick_raw_dir(proj)) if pick_raw_dir(proj) else None),
        "pending": (bool(read_task(proj)) and not vids and not ref and not shots),
    }


def scan() -> dict:
    items = []
    if ROOT.is_dir():
        for d in sorted(ROOT.iterdir(), key=lambda x: x.name):
            if not d.is_dir() or not re.match(r"^\d{2}_", d.name):
                continue
            if d.name.lower() in HIDDEN:
                continue
            has = (d / "11_成片").is_dir() or (d / "02_生成源").is_dir()
            if not has:
                continue
            try:
                it = build(d)
            except Exception as e:
                print(f"[warn] {d.name}: {e}")
                continue
            if it["shots"] or it["ref"] or it["task"]:
                items.append(it)
    # 待生成的排最前（刚建的要看得到），其余有定妆照的在前、按时间倒序
    items.sort(key=lambda x: (0 if x.get("pending") else 1,
                              -int(bool(x["ref"])), -x["mtime"]))
    return {
        "app": CFG.get("app_name") or "Live Clip Studio",
        "generated_at": time.time(),
        "root": str(ROOT),
        "projects": items,
        "hidden_count": len(HIDDEN),
        "stats": {
            "projects": len(items),
            "pending": sum(1 for x in items if x.get("pending")),
            "with_ref": sum(1 for x in items if x["ref"]),
            "videos": sum(len(x["videos"]) for x in items),
            "duration": round(sum(x["duration"] for x in items), 2),
        },
    }


# ---------------------------------------------------------------- HTTP
def allowed(path: Path) -> Path | None:
    try:
        rp = path.resolve()
        rp.relative_to(ROOT.resolve())
        return rp
    except (ValueError, OSError):
        return None


def export_copy(src: Path, target_dir: Path | None = None) -> Path:
    """把成片复制到系统「下载」目录（重名自动加序号），返回落地路径。

    WebView2 内核不接管浏览器下载事件，页面里的下载链接点了没有任何反应，
    所以下载动作放到服务端做文件复制，前端只负责提示结果。
    """
    dest_dir = target_dir or (Path(os.path.expanduser("~")) / "Downloads")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    i = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.copy2(src, dest)
    return dest


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveShortsWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # 窗口关闭 / 切项目时 WebView2 会硬断连接，别刷 traceback
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _file(self, path: Path, dl: bool = False):
        try:
            size = path.stat().st_size
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if dl:
            ascii_name = re.sub(r"[^\x20-\x7e]", "_", path.name).replace('"', "_")
            self.send_header(
                "Content-Disposition",
                "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (ascii_name, urllib.parse.quote(path.name)),
            )
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            payload = {}

        if u.path == "/api/prompts/save":
            proj = allowed(Path((qs.get("p") or [""])[0]))
            if not proj or not (proj / "task.json").is_file():
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            try:
                r = save_prompts(proj, payload)
                r["state"] = prompts_state(proj)
                self._json(r)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        self._json({"ok": False, "error": "未知接口"}, 404)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path == "/api/state":
            self._json(scan())
            return

        # 从工作台列表移除 / 恢复。只写 config.json 的 hidden 名单，不删磁盘文件。
        if u.path == "/api/hide":
            if (qs.get("all") or [""])[0] == "1":
                for n in list(HIDDEN):
                    set_hidden(n, False)
                self._json({"ok": True, "hidden_count": len(HIDDEN)})
                return
            raw = (qs.get("p") or [""])[0]
            name = Path(raw).name
            if not name or not (ROOT / name).is_dir():
                self._json({"ok": False, "error": "项目不存在"}, 404)
                return
            set_hidden(name, (qs.get("on") or ["1"])[0] != "0")
            self._json({"ok": True, "hidden_count": len(HIDDEN)})
            return

        if u.path == "/api/talents":
            self._json({"ok": True, "talents": talents()})
            return

        if u.path == "/api/prompts/get":
            proj = allowed(Path((qs.get("p") or [""])[0]))
            if not proj or not (proj / "task.json").is_file():
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            self._json({"ok": True, "prompts": read_prompts(proj),
                        "state": prompts_state(proj), "task": read_task(proj)})
            return

        # 新建任务：只建目录骨架 + task.json，不跑生成
        if u.path == "/api/task/create":
            def q(k, d=""):
                return (qs.get(k) or [d])[0]
            try:
                seconds = int(float(q("seconds", "60") or 60))
            except ValueError:
                seconds = 60
            try:
                shots = int(float(q("shots", "8") or 8))
            except ValueError:
                shots = 8
            seconds = max(4, min(600, seconds))
            shots = max(1, min(40, shots))
            self._json(create_task(q("product"), q("talent"), seconds, shots,
                                   q("resolution", "480P"), q("aspect", "9:16")))
            return

        if u.path == "/api/task/run":
            proj = allowed(Path((qs.get("p") or [""])[0]))
            stage = (qs.get("stage") or ["assets"])[0]
            if not proj or not (proj / "task.json").is_file():
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            if stage not in ("assets", "shots", "final"):
                self._json({"ok": False, "error": "未知阶段"}, 400)
                return
            regen = (qs.get("regen") or ["0"])[0] in ("1", "true", "yes")
            self._json(start_stage(proj, stage, regen))
            return

        if u.path == "/api/task/status":
            proj = allowed(Path((qs.get("p") or [""])[0]))
            if not proj:
                self._json({"ok": False, "error": "路径不在允许范围"}, 404)
                return
            proc = RUN_PROCS.get(str(proj))
            running = bool(proc is not None and proc.poll() is None)
            log = ""
            lp = proj / "02_生成源" / "run.log"
            if lp.is_file():
                log = "\n".join(
                    lp.read_text(encoding="utf-8", errors="ignore").splitlines()[-60:])
            prod_dir = proj / "03_商品素材"
            has_ref = (proj / "04_分镜参考" / "host_scene_ref.png").is_file()
            ps = prompts_state(proj)
            done, need = raw_shots(proj)
            self._json({
                "ok": True, "running": running,
                "progress": read_progress(proj),
                "candidates": candidates(proj),
                "has_ref": has_ref,
                "has_product": bool(prod_dir.is_dir() and any(prod_dir.glob("*.png"))),
                "prompts": ps,
                "shots_done": done, "shots_need": need,
                "can_assets": ps["desc_ok"],
                "can_shots": bool(has_ref and ps["desc_ok"] and ps["voices_ok"]),
                "can_final": done >= need,
                "final": final_video(proj),
                "log": log,
            })
            return

        if u.path == "/api/ref/pick":
            proj = allowed(Path((qs.get("p") or [""])[0]))
            f = allowed(Path((qs.get("f") or [""])[0]))
            if not proj or not f or not f.is_file():
                self._json({"ok": False, "error": "文件不存在"}, 404)
                return
            try:
                self._json(commit_ref(proj, f))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if u.path == "/api/reveal":
            t = allowed(Path((qs.get("p") or [""])[0]))
            if not t or not t.exists():
                self._json({"ok": False, "error": "路径不在允许范围"}, 403)
                return
            try:
                if t.is_dir():
                    subprocess.Popen(["explorer.exe", str(t)])
                else:
                    subprocess.Popen(["explorer.exe", "/select,", str(t)])
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if u.path == "/api/open":
            t = allowed(Path((qs.get("p") or [""])[0]))
            if not t or not t.is_file():
                self._json({"ok": False, "error": "文件不存在"}, 403)
                return
            try:
                os.startfile(str(t))
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if u.path in ("/api/poster", "/api/shot"):
            t = allowed(Path((qs.get("p") or [""])[0]))
            if not t or not t.is_file():
                self._json({"error": "forbidden"}, 403)
                return
            if u.path == "/api/shot":
                at, w = 3.5, 300
            else:
                at = float((qs.get("t") or ["1"])[0])
                w = 560
            img = grab(t, at, w)
            if not img:
                self._json({"error": "no frame"}, 404)
                return
            self._file(img)
            return

        if u.path == "/api/download":
            # WebView2 内核不处理浏览器下载事件，改成服务端把文件复制到系统「下载」目录
            t = allowed(Path((qs.get("p") or [""])[0]))
            if not t or not t.is_file():
                self._json({"ok": False, "error": "文件不存在"}, 403)
                return
            try:
                dest = export_copy(t)
                self._json({"ok": True, "path": str(dest), "size": dest.stat().st_size})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if u.path == "/f":
            t = allowed(Path((qs.get("p") or [""])[0]))
            if not t or not t.is_file():
                self._json({"error": "forbidden"}, 403)
                return
            self._file(t, dl=(qs.get("dl") or [""])[0] in ("1", "true"))
            return

        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        if target.is_file():
            self._file(target)
        else:
            self._json({"error": "not found"}, 404)


def make_server(port: int | None = None, host: str = "127.0.0.1"):
    """绑定端口；被占用时顺延，返回 (httpd, 实际端口)。"""
    ports = ([int(port)] if port else []) + list(range(PORT, PORT + 12))
    last: Exception | None = None
    for p in ports:
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
        except OSError as e:      # 端口占用
            last = e
            continue
        httpd.daemon_threads = True
        return httpd, p
    raise SystemExit(f"无法绑定端口 {port or PORT}: {last}")


def serve(port: int | None = None):
    """后台线程起服务，给 app.py 用；返回 (httpd, 端口)。"""
    httpd, p = make_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd, p


def main():
    httpd, p = serve()
    print(f"Live Clip Studio 工作台 -> http://127.0.0.1:{p}")
    print(f"项目根目录: {ROOT}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
