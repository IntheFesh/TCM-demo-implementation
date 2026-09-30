#!/usr/bin/env bash
# 一键运行脚本：建虚拟环境 -> 装依赖 -> （按需，离线）重切医案数据 -> （按需，需要 key）
# 抽取医案 -> 启动服务。
#
# 「重切数据」与「抽取医案」是先后依赖：split_cases.py 只把古籍切成"粗段"，
# extract_cases.py 的 LLM 步骤才判断病人与诊次，它读前者的输出。用法见 README「快速开始」：
#   ./run.sh                              本地默认端口 8000 启动
#   PORT=8080 ./run.sh                    指定端口
#   ./run.sh --skip-extract               跳过自动抽取医案（cases.json 已存在或想手动控制时用）
#   ./run.sh --resplit-data=<书所在目录>   纯正则重切出粗段，不需要 API key
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SKIP_EXTRACT=0
RESPLIT_BOOKS_DIR=""
for arg in "$@"; do
  case "$arg" in
    --skip-extract) SKIP_EXTRACT=1 ;;
    --resplit-data=*) RESPLIT_BOOKS_DIR="${arg#*=}" ;;
  esac
done

echo "== 1/5 检查虚拟环境 =="
if [ ! -d .venv ]; then
  python3 -m venv .venv
  echo "已创建 .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "== 2/5 安装依赖 =="
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "== 3/5 离线重切医案数据（可选，不需要 API key）=="
if [ -n "$RESPLIT_BOOKS_DIR" ]; then
  echo "--resplit-data=$RESPLIT_BOOKS_DIR，纯正则统计门类命中并重新切分……"
  python -m offline.split_cases --stats-only --books-dir "$RESPLIT_BOOKS_DIR"
  python -m offline.split_cases --books-dir "$RESPLIT_BOOKS_DIR"
  echo "已写出 out/ye_tianshi/、out/wu_jutong/、out/zhang_xichun/（粗段 .json，还不是最终病人级案例；缺书的医家会被跳过）。"
  echo "extract_cases.py 读的是 data/{physician}/*.json。确认切分结果后手动执行："
  echo "  for p in ye_tianshi wu_jutong zhang_xichun; do mkdir -p data/\$p && cp out/\$p/*.json data/\$p/; done"
else
  echo "未传 --resplit-data，跳过（data/ye_tianshi、data/wu_jutong、data/zhang_xichun 里"
  echo "  已有随仓库提交的 .json 粗段）"
fi

echo "== 4/5 检查配置与医案抽取（需要 API key）=="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "未找到 .env，已从 .env.example 生成。"
  echo "请编辑 .env 填入 LLM_API_KEY（DeepSeek API key）后重新运行 ./run.sh。"
  exit 1
fi

# 把 .env 载入当前 shell 环境（简单 KEY=VALUE 格式，跳过注释与空行）
set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${LLM_API_KEY:-}" ] && [ "${LLM_MODE:-api}" = "api" ]; then
  echo "警告：.env 中 LLM_API_KEY 为空，/api/consult 会报错。"
  echo "如只想先看前端页面可以忽略；要跑通辨证功能请先在 .env 中填好 LLM_API_KEY。"
fi

N_SEGMENTS=$(find data -mindepth 2 -maxdepth 2 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')

if [ ! -f cases.json ]; then
  if [ "$N_SEGMENTS" = "0" ]; then
    # extract_cases.py 找不到 data/{physician}/*.json 时会正常退出并写出空的
    # cases.json，下面的 "if ... ; then" 会把它当成抽取完成。所以先在这里拦住。
    echo "cases.json 不存在，且 data/{physician}/ 下没有粗段 .json（extract_cases.py"
    echo "读的就是这个）——直接跑抽取只会得到一个空 cases.json，不会报错也不会提醒你。"
    echo "先按 README「快速开始」跑 split_cases.py 并把粗段放进 data/{physician}/，"
    echo "再重新执行 ./run.sh。"
  elif [ "$SKIP_EXTRACT" = "1" ]; then
    echo "cases.json 不存在，且传入了 --skip-extract，跳过自动抽取。"
    echo "检索/辨证功能在 cases.json 生成前无法使用，请手动运行："
    echo "  python -m offline.extract_cases"
  elif [ -z "${LLM_API_KEY:-}" ] && [ "${LLM_MODE:-api}" = "api" ]; then
    echo "cases.json 不存在，但 LLM_API_KEY 未配置，无法自动抽取。"
    echo "请先配置 .env，再手动运行：python -m offline.extract_cases"
  else
    echo "cases.json 不存在，开始离线抽取 data/ 下的 $N_SEGMENTS 个粗段（需调用 LLM，可能需要几分钟）……"
    if python -m offline.extract_cases; then
      echo "医案抽取完成。extract_warnings.json 里如果有内容，建议人工过一遍。"
    else
      echo "医案抽取失败，请检查 .env 配置或网络后手动运行：python -m offline.extract_cases"
    fi
  fi
else
  echo "cases.json 已存在，跳过抽取。"
fi

echo "== 5/5 启动服务 =="
PORT="${PORT:-8000}"
echo "打开 http://localhost:${PORT} 使用（首次请求会下载 embedding 模型，稍慢）"
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT}"
