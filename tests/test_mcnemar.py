"""eval/mcnemar.py 的离线测试。数值用 scipy（开发环境验证工具，不是项目依赖）
逐个核对过，见 eval/mcnemar.py 模块文档字符串。这里钉住那些核对过的具体数值，
不在测试里 import scipy——项目本身不依赖它，测试也不该依赖。
"""
import pytest

from eval.mcnemar import mcnemar_test, paired_outcomes_to_bc


def test_no_discordant_pairs_returns_p_value_one():
    r = mcnemar_test(0, 0)
    assert r["p_value"] == 1.0
    assert r["method"] == "no_discordant_pairs"


def test_exact_binomial_matches_verified_values():
    # 核对值来自 scipy.stats.binomtest(min(b,c), b+c, 0.5, alternative="two-sided")。
    # 全部取 n=b+c < 25，确保落在精确二项分支——n=25 会走连续性校正卡方分支，
    # 两个分支算法不同，数值本来就对不上，不能拿卡方分支的输入核对二项公式。
    assert mcnemar_test(3, 10)["p_value"] == pytest.approx(0.09228515625, abs=1e-6)
    assert mcnemar_test(0, 5)["p_value"] == pytest.approx(0.0625, abs=1e-6)
    assert mcnemar_test(5, 5)["p_value"] == pytest.approx(1.0, abs=1e-6)
    assert mcnemar_test(10, 14)["p_value"] == pytest.approx(0.5412561893463135, abs=1e-6)


def test_exact_branch_used_below_25_discordant():
    r = mcnemar_test(10, 14)  # n=24 < 25
    assert r["method"] == "exact_binomial"


def test_chi2_branch_used_at_or_above_25_discordant():
    r = mcnemar_test(10, 15)  # n=25
    assert r["method"] == "chi2_continuity_corrected"


def test_chi2_branch_matches_verified_values():
    # statistic = (|b-c|-1)^2 / n；p = 1 - erf(sqrt(stat/2))，核对值来自 scipy.stats.chi2.cdf
    r = mcnemar_test(10, 40)  # n=50, |b-c|=30
    assert r["statistic"] == pytest.approx((29) ** 2 / 50, abs=1e-6)


def test_symmetric_in_b_and_c():
    """McNemar 只关心不一致配对的两个方向差多少，不关心哪个叫 b 哪个叫 c。"""
    r1 = mcnemar_test(3, 20)
    r2 = mcnemar_test(20, 3)
    assert r1["p_value"] == r2["p_value"]


def test_more_discordant_pairs_gives_smaller_p_value():
    """同样的不对称比例，配对数越多，越有把握说这不是巧合。"""
    small = mcnemar_test(2, 8)
    large = mcnemar_test(20, 80)
    assert large["p_value"] < small["p_value"]


def test_negative_counts_raise():
    with pytest.raises(ValueError):
        mcnemar_test(-1, 5)


# ---------- paired_outcomes_to_bc ----------


def test_paired_outcomes_to_bc_basic():
    a = [True, True, False, False, True]
    b = [True, False, False, True, True]
    # idx1: a=T,b=F -> b计数+1；idx3: a=F,b=T -> c计数+1；其余一致
    assert paired_outcomes_to_bc(a, b) == (1, 1)


def test_paired_outcomes_to_bc_length_mismatch_raises():
    with pytest.raises(ValueError, match="长度"):
        paired_outcomes_to_bc([True], [True, False])


def test_paired_outcomes_to_bc_all_agree_gives_zero():
    a = [True, False, True]
    assert paired_outcomes_to_bc(a, list(a)) == (0, 0)
