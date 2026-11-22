"""Tests for ingestion and validation behaviour."""

from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from renewal_analytics.io import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def copy_data(target: Path) -> Path:
    shutil.copytree(DATA, target / "data")
    return target / "data"


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = copy_data(self.tmp)

    def _rewrite(self, name: str, text: str) -> None:
        (self.data / name).write_text(text, encoding="utf-8")

    def _append(self, name: str, row: list[str]) -> None:
        with (self.data / name).open("a", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(row)

    def _rows(self, name: str) -> list[str]:
        return (self.data / name).read_text(encoding="utf-8").splitlines()

    def test_baseline_dataset_is_valid(self) -> None:
        dataset = load_dataset(self.data)
        self.assertEqual(dataset.errors, [])
        self.assertEqual(set(dataset.projects), {"P-01", "P-02", "P-03"})

    def test_empty_cost_is_unknown_not_zero(self) -> None:
        lines = self._rows("projects.csv")
        lines[1] = "P-01,North,pipe,,,2027,true"
        self._rewrite("projects.csv", "\n".join(lines) + "\n")
        dataset = load_dataset(self.data)
        codes = {issue.code for issue in dataset.errors}
        self.assertIn("PROJECT_NO_COST", codes)
        # the record is dropped rather than defaulted to a zero-cost project
        self.assertNotIn("P-01", dataset.projects)

    def test_empty_cap_is_an_error(self) -> None:
        self._rewrite(
            "funding.csv",
            "fund_id,year,category,cap,allowed_work\n"
            "F-01,2027,drainage,,pipe;street\n"
            "F-02,2027,community,650000,service;housing\n",
        )
        dataset = load_dataset(self.data)
        self.assertIn("FIELD_EMPTY", {issue.code for issue in dataset.errors})

    def test_empty_contracted_flag_is_an_error(self) -> None:
        lines = self._rows("projects.csv")
        lines[1] = "P-01,North,pipe,480000,520000,2027,"
        self._rewrite("projects.csv", "\n".join(lines) + "\n")
        dataset = load_dataset(self.data)
        self.assertIn("FIELD_EMPTY", {issue.code for issue in dataset.errors})

    def test_duplicate_project_id_is_fatal(self) -> None:
        self._append("projects.csv", ["P-01", "South", "street", "1", "", "2027", "false"])
        dataset = load_dataset(self.data)
        self.assertIn("PROJECT_DUPLICATE_ID", {issue.code for issue in dataset.errors})

    def test_self_dependency_and_unknown_reference(self) -> None:
        self._append("dependencies.csv", ["P-01", "P-01", "false", "0"])
        self._append("dependencies.csv", ["P-99", "P-02", "false", "0"])
        dataset = load_dataset(self.data)
        codes = {issue.code for issue in dataset.errors}
        self.assertIn("DEP_SELF", codes)
        self.assertIn("DEP_UNKNOWN_PROJECT", codes)

    def test_dependency_cycle_is_fatal(self) -> None:
        self._append("dependencies.csv", ["P-02", "P-01", "false", "0"])
        dataset = load_dataset(self.data)
        self.assertIn("DEP_CYCLE", {issue.code for issue in dataset.errors})

    def test_duplicate_funding_key_is_fatal(self) -> None:
        self._append("funding.csv", ["F-01", "2027", "drainage", "10", "pipe"])
        dataset = load_dataset(self.data)
        self.assertIn("FUND_DUPLICATE", {issue.code for issue in dataset.errors})

    def test_shared_site_zero_cost_warns(self) -> None:
        lines = self._rows("dependencies.csv")
        lines[1] = "P-01,P-02,true,0"
        self._rewrite("dependencies.csv", "\n".join(lines) + "\n")
        dataset = load_dataset(self.data)
        self.assertIn(
            "DEP_SHARED_SITE_ZERO_REPEAT_COST",
            {issue.code for issue in dataset.warnings},
        )
        self.assertEqual(dataset.errors, [])

    def test_duplicate_need_is_fatal(self) -> None:
        self._append("needs.csv", ["North", "older", "step_free", "84", "true"])
        dataset = load_dataset(self.data)
        self.assertIn("NEED_DUPLICATE", {issue.code for issue in dataset.errors})

    def test_district_without_need_record_warns(self) -> None:
        self._append("projects.csv", ["P-09", "West", "service", "1000", "", "2027", "false"])
        dataset = load_dataset(self.data)
        self.assertEqual(dataset.errors, [])
        warnings = {issue.code for issue in dataset.warnings}
        self.assertIn("DISTRICT_NO_NEED_RECORD", warnings)

    def test_project_in_unfunded_year_warns(self) -> None:
        self._append("projects.csv", ["P-09", "North", "service", "1000", "", "2029", "false"])
        dataset = load_dataset(self.data)
        self.assertIn("PROJECT_YEAR_UNFUNDED", {issue.code for issue in dataset.warnings})

    def test_missing_file_is_reported(self) -> None:
        (self.data / "needs.csv").unlink()
        dataset = load_dataset(self.data)
        self.assertIn("INPUT_MISSING", {issue.code for issue in dataset.errors})

    def test_header_drift_is_reported(self) -> None:
        self._rewrite(
            "projects.csv",
            "project_id,district,work_type,estimate,year,contracted\n"
            "P-01,North,pipe,480000,2027,true\n",
        )
        dataset = load_dataset(self.data)
        self.assertIn("INPUT_HEADER_MISSING", {issue.code for issue in dataset.errors})

    def test_input_fingerprints_are_recorded(self) -> None:
        dataset = load_dataset(self.data)
        files = {item["file"] for item in dataset.snapshot["inputs"]}
        self.assertEqual(
            files,
            {"projects.csv", "funding.csv", "dependencies.csv", "needs.csv"},
        )
        self.assertTrue(all(len(item["sha256"]) == 64 for item in dataset.snapshot["inputs"]))


if __name__ == "__main__":
    unittest.main()
