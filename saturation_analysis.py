import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from load_test_plan import SATURATION_ANALYSIS_SETTINGS

SATURATION_CURVE_FILE_NAME = "saturation_curve.csv"
SATURATION_REPORT_FILE_NAME = "saturation_curve.md"
CURVE_STAGE_KINDS = ("scale", "load", "stress")
RECOVERY_STAGE_KIND = "recovery"
THROUGHPUT_BAR_WIDTH = 24
THROUGHPUT_BAR_GLYPH = "█"
PASSED_MARK = "✔"
FAILED_MARK = "✘"


@dataclass(frozen=True)
class SaturationCurvePoint:
    """One point of the throughput curve: what the database gave at this client count."""

    stage: str
    clients: int
    transactions_per_second: float
    share_of_load_stage: float
    transactions_per_second_per_client: float
    average_milliseconds: float
    little_law_milliseconds: float
    percentile_95_milliseconds: float
    percentile_99_milliseconds: float
    error_percent: float
    # None на етапах виду "scale": вони формують криву, але вимог до них немає.
    meets_requirements: bool | None


def build_saturation_curve(stage_reports, load_stage_tps):
    """Turn the measured load and stress stages into points of the throughput curve.

    Args:
        stage_reports: List of (stage, aggregated measurement, requirement checks) tuples.
        load_stage_tps: Transactions per second of the working load stage.

    Returns:
        Curve points sorted by client count.
    """

    points = []
    for stage, aggregated, checks in stage_reports:
        if stage.kind not in CURVE_STAGE_KINDS:
            continue
        transactions_per_second = aggregated.transactions_per_second
        points.append(SaturationCurvePoint(
            stage=stage.name,
            clients=stage.concurrent_clients,
            transactions_per_second=transactions_per_second,
            share_of_load_stage=transactions_per_second / load_stage_tps if load_stage_tps else 0.0,
            transactions_per_second_per_client=transactions_per_second / stage.concurrent_clients,
            average_milliseconds=aggregated.average_milliseconds,
            little_law_milliseconds=stage.concurrent_clients / transactions_per_second * 1000
            if transactions_per_second else 0.0,
            percentile_95_milliseconds=aggregated.percentile_95_milliseconds,
            percentile_99_milliseconds=aggregated.percentile_99_milliseconds,
            error_percent=aggregated.error_percent,
            meets_requirements=all(passed for _, _, passed in checks) if checks else None,
        ))
    return sorted(points, key=lambda point: point.clients)


def find_peak_point(points):
    """Find the stage with the highest throughput of the whole ladder.

    Args:
        points: Curve points sorted by client count.

    Returns:
        Curve point with the highest transactions per second, or None for an empty curve.
    """

    return max(points, key=lambda point: point.transactions_per_second) if points else None


def find_knee_point(points, peak_point):
    """Find the last point of the throughput plateau and the first point past it.

    The knee is the largest client count whose throughput still holds within
    `knee_drop_share` of the peak; beyond it the database gives measurably less.

    Args:
        points: Curve points sorted by client count.
        peak_point: Curve point with the highest transactions per second.

    Returns:
        Tuple of the knee point and the first point past the knee, which is None
        when the plateau lasts to the end of the ladder.
    """

    if peak_point is None:
        return None, None
    minimum_plateau_tps = peak_point.transactions_per_second * (1 - SATURATION_ANALYSIS_SETTINGS.knee_drop_share)
    knee_point = peak_point
    for point in points:
        if point.clients <= peak_point.clients:
            continue
        if point.transactions_per_second < minimum_plateau_tps:
            return knee_point, point
        knee_point = point
    return knee_point, None


def find_maximum_capacity_point(points):
    """Find the largest client count up to which every stage still meets its requirements.

    Points of the rising branch carry no requirements and neither break the chain nor extend it.

    Args:
        points: Curve points sorted by client count.

    Returns:
        Last curve point of the unbroken passing prefix, or None when even the first point fails.
    """

    maximum_capacity_point = None
    for point in points:
        if point.meets_requirements is None:
            continue
        if not point.meets_requirements:
            break
        maximum_capacity_point = point
    return maximum_capacity_point


def format_requirements_mark(meets_requirements):
    """Format the requirement verdict of one curve point.

    Args:
        meets_requirements: True, False, or None when the stage has no requirements.

    Returns:
        Verdict mark for the Markdown table.
    """

    if meets_requirements is None:
        return "—"
    return PASSED_MARK if meets_requirements else FAILED_MARK


def build_throughput_bar(point, peak_point):
    """Draw the throughput of one point as a bar relative to the peak of the ladder.

    Args:
        point: Curve point to draw.
        peak_point: Curve point with the highest transactions per second.

    Returns:
        Bar of block glyphs, at least one glyph wide for a non-zero throughput.
    """

    if not peak_point or not peak_point.transactions_per_second:
        return ""
    width = round(point.transactions_per_second / peak_point.transactions_per_second * THROUGHPUT_BAR_WIDTH)
    return THROUGHPUT_BAR_GLYPH * max(1, width) if point.transactions_per_second else ""


def write_curve_rows(results_directory, points):
    """Write every curve point to the CSV file of the run.

    Args:
        results_directory: Directory of this run.
        points: Curve points sorted by client count.
    """

    field_names = list(asdict(points[0]))
    with (Path(results_directory) / SATURATION_CURVE_FILE_NAME).open("w", encoding="utf-8-sig",
                                                                     newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=field_names)
        writer.writeheader()
        for point in points:
            writer.writerow(asdict(point))


def build_curve_table(points, peak_point):
    """Build the Markdown table of the throughput curve.

    Args:
        points: Curve points sorted by client count.
        peak_point: Curve point with the highest transactions per second.

    Returns:
        List of Markdown lines.
    """

    lines = ["| Етап | Клієнти | TPS | % від load | TPS на клієнта | avg, мс | N/X, мс | p95, мс | p99, мс |"
             " Помилки, % | Вимоги | Пропускна здатність |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for point in points:
        lines.append(
            f"| {point.stage} | {point.clients} | {point.transactions_per_second:,.0f} | "
            f"{point.share_of_load_stage:.1%} | {point.transactions_per_second_per_client:.2f} | "
            f"{point.average_milliseconds:.0f} | {point.little_law_milliseconds:.0f} | "
            f"{point.percentile_95_milliseconds:.0f} | {point.percentile_99_milliseconds:.0f} | "
            f"{point.error_percent:.2f} | {format_requirements_mark(point.meets_requirements)} | "
            f"{build_throughput_bar(point, peak_point)} |")
    return lines


def build_verdict_lines(points, peak_point, recovery_report, load_stage_tps):
    """Build the conclusion of the saturation analysis.

    Args:
        points: Curve points sorted by client count.
        peak_point: Curve point with the highest transactions per second.
        recovery_report: (stage, aggregated measurement, checks) of the recovery stage, or None.
        load_stage_tps: Transactions per second of the working load stage.

    Returns:
        List of Markdown lines.
    """

    knee_point, first_point_past_knee = find_knee_point(points, peak_point)
    maximum_capacity_point = find_maximum_capacity_point(points)
    lines = ["", "## Висновок", ""]
    lines.append(f"- **Пік пропускної здатності** — етап `{peak_point.stage}`: "
                 f"{peak_point.transactions_per_second:,.0f} TPS на {peak_point.clients} клієнтах.")
    if peak_point is points[0]:
        lines.append(f"- ⚠ Пік припав на найнижчу точку сходів, тож висхідна гілка кривої не виміряна: "
                     f"насичення настає раніше за {peak_point.clients} клієнтів. Сходи треба продовжити вниз.")
    if first_point_past_knee is None:
        lines.append(f"- **Точка перегину** не досягнута: до {knee_point.clients} клієнтів включно пропускна "
                     f"здатність тримається в межах {SATURATION_ANALYSIS_SETTINGS.knee_drop_share:.0%} від піку. "
                     f"Сходи треба продовжити вище.")
    else:
        lines.append(f"- **Точка перегину** — {knee_point.clients} клієнтів "
                     f"({knee_point.transactions_per_second:,.0f} TPS, {knee_point.share_of_load_stage:.1%} від "
                     f"load). Уже на {first_point_past_knee.clients} клієнтах TPS падає до "
                     f"{first_point_past_knee.transactions_per_second:,.0f}, а avg росте з "
                     f"{knee_point.average_milliseconds:.0f} до "
                     f"{first_point_past_knee.average_milliseconds:.0f} мс.")
    if maximum_capacity_point is None:
        lines.append("- **Максимальна робоча ємність** не визначена: вимоги не виконані вже на першому етапі сходів.")
    else:
        lines.append(f"- **Максимальна робоча ємність** — {maximum_capacity_point.clients} клієнтів: це найбільше "
                     f"навантаження, до якого включно всі етапи мають {PASSED_MARK}.")
    if recovery_report:
        _, recovery_aggregated, recovery_checks = recovery_report
        recovery_share = recovery_aggregated.transactions_per_second / load_stage_tps if load_stage_tps else 0.0
        lines.append(f"- **Відновлення після сходів** — {recovery_aggregated.transactions_per_second:,.0f} TPS "
                     f"({recovery_share:.1%} від load), avg {recovery_aggregated.average_milliseconds:.0f} мс, "
                     f"p95 {recovery_aggregated.percentile_95_milliseconds:.0f} мс: "
                     f"{PASSED_MARK if all(passed for _, _, passed in recovery_checks) else FAILED_MARK}")
    return lines


def build_legend_lines():
    """Build the explanation of the columns and of the verdict terms.

    Returns:
        List of Markdown lines.
    """

    return [
        "", "## Як читаються цифри", "",
        "- **% від load** — частка від базового етапу `load_500`; вимога до stress-етапів — не нижче 70%.",
        "- **TPS на клієнта** — скільки транзакцій за секунду дає один клієнт. Падіння цієї величини означає, "
        "що клієнти стоять у черзі, а не працюють.",
        "- **N/X, мс** — очікувана латентність за законом Літтла для закритої моделі з нульовим think time: "
        "кількість клієнтів поділена на TPS. Збіг із виміряним avg підтверджує, що черга стоїть на сервері, "
        "а не в генераторі навантаження.",
        "- **Точка перегину** — остання точка, де TPS ще тримається в межах "
        f"{SATURATION_ANALYSIS_SETTINGS.knee_drop_share:.0%} від піку.",
        "- **Максимальна робоча ємність** — найбільша кількість клієнтів, до якої включно жоден етап "
        "не порушив вимог.",
        "- **Вимоги** = «—» на етапах висхідної гілки (`scale_*`): вони нижчі за робоче навантаження, "
        "вимог до них немає, вони потрібні лише щоб побачити, де крива виходить на насичення.",
        "- Крива вимірюється в одному прогоні, тож усі точки бачать однаковий стан БД: порівнювати їх між собою "
        "коректно, а з точками іншого прогону — ні, бо транзакції T5 і T6 дописують рядки під час тесту.",
    ]


def write_saturation_report(results_directory, stage_reports, load_stage_tps):
    """Write the scalability curve of the run as CSV and Markdown, and return its text.

    Args:
        results_directory: Directory of this run.
        stage_reports: List of (stage, aggregated measurement, requirement checks) tuples.
        load_stage_tps: Transactions per second of the working load stage.

    Returns:
        Markdown text of the report, or an empty string when the ladder has no measured points.
    """

    points = build_saturation_curve(stage_reports, load_stage_tps)
    if not points:
        return ""
    peak_point = find_peak_point(points)
    recovery_report = next((report for report in stage_reports if report[0].kind == RECOVERY_STAGE_KIND), None)
    report_lines = [
        "# Крива насичення БД parcel_delivery", "",
        f"Сходи навантаження від {points[0].clients} до {points[-1].clients} клієнтів в одному прогоні: "
        "шукаємо, де саме система «просіла».", "",
        *build_curve_table(points, peak_point),
        *build_verdict_lines(points, peak_point, recovery_report, load_stage_tps),
        *build_legend_lines(),
    ]
    report_text = "\n".join(report_lines)
    write_curve_rows(results_directory, points)
    (Path(results_directory) / SATURATION_REPORT_FILE_NAME).write_text(report_text, encoding="utf-8")
    return report_text
