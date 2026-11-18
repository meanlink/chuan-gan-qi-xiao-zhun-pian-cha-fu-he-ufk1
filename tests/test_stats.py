"""统计原语测试：t 分布分位数对照已知统计表，置信区间对照手算值。"""
import math
import unittest

from calibseq import stats


class TestRegularizedBeta(unittest.TestCase):
    def test_beta_1_1_is_identity(self):
        for x in (0.0, 0.25, 0.5, 0.9, 1.0):
            self.assertAlmostEqual(stats.regularized_beta(x, 1.0, 1.0), x, places=12)

    def test_beta_symmetric_point(self):
        self.assertAlmostEqual(stats.regularized_beta(0.5, 2.0, 2.0), 0.5, places=12)

    def test_invalid_x(self):
        with self.assertRaises(ValueError):
            stats.regularized_beta(1.5, 1.0, 1.0)


class TestTPpf(unittest.TestCase):
    """对照标准 t 分布表（四位小数）。"""

    CASES = [
        (0.975, 1, 12.7062),
        (0.975, 4, 2.7764),
        (0.975, 9, 2.2622),
        (0.995, 9, 3.2498),
        (0.9995, 4, 8.6103),
        (0.975, 29, 2.0452),
        (0.975, 1000, 1.9623),
    ]

    def test_known_quantiles(self):
        for p, df, expected in self.CASES:
            with self.subTest(p=p, df=df):
                self.assertAlmostEqual(stats.t_ppf(p, df), expected, delta=5e-4)

    def test_cdf_ppf_roundtrip(self):
        for df in (1, 2, 5, 30, 200):
            for p in (0.6, 0.9, 0.975, 0.999):
                t = stats.t_ppf(p, df)
                self.assertAlmostEqual(stats.t_cdf(t, df), p, places=10)

    def test_cdf_symmetry(self):
        self.assertAlmostEqual(stats.t_cdf(0.0, 7), 0.5, places=14)
        self.assertAlmostEqual(stats.t_cdf(1.5, 7) + stats.t_cdf(-1.5, 7), 1.0, places=12)

    def test_invalid_args(self):
        with self.assertRaises(ValueError):
            stats.t_ppf(0.0, 5)
        with self.assertRaises(ValueError):
            stats.t_ppf(0.975, 0)


class TestMeanCI(unittest.TestCase):
    def test_known_interval(self):
        # [1..5]: mean=3, sd=sqrt(2.5), t(0.975,4)=2.7764 -> half=2.7764*sd/sqrt(5)
        ci = stats.mean_confidence_interval([1, 2, 3, 4, 5], 0.05)
        self.assertEqual(ci["n"], 5)
        self.assertAlmostEqual(ci["mean"], 3.0, places=12)
        self.assertAlmostEqual(ci["sd"], math.sqrt(2.5), places=12)
        half = 2.776445105 * math.sqrt(2.5) / math.sqrt(5)
        self.assertAlmostEqual(ci["lo"], 3.0 - half, places=6)
        self.assertAlmostEqual(ci["hi"], 3.0 + half, places=6)

    def test_constant_values_degenerate(self):
        ci = stats.mean_confidence_interval([0.2, 0.2, 0.2], 0.05)
        self.assertEqual(ci["sd"], 0.0)
        self.assertEqual(ci["lo"], ci["hi"])

    def test_too_few_samples(self):
        with self.assertRaises(ValueError):
            stats.mean_confidence_interval([1.0], 0.05)


if __name__ == "__main__":
    unittest.main()
