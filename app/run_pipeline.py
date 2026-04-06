"""
直接从命令行跑完整流程（跳过 GUI）
python run_pipeline.py
"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cutter_logic import process_video

VIDEO = None
import glob
candidates = glob.glob(r"D:\切片\小贤\**\3月18日 (1)(2)-1.mp4", recursive=True)
if candidates:
    VIDEO = candidates[0]
else:
    candidates = glob.glob(r"D:\切片\小贤\**\*.mp4", recursive=True)
    if candidates:
        VIDEO = candidates[0]

if not VIDEO:
    print("ERROR: no video found")
    sys.exit(1)

print(f"Video: {VIDEO}")
print(f"Size: {os.path.getsize(VIDEO)/1024/1024:.1f} MB")
print()

OUTPUT = r"C:\lc_temp\test_output.mp4"
os.makedirs(r"C:\lc_temp", exist_ok=True)

ok = process_video(
    video_path=VIDEO,
    srt_path=None,
    output_path=OUTPUT,
    dedup_preset="medium",
    subtitle_overlay=True,
    log_fn=lambda msg: print(msg),
)

if ok:
    sz = os.path.getsize(OUTPUT) / 1024 / 1024
    print(f"\nSUCCESS! Output: {OUTPUT} ({sz:.1f} MB)")
else:
    print("\nFAILED")
