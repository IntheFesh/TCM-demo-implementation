"""R17：README 的「公开部署」一节必须齐全（docs/DESIGN.md §5.1）。

## 为什么文档也要测

**默认配置不适合直接对外**：`LLM_MODE=api` + 你自己的 key = 任何人都能用你的钱
跑推理。六个环境变量里少写一个，部署的人就少设一个闸——而少设闸不会报错，
只会在月底的账单上出现。

尤其是 `TRUSTED_PROXY_HOPS`：它默认 0（不读 XFF），文档不写清楚"什么时候才该
设成 1"，两种错都会发生——不设，所有人被算成同一个 IP，限额形同虚设；
乱设，任何人加一个头就换一个"IP"，限额直接被送人。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_all_six_deployment_env_vars_are_documented():
    """六个都要出现。`LLM_MAX_INFLIGHT` 本来就在环境变量总表里，
    这里要求它在**部署这一节**也出现——部署的人不会把整份 README 读完。"""
    section = README[README.index("## 公开部署"):README.index("## 前端页面")]
    for name in ("QUOTA_PER_IP_DAILY_CALLS", "QUOTA_GLOBAL_DAILY_CALLS",
                 "QUOTA_MAX_TRACKED_IPS", "TRUSTED_PROXY_HOPS",
                 "FORCE_REPLAY", "LLM_MAX_INFLIGHT"):
        assert f"`{name}`" in section, f"部署一节没写 {name}"


def test_the_nginx_example_pairs_the_header_with_the_hop_count():
    """`proxy_set_header X-Forwarded-For` 和 `TRUSTED_PROXY_HOPS=1` **必须成对
    出现**。只给 nginx 那一行、不说服务端要设 hops，读者会以为配好了——
    而服务端默认 0，根本不读那个头。"""
    section = README[README.index("## 公开部署"):README.index("## 前端页面")]
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for" in section
    assert "TRUSTED_PROXY_HOPS=1" in section
    # SSE 要的三条：nginx 默认开 proxy_buffering，开着的话进度事件会被攒起来
    # 一次性吐出，"分步进度"就没了——而页面看起来只是"慢"。
    assert "proxy_buffering off" in section


def test_the_byok_boundary_is_quoted_not_paraphrased():
    """§5.1 给的是**原话**。改写它会丢掉两件事里的一件：
    "只在本次请求中转发"（不代理别的）和"关闭标签页后即清除"（不持久化）。"""
    section = README[README.index("## 公开发布" if "## 公开发布" in README else "## 公开部署"):]
    assert "你的 key 只在本次请求中转发给 DeepSeek，不会存储在服务器上。" in section
    assert "关闭标签页后即清除。" in section
    assert "不落盘、不进日志、不进 manifest" in section


def test_the_three_layers_and_four_hard_requirements_are_all_there():
    """三层（BYOK / 共享额度 / 用量看板）+ 四条硬要求。少一条就是少一道闸。"""
    section = README[README.index("## 公开部署"):README.index("## 前端页面")]
    for layer in ("BYOK", "共享额度", "用量看板"):
        assert layer in section
    assert "请求前拦截" in section and "不产生费用" in section
    assert "llm_calls" in section and "不是按请求数" in section
    assert "降级到回放而不是报错" in section
    assert "不做通用代理" in section


def test_the_quota_defaults_in_the_doc_match_the_code():
    """文档里的默认值是**从代码里核过的**，不是拍的。`QUOTA_PER_IP_DAILY_CALLS`
    的默认是 `CALLS_PER_CONSULT * 5`——写成"5 次/天"是对的（那是问诊数），
    但环境变量的单位是**调用数**，两者差 CALLS_PER_CONSULT 倍。这一条就是
    防那个换算被写反。"""
    import api.main as api_main
    from core.usage import CALLS_PER_CONSULT
    section = README[README.index("## 公开部署"):README.index("## 前端页面")]
    assert f"`{CALLS_PER_CONSULT * 5}`" in section, "每 IP 默认调用数写错了"
    assert f"`{CALLS_PER_CONSULT * 200}`" in section, "全局默认调用数写错了"
    assert f"`{api_main.MAX_TRACKED_IPS}`" in section


def test_degradation_is_described_as_not_an_error():
    """§5.1 第 3 条 + §5.2 的样式要求：降级用 `--surface-2` 底、**不是警告色**。
    文档和实现要说同一件事——文档说它是错误、界面画成不是错误（或反过来），
    读者不知道信哪个。"""
    section = README[README.index("## 公开部署"):README.index("## 前端页面")]
    assert "不是警告色" in section
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    block = css[css.index("#degrade-banner.show {"):]
    block = block[:block.index("}")]
    assert "var(--surface-2)" in block
    assert "var(--danger)" not in block
