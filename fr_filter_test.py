import subprocess, sqlite3
from pathlib import Path
from subber.library_pipeline import _filter_to_dialogue

c = sqlite3.connect("/app/data/library.db")
p = c.execute("SELECT file_path FROM library_files WHERE file_path LIKE '%Seirei Gensouki%04%'").fetchone()[0]

tmp = Path("/tmp/fr_test.ass")
subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-map", "0:3", "-f", "ass", str(tmp)], check=True)

orig = tmp.read_text(encoding="utf-8", errors="replace")
orig_lines = [l for l in orig.splitlines() if l.startswith("Dialogue:")]

filtered = _filter_to_dialogue(tmp)

if filtered is None:
    print(f"ORIGINAL Dialogue lines: {len(orig_lines)}")
    print("FILTER RESULT: None (no dialogue)")
elif filtered == tmp:
    print(f"ORIGINAL Dialogue lines: {len(orig_lines)}")
    print("FILTER RESULT: returned original (not ASS)")
else:
    ftext = filtered.read_text(encoding="utf-8", errors="replace")
    f_lines = [l for l in ftext.splitlines() if l.startswith("Dialogue:")]
    print(f"ORIGINAL Dialogue lines: {len(orig_lines)}")
    print(f"FILTERED Dialogue lines: {len(f_lines)}")

    # style breakdown of what was dropped
    from collections import Counter
    def style_of(line):
        parts = line.split(",", 9)
        return parts[3].strip() if len(parts) >= 4 else "?"
    orig_styles = Counter(style_of(l) for l in orig_lines)
    kept_styles = Counter(style_of(l) for l in f_lines)
    dropped = orig_styles - kept_styles
    print("Styles (orig counts):", dict(orig_styles))
    print("Styles dropped:", dict(dropped))
