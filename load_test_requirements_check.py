import csv
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from database_connection_settings import open_database_connection
from load_test_plan import (
    AGGREGATED_TRANSACTION_NAME,
    DATABASE_CONNECTION_FAILURE_NAME,
    FINAL_LOAD_STAGE_KIND,
    LOAD_STAGE_REQUIREMENTS,
    RECOVERY_STAGE_REQUIREMENTS,
    STRESS_STAGE_REQUIREMENTS,
)
from saturation_analysis import write_saturation_report

STAGE_RESULTS_FILE_NAME = "stage_results.csv"
TRANSACTION_RESULTS_FILE_NAME = "stage_results_by_transaction.csv"
REQUIREMENTS_REPORT_FILE_NAME = "requirements_check.md"
DATASET_GROWTH_FILE_NAME = "dataset_growth.csv"
PASSED_MARK = "✔"
FAILED_MARK = "✘"

GROWING_TABLE_NAMES = ("parcels", "tracking_events")


def measure_dataset_size():
    """Read the last row id of the tables that the write transactions grow.

    MAX(id) іде по первинному ключу, тож це константний час навіть на десятках мільйонів рядків —
    на відміну від COUNT(*), який під навантаженням сам би став помітною частиною навантаження.

    Returns:
        Dict of table name to last row id, empty when the database is unreachable.
    """

    try:
        with open_database_connection(autocommit=True) as connection, connection.cursor() as cursor:
            sizes = {}
            for table_name in GROWING_TABLE_NAMES:
                cursor.execute(f"SELECT MAX(id) FROM {table_name}")
                sizes[table_name] = cursor.fetchone()[0] or 0
            return sizes
    except Exception as error:
        print(f"[дані] не вдалося зміряти обсяг БД: {error}")
        return {}


@dataclass(frozen=True)
class TransactionMeasurement:
    """Measured numbers of one transaction type inside one stage."""

    stage: str
    clients: int
    transaction: str
    transactions_count: int
    failures_count: int
    transactions_per_second: float
    average_milliseconds: float
    percentile_50_milliseconds: float
    percentile_95_milliseconds: float
    percentile_99_milliseconds: float
    error_percent: float


def take_statistics_snapshot(request_statistics):
    """Copy cumulative counters and latency histograms of every transaction.

    Args:
        request_statistics: Locust request statistics of the master.

    Returns:
        Dict of transaction name to counters and latency histogram.
    """

    snapshot = {}
    for entry in [*request_statistics.entries.values(), request_statistics.total]:
        snapshot[entry.name] = (entry.num_requests, entry.num_failures, entry.total_response_time,
                                Counter(entry.response_times))
    return snapshot


def calculate_percentile(latency_histogram, transactions_count, percentile):
    """Calculate a latency percentile from the rounded histogram that Locust keeps.

    Args:
        latency_histogram: Counter of rounded response times in milliseconds.
        transactions_count: Number of transactions in the histogram.
        percentile: Percentile between 0 and 1.

    Returns:
        Latency in milliseconds.
    """

    if transactions_count <= 0:
        return 0.0
    target_count = transactions_count * percentile
    counted = 0
    for milliseconds in sorted(latency_histogram):
        counted += latency_histogram[milliseconds]
        if counted >= target_count:
            return float(milliseconds)
    return 0.0


class LoadTestResultsRecorder:
    """Collects the measurements of every stage and writes the result files of the run."""

    def __init__(self, results_directory):
        self.results_directory = Path(results_directory)
        self.results_directory.mkdir(parents=True, exist_ok=True)
        self.current_stage = None
        self.start_snapshot = {}
        self.stage_measurements = []
        # (підпис, обсяги таблиць) у порядку заміру: перший запис — стан до першого етапу.
        self.dataset_sizes = []

    def start_stage(self, stage, request_statistics):
        """Remember the counters at the start of the measured part of a stage.

        Args:
            stage: Stage that reached its target client count.
            request_statistics: Locust request statistics of the master.
        """

        self.current_stage = stage
        self.start_snapshot = take_statistics_snapshot(request_statistics)
        if not self.dataset_sizes:
            self.record_dataset_size("до першого етапу")
        print(f"[план] етап {stage.name}: {stage.concurrent_clients} клієнтів, "
              f"вимірюємо {stage.steady_minutes:g} хв ({stage.description})")

    def finish_stage(self, request_statistics, measured_seconds):
        """Measure the finished stage and append its rows to the result files.

        Args:
            request_statistics: Locust request statistics of the master.
            measured_seconds: Length of the measured steady part of the stage.
        """

        end_snapshot = take_statistics_snapshot(request_statistics)
        measurements = {}
        for transaction_name, counters in end_snapshot.items():
            requests_count, failures_count, total_response_time, latency_histogram = counters
            start_counters = self.start_snapshot.get(transaction_name, (0, 0, 0.0, Counter()))
            transactions_count = requests_count - start_counters[0]
            if transactions_count <= 0 and transaction_name != AGGREGATED_TRANSACTION_NAME:
                continue
            stage_histogram = latency_histogram - start_counters[3]
            stage_failures = failures_count - start_counters[1]
            stage_response_time = total_response_time - start_counters[2]
            measurements[transaction_name] = TransactionMeasurement(
                stage=self.current_stage.name,
                clients=self.current_stage.concurrent_clients,
                transaction=transaction_name,
                transactions_count=transactions_count,
                failures_count=stage_failures,
                transactions_per_second=transactions_count / measured_seconds if measured_seconds else 0.0,
                average_milliseconds=stage_response_time / transactions_count if transactions_count else 0.0,
                percentile_50_milliseconds=calculate_percentile(stage_histogram, transactions_count, 0.50),
                percentile_95_milliseconds=calculate_percentile(stage_histogram, transactions_count, 0.95),
                percentile_99_milliseconds=calculate_percentile(stage_histogram, transactions_count, 0.99),
                error_percent=stage_failures / transactions_count * 100 if transactions_count else 0.0,
            )
        self.stage_measurements.append((self.current_stage, measurements))
        self.record_dataset_size(self.current_stage.name)
        self.write_measurement_rows(measurements)
        aggregated = measurements[AGGREGATED_TRANSACTION_NAME]
        print(f"[план] етап {self.current_stage.name} завершено: {aggregated.transactions_per_second:,.0f} TPS, "
              f"avg {aggregated.average_milliseconds:.0f} мс, p95 {aggregated.percentile_95_milliseconds:.0f} мс, "
              f"помилок {aggregated.error_percent:.2f}%")

    def record_dataset_size(self, label):
        """Measure and remember how large the growing tables are at this moment.

        Args:
            label: What the measurement is tied to — a stage name or the start of the run.
        """

        sizes = measure_dataset_size()
        if sizes:
            self.dataset_sizes.append((label, sizes))

    def dataset_growth_rows(self):
        """Turn the recorded table sizes into per-stage growth rows.

        Returns:
            List of dicts for dataset_growth.csv, empty when nothing could be measured.
        """

        rows = []
        previous_sizes = None
        for label, sizes in self.dataset_sizes:
            row = {"після": label}
            for table_name in GROWING_TABLE_NAMES:
                last_id = sizes.get(table_name, 0)
                row[f"{table_name}_last_id"] = last_id
                row[f"{table_name}_added"] = last_id - previous_sizes.get(table_name, 0) if previous_sizes else 0
            rows.append(row)
            previous_sizes = sizes
        return rows

    def write_dataset_growth_rows(self):
        """Write how much each stage added to the growing tables."""

        rows = self.dataset_growth_rows()
        if not rows:
            return
        path = self.results_directory / DATASET_GROWTH_FILE_NAME
        with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def build_baseline_drift_lines(self, load_stage_tps, final_load_stage_tps, stage_reports):
        """Build the report section that separates dataset growth from loss of stability.

        Args:
            load_stage_tps: Throughput of the first load stage.
            final_load_stage_tps: Throughput of the repeated load stage, 0.0 when absent.
            stage_reports: List of (stage, aggregated measurement, checks) tuples.

        Returns:
            List of Markdown lines, empty when the run has no repeated load stage.
        """

        if load_stage_tps <= 0 or final_load_stage_tps <= 0:
            return []
        drift_share = final_load_stage_tps / load_stage_tps - 1
        lines = [
            "## Дрейф базової точки", "",
            "Те саме навантаження 500 клієнтів зміряне двічі: на початку прогону і після сходів. "
            "Різниця між замірами — це не втрата стійкості, а зростання БД: T5 і T6 дописують рядки "
            "весь прогін, тож кінець прогону працює з більшою таблицею, ніж початок.", "",
            "| Замір базової точки | TPS | Відхилення |", "|---|---|---|",
            f"| на початку прогону | {load_stage_tps:,.0f} | — |",
            f"| повторно, після сходів | {final_load_stage_tps:,.0f} | {drift_share:+.1%} |", "",
        ]
        growth_rows = self.dataset_growth_rows()
        if len(growth_rows) > 1:
            added = {name: growth_rows[-1][f"{name}_last_id"] - growth_rows[0][f"{name}_last_id"]
                     for name in GROWING_TABLE_NAMES}
            lines += ["Приріст за прогін: " + ", ".join(f"`{name}` +{count:,} рядків"
                                                        for name, count in added.items())
                      + f" (подробиці по етапах — у `{DATASET_GROWTH_FILE_NAME}`).", ""]
        stress_reports = [item for item in stage_reports if item[0].kind == "stress"]
        if stress_reports:
            lines += [
                "Частки stress-етапів у вердикті рахуються від ПЕРШОГО заміру — так вимагає чекліст. "
                "Нижче обидва відношення: різниця між колонками і є тією частиною недобору, "
                "яку дає зростання БД, а не поведінка сервера під навантаженням.", "",
                "| Етап | % від першого load | % від повторного load |", "|---|---|---|",
            ]
            for stage, aggregated, _checks in stress_reports:
                lines.append(f"| {stage.name} | {aggregated.transactions_per_second / load_stage_tps:.1%} | "
                             f"{aggregated.transactions_per_second / final_load_stage_tps:.1%} |")
            lines.append("")
        return lines

    def write_measurement_rows(self, measurements):
        """Append the measured rows of one stage to both CSV files.

        Args:
            measurements: Transaction measurements of the finished stage.
        """

        aggregated_path = self.results_directory / STAGE_RESULTS_FILE_NAME
        transactions_path = self.results_directory / TRANSACTION_RESULTS_FILE_NAME
        field_names = list(asdict(measurements[AGGREGATED_TRANSACTION_NAME]))
        for path, rows in ((aggregated_path, [measurements[AGGREGATED_TRANSACTION_NAME]]),
                           (transactions_path, measurements.values())):
            write_header = not path.exists()
            with path.open("a", encoding="utf-8-sig", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=field_names)
                if write_header:
                    writer.writeheader()
                for measurement in rows:
                    writer.writerow(asdict(measurement))

    def evaluate_stage(self, stage, measurements, load_stage_tps, final_load_stage_tps):
        """Check one stage against the requirements of its kind.

        Args:
            stage: Measured stage.
            measurements: Transaction measurements of the stage.
            load_stage_tps: Transactions per second of the first working load stage.
            final_load_stage_tps: Transactions per second of the repeated load stage at the end
                of the run, or 0.0 when the plan has no such stage.

        Returns:
            List of (criterion, measured value, passed) tuples.
        """

        aggregated = measurements[AGGREGATED_TRANSACTION_NAME]
        connection_failures = measurements[DATABASE_CONNECTION_FAILURE_NAME].failures_count \
            if DATABASE_CONNECTION_FAILURE_NAME in measurements else 0

        if stage.kind == "load":
            requirements = LOAD_STAGE_REQUIREMENTS
            return [
                (f"TPS ≥ {requirements.minimum_transactions_per_second:,.0f}",
                 f"{aggregated.transactions_per_second:,.0f}",
                 aggregated.transactions_per_second >= requirements.minimum_transactions_per_second),
                (f"avg ≤ {requirements.maximum_average_milliseconds:.0f} мс",
                 f"{aggregated.average_milliseconds:.0f} мс",
                 aggregated.average_milliseconds <= requirements.maximum_average_milliseconds),
                (f"p95 ≤ {requirements.maximum_percentile_95_milliseconds:.0f} мс",
                 f"{aggregated.percentile_95_milliseconds:.0f} мс",
                 aggregated.percentile_95_milliseconds <= requirements.maximum_percentile_95_milliseconds),
                (f"p99 ≤ {requirements.maximum_percentile_99_milliseconds:.0f} мс",
                 f"{aggregated.percentile_99_milliseconds:.0f} мс",
                 aggregated.percentile_99_milliseconds <= requirements.maximum_percentile_99_milliseconds),
                (f"помилки < {requirements.maximum_error_percent}%", f"{aggregated.error_percent:.2f}%",
                 aggregated.error_percent < requirements.maximum_error_percent),
            ]
        if stage.kind == "stress":
            requirements = STRESS_STAGE_REQUIREMENTS
            minimum_tps = load_stage_tps * requirements.minimum_share_of_load_stage_tps
            return [
                (f"TPS ≥ {requirements.minimum_share_of_load_stage_tps:.0%} від рівня load ({minimum_tps:,.0f})",
                 f"{aggregated.transactions_per_second:,.0f}", aggregated.transactions_per_second >= minimum_tps),
                (f"p95 ≤ {requirements.maximum_percentile_95_milliseconds:,.0f} мс",
                 f"{aggregated.percentile_95_milliseconds:,.0f} мс",
                 aggregated.percentile_95_milliseconds <= requirements.maximum_percentile_95_milliseconds),
                (f"помилки < {requirements.maximum_error_percent}%", f"{aggregated.error_percent:.2f}%",
                 aggregated.error_percent < requirements.maximum_error_percent),
                (f"відмов у з'єднанні ≤ {requirements.maximum_connection_failures}", f"{connection_failures}",
                 connection_failures <= requirements.maximum_connection_failures),
            ]
        if stage.kind == "recovery":
            uses_final_baseline = final_load_stage_tps > 0
            baseline_tps = final_load_stage_tps if uses_final_baseline else load_stage_tps
            baseline_label = "повторного load" if uses_final_baseline else "першого load"
            minimum_tps = baseline_tps * RECOVERY_STAGE_REQUIREMENTS.minimum_share_of_load_stage_tps
            return [
                (f"TPS ≥ {RECOVERY_STAGE_REQUIREMENTS.minimum_share_of_load_stage_tps:.0%} від рівня "
                 f"{baseline_label} ({minimum_tps:,.0f})", f"{aggregated.transactions_per_second:,.0f}",
                 aggregated.transactions_per_second >= minimum_tps),
            ]
        return []

    def write_reports(self):
        """Write the Markdown verdict table of the run and print it to the console."""

        load_stage_tps = next((measurements[AGGREGATED_TRANSACTION_NAME].transactions_per_second
                               for stage, measurements in self.stage_measurements if stage.kind == "load"), 0.0)
        final_load_stage_tps = next((measurements[AGGREGATED_TRANSACTION_NAME].transactions_per_second
                                     for stage, measurements in self.stage_measurements
                                     if stage.kind == FINAL_LOAD_STAGE_KIND), 0.0)
        summary_lines = ["# Перевірка вимог до Load Testing", "",
                         "| Етап | Клієнти | TPS | avg, мс | p95, мс | p99, мс | Помилки, % | Вердикт |",
                         "|---|---|---|---|---|---|---|---|"]
        detail_lines = ["", "## Деталі за критеріями", ""]
        stage_reports = []
        for stage, measurements in self.stage_measurements:
            aggregated = measurements[AGGREGATED_TRANSACTION_NAME]
            checks = self.evaluate_stage(stage, measurements, load_stage_tps, final_load_stage_tps)
            stage_reports.append((stage, aggregated, checks))
            verdict = "—" if not checks else (PASSED_MARK if all(passed for _, _, passed in checks) else FAILED_MARK)
            summary_lines.append(
                f"| {stage.name} | {stage.concurrent_clients} | {aggregated.transactions_per_second:,.0f} | "
                f"{aggregated.average_milliseconds:.0f} | {aggregated.percentile_95_milliseconds:.0f} | "
                f"{aggregated.percentile_99_milliseconds:.0f} | {aggregated.error_percent:.2f} | {verdict} |")
            detail_lines.append(f"### {stage.name} — {stage.description}")
            if checks:
                detail_lines.extend(f"- {PASSED_MARK if passed else FAILED_MARK} {criterion} → {measured}"
                                    for criterion, measured, passed in checks)
            else:
                detail_lines.append("- етап довідковий, вимог немає")
            detail_lines.append("")
        self.write_dataset_growth_rows()
        drift_lines = self.build_baseline_drift_lines(load_stage_tps, final_load_stage_tps, stage_reports)
        report_text = "\n".join(summary_lines + detail_lines + drift_lines)
        (self.results_directory / REQUIREMENTS_REPORT_FILE_NAME).write_text(report_text, encoding="utf-8")
        print("\n" + report_text)
        saturation_report_text = write_saturation_report(self.results_directory, stage_reports, load_stage_tps)
        if saturation_report_text:
            print("\n" + saturation_report_text)
        print(f"Результати: {self.results_directory.resolve()}")
