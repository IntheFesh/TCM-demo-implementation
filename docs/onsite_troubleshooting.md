# 上机失败预案

配套 `scripts/run_onsite.sh`。哪一段挂了，先在这里找现象，**不要先去猜**。

## 0. 先看这条：某段静默很久，绝大多数时候是正常的

**这是这个项目上机时最贵的一次误判**：因为看不到进度以为卡死，杀了三个正常运行的
进程，浪费一小时和几百次调用。

- `eval/run_eval.py` 跑基线阶段时**没有逐条进度输出**，几分钟内一行都不打；
- `offline/estimate_epsilon.py` 实测一次完整跑 **1347s**（22 分钟，`eval/epsilon.json`
  的 `elapsed_s`），中途同样安静；
- vLLM 加载权重要几分钟，`start_vllm.sh` 起来之后终端不会有任何动静。

**判据是「文件还在不在长」，不是「终端有没有输出」**：

```bash
# 日志文件的修改时间还在往前走 = 进程活着，在干活
watch -n 10 'ls -l --time-style=full-iso <日志文件>'
```

**不要用 `ps` / `kill` 去"确认"它还活着。** 真判死了再杀也来得及，杀错了要重跑。

## 1. 现象 → 最可能的原因 → 处置

| 现象 | 最可能的原因 | 处置 |
|---|---|---|
| **某段静默很久** | **正常**，见上面第 0 条 | 看日志文件 mtime，**不要 ps/kill** |
| `pytest` 数不是预期的 2005 | 依赖没装齐 | `pip install -r requirements.txt`；训练机还要 `-r requirements-train.txt` |
| `RETRIEVER_MODE` 有残留值 | 上次跑 E8 时 export 过 | `unset RETRIEVER_MODE`——**它会让演示/评测跑的不是默认模式，而且没有任何提示** |
| `LLMTruncatedError` | 真截断 or API 抖动 | **看返回长度**：< `TRUNCATION_MIN_LENGTH`（100 字符）的判为抖动、会自动重试；超过它才是真截断，要看 `max_tokens` |
| 单条样本失败 | API 抖动 | 已有失败容忍，看报告里的 `n_failed`；**超过 20%**（`core/batch.py` 的 `FAILURE_RATE_WARNING_THRESHOLD`）脚本会自己吼一声，那时才需要管 |
| vLLM 不认 `guided_json` | vLLM 版本间键名不同 | `export VLLM_GUIDED_JSON_KEY=<真实键名>`，**不用改代码**；`scripts/verify_local_backend.py` 会把实际生效的键打出来 |
| ZhongJing 基座 404 | HF 仓库 id 没在真机核对过（`scripts/train_lora.py` 里标着 `verified: False`） | `--base-model <真实 id>`，或者 `--base qwen2.5-1.5b` 只训那个 |
| 显存 OOM | `gpu-memory-utilization` 给太高 | `scripts/start_vllm.sh` 现在是 **0.85**，降到 0.8；还不行减 `--max-num-seqs`（现在 16），再不行减 `--max-model-len`（现在 16384，**不是 8192**，理由写在那个脚本的注释里）|
| `replay` 下抛 `LLMError: 未命中` | 主诉/检索模式跟录制时不一致 | 报错里带 prompt 前 100 字和 sha12——对一下是不是打错字或切了检索模式（见 `DEMO.md`「replay 的三条限制」）。**它不会静默退回真实 API**，这是设计 |
| `graph` 检索模式返回 503 | `data/element_index.json` 没生成 | `python -m offline.build_element_index`；503 不是 500——前端能区分「数据没准备好」和「系统故障」 |
| `dense`/`hybrid` 报 `retrieval_error` | 连不上 huggingface hub，embedding 模型下不下来 | 换能连网的机器；**它不会静默降级成别的模式**假装成功 |
| 追问的贝叶斯后验像是没生效 | `offline.graph_stats` 漏跑了 | 补跑它——**那一步名字像只读统计，实际会写 `weight_by_physician` 回图，漏跑不报错** |
| `collect_results --check` 报某个数对不上 | 重跑过评测但没更新凭据记号 | 按报错里的「文件里实际是 X」改 `eval/RESULTS.md` / `README.md` 的记号；`.md` 跟 `.json` 不同步就 `--rerender` |

## 2. 每段挂了之后怎么续

```bash
bash scripts/run_onsite.sh --from <挂掉的段号>    # 从那一段继续
bash scripts/run_onsite.sh --only <段号>          # 只重跑那一段
```

段与段之间**没有依赖崩塌**：段 5 的药理层抽取挂了，段 6 的录制照样该跑。
脚本本身就是这么设计的（一段失败不 `set -e` 掉整个脚本），所以汇总表里
看到某段退出码非 0，不代表后面的段没跑。

**退出码 10 = 人工在卡点中止**，不是程序错误。两处卡点在段 3（role 填充率）
和段 5（抽取质量），它们存在的理由是：闸门没过就往下跑，后面几百次调用全部白花。

## 3. 开跑之前

```bash
bash scripts/run_onsite.sh --dry-run    # 先看每段的预估调用数和成本，决定跑到哪一段
```

零调用的段 0/1 先跑：**免费的问题先发现掉**，不要花了钱才发现环境不对。
