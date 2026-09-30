"""docs/DEPLOYMENT.md 的「公开部署」一节必须齐全（设计见 docs/DESIGN.md §5.1）。

默认配置不适合直接对公网开放：`LLM_MODE=api` 加上部署者自己的 key，任何访问者都在消耗
部署者的额度。环境变量少写一个，部署的人就少设一道闸，而少设闸不会报错。尤其是
`TRUSTED_PROXY_HOPS`：它默认 0（不读 XFF），文档不写清楚"什么时候才该设成 1"，两种错都会
发生——不设，所有人被算成同一个 IP；乱设，任何人加一个头就换一个"IP"。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")


def _section() -> str:
    return DOC[DOC.index("## 5. 公开部署"):DOC.index("## 6. ")]


def test_all_six_deployment_env_vars_are_documented():
    """六个都要出现在这一节里：部署的人不一定读完整份文档。"""
    section = _section()
    for name in ("QUOTA_PER_IP_DAILY_CALLS", "QUOTA_GLOBAL_DAILY_CALLS",
                 "QUOTA_MAX_TRACKED_IPS", "TRUSTED_PROXY_HOPS",
                 "FORCE_REPLAY", "LLM_MAX_INFLIGHT"):
        assert f"`{name}`" in section, f"部署一节没写 {name}"


def test_the_nginx_example_pairs_the_header_with_the_hop_count():
    """`proxy_set_header X-Forwarded-For` 和 `TRUSTED_PROXY_HOPS=1` **必须成对
    出现**。只给 nginx 那一行、不说服务端要设 hops，读者会以为配好了——
    而服务端默认 0，根本不读那个头。"""
    section = _section()
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for" in section
    assert "TRUSTED_PROXY_HOPS=1" in section
    # SSE 要的三条：nginx 默认开 proxy_buffering，开着的话进度事件会被攒起来
    # 一次性吐出，"分步进度"就没了——而页面看起来只是"慢"。
    assert "proxy_buffering off" in section


def test_the_byok_boundary_is_quoted_not_paraphrased():
    """页面上的说明原样引用。改写它会丢掉两件事里的一件：
    "只在本次请求中转发"（不代理别的）和"关闭标签页后即清除"（不持久化）。"""
    section = _section()
    assert "你的 key 只在本次请求中转发给 DeepSeek，不会存储在服务器上。" in section
    assert "关闭标签页后即清除。" in section
    assert "不落盘、不进日志、不进 manifest" in section


def test_the_three_layers_and_four_hard_requirements_are_all_there():
    """三层（BYOK / 共享额度 / 用量看板）+ 四条硬要求。少一条就是少一道闸。"""
    section = _section()
    for layer in ("BYOK", "共享额度", "用量看板"):
        assert layer in section
    assert "请求前拦截" in section and "不产生费用" in section
    assert "llm_calls" in section and "不是按请求数" in section
    assert "降级到回放而不是报错" in section
    assert "不做通用代理" in section


def test_the_quota_defaults_in_the_doc_match_the_code(monkeypatch):
    """文档里的默认值与代码一致。`QUOTA_PER_IP_DAILY_CALLS` 的默认是
    `calls_per_consult() * 5`：环境变量的单位是**调用数**，不是问诊数。"""
    import api.main as api_main
    from core.usage import calls_per_consult

    # 按产品默认配置算：conftest 把 `S3_MODE` 钉成了 legacy，文档写的是产品默认值。
    for var in ("S3_MODE", "S3_BEST_OF_N", "S1S2_MERGED"):
        monkeypatch.delenv(var, raising=False)
    section = _section()
    assert f"`{calls_per_consult() * 5}`" in section, "每 IP 默认调用数写错了"
    assert f"`{calls_per_consult() * 200}`" in section, "全局默认调用数写错了"
    assert f"`{api_main.MAX_TRACKED_IPS}`" in section


def test_degradation_is_described_as_not_an_error():
    """降级用 `--surface-2` 底、**不是警告色**。文档和实现说同一件事。"""
    section = _section()
    assert "不是警告色" in section
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    block = css[css.index("#degrade-banner.show {"):]
    block = block[:block.index("}")]
    assert "var(--surface-2)" in block
    assert "var(--danger)" not in block
