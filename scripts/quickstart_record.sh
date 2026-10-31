#!/usr/bin/env bash
# Run the Record example after the README installation and configuration steps.
set -euo pipefail

usage() {
    cat <<'EOF'
用法：bash scripts/quickstart_record.sh [选项]

执行 Astropy 任务准备、preload、record、查看、恢复校验、导出和可视化。
录制会调用真实模型；沿用本地模型配置或 OPENAI_MODEL / OPENAI_API_KEY 等环境变量。
依赖安装、Btrfs 工具镜像构建和模型配置见 README 的第 1 步。

选项：
  --llm-config FILE  指定已有模型配置 TOML；省略时使用项目默认加载规则
  --output DIR       产物根目录；默认启动目录下的 outputs/
  --max-steps N      最大运行步数，默认 2
  -h, --help         显示帮助

相对参数路径以启动脚本时的工作目录为准。产物按类型和时间戳分类，不覆盖同名结果。
EOF
}

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

output_dir=""
llm_config=""
max_steps=2
while [[ $# -gt 0 ]]; do
    case "$1" in
        --llm-config)
            [[ $# -ge 2 && -n "$2" ]] || fail "--llm-config 需要文件路径"
            llm_config=$2
            shift 2
            ;;
        --output)
            [[ $# -ge 2 && -n "$2" ]] || fail "--output 需要新目录路径"
            output_dir=$2
            shift 2
            ;;
        --max-steps)
            [[ $# -ge 2 ]] || fail "--max-steps 需要正整数"
            max_steps=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数：$1；使用 --help 查看用法"
            ;;
    esac
done

[[ "$max_steps" =~ ^[1-9][0-9]*$ ]] || fail "--max-steps 必须为正整数"
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -z "$output_dir" ]]; then
    output_dir="$PWD/outputs"
elif [[ "$output_dir" != /* ]]; then
    output_dir="$PWD/$output_dir"
fi
if [[ -n "$llm_config" && "$llm_config" != /* ]]; then
    llm_config="$PWD/$llm_config"
fi
stamp=$(date +%y%m%d_%H%M%S)
task_id=astropy__astropy-12907
batch_name="${stamp}_${task_id}"
raw_dir="$output_dir/download/${stamp}_swebench_lite"
tasks_dir="$output_dir/convert/${stamp}_swebench_lite"
preload_dir="$output_dir/preload/$batch_name"
run_dir="$output_dir/record/$batch_name"
restore_report="$output_dir/verify-restore/${batch_name}_C0.json"
export_dir="$output_dir/export/$batch_name"
visual_page="$output_dir/visual/${batch_name}.html"
for product in "$raw_dir" "$tasks_dir" "$preload_dir" "$run_dir" "$restore_report" "$export_dir" "$visual_page"; do
    [[ ! -e "$product" && ! -L "$product" ]] || fail "产物已存在，请稍后重试或选择其他 --output：$product"
done

cd -- "$project_root"
phase="准备产物根目录"
trap 'status=$?; printf "\nRecord 流程停止：%s（退出码 %s）\n" "$phase" "$status" >&2; exit "$status"' ERR
palin="$project_root/.venv/bin/palin"

mkdir -p -- "$output_dir"
printf '产物根目录：%s\n本轮前缀：%s\n' "$output_dir" "$stamp"

phase="下载和转换任务"
printf '\n[1/5] 下载数据并选择 Astropy 任务\n'
"$palin" download dataset --source swebench_lite --output "$raw_dir"
"$palin" convert --input "$raw_dir" \
    --instance-id "$task_id" --output "$tasks_dir"

phase="准备任务镜像"
printf '\n[2/5] 下载并核验任务镜像\n'
"$palin" download mirror "$tasks_dir"

phase="预载 C0"
printf '\n[3/5] 初始化并冻结 C0\n'
"$palin" preload "$tasks_dir" --output "$preload_dir"

phase="录制任务"
printf '\n[4/5] 录制任务，最大步数：%s\n' "$max_steps"
record_args=(
    record run "$tasks_dir"
    --preload "$preload_dir"
    --output "$run_dir"
    --max-steps "$max_steps"
)
if [[ -n "$llm_config" ]]; then
    record_args+=(--llm-config "$llm_config")
fi
"$palin" "${record_args[@]}"

phase="查看、恢复校验、导出和可视化"
printf '\n[5/5] 检查录制、校验 C0、导出并生成可视化\n'
"$palin" record inspect "$run_dir" --check
"$palin" record inspect "$run_dir" --node C0
mkdir -p -- "$(dirname -- "$restore_report")"
"$palin" record verify-restore "$run_dir" \
    --node C0 --report "$restore_report"
"$palin" record export "$run_dir" --output "$export_dir"
"$palin" record visualize "$run_dir" --output "$visual_page"

printf '\nRecord 完成。\n录制：%s\n恢复报告：%s\n导出：%s\n可视化：%s\n' \
    "$run_dir" "$restore_report" "$export_dir" "$visual_page"
