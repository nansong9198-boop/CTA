#!/usr/bin/env python3
"""本地视频/音频 → 中文文字稿管线。

用法:
  python3 scripts/asr_transcribe.py <本地视频或音频文件>
  python3 scripts/asr_transcribe.py --url <mp4/m3u8/排排网路演页地址>
  python3 scripts/asr_transcribe.py --url https://ly.simuwang.com/roadshow/12904.html

输出: 默认在输入文件旁生成 <文件名>.transcript.txt；排排网路演页自动存到
data/roadshow/<路演ID>.transcript.txt。每段带 [HH:MM:SS] 时间戳。

Cookie 仅从 data/simuwang_cookie.txt 读取，不打印、不外传。
"""
import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COOKIE_FILE = ROOT / "data" / "simuwang_cookie.txt"
VOCAB_FILE = ROOT / "data" / "asr_vocab.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
API_HOST = "https://sppwapi.simuwang.com"


def load_cookie() -> str:
    if not COOKIE_FILE.exists():
        return ""
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def http_get(url: str, referer: str = "https://ly.simuwang.com/") -> bytes:
    headers = {"User-Agent": UA, "Referer": referer}
    cookie = load_cookie()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=30).read()


def resolve_simuwang_roadshow(page_url: str) -> tuple[str, str]:
    """排排网路演页 → (polyv 回放 mp4 直链, 路演ID)。VIP 视频需有效 Cookie。"""
    m = re.search(r"/roadshow/(\d+)\.html", page_url)
    if not m:
        raise ValueError(f"无法从 URL 解析路演 ID: {page_url}")
    rid = m.group(1)
    cfg = json.loads(http_get(f"{API_HOST}/sun/roadshow/getPlayerConfig/?id={rid}",
                              referer=page_url))
    data = cfg.get("data") or {}
    inner = data.get("config") or {}
    vid = inner.get("vid")
    if not vid:
        raise RuntimeError(f"未拿到回放 vid（接口返回 status={cfg.get('status')} "
                           f"msg={cfg.get('msg')}），可能是 VIP 鉴权失败")
    # polyv secure videojson: AES-128-CBC, key/iv = md5(vid) 前后 16 字节，内容再 base64
    from Crypto.Cipher import AES
    body = json.loads(http_get(f"https://player.polyv.net/secure/{vid}.json"))["body"]
    md5 = hashlib.md5(vid.encode()).hexdigest()
    raw = AES.new(md5[:16].encode(), AES.MODE_CBC, md5[16:].encode()).decrypt(bytes.fromhex(body))
    raw = raw[:-raw[-1]]
    info = json.loads(base64.b64decode(raw))
    sources = [u for u in (info.get("mp4") or []) if isinstance(u, str) and u.endswith(".mp4")]
    if not sources:
        raise RuntimeError("回放列表中没有 mp4 直链")
    return sources[0], rid


def download(url: str, referer: str, dst: Path) -> Path:
    """下载 mp4 到本地；m3u8 交给 ffmpeg 拉流。"""
    headers = {"User-Agent": UA, "Referer": referer}
    cookie = load_cookie()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dst


def extract_audio(src: str, wav: Path, headers: str = "") -> None:
    """ffmpeg 抽音频为 16kHz 单声道 wav。src 可为本地文件或 URL。"""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", src, "-ar", "16000", "-ac", "1", "-vn", str(wav)]
    subprocess.run(cmd, check=True)


def merge_segments(segs: list, max_ms: int = 30000, max_gap_ms: int = 800) -> list:
    """把 VAD 小段合并成 ≤max_ms 的大段，间隔超过 max_gap_ms 才断开。"""
    merged = []
    for s, e in segs:
        if merged and s - merged[-1][1] <= max_gap_ms and e - merged[-1][0] <= max_ms:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def fmt_ts(ms: int) -> str:
    t = ms // 1000
    return f"[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}]"


def load_corrections() -> dict:
    if not VOCAB_FILE.exists():
        return {}
    vocab = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))
    return vocab.get("corrections", {})


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️\U0001F1E6-\U0001F1FF]")


def correct_text(text: str, corrections: dict) -> str:
    text = EMOJI_RE.sub("", text)
    for wrong, right in corrections.items():
        text = text.replace(wrong, right)
    return text


def transcribe(wav_path: Path, device: str) -> list:
    """VAD 切段 + SenseVoice 转写，返回 [(start_ms, 原文, 纠错后)]。"""
    import soundfile as sf
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    vad = AutoModel(model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                    device=device, disable_update=True)
    asr = AutoModel(model="iic/SenseVoiceSmall",
                    device=device, disable_update=True)

    print("[1/3] VAD 切分长音频 ...", flush=True)
    vad_res = vad.generate(input=str(wav_path))
    segs = merge_segments(vad_res[0]["value"])
    print(f"      共 {len(segs)} 段", flush=True)

    audio, sr = sf.read(str(wav_path), dtype="float32")
    assert sr == 16000, f"采样率异常: {sr}"

    print("[2/3] SenseVoice 转写 ...", flush=True)
    corrections = load_corrections()
    results = []
    for i, (s, e) in enumerate(segs, 1):
        chunk = audio[s * 16:e * 16]
        res = asr.generate(input=chunk, cache={}, language="zh", use_itn=True,
                           batch_size_s=60, merge_vad=False)
        text = rich_transcription_postprocess(res[0]["text"]).strip()
        if not text:
            continue
        fixed = correct_text(text, corrections)
        results.append((s, text, fixed))
        print(f"\r      {i}/{len(segs)} {fmt_ts(s)}", end="", flush=True)
    print()
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="视频/音频转中文文字稿（FunASR SenseVoice-Small）")
    ap.add_argument("input", nargs="?", help="本地视频/音频文件路径")
    ap.add_argument("--url", help="mp4/m3u8 直链或排排网路演页地址")
    ap.add_argument("--out", help="输出文字稿路径")
    ap.add_argument("--device", default="cuda:0", help="推理设备，默认 cuda:0，无 GPU 用 cpu")
    ap.add_argument("--keep-media", action="store_true", help="保留下载的视频文件")
    ap.add_argument("--raw", action="store_true", help="同时输出未纠错的原文段")
    args = ap.parse_args()

    if not args.input and not args.url:
        ap.error("需要提供本地文件路径或 --url")

    tmp = tempfile.TemporaryDirectory(prefix="asr_")
    work = Path(tmp.name)
    referer = "https://ly.simuwang.com/"
    out_path = Path(args.out) if args.out else None

    if args.url:
        url = args.url
        if "ly.simuwang.com/roadshow/" in url:
            print("解析排排网路演页视频地址 ...", flush=True)
            url, rid = resolve_simuwang_roadshow(args.url)
            referer = args.url
            if out_path is None:
                out_path = ROOT / "data" / "roadshow" / f"{rid}.transcript.txt"
        if ".m3u8" in url:
            media = url  # ffmpeg 直接拉流
        else:
            media = work / "video.mp4"
            print("下载视频 ...", flush=True)
            download(url, referer, media)
            if args.keep_media:
                keep = Path.cwd() / media.name
                keep.write_bytes(media.read_bytes())
                print(f"视频已保留: {keep}")
    else:
        media = str(Path(args.input).expanduser().resolve())
        if not Path(media).exists():
            sys.exit(f"文件不存在: {media}")

    wav = work / "audio.wav"
    print("抽取音频 (16kHz 单声道) ...", flush=True)
    hdrs = ""
    if isinstance(media, str) and media.startswith("http"):
        cookie = load_cookie()
        hdrs = f"Referer: {referer}\r\nUser-Agent: {UA}\r\n"
        if cookie:
            hdrs += f"Cookie: {cookie}\r\n"
    extract_audio(str(media), wav, headers=hdrs)

    results = transcribe(wav, args.device)

    print("[3/3] 纠错并写出文字稿 ...", flush=True)
    lines = [f"{fmt_ts(s)} {fixed}" for s, _, fixed in results]
    if out_path is None:
        src = Path(args.input) if args.input else Path("audio")
        out_path = src.with_suffix("").with_name(src.stem + ".transcript.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.raw:
        raw_path = out_path.with_name(out_path.stem + ".raw.txt")
        raw_path.write_text("\n".join(f"{fmt_ts(s)} {t}" for s, t, _ in results) + "\n",
                            encoding="utf-8")
    n_chars = sum(len(f) for _, _, f in results)
    n_fixed = sum(1 for _, t, f in results if t != f)
    print(f"完成: {out_path}  共 {len(results)} 段 / {n_chars} 字，纠错命中 {n_fixed} 段")


if __name__ == "__main__":
    main()
