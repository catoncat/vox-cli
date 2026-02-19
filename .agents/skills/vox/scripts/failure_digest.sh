#!/usr/bin/env bash
set -euo pipefail

INPUT_FILE="${VOX_HOME:-$HOME/.vox}/agent/failures.jsonl"
OUTPUT_MD="${VOX_HOME:-$HOME/.vox}/agent/state/failure_report.md"
OUTPUT_JSON="${VOX_HOME:-$HOME/.vox}/agent/state/failure_report.json"
TOP_N=20

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/failure_digest.sh [options]

Options:
  --input <jsonl>       Input failures file (default: ~/.vox/agent/failures.jsonl)
  --output-md <path>    Markdown report path
  --output-json <path>  JSON report path
  --top <int>           Top grouped failures (default: 20)
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)
      [[ $# -ge 2 ]] || usage
      INPUT_FILE="$2"
      shift 2
      ;;
    --output-md)
      [[ $# -ge 2 ]] || usage
      OUTPUT_MD="$2"
      shift 2
      ;;
    --output-json)
      [[ $# -ge 2 ]] || usage
      OUTPUT_JSON="$2"
      shift 2
      ;;
    --top)
      [[ $# -ge 2 ]] || usage
      TOP_N="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

if ! [[ "$TOP_N" =~ ^[0-9]+$ ]] || [[ "$TOP_N" -le 0 ]]; then
  echo "[vox-failure-digest] --top must be a positive integer" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUTPUT_MD")" "$(dirname "$OUTPUT_JSON")"

python3 - "$INPUT_FILE" "$OUTPUT_MD" "$OUTPUT_JSON" "$TOP_N" <<'PY'
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

input_file = Path(sys.argv[1])
output_md = Path(sys.argv[2])
output_json = Path(sys.argv[3])
top_n = int(sys.argv[4])

def normalize_error(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"/[^\s]+", "<path>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text)
    text = re.sub(r"\b[0-9]+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text)
    return text

def suggest(pattern: str) -> str:
    if "audio file not found" in pattern:
        return "在执行前增加输入音频存在性检查。"
    if "profile not found" in pattern:
        return "在克隆前增加 profile 存在性检查或自动创建提示。"
    if "doctor" in pattern and "failed" in pattern:
        return "把 doctor 失败项拆分成更具体的修复建议。"
    if "model" in pattern and ("verify" in pattern or "not verified" in pattern):
        return "在重操作前强制执行 ensure_model。"
    return "将该错误模式补入对应 playbook 的前置检查。"

entries: list[dict] = []
if input_file.exists():
    for line in input_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

groups: Counter[tuple[str, str]] = Counter()
examples: dict[tuple[str, str], dict] = {}
stage_counts: Counter[str] = Counter()

for row in entries:
    stage = str(row.get("stage") or "unknown")
    err = str(row.get("error") or "")
    norm = normalize_error(err)
    key = (stage, norm)
    groups[key] += 1
    stage_counts[stage] += 1
    examples.setdefault(key, row)

top = groups.most_common(top_n)

report_rows = []
for (stage, norm), count in top:
    sample = examples[(stage, norm)]
    report_rows.append(
        {
            "stage": stage,
            "count": count,
            "pattern": norm,
            "sample_error": sample.get("error"),
            "sample_command": sample.get("command"),
            "suggestion": suggest(norm),
        }
    )

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "input_file": str(input_file),
    "total_entries": len(entries),
    "stage_counts": dict(stage_counts),
    "top_failures": report_rows,
}
output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

lines = [
    "# Vox Failure Digest",
    "",
    f"- Generated: {payload['generated_at']}",
    f"- Source: `{input_file}`",
    f"- Total entries: {len(entries)}",
    "",
    "## Top Failure Patterns",
    "",
]

if not report_rows:
    lines.append("No failure entries found.")
else:
    for idx, row in enumerate(report_rows, start=1):
        lines.extend(
            [
                f"### {idx}. [{row['stage']}] ×{row['count']}",
                f"- Pattern: `{row['pattern']}`",
                f"- Sample command: `{row['sample_command']}`",
                f"- Sample error: `{row['sample_error']}`",
                f"- Suggestion: {row['suggestion']}",
                "",
            ]
        )

output_md.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
print(f"[vox-failure-digest] wrote {output_md}")
print(f"[vox-failure-digest] wrote {output_json}")
PY
